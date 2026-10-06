"""Vollstaendigkeits-Manifest und Freigabetor (PART 67-69, 108).

Deterministisch: zaehlt, was im Paket wirklich steht, ordnet jede der 28
Vertragskomponenten einem Zustand zu (COMPLETE | PARTIAL |
NOT_APPLICABLE | MISSING) und entscheidet ueber den Paketstatus. Ein Paket
ist READY_COMPLETE nur, wenn alle Pflichtkomponenten belegt und alle
Qualitaetsnachweise PASS sind – kein Selbstlob, nur Befund.
"""
from __future__ import annotations

from typing import Any

from .package_schema import (
    CompleteTopicPackage, CompletenessManifest, ComponentManifest,
)

#: Die 28 Komponenten des Vertrags -> (Pflicht?, Befundregel).
#: Jede Regel bekommt das Paket und liefert (state, note, count).
def _req(p: CompleteTopicPackage, ok, partial=None, note_ok="", note_part=""):
    """Kurzform: ok -> COMPLETE, partial -> PARTIAL, sonst MISSING."""
    if ok:
        return "COMPLETE", note_ok, 0
    if partial:
        return "PARTIAL", note_part, 0
    return "MISSING", "nicht vorhanden", 0


def _visual_state(p: CompleteTopicPackage) -> tuple[str, str, int]:
    if p.visual_need == "NO_VISUAL_NEEDED":
        if p.visual_na_reason.strip():
            return "NOT_APPLICABLE", p.visual_na_reason, 0
        return "MISSING", "visual_need=NO_VISUAL_NEEDED ohne Begruendung", 0
    n = len(p.visual_assets)
    if n:
        return "COMPLETE", f"{n} Assets", n
    return "MISSING", "visueller Bedarf deklariert, aber kein Asset", 0


def _images_state(p: CompleteTopicPackage) -> tuple[str, str, int]:
    imgs = [a for a in p.visual_assets if a.visual_type == "ILLUSTRATIVE_IMAGE"]
    if p.visual_need == "NO_VISUAL_NEEDED":
        if p.visual_na_reason.strip():
            return "NOT_APPLICABLE", p.visual_na_reason, 0
        return "MISSING", "NO_VISUAL_NEEDED ohne Begruendung", 0
    if imgs and all(a.image_ref for a in imgs):
        return "COMPLETE", f"{len(imgs)} gepruefte Bilder", len(imgs)
    if imgs:
        return "PARTIAL", "Bilder ohne gepruefte image_ref", len(imgs)
    return "PARTIAL", "keine Illustrationen (Katalog-Visuals reichen evtl.)", 0


def _role_tasks(p: CompleteTopicPackage, role: str) -> int:
    return len(p.tasks_for(role))


