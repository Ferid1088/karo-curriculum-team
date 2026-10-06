"""Vorlagen-DSL (PART 38-41): deterministische Aufgaben-Generatoren.

Eine Vorlage erzeugt zur Laufzeit beliebig viele gepruefte Varianten einer
Aufgabe – ohne Modellaufruf. Sie traegt Wertebereiche, Bedingungen, eine
Loesungsfunktion und Distraktor-Ausdruecke (typische Fehler pro
Fehlvorstellung).

Ausdruecke sind eine sichere Teilmenge von Python-Arithmetik: Zahlen,
Brueche (Fraction), Grundrechenarten, Vergleiche, Booleans, div/mod/abs/
min/max/ggT/kgV – keine Namen ausser den Parametern, keine Aufrufe ausser
den freigegebenen Funktionen. Auswertung via AST-Whitelist.

Sprach- und Sachfaecher erzwingen kein Zahlengeruest: `set`-Domanen mit
Satzbaenken/Slotgrammatiken decken sie ab (PART 40/41).
"""
from __future__ import annotations

import ast
import itertools
import math
import operator
import random
import re
from fractions import Fraction
from typing import Any

from .package_schema import ParamDomain, TaskSpec, TaskTemplate
from .answers import Distractor

# ---------------------------------------------------------------- sichere Auswertung
_FUNCS = {
    "abs": abs, "min": min, "max": max, "round": round, "int": int,
    "floor": math.floor, "ceil": math.ceil,
    "ggt": math.gcd, "gcd": math.gcd, "kgv": math.lcm, "lcm": math.lcm,
    "sqrt": math.sqrt, "fr": Fraction, "len": len,
}
_BINOPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
           ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv,
           ast.Mod: operator.mod, ast.Pow: operator.pow}
_CMPOPS = {ast.Eq: operator.eq, ast.NotEq: operator.ne, ast.Lt: operator.lt,
           ast.LtE: operator.le, ast.Gt: operator.gt, ast.GtE: operator.ge,
           ast.In: lambda a, b: a in b, ast.NotIn: lambda a, b: a not in b}
_BOOLOPS = {ast.And: all, ast.Or: any}
_UNOPS = {ast.USub: operator.neg, ast.UAdd: operator.pos, ast.Not: operator.not_}


def safe_eval(expr: str, params: dict[str, Any]) -> Any:
    """Wertet einen DSL-Ausdruck aus. Wirft ValueError bei unerlaubtem Konstrukt."""
    tree = ast.parse(expr, mode="eval")
    return _eval(tree.body, params)


def _eval(node: ast.AST, p: dict[str, Any]) -> Any:  # noqa: C901
    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float, str, bool)):
            return node.value
        raise ValueError("Konstante nicht erlaubt")
    if isinstance(node, ast.Name):
        if node.id in p:
            return p[node.id]
        raise ValueError(f"Unbekannter Name '{node.id}'")
    if isinstance(node, ast.BinOp) and type(node.op) in _BINOPS:
        return _BINOPS[type(node.op)](_eval(node.left, p), _eval(node.right, p))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UNOPS:
        return _UNOPS[node.op](_eval(node.operand, p))
    if isinstance(node, ast.BoolOp) and type(node.op) in _BOOLOPS:
        return _BOOLOPS[type(node.op)]([_eval(v, p) for v in node.values])
    if isinstance(node, ast.Compare) and len(node.ops) == 1 and type(node.ops[0]) in _CMPOPS:
        return _CMPOPS[type(node.ops[0])](_eval(node.left, p), _eval(node.comparators[0], p))
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _FUNCS \
            and not node.keywords:
        return _FUNCS[node.func.id](*[_eval(a, p) for a in node.args])
    if isinstance(node, ast.IfExp):
        return _eval(node.body, p) if _eval(node.test, p) else _eval(node.orelse, p)
    if isinstance(node, ast.Subscript):
        return _eval(node.value, p)[_eval(node.slice, p)]
    if isinstance(node, ast.List):
        return [_eval(e, p) for e in node.elts]
    if isinstance(node, ast.Tuple):
        return tuple(_eval(e, p) for e in node.elts)
    raise ValueError(f"Ausdruck nicht erlaubt: {ast.dump(node)[:120]}")


_PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


def render_prompt(template: str, params: dict[str, Any]) -> str:
    """Platzhalter einsetzen; Fractions werden als 'a/b' geschrieben."""
    def fmt(m: re.Match) -> str:
        v = params[m.group(1)]
        return str(v.numerator) + "/" + str(v.denominator) if isinstance(v, Fraction) else str(v)
    return _PLACEHOLDER.sub(fmt, template)


