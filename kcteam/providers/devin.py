"""Devin API (v1) als asynchroner Anbieter für die Curriculum-Erzeugung.

Devin arbeitet in Sessions: `POST /v1/sessions` liefert sofort eine
`session_id`, das Ergebnis liegt erst Minuten später als `structured_output`
unter `GET /v1/sessions/{id}`. `complete()` ist trotzdem die gewohnte
synchrone Schnittstelle — nur eben über Worker-Läufe verteilt:

- kein Session-Eintrag   → Session anlegen, Kennung speichern, `ProviderPending`
- Eintrag, läuft noch    → `ProviderPending` (der Worker legt den Auftrag zurück)
- Eintrag, `finished`    → `structured_output` als Completion zurückgeben
- Eintrag, `failed` usw. → kontrollierter `ProviderError`

Damit ein Neustart keine zweite Session anlegt, steht die Zuordnung in
`curriculum.provider_sessions`, indiziert über den Fingerabdruck des
Aufrufs (Anbieter + Modell + Prompts). Derselbe Auftrag erzeugt denselben
Prompt, findet also seine Session wieder; ein überarbeiteter Prompt
(Rückmeldung des Inspektors) ist ein neuer Auftrag und bekommt eine neue
Session — das ist gewollt.

An Devin gehen nur die Auftragsdaten, die der Runner ohnehin in den Prompt
baut (Fach, Klasse, Thema, Schema). Der Prompt wird nie geloggt, der
Schlüssel nie — er steckt ausschließlich im Authorization-Header des Clients.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from typing import Any

from .base import Completion, Provider, ProviderError, ProviderPending

log = logging.getLogger("kcteam.devin")

# Devin hat eine VM mit Werkzeugen — die Session soll nichts davon benutzen.
# Der Auftrag kommt sonst zurück mit „habe eine Datei angelegt" statt JSON.
_PREAMBLE = """Du erzeugst Lerninhalte für eine Kinder-Lern-App — als reiner Inhalts-Generator.

