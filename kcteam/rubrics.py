"""Rubrik-Vertrag und deterministische Bewertung (PART 46-49).

Der Rubrik-Ingenieur erzeugt zur Bauzeit einen Bewertungsvertrag
(`AnswerConceptRubric` in answers.py). Diese Datei bewertet Kind-Antworten
gegen diesen Vertrag – ohne Modell, ohne Zufaelligkeit – und liefert die
adversariale Pruefbatterie, mit der die Fabrik jeden Vertrag vorab gegen
typische Grenzfaelle haertet (Phase 3 der Schleife).
"""
from __future__ import annotations

import re
import unicodedata
from typing import Any

# ---------------------------------------------------------------- Normalisierung
_UMLAUT = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss",
                       "Ä": "ae", "Ö": "oe", "Ü": "ue"})
_ARTICLES = {"der", "die", "das", "dem", "den", "des",
             "ein", "eine", "einer", "eines", "einem", "einen"}
_PUNCT = re.compile(r"[^\w\s]|_", re.UNICODE)


def _tokens(text: str, rules: list[str]) -> list[str]:
    """Antwort -> normalisierte Tokenliste, je nach Vertragsregeln."""
    s = str(text)
    if "umlauts" in rules:
        s = s.translate(_UMLAUT)
    if "lower" in rules:
        s = s.lower()
    else:
        s = unicodedata.normalize("NFC", s)
    if "punctuation" in rules:
        s = _PUNCT.sub(" ", s)
    toks = s.split()
    if "articles" in rules:
        toks = [t for t in toks if t not in _ARTICLES]
    return toks


def _edit1(a: str, b: str) -> bool:
    """Edit-Distanz <= 1 (nur fuer laengere Woerter sinnvoll)."""
    if a == b:
        return True
    la, lb = len(a), len(b)
    if abs(la - lb) > 1 or min(la, lb) < 5:
        return False
    if la == lb:
        return sum(x != y for x, y in zip(a, b)) <= 1
    if la > lb:
        a, b = b, a
    # b ist um 1 laenger: ein eingefuegtes Zeichen erlaubt
    i = j = 0
    skipped = False
    while i < len(a) and j < len(b):
        if a[i] == b[j]:
            i += 1
            j += 1
        elif skipped:
            return False
        else:
            skipped = True
            j += 1
    return True


NEGATIONS = {"nicht", "kein", "keine", "keinen", "keinem", "keiner",
             "niemals", "nie", "ohne"}


def _contains(tokens: list[str], phrase: str, rules: list[str]) -> bool:
    """Phrase (auch mehrwortig) im Tokenstrom? Mit 'typo' edit1-Toleranz."""
    return _match_at(tokens, phrase, rules) is not None


def _match_at(tokens: list[str], phrase: str, rules: list[str]) -> int | None:
    """Index des Phrasentematches im Tokenstrom oder None."""
    want = _tokens(phrase, rules)
    if not want:
        return None
    n = len(want)
    for i in range(len(tokens) - n + 1):
        window = tokens[i:i + n]
        if all(w == g or ("typo" in rules and _edit1(w, g))
               for w, g in zip(want, window)):
            return i
    return None


def _negated(tokens: list[str], idx: int, span: int) -> bool:
    """Steht direkt vor/nach dem Treffer eine Negation? Dann zaehlt der
    Konzepttreffer nicht (Substring-Kollision 'kein X' != 'X')."""
    lo, hi = max(0, idx - 2), min(len(tokens), idx + span + 2)
    return any(tokens[j] in NEGATIONS for j in range(lo, hi) if j < idx or j >= idx + span)


# ---------------------------------------------------------------- Vertrag-Bewertung
def _variants_of(c: Any) -> list[str]:
    if isinstance(c, str):
        return [c]
    return [c.get("concept", ""), *(c.get("accepted") or [])]


def _pattern_hit(spec_list: list[dict] | None, tokens: list[str],
                 rules: list[str]) -> dict | None:
    for m in spec_list or []:
        pats = m.get("patterns") if isinstance(m, dict) else getattr(m, "patterns", [])
        if any(_contains(tokens, p, rules) for p in pats or []):
            return m if isinstance(m, dict) else m.model_dump()
    return None


