"""Kompilierte Lernreise: deterministische Politik + Engine + Sackgassen-Pruefung.

Drei Teile (PART 60-63):

1. `compile_policy` – baut aus einem COMPLETE_TOPIC_PACKAGE die JourneyPolicy:
   explizite Regeln des Lernreise-Architekten plus generische Defaults je
   Ergebnis (PART 24). Nicht jede Kind-Kombination wird enumeriert (PART 62):
   eine generische Zustandsmaschine komponiert themenspezifische Regeln.

2. `Engine` – fuehrt die Reise. Zustand + Ergebnis -> naechste Aktion.
   Kein Modellaufruf. Die Aktionen greifen nur auf vorhandenes Paketmaterial
   zu; erschoepfte Ketten enden bei CHILD_CHOICE (Pause/Wechsel ist die
   Entscheidung des Kindes, PART 25) – nie in einer Sackgasse.

3. `check_deadends` – statische Analyse: fehlende Aktionen, Kreise,
   unerreichbare Meisterschaft, verlorene Rueckwege, endlose Hilfeschleifen,
   unerreichbare Aufgabenrollen.
"""
from __future__ import annotations

import random
import re
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Any

from .package_schema import (CompleteTopicPackage, JourneyPolicy, TransitionRule, DetourPlan,
                             TaskSpec)

# ---------------------------------------------------------------- Ergebnisse
#: Engine-Ergebnisse = Antwortauswertung + abgeleitete Zustaende.
OUTCOMES = ("CORRECT", "PARTIAL", "INCORRECT", "MISCONCEPTION", "UNKNOWN",
            "REPEATED_WRONG", "REPEATED_UNKNOWN", "MASTERED",
            "PREREQUISITE_FAILED", "PREREQUISITE_MASTERED")

#: Generische Default-Ketten je Ergebnis (PART 24). Geordnet.
DEFAULT_ACTIONS: dict[str, list[str]] = {
    "CORRECT":               ["NEXT_TASK", "ADVANCE_ROLE", "ADVANCE_LEVEL", "MASTERY_EVALUATE", "COMPLETE"],
    "PARTIAL":               ["GUIDED_STEP", "SHOW_HINT", "SIMPLER_PARALLEL", "NEXT_TASK"],
    "INCORRECT":             ["SHOW_HINT", "GUIDED_STEP", "WORKED_EXAMPLE", "SIMPLER_PARALLEL",
                              "PREREQUISITE_PROBE", "PREREQUISITE_DETOUR", "CHILD_CHOICE"],
    "MISCONCEPTION":         ["REPAIR_EXPLANATION", "GUIDED_REPAIR", "REPAIR_CHECK", "NEXT_TASK"],
    "UNKNOWN":               ["CLARIFICATION", "GUIDED_STEP", "SHOW_HINT", "SIMPLER_PARALLEL", "CHILD_CHOICE"],
    "REPEATED_WRONG":        ["WORKED_EXAMPLE", "SIMPLER_PARALLEL", "PREREQUISITE_PROBE",
                              "PREREQUISITE_DETOUR", "CHILD_CHOICE"],
    "REPEATED_UNKNOWN":      ["GUIDED_STEP", "WORKED_EXAMPLE", "PREREQUISITE_PROBE",
                              "PREREQUISITE_DETOUR", "CHILD_CHOICE"],
    "MASTERED":              ["ADVANCE_LEVEL", "COMPLETE"],
    "PREREQUISITE_FAILED":   ["PREREQUISITE_DETOUR", "SIMPLER_PARALLEL", "CHILD_CHOICE"],
    "PREREQUISITE_MASTERED": ["RETURN_TO_TARGET", "NEXT_TASK"],
}


# ---------------------------------------------------------------- Antwort-Auswertung (Python-Spiegel von karo.check_answer)
def _norm(s: Any) -> str:
    return re.sub(r"\s+", " ", str(s).strip().lower())


def _num(v: Any) -> float | None:
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace(",", ".")
    m = re.match(r"^(-?\d+)\s+(\d+)\s*/\s*(\d+)$", s)
    try:
        if m:
            return float(m.group(1)) + (1 if not m.group(1).startswith("-") else -1) * 0 + \
                float(Fraction(f"{m.group(2)}/{m.group(3)}"))
        if "/" in s:
            return float(Fraction(s))
        return float(s)
    except (ValueError, ZeroDivisionError):
        return None