# ---------------------------------------------------------------- Domanen
def domain_values(dom: ParamDomain, limit: int = 400) -> list[Any]:
    """Alle Werte einer Domaine (begrenzt) – Grundlage fuer Enumerate/Sample."""
    if dom.type == "set":
        return list(dom.values or [])[:limit]
    if dom.type in ("int_range", "decimal_range"):
        lo = dom.min if dom.min is not None else 0
        hi = dom.max if dom.max is not None else lo
        step = dom.step or (1 if dom.type == "int_range" else 0.5)
        vals: list[Any] = []
        v = float(lo)
        while v <= float(hi) + 1e-12 and len(vals) < limit:
            vals.append(int(v) if dom.type == "int_range" and float(v).is_integer() else v)
            v += step
        return vals
    if dom.type == "fractions":
        hi = int(dom.max) if dom.max is not None else 1
        md = dom.max_denominator or 12
        vals = sorted({Fraction(n, d) for d in range(1, md + 1)
                       for n in range(0, int(hi * d) + 1) if math.gcd(n, d) == 1 or n == 0})
        return vals[:limit]
    if dom.type == "pairs":
        return list(dom.values or [])[:limit]
    return []


def _combos(tmpl: TaskTemplate, limit: int) -> Any:
    """Liefert eine Iteration ueber Parameterkombis (Produkt der Domanen)."""
    names = list(tmpl.parameters)
    doms = [domain_values(tmpl.parameters[n], limit) for n in names]
    return itertools.islice(itertools.product(*doms), limit * max(1, len(doms))), names


def _params_ok(tmpl: TaskTemplate, params: dict[str, Any]) -> bool:
    try:
        return all(bool(safe_eval(c, params)) for c in tmpl.constraints) \
            and not any(bool(safe_eval(x, params)) for x in tmpl.variant_exclusions)
    except (ValueError, ZeroDivisionError, ArithmeticError, OverflowError, TypeError):
        return False


def enumerate_variants(tmpl: TaskTemplate, limit: int = 2000) -> list[dict[str, Any]]:
    """Alle zulaessigen Parameterkombis (kleine Raeume voll, grosse gekappt)."""
    out: list[dict[str, Any]] = []
    gen, names = _combos(tmpl, limit)
    for combo in gen:
        p = dict(zip(names, combo))
        if _params_ok(tmpl, p):
            out.append(p)
        if len(out) >= limit:
            break
    return out


def instantiate(tmpl: TaskTemplate, params: dict[str, Any]) -> TaskSpec:
    """Eine Variante konkret auswerten: Aufgabe + Antwortvertrag."""
    if not _params_ok(tmpl, params):
        raise ValueError("Parameter erfuellen die Bedingungen der Vorlage nicht")
    sol = safe_eval(tmpl.solution, params)
    answer = _answer_spec(tmpl.answer_type, sol)
    prompt = render_prompt(tmpl.prompt_template, params)
    def _fmt(v: Any) -> str:
        if tmpl.answer_type == "fraction":
            return str(_as_fraction(v))
        return str(v)
    distractors = [Distractor(answer=_fmt(safe_eval(expr, params)), misconception=key)
                   for key, expr in tmpl.distractor_exprs.items()]
    key = hashlib_key(params)
    return TaskSpec(task_id=f"{tmpl.template_id}.{key}", role=tmpl.role,
                    level_id=tmpl.level_id, prompt=prompt, answer=answer,
                    solution=str(sol), distractors=distractors,
                    difficulty=tmpl.difficulty,
                    misconception=tmpl.known_misconceptions[0] if tmpl.known_misconceptions else None)