def evaluate_rubric(spec: dict, given: Any) -> tuple[str, str | None, float]:
    """(outcome, misconception_id, score) fuer eine concept_rubric-Antwort.

    Reihenfolge: unknown_markers -> misconceptions -> contradictions ->
    Konzeptabdeckung (correct/partial/unknown). Alles lokal, ohne Modell."""
    rules = spec.get("normalization") or ["lower", "umlauts", "punctuation",
                                          "articles", "typo"]
    tokens = _tokens(str(given), rules)
    if not tokens:
        return "unknown", None, 0.0

    markers = spec.get("unknown_markers") or []
    if any(_contains(tokens, m, rules) for m in markers):
        return "unknown", None, 0.0

    def _concept_hit(c: Any) -> bool:
        for v in _variants_of(c):
            idx = _match_at(tokens, v, rules)
            if idx is not None and not _negated(tokens, idx, len(_tokens(v, rules))):
                return True
        return False

    if len(tokens) <= 2 and not any(
            _concept_hit(c) for c in spec.get("required_concepts", [])):
        return "unknown", None, 0.0

    hit = _pattern_hit(spec.get("misconceptions"), tokens, rules)
    if hit:
        return "misconception", hit.get("misconception"), 0.0
    hit = _pattern_hit(spec.get("contradictions"), tokens, rules)
    if hit:
        return "incorrect", hit.get("misconception"), 0.0

    hits, missing = 0, []
    for c in spec.get("required_concepts", []):
        variants = _variants_of(c)
        if _concept_hit(c):
            hits += 1
        else:
            missing.append(variants[0])
    nreq = max(1, len(spec.get("required_concepts", [])))
    if hits >= (spec.get("min_required") or nreq):
        return "correct", None, 1.0
    if hits >= max(1, spec.get("partial_min", 1)):
        return "partial", None, hits / nreq
    return "unknown", None, 0.0


# ---------------------------------------------------------------- adversariale Batterie
def adversarial_cases(spec: dict) -> list[tuple[str, Any, str]]:
    """(name, probe, erwartetes_outcome) – aus dem Vertrag selbst erzeugt.

    Deckt die Pflichtfaelle ab: richtige Paraphrase, Teilantwort,
    Fehlvorstellung, Widerspruch, Negation, Substring-Kollision, Tippfehler,
    irrelevante Zugabe, Nichtwissen."""
    cases: list[tuple[str, Any, str]] = []
    req = spec.get("required_concepts") or []
    canonical = [_variants_of(c)[0] for c in req]

    paraphrase_parts = []
    for c in req:
        vs = _variants_of(c)
        paraphrase_parts.append(vs[1] if len(vs) > 1 else vs[0])
    if paraphrase_parts:
        cases.append(("paraphrase", " ".join(paraphrase_parts),
                      "correct" if len(req) <= (spec.get("min_required") or len(req))
                       else "correct"))
    if canonical:
        cases.append(("kanonisch", " ".join(canonical), "correct"))
        cases.append(("teilweise", canonical[0], "partial"
                      if len(req) > 1 else "correct"))
        cases.append(("irrelevante_zugabe",
                      " ".join(canonical) + " und ich mag fussball", "correct"))
        longest = max(canonical, key=len)
        typo_probe = " ".join(canonical).replace(longest, _with_typo(longest), 1)
        cases.append(("typo", typo_probe, "correct"
                      if "typo" in (spec.get("normalization") or ["typo"])
                      else "unknown"))
        cases.append(("negation", f"nicht {canonical[0]}", "not_correct"))
    for m in spec.get("misconceptions") or []:
        pat = (m.get("patterns") or [None])[0]
        if pat:
            cases.append((f"fehlvorstellung:{m.get('misconception')}",
                          pat, "misconception"))
    for m in spec.get("contradictions") or []:
        pat = (m.get("patterns") or [None])[0]
        if pat:
            cases.append((f"widerspruch:{pat[:30]}", pat, "incorrect"))
    cases.append(("nichtwissen", spec.get("unknown_markers", ["keine ahnung"])[0]
                  if spec.get("unknown_markers") else "keine ahnung", "unknown"))
    cases.append(("leer", "", "skipped"))
    cases.append(("unsinn", "asdkfj alskdjf", "unknown"))
    if canonical:
        # Substring-Kollision: Konzeptwort in verneintem Kontext darf
        # nicht als Treffer zaehlen.
        cases.append(("substring_kollision",
                      f"das stimmt nicht: ohne {canonical[0]}", "not_correct"))
    return cases


def _with_typo(word: str) -> str:
    """Ein realistischer Vertipper (ein Zeichen geloescht)."""
    w = str(word)
    if len(w) < 6:
        return w
    i = len(w) // 2
    return w[:i] + w[i + 1:]


def run_battery(spec: dict, *, task_id: str = "") -> list[str]:
    """Fuehrt die Batterie aus; liefert Befunde fuer die Fabrik."""
    findings: list[str] = []
    for name, probe, expected in adversarial_cases(spec):
        outcome, mis, _ = evaluate_rubric(spec, probe)
        if expected == "not_correct":
            ok = outcome != "correct"
        elif expected == "skipped":
            ok = outcome in ("unknown", "skipped") or not str(probe).strip()
        else:
            ok = outcome == expected
        if not ok:
            findings.append(
                f"Rubrik {task_id or spec.get('type','?')}: Fall '{name}' -> "
                f"'{outcome}' statt '{expected}' (Probe: {str(probe)[:60]!r})")
    return findings
