"""Integrator: deterministische Prüfungen (ohne KI) – Schema, IDs, Kreise, Niveau-Konsistenz."""
from __future__ import annotations

from collections import defaultdict

from .schemas import Calibration, ConceptGraph, Diagnostics


def find_cycle(edges: list[tuple[str, str]]) -> list[str] | None:
    """edges: (konzept, voraussetzung). Gibt einen Kreis zurück oder None."""
    graph: dict[str, list[str]] = defaultdict(list)
    for a, b in edges:
        graph[a].append(b)
    WHITE, GREY, BLACK = 0, 1, 2
    color: dict[str, int] = defaultdict(int)
    stack: list[str] = []

    def dfs(node: str) -> list[str] | None:
        color[node] = GREY
        stack.append(node)
        for nxt in graph.get(node, []):
            if color[nxt] == GREY:
                return stack[stack.index(nxt):] + [nxt]
            if color[nxt] == WHITE:
                found = dfs(nxt)
                if found:
                    return found
        stack.pop()
        color[node] = BLACK
        return None

    for n in list(graph):
        if color[n] == WHITE:
            found = dfs(n)
            if found:
                return found
    return None


def check_graph(graph: ConceptGraph, block: dict, known: dict[str, dict],
                other_edges: list[tuple[str, str]]) -> tuple[list[str], list[str]]:
    """Gibt (fehler, fehlende_voraussetzungen) zurück. Fehler gehen zurück an den Fachdidaktiker."""
    errors: list[str] = []
    missing: list[str] = []
    ids = [c.id for c in graph.concepts]
    own = {c.id: c for c in graph.concepts}
    if not graph.concepts:
        errors.append("Der Block enthält keine Konzepte.")
    dup = {i for i in ids if ids.count(i) > 1}
    if dup:
        errors.append(f"Doppelte Konzept-IDs: {sorted(dup)}")
    for c in graph.concepts:
        if not c.id.startswith(block["id"] + "."):
            errors.append(f"{c.id}: ID muss mit '{block['id']}.' beginnen.")
        if c.first_contact_grade > c.target_grade:
            errors.append(f"{c.id}: first_contact_grade ({c.first_contact_grade}) > target_grade ({c.target_grade}).")
        for pre in c.prerequisites:
            if pre == c.id:
                errors.append(f"{c.id}: Konzept ist seine eigene Voraussetzung.")
                continue
            if pre.split(".")[0] != block["id"].split(".")[0]:
                errors.append(f"{c.id}: Voraussetzung {pre} liegt in einem anderen Fach. Vorwissen aus anderen Fächern "
                              "(z. B. Sachunterricht) als eigenes Einstiegskonzept in diesem Fach anlegen.")
                continue
            target = own.get(pre)
            pre_grade = target.target_grade if target else (known.get(pre) or {}).get("target_grade")
            if pre_grade is None:
                missing.append(pre)
            elif pre_grade > c.target_grade:
                errors.append(f"{c.id} (Ziel Kl. {c.target_grade}) hat Voraussetzung {pre} mit höherem Ziel "
                              f"(Kl. {pre_grade}). Voraussetzung prüfen oder Klassenstufen anpassen.")
    edges = [(c.id, p) for c in graph.concepts for p in c.prerequisites]
    edges += [(a, b) for a, b in other_edges if a not in own]
    cycle = find_cycle(edges)
    if cycle:
        errors.append("Kreis in den Voraussetzungen: " + " -> ".join(cycle))
    return errors, sorted(set(missing))


def _norm(s: str) -> str:
    import re
    return re.sub(r"[\s.,;:!?]+", " ", str(s).strip().lower()).strip()


