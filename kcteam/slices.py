"""Kuratierte vertikale Slices in die Datenbank bringen.

Die Slices leben in `karo_contract.slices` — dem gemeinsamen Paket von Karo
und diesem Dienst. Dort stehen je Konzept die Dienst-Metadaten (Niveaus,
Kalibrierung, Diagnose) und die verfasste Lektion im Format `karo-adaptiv-v1`.
Diese Datei überführt sie in das native Schema:

* Fächer, Blöcke und der Voraussetzungs-Graph wie vom Fachdidaktiker,
* Niveaus, Anker- und Grenzaufgaben wie vom Kalibrierer,
* Fehlvorstellungen, Diagnose- und Abschlussaufgaben wie vom Diagnostiker,
* visuelle Erklärungen wie vom Visual-Didaktiker — aus den
  Lektions-Komponenten abgebildet, damit `karo.visual_explanations` sie sieht,
* `lesson_draft`: die verfasste Lektion selbst. Der Export-Worker liefert
  sie nach derselben Format- und Abnehmerprüfung aus, die jede generierte
  Lektion durchläuft (Vertrag 1.5).

Kuratiert heißt: der Inhalt wurde von Menschen verfasst und steht unter
dem Vertrag — die Slices gehen als `approved` ein, wie Karos kuratierter
Katalog als `geprueft` eingeht. `seed_slices` ist idempotent und darf bei
jedem Start laufen.
"""
from __future__ import annotations

import logging
from typing import Any

from psycopg.types.json import Jsonb

from .schemas import (BoundaryItems, Calibration, ConceptDraft, Diagnostics,
                      Item, Misconception, Source, TopicBlock)

log = logging.getLogger(__name__)

#: Kuratierte Quelle, die in den Review-Tabellen erkennbar bleibt.
_QUELLE = "karo_contract.slices"


def _slices():
    try:
        from karo_contract import slices
    except ImportError as exc:  # pragma: no cover - Installationssache
        raise RuntimeError(
            "karo_contract ist nicht installiert — ohne das gemeinsame Paket "
            "gibt es keine kuratierten Slices.") from exc
    return slices


def _item(daten: dict) -> Item:
    return Item(**{k: v for k, v in daten.items()
                   if k in ("prompt", "solution", "level", "grade",
                            "representation", "answer", "distractors")})


def _calibration(konzept: dict) -> Calibration:
    return Calibration(
        concept_id=konzept["id"],
        levels=konzept["levels"],
        can_do=konzept["can_do"],
        difficulty_parameters=konzept["difficulty_parameters"],
        anchor_items=[_item(i) for i in konzept["anchor_items"]],
        boundary_items=BoundaryItems(
            **{seite: [_item(i) for i in konzept["boundary_items"].get(seite, [])]
               for seite in ("below", "within", "above")}))


def _diagnostics(konzept: dict) -> Diagnostics:
    diag = konzept["diagnostics"]
    return Diagnostics(
        concept_id=konzept["id"],
        misconceptions=[Misconception(
            key=m["key"], description=m["description"],
            remediation_hint=m["remediation_hint"],
            diagnostic_item=_item(m["diagnostic_item"]))
            for m in diag.get("misconceptions", [])],
        diagnostic_items=[_item(i) for i in diag.get("diagnostic_items", [])],
        exit_items=[_item(i) for i in diag.get("exit_items", [])])


# --------------------------------------------------------------------------
# Lektions-Komponenten -> Visual-Specs des Dienstes
# --------------------------------------------------------------------------

def _fluss(schritte: list[str], alt: str):
    """Karo `GenericStepFlow`/`ConceptMap` -> `flow_diagram` des Dienstes."""
    from .visuals.spec import FlowDiagram, FlowEdge, FlowNode
    knoten = [s.strip() for s in schritte if str(s).strip()][:12]
    if len(knoten) < 2:
        return None
    ids = [f"n{i}" for i in range(len(knoten))]
    return FlowDiagram(
        alt=alt, nodes=[FlowNode(id=i, label=t[:80]) for i, t in zip(ids, knoten)],
        edges=[FlowEdge(source=ids[i], target=ids[i + 1]) for i in range(len(ids) - 1)],
        direction="vertical")


