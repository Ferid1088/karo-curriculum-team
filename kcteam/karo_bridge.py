"""Brücke: COMPLETE_TOPIC_PACKAGE → Lektion im karo_contract-Format.

Schleife PART 14-16/36: liegt fuer ein Konzept ein READY-Paket vor, muss
die Lektion, die Karo per `/v1/lessons` bestellt, ohne Modellaufruf aus
dem Paket entstehen. Diese Datei bildet Paketbestandteile auf den
Abnehmervertrag ab (fehlertypen, hilfe, faq, erstkontakt, Aufgabenrollen,
Rubriken, Visualisierungen). Sie repariert nichts: was sie nicht sauber
abbilden kann, wirft `BrueckeUnmoeglich` – der Aufrufer faellt dann auf
den generierenden Pfad zurueck und die Luecke ist ein Befund, kein
stilles Fehlen.
"""
from __future__ import annotations

import re
from typing import Any

from .package_schema import CompleteTopicPackage, TaskSpec


class BrueckeUnmoeglich(Exception):
    """Das Paket traegt keinen Teil, den der Abnehmervertrag fordert."""


# Paket-Antworttyp -> antwort_art des Abnehmers (antwortvergleich.py).
_ANTWORT_ART = {
    "number": "zahl", "fraction": "bruch", "choice": "auswahl",
    "text": "text", "free_text": "freitext", "order": "text",
    "match": "text", "mark": "text", "concept_rubric": "begriffe",
}

# Paket-Visualtyp/Bezeichner -> Registerkomponente des Abnehmers.
_KOMPONENTE = {
    "number_line": "NumberLine", "fraction_bar": "FractionStrip",
    "fraction_strip": "FractionStrip", "area_model": "AreaModel",
    "balance": "Balance", "concept_map": "ConceptMap",
    "data_chart": "DataTable", "data_table": "DataTable",
    "bar_chart": "DataTable",
}

_FALLBACK_VIS = {"component": "GenericStepFlow", "parameters": {},
                 "animation": "none"}

_HILFE_PHASEN = ("HOOK", "RULE", "WORKED_EXAMPLE", "GUIDED_TASK",
                 "INDEPENDENT_TASK", "ADAPTATION")


def _art(task: TaskSpec) -> str | None:
    if task.answer is None:
        return None
    return _ANTWORT_ART.get(getattr(task.answer, "type", ""), "text")


def _rubrik(task: TaskSpec) -> dict | None:
    """Begriffs-Rubrik (Vertrag 1.5) aus dem AnswerConceptRubric."""
    a = task.answer
    if a is None or getattr(a, "type", "") != "concept_rubric":
        return None
    begriffe = [b for b in (a.required_concepts or []) if str(b).strip()]
    if not begriffe:
        return None
    out: dict[str, Any] = {"begriffe": begriffe[:12]}
    if a.min_required:
        out["mindestens"] = max(1, min(int(a.min_required), len(begriffe)))
    hinweise = {k: v for k, v in (a.misconceptions or {}).items()
                if str(k).strip() and str(v).strip()}
    if hinweise:
        out["hinweise"] = hinweise
    return out


def _aufgabe(pkg: CompleteTopicPackage, task: TaskSpec | None, rolle: str,
             mis_id: str | None = None) -> dict:
    """Paketaufgabe -> Karo-Aufgabe. `rolle` ist die Abnehmer-Rolle."""
    if task is None:
        raise BrueckeUnmoeglich(f"keine Aufgabe fuer Rolle {rolle}"
                                + (f" / Fehlvorstellung {mis_id}" if mis_id else ""))
    out: dict[str, Any] = {"frage": task.prompt.strip(),
                           "loesung": (task.solution or "").strip()}
    if not out["frage"] or not out["loesung"]:
        raise BrueckeUnmoeglich(f"Aufgabe {task.task_id} ohne Frage/Loesung")
    if task.worked_steps:
        out["schritte"] = [s.strip() for s in task.worked_steps if s.strip()]
    if task.hints and task.hints.hints:
        out["tipps"] = [h.strip() for h in task.hints.hints if h.strip()]
    art = _art(task)
    if art:
        out["antwort_art"] = art
    rubrik = _rubrik(task)
    if rubrik:
        out["rubrik"] = rubrik
    if rolle in ("vorhersage", "transfer"):
        # Auswahl: Loesung plus Fehlvorstellungs-Ablenker.
        falsch = [str(d.answer).strip() for d in task.distractors
                  if getattr(d, "answer", None) is not None and str(d.answer).strip()]
        if not falsch and mis_id:
            falsch = _fehlantworten(pkg, mis_id)
        if len(falsch) < 1:
            raise BrueckeUnmoeglich(f"Aufgabe {task.task_id} ({rolle}) ohne Auswahloptionen")
        out["optionen"] = [out["loesung"], *falsch][:4]
        out["aufloesung"] = (f"„{out['loesung']}“ ist richtig. "
                             + "; ".join(f"„{f}“" for f in falsch[:3])
                             + (" folgt einer Fehlvorstellung." if mis_id else " ist falsch."))
    if rolle in ("gefuehrt", "selbststaendig"):
        fehler = next((str(d.answer).strip() for d in task.distractors
                       if getattr(d, "misconception", None) == mis_id and str(d.answer).strip()), None) \
                 or (next(iter(_fehlantworten(pkg, mis_id)), None) if mis_id else None) \
                 or next((str(d.answer).strip() for d in task.distractors
                          if getattr(d, "answer", None) is not None and str(d.answer).strip()), None)
        if not fehler:
            raise BrueckeUnmoeglich(
                f"Aufgabe {task.task_id} ({rolle}) ohne typischen Fehler")
        out["typischer_fehler"] = fehler
    return out