def check_answer(item, where: str, keys: set[str], required: bool) -> list[str]:
    """Prüft das Antwortobjekt einer Aufgabe auf Vollständigkeit und Widerspruchsfreiheit."""
    from .visuals.spec import parse_number
    errors: list[str] = []
    a = item.answer
    if a is None:
        return [f"{where}: answer fehlt (auswertbare Antwort ist Pflicht)."] if required else []
    correct_forms: set[str] = set()
    try:
        if a.type == "number":
            correct_forms = {_norm(a.value), _norm(str(a.value).rstrip("0").rstrip(".") if "." in str(a.value) else a.value)}
        elif a.type == "fraction":
            parse_number(a.value)
            correct_forms = {_norm(a.value)}
        elif a.type == "mark":
            correct_forms = {_norm(a.value)}
        elif a.type == "text":
            correct_forms = {_norm(x) for x in a.accepted}
        elif a.type == "choice":
            for o in a.options:
                if o.misconception and o.misconception.upper() not in keys:
                    errors.append(f"{where}: Option verweist auf unbekannte Fehlvorstellung '{o.misconception}'.")
                if o.correct and o.misconception:
                    errors.append(f"{where}: eine richtige Option darf keiner Fehlvorstellung zugeordnet sein.")
    except ValueError as exc:
        errors.append(f"{where}: {exc}")
    correct_value = None
    if a.type in ("number", "fraction", "mark"):
        try:
            correct_value = parse_number(a.value)
        except ValueError:
            correct_value = None
    for d in item.distractors:
        if d.misconception and d.misconception.upper() not in keys:
            errors.append(f"{where}: distractor verweist auf unbekannte Fehlvorstellung '{d.misconception}'.")
        if correct_forms and _norm(d.answer) in correct_forms:
            errors.append(f"{where}: distractor '{d.answer}' ist gleichzeitig eine richtige Antwort.")
            continue
        if correct_value is not None:
            try:
                dv = parse_number(d.answer)
            except ValueError:
                continue
            tol = getattr(a, "tolerance", 0) or 0
            equivalent = abs(dv - correct_value) <= tol + 1e-9 and (a.type != "fraction" or a.accept_equivalent)
            if equivalent:
                errors.append(f"{where}: distractor '{d.answer}' würde als richtig gewertet "
                              f"(gleichwertig/innerhalb der Toleranz) – Fehlvorstellung wäre nicht erkennbar.")
    return errors


def reveals(item, key: str) -> bool:
    key = key.upper()
    if any((d.misconception or "").upper() == key for d in item.distractors):
        return True
    a = item.answer
    return bool(a and a.type == "choice" and any((o.misconception or "").upper() == key for o in a.options))


def check_calibration(concept: dict, cal: Calibration) -> list[str]:
    cal.concept_id = concept["id"]
    t = concept["target_grade"]
    errors: list[str] = []
    for i, it in enumerate(cal.anchor_items):
        if it.level != "target" or it.grade != t:
            errors.append(f"anchor_items[{i}] muss level 'target' und grade {t} haben (ist {it.level}/{it.grade}).")
    for i, it in enumerate(cal.boundary_items.below):
        if it.level != "below" or it.grade > t:
            errors.append(f"boundary_items.below[{i}] muss level 'below' und grade <= {t} haben.")
    for i, it in enumerate(cal.boundary_items.within):
        if it.level != "target" or it.grade != t:
            errors.append(f"boundary_items.within[{i}] muss level 'target' und grade {t} haben.")
    for i, it in enumerate(cal.boundary_items.above):
        if it.level != "above" or it.grade <= t:
            errors.append(f"boundary_items.above[{i}] muss level 'above' und grade > {t} haben.")
    if not cal.boundary_items.above:
        errors.append("boundary_items.above fehlt: mindestens eine Aufgabe, die schon über dem Ziel liegt.")
    if not cal.boundary_items.within:
        errors.append("boundary_items.within fehlt: mindestens eine Aufgabe, die gerade noch zum Ziel gehört.")
    if not cal.difficulty_parameters:
        errors.append("difficulty_parameters ist leer: messbare Grenzen des Zielniveaus angeben.")
    if not cal.can_do.target:
        errors.append("can_do.target ist leer.")
    return errors