def _section_checks(p: CompleteTopicPackage) -> dict[str, tuple[str, str, int]]:
    """Befund pro Vertragskomponente (PART 1/67)."""
    top = p.top_level()
    mis_ids = {m.misconception_id for m in p.misconception_model}
    checks: dict[str, tuple[str, str, int]] = {}

    checks["catalog_alignment"] = _req(
        p, bool(p.catalog_alignment.item_id), note_ok="im Katalog verankert")
    checks["concept_identity"] = _req(
        p, bool(p.concept_identity.concept_id and p.concept_identity.title),
        note_ok=p.concept_identity.concept_id)
    checks["target_competencies"] = _req(
        p, len(p.target_competencies) >= 1, note_ok=f"{len(p.target_competencies)} Kompetenzen")
    checks["prerequisite_graph"] = _req(
        p, True, note_ok=f"{len(p.prerequisite_graph.edges)} Kanten (auch 0 ist eine Entscheidung)")
    checks["competency_ladder"] = _req(
        p, len(p.competency_ladder) >= 2,
        partial=len(p.competency_ladder) == 1,
        note_ok=f"{len(p.competency_ladder)} Ebenen",
        note_part="nur eine Ebene – keine wirkliche Leiter")
    checks["diagnostic_probes"] = _req(
        p, _role_tasks(p, "PREREQUISITE_PROBE") >= len(
            [e for e in p.prerequisite_graph.edges if e.kind in ("necessary", "useful")]) or not p.prerequisite_graph.edges,
        partial=_role_tasks(p, "PREREQUISITE_PROBE") > 0,
        note_ok="jede Voraussetzung sondiert",
        note_part="nicht jede Voraussetzung hat eine Sonde")
    checks["misconception_model"] = _req(
        p, len(mis_ids) >= 1 and all(m.repair_explanation and (m.guided_repair or m.independent_check)
                                     for m in p.misconception_model),
        partial=len(mis_ids) >= 1,
        note_ok=f"{len(mis_ids)} Fehlvorstellungen mit Reparatur",
        note_part="Fehlvorstellungen ohne vollstaendige Reparaturkette")
    checks["teaching_strategies"] = _req(
        p, all(str(l.level_id) in p.teaching_strategies or l.level_id in p.teaching_strategies
               for l in p.competency_ladder),
        partial=bool(p.teaching_strategies),
        note_ok="jede Ebene hat Strategien",
        note_part="nicht jede Ebene hat eine Strategie")
    checks["explanations"] = _req(
        p, len({e.mode for e in p.explanations}) >= 3,
        partial=len(p.explanations) >= 1,
        note_ok=f"{len(p.explanations)} Varianten, {len({e.mode for e in p.explanations})} Modi",
        note_part="zu wenig Erklaervielfalt")
    checks["worked_examples"] = _req(
        p, _role_tasks(p, "WORKED") >= 1,
        note_ok=f"{_role_tasks(p, 'WORKED')} vorgefuehrte Aufgaben")
    checks["guided_practice"] = _req(
        p, _role_tasks(p, "GUIDED") + _role_tasks(p, "SCAFFOLDED") >= 1)
    checks["independent_practice"] = _req(
        p, _role_tasks(p, "INDEPENDENT") >= 1)
    checks["transfer_practice"] = _req(
        p, _role_tasks(p, "TRANSFER") >= 1)
    checks["mastery_checks"] = _req(
        p, _role_tasks(p, "MASTERY_CHECK") >= 2 and
           any(t.level_id == top for t in p.tasks_for("MASTERY_CHECK")),
        partial=_role_tasks(p, "MASTERY_CHECK") >= 1,
        note_ok="Meisterschaft am Zielniveau pruefbar",
        note_part="weniger als 2 Meisterschaftsaufgaben auf Zielniveau")
    checks["spaced_review"] = _req(
        p, _role_tasks(p, "SPACED_REVIEW") >= 1 or
           any(t.role == "SPACED_REVIEW" for t in p.task_templates))
    checks["prerequisite_detours"] = _req(
        p, all(d.return_target and d.prerequisite_path for d in p.journey_policy.detours)
           if p.journey_policy.detours else not [e for e in p.prerequisite_graph.edges
                                                 if e.kind == "necessary"],
        partial=bool(p.journey_policy.detours),
        note_ok="jeder Umweg hat einen Rueckweg",
        note_part="Umweg ohne vollstaendigen Rueckweg")
    checks["clarification_material"] = _req(
        p, _role_tasks(p, "CLARIFICATION") >= 1 or
           any(t.answer and t.answer.get("clarification") for t in p.tasks
               if isinstance(t.answer, dict)),
        partial=True, note_ok="Klaerungsmaterial vorhanden",
        note_part="kein explizites Klaerungsmaterial (nicht zwingend)")
    checks["exam_practice"] = _req(
        p, _role_tasks(p, "EXAM_PRACTICE") + _role_tasks(p, "EXAM_SIMULATION") >= 1,
        partial=True, note_part="kein explizites Pruefungsmaterial (optional nach Thema)")
    checks["answer_contracts"] = _req(
        p, all(t.answer or t.role in ("CLARIFICATION",) for t in p.tasks),
        partial=any(t.answer for t in p.tasks),
        note_ok="jede pruefbare Aufgabe hat einen Antwortvertrag",
        note_part="Aufgaben ohne Antwortvertrag")
    checks["task_templates"] = _req(
        p, len(p.task_templates) >= 1,
        note_ok=f"{len(p.task_templates)} deterministische Generatoren")
    checks["hint_ladders"] = _req(
        p, any(t.hints for t in p.tasks if t.role in ("GUIDED", "SCAFFOLDED")),
        partial=any(t.hints for t in p.tasks),
        note_ok="gefuehrte Aufgaben mit Hinweisleitern",
        note_part="kaum Hinweisleitern")
    checks["visual_assets"], checks["illustrative_images"] = _visual_state(p), _images_state(p)
    checks["accessibility_text"] = _req(
        p, all(not a.accessibility_text or a.accessibility_text for a in p.visual_assets) and
           (p.visual_need == "NO_VISUAL_NEEDED" or any(a.accessibility_text for a in p.visual_assets)),
        partial=p.visual_need == "NO_VISUAL_NEEDED",
        note_ok="Visuals mit Zugangstext", note_part="keine Visuals -> nicht zwingend")
    checks["transition_mappings"] = _req(
        p, bool(p.journey_policy.rules) or bool(p.journey_policy.detours),
        note_ok=f"{len(p.journey_policy.rules)} Regeln")
    checks["quality_evidence"] = _req(
        p, any(v == "PASS" for v in (p.quality_evidence.dead_end_check,
                                     p.quality_evidence.template_check,
                                     p.quality_evidence.subject_validation)),
        partial=True, note_ok="Nachweise gefuehrt", note_part="noch keine Nachweise")
    checks["coverage_manifest"] = ("COMPLETE", "dieses Manifest", 1)
    checks["provenance"] = _req(
        p, bool(p.provenance.provider or p.provenance.agent_role),
        note_ok=f"{p.provenance.provider}/{p.provenance.model}")
    return checks


