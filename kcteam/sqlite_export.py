"""Stufe 1 (Karo-MVP mit SQLite): freigegebene Inhalte als eine SQLite-Datei in Karos Volume.

Postgres bleibt die Arbeitsdatenbank des Teams (Inspektor, Prüfprotokoll, Warteschlange). Diese Datei ist eine
schreibgeschützte Kopie **nur freigegebener** Inhalte – ohne Kinderdaten, ohne Aufträge, ohne Prüfprotokoll.

Karo liest sie ohne Umbau mit:
    ATTACH DATABASE '/data/curriculum.db' AS cur;

Die Datei wird atomar ersetzt (neue Datei schreiben, dann umbenennen): Karo sieht immer einen vollständigen
Stand, nie eine halb geschriebene Datei.
"""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path

EXPORT_VERSION = 1

SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE subjects (
    code TEXT PRIMARY KEY, name TEXT NOT NULL, grade_min INTEGER NOT NULL, grade_max INTEGER NOT NULL);

CREATE TABLE topic_blocks (
    id TEXT PRIMARY KEY, subject_code TEXT NOT NULL REFERENCES subjects(code), title TEXT NOT NULL,
    description TEXT, grade_min INTEGER, grade_max INTEGER, typical_grade INTEGER, varies INTEGER);

CREATE TABLE concepts (
    id TEXT PRIMARY KEY, block_id TEXT NOT NULL REFERENCES topic_blocks(id),
    subject_code TEXT NOT NULL REFERENCES subjects(code), title TEXT NOT NULL, description TEXT,
    first_contact_grade INTEGER, target_grade INTEGER NOT NULL, varies INTEGER, sort_order INTEGER,
    track TEXT, learning_year INTEGER, cefr TEXT, visual_need TEXT, version INTEGER,
    levels TEXT, can_do TEXT, difficulty_parameters TEXT,  -- JSON
    boundary_items TEXT,                                   -- JSON: below/within/above (Leitplanken für Generatoren)
    search_terms TEXT,                                     -- JSON-Liste
    missing_prerequisites INTEGER NOT NULL DEFAULT 0       -- Voraussetzungen, die (noch) nicht freigegeben sind
);
CREATE INDEX concepts_subject_grade ON concepts(subject_code, target_grade);

-- nur Kanten zwischen freigegebenen Konzepten
CREATE TABLE concept_prerequisites (
    concept_id TEXT NOT NULL REFERENCES concepts(id), prerequisite_id TEXT NOT NULL REFERENCES concepts(id),
    PRIMARY KEY (concept_id, prerequisite_id));
CREATE INDEX prereq_rev ON concept_prerequisites(prerequisite_id);

CREATE TABLE misconceptions (
    id TEXT PRIMARY KEY, concept_id TEXT NOT NULL REFERENCES concepts(id), key TEXT, description TEXT,
    remediation_hint TEXT);

CREATE TABLE items (
    id TEXT PRIMARY KEY, concept_id TEXT NOT NULL REFERENCES concepts(id),
    kind TEXT NOT NULL,            -- anchor | boundary_below | boundary_within | boundary_above | diagnostic |
                                   -- misconception | exit | practice
    level TEXT, grade INTEGER, prompt TEXT NOT NULL, solution TEXT, representation TEXT,
    misconception_id TEXT REFERENCES misconceptions(id), sort_order INTEGER,
    answer TEXT,                   -- JSON: auswertbare Antwort (type number/fraction/choice/…)
    distractors TEXT,              -- JSON: typische falsche Antworten -> Fehlvorstellung
    auto_checkable INTEGER NOT NULL DEFAULT 0,
    visual TEXT, visual_svg TEXT, interaction TEXT);
CREATE INDEX items_concept_kind ON items(concept_id, kind, level);

CREATE TABLE visual_explanations (
    id TEXT PRIMARY KEY, concept_id TEXT NOT NULL REFERENCES concepts(id), key TEXT, purpose TEXT, level TEXT,
    misconception_id TEXT REFERENCES misconceptions(id),
    steps TEXT,                    -- JSON: [{caption, visual, svg}]
    sort_order INTEGER);

