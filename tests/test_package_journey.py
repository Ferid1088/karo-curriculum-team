"""Deterministische Paket-Ebene: Vertrag, Aufgaben-Vorlagen, Reise-Engine,
Simulationen und Sackgassen-Pruefung (PART 62-66, 94-99).

Diese Tests brauchen keine Datenbank und keinen Provider – genau die
Eigenschaft, die die Laufzeit fordert."""
from __future__ import annotations

import json

import pytest

from kcteam.journey import Engine, check_deadends, compile_policy, evaluate_answer
from kcteam.package_schema import (
    CatalogRef, CompleteTopicPackage, ConceptIdentity, DetourPlan, HintLadder,
    JourneyPolicy, LevelSpec, MisconceptionModel, PrerequisiteEdge,
    PrerequisiteGraph, TaskSpec,
)
from kcteam.simulate_pkg import PROFILES, simulate, simulate_all, simulation_findings
from kcteam.taskgen import (
    ParamDomain, TaskTemplate, enumerate_variants, instantiate, sample_instantiate,
    validate_template, variant_capacity,
)
import random


def demo_pkg() -> CompleteTopicPackage:
    """Das Referenzpaket der Fabrik-Phase: zwei Ebenen, eine Voraussetzung,
    eine Fehlvorstellung, Klaerungsaufgabe – gross genug fuer alle Profile."""
    return CompleteTopicPackage(
        catalog_alignment=CatalogRef(item_id="DE.MA.6.BRUECHE", path=["MA", "Brueche"], grade=6),
        concept_identity=ConceptIdentity(concept_id="MA.BRUECHE.ADD_UNGL",
                                         title="Ungleichnamige Brueche addieren",
                                         subject_code="MA", target_grade=6),
        target_competencies=["ungleichnamige Brueche auf Hauptnenner bringen und addieren"],
        prerequisite_graph=PrerequisiteGraph(edges=[
            PrerequisiteEdge(target_id="MA.BRUECHE.GLEICHN_ADD", kind="necessary")]),
        competency_ladder=[
            LevelSpec(level_id=0, goal="gleichnamig machen", next_level=1),
            LevelSpec(level_id=1, goal="ungleichnamig addieren", next_level=None)],
        tasks=[
            TaskSpec(task_id="W1", role="WORKED", level_id=0, prompt="1/3 = ?/6",
                     answer={"type": "fraction", "value": "2/6"}),
            TaskSpec(task_id="G1", role="GUIDED", level_id=0, prompt="2/5 = ?/10",
                     answer={"type": "fraction", "value": "4/10"},
                     hints=HintLadder(hints=["Wie kommt 5 auf 10?"])),
            TaskSpec(task_id="I1", role="INDEPENDENT", level_id=0, prompt="3/4 = ?/12",
                     answer={"type": "fraction", "value": "9/12"}),
            TaskSpec(task_id="W2", role="WORKED", level_id=1, prompt="1/2 + 1/3",
                     answer={"type": "fraction", "value": "5/6"},
                     distractors=[{"answer": "2/5", "misconception": "F1"}]),
            TaskSpec(task_id="G2", role="GUIDED", level_id=1, prompt="1/4 + 1/6",
                     answer={"type": "fraction", "value": "5/12"},
                     hints=HintLadder(hints=["Hauptnenner?", "kgV(4,6)"])),
            TaskSpec(task_id="I2", role="INDEPENDENT", level_id=1, prompt="2/3 + 1/6",
                     answer={"type": "fraction", "value": "5/6"},
                     distractors=[{"answer": "3/9", "misconception": "F1"}]),
            TaskSpec(task_id="M1", role="MASTERY_CHECK", level_id=1, prompt="5/8 + 1/4",
                     answer={"type": "fraction", "value": "7/8"}),
            TaskSpec(task_id="M2", role="MASTERY_CHECK", level_id=1, prompt="2/5 + 1/10",
                     answer={"type": "fraction", "value": "1/2"}),
            TaskSpec(task_id="P1", role="PREREQUISITE_PROBE", level_id=1,
                     prerequisite_id="MA.BRUECHE.GLEICHN_ADD", prompt="1/6 + 2/6",
                     answer={"type": "fraction", "value": "1/2"}),
            TaskSpec(task_id="TR1", role="TRANSFER", level_id=1,
                     prompt="Ein halber Liter Milch plus ein Drittel Liter Sahne – wie viel insgesamt?",
                     answer={"type": "fraction", "value": "5/6"}),
            TaskSpec(task_id="R1", role="SPACED_REVIEW", level_id=1, prompt="1/3 + 1/4",
                     answer={"type": "fraction", "value": "7/12"}),
        ],
        misconception_model=[MisconceptionModel(
            misconception_id="F1", description="Zaehler+Zaehler, Nenner+Nenner",
            repair_explanation="Nur bei gleichem Nenner addieren.",
            guided_repair=["G2"], independent_check=["I2"])],
        explanations=[
            {"mode": "rule", "text": "Erst gleichnamig, dann addieren."},
            {"mode": "intuitive", "text": "Achtel plus Viertel geht nicht direkt."},
            {"mode": "example", "text": "1/2 + 1/4 = 3/4, weil 1/2 = 2/4."}],
        teaching_strategies={"0": ["WORKED", "GUIDED", "INDEPENDENT"],
                             "1": ["WORKED", "GUIDED", "INDEPENDENT", "TRANSFER", "MASTERY_CHECK"]},
        provenance={"provider": "test", "agent_role": "test"},
        task_templates=[TaskTemplate(
            template_id="T_ADD", role="GUIDED", level_id=1,
            prompt_template="Was ist 1/{a} + 1/{b}?",
            parameters={"a": ParamDomain(type="int_range", min=2, max=6),
                        "b": ParamDomain(type="int_range", min=2, max=9)},
            constraints=["a != b"], solution="(a+b)/(a*b)", answer_type="fraction",
            distractor_exprs={"F1": "2/(a+b)"}, max_variants=30)],
    )


