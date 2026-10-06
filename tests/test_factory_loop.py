"""Geschlossene Schleife: Rubrik-Batterie, Kontrolleur-Gate, lokale Reparatur,
Sitzung-vs-Thema-Eskalation, Paket-Erschoepfung (PART 19-23, 35-37)."""
from __future__ import annotations

import pytest

from kcteam.journey import Engine, Step, compile_policy
from kcteam.package_schema import CompleteTopicPackage
from kcteam.rubrics import adversarial_cases, evaluate_rubric, run_battery
from kcteam.simulate_pkg import simulate
from test_package_journey import demo_pkg


RUBRIC = {
    "type": "concept_rubric",
    "required_concepts": [
        {"concept": "Photosynthese", "accepted": ["Lichtenergie in Zucker"]},
        {"concept": "Chlorophyll", "accepted": ["Blattgruen"]}],
    "misconceptions": [{"patterns": ["Pflanzen essen Erde"], "misconception": "B1"}],
    "contradictions": [{"patterns": ["braucht kein Licht"], "misconception": None}],
    "unknown_markers": ["keine ahnung"],
    "normalization": ["lower", "umlauts", "punctuation", "articles", "typo"],
}


# ---------------------------------------------------------------- Rubrik-Vertrag
def test_rubrik_batterie_sauber():
    assert run_battery(RUBRIC, task_id="T") == []


@pytest.mark.parametrize("probe,expected", [
    ("Photosynthese und Chlorophyll", "correct"),
    ("Lichtenergie in Zucker, Blattgruen", "correct"),          # Paraphrase
    ("Photosynthese", "partial"),
    ("Pflanzen essen Erde und Photosynthese", "misconception"), # FV schlaegt Treffer
    ("Photosynthese braucht kein Licht", "incorrect"),          # Widerspruch
    ("nicht Photosynthese, nicht Chlorophyll", "unknown"),      # Negation != Treffer
    ("Photosynthese Chlorophyll und fussball", "correct"),      # irrelevante Zugabe
    ("Photosynthese Chlorophyl", "correct"),                    # Vertipper
    ("keine ahnung", "unknown"),
    ("asdkjf", "unknown"),
])
def test_rubrik_einordnung(probe, expected):
    out, mis, _ = evaluate_rubric(RUBRIC, probe)
    assert out == expected, (probe, out)


def test_rubrik_batterie_findet_defekte():
    """Ein Muster, das das Pflichtkonzept selbst beschattet, ist ein echter
    Vertragsfehler: die kanonische Antwort wird zur Fehlvorstellung."""
    schlecht = {"type": "concept_rubric",
                "required_concepts": ["Photosynthese"],
                "misconceptions": [{"patterns": ["Photosynthese"],
                                    "misconception": "F9"}]}
    findings = run_battery(schlecht, task_id="S1")
    assert any("kanonisch" in f or "fehlvorstellung" in f for f in findings), findings


# ---------------------------------------------------------------- Sitzung vs Thema
def test_partial_erreicht_terminale_pause_schnell():
    res = simulate(demo_pkg(), "PARTIAL")
    assert res.outcome == "PAUSED"
    assert res.steps < 80, f"PARTIAL brauchte {res.steps} Schritte"


def test_unknown_erreicht_terminale_pause_schnell():
    res = simulate(demo_pkg(), "UNKNOWN")
    assert res.outcome == "PAUSED"
    assert res.steps < 60


def test_thema_bleibt_nach_pause_fortsetzbar():
    """Sitzung kann pausieren, Thema bleibt offen (PART 22): nach terminaler
    CHILD_CHOICE liefert resume() weiterhin einen echten Schritt."""
    pkg = demo_pkg()
    pkg.journey_policy = compile_policy(pkg)
    eng = Engine(pkg)
    step = eng.current_task()
    for _ in range(200):
        if step.action == "COMPLETE":
            break
        if step.action == "CHILD_CHOICE":
            if step.terminal:
                break
            step = eng.resume()
            continue
        if step.task is None:
            break
        step = eng.step("PARTIAL", task=step.task)
    assert step.action == "CHILD_CHOICE" and step.terminal
    nxt = eng.resume()
    assert nxt.task is not None or nxt.action in ("CHILD_CHOICE", "COMPLETE")
    assert not eng.state.mastered    # Pause ist weder Erfolg noch Abbruch


def test_verklemmte_aufgaben_werden_nicht_wiederholt():
    """Nach resume() kommen stuck_tasks nicht blind wieder (PART 23)."""
    pkg = demo_pkg()
    pkg.journey_policy = compile_policy(pkg)
    eng = Engine(pkg)
    step = eng.current_task()
    tid = step.task.task_id
    for _ in range(30):
        if step.action == "CHILD_CHOICE":
            break
        step = eng.step("INCORRECT", task=step.task) if step.task else step
    eng.state.stuck_tasks.add(tid)
    eng.resume()
    assert tid in eng.state.used_tasks


# ---------------------------------------------------------------- Erschoepfung (PART 35)
def test_paket_erschoepfung_varianten_und_keine_endlosschleife():
    """Lange Nutzung: Vorlagen liefern frische Varianten; identische Aufgabe
    kommt nicht sofort wieder; die Reise endet gebunden."""
    pkg = demo_pkg()
    pkg.journey_policy = compile_policy(pkg)
    eng = Engine(pkg)
    served: list[str] = []
    step = eng.current_task()
    for _ in range(120):
        if step.action in ("COMPLETE", "CHILD_CHOICE"):
            if step.action == "CHILD_CHOICE" and not step.terminal:
                step = eng.resume()
                continue
            break
        if step.action == "PREREQUISITE_DETOUR":
            step = eng.step("PREREQUISITE_MASTERED")
            continue
        if step.task is None:
            break
        # nur neue Aufgaben zaehlen – Hints/Reparaturen stellen die gleiche
        # Aufgabe bewusst noch einmal (mit Hilfe) vor.
        if step.action in ("NEXT_TASK", "GUIDED_STEP", "WORKED_EXAMPLE",
                           "SIMPLER_PARALLEL", "MASTERY_EVALUATE",
                           "RETURN_TO_TARGET", "SPACED_REVIEW"):
            served.append(step.task.task_id)
        step = eng.step("INCORRECT", task=step.task)
    # dieselbe konkrete Aufgabe kommt begrenzt oft (Umweg-Rueckkehr darf sie
    # erneut bringen, aber nicht endlos), und die Reise endet gebunden
    assert all(served.count(t) <= 4 for t in set(served)), served
    assert step.action in ("COMPLETE", "CHILD_CHOICE")