def _fehlantworten(pkg: CompleteTopicPackage, mis_id: str) -> list[str]:
    out: list[str] = []
    for m in pkg.misconception_model:
        if m.misconception_id == mis_id:
            out.extend(p.strip() for p in m.observable_answer_patterns if p.strip())
    for t in pkg.tasks:
        if t.misconception == mis_id and t.answer is not None:
            for k, v in (getattr(t.answer, "misconceptions", None) or {}).items():
                if v.strip():
                    out.append(v.strip())
            for d in t.distractors:
                if getattr(d, "answer", None) is not None and str(d.answer).strip():
                    out.append(str(d.answer).strip())
    return list(dict.fromkeys(out))


def _task_fuer(pkg: CompleteTopicPackage, rollen: tuple[str, ...],
               mis_id: str | None = None, level: int | None = None,
               ausser: set[str] = frozenset()) -> TaskSpec | None:
    kandidaten = [t for t in pkg.tasks
                  if t.role in rollen and t.task_id not in ausser]
    for t in kandidaten:
        if mis_id and t.misconception == mis_id:
            return t
    for t in kandidaten:
        if level is not None and t.level_id == level:
            return t
    return kandidaten[0] if kandidaten else None


def _rollen_aufgaben(pkg: CompleteTopicPackage, m) -> dict:
    """Die fuenf Abnehmer-Rollen fuer einen Fehlertyp — ohne die gleiche
    Paketaufgabe zweimal zu verwenden (Abnehmerpruefung verlangt verschiedene
    Aufgaben fuer beispiel/gefuehrt/selbststaendig)."""
    mis = m.misconception_id
    benutzt: set[str] = set()

    def nimm(*rollen: str, bevorzugt: list[str] | None = None) -> TaskSpec | None:
        t = None
        for tid in (bevorzugt or []):
            t = _task_mit_id(pkg, tid)
            if t is not None and t.task_id in benutzt:
                t = None
            if t is not None:
                break
        if t is None:
            t = _task_fuer(pkg, rollen, mis_id=mis, ausser=benutzt)
        if t is not None:
            benutzt.add(t.task_id)
        return t

    return {
        "vorhersage": _aufgabe(pkg, nimm("MISCONCEPTION_PROBE", "DIAGNOSTIC"), "vorhersage", mis),
        "beispiel": _aufgabe(pkg, nimm("WORKED"), "beispiel", mis),
        "gefuehrt": _aufgabe(pkg, nimm("GUIDED", "SCAFFOLDED",
                                     bevorzugt=m.guided_repair), "gefuehrt", mis),
        "selbststaendig": _aufgabe(pkg, nimm("INDEPENDENT",
                                           bevorzugt=m.independent_check), "selbststaendig", mis),
        "transfer": _aufgabe(pkg, nimm("TRANSFER"), "transfer", mis),
    }


def _task_mit_id(pkg: CompleteTopicPackage, task_id: str | None) -> TaskSpec | None:
    if not task_id:
        return None
    return next((t for t in pkg.tasks if t.task_id == task_id), None)


