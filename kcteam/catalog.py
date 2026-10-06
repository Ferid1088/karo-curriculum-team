"""Themenkatalog (PART 5-12): Was soll gelernt werden?

Zweistufig: recherchierte Eintraege landen als Change-Set mit Diff im
Staging – ein Mensch gibt sie frei, bevor sie den freigegebenen Katalog
veraendern. Der Katalog ist hierarchisch:

    SUBJECT -> DOMAIN/THEMENBEREICH -> TOPIC -> SUBTOPIC -> COMPETENCY
             -> ATOMIC_CONCEPT

Selektion (PART 3): eine Auswahl (Lehrplanwerk, Schulart, Klassen, Faecher,
Themen) wird deterministisch in eine Liste freigegebener Themen aufgeloest
– ohne Modellaufruf.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Literal

from psycopg.types.json import Jsonb

KINDS = ("subject", "domain", "topic", "subtopic", "competency", "atomic_concept")
OPS = ("ADD", "RENAME", "UPDATE", "MOVE", "SPLIT", "MERGE", "DEPRECATE", "UNCHANGED")
ITEM_COLS = ("title", "description", "framework", "country", "region", "school_type",
             "grade", "subject_code", "path", "sort_order", "kind", "parent_id")


#: Faecher-Matrix (PART 2): welche Hauptfaecher pro Klassenbereich aktiviert
#: sind. Konfiguration, kein hartes Limit – erweiterbar ueber config.yaml.
DEFAULT_SUBJECT_MATRIX: dict[tuple[int, int], list[str]] = {
    (1, 2):  ["DE", "MA", "SU"],
    (3, 4):  ["DE", "MA", "EN", "SU"],
    (5, 6):  ["DE", "MA", "EN", "NW"],
    (7, 10): ["DE", "MA", "EN", "BI", "PH", "CH"],
    (11, 13): ["DE", "MA", "EN", "BI", "PH", "CH"],
}


def subjects_for_grade(grade: int, matrix: dict | None = None) -> list[str]:
    """Welche Faecher sind in dieser Klassenstufe aktiviert?"""
    m = matrix or DEFAULT_SUBJECT_MATRIX
    out: list[str] = []
    for (lo, hi), codes in m.items():
        if lo <= grade <= hi:
            out += [c for c in codes if c not in out]
    return out


def slug_id(*parts: str) -> str:
    """Stabiler Katalog-Schluessel aus Pfadteilen."""
    def clean(s: str) -> str:
        s = unicodedata.normalize("NFKD", s)
        s = s.encode("ascii", "ignore").decode().upper()
        return re.sub(r"[^A-Z0-9]+", "_", s).strip("_") or "X"
    return ".".join(clean(p) for p in parts if p)


@dataclass
class Scope:
    """PART 3: die Auswahl des Admins."""
    framework: str = "de-kmk"
    country: str = "DE"
    region: str = ""
    school_type: str = ""
    grades: list[int] = field(default_factory=list)
    subjects: list[str] = field(default_factory=list)   # Fachkuerzel
    topics: list[str] = field(default_factory=list)     # catalog_item-IDs oder Titel
    matrix: dict | None = None                          # optionale Faecher-Matrix

    def subject_codes(self) -> list[str]:
        if self.subjects:
            return [s.upper() for s in self.subjects]
        out: list[str] = []
        for g in self.grades:
            out += [c for c in subjects_for_grade(g, self.matrix) if c not in out]
        return out


def resolve_scope(db, scope: Scope) -> list[dict]:
    """Auswahl -> freigegebene Katalog-Themen (kind 'topic' oder feiner,
    die in einem gewaehlten Thema haengen). Deterministisch, ohne KI."""
    subjects = scope.subject_codes()
    if not subjects:
        return []
    if scope.topics:
        keys = [t.strip() for t in scope.topics]
        rows = db.query(
            """SELECT * FROM curriculum.catalog_items
               WHERE framework=%s AND coalesce(region,'')=%s
                 AND coalesce(school_type,'')=%s
                 AND status='active'
                 AND (id = ANY(%s) OR upper(title) = ANY(%s))""",
            (scope.framework, scope.region, scope.school_type,
             keys, [k.upper() for k in keys]))
        found_ids = {r["id"] for r in rows}
        # Kinder der gewaehlten Themen (Subtopics etc.) kommen mit
        extra = db.query(
            """SELECT * FROM curriculum.catalog_items
               WHERE framework=%s AND status='active' AND parent_id = ANY(%s)""",
            (scope.framework, list(found_ids)))
        rows += [r for r in extra if r["id"] not in found_ids]
        return sorted(rows, key=lambda r: (r["subject_code"], r["grade"] or 0, r["sort_order"], r["id"]))
    grades = scope.grades or list(range(1, 14))
    rows = db.query(
        """SELECT * FROM curriculum.catalog_items
           WHERE framework=%s AND coalesce(region,'')=%s AND coalesce(school_type,'')=%s
             AND subject_code = ANY(%s) AND status='active'
             AND kind IN ('topic','subtopic')
             AND (grade IS NULL OR grade = ANY(%s))""",
        (scope.framework, scope.region, scope.school_type, subjects, grades))
    return sorted(rows, key=lambda r: (r["subject_code"], r["grade"] or 0, r["sort_order"], r["id"]))


# ---------------------------------------------------------------- Diff
def _fingerprint(item: dict) -> dict:
    """Vergleichsfelder eines Katalogeintrags (ohne Provenienz/Zeit)."""
    return {k: item.get(k) for k in ITEM_COLS}


def compute_diff(existing: list[dict], proposed: list[dict]) -> list[dict]:
    """Bestand gegen Recherche: ADD/UPDATE/RENAME/MOVE/DEPRECATE/UNCHANGED.

    Gleiche ID + geaenderter Titel = RENAME; anderer parent_id = MOVE;
    andere Felder = UPDATE. Nicht mehr gefundene Bestandseintraege werden
    DEPRECATED – niemals still geloescht (PART 10).
    """
    changes: list[dict] = []
    old = {i["id"]: i for i in existing}
    seen: set[str] = set()
    for p in proposed:
        pid = p["id"]
        seen.add(pid)
        cur = old.get(pid)
        if cur is None:
            changes.append({"op": "ADD", "item_id": None, "proposed": p,
                            "diff_note": "neu"})
            continue
        cur, cand = _fingerprint(cur), _fingerprint(p)
        if cur == cand:
            changes.append({"op": "UNCHANGED", "item_id": pid, "proposed": p, "diff_note": ""})
            continue
        ops: list[str] = []
        if cur["title"] != cand["title"]:
            ops.append("RENAME")
        if cur["parent_id"] != cand["parent_id"]:
            ops.append("MOVE")
        other = [k for k in ITEM_COLS if k not in ("title", "parent_id") and cur[k] != cand[k]]
        if other:
            ops.append("UPDATE")
        note = ", ".join(f"{k}: {cur[k]!r} -> {cand[k]!r}"
                         for k in ("title", "parent_id", *other))
        changes.append({"op": ops[0] if ops else "UPDATE", "item_id": pid, "proposed": p,
                        "diff_note": note})
    for i in existing:
        if i["id"] not in seen and i.get("status") == "active":
            changes.append({"op": "DEPRECATE", "item_id": i["id"],
                            "proposed": {**_fingerprint(i), "id": i["id"], "status": "deprecated"},
                            "diff_note": "in der Recherche nicht mehr aufgefuehrt"})
    return changes


def stage_change_set(db, scope: Scope, proposed: list[dict], *, agent_role: str = "",
                     provider: str = "", model: str = "", run_id: str | None = None,
                     summary: str = "") -> dict:
    """Recherche-Ergebnis einstellen: Diff berechnen, Change-Set ablegen.
    Rueckgabe: das Set inkl. der Aenderungen zur Anzeige."""
    subjects = scope.subject_codes()
    existing = db.query(
        """SELECT * FROM curriculum.catalog_items
           WHERE framework=%s AND coalesce(region,'')=%s AND coalesce(school_type,'')=%s
             AND subject_code = ANY(%s)
             AND (%s::int[] = '{}' OR grade = ANY(%s) OR grade IS NULL)""",
        (scope.framework, scope.region, scope.school_type, subjects,
         scope.grades, scope.grades))
    changes = compute_diff(existing, proposed)
    row = db.one("""INSERT INTO curriculum.catalog_change_sets(scope, provider, model, agent_role, run_id, summary)
                    VALUES (%s,%s,%s,%s,%s,%s) RETURNING *""",
                 (Jsonb({"framework": scope.framework, "region": scope.region,
                         "school_type": scope.school_type, "grades": scope.grades,
                         "subjects": subjects, "topics": scope.topics}),
                  provider, model, agent_role, run_id, summary))
    for c in changes:
        db.query("""INSERT INTO curriculum.catalog_changes(change_set_id, op, item_id, proposed, diff_note)
                    VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
                 (row["id"], c["op"], c["item_id"], Jsonb(c["proposed"]), c["diff_note"]))
    row["changes"] = db.query("SELECT * FROM curriculum.catalog_changes WHERE change_set_id=%s ORDER BY id",
                              (row["id"],))
    return row