ARBEITSREGELN, strikt:
- Liefere ausschließlich das geforderte JSON-Ergebnis als structured output.
- Keine Code-Änderungen, keine Dateien, kein Repository, keine Git-Aktionen, keine Shell-Befehle.
- Keine Rückfragen: fehlende Angaben ergänzt du fachlich sinnvoll.
- Keine erfundenen oder echten personenbezogenen Daten.
- Das Ergebnis muss exakt dem im Prompt genannten JSON-Schema entsprechen.
"""

_NUDGE = ("Bitte ohne Rückfragen weiterarbeiten und das geforderte JSON-Ergebnis jetzt als "
          "structured output abgeben. Es sind keinerlei Datei-, Git- oder Shell-Aktionen nötig.")

# `POST /sessions` lehnt Prompts ab 30 000 Zeichen mit 400. Die Agenten
# betten das JSON-Schema der Antwort noch einmal in den Prompt, obwohl es
# als `structured_output_schema` ohnehin maschinell erzwungen wird — die
# Prompt-Kopie ist redundant und allein ihr Fehlen reicht regelmaessig, um
# unter die Grenze zu kommen.
_PROMPT_LIMIT = 29_500
_SCHEMA_ABSCHNITT = "## JSON-Schema deiner Antwort"


def _schema_block_loeschen(prompt: str) -> str:
    """Entfernt den eingebetteten Schema-Block aus einem call_json-Prompt.

    Der Abschnitt endet am schliessenden Codezaun. Fehlt Marker oder Zaun,
    bleibt der Prompt unangetastet — lieber ein ehrlicher Fehler als eine
    still gekuerzte Lektionsvorgabe.
    """
    kopf, trenner, rest = prompt.partition(_SCHEMA_ABSCHNITT)
    if not trenner:
        return prompt
    ende = rest.find("```", rest.find("```") + 3)
    if ende < 0:
        return prompt
    return (kopf + rest[ende + 3:]).strip()

#: Statuswerte der v1-API, bei denen die Session noch arbeitet.
_LAEUFT = {"working", "running", "suspend_requested", "resumed", "resume_requested", "queued", "new"}
#: Danach wird die Session aufgegeben — sonst wartet ein Auftrag für immer.
_FERTIG, _BLOCKIERT = "finished", "blocked"


def _key(provider: str, model: str, system: str, user: str) -> str:
    """Fingerabdruck des Aufrufs — identischer Auftrag findet seine Session wieder."""
    return hashlib.sha256("\x00".join([provider, model, system, user]).encode()).hexdigest()


class DevinProvider(Provider):
    def __init__(self, settings: dict):
        super().__init__(name="devin", settings=settings, required_env=("DEVIN_API_KEY",))
        self.structured_output_native = True
        #: Grenze fuer den ganzen Auftragsprompt plus der Rahmen, den die API
        #: selbst dazulegt — der Auftraggeber passt seine Nutzdaten daran an,
        #: statt sie erst hier messen zu lassen.
        self.prompt_limit = _PROMPT_LIMIT
        self.prompt_overhead = len(_PREAMBLE) + len("\n\n") + len("\n\n## Aufgabe\n")
        self.base_url = (settings.get("base_url") or "https://api.devin.ai/v1").rstrip("/")
        self.poll_seconds = int(settings.get("poll_seconds", 300))
        self.max_session_seconds = int(settings.get("max_session_seconds", 7200))
        self.timeout_s = float(settings.get("timeout_s", 60))
        # Getrennt: ein langsamer Connect soll nicht die ganze Lesezeit fressen,
        # und ein haengender Server darf den Worker nicht unbegrenzt halten.
        self.connect_timeout_s = float(settings.get("connect_timeout_s", 10))
        self.max_restarts = int(settings.get("max_session_restarts", 1))
        self.max_acu = settings.get("max_acu_limit")          # optionaler Kostenrahmen pro Session
        self._db = None
        self._mem: dict[str, dict] = {}                        # Ersatz ohne Datenbank (Tests)
        self._http = None

    def bind_db(self, db) -> None:
        """Die Pipeline gibt ihren Datenbank-Handle durch — Sessions überleben Neustarts."""
        self._db = db

    # ------------------------------------------------------------- HTTP
    @property
    def client(self):
        if self._http is None:
            import os
            import httpx
            key = os.environ.get("DEVIN_API_KEY")
            if not key:
                raise ProviderError("DEVIN_API_KEY fehlt (Provider 'devin' ist gewählt)", retryable=False)
            self._http = httpx.Client(
                base_url=self.base_url,
                headers={"Authorization": f"Bearer {key}"},
                timeout=httpx.Timeout(self.timeout_s,
                                      connect=self.connect_timeout_s))
        return self._http

    def _api(self, method: str, path: str, payload: dict | None = None) -> dict:
        """Ein API-Aufruf mit einheitlicher Fehler-Einordnung. Nie Header oder Body loggen."""
        import httpx
        started = time.monotonic()
        try:
            r = self.client.request(method, path, json=payload)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise ProviderError(f"Devin API {method} {path}: {exc.__class__.__name__}",
                                retryable=True) from exc
        ms = round((time.monotonic() - started) * 1000)
        if r.status_code < 400:
            try:
                return r.json()
            except ValueError as exc:
                raise ProviderError(f"Devin API {method} {path}: ungültige Antwort",
                                    retryable=False) from exc
        if r.status_code in (401, 403):
            raise ProviderError(f"Devin API {method} {path}: {r.status_code} (Schlüssel ungültig?)",
                                retryable=False)
        if r.status_code == 429:
            try:
                ra = float(r.headers.get("retry-after", ""))
            except ValueError:
                ra = None
            raise ProviderError(f"Devin API {method} {path}: 429 Rate-Limit",
                                rate_limited=True, retryable=True, retry_after=ra)
        if r.status_code >= 500:
            raise ProviderError(f"Devin API {method} {path}: {r.status_code} Serverfehler ({ms} ms)",
                                retryable=True)
        raise ProviderError(f"Devin API {method} {path}: {r.status_code}", retryable=False)

    # ------------------------------------------------------------- Session-Zustand
    def _row(self, key: str) -> dict | None:
        if self._db is None:
            return self._mem.get(key)
        return self._db.one("SELECT * FROM curriculum.provider_sessions WHERE call_key=%s", (key,))

    def _save(self, key: str, sid: str, meta: dict) -> None:
        if self._db is None:
            self._mem[key] = {"call_key": key, "session_id": sid, "status": "working",
                              "nudged": False, "restarts": 0, "detail": None,
                              "role": meta.get("role"), "entity_id": meta.get("entity_id"),
                              "created_ts": time.time()}
            return
        self._db.query("""INSERT INTO curriculum.provider_sessions
                            (call_key, provider, session_id, role, entity_id, status)
                          VALUES (%s, 'devin', %s, %s, %s, 'working')
                          ON CONFLICT (call_key) DO NOTHING""",
                       (key, sid, meta.get("role"), meta.get("entity_id")))

    def _mark(self, key: str, status: str, **fields) -> None:
        import json as _json
        if self._db is None:
            row = self._mem.get(key)
            if row is not None:
                row.update(status=status, **fields)
            return
        fields = {k: (_json.dumps(v, ensure_ascii=False) if k == "result" else v)
                  for k, v in fields.items()}
        sets = ", ".join(f"{k}=%s" for k in fields)
        self._db.query(f"UPDATE curriculum.provider_sessions SET status=%s, updated_at=now()"
                       + (f", {sets}" if sets else "") + " WHERE call_key=%s",
                       (status, *fields.values(), key))

    def _restarted(self, key: str, sid: str) -> None:
        if self._db is None:
            row = self._mem.get(key)
            if row is not None:
                row.update(session_id=sid, status="working", nudged=False,
                           restarts=row.get("restarts", 0) + 1, created_ts=time.time())
            return
        self._db.query("""UPDATE curriculum.provider_sessions
                             SET session_id=%s, status='working', nudged=false,
                                 restarts=restarts+1, updated_at=now()
                           WHERE call_key=%s""", (sid, key))

    # ------------------------------------------------------------- Devin-Aktionen
    def _create(self, system: str, user: str, meta: dict) -> str:
        """Session anlegen. Gibt die session_id zurück."""
        payload: dict[str, Any] = {
            "prompt": _PREAMBLE + "\n\n" + system + "\n\n## Aufgabe\n" + user,
            "idempotent": True,           # ein Create-Timeout legt keine zweite Session an
            "unlisted": True,
            "title": ("kcteam " + str(meta.get("role") or "agent") + " "
                      + str(meta.get("entity_id") or "")).strip()[:120],
            "tags": ["karo-curriculum", str(meta.get("role") or "agent")][:8],
            "secret_ids": [],             # die Session bekommt keinerlei hinterlegte Zugänge
            "knowledge_ids": [],          # und kein Organisations-Wissen fremder Aufträge
        }
        schema = meta.get("json_schema")
        if isinstance(schema, dict) and len(json.dumps(schema, default=str)) <= 60_000:
            payload["structured_output_schema"] = schema   # v1: JSON Schema (Draft 7), max 64 KB
        if self.max_acu:
            payload["max_acu_limit"] = int(self.max_acu)
        schema_native = "structured_output_schema" in payload
        if len(payload["prompt"]) > _PROMPT_LIMIT and schema_native:
            # Das Schema ist an die API ohnehin als Feld angeheftet; die
            # Prompt-Kopie darf weichen, bevor eine Vorgabe abgeschnitten wird.
            payload["prompt"] = _schema_block_loeschen(payload["prompt"])
        # Groesse protokollieren — nie Inhalt: nahe am Limit ist ein Frueh-
        # warnsignal, ueber dem Limit ein lokaler, klassifizierter Fehler.
        (log.warning if len(payload["prompt"]) > _PROMPT_LIMIT - 1_500 else log.info)(
            "devin_prompt_size session_role=%s entity_id=%s prompt_chars=%d schema_native=%s",
            meta.get("role"), meta.get("entity_id"),
            len(payload["prompt"]), schema_native)
        if len(payload["prompt"]) > _PROMPT_LIMIT - 1_500:
            log.warning("devin_prompt_split entity_id=%s system_chars=%d user_chars=%d",
                        meta.get("entity_id"), len(system), len(user))
        if len(payload["prompt"]) > _PROMPT_LIMIT:
            raise ProviderError(
                f"Devin API: Auftrag hat {len(payload['prompt'])} Zeichen — über der "
                f"Grenze von {_PROMPT_LIMIT}. Die Vorgabe wird nicht gekürzt, sondern "
                "als zu groß zurückgemeldet.", retryable=False)
        try:
            info = self._api("POST", "/sessions", payload)
        except ProviderError as exc:
            # Lehnt die API das Schema ab, lieber ohne es senden — die lokale
            # Prüfung bleibt ohnehin und das Schema steht meist noch im Prompt.
            if "structured_output_schema" in payload and "422" in str(exc):
                payload.pop("structured_output_schema")
                info = self._api("POST", "/sessions", payload)
            else:
                raise
        sid = info.get("session_id")
        if not sid:
            raise ProviderError("Devin API: Antwort ohne session_id", retryable=False)
        return sid

    # ------------------------------------------------------------- Schnittstelle
    def complete(self, *, system, user, model, web_search=False, meta=None) -> Completion:
        meta = meta or {}
        key = _key(self.name, model, system, user)
        row = self._row(key)
        if row is None:
            try:
                sid = self._create(system, user, meta)
            except ProviderError as exc:
                log.warning("devin_request_failed role=%s entity_id=%s attempt=%s error_type=create",
                            meta.get("role"), meta.get("entity_id"), meta.get("attempt"))
                raise exc
            self._save(key, sid, meta)
            log.info("devin_request_started session_id=%s role=%s entity_id=%s attempt=%s",
                     sid, meta.get("role"), meta.get("entity_id"), meta.get("attempt"))
            raise ProviderPending(f"Devin-Session {sid} läuft", wait_seconds=self.poll_seconds,
                                  session_id=sid)
        if row["status"] == "failed":
            raise ProviderError(f"Devin-Session {row['session_id']} aufgegeben: {row.get('detail')}",
                                retryable=False)
        if row["status"] == "finished":
            # Endzustand: das Ergebnis liegt lokal — kein Neubau, kein Poll,
            # auch wenn die Session remote laengst weg ist.
            result = row.get("result")
            if isinstance(result, (dict, list)):
                return Completion(text=json.dumps(result, ensure_ascii=False),
                                  model=model)
            if isinstance(result, str) and result:
                return Completion(text=result, model=model)
            # Rows aus der Zeit vor dem Ergebnis-Cache: einmal remote holen.
        started = self._age(row)
        if started > self.max_session_seconds:
            self._mark(key, "failed", detail="Zeitfenster überschritten")
            log.warning("devin_request_failed session_id=%s entity_id=%s error_type=max_age",
                        row["session_id"], meta.get("entity_id"))
            raise ProviderError(f"Devin-Session {row['session_id']} älter als "
                                f"{self.max_session_seconds}s — aufgegeben", retryable=False)

        try:
            info = self._api("GET", f"/sessions/{row['session_id']}")
            state = str(info.get("status_enum") or info.get("status") or "working").lower()
        except ProviderError as exc:
            if ": 404" not in str(exc):
                log.warning("devin_request_failed session_id=%s entity_id=%s error_type=poll",
                            row["session_id"], meta.get("entity_id"))
                raise exc
            # Lokale Zeile vorhanden, der Anbieter kennt die Session nicht
            # mehr — wie „expired": begrenzt neu starten, nie sofort scheitern.
            info, state = {}, "missing_remote"
            self._mark(key, row["status"],
                       missing_remote=int(row.get("missing_remote") or 0) + 1,
                       detail="missing_remote")
            log.warning("devin_session_missing session_id=%s entity_id=%s",
                        row["session_id"], meta.get("entity_id"))
        log.info("devin_request_status session_id=%s status=%s entity_id=%s",
                 row["session_id"], state, meta.get("entity_id"))

        out = info.get("structured_output")
        if state == _FERTIG or (state == _BLOCKIERT and isinstance(out, dict) and out):
            if not isinstance(out, dict) or not out:
                self._mark(key, "failed", detail="finished ohne structured_output")
                log.warning("devin_request_failed session_id=%s entity_id=%s error_type=invalid_output",
                            row["session_id"], meta.get("entity_id"))
                raise ProviderError(f"Devin-Session {row['session_id']} endete ohne "
                                    "structured_output", retryable=False)
            self._mark(key, "finished", result=out)
            log.info("devin_request_completed session_id=%s entity_id=%s",
                     row["session_id"], meta.get("entity_id"))
            return Completion(text=json.dumps(out, ensure_ascii=False), model=model)

        if state == _BLOCKIERT:
            if row.get("nudged"):
                self._mark(key, "failed", detail="dauerhaft blocked")
                log.warning("devin_request_failed session_id=%s entity_id=%s error_type=blocked",
                            row["session_id"], meta.get("entity_id"))
                raise ProviderError(f"Devin-Session {row['session_id']} wartet weiterhin auf "
                                    "Eingabe", retryable=False)
            # Einmal antworten: die Regeln verbieten Rückfragen, trotzdem kann
            # Devin blockieren — und auch diese Session kann weg sein.
            try:
                self._api("POST", f"/sessions/{row['session_id']}/message",
                          {"message": _NUDGE})
            except ProviderError as exc:
                if ": 404" not in str(exc):
                    raise
                state = "missing_remote"
                self._mark(key, "working",
                           missing_remote=int(row.get("missing_remote") or 0) + 1,
                           detail="missing_remote")
            else:
                self._mark(key, "working", nudged=True)
                raise ProviderPending(
                    f"Devin-Session {row['session_id']} nach Rückfrage fortgesetzt",
                    wait_seconds=self.poll_seconds, session_id=row["session_id"])

        if state in _LAEUFT:
            self._mark(key, "working")
            raise ProviderPending(f"Devin-Session {row['session_id']} läuft ({state})",
                                  wait_seconds=self.poll_seconds, session_id=row["session_id"])

        # expired / failed / missing_remote / unbekannt: begrenzt neu
        # versuchen, dann aufgeben — nie unbegrenzt Provider-Kosten laufen.
        if int(row.get("restarts") or 0) < self.max_restarts:
            sid = self._create(system, user, meta)
            self._restarted(key, sid)
            log.info("devin_request_started session_id=%s restart=%s entity_id=%s",
                     sid, row.get("restarts"), meta.get("entity_id"))
            raise ProviderPending(f"Devin-Session {sid} neu gestartet ({state})",
                                  wait_seconds=self.poll_seconds, session_id=sid)
        self._mark(key, "failed", detail=f"Status {state}")
        log.warning("devin_request_failed session_id=%s entity_id=%s error_type=session_%s",
                    row["session_id"], meta.get("entity_id"), state)
        raise ProviderError(f"Devin-Session {row['session_id']} fehlgeschlagen: {state}",
                            retryable=False)

    @staticmethod
    def _age(row: dict) -> float:
        ts = row.get("created_ts")
        if ts is not None:
            return time.time() - float(ts)
        created = row.get("created_at")
        if hasattr(created, "timestamp"):
            return time.time() - created.timestamp()
        return 0.0