def _visual(pkg: CompleteTopicPackage, ref: str | None = None) -> dict:
    """VisualAsset -> Registerauswahl. Ohne belastbare Parameter: der sichere
    Schrittfluss-Fallback, auf den der Vertrag ohnehin degradiert."""
    asset = None
    if ref:
        asset = next((v for v in pkg.visual_assets if v.asset_id == ref), None)
    if asset is None:
        asset = next((v for v in pkg.visual_assets
                      if v.visual_type in ("DETERMINISTIC_DIAGRAM", "DATA_CHART")), None)
    if asset is not None:
        daten = asset.structured_data or {}
        comp = daten.get("component") or _KOMPONENTE.get(
            str(daten.get("renderer") or "").lower()) or (
            "DataTable" if asset.visual_type == "DATA_CHART" else None)
        params = daten.get("parameters")
        if comp and isinstance(params, dict):
            return {"component": comp, "parameters": params, "animation": "none"}
        if comp == "DataTable" and daten.get("columns") and daten.get("rows"):
            return {"component": "DataTable",
                    "parameters": {"spalten": list(daten["columns"]),
                                   "zeilen": [" | ".join(map(str, z)) for z in daten["rows"]]},
                    "animation": "none"}
        schritte = [s for s in (*asset.labels, asset.what_child_should_notice) if s.strip()]
        return {"component": "GenericStepFlow",
                "parameters": {"schritte": schritte[:8]} if schritte else {},
                "animation": "none"}
    return dict(_FALLBACK_VIS)


def _erklaerung(pkg: CompleteTopicPackage, mis_id: str) -> dict:
    m = next((x for x in pkg.misconception_model if x.misconception_id == mis_id), None)
    if m is None:
        raise BrueckeUnmoeglich(f"Fehlvorstellung {mis_id} fehlt im Modell")
    regel = next((e.text for e in pkg.explanations
                  if e.mode == "rule"), "") or m.repair_explanation
    haken = (m.likely_cause or m.description).strip()
    erkenntnis = (m.repair_explanation or m.resolution_evidence
                  or f"Was hier stimmt: {m.description}").strip()
    if not haken or not erkenntnis or not regel:
        raise BrueckeUnmoeglich(f"Fehlvorstellung {mis_id} ohne Erklaerung")
    bild = next((v for v in pkg.visual_assets
                 if v.visual_type in ("DETERMINISTIC_DIAGRAM", "DATA_CHART")), None)
    return {
        "haken": haken, "erkenntnis": erkenntnis, "regel": regel.strip(),
        "bild": {
            "zeigt": (bild.what_child_should_notice or bild.learning_goal
                      or "die Groessen und ihre Beziehung") if bild
                     else "die Groessen und ihre Beziehung",
            "bewegt": bild.interaction or "die Schritte der Erklaerung",
            "bleibt_gleich": (bild.fallback_text
                              or "die fachliche Beziehung in jeder Darstellung"),
        },
        "aufgabe": None,  # wird unten gefuellt
    }


def _hilfe(pkg: CompleteTopicPackage) -> dict:
    """Hilfetext je Phase — aus Erklaervarianten und Strategien, nie leer."""
    def text(*modi: str, strategie: str = "") -> str:
        for modus in modi:
            t = next((e.text for e in pkg.explanations if e.mode == modus), None)
            if t and t.strip():
                return t.strip()
        for stra in pkg.teaching_strategies.values():
            for s in stra:
                if strategie and strategie.lower() in s.lower():
                    return s.strip()
        return ""
    quelle = {
        "HOOK": text("intuitive", "analogy", strategie="hook")
                or "Schau dir die Aufgabe genau an – was faellt dir auf?",
        "RULE": text("rule") or "Merke dir die Regel und wende sie Schritt fuer Schritt an.",
        "WORKED_EXAMPLE": text("worked", "example")
                        or _schritte_text(pkg) or "Schau dir das Beispiel Schritt fuer Schritt an.",
        "GUIDED_TASK": text("example") or "Nutze den Hinweis und rechne einen Schritt nach dem anderen.",
        "INDEPENDENT_TASK": text("intuitive")
                            or "Du schaffst das – denk an die Regel aus dem Beispiel.",
        "ADAPTATION": text("misconception_specific", "visual")
                      or "Sieh dir dieselbe Sache einmal anders dargestellt an.",
    }
    return {phase: {"text": quelle[phase],
                    **({"visualisierung": _visual(pkg)}
                       if phase == "ADAPTATION" and pkg.visual_assets else {})}
            for phase in _HILFE_PHASEN}


def _schritte_text(pkg: CompleteTopicPackage) -> str:
    for t in pkg.tasks:
        if t.role == "WORKED" and t.worked_steps:
            return "Schau: " + " → ".join(t.worked_steps[:4])
    return ""