# ---------------------------------------------------------------- Entscheiden & Anwenden
def apply_change(db, change: dict, *, edited: dict | None = None, decided_by: str = "") -> None:
    """Eine freigegebene Aenderung auf den Bestand anwenden."""
    p = dict(edited or change["proposed"])
    op = change["op"]
    if op == "UNCHANGED":
        return
    if op == "ADD":
        db.query(f"""INSERT INTO curriculum.catalog_items(id, parent_id, kind, title, description,
                        framework, country, region, school_type, grade, subject_code, path, sort_order,
                        status, origin, source, source_version, source_reference, retrieved_at,
                        agent_role, provider, model, confidence)
                     VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'active',
                             coalesce(%s,'agent_research'),%s,%s,%s,%s,%s,%s,%s,%s)
                     ON CONFLICT (id) DO UPDATE SET title=EXCLUDED.title, status='active',
                           updated_at=now()""",
                 (p["id"], p.get("parent_id"), p.get("kind", "topic"), p["title"],
                  p.get("description", ""), p.get("framework", "de-kmk"), p.get("country", "DE"),
                  p.get("region", ""), p.get("school_type", ""), p.get("grade"),
                  p["subject_code"], p.get("path") or [], p.get("sort_order", 0),
                  p.get("origin"), p.get("source", ""), p.get("source_version", ""),
                  p.get("source_reference", ""), p.get("retrieved_at"), p.get("agent_role"),
                  p.get("provider"), p.get("model"), p.get("confidence")))
        return
    if op == "DEPRECATE":
        db.query("UPDATE curriculum.catalog_items SET status='deprecated', updated_at=now() WHERE id=%s",
                 (change["item_id"],))
        _invalidate_packages(db, change["item_id"])
        return
    if op == "SPLIT":
        # SPLIT: das angegebene Elternelement bleibt (deprecated), die
        # vorgeschlagenen Kinder werden angelegt.
        db.query("UPDATE curriculum.catalog_items SET status='deprecated', updated_at=now() WHERE id=%s",
                 (change["item_id"],))
        apply_change(db, {**change, "op": "ADD"}, edited=edited, decided_by=decided_by)
        return
    if op == "MERGE":
        for src in p.get("merge_from", []):
            db.query("UPDATE curriculum.catalog_items SET status='deprecated', updated_at=now() WHERE id=%s",
                     (src,))
        apply_change(db, {**change, "op": "ADD"}, edited=edited, decided_by=decided_by)
        return
    # RENAME | UPDATE | MOVE: Ziel ueberschreiben (nur Fingerprint-Felder)
    sets, vals = [], []
    for k in ITEM_COLS:
        if k in p:
            sets.append(f"{k}=%s")
            vals.append(p[k])
    sets.append("updated_at=now()")
    db.query(f"UPDATE curriculum.catalog_items SET {', '.join(sets)} WHERE id=%s",
             (*vals, change["item_id"]))
    _invalidate_packages(db, change["item_id"])


