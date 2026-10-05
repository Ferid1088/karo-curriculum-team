"""Agenten-Aufrufe: Prompt bauen, KI fragen, JSON prüfen, protokollieren."""
from __future__ import annotations

import hashlib
import json
import random
import threading
import time
from pathlib import Path
from typing import Any, Callable, TypeVar

from pydantic import BaseModel, ValidationError

from . import kontingent, pause_store
from .kontingent import KontingentErschoepft
from .providers.base import Provider, ProviderError

PROMPT_DIR = Path(__file__).parent / "prompts"
T = TypeVar("T", bound=BaseModel)

ROLES = {
    "curriculum_analyst": "Curriculum-Analyst",
    "fachdidaktiker": "Fachdidaktiker",
    "niveau_kalibrierer": "Niveau-Kalibrierer",
    "diagnostiker": "Diagnostiker",
    "visual_didaktiker": "Visual-Didaktiker",
    "kritiker": "Kritiker",
    "kinderrechts_inspektor": "Kinderrechts-Inspektor",
    "curriculum_agent": "Curriculum-Agent",
    "lektionsautor": "Lektionsautor",
}
# Rollen, für die Karos Spezifikation relevant ist (spart Tokens bei den anderen)
SPEC_ROLES = {"curriculum_analyst", "fachdidaktiker", "diagnostiker", "visual_didaktiker", "curriculum_agent"}
ROLE_STAGE = {"curriculum_analyst": "curriculum", "fachdidaktiker": "graph", "niveau_kalibrierer": "calibration",
              "diagnostiker": "diagnostics", "visual_didaktiker": "visuals", "kritiker": "critic",
              "kinderrechts_inspektor": "inspection", "curriculum_agent": "match",
              "lektionsautor": "lesson"}

#: Obergrenze fuer die Rückmeldung im Auftrag — genug Raum fuer konkrete
#: Hinweise, nicht genug, um die Anbietergrenze zu sprengen.
FEEDBACK_MAX = 4_000


class BudgetExhausted(RuntimeError):
    """Lauf anhalten (Budget, Nutzungslimit, Abbruch). Der nächste Lauf macht an derselben Stelle weiter."""


class RateLimited(BudgetExhausted):
    pass


class RunStopped(BudgetExhausted):
    pass


class AgentFailed(RuntimeError):
    def __init__(self, msg: str, stage: str | None = None):
        super().__init__(msg)
        self.stage = stage


def extract_json(text: str) -> Any:
    """Holt das erste JSON-Objekt aus einer Antwort (toleriert ```json-Zäune und Vor-/Nachtext)."""
    start = text.find("{")
    if start < 0:
        raise ValueError("keine JSON-Objekt-Klammer gefunden")
    decoder = json.JSONDecoder()
    while start >= 0:
        try:
            obj, _ = decoder.raw_decode(text[start:])
            return obj
        except json.JSONDecodeError:
            start = text.find("{", start + 1)
    raise ValueError("kein gültiges JSON gefunden")


def compact(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), default=str)