def evaluate_answer(task: TaskSpec | dict, given: Any) -> tuple[str, str | None, float]:
    """(outcome, misconception_id, score) – gespiegelt zur DB-Funktion.

    UNKNOWN statt falsch, wenn nichts Einordnenbares kommt; Fehlvorstellung
    schlaegt jede noch so vollstaendige Antwort (PART 46/48)."""
    a = task.answer if isinstance(task, TaskSpec) else (task or {}).get("answer")
    if given is None or (isinstance(given, str) and not given.strip()):
        return "skipped", None, 0.0
    if isinstance(given, dict) and "value" in given:
        given = given["value"]
    if isinstance(given, dict) and "text" in given:
        given = given["text"]
    if a is None:
        return "needs_review", None, 0.0
    spec = a.model_dump() if hasattr(a, "model_dump") else a
    t = spec["type"]

    if t in ("number", "unit_value"):
        gnum = _num(given)
        return ("correct", None, 1.0) if gnum is not None and \
            abs(gnum - float(spec["value"])) <= float(spec.get("tolerance") or 0) + 1e-9 \
            else _missed(spec, given, task)
    if t == "fraction":
        gnum = _num(given)
        ok = gnum is not None and abs(gnum - float(Fraction(str(spec["value"])))) < 1e-9
        if ok and not spec.get("accept_decimal", False) and re.search(r"\d[.,]\d", str(given)):
            ok = False
        return ("correct", None, 1.0) if ok else _missed(spec, given, task)
    if t == "mark":
        gnum = _num(given)
        cval = _num(spec["value"])
        ok = abs(gnum - cval) <= float(spec.get("tolerance") or 0) + 1e-9 if cval is not None \
            else _norm(given) == _norm(spec["value"])
        return ("correct", None, 1.0) if ok else _missed(spec, given, task)
    if t == "text":
        ok = _norm(given) in {_norm(x) for x in spec["accepted"]}
        return ("correct", None, 1.0) if ok else _missed(spec, given, task)
    if t == "choice":
        opts = spec["options"]
        if isinstance(given, int):
            sel = [given]
        elif isinstance(given, list) and all(isinstance(x, int) for x in given):
            sel = given
        else:
            gv = given if isinstance(given, list) else [given]
            sel = [i for i, o in enumerate(opts) if _norm(o["text"]) in {_norm(v) for v in gv}]
        corr = [i for i, o in enumerate(opts) if o.get("correct")]
        if sel and sorted(sel) == sorted(corr):
            return "correct", None, 1.0
        for i in sel:
            if i < len(opts) and opts[i].get("misconception"):
                return "misconception", opts[i]["misconception"], 0.0
        if sel and set(sel) <= set(corr):
            return "partial", None, len(sel) / max(1, len(corr))
        return _missed(spec, given, task)
    if t == "order":
        ok = isinstance(given, list) and len(given) == len(spec["items"]) and \
            all(_norm(a) == _norm(b) for a, b in zip(given, spec["items"]))
        return ("correct", None, 1.0) if ok else _missed(spec, given, task)
    if t == "match":
        ok = isinstance(given, list) and len(given) == len(spec["pairs"]) and all(
            any(_norm(q[0]) == _norm(p[0]) and _norm(q[1]) == _norm(p[1])
                for q in given if isinstance(q, (list, tuple)) and len(q) == 2)
            for p in spec["pairs"])
        return ("correct", None, 1.0) if ok else _missed(spec, given, task)
    if t == "concept_rubric":
        from .rubrics import evaluate_rubric
        return evaluate_rubric(spec, given)
    if t == "free_text":
        return "needs_review", None, 0.0
    return "unknown", None, 0.0


def _missed(spec: dict, given: Any, task) -> tuple[str, str | None, float]:
    """Falsch – aber ist es eine bekannte Fehlvorstellung? (distractors)"""
    gnorm = _norm(given)
    gnum = _num(given)
    ds = spec.get("distractors") or getattr(task, "distractors", None) or []
    for d in ds:
        da = d["answer"] if isinstance(d, dict) else d.answer
        dm = d.get("misconception") if isinstance(d, dict) else d.misconception
        if not dm:
            continue
        if gnum is not None and _num(da) is not None and abs(gnum - _num(da)) < 1e-9 \
                or _norm(da) == gnorm:
            return "misconception", dm, 0.0
    return "incorrect", None, 0.0