def _netz(knoten: list[str], kanten: list[str], alt: str):
    from .visuals.spec import FlowDiagram, FlowEdge, FlowNode
    namen = [str(k).strip() for k in knoten if str(k).strip()][:12]
    if len(namen) < 2:
        return None
    ids = {name: f"n{i}" for i, name in enumerate(namen)}
    kanten_auf = []
    for kante in kanten or []:
        teile = [t.strip() for t in str(kante).split("→", 1)]
        if len(teile) == 2 and teile[0] in ids and teile[1] in ids:
            kanten_auf.append(FlowEdge(source=ids[teile[0]], target=ids[teile[1]]))
    if not kanten_auf:
        kanten_auf = [FlowEdge(source=list(ids.values())[i],
                               target=list(ids.values())[i + 1])
                      for i in range(len(ids) - 1)]
    return FlowDiagram(
        alt=alt, nodes=[FlowNode(id=ids[n], label=n[:80]) for n in namen],
        edges=kanten_auf[:16], direction="vertical")


def _tabelle(spalten: list[str], zeilen: list[str], alt: str):
    from .visuals.spec import Table
    kopf = [str(s).strip()[:80] for s in spalten if str(s).strip()][:8]
    if not kopf:
        return None
    zeilen_auf = [[z.strip()[:40] for z in str(zl).split("|")]
                  for zl in zeilen if str(zl).strip()][:12]
    zeilen_auf = [z for z in zeilen_auf if len(z) == len(kopf)]
    if not zeilen_auf:
        return None
    return Table(alt=alt, headers=kopf, rows=zeilen_auf)


def _flaeche(zeilen: int, spalten: int, markiert: int, alt: str):
    from .visuals.spec import AreaModel, GridRegion
    z, s = max(1, min(20, int(zeilen or 0))), max(1, min(20, int(spalten or 0)))
    felder = []
    for i in range(min(int(markiert or 0), z * s, 6)):
        felder.append(GridRegion(row_start=i // s, row_end=i // s + 1,
                                 col_start=i % s, col_end=i % s + 1, color=2))
    return AreaModel(alt=alt, rows=z, cols=s, regions=felder)


def _visual_spec(auswahl: dict | None):
    """Eine Karo-Komponentenauswahl in eine Visual-Spec überführen.

    Gibt None zurück, wo keine sinnvolle Abbildung existiert — eine fehlende
    Dienst-Visualisierung ist ehrlicher als eine hingebogene.
    """
    if not isinstance(auswahl, dict):
        return None
    p = auswahl.get("parameters") or {}
    alt = str(auswahl.get("alt") or "Visualisierung zum Konzept")[:400]
    if len(alt) < 5:
        alt = "Visualisierung zum Konzept"
    art = auswahl.get("component")
    try:
        if art == "GenericStepFlow":
            return _fluss(p.get("schritte") or [], alt)
        if art == "ConceptMap":
            return _netz(p.get("knoten") or [], p.get("kanten") or [], alt)
        if art == "DataTable":
            return _tabelle(p.get("spalten") or [], p.get("zeilen") or [], alt)
        if art == "AreaModel":
            return _flaeche(p.get("zeilen"), p.get("spalten"), p.get("markiert"), alt)
    except Exception:  # noqa: BLE001 - eine ungültige Spec stoppt keinen Import
        log.debug("Visual-Spec für %s nicht abbildbar", art, exc_info=True)
    return None