def _faq(pkg: CompleteTopicPackage) -> list[dict]:
    out: list[dict] = []
    seen = set()

    def add(frage: str, antwort: str):
        frage, antwort = frage.strip(), antwort.strip()
        if frage and antwort and frage.lower() not in seen:
            seen.add(frage.lower())
            out.append({"frage": frage, "antwort": antwort})

    for t in pkg.tasks:
        if t.role == "CLARIFICATION" and t.prompt and t.solution:
            add(f"Was mache ich, wenn „{t.prompt.strip().rstrip('?')}“ unklar ist?",
                t.solution)
        for klaerung in ([getattr(t.answer, "clarification", None)]
                         if t.answer is not None else []):
            if klaerung is not None and getattr(klaerung, "prompt", None):
                richtig = next((o.text for o in klaerung.options if o.correct), "")
                add(klaerung.prompt, richtig or t.solution)
    for m in pkg.misconception_model:
        if m.observable_answer_patterns and m.repair_explanation.strip():
            add(f"Warum ist „{m.observable_answer_patterns[0]}“ falsch?",
                m.repair_explanation)
        if m.resolution_evidence.strip():
            add(f"Woran erkenne ich, dass ich „{pkg.concept_identity.title}“ "
                f"sicher kann?", m.resolution_evidence)
    if len(out) < 2:
        # Letzte ehrliche Quelle: die Regel selbst plus Hilfsangebot.
        regel = next((e.text for e in pkg.explanations if e.mode == "rule"), "")
        if regel.strip():
            add(f"Was ist die Regel bei „{pkg.concept_identity.title}“?", regel)
    if len(out) < 2:
        raise BrueckeUnmoeglich("weniger als zwei FAQ-Eintraege moeglich")
    return out[:4]


def package_to_lesson(pkg: CompleteTopicPackage, *, thema: str = "",
                      einordnung: tuple[int, int] | None = None) -> dict:
    """Paket → Lektion im Format des Abnehmers (karo-adaptiv-v1).

    Wirft `BrueckeUnmoeglich`, wenn ein vertraglich geforderter Teil nicht
    belastbar aus dem Paket kommt — der Aufrufer entscheidet dann ueber
    den generierenden Rueckfall, still geliefert wird nichts.
    """
    if not pkg.misconception_model:
        raise BrueckeUnmoeglich("kein Fehlvorstellungs-Modell – der Vertrag ist fehlertyp-zentriert")
    von, bis = einordnung or (
        pkg.concept_identity.target_grade, pkg.concept_identity.target_grade)
    konzept = {
        "konzept_key": pkg.concept_identity.concept_id.lower(),
        "thema_key": re.sub(r"[^a-z0-9]+", "-", (thema or pkg.concept_identity.title).lower()).strip("-") or "thema",
        "label": pkg.concept_identity.title,
        "klasse_von": von,
        "klasse_bis": bis,
        "stichworte": [w for w in re.split(r"[\s,/]+", pkg.concept_identity.title) if len(w) > 3][:8],
    }
    fehlertypen = []
    for m in pkg.misconception_model:
        mis = m.misconception_id
        antworten = _fehlantworten(pkg, mis)
        if not antworten:
            raise BrueckeUnmoeglich(f"Fehlvorstellung {mis} ohne bekannte falsche Antworten")
        erklaerung = _erklaerung(pkg, mis)
        sonde = (_task_mit_id(pkg, m.disambiguating_probe)
                 or _task_fuer(pkg, ("MISCONCEPTION_PROBE", "GUIDED", "DIAGNOSTIC"), mis_id=mis))
        erklaerung["aufgabe"] = {"frage": sonde.prompt.strip() if sonde else "Was stimmt hier nicht?",
                                 "loesung": (sonde.solution if sonde else erklaerung["regel"]).strip()}
        fehlertypen.append({
            "key": mis.lower(), "label": m.description[:80],
            "beschreibung": m.description,
            "antworten": antworten[:8],
            "erklaerung": erklaerung,
            "visualisierung": _visual(pkg,
                (sonde.visual_ref if sonde else None)
                or next((v.asset_id for v in pkg.visual_assets
                         if v.visual_type in ("DETERMINISTIC_DIAGRAM", "DATA_CHART")), None)),
            "aufgaben": _rollen_aufgaben(pkg, m),
        })
    lesson = {"konzept": konzept, "fehlertypen": fehlertypen,
              "hilfe": _hilfe(pkg), "faq": _faq(pkg)}
    erst = _task_fuer(pkg, ("WORKED",))
    if erst is not None:
        lesson["erstkontakt"] = {
            "anker": (pkg.explanations[0].text[:300] if pkg.explanations
                      else pkg.concept_identity.description or konzept["label"]),
            "erste_aufgabe": {"frage": erst.prompt.strip(),
                              "loesung": (erst.solution or "").strip()},
            "benennung": konzept["label"],
        }
        if not lesson["erstkontakt"]["erste_aufgabe"]["loesung"]:
            lesson.pop("erstkontakt")
    return lesson


def find_package(db, concept_id: str) -> CompleteTopicPackage | None:
    """READY-Paket zum Konzept, wenn eines fertig gebaut ist."""
    row = db.one("""SELECT content FROM curriculum.complete_packages
                    WHERE concept_id=%s AND status='READY_COMPLETE'""", (concept_id,))
    if not row:
        return None
    return CompleteTopicPackage.model_validate(row["content"])