def _invalidate_packages(db, item_id: str) -> None:
    """Ein geaenderter/veralteter Katalogeintrag macht sein Paket ungueltig –
    OUTDATED, nicht still neu gebaut (PART 81)."""
    db.query("""UPDATE curriculum.complete_packages
                SET status='OUTDATED', updated_at=now()
                WHERE topic_id=%s AND status IN ('READY_CORE','READY_COMPLETE','PARTIAL')""",
             (item_id,))


def decide_change_set(db, set_id: int, *, approve: bool, decided_by: str = "",
                      rejected_ops: list[int] | None = None,
                      edits: dict[int, dict] | None = None) -> dict:
    """Mensch entscheidet ueber das Change-Set: einzelne Aenderungen duerfen
    abgelehnt oder nachbearbeitet werden (edits: change_id -> Felder)."""
    changes = db.query("SELECT * FROM curriculum.catalog_changes WHERE change_set_id=%s ORDER BY id",
                       (set_id,))
    if not approve:
        db.query("""UPDATE curriculum.catalog_change_sets SET status='rejected', decided_at=now(),
                    decided_by=%s WHERE id=%s""", (decided_by, set_id))
        return {"status": "rejected", "applied": 0}
    applied = 0
    rej = set(rejected_ops or [])
    for ch in changes:
        if ch["id"] in rej:
            db.query("""UPDATE curriculum.catalog_changes SET decision='rejected', decided_by=%s
                        WHERE id=%s""", (decided_by, ch["id"]))
            continue
        edited = (edits or {}).get(ch["id"])
        apply_change(db, ch, edited=edited, decided_by=decided_by)
        db.query("""UPDATE curriculum.catalog_changes SET decision=%s, edited=%s, decided_by=%s
                    WHERE id=%s""",
                 ("edited" if edited else "approved",
                  Jsonb(edited) if edited else None, decided_by, ch["id"]))
        applied += 1
    db.query("""UPDATE curriculum.catalog_change_sets SET status='approved', decided_at=now(),
                decided_by=%s WHERE id=%s""", (decided_by, set_id))
    return {"status": "approved", "applied": applied, "rejected": len(rej)}