# ---------------------------------------------------------------- Compiler
def compile_policy(pkg: CompleteTopicPackage) -> JourneyPolicy:
    """Paket -> Politik. Bestehende Regeln bleiben; fehlende (level, role,
    outcome)-Kombis bekommen die generische Default-Kette (PART 62)."""
    rules: dict[tuple, TransitionRule] = {}
    for r in pkg.journey_policy.rules:
        rules[(r.level_id, r.task_role, r.outcome, r.misconception)] = r
    for level in pkg.competency_ladder:
        for role in {t.role for t in pkg.tasks} | set(pkg.journey_policy.role_order):
            for oc in OUTCOMES:
                if (level.level_id, role, oc, None) not in rules:
                    rules[(level.level_id, role, oc, None)] = TransitionRule(
                        level_id=level.level_id, task_role=role, outcome=oc,
                        actions=list(DEFAULT_ACTIONS[oc]))
    # Rollen ohne jegliches Material fallen aus der Reihenfolge – sie wuerden
    # sonst als tote Stellen in jeder Kette stehen.
    material = {t.role for t in pkg.tasks} | {t.role for t in pkg.task_templates}
    order = [r for r in pkg.journey_policy.role_order if r in material] \
        or list(pkg.journey_policy.role_order)
    policy = JourneyPolicy(rules=list(rules.values()),
                           detours=list(pkg.journey_policy.detours),
                           role_order=order,
                           max_attempts_per_task=pkg.journey_policy.max_attempts_per_task,
                           max_unknown_per_task=pkg.journey_policy.max_unknown_per_task,
                           max_resume_cycles=pkg.journey_policy.max_resume_cycles,
                           max_no_progress=pkg.journey_policy.max_no_progress)
    policy.detours = policy.detours or _default_detours(pkg)
    return policy


def _default_detours(pkg: CompleteTopicPackage) -> list[DetourPlan]:
    """Umwegplaene aus dem Voraussetzungsgraphen (PART 26): jeder Umweg kennt
    Ziel, Pfad, Rueckkehrbedingung und Wiedereinstieg."""
    cid = pkg.concept_identity.concept_id
    out = []
    for e in pkg.prerequisite_graph.edges:
        if e.kind in ("necessary", "useful"):
            out.append(DetourPlan(target_before_detour=cid,
                                  prerequisite_path=[e.target_id],
                                  return_condition=e.return_condition,
                                  return_target=cid,
                                  resume_level=e.resume_level))
    return out


# ---------------------------------------------------------------- Engine
@dataclass
class Step:
    """Was die Laufzeit als Naechstes tut."""
    action: str                          # ActionType
    task: TaskSpec | None = None
    hint: str | None = None
    explanation: str | None = None
    level_id: int | None = None
    detour: DetourPlan | None = None
    note: str = ""
    terminal: bool = False               # CHILD_CHOICE: verbindliche Pause-Empfehlung


@dataclass
class EngineState:
    """Laufzustand. Zweigeteilt (PART 20-22):
    - Sitzung: attempts/unknowns/hint_idx/no_progress werden bei resume()
      zurueckgesetzt – eine Pause beendet die Sitzung, nicht das Thema.
    - Thema: topic_no_progress/stuck_tasks/sessions ueberleben resume() –
      das Thema merkt sich, was schon erfolglos versucht wurde."""
    level_id: int = 0
    role: str = "WORKED"
    task_idx: int = 0
    attempts: int = 0
    unknowns: int = 0
    hint_idx: int = 0
    used_tasks: set[str] = field(default_factory=set)
    stuck_tasks: set[str] = field(default_factory=set)   # Hilfekette erfolglos durchlaufen
    last_task_id: str | None = None
    misconception: str | None = None     # aktiv in Reparatur
    repair_phase: int = 0
    detour_stack: list[DetourPlan] = field(default_factory=list)
    detour_counts: dict[str, int] = field(default_factory=dict)
    no_progress: int = 0                 # Sitzungszaehler
    topic_no_progress: int = 0           # Themenzaehler (ueberlebt Sitzungen)
    sessions: int = 0
    mastered: bool = False
    paused: bool = False
    cycles_at_child_choice: int = 0