-- Volltextsuche für "Arbeitsblatt -> Konzept" (Titel, Beschreibung, gelernte Suchbegriffe)
CREATE VIRTUAL TABLE concept_search USING fts5(
    concept_id UNINDEXED, subject_code UNINDEXED, title, description, search_terms,
    tokenize = 'unicode61 remove_diacritics 2');

-- Aufgaben ohne Lösung (für Ansichten, die das Kind sieht)
CREATE VIEW items_for_child AS
SELECT id, concept_id, kind, level, grade, prompt, representation, visual_svg, interaction,
       CASE WHEN json_extract(answer, '$.type') = 'choice'
            THEN (SELECT json_group_array(json_extract(o.value, '$.text'))
                  FROM json_each(json_extract(answer, '$.options')) o) END AS choices
FROM items;
"""

# Lernpfad wie karo.learning_path: vom Ziel rückwärts über alle Voraussetzungen, tiefste zuerst
LEARNING_PATH_SQL = """
WITH RECURSIVE path(id, depth) AS (
    SELECT value, 0 FROM json_each(:targets)
    UNION
    SELECT p.prerequisite_id, path.depth + 1
    FROM concept_prerequisites p JOIN path ON p.concept_id = path.id
    WHERE path.depth < 40
      AND p.prerequisite_id NOT IN (SELECT value FROM json_each(:mastered))
)
SELECT c.id AS concept_id, c.title, c.target_grade, max(path.depth) AS depth
FROM path JOIN concepts c ON c.id = path.id
GROUP BY c.id ORDER BY depth DESC, c.target_grade, c.id
"""


def _j(v) -> str | None:
    return None if v is None else json.dumps(v, ensure_ascii=False, separators=(",", ":"))


def export_sqlite(db, out_path: str | os.PathLike, subject_codes: list[str] | None = None) -> dict:
    """Schreibt alle freigegebenen Inhalte (optional nur bestimmte Fächer) nach out_path. Gibt Zahlen zurück."""
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    codes = subject_codes or [r["code"] for r in db.query(
        "SELECT DISTINCT subject_code AS code FROM curriculum.concepts WHERE status='approved'")]
    fd, tmp = tempfile.mkstemp(prefix=".curriculum-", suffix=".db", dir=out.parent)
    os.close(fd)
    counts: dict[str, int] = {}
    try:
        con = sqlite3.connect(tmp)
        con.executescript(SCHEMA)
        con.execute("PRAGMA foreign_keys = ON")
        subjects = db.query("SELECT code, name, grade_min, grade_max FROM curriculum.subjects WHERE code = ANY(%s)",
                            (codes,))
        con.executemany("INSERT INTO subjects VALUES (?,?,?,?)",
                        [(s["code"], s["name"], s["grade_min"], s["grade_max"]) for s in subjects])
        concepts = db.query("""SELECT c.*, (SELECT count(*) FROM curriculum.concept_prerequisites p
                                            LEFT JOIN curriculum.concepts x ON x.id = p.prerequisite_id
                                            WHERE p.concept_id = c.id AND (x.status IS DISTINCT FROM 'approved'))
                                           AS missing
                               FROM curriculum.concepts c
                               WHERE c.status='approved' AND c.subject_code = ANY(%s)
                               ORDER BY c.block_id, c.sort_order, c.id""", (codes,))
        ids = [c["id"] for c in concepts]
        blocks = db.query("""SELECT * FROM curriculum.topic_blocks WHERE id IN
                             (SELECT DISTINCT block_id FROM curriculum.concepts WHERE id = ANY(%s))""", (ids,))
        con.executemany("INSERT INTO topic_blocks VALUES (?,?,?,?,?,?,?,?)",
                        [(b["id"], b["subject_code"], b["title"], b["description"], b["grade_min"], b["grade_max"],
                          b["typical_grade"], int(bool(b["varies"]))) for b in blocks])
        rows = []
        for c in concepts:
            cal = c.get("calibration") or {}
            rows.append((c["id"], c["block_id"], c["subject_code"], c["title"], c["description"],
                         c["first_contact_grade"], c["target_grade"], int(bool(c["varies"])), c["sort_order"],
                         c.get("track"), c.get("learning_year"), c.get("cefr"), c.get("visual_need"), c.get("version"),
                         _j(c.get("levels")), _j(c.get("can_do")), _j(c.get("difficulty_parameters")),
                         _j(cal.get("boundary_items")), _j(list(c.get("search_terms") or [])), c["missing"]))
        con.executemany(f"INSERT INTO concepts VALUES ({','.join('?' * 20)})", rows)
        con.executemany("INSERT INTO concept_search VALUES (?,?,?,?,?)",
                        [(c["id"], c["subject_code"], c["title"], c["description"] or "",
                          " ".join(c.get("search_terms") or [])) for c in concepts])
        edges = db.query("""SELECT concept_id, prerequisite_id FROM curriculum.concept_prerequisites
                            WHERE concept_id = ANY(%s) AND prerequisite_id = ANY(%s)""", (ids, ids))
        con.executemany("INSERT INTO concept_prerequisites VALUES (?,?)",
                        [(e["concept_id"], e["prerequisite_id"]) for e in edges])
        mis = db.query("SELECT * FROM curriculum.misconceptions WHERE concept_id = ANY(%s)", (ids,))
        con.executemany("INSERT INTO misconceptions VALUES (?,?,?,?,?)",
                        [(m["id"], m["concept_id"], m["key"], m["description"], m["remediation_hint"]) for m in mis])
        items = db.query("SELECT * FROM curriculum.items WHERE concept_id = ANY(%s) ORDER BY concept_id, sort_order",
                         (ids,))
        con.executemany(f"INSERT INTO items VALUES ({','.join('?' * 16)})",
                        [(i["id"], i["concept_id"], i["kind"], i["level"], i["grade"], i["prompt"], i["solution"],
                          i["representation"], i["misconception_id"], i["sort_order"], _j(i["answer"]),
                          _j(i["distractors"]), int(bool(i["auto_checkable"])), _j(i["visual"]), i["visual_svg"],
                          i["interaction"]) for i in items])
        vis = db.query("SELECT * FROM curriculum.visual_explanations WHERE concept_id = ANY(%s)", (ids,))
        con.executemany("INSERT INTO visual_explanations VALUES (?,?,?,?,?,?,?,?)",
                        [(v["id"], v["concept_id"], v["key"], v["purpose"], v["level"], v["misconception_id"],
                          _j(v["steps"]), v["sort_order"]) for v in vis])
        counts = {"subjects": len(subjects), "concepts": len(concepts), "prerequisites": len(edges),
                  "misconceptions": len(mis), "items": len(items), "visual_explanations": len(vis)}
        meta = {"export_version": str(EXPORT_VERSION), "exported_at": datetime.now(timezone.utc).isoformat(),
                "source": "karo-curriculum-team", "subjects": ",".join(sorted(codes)),
                "learning_path_sql": LEARNING_PATH_SQL.strip(), **{f"count_{k}": str(v) for k, v in counts.items()}}
        con.executemany("INSERT INTO meta VALUES (?,?)", list(meta.items()))
        problems = con.execute("PRAGMA foreign_key_check").fetchall()
        if problems:
            raise RuntimeError(f"Export inkonsistent (Fremdschlüssel): {problems[:5]}")
        con.commit()
        con.execute("VACUUM")
        con.close()
        os.chmod(tmp, 0o644)
        os.replace(tmp, out)          # atomar: Karo sieht alten oder neuen Stand, nie einen halben
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return counts


def auto_export(cfg, db, log=print) -> None:
    """Nach einem Lauf/Auftrag automatisch exportieren, wenn in config.yaml eingeschaltet."""
    sc = (cfg.raw.get("sqlite_export") or {}) if hasattr(cfg, "raw") else {}
    if str(sc.get("enabled", "")).strip().lower() not in ("1", "true", "yes", "ja", "on") or not sc.get("path"):
        return
    try:
        counts = export_sqlite(db, sc["path"])
        log(f"✓ SQLite für Karo aktualisiert: {sc['path']} ({counts['concepts']} Konzepte)")
    except Exception as exc:  # noqa: BLE001 – Export darf Lauf/Dienst nicht beenden
        log(f"⚠ SQLite-Export fehlgeschlagen: {exc}")