class AgentRunner:
    def __init__(self, *, cfg, provider: Provider, db, run_id: str, karo_spec: str = "", team=None):
        self.cfg = cfg
        self.team = team
        self.provider = provider
        self.db = db
        self.run_id = run_id
        self.karo_spec = karo_spec
        self.stop = threading.Event()
        self._cooldown_until = 0.0     # gemeinsame Pause aller Worker nach einem Rate-Limit
        self._calls = 0
        self._lock = threading.Lock()
        self._prompt_cache: dict[tuple, str] = {}
        self._common = (PROMPT_DIR / "_common.md").read_text(encoding="utf-8")
        self._role_prompts = {r: (PROMPT_DIR / f"{r}.md").read_text(encoding="utf-8") for r in ROLES}

    @property
    def calls(self) -> int:
        return self._calls

    @property
    def budget(self) -> int:
        return int(self.cfg.p("max_agent_calls", 3000))

    def remaining(self) -> int:
        """Wie viele Modellaufrufe noch im Kontingent sind."""
        with self._lock:
            return max(0, self.budget - self._calls)

    def _count(self) -> None:
        if self.stop.is_set():
            raise RunStopped("Lauf wurde angehalten")
        with self._lock:
            self._calls += 1
            if self._calls > int(self.cfg.p("max_agent_calls", 3000)):
                self.stop.set()
                raise BudgetExhausted(f"Budget von {self.cfg.p('max_agent_calls')} Agenten-Aufrufen erreicht")

    def system_prompt(self, role: str, schema: type[BaseModel]) -> str:
        """Pro Rolle und Schema identisch -> einmal bauen, danach aus dem Zwischenspeicher (und Prompt-Caching
        beim Anbieter greift)."""
        key = (role, schema.__name__, id(self.team))
        if key not in self._prompt_cache:
            parts = [self._common, self._role_prompts[role]]
            if self.team is not None:
                parts.append(self.team.section(role))
            if self.karo_spec and role in SPEC_ROLES:
                parts.append("## Kontext: Karo-Spezifikation (Auszug)\n" + self.karo_spec)
            if role == "visual_didaktiker":
                from .visuals.examples import EXAMPLES
                sample = {k: EXAMPLES[k] for k in ("fraction_bar", "number_line", "labeled_diagram", "sentence_parts")}
                parts.append("## Beispiele für Katalog-Darstellungen (Format-Orientierung)\n```json\n"
                             + compact(sample) + "\n```")
            if not getattr(self.provider, "structured_output_native", False):
                parts.append("## JSON-Schema deiner Antwort\n```json\n" + compact(schema.model_json_schema()) + "\n```")
            self._prompt_cache[key] = "\n\n".join(parts)
        return self._prompt_cache[key]

    def _kontingent_pruefen(self) -> None:
        """Vor jedem Aufruf: ist die Tuer gerade zu?

        Hier steht der eine Riegel. Frueher lag das Wissen in einer
        Prozessvariablen — ein Neustart wusste nichts davon, und die API
        ohnehin nie. Jetzt fragt jeder Aufruf dieselbe Tabelle.
        """
        laufend = pause_store.aktiv(self.db, self.provider.name)
        if not laufend:
            return
        pause_store.verhindert_zaehlen(self.db, self.provider.name)
        raise KontingentErschoepft(
            f"Kontingent von {self.provider.name} erschoepft, Pause bis "
            f"{laufend['bis']:%d.%m. %H:%M} ({laufend['grund'][:120]})",
            bis=laufend["bis"], provider=self.provider.name)

    def _kontingent_merken(self, meldung: str) -> None:
        erkannt, bis = kontingent.erkennen(meldung)
        if not erkannt:
            return
        pause_store.setzen(self.db, self.provider.name, meldung, bis)
        stand = pause_store.stand(self.db, self.provider.name)
        raise KontingentErschoepft(
            f"Kontingent von {self.provider.name} erschoepft, Pause bis "
            f"{stand['bis']:%d.%m. %H:%M}", bis=stand["bis"], provider=self.provider.name)

    def _complete(self, *, role, system, prompt, model, web_search, meta, entity_id):
        """Anbieteraufruf mit Wiederholung bei vorübergehenden Fehlern (exponentiell + Zufall, retry-after)."""
        retries = int(self.cfg.p("provider_retries", 5))
        # Ist die Pause gerade abgelaufen, darf genau EIN Aufruf durch: der
        # Probeaufruf. Alle anderen warten weiter, bis er das Ergebnis kennt.
        probe = pause_store.probe_beanspruchen(self.db, self.provider.name)
        for attempt in range(retries + 1):
            if self.stop.is_set():
                raise RunStopped("Lauf wurde angehalten")
            if not probe:
                self._kontingent_pruefen()
            pause = self._cooldown_until - time.time()
            if pause > 0 and self.stop.wait(pause):
                raise RunStopped("Lauf wurde angehalten")
            started = time.time()
            try:
                ergebnis = self.provider.complete(system=system, user=prompt, model=model,
                                                  web_search=web_search, meta=meta)
            except ProviderError as exc:
                self.db.log_call(self.run_id, role, self.provider.name, model, entity_id, 0, 0,
                                 int((time.time() - started) * 1000), False, str(exc))
                # Erschoepftes Kontingent ist kein wiederholbarer Fehler: hier
                # endet der Versuch sofort, fuer alle Prozesse.
                self._kontingent_merken(str(exc))
                if not exc.retryable or attempt == retries:
                    if exc.rate_limited:
                        self.stop.set()
                        raise RateLimited(f"Nutzungslimit/Rate-Limit des Anbieters erreicht: {exc}") from exc
                    raise
                cap = float(self.cfg.p("max_retry_wait", 300))
                wait = min(cap, exc.retry_after or (2 ** attempt) * (10 if exc.rate_limited else 2))
                wait *= 0.8 + 0.4 * random.random()
                if exc.rate_limited:   # alle Worker halten an, statt weiter auf das Limit zu laufen
                    with self._lock:
                        self._cooldown_until = max(self._cooldown_until, time.time() + wait)
                if self.stop.wait(wait):   # Abbruch (Strg+C, Budget) beendet das Warten sofort
                    raise RunStopped("Lauf wurde angehalten")
            else:
                if probe:
                    # Der Probeaufruf kam durch — die Schlange laeuft weiter.
                    pause_store.probe_geglueckt(self.db, self.provider.name)
                return ergebnis, started
        raise ProviderError("unerreichbar")  # pragma: no cover

    def call(
        self,
        role: str,
        task: str,
        payload: dict[str, Any],
        schema: type[T],
        *,
        entity_id: str | None = None,
        feedback: str | None = None,
        web_search: bool = False,
        meta: dict[str, Any] | None = None,
        stage: str | None = None,
    ) -> T:
        meta = {"json_schema": schema.model_json_schema(), **(meta or {})}
        return self._call(role, task, payload, self.system_prompt(role, schema), schema.model_validate,
                          entity_id=entity_id, feedback=feedback, web_search=web_search, meta=meta, stage=stage)

    def _system_parts(self, role: str, json_schema: dict, extra_system: str) -> list[str]:
        parts = [self._common, self._role_prompts[role]]
        if self.team is not None:
            parts.append(self.team.section(role))
        if extra_system:
            parts.append(extra_system)
        if not getattr(self.provider, "structured_output_native", False):
            parts.append("## JSON-Schema deiner Antwort\n```json\n" + compact(json_schema) + "\n```")
        # Bei Providern mit `structured_output_native` wird das Schema als
        # API-Feld erzwungen; die Prompt-Kopie (bei Karos Lektionsformat
        # ~29k Zeichen) wuerde sonst die Vorgaben-Grenze sprengen — jede
        # Nachfrage mit Rückmeldung wächst noch einmal.
        return parts

    def system_laenge(self, role: str, json_schema: dict, extra_system: str = "") -> int:
        """Laenge des Systemprompts, wie `call_json` ihn zusammensetzt.

        Damit kann der Auftraggeber seine Nutzdaten in die Anbietergrenze
        einpassen, statt sie erst dort messen zu lassen."""
        return len("\n\n".join(self._system_parts(role, json_schema, extra_system)))

    def call_json(self, role: str, task: str, payload: dict[str, Any], json_schema: dict,
                  validate: Callable[[Any], Any], *, entity_id: str | None = None, feedback: str | None = None,
                  meta: dict[str, Any] | None = None, stage: str | None = None, extra_system: str = "") -> Any:
        """Wie `call`, aber mit einem fremden JSON-Schema (z. B. dem Lektionsformat eines Abnehmers).
        `validate` wirft ValueError, wenn die Antwort nicht passt – dann wird wie bei `call` nachgefragt."""
        key = (role, "json:" + hashlib.sha256(compact(json_schema).encode()).hexdigest()[:16], id(self.team),
               hashlib.sha256(extra_system.encode()).hexdigest()[:16])
        if key not in self._prompt_cache:
            self._prompt_cache[key] = "\n\n".join(
                self._system_parts(role, json_schema, extra_system))
        meta = {**(meta or {}), "json_schema": json_schema}
        return self._call(role, task, payload, self._prompt_cache[key], validate, entity_id=entity_id,
                          feedback=feedback, web_search=False, meta=meta, stage=stage)

    def _call(self, role, task, payload, system, parse, *, entity_id, feedback, web_search, meta, stage):
        stage = stage or ROLE_STAGE.get(role)
        user = f"## Auftrag\n{task}\n\n## Daten\n```json\n{compact(payload)}\n```"
        if feedback:
            # Die Rückmeldung darf den Auftrag nicht ohne Grenze wachsen
            # lassen — ein gesprächiger Inspektor sprengte sonst die
            # Anbietergrenze (EXP-201: 40.876 Zeichen).
            user += ("\n\n## Rückmeldung, die du vollständig umsetzen musst\n" + feedback[:FEEDBACK_MAX] +
                     "\n\nGib das vollständige, überarbeitete JSON-Objekt zurück.")
        model = self.cfg.model_for(self.provider.name, role)
        meta = {**(meta or {}), "role": role, "entity_id": entity_id, "feedback": feedback, "payload": payload}

        last_error = ""
        attempts = 1 + int(self.cfg.p("max_json_retries", 2))
        for attempt in range(attempts):
            self._count()
            prompt = user
            if last_error:
                prompt += ("\n\n## Deine letzte Antwort war ungültig\n" + last_error[:1500] +
                           "\nAntworte nur mit gültigem JSON nach Schema.")
            # Letzte Wache vor dem Versand: die Budget-Rechnung der Auftraggeber
            # modelliert den Auftrag — gemessen wird hier das Echte. Was die
            # Grenze ueberschreitet, wird als lokaler Befund gemeldet, nicht
            # erst beim Anbieter (EXP-205: 29.805 Zeichen trotz Budgetierung).
            limit = getattr(self.provider, "prompt_limit", None)
            if limit:
                groesse = (len(system) + len(prompt)
                           + int(getattr(self.provider, "prompt_overhead", 0) or 0))
                if groesse > limit:
                    raise AgentFailed(
                        f"{ROLES[role]}: Auftrag hat {groesse} Zeichen — ueber der Grenze von {limit} "
                        f"(lokal gemessen: system={len(system)} nutzer={len(prompt)}).",
                        stage=stage)
            try:
                comp, started = self._complete(role=role, system=system, prompt=prompt, model=model,
                                               web_search=web_search, meta={**meta, "attempt": attempt},
                                               entity_id=entity_id)
            except ProviderError as exc:
                raise AgentFailed(f"{ROLES[role]}: Anbieterfehler: {exc}", stage=stage) from exc
            ms = int((time.time() - started) * 1000)
            try:
                if comp.truncated:
                    raise ValueError("Die Antwort wurde wegen Längenbegrenzung abgeschnitten. Antworte kompakter: "
                                     "weniger und kürzere Aufgaben, keine unnötigen Felder.")
                obj = parse(extract_json(comp.text))
            except (ValueError, ValidationError) as exc:
                last_error = str(exc)
                self.db.log_call(self.run_id, role, self.provider.name, model, entity_id,
                                 comp.input_tokens, comp.output_tokens, ms, False, "ungültiges JSON: " + last_error)
                continue
            self.db.log_call(self.run_id, role, self.provider.name, model, entity_id,
                             comp.input_tokens, comp.output_tokens, ms, True)
            return obj
        raise AgentFailed(f"{ROLES[role]} lieferte nach {attempts} Versuchen kein gültiges Ergebnis: {last_error[:500]}",
                          stage=stage)