class Engine:
    """Deterministische Lernmaschine ueber einem Paket. Kein LLM."""

    def __init__(self, pkg: CompleteTopicPackage, policy: JourneyPolicy | None = None,
                 *, rng: random.Random | None = None):
        self.pkg = pkg
        self.policy = policy or compile_policy(pkg)
        self.rng = rng or random.Random(0)
        self.state = EngineState(level_id=pkg.bottom_level())
        self.state.role = self._first_role(self.state.level_id)
        self._template_use: dict[str, int] = {}

    # ---------------- Inhalt ----------------
    def _first_role(self, level_id: int) -> str:
        for r in self.policy.role_order:
            if self._tasks(r, level_id):
                return r
        # kein Standardmaterial: nimm, was da ist (Diagnose, Sondierung …)
        for role in {t.role for t in self.pkg.tasks if t.level_id == level_id}:
            return role
        return "INDEPENDENT"

    def _tasks(self, role: str, level_id: int, allow_used: bool = False) -> list[TaskSpec]:
        tasks = self.pkg.tasks_for(role, level_id)
        if allow_used:
            return tasks
        return [t for t in tasks if t.task_id not in self.state.used_tasks]

    def _next_task(self, role: str, level_id: int, allow_used: bool = False) -> TaskSpec | None:
        fresh = self._tasks(role, level_id, allow_used=allow_used)
        if fresh:
            return fresh[0]
        # Vorlagen decken die Wiederholung (PART 99: kein Ausgehen der Aufgaben)
        for tmpl in self.pkg.task_templates:
            if tmpl.role == role and tmpl.level_id == level_id \
                    and self._template_use.get(tmpl.template_id, 0) < tmpl.max_variants:
                from .taskgen import sample_instantiate
                task = sample_instantiate(tmpl, self.rng)
                if task:
                    self._template_use[tmpl.template_id] = \
                        self._template_use.get(tmpl.template_id, 0) + 1
                    return task
        return None

    def _role_order(self, level_id: int) -> list[str]:
        order = [r for r in self.policy.role_order if self.pkg.tasks_for(r, level_id)
                 or any(t.level_id == level_id and t.role == r for t in self.pkg.task_templates)]
        for role in {t.role for t in self.pkg.tasks if t.level_id == level_id}:
            if role not in order and role not in ("DIAGNOSTIC", "MISCONCEPTION_PROBE",
                                                  "PREREQUISITE_PROBE", "CLARIFICATION"):
                order.append(role)
        return order

    # ---------------- Regelwerk ----------------
    def _actions_for(self, outcome: str, misconception: str | None = None) -> list[str]:
        s = self.state
        best = None
        for r in self.policy.rules:
            if r.level_id != s.level_id or r.task_role != s.role or r.outcome != outcome:
                continue
            if outcome == "MISCONCEPTION" and r.misconception and r.misconception != misconception:
                continue
            if r.misconception and r.misconception == misconception:
                return list(r.actions)
            best = best or r
        if best:
            return list(best.actions)
        return list(DEFAULT_ACTIONS.get(outcome, ["CHILD_CHOICE"]))

    # ---------------- Hauptschleife ----------------
    def current_task(self) -> Step:
        """Die Aufgabe, die das Kind jetzt sieht (Einstieg/Repositionierung)."""
        s = self.state
        task = self._next_task(s.role, s.level_id)
        if task:
            s.used_tasks.add(task.task_id)
            return Step(action="NEXT_TASK", task=task, level_id=s.level_id)
        # Material erschoepft (inkl. Vorlagen): zaehlt als Pause-Angebot,
        # nicht als stilles Weiterreichen leerer Schritte.
        return self._child_choice("kein frisches Material – Pause anbieten")

    def submit(self, given: Any, task: TaskSpec | None = None) -> Step:
        """Antwort auswerten und die Reise deterministisch fortsetzen."""
        task = task or getattr(self, "_last_task", None)
        outcome, mis, _score = evaluate_answer(task, given) if task else ("UNKNOWN", None, 0.0)
        return self.step(outcome, misconception=mis, task=task)

    def step(self, outcome: str, *, misconception: str | None = None,
             task: TaskSpec | None = None) -> Step:
        s = self.state
        if s.mastered:
            return Step(action="COMPLETE")
        # Wiederholungs-Zaehler -> REPEATED_*.
        # unknowns laufen ueber Aufgabenwechsel hinweg mit: ein Kind, das zu
        # allem nichts Einordnbares sagt, braucht die tiefere Kaskade – auch
        # wenn die Engine zwischenzeitlich anderes Material gezeigt hat.
        if outcome == "INCORRECT":
            s.attempts += 1
            s.unknowns = 0
            if s.attempts >= self.policy.max_attempts_per_task:
                outcome = "REPEATED_WRONG"
        elif outcome in ("skipped", "unknown"):
            outcome = "UNKNOWN"
            s.unknowns += 1
            if s.unknowns >= self.policy.max_unknown_per_task:
                outcome = "REPEATED_UNKNOWN"
        elif outcome == "CORRECT":
            s.unknowns = 0
            s.no_progress = 0
            s.topic_no_progress = 0
            # cycles_at_child_choice absichtlich NICHT zuruecksetzen: ein Kind,
            # das abwechselnd richtig und unbrauchbar antwortet, soll die
            # Pause-Empfehlung nicht auf ewig hinausschieben.
        elif outcome == "MISCONCEPTION" and s.misconception == misconception:
            outcome = "REPEATED_WRONG"
        # PART 25: aufgeben tut nur das Kind. Aber ein System, das endlos
        # Hilfeketten abarbeitet, ist keine Hilfe – nach genug vergeblichen
        # Versuchen ist die Pause-Frage die ehrliche Antwort (fortsetzbar).
        # Sitzung (no_progress) eskaliert zur Pause; das Thema
        # (topic_no_progress) merkt sich die fruchtlosen Sitzungen.
        if outcome not in ("CORRECT", "MASTERED", "PREREQUISITE_MASTERED"):
            s.no_progress += 1
            s.topic_no_progress += 1
            if s.no_progress >= self.policy.max_no_progress:
                if task is not None:
                    s.stuck_tasks.add(task.task_id)
                return self._child_choice("viele Versuche ohne Fortschritt – Pause anbieten")
        for action in self._actions_for(outcome, misconception):
            st = self._try(action, task, misconception)
            if st is not None:
                return st
        # Hilfekette fuer diese Aufgabe komplett erfolglos durchlaufen:
        # sie gilt fuer das Thema als "verklemmt" und wird nach resume()
        # nicht blind wieder vorgesetzt (PART 23: kein Rundendrehen).
        if task is not None:
            s.stuck_tasks.add(task.task_id)
        return self._child_choice("Aktionskette erschoepft")

    # ---------------- Aktionen ----------------
    def _try(self, action: str, task: TaskSpec | None, mis: str | None) -> Step | None:  # noqa: C901
        s = self.state
        if action == "NEXT_TASK":
            nxt = self._next_task(s.role, s.level_id)
            if nxt:
                self._reset_attempts()
                s.used_tasks.add(nxt.task_id)
                return Step(action="NEXT_TASK", task=nxt, level_id=s.level_id)
            return self._try("ADVANCE_ROLE", task, mis)
        if action == "ADVANCE_ROLE":
            order = self._role_order(s.level_id)
            if s.role in order:
                for r in order[order.index(s.role) + 1:]:
                    nxt = self._next_task(r, s.level_id)
                    if nxt:
                        s.role, s.task_idx = r, 0
                        self._reset_attempts()
                        s.used_tasks.add(nxt.task_id)
                        return Step(action="ADVANCE_ROLE", task=nxt, level_id=s.level_id)
            return self._try("ADVANCE_LEVEL", task, mis)
        if action == "ADVANCE_LEVEL":
            level = self.pkg.level(s.level_id)
            if level and level.next_level is not None:
                s.level_id = level.next_level
                s.role = self._first_role(s.level_id)
                self._reset_attempts()
                nxt = self._next_task(s.role, s.level_id)
                if nxt:
                    s.used_tasks.add(nxt.task_id)
                    return Step(action="ADVANCE_LEVEL", task=nxt, level_id=s.level_id)
                # neue Ebene ohne Material: Kette weiterlaufen lassen
                return self._try("MASTERY_EVALUATE", task, mis)
            return self._try("MASTERY_EVALUATE", task, mis)
        if action == "MASTERY_EVALUATE":
            checks = self._next_task("MASTERY_CHECK", s.level_id)
            if checks:
                s.role = "MASTERY_CHECK"
                self._reset_attempts()
                s.used_tasks.add(checks.task_id)
                return Step(action="MASTERY_EVALUATE", task=checks, level_id=s.level_id)
            if s.level_id >= self.pkg.top_level():
                s.mastered = True
                return Step(action="COMPLETE")
            return self._try("ADVANCE_LEVEL", task, mis)
        if action == "COMPLETE":
            s.mastered = True
            return Step(action="COMPLETE")
        if action == "SHOW_HINT":
            if task and task.hints and s.hint_idx < len(task.hints.hints):
                hint = task.hints.hints[s.hint_idx]
                s.hint_idx += 1
                return Step(action="SHOW_HINT", task=task, hint=hint)
            return None
        if action == "GUIDED_STEP":
            nxt = self._next_task("GUIDED", s.level_id) or self._next_task("SCAFFOLDED", s.level_id)
            if nxt:
                s.role = "GUIDED" if nxt.role == "GUIDED" else "SCAFFOLDED"
                self._reset_attempts()
                s.used_tasks.add(nxt.task_id)
                return Step(action="GUIDED_STEP", task=nxt)
            return None
        if action == "WORKED_EXAMPLE":
            nxt = self._next_task("WORKED", s.level_id)
            if nxt:
                s.role = "WORKED"
                self._reset_attempts()
                s.used_tasks.add(nxt.task_id)
                return Step(action="WORKED_EXAMPLE", task=nxt)
            return None
        if action == "SIMPLER_PARALLEL":
            nxt = self._next_task(s.role, s.level_id)
            if nxt:
                self._reset_attempts()
                s.used_tasks.add(nxt.task_id)
                return Step(action="SIMPLER_PARALLEL", task=nxt)
            # Ebene tiefer?
            level = self.pkg.level(s.level_id)
            if level and level.fallback_level is not None:
                s.level_id = level.fallback_level
                s.role = self._first_role(s.level_id)
                self._reset_attempts()
                nxt2 = self._next_task(s.role, s.level_id)
                if nxt2:
                    s.used_tasks.add(nxt2.task_id)
                    return Step(action="SIMPLER_PARALLEL", task=nxt2, level_id=s.level_id)
            return None
        if action == "ALTERNATE_EXPLANATION":
            for e in self.pkg.explanations:
                if e.level_id in (None, s.level_id) and not e.for_misconception:
                    return Step(action="ALTERNATE_EXPLANATION", explanation=e.text)
            return None
        if action == "REPAIR_EXPLANATION":
            m = self._mis(mis)
            if m and (m.repair_explanation or m.guided_repair):
                s.misconception = mis
                s.repair_phase = 1
                text = m.repair_explanation
                if text:
                    return Step(action="REPAIR_EXPLANATION", explanation=text)
            return self._try("GUIDED_REPAIR", task, mis) if s.misconception else None
        if action == "GUIDED_REPAIR":
            m = self._mis(mis or s.misconception)
            if m:
                s.misconception = m.misconception_id
                for tid in m.guided_repair:
                    if tid not in s.used_tasks:
                        t = self.pkg_task(tid)
                        if t:
                            s.used_tasks.add(tid)
                            s.repair_phase = 2
                            return Step(action="GUIDED_REPAIR", task=t)
            return self._try("REPAIR_CHECK", task, s.misconception) if s.misconception else None
        if action == "REPAIR_CHECK":
            m = self._mis(mis or s.misconception)
            if m:
                for tid in m.independent_check:
                    if tid not in s.used_tasks:
                        t = self.pkg_task(tid)
                        if t:
                            s.used_tasks.add(tid)
                            s.misconception = None
                            s.repair_phase = 0
                            self._reset_attempts()
                            return Step(action="REPAIR_CHECK", task=t)
                # keine eigene Checkaufgabe: gleiche Rolle weiter
            s.misconception = None
            s.repair_phase = 0
            return self._try("NEXT_TASK", task, mis)
        if action == "CLARIFICATION":
            if task:
                for t in self.pkg.tasks_for("CLARIFICATION"):
                    if t.clarification_for == task.task_id:
                        return Step(action="CLARIFICATION", task=t)
                # eingebettete Klaerung im Rubrik-Vertrag (PART 46)
                spec = task.answer.model_dump() if task.answer and hasattr(
                    task.answer, "model_dump") else (task.answer or {})
                clar = spec.get("clarification")
                if spec.get("type") == "concept_rubric" and clar:
                    clar_task = TaskSpec(
                        task_id=f"{task.task_id}#CLAR", role="CLARIFICATION",
                        level_id=task.level_id,
                        prompt=clar.get("prompt", "Was meinst du?"),
                        answer={"type": "choice",
                                "multiple": False,
                                "options": clar.get("options", [])},
                        clarification_for=task.task_id)
                    return Step(action="CLARIFICATION", task=clar_task)
            return None
        if action == "PREREQUISITE_PROBE":
            probes = [t for t in self.pkg.tasks_for("PREREQUISITE_PROBE")
                      if t.task_id not in s.used_tasks]
            if probes:
                s.used_tasks.add(probes[0].task_id)
                return Step(action="PREREQUISITE_PROBE", task=probes[0])
            # keine Sondierung -> Umweg direkt, falls definiert
            return self._try("PREREQUISITE_DETOUR", task, mis)
        if action == "PREREQUISITE_DETOUR":
            for d in self.policy.detours:
                key = ">".join(d.prerequisite_path)
                # Derselbe Umweg hoechstens zweimal pro Reise: hilft er
                # wiederholt nicht, ist die Pause-Frage ehrlicher.
                if d not in s.detour_stack and s.detour_counts.get(key, 0) < 2:
                    s.detour_stack.append(d)
                    s.detour_counts[key] = s.detour_counts.get(key, 0) + 1
                    return Step(action="PREREQUISITE_DETOUR", detour=d,
                                note=f"Umweg ueber {d.prerequisite_path[0]}, Rueckkehr: {d.return_condition}")
            return None
        if action == "RETURN_TO_TARGET":
            if s.detour_stack:
                d = s.detour_stack.pop()
                s.no_progress = 0   # der Umweg wurde gemeistert = Fortschritt
                s.topic_no_progress = 0
                s.cycles_at_child_choice = 0
                s.level_id = min(d.resume_level, self.pkg.top_level())
                s.role = self._first_role(s.level_id)
                self._reset_attempts()
                # Nach dem Umweg darf bereits gezeigtes Material der Zielebene
                # wieder drankommen – die Luecke ist geschlossen, es ist
                # Wiederholung mit neuem Verstaendnis, kein Rundendrehen.
                nxt = self._next_task(s.role, s.level_id) \
                    or self._next_task(s.role, s.level_id, allow_used=True)
                if nxt:
                    s.used_tasks.add(nxt.task_id)
                return Step(action="RETURN_TO_TARGET", task=nxt, level_id=s.level_id,
                            note=f"zurueck zu {d.return_target}")
            return None
        if action == "SPACED_REVIEW":
            nxt = self._next_task("SPACED_REVIEW", s.level_id)
            if nxt:
                s.used_tasks.add(nxt.task_id)
                return Step(action="SPACED_REVIEW", task=nxt)
            return None
        if action == "CHILD_CHOICE":
            if task is not None:
                s.stuck_tasks.add(task.task_id)
            return self._child_choice()
        return None

    def _child_choice(self, note: str = "") -> Step:
        """Nie aufgeben: das Kind entscheidet (Pause/genug/naechstes Thema).
        Der Themenstand bleibt fortsetzbar (PART 25). Nach max_resume_cycles
        fruchtlosen Sitzungszyklen wird die Empfehlung verbindlich
        (terminal=True): die Laufzeit soll nicht weiter automatisch neu
        anbieten – ein bewusster Wiedereinstieg bleibt aber moeglich."""
        s = self.state
        s.paused = True
        s.cycles_at_child_choice += 1
        terminal = s.cycles_at_child_choice >= self.policy.max_resume_cycles
        return Step(action="CHILD_CHOICE", terminal=terminal,
                    note=note or ("staerkere Pause empfohlen – Thema bleibt offen" if terminal
                                  else "Kind entscheidet: Pause / genug / anderes Thema"))

    def resume(self) -> Step:
        """Neue Sitzung: Sitzungszaehler zurueck, Themenzaehler bleiben.
        Verklemmte Aufgaben (stuck_tasks) werden nicht erneut vorgesetzt –
        resume ist Fortsetzung, kein Reset auf Anfang (PART 22/23)."""
        s = self.state
        s.sessions += 1
        s.paused = False
        s.no_progress = 0
        self._reset_attempts()
        s.used_tasks = set(s.stuck_tasks)
        s.hint_idx = 0
        return self.current_task()

    def _reset_attempts(self) -> None:
        self.state.attempts = 0
        self.state.hint_idx = 0

    def _mis(self, key: str | None):
        for m in self.pkg.misconception_model:
            if key and m.misconception_id.upper() == key.upper().split(".")[-1]:
                return m
        return None

    def pkg_task(self, task_id: str) -> TaskSpec | None:
        for t in self.pkg.tasks:
            if t.task_id == task_id:
                return t
        return None