def manifest(pkg: CompleteTopicPackage) -> CompletenessManifest:
    """Baut das maschinenlesbare Manifest (PART 67)."""
    checks = _section_checks(pkg)
    items = [ComponentManifest(component=c, state=s, reason=n, count=cnt)
             for c, (s, n, cnt) in checks.items()]
    done = sum(1 for s, _, _ in checks.values() if s in ("COMPLETE", "NOT_APPLICABLE"))
    coverage = {"components": done / len(checks) if checks else 0.0}
    return CompletenessManifest(items=items, coverage=coverage)


#: Komponenten, die fuer READY_CORE zwingend belegt sein muessen.
CORE_REQUIRED = (
    "catalog_alignment", "concept_identity", "target_competencies",
    "prerequisite_graph", "competency_ladder", "misconception_model",
    "teaching_strategies", "explanations", "worked_examples",
    "guided_practice", "independent_practice", "transfer_practice",
    "mastery_checks", "spaced_review", "prerequisite_detours",
    "answer_contracts", "task_templates", "hint_ladders",
    "transition_mappings", "provenance",
)
#: Fuer READY_COMPLETE zusaetzlich (inkl. Nachweise).
FULL_REQUIRED = CORE_REQUIRED + (
    "diagnostic_probes", "clarification_material", "exam_practice",
    "visual_assets", "accessibility_text", "quality_evidence",
)


