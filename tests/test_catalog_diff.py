"""Katalog: Diff, Provenienz, Selektion (PART 3, 9-12). Reine Logik – kein DB."""
from __future__ import annotations

from kcteam.catalog import (
    Scope, compute_diff, slug_id, subjects_for_grade,
)

BESTAND = [
    {"id": "DE.MA.6.BRUECHE", "title": "Brueche", "kind": "topic", "parent_id": "DE.MA",
     "subject_code": "MA", "grade": 6, "framework": "de-kmk", "country": "DE",
     "region": "", "school_type": "", "path": ["Mathematik", "Brueche"],
     "sort_order": 0, "status": "active", "description": ""},
    {"id": "DE.MA.6.DEZI", "title": "Dezimalbrueche", "kind": "topic", "parent_id": "DE.MA",
     "subject_code": "MA", "grade": 6, "framework": "de-kmk", "country": "DE",
     "region": "", "school_type": "", "path": [], "sort_order": 1,
     "status": "active", "description": ""},
]


def test_slug_id_stabil():
    assert slug_id("Mathematik", "Brüche") == "MATHEMATIK.BRUCHE"
    assert slug_id("DE", "10. Klasse") == "DE.10_KLASSE"


def test_faecher_matrix():
    assert "SU" in subjects_for_grade(1) and "PH" not in subjects_for_grade(1)
    assert "PH" in subjects_for_grade(8) and "DE" in subjects_for_grade(8)


def test_diff_unveraendert():
    changes = compute_diff(BESTAND, [dict(b) for b in BESTAND])
    assert all(c["op"] == "UNCHANGED" for c in changes)


def test_diff_add_rename_move_deprecate():
    v = [dict(b) for b in BESTAND]
    v[0]["title"] = "Bruchrechnung"                    # RENAME
    v[1]["parent_id"] = "DE.MA.NEU"                    # MOVE
    v.append({"id": "DE.MA.6.PROZENT", "title": "Prozente", "kind": "topic",
              "parent_id": "DE.MA", "subject_code": "MA", "grade": 6,
              "framework": "de-kmk", "country": "DE", "region": "", "school_type": "",
              "path": [], "sort_order": 2, "status": "active", "description": ""})
    changes = compute_diff(BESTAND, v)
    ops = {(c["item_id"] or c["proposed"]["id"]): c["op"] for c in changes}
    assert ops["DE.MA.6.BRUECHE"] == "RENAME"
    assert ops["DE.MA.6.DEZI"] == "MOVE"
    assert ops["DE.MA.6.PROZENT"] == "ADD"


def test_diff_deprecate_statt_loeschen():
    changes = compute_diff(BESTAND, [dict(BESTAND[0])])
    dep = [c for c in changes if c["op"] == "DEPRECATE"]
    assert dep and dep[0]["item_id"] == "DE.MA.6.DEZI"


def test_scope_themen_nur_auswahl():
    s = Scope(grades=[6], subjects=["MA"], topics=["DE.MA.6.BRUECHE", "Altes Thema"])
    assert s.subject_codes() == ["MA"]
    assert len(s.topics) == 2