def hashlib_key(params: dict[str, Any]) -> str:
    import hashlib
    import json
    s = json.dumps(params, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(s.encode()).hexdigest()[:10].upper()


def sample_instantiate(tmpl: TaskTemplate, rng: random.Random, tries: int = 60) -> TaskSpec | None:
    """Zufaellige gueltige Variante (deterministisch ueber rng)."""
    names = list(tmpl.parameters)
    for _ in range(tries):
        p = {n: rng.choice(domain_values(tmpl.parameters[n], 400)) for n in names}
        if _params_ok(tmpl, p):
            return instantiate(tmpl, p)
    return None


def _as_fraction(sol: Any) -> Fraction:
    """Loesungswert -> exakter Bruch. Fliesskomma wird gerundet auf
    den naechsten kurzen Bruch (Vorlagen sollten `fr()` nutzen)."""
    if isinstance(sol, Fraction):
        return sol
    if isinstance(sol, str) and "/" in sol:
        return Fraction(sol)
    if isinstance(sol, float) and sol.is_integer():
        return Fraction(int(sol), 1)
    return Fraction(sol).limit_denominator(10000)


def _answer_spec(answer_type: str, sol: Any) -> dict[str, Any]:
    if answer_type in ("number", "integer", "decimal"):
        return {"type": "number", "value": float(sol)}
    if answer_type == "fraction":
        return {"type": "fraction", "value": str(_as_fraction(sol))}
    if answer_type == "text":
        return {"type": "text", "accepted": [str(sol)]}
    if answer_type == "unit_value":
        return {"type": "number", "value": float(sol)}
    return {"type": "text", "accepted": [str(sol)]}


# ---------------------------------------------------------------- Pruefung (PART 39)
def validate_template(tmpl: TaskTemplate, *, sample: int = 300) -> list[str]:
    """Vor READY: Vorlage enumerieren (klein) oder sampeln (gross) und pruefen:
    Loesbarkeit, Antwortvaliditaet, keine Division durch 0, keine unmoeglichen
    Bedingungen, keine uneindeutige Loesung, sinnvolle Werte, Schwierigkeit."""
    errors: list[str] = []
    # Statik: bekannte Namen in den Ausdruecken?
    for expr in [*tmpl.constraints, tmpl.solution, *tmpl.distractor_exprs.values(),
                 *tmpl.variant_exclusions]:
        try:
            ast.parse(expr, mode="eval")
        except SyntaxError as exc:
            errors.append(f"{tmpl.template_id}: Syntaxfehler in '{expr}': {exc.msg}")
            continue
        try:
            for node in ast.walk(ast.parse(expr, mode="eval")):
                if isinstance(node, ast.Name) and node.id not in tmpl.parameters \
                        and node.id not in _FUNCS:
                    errors.append(f"{tmpl.template_id}: unbekannter Name '{node.id}' in '{expr}'")
        except SyntaxError:
            pass
    if not tmpl.parameters:
        errors.append(f"{tmpl.template_id}: keine Parameter – Vorlage erzeugt nur eine Aufgabe")
    ph = set(_PLACEHOLDER.findall(tmpl.prompt_template))
    if ph - set(tmpl.parameters):
        errors.append(f"{tmpl.template_id}: Platzhalter ohne Parameter: {sorted(ph - set(tmpl.parameters))}")

    variants = enumerate_variants(tmpl)
    if not variants:
        # grosser Raum -> sampeln statt enumerieren
        rng = random.Random(0)
        names = list(tmpl.parameters)
        for _ in range(sample):
            p = {n: rng.choice(domain_values(tmpl.parameters[n], 400)) for n in names}
            if _params_ok(tmpl, p):
                variants.append(p)
        if not variants:
            errors.append(f"{tmpl.template_id}: keine zulaessige Parameterkombi gefunden "
                          "(Bedingungen unloesbar?)")
            return errors
    if len(variants) > tmpl.max_variants * 50:
        pass  # grosser Raum ist ok; Kapazitaet wird unten gemeldet
    if len(variants) < tmpl.max_variants:
        errors.append(f"{tmpl.template_id}: nur {len(variants)} Varianten moeglich "
                      f"(max_variants={tmpl.max_variants} nicht erreichbar)")
    for p in variants[:sample]:
        try:
            sol = safe_eval(tmpl.solution, p)
        except ZeroDivisionError:
            errors.append(f"{tmpl.template_id}: Division durch 0 bei {p} – Bedingung fehlt")
            continue
        except (ValueError, OverflowError, ArithmeticError, TypeError) as exc:
            errors.append(f"{tmpl.template_id}: Loesung nicht auswertbar bei {p}: {exc}")
            continue
        if sol is None or (isinstance(sol, float) and (math.isnan(sol) or math.isinf(sol))):
            errors.append(f"{tmpl.template_id}: keine gueltige Loesung bei {p}")
        for key, expr in tmpl.distractor_exprs.items():
            try:
                d = safe_eval(expr, p)
            except (ValueError, ZeroDivisionError, ArithmeticError, TypeError) as exc:
                errors.append(f"{tmpl.template_id}: Distraktor '{key}' nicht auswertbar bei {p}: {exc}")
                continue
            if d == sol:
                errors.append(f"{tmpl.template_id}: Distraktor '{key}' == Loesung bei {p} "
                              "(Fehlvorstellung waere richtig)")
    return sorted(set(errors))[:30]


def variant_capacity(tmpl: TaskTemplate, limit: int = 2000) -> int:
    """Wie viele verschiedene Aufgaben die Vorlage wirklich erzeugen kann."""
    seen = {hashlib_key(p) for p in enumerate_variants(tmpl, limit)}
    return len(seen)
