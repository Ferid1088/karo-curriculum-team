"""Datenbankzugriff (PostgreSQL / Supabase) mit psycopg 3."""
from __future__ import annotations

import hashlib
import os
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable

import psycopg
from psycopg import sql as pgsql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .schemas import Calibration, ConceptDraft, Diagnostics, TopicBlock

SQL_DIR = Path(__file__).parent / "sql"
ROLES_VERSION = "4"   # erhöhen, wenn sich _setup_roles ändert


APP_FUNCS = ["start_diagnosis(text, text, integer, text[], text[], text)", "next_step(uuid)",
             "record_response(uuid, text, jsonb)", "item_for_child(text)", "forget_learner(text, text)",
             "learning_path(text[], text[])", "find_concepts(text, integer, text)",
             "resolve_topic(text, integer, text, text[], jsonb, text)", "request_status(bigint)"]
READER_FUNCS = ["learning_path(text[], text[])", "concept_bundle(text)", "find_concepts(text, integer, text)",
                "item_for_child(text)"]
REVIEWER_FUNCS = ["review_response(bigint, text, numeric, text)",
                  "record_external_result(uuid, text, jsonb, text, numeric, text)", "purge_learner_data(integer)"]


class DB:
    @staticmethod
    def connection_warnings(url: str) -> list[str]:
        """Supabase/Neon: LISTEN/NOTIFY und Sperren pro Fach brauchen eine Sitzungsverbindung, keinen
        Transaktions-Pooler."""
        warn = []
        u = url.lower()
        if ":6543" in u:
            warn.append("Port 6543 ist Supabases Transaktions-Pooler – dort funktionieren LISTEN/NOTIFY und die "
                        "Fach-Sperre nicht. Bitte den Session-Pooler (Port 5432) oder die direkte Verbindung nutzen.")
        if "-pooler." in u and "neon" in u:
            warn.append("Neon-Pooler-Adresse (-pooler) läuft im Transaktionsmodus – für kcteam die direkte "
                        "Verbindung ohne '-pooler' verwenden.")
        if ("supabase" in u or "neon" in u) and "sslmode=" not in u:
            warn.append("Für Supabase/Neon '?sslmode=require' an DATABASE_URL anhängen.")
        return warn

    def __init__(self, url: str):
        if not url:
            raise ValueError("DATABASE_URL ist nicht gesetzt")
        self.url = url
        self._local = threading.local()
        self._all: list[psycopg.Connection] = []
        self._lock = threading.Lock()

    # Eine Verbindung pro Thread (Konzepte laufen parallel); Keepalive gegen Abbrüche bei langen KI-Aufrufen
    def _conn(self) -> psycopg.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None or conn.closed or conn.broken:
            conn = psycopg.connect(self.url, row_factory=dict_row, autocommit=False,
                                   prepare_threshold=None,           # Supabase-Pooler-freundlich
                                   keepalives=1, keepalives_idle=30, keepalives_interval=10, keepalives_count=5,
                                   connect_timeout=15)
            self._local.conn = conn
            with self._lock:
                self._all = [c for c in self._all if not c.closed] + [conn]
        return conn

    def close_all(self) -> None:
        with self._lock:
            for c in self._all:
                try:
                    c.close()
                except Exception:  # noqa: BLE001
                    pass
            self._all = []
        self._local = threading.local()

    @contextmanager
    def tx(self):
        conn = self._conn()
        try:
            with conn.cursor() as cur:
                yield cur
            conn.commit()
        except Exception:
            try:
                conn.rollback()
            except Exception:  # noqa: BLE001 – die ursprüngliche Ausnahme ist wichtiger
                pass
            raise

    def query(self, sql: str, params: Iterable[Any] | dict | None = None) -> list[dict]:
        for attempt in (1, 2):
            try:
                with self.tx() as cur:
                    cur.execute(sql, params)
                    return cur.fetchall() if cur.description else []
            except psycopg.OperationalError:
                conn = getattr(self._local, "conn", None)
                if attempt == 2 or conn is None or not (conn.closed or conn.broken):
                    raise
                self._local.conn = None          # Verbindung weg -> einmal neu verbinden
        return []

    def one(self, sql: str, params=None) -> dict | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    # ---------------- Setup ----------------
    @staticmethod
    def schema_hash() -> str:
        h = hashlib.sha256()
        for f in sorted(SQL_DIR.glob("*.sql")):
            h.update(f.read_bytes())
        h.update(ROLES_VERSION.encode())
        return h.hexdigest()[:16]

    def wait_until_ready(self, timeout: float = 30) -> None:
        """Beim Start im Container ist die Datenbank manchmal noch nicht da (Name/Port noch nicht erreichbar)."""
        import time as _t
        deadline = _t.time() + timeout
        while True:
            try:
                self.one("SELECT 1 AS ok")
                return
            except psycopg.OperationalError:
                if _t.time() >= deadline:
                    raise
                _t.sleep(2)

    def migrate(self, force: bool = False) -> bool:
        """Schema anlegen/aktualisieren – nur wenn sich die SQL-Dateien geändert haben. Gibt True zurück, wenn gelaufen."""
        self.wait_until_ready()
        want = self.schema_hash()
        if not force:
            try:
                row = self.one("SELECT to_regclass('curriculum.schema_meta') IS NOT NULL AS ok")
                if row and row["ok"]:
                    cur = self.one("SELECT value FROM curriculum.schema_meta WHERE key='sql_hash'")
                    if cur and cur["value"] == want:
                        return False
            except psycopg.Error:
                pass
        for f in sorted(SQL_DIR.glob("*.sql")):
            with self.tx() as cur:
                cur.execute(f.read_text(encoding="utf-8"))
        if self._setup_roles():   # nur als erledigt markieren, wenn auch die Rechte vollständig gesetzt sind
            self.query("""INSERT INTO curriculum.schema_meta VALUES ('sql_hash', %s)
                          ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value""", (want,))
        else:
            print("⚠ Rechte/Rollen unvollständig – wird beim nächsten Start erneut versucht.")
        return True

    def _try(self, statements: list, label: str) -> bool:
        try:
            with self.tx() as cur:
                for st in statements:
                    cur.execute(st)
            return True
        except psycopg.Error as exc:
            print(f"Hinweis: {label} nicht möglich ({exc.__class__.__name__}: {str(exc).strip().splitlines()[0]}). "
                  "Rechte ggf. manuell vergeben.")
            return False

    def _setup_roles(self) -> bool:
        """Rollen und Rechte:
        karo_owner    (NOLOGIN)  besitzt die Funktionen/Sichten – kein Superuser
        karo_reader              liest Inhalte (inkl. Lösungen) – für Karos Backend
        karo_app                 Diagnose durchführen (ohne Lösungen, ohne Ergebnis-Überschreiben)
        karo_reviewer            zusätzlich Freitext nachbewerten
        PUBLIC                   nichts
        """
        def ensure_role(name: str, env: str | None) -> pgsql.Composed:
            pw = os.environ.get(env, "") if env else ""
            login = pgsql.SQL("LOGIN PASSWORD {}").format(pgsql.Literal(pw)) if pw else pgsql.SQL("NOLOGIN")
            return pgsql.SQL("DO $$BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = {n}) THEN "
                             "CREATE ROLE {r} {l}; END IF; END$$").format(
                n=pgsql.Literal(name), r=pgsql.Identifier(name), l=login)

        ok = self._try([ensure_role("karo_owner", None), ensure_role("karo_reader", "KARO_READER_PASSWORD"),
                        ensure_role("karo_app", "KARO_APP_PASSWORD"),
                        ensure_role("karo_reviewer", "KARO_REVIEWER_PASSWORD")], "Rollen anlegen")
        # Passwort nachträglich gesetzt/geändert -> Login aktivieren (init-db ausführen)
        pw_updates = [pgsql.SQL("ALTER ROLE {} LOGIN PASSWORD {}").format(pgsql.Identifier(r), pgsql.Literal(os.environ[e]))
                      for r, e in (("karo_reader", "KARO_READER_PASSWORD"), ("karo_app", "KARO_APP_PASSWORD"),
                                   ("karo_reviewer", "KARO_REVIEWER_PASSWORD")) if os.environ.get(e)]
        if pw_updates:
            ok &= self._try(pw_updates, "Passwörter setzen")
        ok &= self._try([
            "REVOKE ALL ON SCHEMA curriculum FROM PUBLIC", "REVOKE ALL ON SCHEMA learner FROM PUBLIC",
            "REVOKE ALL ON SCHEMA karo FROM PUBLIC",
            "REVOKE EXECUTE ON ALL FUNCTIONS IN SCHEMA karo FROM PUBLIC",
            "ALTER DEFAULT PRIVILEGES IN SCHEMA karo REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC",
        ], "Öffentliche Rechte entziehen")
        # Besitzer ohne Superuser-Rechte (SECURITY DEFINER läuft dann nur mit diesen Rechten)
        owner = ["GRANT karo_owner TO CURRENT_USER",
                 "GRANT USAGE ON SCHEMA curriculum, learner, karo TO karo_owner",
                 "GRANT SELECT ON ALL TABLES IN SCHEMA curriculum TO karo_owner",
                 "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA learner TO karo_owner",
                 "GRANT USAGE ON ALL SEQUENCES IN SCHEMA learner TO karo_owner",
                 # Aufträge an den Curriculum-Agenten (karo.resolve_topic) und Nachfrage-Protokoll
                 "GRANT INSERT, UPDATE ON curriculum.topic_requests, curriculum.topic_demand TO karo_owner",
                 "GRANT USAGE ON ALL SEQUENCES IN SCHEMA curriculum TO karo_owner",
                 """DO $$DECLARE s text; BEGIN
                      SELECT n.nspname INTO s FROM pg_extension e JOIN pg_namespace n ON n.oid = e.extnamespace
                       WHERE e.extname = 'pg_trgm';
                      IF s IS NOT NULL THEN EXECUTE format('GRANT USAGE ON SCHEMA %I TO karo_owner', s); END IF;
                    END$$""",
                 """DO $$DECLARE r record; BEGIN
                      FOR r IN SELECT p.oid::regprocedure AS f FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
                               WHERE n.nspname = 'karo' LOOP
                        EXECUTE format('ALTER FUNCTION %s OWNER TO karo_owner', r.f);
                      END LOOP;
                      FOR r IN SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
                               WHERE n.nspname = 'karo' AND c.relkind = 'v' LOOP
                        EXECUTE format('ALTER VIEW karo.%I OWNER TO karo_owner', r.relname);
                      END LOOP; END$$"""]
        ok &= self._try(owner, "Besitzerrolle karo_owner einrichten")
        grants = ["GRANT USAGE ON SCHEMA karo TO karo_reader, karo_app, karo_reviewer",
                  "GRANT SELECT ON ALL TABLES IN SCHEMA karo TO karo_reader",
                  "GRANT karo_reader TO karo_reviewer", "GRANT karo_app TO karo_reviewer",
                  "REVOKE ALL ON SCHEMA curriculum, learner FROM karo_reader, karo_app, karo_reviewer",
                  "REVOKE EXECUTE ON ALL FUNCTIONS IN SCHEMA karo FROM karo_reader, karo_app, karo_reviewer"]
        grants += [f"GRANT EXECUTE ON FUNCTION karo.{f} TO karo_reader" for f in READER_FUNCS]
        grants += [f"GRANT EXECUTE ON FUNCTION karo.{f} TO karo_app" for f in APP_FUNCS]
        grants += [f"GRANT EXECUTE ON FUNCTION karo.{f} TO karo_reviewer" for f in REVIEWER_FUNCS]
        ok &= self._try(grants, "Rechte für karo_reader/karo_app/karo_reviewer vergeben")
        return ok

    # ---------------- Runs & Logging ----------------
    def start_run(self, subject: str, grades: tuple[int, int], provider: str) -> str:
        run_id = str(uuid.uuid4())
        self.query("INSERT INTO curriculum.runs(id, subject, grade_min, grade_max, provider) VALUES (%s,%s,%s,%s,%s)",
                   (run_id, subject, grades[0], grades[1], provider))
        return run_id

    def finish_run(self, run_id: str, status: str, stats: dict, error: str | None = None) -> None:
        self.query("UPDATE curriculum.runs SET status=%s, stats=%s, error=%s, finished_at=now() WHERE id=%s",
                   (status, Jsonb(stats), error, run_id))

    def log_call(self, run_id, role, provider, model, entity_id, in_tok, out_tok, ms, ok, error=None) -> None:
        self.query("""INSERT INTO curriculum.agent_calls(run_id, role, provider, model, entity_id,
                      input_tokens, output_tokens, duration_ms, ok, error)
                      VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                   (run_id, role, provider, model, entity_id, in_tok, out_tok, ms, ok, (error or "")[:2000] or None))

    def log_review(self, run_id, entity_type, entity_id, stage, reviewer, decision, round_=1, findings=None, note=None):
        self.query("""INSERT INTO curriculum.reviews(run_id, entity_type, entity_id, stage, reviewer, decision, round, findings, note)
                      VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                   (run_id, entity_type, entity_id, stage, reviewer, decision, round_, Jsonb(findings or []), note))

    def enqueue_human(self, entity_type, entity_id, stage, reason, payload=None, kind="veto",
                      urgent: bool = False) -> None:
        existing = self.one("""SELECT id FROM curriculum.human_queue WHERE status='open'
                               AND entity_type=%s AND entity_id=%s AND stage=%s AND kind=%s""",
                            (entity_type, entity_id, stage, kind))
        if existing:
            self.query("""UPDATE curriculum.human_queue SET reason=%s, payload=%s, created_at=now(),
                            urgent = urgent OR %s WHERE id=%s""",
                       (reason, Jsonb(payload or {}), urgent, existing["id"]))
        else:
            self.query("""INSERT INTO curriculum.human_queue(entity_type, entity_id, stage, kind, reason, payload, urgent)
                          VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                       (entity_type, entity_id, stage, kind, reason, Jsonb(payload or {}), urgent))

    def mark_urgent(self, entity_ids: list[str]) -> None:
        """Ein Kind wartet: offene Prüfungen zu diesen Inhalten zuerst bearbeiten."""
        self.query("UPDATE curriculum.human_queue SET urgent=true WHERE status='open' AND entity_id = ANY(%s)",
                   (list(entity_ids),))

    # ---------------- Sperre pro Fach (Stapellauf und Curriculum-Agent nie gleichzeitig am selben Fach) ----
    @contextmanager
    def subject_lock(self, code: str, wait: bool = True, stop=None, on_wait=None):
        conn = psycopg.connect(self.url, autocommit=True)
        key = f"kcteam:{code}"
        try:
            got = conn.execute("SELECT pg_try_advisory_lock(hashtext(%s))", (key,)).fetchone()[0]
            warned = False
            while not got and wait:
                if on_wait and not warned:
                    on_wait()
                    warned = True
                if stop is not None and stop.wait(2):
                    raise TimeoutError("abgebrochen")
                if stop is None:
                    import time as _t
                    _t.sleep(2)
                got = conn.execute("SELECT pg_try_advisory_lock(hashtext(%s))", (key,)).fetchone()[0]
            yield got
        finally:
            conn.close()   # beendet die Sitzung -> Sperre wird frei

    # ---------------- Fach & Blöcke ----------------
    def get_subject(self, name: str) -> dict | None:
        return self.one("SELECT * FROM curriculum.subjects WHERE lower(name)=lower(%s)", (name,))

    def save_subject(self, code: str, name: str, grades: tuple[int, int], status: str = "mapped",
                     profile: str | None = None) -> None:
        self.query("""INSERT INTO curriculum.subjects(code, name, grade_min, grade_max, status, profile)
                      VALUES (%s,%s,%s,%s,%s,%s)
                      ON CONFLICT (code) DO UPDATE SET name=EXCLUDED.name,
                        grade_min=LEAST(curriculum.subjects.grade_min, EXCLUDED.grade_min),
                        grade_max=GREATEST(curriculum.subjects.grade_max, EXCLUDED.grade_max),
                        status=EXCLUDED.status, profile=coalesce(EXCLUDED.profile, curriculum.subjects.profile),
                        updated_at=now()""",
                   (code, name, grades[0], grades[1], status, profile))

    def save_block(self, subject_code: str, b: TopicBlock) -> None:
        self.query("""INSERT INTO curriculum.topic_blocks(id, subject_code, title, description, grade_min, grade_max,
                        typical_grade, varies, variance_note, sources)
                      VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                      ON CONFLICT (id) DO UPDATE SET title=EXCLUDED.title, description=EXCLUDED.description,
                        grade_min=EXCLUDED.grade_min, grade_max=EXCLUDED.grade_max, typical_grade=EXCLUDED.typical_grade,
                        varies=EXCLUDED.varies, variance_note=EXCLUDED.variance_note, sources=EXCLUDED.sources,
                        updated_at=now()""",
                   (b.id, subject_code, b.title, b.description, b.grade_min, b.grade_max, b.typical_grade,
                    b.varies, b.variance_note, Jsonb([s.model_dump() for s in b.sources])))

    def blocks(self, subject_code: str) -> list[dict]:
        return self.query("SELECT * FROM curriculum.topic_blocks WHERE subject_code=%s ORDER BY grade_min, id",
                          (subject_code,))

    def set_block_status(self, block_id: str, status: str) -> None:
        self.query("UPDATE curriculum.topic_blocks SET status=%s, updated_at=now() WHERE id=%s", (status, block_id))

    # ---------------- Konzepte ----------------
    LIGHT_COLS = ("id, block_id, subject_code, title, first_contact_grade, target_grade, varies, sort_order, "
                  "status, track, learning_year, cefr, visual_need, pending_feedback")

    def concepts(self, block_id: str | None = None, subject_code: str | None = None,
                 include_retired: bool = False, light: bool = False, with_description: bool = False) -> list[dict]:
        """light=True: ohne die großen JSON-Spalten (schneller, weniger Speicher)."""
        where, params = [], []
        if block_id:
            where.append("block_id=%s"); params.append(block_id)
        if subject_code:
            where.append("subject_code=%s"); params.append(subject_code)
        if not include_retired:
            where.append("status <> 'retired'")
        cols = (self.LIGHT_COLS + (", description" if with_description else "")) if light else "*"
        sql = f"SELECT {cols} FROM curriculum.concepts"
        if where:
            sql += " WHERE " + " AND ".join(where)
        return self.query(sql + " ORDER BY block_id, sort_order, id", params)

    def resolve_errors(self, entity_type: str, entity_id: str) -> None:
        """Offene Fehlermeldungen schließen, wenn der Inhalt inzwischen erfolgreich durchgelaufen ist."""
        self.query("""UPDATE curriculum.human_queue SET status='resolved', resolution='automatisch: später erfolgreich',
                             resolved_by='system', resolved_at=now()
                      WHERE status='open' AND kind='error' AND entity_type=%s AND entity_id=%s""",
                   (entity_type, entity_id))

    def concept(self, concept_id: str) -> dict | None:
        return self.one("SELECT * FROM curriculum.concepts WHERE id=%s", (concept_id,))

    def prerequisites(self, concept_id: str) -> list[str]:
        return [r["prerequisite_id"] for r in self.query(
            "SELECT prerequisite_id FROM curriculum.concept_prerequisites WHERE concept_id=%s ORDER BY 1", (concept_id,))]

    def all_edges(self, subject_code: str) -> list[tuple[str, str]]:
        rows = self.query("""SELECT p.concept_id, p.prerequisite_id FROM curriculum.concept_prerequisites p
                             JOIN curriculum.concepts c ON c.id=p.concept_id
                             WHERE c.subject_code=%s AND c.status <> 'retired'""", (subject_code,))
        return [(r["concept_id"], r["prerequisite_id"]) for r in rows]

    def save_graph(self, subject_code: str, block_id: str, concepts: list[ConceptDraft],
                   keep_approved: bool = False) -> dict[str, str]:
        """Speichert die Struktur eines Blocks. Gibt zurück, welche Konzepte neu/geändert sind.
        Geänderte Konzepte (Titel/Klassen) laufen neu durch das Team.
        keep_approved=True: freigegebene Konzepte bleiben unverändert sichtbar (nur Kanten werden aktualisiert)
        und werden nicht stillgelegt."""
        changes: dict[str, str] = {}
        new_ids = {c.id for c in concepts}
        with self.tx() as cur:
            cur.execute("SELECT * FROM curriculum.concepts WHERE block_id=%s", (block_id,))
            existing = {r["id"]: r for r in cur.fetchall()}
            for c in concepts:
                old = existing.get(c.id)
                if old is None:
                    cur.execute("""INSERT INTO curriculum.concepts(id, block_id, subject_code, title, description,
                                     first_contact_grade, target_grade, varies, sort_order, status, track,
                                     learning_year, cefr)
                                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'structured',%s,%s,%s)
                                   ON CONFLICT (id) DO UPDATE SET block_id=EXCLUDED.block_id, title=EXCLUDED.title,
                                     description=EXCLUDED.description, first_contact_grade=EXCLUDED.first_contact_grade,
                                     target_grade=EXCLUDED.target_grade, varies=EXCLUDED.varies,
                                     sort_order=EXCLUDED.sort_order, status='structured', track=EXCLUDED.track,
                                     learning_year=EXCLUDED.learning_year, cefr=EXCLUDED.cefr, updated_at=now()""",
                                (c.id, block_id, subject_code, c.title, c.description, c.first_contact_grade,
                                 c.target_grade, c.varies, c.order, c.track, c.learning_year, c.cefr))
                    changes[c.id] = "new"
                elif keep_approved and old["status"] == "approved":
                    pass   # Inhalt bleibt, Kanten werden unten aktualisiert
                else:
                    material = (old["title"] != c.title or old["first_contact_grade"] != c.first_contact_grade
                                or old["target_grade"] != c.target_grade or old["status"] == "retired")
                    cur.execute("""UPDATE curriculum.concepts SET title=%s, description=%s, first_contact_grade=%s,
                                     target_grade=%s, varies=%s, sort_order=%s, updated_at=now(),
                                     track=%s, learning_year=%s, cefr=%s,
                                     status = CASE WHEN %s THEN 'structured' ELSE status END,
                                     version = CASE WHEN %s THEN version + 1 ELSE version END
                                   WHERE id=%s""",
                                (c.title, c.description, c.first_contact_grade, c.target_grade, c.varies, c.order,
                                 c.track, c.learning_year, c.cefr, material, material, c.id))
                    if material:
                        changes[c.id] = "changed"
                cur.execute("DELETE FROM curriculum.concept_prerequisites WHERE concept_id=%s", (c.id,))
                for pre in dict.fromkeys(c.prerequisites):
                    if pre != c.id:
                        cur.execute("INSERT INTO curriculum.concept_prerequisites VALUES (%s,%s) ON CONFLICT DO NOTHING",
                                    (c.id, pre))
            for old_id in set(existing) - new_ids:
                if keep_approved and existing[old_id]["status"] == "approved":
                    continue
                cur.execute("UPDATE curriculum.concepts SET status='retired', updated_at=now() WHERE id=%s", (old_id,))
                changes[old_id] = "retired"
        return changes

    def update_concept_meta(self, c: ConceptDraft) -> bool:
        """Titel, Beschreibung, Klassenstufen und Voraussetzungen eines Konzepts ändern.
        Gibt True zurück, wenn sich die Klassenstufen geändert haben (dann muss neu kalibriert werden)."""
        with self.tx() as cur:
            cur.execute("SELECT first_contact_grade, target_grade FROM curriculum.concepts WHERE id=%s", (c.id,))
            old = cur.fetchone()
            cur.execute("""UPDATE curriculum.concepts SET title=%s, description=%s, first_contact_grade=%s,
                             target_grade=%s, track=%s, learning_year=%s, cefr=%s, updated_at=now() WHERE id=%s""",
                        (c.title, c.description, c.first_contact_grade, c.target_grade, c.track, c.learning_year,
                         c.cefr, c.id))
            cur.execute("DELETE FROM curriculum.concept_prerequisites WHERE concept_id=%s", (c.id,))
            for pre in dict.fromkeys(c.prerequisites):
                if pre != c.id:
                    cur.execute("INSERT INTO curriculum.concept_prerequisites VALUES (%s,%s) ON CONFLICT DO NOTHING",
                                (c.id, pre))
        return bool(old) and (old["first_contact_grade"], old["target_grade"]) != (c.first_contact_grade, c.target_grade)

    def add_concepts(self, subject_code: str, block_id: str, concepts: list[ConceptDraft],
                     search_terms: list[str] | None = None, feedback: str | None = None) -> list[str]:
        """Einzelne neue Konzepte in einen bestehenden Block einfügen (Curriculum-Agent). Bestehende bleiben unberührt."""
        added = []
        with self.tx() as cur:
            cur.execute("SELECT coalesce(max(sort_order), 0) AS m FROM curriculum.concepts WHERE block_id=%s",
                        (block_id,))
            order = cur.fetchone()["m"]
            for i, c in enumerate(concepts, 1):
                cur.execute("""INSERT INTO curriculum.concepts(id, block_id, subject_code, title, description,
                                 first_contact_grade, target_grade, varies, sort_order, status, track, learning_year,
                                 cefr, search_terms, pending_feedback)
                               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'structured',%s,%s,%s,%s,%s)
                               ON CONFLICT (id) DO NOTHING""",
                            (c.id, block_id, subject_code, c.title, c.description, c.first_contact_grade,
                             c.target_grade, c.varies, order + i, c.track, c.learning_year, c.cefr,
                             (search_terms or []) if i == 1 else [], feedback))
                if cur.rowcount:
                    added.append(c.id)
                    for pre in dict.fromkeys(c.prerequisites):
                        if pre != c.id:
                            cur.execute("INSERT INTO curriculum.concept_prerequisites VALUES (%s,%s) "
                                        "ON CONFLICT DO NOTHING", (c.id, pre))
        return added

    def add_search_terms(self, concept_ids: list[str], terms: list[str]) -> None:
        """Gelernte Synonyme: die nächste gleiche Anfrage findet das Konzept sofort (ohne KI)."""
        terms = [t.strip()[:120] for t in terms if t and t.strip()]
        if not terms or not concept_ids:
            return
        self.query("""UPDATE curriculum.concepts SET search_terms =
                        (SELECT array_agg(DISTINCT t) FROM unnest(search_terms || %s::text[]) t)
                      WHERE id = ANY(%s)""", (terms, list(concept_ids)))

    # ---------------- Aufträge an den Curriculum-Agenten ----------------
    def claim_request(self) -> dict | None:
        # 'ready' + stage 'resume': Karo nutzt das Konzept schon, nur das Vervollständigen wird fortgesetzt
        rows = self.query("""UPDATE curriculum.topic_requests SET started_at=now(), heartbeat_at=now(),
                                    status = CASE WHEN status='ready' THEN 'ready' ELSE 'running' END,
                                    attempts = attempts + 1, stage='match', updated_at=now()
                             WHERE id = (SELECT id FROM curriculum.topic_requests
                                         WHERE (status='queued' OR (status='ready' AND stage='resume'))
                                           AND next_attempt_at <= now()
                                         ORDER BY priority DESC, created_at
                                         FOR UPDATE SKIP LOCKED LIMIT 1)
                             RETURNING *""")
        return rows[0] if rows else None

    def update_request(self, rid: int, **fields) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k}=%s" for k in fields)
        vals = [Jsonb(v) if isinstance(v, (dict, list)) and k in ("tasks", "candidates") else v
                for k, v in fields.items()]
        self.query(f"UPDATE curriculum.topic_requests SET {cols}, updated_at=now() WHERE id=%s", (*vals, rid))

    def finish_request(self, rid: int, status: str, concepts: list[str] | None = None,
                       reason_code: str | None = None, message: str | None = None) -> None:
        """Abschluss: Aufgaben des Arbeitsblatts löschen (Datenschutz) und Karo benachrichtigen."""
        final = status in ("done", "blocked", "rejected", "failed")
        self.query("""UPDATE curriculum.topic_requests SET status=%s, result_concepts=coalesce(%s, result_concepts),
                        reason_code=%s, message=%s, stage=NULL, updated_at=now(),
                        finished_at = CASE WHEN %s THEN now() ELSE finished_at END,
                        tasks = CASE WHEN %s THEN '[]'::jsonb ELSE tasks END
                      WHERE id=%s""", (status, concepts, reason_code, message, final, final, rid))
        payload = {"request_id": rid, "status": status, "concepts": concepts or [], "reason_code": reason_code}
        import json as _json
        self.query("SELECT pg_notify('karo_topic_ready', %s)", (_json.dumps(payload),))

    def requeue_stale_requests(self, minutes: int = 10) -> int:
        rows = self.query("""UPDATE curriculum.topic_requests
                             SET status = CASE WHEN status='ready' THEN 'ready' ELSE 'queued' END,
                                 stage = CASE WHEN status='ready' THEN 'resume' ELSE NULL END, updated_at=now()
                             WHERE (status='running' OR (status='ready' AND stage IS DISTINCT FROM 'resume'))
                               AND heartbeat_at < now() - make_interval(mins => %s)
                             RETURNING id""", (minutes,))
        return len(rows)

    def save_calibration(self, concept_id: str, cal: Calibration, keep_status: bool = False) -> None:
        data = cal.model_dump()
        with self.tx() as cur:
            cur.execute("""UPDATE curriculum.concepts SET levels=%s, can_do=%s, difficulty_parameters=%s,
                             calibration=%s, updated_at=now()""" + ("" if keep_status else ", status='calibrated'")
                        + " WHERE id=%s",
                        (Jsonb(data["levels"]), Jsonb(data["can_do"]), Jsonb(data["difficulty_parameters"]),
                         Jsonb(data), concept_id))
            cur.execute("DELETE FROM curriculum.items WHERE concept_id=%s AND kind IN ('anchor','boundary') "
                        "AND visual IS NULL", (concept_id,))
            n = 0
            for i, it in enumerate(cal.anchor_items, 1):
                n += 1
                self._insert_item(cur, f"{concept_id}.ANCHOR_{i}", concept_id, "anchor", it, n)
            for side in ("below", "within", "above"):
                for i, it in enumerate(getattr(cal.boundary_items, side), 1):
                    n += 1
                    self._insert_item(cur, f"{concept_id}.BOUNDARY_{side.upper()}_{i}", concept_id, "boundary", it, n)

    def save_diagnostics(self, concept_id: str, diag: Diagnostics, keep_status: bool = False) -> None:
        with self.tx() as cur:
            cur.execute("UPDATE curriculum.concepts SET diagnostics=%s, updated_at=now()"
                        + ("" if keep_status else ", status='diagnosed'") + " WHERE id=%s",
                        (Jsonb(diag.model_dump()), concept_id))
            cur.execute("DELETE FROM curriculum.items WHERE concept_id=%s AND kind IN ('diagnostic','misconception','exit') "
                        "AND visual IS NULL", (concept_id,))
            cur.execute("DELETE FROM curriculum.misconceptions WHERE concept_id=%s", (concept_id,))
            n = 100
            for m in diag.misconceptions:
                mid = f"{concept_id}.{m.key.upper()}"
                cur.execute("INSERT INTO curriculum.misconceptions VALUES (%s,%s,%s,%s,%s)",
                            (mid, concept_id, m.key.upper(), m.description, m.remediation_hint))
                n += 1
                self._insert_item(cur, f"{mid}.DIAG", concept_id, "misconception", m.diagnostic_item, n, mid)
            for i, it in enumerate(diag.diagnostic_items, 1):
                n += 1
                self._insert_item(cur, f"{concept_id}.DIAG_{i}", concept_id, "diagnostic", it, n)
            for i, it in enumerate(diag.exit_items, 1):
                n += 1
                self._insert_item(cur, f"{concept_id}.EXIT_{i}", concept_id, "exit", it, n)

    def save_visuals(self, concept_id: str, vset, keep_status: bool = False) -> None:
        """Speichert Visuals inkl. vorgerendertem SVG. keep_status=True: Konzept bleibt, wie es ist (Nachrüsten)."""
        from .visuals.render import render
        data = vset.model_dump()
        with self.tx() as cur:
            cur.execute("""UPDATE curriculum.concepts SET visuals=%s, visual_need=%s, updated_at=now()"""
                        + ("" if keep_status else ", status='visualized'") + " WHERE id=%s",
                        (Jsonb(data), vset.visual_need, concept_id))
            cur.execute("DELETE FROM curriculum.visual_explanations WHERE concept_id=%s", (concept_id,))
            cur.execute("DELETE FROM curriculum.items WHERE concept_id=%s AND visual IS NOT NULL", (concept_id,))
            for n, e in enumerate(vset.explanations, 1):
                steps = [{"caption": s.caption, "visual": s.visual.model_dump(), "svg": render(s.visual)}
                         for s in e.steps]
                mid = f"{concept_id}.{e.for_misconception.upper()}" if e.for_misconception else None
                cur.execute("""INSERT INTO curriculum.visual_explanations
                               (id, concept_id, key, purpose, level, misconception_id, steps, sort_order)
                               VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
                            (f"{concept_id}.{e.key.upper()}", concept_id, e.key.upper(), e.purpose, e.level, mid,
                             Jsonb(steps), n))
            counters: dict[str, int] = {}
            for it in vset.visual_items:
                counters[it.use] = counters.get(it.use, 0) + 1
                cur.execute("""INSERT INTO curriculum.items(id, concept_id, kind, level, grade, prompt, solution,
                                 representation, sort_order, visual, visual_svg, interaction, answer, distractors,
                                 auto_checkable)
                               VALUES (%s,%s,%s,%s,%s,%s,%s,'visuell',%s,%s,%s,%s,%s,%s,%s)""",
                            (f"{concept_id}.VIS_{it.use.upper()}_{counters[it.use]}", concept_id, it.use, it.level,
                             it.grade, it.prompt, it.solution, 500 + sum(counters.values()),
                             Jsonb(it.visual.model_dump()), render(it.visual), it.interaction, *self._answer_cols(it)))

    @staticmethod
    def _answer_cols(it) -> tuple:
        from .answers import is_auto_checkable
        return (Jsonb(it.answer.model_dump()) if it.answer else None,
                Jsonb([d.model_dump() for d in it.distractors]), is_auto_checkable(it.answer))

    def _insert_item(self, cur, item_id, concept_id, kind, it, order, misconception_id=None):
        cur.execute("""INSERT INTO curriculum.items(id, concept_id, kind, level, grade, prompt, solution,
                         representation, misconception_id, sort_order, answer, distractors, auto_checkable)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (item_id, concept_id, kind, it.level, it.grade, it.prompt, it.solution,
                     it.representation, misconception_id, order, *self._answer_cols(it)))

    def set_concept_status(self, concept_id: str, status: str, pending_feedback: str | None = None,
                           keep_feedback: bool = False) -> None:
        if keep_feedback:
            self.query("UPDATE curriculum.concepts SET status=%s, updated_at=now() WHERE id=%s", (status, concept_id))
        else:
            self.query("UPDATE curriculum.concepts SET status=%s, pending_feedback=%s, updated_at=now() WHERE id=%s",
                       (status, pending_feedback, concept_id))

    # ---------------- Auswertung ----------------
    def run_stats(self, run_id: str) -> dict:
        r = self.one("""SELECT count(*) AS calls, coalesce(sum(input_tokens),0) AS input_tokens,
                               coalesce(sum(output_tokens),0) AS output_tokens,
                               count(*) FILTER (WHERE NOT ok) AS failed
                        FROM curriculum.agent_calls WHERE run_id=%s""", (run_id,))
        return {k: int(v) for k, v in (r or {}).items()}