# ---------------------------------------------------------------- Manuelle Pflege (PART 11)
def manual_add(db, item: dict, *, decided_by: str = "manual") -> dict:
    item = {**item, "origin": "manual"}
    apply_change(db, {"op": "ADD", "item_id": None, "proposed": item}, decided_by=decided_by)
    return db.one("SELECT * FROM curriculum.catalog_items WHERE id=%s", (item["id"],))


def manual_edit(db, item_id: str, **fields) -> None:
    """Umbenennen, Beschreibung, Sortierung, Elternteil …"""
    allowed = {"title", "description", "parent_id", "sort_order", "grade",
               "region", "school_type", "framework"}
    sets = [f"{k}=%s" for k in fields if k in allowed]
    vals = [fields[k] for k in fields if k in allowed]
    if not sets:
        return
    sets.append("updated_at=now()")
    db.query(f"UPDATE curriculum.catalog_items SET {', '.join(sets)} WHERE id=%s", (*vals, item_id))


def manual_deactivate(db, item_id: str, *, restore: bool = False) -> None:
    db.query("UPDATE curriculum.catalog_items SET status=%s, updated_at=now() WHERE id=%s",
             ("active" if restore else "deactivated", item_id))


def manual_merge(db, target_id: str, source_ids: list[str]) -> None:
    db.query("""UPDATE curriculum.catalog_items SET status='deprecated', updated_at=now(),
                description=description || ' [zusammengefuehrt in ' || %s || ']'
                WHERE id = ANY(%s)""", (target_id, source_ids))
    db.query("UPDATE curriculum.catalog_items SET parent_id=%s WHERE parent_id = ANY(%s)",
             (target_id, source_ids))


def manual_split(db, item_id: str, children: list[dict]) -> list[dict]:
    """Ein Thema aufspalten: Quelle wird deprecated, Kinder haengen an ihrem Elternteil."""
    src = db.one("SELECT * FROM curriculum.catalog_items WHERE id=%s", (item_id,))
    if not src:
        raise ValueError(f"Katalogeintrag {item_id} unbekannt")
    made = []
    for c in children:
        c = {**c, "parent_id": src["parent_id"], "framework": src["framework"],
             "country": src["country"], "region": src["region"],
             "school_type": src["school_type"], "subject_code": src["subject_code"],
             "grade": c.get("grade", src["grade"])}
        made.append(manual_add(db, c))
    manual_deactivate(db, item_id)
    return made


# ---------------------------------------------------------------- Ausblick
def catalog_overview(db, scope: Scope) -> dict:
    """Fuer die Admin-Ansicht: freigegebene Themen, gestufte Sets, Frische."""
    subjects = scope.subject_codes()
    items = resolve_scope(db, scope)
    staged = db.query("""SELECT c.id, c.status, c.summary, c.created_at, c.provider, c.model,
                                (SELECT count(*) FROM curriculum.catalog_changes x
                                  WHERE x.change_set_id=c.id AND x.op <> 'UNCHANGED') AS pending_ops
                         FROM curriculum.catalog_change_sets c
                         WHERE c.status='staged' ORDER BY c.id DESC LIMIT 20""")
    freshness = db.query("""SELECT subject_code, max(retrieved_at) AS zuletzt, count(*) AS eintraege
                            FROM curriculum.catalog_items
                            WHERE subject_code = ANY(%s) AND status='active'
                            GROUP BY 1 ORDER BY 1""", (subjects,))
    return {"topics": items, "staged_sets": staged, "freshness": freshness,
            "subjects": subjects}