# ---------------------------------------------------------------- Sackgassen-Pruefung (PART 63)
def check_deadends(pkg: CompleteTopicPackage) -> list[str]:  # noqa: C901
    """Statische Analyse. Fuer alle erreichbaren Zustaende muss es eine
    naechste Aktion geben – ausser MASTERED oder CHILD_SELECTED_STOP."""
    errors: list[str] = []
    level_ids = {l.level_id for l in pkg.competency_ladder}

    # 1. Leiter: von unten bis oben durchgehend
    seen, cur, steps = set(), pkg.bottom_level(), 0
    while cur is not None and steps <= len(level_ids) + 1:
        seen.add(cur)
        cur = (pkg.level(cur) or pkg.level(0)).next_level
        steps += 1
    unreached = level_ids - seen
    if unreached:
        errors.append(f"Ebenen vom Einstieg aus nicht erreichbar: {sorted(unreached)}")
    if cur is not None:
        errors.append("Kompetenzleiter hat einen Kreis (next_level)")

    # 2. Voraussetzungen: Kreise
    from .integrator import find_cycle
    edges = [(pkg.concept_identity.concept_id, e.target_id) for e in pkg.prerequisite_graph.edges]
    cyc = find_cycle(edges)
    if cyc:
        errors.append("Kreis im Voraussetzungsgraphen: " + " -> ".join(cyc))

    # 3. Umwege: Rueckweg vorhanden und konsistent (PART 26)
    for d in pkg.journey_policy.detours:
        if not d.return_target:
            errors.append(f"Umweg {d.prerequisite_path}: kein return_target (Sackgasse)")
        elif d.return_target != pkg.concept_identity.concept_id:
            errors.append(f"Umweg {d.prerequisite_path}: return_target '{d.return_target}' "
                          "ist nicht das Zielkonzept")
        if d.resume_level not in level_ids:
            errors.append(f"Umweg {d.prerequisite_path}: resume_level {d.resume_level} existiert nicht")
        if not d.prerequisite_path:
            errors.append("Umweg ohne prerequisite_path")

    # 4. Meisterschaft erreichbar: oberste Ebene braucht Nachweis
    top = pkg.top_level()
    mastery = [t for t in pkg.tasks if t.role == "MASTERY_CHECK" and t.level_id == top]
    indep = [t for t in pkg.tasks if t.role == "INDEPENDENT" and t.level_id == top]
    tmpl_top = [t for t in pkg.task_templates if t.level_id == top]
    if not mastery:
        errors.append(f"Ebene {top}: keine MASTERY_CHECK-Aufgaben – Meisterschaft nicht nachweisbar")
    if not indep and not tmpl_top:
        errors.append(f"Ebene {top}: keine INDEPENDENT-Aufgaben/Vorlagen")

    # 5. Fehlvorstellungen: jede hat Reparaturweg
    for m in pkg.misconception_model:
        if not m.repair_explanation:
            errors.append(f"{m.misconception_id}: keine repair_explanation")
        if not m.guided_repair and not m.independent_check:
            errors.append(f"{m.misconception_id}: weder guided_repair noch independent_check")

    # 6. UNKNOWN muss Klaerung haben (PART 46): Aufgaben, die 'unknown' liefern
    #    koennen (rubric/free_text), brauchen CLARIFICATION oder eine
    #    clarification im Antwortvertrag selbst.
    clar_for = {t.clarification_for for t in pkg.tasks if t.role == "CLARIFICATION"}
    for t in pkg.tasks:
        if t.answer is None:
            continue
        spec = t.answer.model_dump() if hasattr(t.answer, "model_dump") else t.answer
        can_unknown = spec["type"] in ("concept_rubric", "free_text")
        own_clar = bool(spec.get("clarification")) if spec["type"] == "concept_rubric" else False
        if can_unknown and not own_clar and t.task_id not in clar_for:
            errors.append(f"{t.task_id}: kann UNKNOWN liefern, hat aber keine Klaerungsaufgabe")

    # 7. Jede benutzte Aufgabenrolle hat Material (statisch oder Vorlage)
    roles_have = {t.role for t in pkg.tasks} | {t.role for t in pkg.task_templates}
    for role in pkg.journey_policy.role_order:
        if role not in roles_have:
            errors.append(f"Rolle {role} in role_order ohne Aufgaben/Vorlagen")

    # 8. Einstieg: unterste Ebene braucht Material
    bottom = pkg.bottom_level()
    if not any(t.level_id == bottom for t in pkg.tasks) \
            and not any(t.level_id == bottom for t in pkg.task_templates):
        errors.append(f"Ebene {bottom} (Einstieg) hat kein Material")

    # 9. Hilfskette: wenigstens ein Ausweg pro Ebene neben CHILD_CHOICE
    for l in pkg.competency_ladder:
        exits = bool(
            any(t.hints for t in pkg.tasks if t.level_id == l.level_id)
            or any(t.role in ("GUIDED", "SCAFFOLDED", "WORKED") and t.level_id == l.level_id
                   for t in pkg.tasks)
            or any(t.level_id == l.level_id for t in pkg.task_templates)
            or l.fallback_level is not None
            or pkg.journey_policy.detours)
        if not exits:
            errors.append(f"Ebene {l.level_id}: keine Hilfskette (keine Hinweise/GUIDED/WORKED/"
                          "Vorlagen/Fallback/Umweg) – einziger Ausweg waere CHILD_CHOICE")
    return errors
