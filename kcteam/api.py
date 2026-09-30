"""Curriculum-Service über HTTP: so nutzen Abnehmer wie Karo den Curriculum-Agenten, ohne die Datenbank zu kennen.

Der Dienst ist nie im Klick eines Kindes blockierend: Sofortsuche und vorgehaltene Lektionen kommen direkt aus
der Datenbank (ein Aufruf, Millisekunden). Fehlt etwas, antwortet er 202 „pending“ und der Curriculum-Agent
arbeitet im Hintergrund. Der Abnehmer fragt später nach (oder bekommt einen Webhook).

    GET  /health
    POST /v1/resolve            Thema suchen / bestellen            (karo.resolve_topic)
    GET  /v1/requests/{id}      Stand eines Auftrags
    GET  /v1/concepts/{id}      geprüftes Konzept (ETag)
    POST /v1/learning-path      Lernpfad zu Zielen
    POST /v1/lessons            Lektion im Format des Abnehmers (ready | pending | unavailable)
    GET  /v1/lessons/{id}       Stand einer Lektion
    POST /v1/lessons/{id}/reject  Abnehmer verwirft die Lektion (seine eigene Prüfung) -> wird neu geschrieben

Anmeldung: `Authorization: Bearer <Schlüssel>` (kcteam api-client add). Der Schlüssel bestimmt die Einrichtung
(Tageslimit, Nachfrage). POST-Aufrufe dürfen `Idempotency-Key` mitschicken.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import time
from contextlib import asynccontextmanager
from datetime import date
from typing import Any, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field
from psycopg.types.json import Jsonb

from . import lessons, version
from .db import DB

KEY_PREFIX = "kc_"


# ---------------------------------------------------------------- Schlüssel
def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def add_client(db, name: str, tenant: str, webhook_url: str | None = None,
               key: str | None = None) -> tuple[dict, str]:
    """Legt einen Abnehmer an. Der Schlüssel wird nur hier einmal zurückgegeben und nie gespeichert."""
    if key is not None and (not key.startswith(KEY_PREFIX) or len(key) < 32):
        raise ValueError(f"Schlüssel muss mit {KEY_PREFIX} beginnen und mindestens 32 Zeichen lang sein")
    key = key or KEY_PREFIX + secrets.token_urlsafe(32)
    secret = secrets.token_urlsafe(32) if webhook_url else None
    row = db.one("""INSERT INTO curriculum.api_clients(name, tenant, key_prefix, key_hash, webhook_url, webhook_secret)
                    VALUES (%s,%s,%s,%s,%s,%s) RETURNING id, name, tenant, key_prefix, webhook_url, webhook_secret""",
                 (name, tenant, key[:10], hash_key(key), webhook_url, secret))
    return row, key


def list_clients(db) -> list[dict]:
    return db.query("""SELECT id, name, tenant, key_prefix, webhook_url IS NOT NULL AS webhook, active, created_at,
                              last_used_at FROM curriculum.api_clients ORDER BY id""")


def revoke_client(db, name: str) -> bool:
    return bool(db.query("UPDATE curriculum.api_clients SET active=false WHERE name=%s RETURNING id", (name,)))


# ---------------------------------------------------------------- Eingaben
class ResolveIn(BaseModel):
    subject: str = Field(min_length=1, max_length=80)
    grade: int = Field(ge=1, le=13)
    topic: str = Field(min_length=1, max_length=300)
    keywords: list[str] = Field(default_factory=list, max_length=20)
    tasks: list[str] = Field(default_factory=list, max_length=20)
    include_bundle: bool = False
    #: Bis wann der Abnehmer es braucht (Pruefungsdatum). Steuert nur die
    #: Reihenfolge der Schlange, nie den Inhalt. Ohne Angabe: nicht dringend.
    needed_by: date | None = None


class FormatIn(BaseModel):
    id: str
    schema_: dict = Field(alias="schema")
    registry: list[dict] = Field(default_factory=list)
    instructions: str = ""

    model_config = {"populate_by_name": True}


class LessonIn(BaseModel):
    subject: str = Field(min_length=1, max_length=80)
    grade: int = Field(ge=1, le=13)
    topic: str | None = Field(default=None, max_length=300)
    keywords: list[str] = Field(default_factory=list, max_length=20)
    tasks: list[str] = Field(default_factory=list, max_length=20)
    concept_id: str | None = Field(default=None, max_length=120)
    format: FormatIn
    #: Siehe ResolveIn.needed_by.
    needed_by: date | None = None


class PathIn(BaseModel):
    targets: list[str] = Field(min_length=1, max_length=50)
    mastered: list[str] = Field(default_factory=list, max_length=500)


class RejectIn(BaseModel):
    reason: str = Field(min_length=1, max_length=2000)
    #: Woran es lag. "content" = fachlich falsch und zaehlt gegen den
    #: Abnehmer; "contract" = Format, Version oder Pflichtfeld passt nicht
    #: und sagt nichts ueber den Inhalt. Aeltere Abnehmer schicken das Feld
    #: nicht — fuer die bleibt es bei "content" wie bisher.
    reason_code: Literal["content", "contract"] = "content"


# ---------------------------------------------------------------- App
def create_app(db: DB | None = None, webhooks: bool = False) -> FastAPI:
    """`webhooks`: Webhooks aus diesem Prozess senden. Im Betrieb sendet sie der Curriculum-Agent (genau ein
    Prozess) – die API läuft mit mehreren Prozessen und würde sonst jede Nachricht mehrfach schicken."""
    state: dict[str, Any] = {"db": db}

    @asynccontextmanager
    async def lifespan(_app):
        disp = None
        if webhooks:
            from .webhooks import Dispatcher
            disp = Dispatcher(get_db())
            disp.start()
        try:
            yield
        finally:
            if disp:
                disp.shutdown()
            if db is None and state["db"] is not None:
                state["db"].close_all()

    app = FastAPI(title="Karo Curriculum-Service", version="1", docs_url="/docs", redoc_url=None, lifespan=lifespan)
    touch: dict[int, float] = {}

    def get_db() -> DB:
        if state["db"] is None:
            state["db"] = DB(os.environ.get("DATABASE_URL", ""))
        return state["db"]

    def client(authorization: str | None = Header(default=None)) -> dict:
        if not authorization or not authorization.lower().startswith("bearer "):
            raise HTTPException(401, "Authorization: Bearer <Schlüssel> fehlt",
                                headers={"WWW-Authenticate": "Bearer"})
        row = get_db().one("SELECT id, name, tenant FROM curriculum.api_clients WHERE key_hash=%s AND active",
                           (hash_key(authorization[7:].strip()),))
        if not row:
            raise HTTPException(401, "Schlüssel unbekannt oder gesperrt", headers={"WWW-Authenticate": "Bearer"})
        now = time.monotonic()
        if now - touch.get(row["id"], 0) > 60:          # höchstens einmal pro Minute schreiben
            touch[row["id"]] = now
            get_db().query("UPDATE curriculum.api_clients SET last_used_at=now() WHERE id=%s", (row["id"],))
        return row

    def idempotent(c: dict, key: str | None, endpoint: str, body_in: BaseModel, fn) -> JSONResponse:
        """Gleicher Schlüssel + gleicher Aufruf = gleiche Antwort. Vorläufige Antworten (202) werden nicht
        gespeichert – die Wiederholung soll den neuen Stand sehen. Andere Nutzlast mit demselben Schlüssel: 422."""
        d = get_db()
        rhash = hashlib.sha256((endpoint + body_in.model_dump_json()).encode()).hexdigest()
        if key:
            if len(key) > 200:
                raise HTTPException(400, "Idempotency-Key zu lang")
            old = d.one("""SELECT status_code, response, endpoint, request_hash FROM curriculum.api_idempotency
                           WHERE client_id=%s AND key=%s AND created_at > now() - interval '1 day'""", (c["id"], key))
            if old:
                if old["endpoint"] != endpoint or (old["request_hash"] and old["request_hash"] != rhash):
                    raise HTTPException(422, "Idempotency-Key wurde für einen anderen Aufruf verwendet")
                return JSONResponse(old["response"], old["status_code"], headers={"Idempotent-Replayed": "true"})
        code, body = fn()
        if key and code < 500 and code != 202:
            d.query("""INSERT INTO curriculum.api_idempotency(client_id, key, endpoint, status_code, response,
                         request_hash) VALUES (%s,%s,%s,%s,%s,%s)
                       ON CONFLICT (client_id, key) DO UPDATE SET endpoint=EXCLUDED.endpoint,
                         status_code=EXCLUDED.status_code, response=EXCLUDED.response,
                         request_hash=EXCLUDED.request_hash, created_at=now()
                       WHERE curriculum.api_idempotency.created_at <= now() - interval '1 day'""",
                    (c["id"], key, endpoint, code, Jsonb(body), rhash))
            if secrets.randbelow(200) == 0:      # gelegentlich aufräumen
                d.query("DELETE FROM curriculum.api_idempotency WHERE created_at < now() - interval '2 days'")
        headers = {"Retry-After": str(body["retry_after"])} if code == 202 and body.get("retry_after") else None
        return JSONResponse(body, code, headers=headers)

    @app.get("/health")
    def health():
        try:
            get_db().one("SELECT 1 AS ok")
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"status": "down", "error": type(exc).__name__}, 503)
        return {"status": "ok"}

    # ------------------------------------------------------------ Themen
    def _resolve(c: dict, body: ResolveIn | LessonIn) -> dict:
        topic = body.topic or ""
        from .ondemand import scrub
        try:
            r = get_db().one("SELECT karo.resolve_topic(%s,%s,%s,%s,%s,%s) AS r",
                             (body.subject, body.grade, topic, body.keywords, Jsonb(scrub(body.tasks)),
                              c["tenant"]))["r"]
        except Exception as exc:  # noqa: BLE001 – Eingabefehler aus der Datenbank (RAISE EXCEPTION)
            if type(exc).__name__ == "RaiseException":
                raise HTTPException(422, str(exc).splitlines()[0]) from None
            raise
        if r.get("request_id") and getattr(body, "needed_by", None):
            # Gleiche Anfragen werden zusammengelegt. Braucht eine Familie es
            # frueher als die, die zuerst bestellt hat, gilt das fruehere
            # Datum — sonst wartet die Arbeit am Freitag hinter „irgendwann".
            get_db().query("""UPDATE curriculum.topic_requests
                              SET needed_by = least(coalesce(needed_by, %s), %s), updated_at=now()
                              WHERE id=%s""", (body.needed_by, body.needed_by, r["request_id"]))
        return r

    @app.post("/v1/resolve")
    def resolve(body: ResolveIn, c: dict = Depends(client),
                idempotency_key: str | None = Header(default=None)):
        def run():
            r = _resolve(c, body)
            if body.include_bundle and r.get("status") in ("found", "other_level"):
                cid = r["concepts"][0]["concept_id"]
                r["bundle"] = get_db().one("SELECT karo.concept_bundle(%s) AS b", (cid,))["b"]
            if r.get("status") == "ordered":
                r["retry_after"] = 30
            return (202 if r.get("status") == "ordered" else 200), r
        return idempotent(c, idempotency_key, "resolve", body, run)

    @app.get("/v1/requests/{rid}")
    def request_status(rid: int, c: dict = Depends(client)):
        d = get_db()
        ok = d.one("""SELECT 1 AS ok FROM curriculum.topic_requests r WHERE r.id=%s AND (r.tenant=%s OR EXISTS
                        (SELECT 1 FROM curriculum.topic_demand t WHERE t.request_id=r.id AND t.tenant=%s))""",
                   (rid, c["tenant"], c["tenant"]))
        if not ok:
            raise HTTPException(404, "Auftrag nicht gefunden")
        return d.one("SELECT karo.request_status(%s) AS s", (rid,))["s"]

    @app.get("/v1/concepts/{cid}")
    def concept(cid: str, request: Request, c: dict = Depends(client)):
        d = get_db()
        row = d.one("SELECT version, updated_at FROM curriculum.concepts WHERE id=%s AND status='approved'", (cid,))
        if not row:
            raise HTTPException(404, "Konzept nicht gefunden oder nicht freigegeben")
        etag = f'"{cid}:{row["version"]}:{int(row["updated_at"].timestamp())}"'
        if request.headers.get("if-none-match") == etag:
            return Response(status_code=304, headers={"ETag": etag})
        b = d.one("SELECT karo.concept_bundle(%s) AS b", (cid,))["b"]
        return JSONResponse(json.loads(json.dumps(b, default=str)), headers={"ETag": etag,
                                                                              "Cache-Control": "private, max-age=60"})

    @app.post("/v1/learning-path")
    def learning_path(body: PathIn, c: dict = Depends(client)):
        rows = get_db().query("SELECT * FROM karo.learning_path(%s,%s)", (body.targets, body.mastered))
        return {"path": rows}

    # ------------------------------------------------------------ Lektionen
    def _link(eid: int | None, cid: int) -> None:
        if eid is None:
            return
        get_db().query("INSERT INTO curriculum.lesson_export_clients VALUES (%s,%s) ON CONFLICT DO NOTHING",
                       (eid, cid))

    @app.post("/v1/lessons")
    def lesson(body: LessonIn, c: dict = Depends(client), idempotency_key: str | None = Header(default=None)):
        try:
            spec = lessons.validate_spec({"id": body.format.id, "schema": body.format.schema_,
                                          "registry": body.format.registry, "instructions": body.format.instructions})
        except lessons.FormatInvalid as exc:
            raise HTTPException(422, str(exc)) from None
        if not body.concept_id and not body.topic:
            raise HTTPException(422, "topic oder concept_id angeben")

        def run():
            d = get_db()
            try:
                if body.concept_id:
                    row = lessons.request_export(d, client_id=c["id"], spec=spec, grade=body.grade,
                                                 concept_id=body.concept_id, topic=body.topic,
                                                 needed_by=body.needed_by)
                    _link(row["id"], c["id"])
                    return lessons.export_response(row, d)
                r = _resolve(c, body)
                st = r.get("status")
                if st in ("found", "other_level"):
                    row = lessons.request_export(d, client_id=c["id"], spec=spec, grade=body.grade,
                                                 concept_id=r["concepts"][0]["concept_id"], topic=body.topic,
                                                 needed_by=body.needed_by)
                elif st == "ordered":
                    row = lessons.request_export(d, client_id=c["id"], spec=spec, grade=body.grade,
                                                 topic=body.topic, request_id=r["request_id"],
                                                 needed_by=body.needed_by)
                else:
                    return 200, {"status": "unavailable", "reason_code": r.get("reason_code") or st,
                                 "request_id": r.get("request_id")}
            except lessons.FormatInvalid as exc:
                return 422, {"detail": str(exc)}
            _link(row["id"], c["id"])
            code, out = lessons.export_response(row, d)
            if r.get("request_id"):
                out["request_id"] = r["request_id"]
            return code, out
        return idempotent(c, idempotency_key, "lessons", body, run)

    def _own_export(eid: int, c: dict) -> dict:
        row = get_db().one("""SELECT e.* FROM curriculum.lesson_exports e JOIN curriculum.lesson_export_clients l
                                ON l.export_id=e.id AND l.client_id=%s WHERE e.id=%s""", (c["id"], eid))
        if not row:
            raise HTTPException(404, "Lektion nicht gefunden")
        return row

    # Lebenszeichen mit Stand: sonst laesst sich von aussen nicht sagen, ob
    # API, Agent und Browser aus demselben Image laufen (`kcteam doctor`).
    version.puls(get_db, "api")

    @app.get("/v1/meta")
    def meta():
        """Wer bin ich und nach welchem Vertrag rede ich?

        Ein Abnehmer fragt das, bevor er einen Auftrag stellt. Laufen die
        Vertragsfassungen auseinander, stellt er zurueck statt Lieferungen
        abzulehnen — sonst zaehlt der Dienst Ablehnungen, die nichts mit dem
        Inhalt zu tun haben, und sperrt das Thema dauerhaft.

        Ohne Schluessel erreichbar: die Angaben sind nicht vertraulich, und
        ein Abnehmer muss die Fassungen vergleichen koennen, bevor er sich
        anmeldet.
        """
        return {"contract_version": lessons.CONTRACT_VERSION,
                "git_sha": version.GIT_SHA,
                "formats": list(lessons.SUPPORTED_FORMATS)}

    @app.get("/v1/lessons/{eid}")
    def lesson_status(eid: int, c: dict = Depends(client)):
        code, out = lessons.export_response(_own_export(eid, c), get_db())
        headers = {"Retry-After": str(out["retry_after"])} if code == 202 else None
        return JSONResponse(out, code, headers=headers)

    @app.post("/v1/lessons/{eid}/reject")
    def lesson_reject(eid: int, body: RejectIn, c: dict = Depends(client)):
        """Die eigene Prüfung des Abnehmers hat die Lektion verworfen. Andere Abnehmer behalten ihre Fassung;
        dieser bekommt eine eigene, neu geschriebene – höchstens KCTEAM_MAX_CLIENT_REJECTS-mal, dann Mensch."""
        row = _own_export(eid, c)
        if row["status"] != "ready":
            raise HTTPException(409, f"Lektion ist {row['status']}, nicht ready")
        d = get_db()
        d.log_review(None, "export", f"EXP-{eid}", "lesson", "client", "rejected", 1,
                     [{"error": body.reason[:2000]}], c["name"])
        new = lessons.reject_by_client(d, eid, c["id"], body.reason, body.reason_code)
        if body.reason_code == "contract":
            # Kein Inhaltsproblem: das gehoert einem Menschen auf den Tisch,
            # nicht dem Lektionsautor. Und es sperrt das Thema nicht.
            d.enqueue_human("export", f"EXP-{eid}", "lesson",
                            f"Vertragsverstoss gemeldet von {c['name']}: {body.reason[:300]}",
                            {"content": row["lesson"], "raw": row["lesson"]}, kind="error")
        elif new.get("status") == "unavailable":
            d.enqueue_human("export", f"EXP-{eid}", "lesson",
                            f"{new.get('rejections')}× vom Abnehmer {c['name']} verworfen: {body.reason[:300]}",
                            {"content": row["lesson"], "raw": row["lesson"]}, kind="error")
        code, out = lessons.export_response(new, get_db())
        return JSONResponse(out, code)

    return app


app = None  # uvicorn kcteam.api:create_app --factory