def _visual_set(konzept: dict):
    """VisualSet aus den Lektions-Darstellungen: Haupt- und Alternativbild
    je Fehlvorstellung als eigene visuelle Erklärung."""
    from .visuals.spec import VisualExplanation, VisualSet, VisualStep
    erklaerungen = []
    for i, fehler in enumerate(konzept["lektion"].get("fehlertypen") or [], 1):
        for feld in ("visualisierung", "visualisierung_alternativ"):
            spec = _visual_spec(fehler.get(feld))
            if spec is None:
                continue
            caption = ((fehler.get("erklaerung") or {}).get("bild") or {}) \
                .get("zeigt") or fehler.get("label") or konzept["title"]
            erklaerungen.append(VisualExplanation(
                key=f"V{len(erklaerungen) + 1}",
                purpose=str(fehler.get("label") or konzept["title"])[:200],
                steps=[VisualStep(visual=spec, caption=str(caption)[:300])]))
    if not erklaerungen:
        return None
    return VisualSet(concept_id=konzept["id"], visual_need="helpful",
                     rationale="Darstellungen der kuratierten Lektion.",
                     explanations=erklaerungen[:6])


# --------------------------------------------------------------------------
# Der Import
# --------------------------------------------------------------------------

def _stichworte(konzept: dict) -> list[str]:
    worte = list((konzept["lektion"].get("konzept") or {}).get("stichworte") or [])
    worte.append(konzept["title"])
    return list(dict.fromkeys(w for w in worte if str(w).strip()))[:12]


def seed_slices(db, *, faecher: set[str] | None = None) -> dict:
    """Schreibt alle kuratierten Slices in den Katalog. Idempotent.

    Gibt je Fach die gezählten Konzepte zurück — für das Protokoll und für
    Tests, die prüfen, dass nichts verloren ging.
    """
    ergebnis: dict[str, list[str]] = {}
    for fach, sl in _slices().SLICES.items():
        if faecher and fach not in faecher:
            continue
        code = sl["code"]
        konzepte = [k for b in sl["blocks"] for k in b["concepts"]]
        db.save_subject(code, sl["name"], (min(k["first_contact_grade"] for k in konzepte),
                                           max(k["target_grade"] for k in konzepte)))
        for block in sl["blocks"]:
            db.save_block(code, TopicBlock(
                id=block["id"], title=block["title"],
                description=block.get("description", ""),
                grade_min=block["grade_min"], grade_max=block["grade_max"],
                typical_grade=block.get("typical_grade") or block["grade_min"],
                varies=bool(block.get("varies")),
                variance_note=block.get("variance_note"),
                sources=[Source(title=s["title"], url=s.get("url"))
                         for s in block.get("sources", [])
                         if isinstance(s, dict) and s.get("title")]
                or [Source(title="Kuratierter Slice (karo_contract)")]))
            db.save_graph(code, block["id"], [
                ConceptDraft(id=k["id"], title=k["title"],
                             description=k["description"],
                             first_contact_grade=k["first_contact_grade"],
                             target_grade=k["target_grade"],
                             prerequisites=list(k.get("prerequisites") or []),
                             order=i)
                for i, k in enumerate(block["concepts"], 1)])
            db.set_block_status(block["id"], "done")
            for k in block["concepts"]:
                db.save_calibration(k["id"], _calibration(k))
                db.save_diagnostics(k["id"], _diagnostics(k))
                vset = _visual_set(k)
                if vset is not None:
                    db.save_visuals(k["id"], vset)
                else:
                    db.query("UPDATE curriculum.concepts SET visual_need='none', "
                             "visuals=%s WHERE id=%s",
                             (Jsonb({"concept_id": k["id"], "visual_need": "none",
                                     "rationale": "Die Lektion erklärt ohne Bild."}),
                              k["id"]))
                # Kuratiert = freigegeben: die Prüfung läuft beim Export
                # noch einmal gegen dasselbe Paket wie bei Karo.
                db.query("""UPDATE curriculum.concepts
                               SET lesson_draft=%s, search_terms=%s::text[],
                                   status='approved', updated_at=now()
                             WHERE id=%s""",
                         (Jsonb(k["lektion"]), _stichworte(k), k["id"]))
                ergebnis.setdefault(fach, []).append(k["id"])
    return ergebnis
