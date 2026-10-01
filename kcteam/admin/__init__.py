"""Datenbank-Browser: Curriculum (Postgres) und Karo (SQLite) ansehen – fast nur lesen.

Sicherheit:
- Startet nicht ohne ADMIN_PASSWORD; HTTP Basic Auth (Benutzer ADMIN_USER, Standard „admin“).
- Postgres-Sitzungen sind READ ONLY (jede Transaktion), dazu ein Zeitlimit pro Abfrage.
- Zwei Ausnahmen, beide ohne Eingabe aus der URL und ueber eine eigene,
  schreibende Verbindung: „Wieder freigeben“ an einer Lektion, und das eigene
  Lebenszeichen (welcher Stand hier laeuft, fuer `kcteam doctor`).
- Karos SQLite-Datei wird mit mode=ro geöffnet (Volume zusätzlich :ro gemountet).
- Spalten mit Geheimnissen (password, secret, token, key_hash …) werden nie angezeigt.
- Tabellen- und Spaltennamen kommen nur aus dem Katalog der Datenbank, nie aus der URL in SQL.
- Kein CDN, keine externen Skripte; strenge Content-Security-Policy.
"""
from __future__ import annotations

import base64
import json
import os
import re
import secrets
import sqlite3
import weakref
from contextlib import closing
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates
from psycopg import sql as pgsql

from .. import lessons, version
from ..db import DB

HIDDEN = re.compile(r"pass(word)?|secret|token|key_hash|api_?key|credential", re.IGNORECASE)
PG_SCHEMAS = ("curriculum", "karo", "learner")
PAGE = 50
TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


class ReadOnlyDB(DB):
    """Wie DB, aber jede Verbindung ist schreibgeschützt und hat ein Zeitlimit."""

    def __init__(self, url: str):
        super().__init__(url)
        self._ro: weakref.WeakSet = weakref.WeakSet()

    def _conn(self):
        conn = super()._conn()
        if conn not in self._ro:
            with conn.cursor() as cur:
                cur.execute("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY")
                cur.execute("SET statement_timeout = '5s'")
            conn.commit()
            self._ro.add(conn)
        return conn


def _pretty(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False, indent=1, default=str)
    if isinstance(v, str) and v[:1] in "[{":
        try:
            return json.dumps(json.loads(v), ensure_ascii=False, indent=1)
        except ValueError:
            pass
    return str(v)


def _svg_uri(svg: str | None) -> str | None:
    """SVG nur als <img> (data:-URI): so wird kein Skript darin je ausgeführt."""
    if not svg or "<svg" not in svg:
        return None
    return "data:image/svg+xml;base64," + base64.b64encode(svg.encode()).decode()


TEMPLATES.env.filters["pretty"] = _pretty
TEMPLATES.env.filters["svg_uri"] = _svg_uri
TEMPLATES.env.filters["short"] = lambda v, n=80: (lambda s: s if len(s) <= n else s[:n] + "…")(
    _pretty(v).replace("\n", " "))