def check_diagnostics(concept: dict, diag: Diagnostics, min_auto: int = 2) -> list[str]:
    from .answers import is_auto_checkable
    diag.concept_id = concept["id"]
    t = concept["target_grade"]
    errors: list[str] = []
    keys = {m.key.upper() for m in diag.misconceptions}
    for i, it in enumerate(diag.diagnostic_items):
        errors += check_answer(it, f"diagnostic_items[{i}]", keys, required=True)
    for i, it in enumerate(diag.exit_items):
        errors += check_answer(it, f"exit_items[{i}]", keys, required=True)
    for m in diag.misconceptions:
        errors += check_answer(m.diagnostic_item, f"misconceptions[{m.key}].diagnostic_item", keys, required=True)
        if m.diagnostic_item.answer and not reveals(m.diagnostic_item, m.key):
            errors.append(f"misconceptions[{m.key}].diagnostic_item: die typische falsche Antwort muss als distractor "
                          f"(oder choice-Option) mit misconception '{m.key}' hinterlegt sein, sonst erkennt Karo sie nicht.")
    target_probes = [it for it in diag.diagnostic_items if it.level == "target"] + \
                    [m.diagnostic_item for m in diag.misconceptions]
    if sum(is_auto_checkable(it.answer) for it in target_probes) < min_auto:
        errors.append(f"Mindestens {min_auto} automatisch auswertbare Diagnoseaufgaben auf Zielniveau nötig "
                      "(number, fraction, choice, text, order, match, mark).")
    if sum(is_auto_checkable(it.answer) for it in diag.exit_items) < min_auto:
        errors.append(f"Mindestens {min_auto} automatisch auswertbare Abschlussaufgaben nötig.")
    for i, it in enumerate(diag.exit_items):
        if it.level != "target" or it.grade != t:
            errors.append(f"exit_items[{i}] muss level 'target' und grade {t} haben (ist {it.level}/{it.grade}).")
    keys = [m.key.upper() for m in diag.misconceptions]
    if len(keys) != len(set(keys)):
        errors.append(f"Doppelte Fehlvorstellungs-Keys: {keys}")
    for m in diag.misconceptions:
        if not m.key.upper().replace("_", "").isalnum():
            errors.append(f"Key '{m.key}' ungültig (nur Buchstaben/Ziffern).")
    for i, it in enumerate(diag.diagnostic_items):
        if it.level == "above":
            errors.append(f"diagnostic_items[{i}] liegt über dem Ziel – Diagnose nur auf below/target.")
    if t > 1 and not any(it.level == "below" for it in diag.diagnostic_items):
        errors.append("diagnostic_items braucht mindestens eine Aufgabe auf 'below', damit Karo tiefere Lücken erkennt.")
    return errors


#: Darstellungen, deren Beschriftungen automatisch auf Überlappung und Rand geprüft werden
AUDITED = {"flow_diagram", "table", "cycle"}


def check_visuals(concept: dict, vset, misconception_keys: set[str]) -> list[str]:  # noqa: C901
    """Prüft die Ausgabe des Visual-Didaktikers: Bedarf, Verweise, Niveaus und ob jede Darstellung zeichenbar ist."""
    from .visuals.render import layout_problems, render

    def drawable(visual, where: str) -> None:
        try:
            svg = render(visual)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{where}: Darstellung nicht zeichenbar ({exc}).")
            return
        if getattr(visual, "type", "") in AUDITED:
            for p in layout_problems(svg):
                errors.append(f"{where}: Beschriftung nicht lesbar – {p}. Kürzer formulieren oder aufteilen.")

    vset.concept_id = concept["id"]
    t = concept["target_grade"]
    errors: list[str] = []
    if vset.visual_need == "none":
        if vset.explanations or vset.visual_items:
            errors.append("visual_need ist 'none', aber es gibt Erklärungen/visuelle Aufgaben – entweder Bedarf "
                          "anpassen oder Listen leeren.")
        return errors
    if not vset.explanations:
        errors.append(f"visual_need ist '{vset.visual_need}', aber explanations ist leer.")
    if vset.visual_need == "essential" and not any(e.level == "target" for e in vset.explanations):
        errors.append("Bei visual_need 'essential' braucht es mindestens eine Erklärung auf level 'target'.")
    keys = [e.key.upper() for e in vset.explanations]
    if len(keys) != len(set(keys)):
        errors.append(f"Doppelte Erklärungs-Keys: {keys}")
    for i, e in enumerate(vset.explanations):
        if e.for_misconception and e.for_misconception.upper() not in misconception_keys:
            errors.append(f"explanations[{i}].for_misconception '{e.for_misconception}' gibt es nicht "
                          f"(vorhanden: {sorted(misconception_keys) or 'keine'}).")
        for k, step in enumerate(e.steps):
            drawable(step.visual, f"explanations[{i}].steps[{k}]")
    for i, it in enumerate(vset.visual_items):
        if it.use == "exit" and (it.level != "target" or it.grade != t):
            errors.append(f"visual_items[{i}] (exit) muss level 'target' und grade {t} haben.")
        if it.use in ("diagnostic", "exit") and it.level == "above":
            errors.append(f"visual_items[{i}]: Diagnose/Abschluss nicht über dem Zielniveau.")
        errors += check_answer(it, f"visual_items[{i}]", misconception_keys, required=it.use != "practice")
        drawable(it.visual, f"visual_items[{i}]")
    return errors