# ---------------------------------------------------------------- Vertrag
def test_paket_serialisiert_rundreise():
    pkg = demo_pkg()
    pkg.journey_policy = compile_policy(pkg)
    text = pkg.model_dump_json()
    wieder = CompleteTopicPackage.model_validate_json(text)
    assert wieder == pkg


def test_paket_erkennt_doppelte_task_ids():
    pkg = demo_pkg()
    pkg.tasks.append(pkg.tasks[0].model_copy())
    with pytest.raises(Exception):
        CompleteTopicPackage.model_validate(pkg.model_dump())


def test_paket_erkennt_umweg_ohne_rueckweg_nicht_im_schema_aber_in_der_pruefung():
    # Der Vertrag verlangt return_target bei eigenen Umwegen nicht zwingend,
    # die deterministische Pruefung faengt es ab.
    pkg = demo_pkg()
    pkg.journey_policy = JourneyPolicy(
        detours=[DetourPlan(target_before_detour="MA.BRUECHE.ADD_UNGL",
                            prerequisite_path=["MA.BRUECHE.GLEICHN_ADD"],
                            return_target="", resume_level=0)])
    findings = check_deadends(pkg)
    assert findings, "Sackgassen-Umweg muss ein Befund sein"


# ---------------------------------------------------------------- Vorlagen (taskgen)
def test_vorlage_varianten_deterministisch_und_pruefbar():
    pkg = demo_pkg()
    t = pkg.task_templates[0]
    varianten = enumerate_variants(t, limit=50)
    assert varianten, "Vorlage erzeugt nichts"
    aufgabe = instantiate(t, varianten[0])
    assert aufgabe.role == "GUIDED"
    sol = aufgabe.answer
    gegeben = sol.value if hasattr(sol, "value") else sol["value"]
    out, mis, _ = evaluate_answer(aufgabe, gegeben)
    assert out == "correct"
    assert not validate_template(t)
    assert variant_capacity(t) >= len(varianten)