def create_app(db: DB | None = None, karo_db: str | None = None,
               write_db: DB | None = None) -> FastAPI:
    user = os.environ.get("ADMIN_USER", "admin")
    password = os.environ.get("ADMIN_PASSWORD", "")
    if not password:
        raise RuntimeError("ADMIN_PASSWORD ist nicht gesetzt")
    state: dict[str, Any] = {"db": db, "write_db": write_db}
    karo_path = karo_db or os.environ.get("KARO_DB_PATH", "/karo/karo.db")
    security = HTTPBasic(realm="Karo Datenbank-Browser")
    app = FastAPI(title="Karo Datenbank-Browser", docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def headers(request: Request, call_next):
        resp = await call_next(request)
        resp.headers["Content-Security-Policy"] = ("default-src 'none'; style-src 'unsafe-inline'; img-src data:; "
                                                   "form-action 'self'; frame-ancestors 'none'; base-uri 'none'")
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["Cache-Control"] = "no-store"
        return resp

    def auth(cred: HTTPBasicCredentials = Depends(security)) -> str:
        ok = secrets.compare_digest(cred.username.encode(), user.encode()) & \
            secrets.compare_digest(cred.password.encode(), password.encode())
        if not ok:
            raise HTTPException(401, "Anmeldung fehlgeschlagen", headers={"WWW-Authenticate": "Basic"})
        return cred.username

    def pg() -> DB:
        if state["db"] is None:
            state["db"] = ReadOnlyDB(os.environ.get("DATABASE_URL", ""))
        return state["db"]

    def pg_write() -> DB:
        """Nur für „Wieder freigeben“. Eine eigene Verbindung – die zum Lesen
        bleibt schreibgeschützt, sonst wäre die Zusage dieses Moduls hinfällig."""
        if state["write_db"] is None:
            state["write_db"] = DB(os.environ.get("DATABASE_URL", ""))
        return state["write_db"]

    def sq() -> sqlite3.Connection | None:
        if not Path(karo_path).exists():
            return None
        con = sqlite3.connect(f"file:{karo_path}?mode=ro", uri=True, timeout=2)
        con.row_factory = sqlite3.Row
        return con

    # Lebenszeichen mit Stand: sonst laeuft der Browser unbemerkt aus einem
    # aelteren Image weiter als API und Agent (`kcteam doctor`).
    version.puls(pg_write, "admin")

    def render(request: Request, name: str, **ctx) -> HTMLResponse:
        return TEMPLATES.TemplateResponse(request, name, {"karo_path": karo_path, **ctx})

    def pg_safe(fn, default=None):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 – Browser soll auch mit halber Datenbank funktionieren
            return default if default is not None else {"error": str(exc).splitlines()[0]}

    # ------------------------------------------------------------ Übersicht
    def kontingent_pause() -> dict | None:
        """Laeuft gerade eine Kontingent-Pause? Gehoert ganz oben hin.

        Ohne diesen Hinweis sieht ein stillstehender Dienst aus wie ein
        kaputter — und man sucht den Fehler an der falschen Stelle.
        """
        from .. import pause_store
        return pg_safe(lambda: pause_store.aktiv(pg(), "claude_token")
                       or pause_store.aktiv(pg(), "anthropic_api"), default=None)

    @app.get("/", response_class=HTMLResponse)
    def home(request: Request, _u: str = Depends(auth)):
        d = pg()
        cur = pg_safe(lambda: {
            "concepts": d.query("SELECT status, count(*) AS n FROM curriculum.concepts GROUP BY 1 ORDER BY 2 DESC"),
            "subjects": d.query("""SELECT s.code, s.name, s.status, count(c.id) AS n,
                                          count(c.id) FILTER (WHERE c.status='approved') AS approved
                                   FROM curriculum.subjects s LEFT JOIN curriculum.concepts c ON c.subject_code=s.code
                                   GROUP BY 1,2,3 ORDER BY 2"""),
            "requests": d.query("SELECT status, count(*) AS n FROM curriculum.topic_requests GROUP BY 1 ORDER BY 1"),
            "exports": d.query("SELECT status, count(*) AS n FROM curriculum.lesson_exports GROUP BY 1 ORDER BY 1"),
            "queue": d.one("""SELECT count(*) AS n, count(*) FILTER (WHERE urgent) AS urgent
                              FROM curriculum.human_queue WHERE status='open'"""),
        })
        karo = None
        con = sq()
        if con:
            with closing(con):
                karo = pg_safe(lambda: {
                    "konzepte": con.execute("""SELECT quelle, count(*) AS n, sum(geprueft_am IS NOT NULL) AS geprueft
                                               FROM lern_konzept GROUP BY 1 ORDER BY 2 DESC""").fetchall(),
                    "tabellen": len(_sq_tables(con)),
                })
        return render(request, "home.html", cur=cur, karo=karo,
                      pause=kontingent_pause())

    # ------------------------------------------------------------ Curriculum
    @app.get("/curriculum/subjects/{code}", response_class=HTMLResponse)
    def subject(request: Request, code: str, q: str = "", status: str = "", _u: str = Depends(auth)):
        d = pg()
        s = d.one("SELECT * FROM curriculum.subjects WHERE code=%s", (code,))
        if not s:
            raise HTTPException(404)
        blocks = d.query("SELECT * FROM curriculum.topic_blocks WHERE subject_code=%s ORDER BY grade_min, id", (code,))
        concepts = d.query("""SELECT id, block_id, title, target_grade, first_contact_grade, status, version
                              FROM curriculum.concepts WHERE subject_code=%s
                                AND (%s = '' OR status = %s)
                                AND (%s = '' OR title ILIKE %s OR id ILIKE %s OR description ILIKE %s)
                              ORDER BY target_grade, sort_order, id""",
                           (code, status, status, q, f"%{q}%", f"%{q}%", f"%{q}%"))
        by_block: dict[str, list] = {}
        for c in concepts:
            by_block.setdefault(c["block_id"], []).append(c)
        statuses = [r["status"] for r in d.query("SELECT DISTINCT status FROM curriculum.concepts ORDER BY 1")]
        return render(request, "subject.html", s=s, blocks=blocks, by_block=by_block, q=q, status=status,
                      statuses=statuses)

    @app.get("/curriculum/concepts/{cid}", response_class=HTMLResponse)
    def concept(request: Request, cid: str, _u: str = Depends(auth)):
        d = pg()
        c = d.concept(cid)
        if not c:
            raise HTTPException(404)
        ctx = {
            "c": c,
            "pre": d.query("""SELECT x.id, x.title, x.status FROM curriculum.concept_prerequisites p
                              JOIN curriculum.concepts x ON x.id=p.prerequisite_id WHERE p.concept_id=%s""", (cid,)),
            "post": d.query("""SELECT x.id, x.title, x.status FROM curriculum.concept_prerequisites p
                               JOIN curriculum.concepts x ON x.id=p.concept_id WHERE p.prerequisite_id=%s""", (cid,)),
            "mis": d.query("SELECT * FROM curriculum.misconceptions WHERE concept_id=%s ORDER BY key", (cid,)),
            "items": d.query("""SELECT * FROM curriculum.items WHERE concept_id=%s ORDER BY kind, sort_order""",
                             (cid,)),
            "visuals": d.query("SELECT * FROM curriculum.visual_explanations WHERE concept_id=%s ORDER BY sort_order",
                               (cid,)),
            "reviews": d.query("""SELECT created_at, stage, reviewer, decision, round, note, findings
                                  FROM curriculum.reviews WHERE entity_id=%s ORDER BY id DESC LIMIT 30""", (cid,)),
            "exports": d.query("""SELECT id, format_id, grade, status, concept_version, updated_at
                                  FROM curriculum.lesson_exports WHERE concept_id=%s ORDER BY id DESC""", (cid,)),
        }
        hide = {"search_fold", "description_fold"}
        ctx["fields"] = {k: v for k, v in c.items() if k not in hide and not HIDDEN.search(k)}
        return render(request, "concept.html", **ctx)

    @app.get("/curriculum/requests", response_class=HTMLResponse)
    def requests_(request: Request, status: str = "", _u: str = Depends(auth)):
        rows = pg().query("""SELECT id, status, stage, tenant, subject, grade, topic, requested_count, source,
                                    result_concepts, reason_code, message, created_at, updated_at
                             FROM curriculum.topic_requests WHERE (%s = '' OR status=%s)
                             ORDER BY id DESC LIMIT 200""", (status, status))
        return render(request, "list.html", title="Aufträge an den Curriculum-Agenten", rows=rows,
                      links={"result_concepts": "concepts"}, filter_status=status,
                      statuses=["queued", "running", "ready", "done", "blocked", "rejected", "failed"])

    @app.get("/curriculum/queue", response_class=HTMLResponse)
    def queue(request: Request, _u: str = Depends(auth)):
        rows = pg().query("""SELECT id, urgent, kind, entity_type, entity_id, stage, reason, created_at
                             FROM curriculum.human_queue WHERE status='open' ORDER BY urgent DESC, created_at""")
        return render(request, "list.html", title="Menschliche Prüfung (offen)", rows=rows,
                      note="Entscheiden mit: kcteam review show|approve|reject|retry <id> --note \"…\"")

    @app.get("/curriculum/demand", response_class=HTMLResponse)
    def demand(request: Request, _u: str = Depends(auth)):
        rows = pg().query("SELECT * FROM curriculum.demand_report ORDER BY misses DESC, requests DESC LIMIT 200")
        return render(request, "list.html", title="Nachfrage: welche Themen fehlen?", rows=rows)

    @app.get("/curriculum/exports", response_class=HTMLResponse)
    def exports(request: Request, _u: str = Depends(auth)):
        rows = pg().query("""SELECT e.id, e.status, e.format_id, e.grade, e.concept_id, e.concept_version,
                                    e.request_id, e.reason_code, e.attempts, e.updated_at,
                                    (SELECT string_agg(c.name, ', ') FROM curriculum.lesson_export_clients l
                                       JOIN curriculum.api_clients c ON c.id=l.client_id WHERE l.export_id=e.id)
                                      AS abnehmer
                             FROM curriculum.lesson_exports e ORDER BY e.id DESC LIMIT 200""")
        return render(request, "list.html", title="Lektionen im Format der Abnehmer", rows=rows,
                      links={"id": "exports", "concept_id": "concepts"})

    @app.get("/curriculum/exports/{eid}", response_class=HTMLResponse)
    def export(request: Request, eid: int, _u: str = Depends(auth)):
        e = pg().one("SELECT * FROM curriculum.lesson_exports WHERE id=%s", (eid,))
        if not e:
            raise HTTPException(404)
        e = {k: v for k, v in e.items() if k != "format_spec"}
        verworfen = pg_safe(lambda: pg().query(
            """SELECT c.name AS abnehmer, x.reason_code, x.contract_version, x.reason, x.created_at
               FROM curriculum.lesson_export_rejections x
               JOIN curriculum.api_clients c ON c.id = x.client_id
               WHERE x.export_id = %s ORDER BY x.created_at""", (eid,)), default=[]) or []
        return render(request, "export.html", e=e, verworfen=verworfen)

    @app.post("/curriculum/exports/{eid}/freigeben")
    def export_unblock(eid: int, _u: str = Depends(auth)):
        """Verwerfungen zu diesem Thema aufheben – der Weg aus der Sackgasse.

        Verwirft ein Abnehmer eine Lektion öfter als erlaubt, liefert der
        Dienst sie ihm nicht mehr. Lag es an einem Fehler des Abnehmers, hilft
        kein Neuschreiben, sondern nur dieser Knopf. Dasselbe tut
        `kcteam review unblock-export`.
        """
        e = pg().one("""SELECT concept_id, grade, format_id FROM curriculum.lesson_exports
                        WHERE id=%s""", (eid,))
        if not e or not e["concept_id"]:
            raise HTTPException(404)
        lessons.unblock_export(pg_write(), e["concept_id"], e["grade"], e["format_id"])
        return RedirectResponse(f"/curriculum/exports/{eid}", 303)

    @app.get("/curriculum/clients", response_class=HTMLResponse)
    def clients(request: Request, _u: str = Depends(auth)):
        rows = pg().query("""SELECT id, name, tenant, key_prefix, webhook_url, active, created_at, last_used_at
                             FROM curriculum.api_clients ORDER BY id""")
        return render(request, "list.html", title="Abnehmer (API-Schlüssel)", rows=rows,
                      note="Schlüssel werden nur als Hash gespeichert. Verwalten: kcteam api-client add|list|revoke")

    # ------------------------------------------------------------ Karo
    @app.get("/karo", response_class=HTMLResponse)
    def karo(request: Request, q: str = "", _u: str = Depends(auth)):
        con = sq()
        if not con:
            return render(request, "karo.html", missing=True, konzepte=[])
        with closing(con):
            tables = {t for t, _ in _sq_tables(con)}
            konzepte = []
            if "lern_konzept" in tables:
                konzepte = con.execute("""SELECT k.id, k.fach, k.thema_key, k.konzept_key, k.label, k.klasse_von,
                                                 k.klasse_bis, k.quelle, k.geprueft_am, k.aktiv,
                                                 (SELECT count(*) FROM lern_fehlertyp f WHERE f.konzept_id=k.id) AS fehlertypen
                                          FROM lern_konzept k
                                          WHERE ?='' OR k.label LIKE ? OR k.konzept_key LIKE ? OR k.stichworte LIKE ?
                                          ORDER BY k.fach, k.klasse_von, k.label""",
                                       (q, f"%{q}%", f"%{q}%", f"%{q}%")).fetchall()
        return render(request, "karo.html", missing=False, konzepte=konzepte, q=q)

    @app.get("/karo/konzept/{kid}", response_class=HTMLResponse)
    def karo_konzept(request: Request, kid: int, _u: str = Depends(auth)):
        con = sq()
        if not con:
            raise HTTPException(404)
        with closing(con):
            k = con.execute("SELECT * FROM lern_konzept WHERE id=?", (kid,)).fetchone()
            if not k:
                raise HTTPException(404)
            fehler = []
            for f in con.execute("SELECT * FROM lern_fehlertyp WHERE konzept_id=? ORDER BY id", (kid,)).fetchall():
                fehler.append({
                    "f": dict(f),
                    "alias": [r[0] for r in con.execute("SELECT muster FROM lern_fehler_alias WHERE fehlertyp_id=?",
                                                        (f["id"],))],
                    "erklaerungen": [dict(r) for r in con.execute(
                        "SELECT * FROM lern_erklaerung WHERE fehlertyp_id=? ORDER BY klasse, version", (f["id"],))],
                    "aufgaben": [dict(r) for r in con.execute(
                        "SELECT * FROM lern_aufgabe WHERE fehlertyp_id=? ORDER BY rolle, position", (f["id"],))],
                })
            hilfe = [dict(r) for r in con.execute("SELECT * FROM lern_hilfe WHERE konzept_id=? ORDER BY art, sortierung",
                                                  (kid,))]
            erst = [dict(r) for r in con.execute("SELECT * FROM lern_erstkontakt WHERE konzept_id=?", (kid,))]
        return render(request, "karo_konzept.html", k=dict(k), fehler=fehler, hilfe=hilfe, erst=erst)

    # ------------------------------------------------------------ alle Tabellen
    @app.get("/tables/{which}", response_class=HTMLResponse)
    def tables(request: Request, which: str, _u: str = Depends(auth)):
        if which == "curriculum":
            rows = pg().query("""SELECT c.relnamespace::regnamespace::text AS schema, c.relname AS name,
                                        CASE c.relkind WHEN 'v' THEN 'Sicht' ELSE 'Tabelle' END AS art,
                                        c.reltuples::bigint AS zeilen_ca
                                 FROM pg_class c WHERE c.relkind IN ('r','v')
                                   AND c.relnamespace::regnamespace::text = ANY(%s) ORDER BY 1, 2""",
                              (list(PG_SCHEMAS),))
            items = [(f"{r['schema']}.{r['name']}", r["art"], r["zeilen_ca"]) for r in rows]
        elif which == "karo":
            con = sq()
            if not con:
                raise HTTPException(404, f"Karo-Datenbank nicht gefunden: {karo_path}")
            with closing(con):
                items = [(t, a, con.execute(f'SELECT count(*) FROM {_q(t)}').fetchone()[0]) for t, a in _sq_tables(con)]
        else:
            raise HTTPException(404)
        return render(request, "tables.html", which=which, items=items)

    @app.get("/tables/{which}/{table}", response_class=HTMLResponse)
    def table(request: Request, which: str, table: str, q: str = "", page: int = 1, _u: str = Depends(auth)):
        page = max(1, page)
        if which == "curriculum":
            schema, _, name = table.partition(".")
            cols = [r["column_name"] for r in pg().query(
                """SELECT column_name FROM information_schema.columns WHERE table_schema=%s AND table_name=%s
                   ORDER BY ordinal_position""", (schema, name))]
            if schema not in PG_SCHEMAS or not cols:
                raise HTTPException(404)
            show = [c for c in cols if not HIDDEN.search(c)]
            ident = pgsql.Identifier(schema, name)
            # nur in sichtbaren Spalten suchen – sonst ließen sich ausgeblendete Geheimnisse erraten
            hay = pgsql.SQL("concat_ws(' ', {})").format(
                pgsql.SQL(", ").join(pgsql.SQL("{}::text").format(pgsql.Identifier(c)) for c in show))
            where = pgsql.SQL("WHERE {} ILIKE %s").format(hay) if q else pgsql.SQL("")
            params = [f"%{q}%"] if q else []
            total = pg().one(pgsql.SQL("SELECT count(*) AS n FROM {} t {}").format(ident, where), params)["n"]
            rows = pg().query(pgsql.SQL("SELECT {} FROM {} t {} LIMIT %s OFFSET %s").format(
                pgsql.SQL(", ").join(pgsql.Identifier(c) for c in show), ident, where),
                params + [PAGE, (page - 1) * PAGE])
        elif which == "karo":
            con = sq()
            if not con:
                raise HTTPException(404)
            with closing(con):
                if table not in {t for t, _ in _sq_tables(con)}:
                    raise HTTPException(404)
                cols = [r[1] for r in con.execute(f'PRAGMA table_info({_q(table)})')]
                show = [c for c in cols if not HIDDEN.search(c)]
                sel = ", ".join(_q(c) for c in show)
                where, params = "", []
                if q:
                    where = "WHERE (" + " || ' ' || ".join(f"coalesce({_q(c)}, '')" for c in show) + ") LIKE ?"
                    params = [f"%{q}%"]
                total = con.execute(f'SELECT count(*) FROM {_q(table)} {where}', params).fetchone()[0]
                rows = [dict(r) for r in con.execute(f'SELECT {sel} FROM {_q(table)} {where} LIMIT ? OFFSET ?',
                                                     params + [PAGE, (page - 1) * PAGE])]
        else:
            raise HTTPException(404)
        return render(request, "table.html", which=which, table=table, cols=show, rows=rows, q=q, page=page,
                      pages=max(1, -(-total // PAGE)), total=total, hidden=[c for c in cols if c not in show])

    return app


def _q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _sq_tables(con: sqlite3.Connection) -> list[tuple[str, str]]:
    return [(r[0], "Sicht" if r[1] == "view" else "Tabelle") for r in con.execute(
        "SELECT name, type FROM sqlite_master WHERE type IN ('table','view') AND name NOT LIKE 'sqlite_%' "
        "ORDER BY name")]
