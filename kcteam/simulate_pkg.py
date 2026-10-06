"""Adversariale Lernsimulation ueber der kompilierten Politik (PART 64-65).

Der Simulator ist Kritiker, nicht Wahrheit: die Engine bleibt Quelle des
Laufzeitverhaltens. Die Profile suchen Sackgassen, falsche Weiterleitung,
verfruehte Meisterschaft, Uebertraining, kaputte Umweg-Rueckkehr und
zu wenige Aufgabenvarianten.

Profile: STRONG AVERAGE LARGE_GAP TYPICAL_MISCONCEPTION
         REPEATED_MISCONCEPTION CARELESS PARTIAL UNKNOWN
         CONTRADICTORY INCONSISTENT
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any

from .journey import Engine, evaluate_answer
from .package_schema import CompleteTopicPackage, TaskSpec

PROFILES = ("STRONG", "AVERAGE", "LARGE_GAP", "TYPICAL_MISCONCEPTION",
            "REPEATED_MISCONCEPTION", "CARELESS", "PARTIAL", "UNKNOWN",
            "CONTRADICTORY", "INCONSISTENT")

MAX_STEPS = 400


@dataclass
class SimResult:
    profile: str
    outcome: str              # MASTERED | PAUSED | STEP_LIMIT | DEAD_END
    steps: int
    mastered_level: int | None = None
    detours: int = 0
    misconceptions_seen: list[str] = field(default_factory=list)
    actions: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


# ---------------------------------------------------------------- Antworten des "Kinds"
def correct(task: TaskSpec) -> Any:
    a = task.answer
    if a is None:
        return "Ich weiss es nicht"
    spec = a.model_dump() if hasattr(a, "model_dump") else a
    t = spec["type"]
    if t in ("number", "fraction", "mark", "unit_value"):
        return spec["value"]
    if t == "text":
        return spec["accepted"][0]
    if t == "choice":
        idx = [i for i, o in enumerate(spec["options"]) if o.get("correct")]
        return idx if spec.get("multiple") else idx[0]
    if t == "order":
        return list(spec["items"])
    if t == "match":
        return [list(p) for p in spec["pairs"]]
    if t == "concept_rubric":
        parts = [c if isinstance(c, str) else c["concept"] for c in spec["required_concepts"]]
        return " ".join(parts)
    if t == "free_text":
        return spec.get("sample_answer") or "Antwort"
    return "x"


def misconception(task: TaskSpec) -> Any:
    """Antwort, die eine bekannte Fehlvorstellung zeigt."""
    spec = task.answer.model_dump() if task.answer and hasattr(task.answer, "model_dump") \
        else (task.answer or {})
    for d in task.distractors or []:
        d = d.model_dump() if hasattr(d, "model_dump") else d
        if d.get("misconception"):
            return d["answer"]
    if spec.get("type") == "choice":
        for i, o in enumerate(spec["options"]):
            if o.get("misconception"):
                return i
    if spec.get("type") == "concept_rubric":
        for m in spec.get("misconceptions") or []:
            if m.get("patterns"):
                return m["patterns"][0]
    return wrong(task)


def wrong(task: TaskSpec) -> Any:
    spec = task.answer.model_dump() if task.answer and hasattr(task.answer, "model_dump") \
        else (task.answer or {})
    t = spec.get("type")
    if t in ("number", "unit_value"):
        return float(spec["value"]) + 7
    if t == "fraction":
        return "1/2" if spec["value"] != "1/2" else "1/3"
    if t == "text":
        return "etwas anderes"
    if t == "choice":
        bad = [i for i, o in enumerate(spec["options"]) if not o.get("correct")]
        return bad[0] if bad else 99
    if t == "order":
        return list(reversed(spec["items"]))
    if t == "match":
        ps = spec["pairs"]
        return [[ps[i][0], ps[(i + 1) % len(ps)][1]] for i in range(len(ps))]
    if t == "concept_rubric":
        return "blabla"     # nichts Treffbares -> UNKNOWN erwartet
    return "42"


def partial(task: TaskSpec) -> Any:
    spec = task.answer.model_dump() if task.answer and hasattr(task.answer, "model_dump") \
        else (task.answer or {})
    if spec.get("type") == "concept_rubric":
        c = spec["required_concepts"][0]
        return c if isinstance(c, str) else c["concept"]
    if spec.get("type") == "choice" and spec.get("multiple"):
        corr = [i for i, o in enumerate(spec["options"]) if o.get("correct")]
        return corr[:1]
    return wrong(task)


def unknown(task: TaskSpec) -> Any:
    if task.answer and (task.answer.model_dump() if hasattr(task.answer, "model_dump")
                        else task.answer).get("type") == "concept_rubric":
        return "keine Ahnung, irgendwas"
    return None     # leer = skipped -> UNKNOWN


# ---------------------------------------------------------------- Simulation
def simulate(pkg: CompleteTopicPackage, profile: str, *, seed: int = 0,
             max_steps: int = MAX_STEPS) -> SimResult:
    """Eine Lernreise durchspielen. Antworten kommen deterministisch aus den
    Aufgaben – das Kind ist skriptbar, die Engine nicht."""
    eng = Engine(pkg, rng=random.Random(seed))
    rng = random.Random(seed * 7919 + 17)
    res = SimResult(profile=profile, outcome="DEAD_END", steps=0)
    state = {"detour": 0, "mis_fixed": False, "n": 0}
    step = eng.current_task()

    def answer_for(task: TaskSpec) -> Any:
        """Profil -> konkrete Kind-Antwort fuer diese Aufgabe."""
        if profile == "STRONG":
            return correct(task)
        if profile == "AVERAGE":
            state["n"] += 1
            # jede vierte Aufgabe erst falsch, dann richtig (Inkonsistenz light)
            if state["n"] % 4 == 0 and (eng.state.attempts == 0):
                return wrong(task)
            return correct(task)
        if profile == "LARGE_GAP":
            # kann erst, wenn der Umweg gelaufen ist; sonst immer falsch
            return correct(task) if eng.state.detour_stack or state["detour"] > 0 else wrong(task)
        if profile in ("TYPICAL_MISCONCEPTION", "REPEATED_MISCONCEPTION"):
            if not state["mis_fixed"] and (task.misconception or has_mis_option(task)):
                out, _, _ = evaluate_answer(task, misconception(task))
                if out == "misconception":
                    return misconception(task)
            if profile == "REPEATED_MISCONCEPTION" and not state["mis_fixed"]:
                out, _, _ = evaluate_answer(task, misconception(task))
                if out == "misconception":
                    return misconception(task)
            return correct(task)
        if profile == "CARELESS":
            state["n"] += 1
            return wrong(task) if state["n"] % 3 == 1 else correct(task)
        if profile == "PARTIAL":
            return partial(task)
        if profile == "UNKNOWN":
            return unknown(task)
        if profile == "CONTRADICTORY":
            state["n"] += 1
            return misconception(task) if state["n"] % 2 else correct(task)
        if profile == "INCONSISTENT":
            return rng.choice([correct, wrong, partial, unknown])(task)
        return correct(task)

    while res.steps < max_steps:
        res.steps += 1
        res.actions.append(step.action)
        if step.action == "COMPLETE":
            res.outcome = "MASTERED"
            res.mastered_level = eng.state.level_id
            return res
        if step.action == "CHILD_CHOICE":
            # Das Kind waehlt weiter – die Reise muss fortsetzbar bleiben.
            # terminal=True (Thema meldet: genug fruchtlose Zyklen) -> PAUSED.
            if step.terminal or eng.state.cycles_at_child_choice > 6:
                res.outcome = "PAUSED"
                return res
            step = eng.resume()
            continue
        if step.action == "PREREQUISITE_DETOUR":
            res.detours += 1
            state["detour"] += 1
            # Umweg simulieren: die Voraussetzung wird gelernt und gemeistert
            step = eng.step("PREREQUISITE_MASTERED")
            continue
        if step.action in ("REPAIR_EXPLANATION", "ALTERNATE_EXPLANATION"):
            step = eng.step("UNKNOWN") if step.action == "ALTERNATE_EXPLANATION" \
                else eng.step("MISCONCEPTION", misconception=eng.state.misconception)
            continue
        if step.task is None:
            res.problems.append(f"Aktion {step.action} ohne Aufgabe")
            res.outcome = "DEAD_END"
            return res
        task = step.task
        given = answer_for(task)
        oc, mis, _ = evaluate_answer(task, given)
        if oc == "misconception" and mis:
            res.misconceptions_seen.append(mis)
        if step.action in ("REPAIR_CHECK",) and oc == "correct":
            state["mis_fixed"] = True
        step = eng.step({"correct": "CORRECT", "partial": "PARTIAL",
                         "incorrect": "INCORRECT", "misconception": "MISCONCEPTION",
                         "unknown": "UNKNOWN", "skipped": "UNKNOWN",
                         "needs_review": "UNKNOWN"}.get(oc, "INCORRECT"),
                        misconception=mis, task=task)
    res.outcome = "STEP_LIMIT"
    return res


def has_mis_option(task: TaskSpec) -> bool:
    spec = task.answer.model_dump() if task.answer and hasattr(task.answer, "model_dump") \
        else (task.answer or {})
    return any(o.get("misconception") for o in spec.get("options", [])) \
        or bool(spec.get("misconceptions")) \
        or any((d.get("misconception") if isinstance(d, dict) else d.misconception)
               for d in (task.distractors or []))


def simulate_all(pkg: CompleteTopicPackage, *, seed: int = 0) -> dict[str, SimResult]:
    """Alle Profile. Kritisch: jede Reise muss enden – MASTERED oder bewusst
    PAUSED, nie DEAD_END und nie STEP_LIMIT."""
    return {p: simulate(pkg, p, seed=seed) for p in PROFILES}


def simulation_findings(results: dict[str, SimResult]) -> list[str]:
    """Aus den Laeufen die Befunde, die den Controller interessieren."""
    problems: list[str] = []
    for p, r in results.items():
        if r.outcome == "DEAD_END":
            problems.append(f"{p}: Sackgasse ({'; '.join(r.problems) or 'ohne Detail'})")
        elif r.outcome == "STEP_LIMIT":
            problems.append(f"{p}: Schrittlimit erreicht – moegliche Endlosschleife")
        if p == "STRONG" and r.outcome == "MASTERED" and r.steps > 120:
            problems.append(f"STRONG: {r.steps} Schritte bis Meisterschaft – Uebertraining")
        if p == "LARGE_GAP" and r.detours == 0:
            problems.append("LARGE_GAP: kein einziger Voraussetzungs-Umweg ausgeloest")
        if p == "TYPICAL_MISCONCEPTION" and not r.misconceptions_seen \
                and r.outcome != "MASTERED":
            problems.append("TYPICAL_MISCONCEPTION: keine Fehlvorstellung erkannt")
    return problems