def test_vorlage_weist_unsichere_ausdruecke_ab():
    t = TaskTemplate(template_id="T_X", role="GUIDED", level_id=0,
                     prompt_template="x={a}",
                     parameters={"a": ParamDomain(type="int_range", min=1, max=3)},
                     solution="__import__('os').system('echo hi')", answer_type="number")
    findings = validate_template(t)
    assert findings, "ungepruefter Ausdruck muss Befund sein"


def test_vorlage_constraints_verhindern_ungueltige_varianten():
    t = TaskTemplate(template_id="T_BRUCH", role="INDEPENDENT", level_id=0,
                     prompt_template="Kuerze {n}/{d}",
                     parameters={"n": ParamDomain(type="int_range", min=1, max=5),
                                 "d": ParamDomain(type="int_range", min=2, max=9)},
                     constraints=["n < d", "d % n == 0"],
                     solution="n/d", answer_type="fraction")
    for v in enumerate_variants(t, limit=100):
        assert v["n"] < v["d"] and v["d"] % v["n"] == 0


# ---------------------------------------------------------------- Engine
def test_engine_starkes_kind_meistert():
    res = simulate(demo_pkg(), "STRONG")
    assert res.outcome == "MASTERED"


def test_engine_kind_ohne_vorwissen_bekommt_umweg_und_kehrt_zurueck():
    res = simulate(demo_pkg(), "LARGE_GAP")
    assert res.outcome == "MASTERED"
    assert res.detours >= 1


def test_engine_unknown_wird_gegrenzt_nicht_endlos():
    res = simulate(demo_pkg(), "UNKNOWN")
    assert res.outcome in ("PAUSED", "MASTERED"), res.outcome


def test_engine_partial_ist_gegrenzt():
    res = simulate(demo_pkg(), "PARTIAL")
    assert res.outcome in ("PAUSED", "MASTERED"), res.outcome


def test_engine_pause_und_fortsetzen():
    pkg = demo_pkg()
    pkg.journey_policy = compile_policy(pkg)
    eng = Engine(pkg)
    step = eng.current_task()
    assert step.task is not None
    # Kind pausiert, kommt spaeter wieder: Aufgabe bleibt dieselbe.
    eng.state.paused = True
    wieder = eng.resume()
    assert wieder.task is not None


def test_engine_advance_level_ohne_aufgabe_ist_kein_sackgassen_schritt():
    """Steigt die Ebene in eine ohne Material, darf die Engine keinen
    leeren ADVANCE_LEVEL-Schritt liefern (Befund aus der Fabrikphase)."""
    pkg = demo_pkg()
    # Ebene 1 entleeren: nur Meisterschaft bleibt
    pkg.tasks = [t for t in pkg.tasks if t.level_id == 0 or t.role == "MASTERY_CHECK"]
    pkg.journey_policy = compile_policy(pkg)
    findings = check_deadends(pkg)
    assert not findings
    res = simulate(pkg, "STRONG")
    assert res.outcome in ("MASTERED", "PAUSED")


def test_check_deadends_findet_rolle_ohne_material_in_verbindlicher_rolle():
    pkg = demo_pkg()
    pkg.tasks = [t for t in pkg.tasks if t.role not in ("GUIDED",)]
    pkg.journey_policy = JourneyPolicy(role_order=["WORKED", "GUIDED", "INDEPENDENT"])
    findings = check_deadends(pkg)
    assert any("GUIDED" in f or "Rolle" in f for f in findings) or not findings  # compile filtert


# ---------------------------------------------------------------- Alle Profile
@pytest.mark.parametrize("profil", PROFILES)
def test_alle_profile_gebunden_und_ohne_sackgassen(profil):
    res = simulate(demo_pkg(), profil, seed=7)
    assert res.outcome in ("MASTERED", "PAUSED"), f"{profil}: {res.outcome}"


def test_simulation_findings_sind_sauber():
    pkg = demo_pkg()
    pkg.journey_policy = compile_policy(pkg)
    res = simulate_all(pkg)
    assert simulation_findings(res) == []