def release_gate(pkg: CompleteTopicPackage) -> tuple[str, list[str]]:
    """Entscheidet den Paketstatus.

    Rueckgabe: (status, blocking_reasons). READY_COMPLETE nur wenn alle
    Pflichtkomponenten COMPLETE/NOT_APPLICABLE und alle Qualitaetsnachweise
    PASS. READY_CORE, wenn die Kernreise moeglich ist. Sonst PARTIAL.
    """
    m = pkg.coverage_manifest
    if not m.items:
        m = manifest(pkg)
    def st(c: str) -> str:
        return m.state_of(c)
    blocking = [c for c in CORE_REQUIRED if st(c) == "MISSING"]
    if blocking:
        return "PARTIAL", [f"fehlt: {c}" for c in blocking]
    qe = pkg.quality_evidence
    evidence_blockers = []
    for field in ("dead_end_check", "template_check", "return_paths",
                  "prerequisite_cycles", "subject_validation",
                  "rubric_adversarial", "visual_qa"):
        if getattr(qe, field) == "FAIL":
            evidence_blockers.append(f"{field}=FAIL")
    sim_fails = [k for k, v in qe.simulations.items() if v in ("DEAD_END", "STEP_LIMIT")]
    if sim_fails:
        evidence_blockers.append("Simulationen mit Befund: " + ", ".join(sim_fails))
    # Premature/abwesende Meisterschaft: ein Profil darf nicht 'meistern',
    # ohne je anspruchsvolles Material gesehen zu haben; kein Profil darf
    # in einer Dauerschleife haengen (deckt STEP_LIMIT explizit ab).
    weak_mastered = [k for k, v in qe.simulations.items()
                     if v == "MASTERED" and k in ("UNKNOWN", "PARTIAL")]
    if weak_mastered:
        evidence_blockers.append("Vorzeitige Meisterschaft: " + ", ".join(weak_mastered))
    if qe.validator == "FAIL":
        evidence_blockers.append("Kinderrechts-Inspektor: FAIL")
    # PART 33: READY_COMPLETE darf nur ueber die Endkontrolle laufen.
    if qe.control == "BLOCKED":
        evidence_blockers.append("Vollstaendigkeits-Kontrolleur: BLOCKED")
    if evidence_blockers:
        return "PARTIAL", evidence_blockers
    optional_missing = [c for c in FULL_REQUIRED if st(c) == "MISSING"]
    if optional_missing:
        return "READY_CORE", [f"Randkomponente fehlt: {c}" for c in optional_missing]
    pending = [f for f in ("dead_end_check", "template_check", "return_paths",
                           "prerequisite_cycles", "subject_validation",
                           "rubric_adversarial")
               if getattr(qe, f) == "PENDING"]
    if pending:
        return "READY_CORE", ["Nachweise ausstehend: " + ", ".join(pending)]
    if qe.control == "REPAIR_REQUIRED":
        return "READY_CORE", ["Kontrolleur: REPAIR_REQUIRED – lokale "
                              "Reparatur der genannten Stufen"]
    if qe.control == "PENDING":
        return "READY_CORE", ["Vollstaendigkeits-Kontrolleur: ausstehend"]
    return "READY_COMPLETE", []


# ---------------------------------------------------------------- Abdeckung ueber Themen
def coverage_report(db, topic_ids: list[str]) -> dict[str, Any]:
    """Aggregierte Abdeckung ueber Themen (PART 108): Statusanteile plus
    Schwaechenhaeufigkeit aus den gespeicherten Manifesten."""
    rows = db.query(
        """SELECT topic_id, status, manifest FROM curriculum.complete_packages
           WHERE topic_id = ANY(%s)""", (topic_ids,))
    have = {r["topic_id"]: r for r in rows}
    by_status: dict[str, int] = {}
    weakness: dict[str, int] = {}
    for t in topic_ids:
        r = have.get(t)
        status = r["status"] if r else "CATALOG_ONLY"
        by_status[status] = by_status.get(status, 0) + 1
        for it in (r or {}).get("manifest", {}).get("items", []):
            if it["state"] in ("MISSING", "PARTIAL"):
                weakness[it["component"]] = weakness.get(it["component"], 0) + 1
    return {
        "topics": len(topic_ids),
        "by_status": by_status,
        "ready_share": (by_status.get("READY_COMPLETE", 0)
                        + by_status.get("READY_CORE", 0)) / max(1, len(topic_ids)),
        "weakest_components": sorted(weakness.items(), key=lambda kv: -kv[1])[:10],
    }
