"""Mock-Provider: offline, ohne Kosten. Erzeugt gültige, deterministische Antworten.

Er simuliert auch die schwierigen Fälle, damit die Pipeline getestet werden kann:
- Kalibrierung von MA.BRUECHE.ADD_UNGL enthält beim ersten Versuch eine Marke -> Inspektor lehnt ab,
  nach der Rückmeldung ist sie weg.
- Diagnostik von MA.BRUECHE.ERWEITERN enthält immer eine Wette -> nach 3 Runden gesperrt (Mensch).
- Der Kritiker meldet in Runde 1 einen Niveaufehler bei MA.BRUECHE.ADD_UNGL.
- Curriculum-Agent: Thema passt zu einem Kandidatentitel -> existing; "Raumfahrt" -> out_of_scope; sonst new.
  Ein Thema mit "Wette" wird vom Inspektor gesperrt.
- Der Kalibrierer schreibt „Ankeraufgabe 1 zu <Titel>“ – bis ihn der Kopierschutz auf „wörtlich“ hinweist.
"""
from __future__ import annotations

import json
import re

from .base import Completion, Provider

MATH_BLOCKS = [
    {"id": "MA.ZAHLEN", "title": "Zahlen und Rechnen im Grundschulbereich",
     "description": "Zahlraum, Einmaleins, schriftliche Verfahren", "grade_min": 1, "grade_max": 4, "typical_grade": 2},
    {"id": "MA.TEILBARKEIT", "title": "Teilbarkeit", "description": "Teiler, Vielfache, ggT, kgV",
     "grade_min": 5, "grade_max": 6, "typical_grade": 5},
    {"id": "MA.BRUECHE", "title": "Brüche", "description": "Bruchbegriff bis Bruchrechnung",
     "grade_min": 3, "grade_max": 7, "typical_grade": 6, "varies": True,
     "variance_note": "Bruchrechnung je nach Land in Klasse 5 oder 6"},
    {"id": "MA.ALGEBRA", "title": "Terme und Gleichungen", "description": "Variablen, Terme, lineare Gleichungen",
     "grade_min": 7, "grade_max": 10, "typical_grade": 8},
]

MATH_CONCEPTS = {
    "MA.ZAHLEN": [
        ("MA.ZAHLEN.ZR100", "Zahlraum bis 100", 1, 2, []),
        ("MA.ZAHLEN.EINMALEINS", "Kleines Einmaleins", 2, 3, ["MA.ZAHLEN.ZR100"]),
    ],
    "MA.TEILBARKEIT": [
        ("MA.TEILBARKEIT.VIELFACHE", "Vielfache einer Zahl", 5, 5, ["MA.ZAHLEN.EINMALEINS"]),
        ("MA.TEILBARKEIT.KGV", "Kleinstes gemeinsames Vielfaches", 5, 6, ["MA.TEILBARKEIT.VIELFACHE"]),
    ],
    "MA.BRUECHE": [
        ("MA.BRUECHE.BEGRIFF", "Bruch als Teil eines Ganzen", 3, 5, ["MA.ZAHLEN.ZR100"]),
        ("MA.BRUECHE.GLEICHN_ADD", "Gleichnamige Brüche addieren", 5, 6, ["MA.BRUECHE.BEGRIFF"]),
        ("MA.BRUECHE.ERWEITERN", "Brüche erweitern und kürzen", 5, 6, ["MA.BRUECHE.BEGRIFF", "MA.ZAHLEN.EINMALEINS"]),
        ("MA.BRUECHE.ADD_UNGL", "Ungleichnamige Brüche addieren", 6, 6,
         ["MA.BRUECHE.GLEICHN_ADD", "MA.BRUECHE.ERWEITERN", "MA.TEILBARKEIT.KGV"]),
    ],
    "MA.ALGEBRA": [
        ("MA.ALGEBRA.TERME", "Terme mit einer Variablen", 7, 7, ["MA.ZAHLEN.EINMALEINS"]),
        ("MA.ALGEBRA.GLEICHUNGEN", "Lineare Gleichungen lösen", 7, 8, ["MA.ALGEBRA.TERME"]),
    ],
}


def _code_for(subject: str) -> str:
    s = subject.upper().replace("Ä", "AE").replace("Ö", "OE").replace("Ü", "UE")
    s = re.sub(r"[^A-Z]", "", s)
    return "MA" if s.startswith("MATH") else (s[:2] or "XX")


def _item(prompt: str, level: str, grade: int, solution: str = "siehe Lösungsweg", answer: dict | None = None,
          distractors: list | None = None) -> dict:
    d = {"prompt": prompt, "solution": solution, "level": level, "grade": grade, "representation": "symbolisch"}
    if answer:
        d["answer"] = answer
    if distractors:
        d["distractors"] = distractors
    return d


def _num(v) -> dict:
    return {"type": "number", "value": v}


class MockProvider(Provider):
    def __init__(self, settings: dict):
        super().__init__(name="mock", settings=settings)
        self.critic_calls = 0

    def complete(self, *, system, user, model, web_search=False, meta=None) -> Completion:
        meta = meta or {}
        role = meta.get("role")
        handler = getattr(self, f"_{role}")
        data = handler(meta)
        return Completion(text="```json\n" + json.dumps(data, ensure_ascii=False) + "\n```",
                          input_tokens=len(system + user) // 4, output_tokens=200, model="mock")

    # ---------------- Rollen ----------------
    def _curriculum_analyst(self, meta):
        subject = meta.get("subject", "Fach")
        g0, g1 = meta.get("grades", [1, 10])
        code = meta.get("payload", {}).get("fachkuerzel") or _code_for(subject)
        if code == "MA":
            blocks = [dict(b) for b in MATH_BLOCKS]
        else:
            blocks = [{"id": f"{code}.GRUNDLAGEN", "title": f"{subject}: Grundlagen", "description": "Einstieg",
                       "grade_min": g0, "grade_max": g1, "typical_grade": (g0 + g1) // 2}]
        # Klassenbereich lückenlos abdecken
        lo = min(b["grade_min"] for b in blocks)
        hi = max(b["grade_max"] for b in blocks)
        if g0 < lo:
            blocks[0]["grade_min"] = g0
        if g1 > hi:
            blocks[-1]["grade_max"] = g1
        for b in blocks:
            b["sources"] = [{"title": "KMK-Bildungsstandards (Mock)", "url": None}]
        return {"subject_code": code, "blocks": blocks}

    def _fachdidaktiker(self, meta):
        payload = meta.get("payload", {})
        if meta.get("meta_fix"):
            c = payload["konzept"]
            return {"id": c["id"], "title": c["title"], "description": c["description"] + " (überarbeitet)",
                    "first_contact_grade": c["first_contact_grade"], "target_grade": c["target_grade"],
                    "prerequisites": payload.get("prerequisites", []), "order": payload.get("order", 0)}
        block = meta["block"]
        bid = block["id"]
        spec = MATH_CONCEPTS.get(bid) or [
            (f"{bid}.EINSTIEG", f"{block['title']} – Einstieg", block["grade_min"], block["grade_min"], []),
            (f"{bid}.VERTIEFUNG", f"{block['title']} – Vertiefung", block["grade_min"], block["grade_max"],
             [f"{bid}.EINSTIEG"]),
        ]
        concepts = [{"id": cid, "title": t, "description": f"Das Kind kann: {t}.", "first_contact_grade": f,
                     "target_grade": tg, "prerequisites": pre, "order": i}
                    for i, (cid, t, f, tg, pre) in enumerate(spec, 1)]
        return {"block_id": bid, "concepts": concepts}

    def _niveau_kalibrierer(self, meta):
        c = meta["concept"]
        t = c["target_grade"]
        cid = c["id"]
        fb = meta.get("feedback") or ""
        anchor1 = f"Ankeraufgabe 1 zu {c['title']}"
        if "wörtlich" in fb:
            anchor1 = f"Eigene Ankeraufgabe zu {c['title']} mit neuen Zahlen"
        anchor2 = "Mia und Ole teilen sich eine Pizza."
        if cid == "MA.BRUECHE.ADD_UNGL" and "Inspektor" not in fb:
            anchor2 = "Mia kauft ein Glas Nutella und teilt es in Drittel."  # Marke -> Inspektor lehnt ab
        return {
            "concept_id": cid,
            "levels": {"below": f"Vorstufe zu {c['title']} mit Bildern", "target": f"{c['title']} sicher, Klasse {t}",
                       "above": f"{c['title']} mit Erweiterungen aus höheren Klassen"},
            "can_do": {"below": ["Das Kind erkennt die Grundidee am Bild."],
                       "target": [f"Das Kind kann {c['title'].lower()} ohne Hilfe."],
                       "above": ["Das Kind überträgt das Verfahren auf neue Zahlbereiche."]},
            "difficulty_parameters": {"max_zahlenraum": 100 * t, "max_rechenschritte": 2 + t // 4,
                                      "darstellung": ["bild", "symbolisch"]},
            "anchor_items": [_item(anchor1, "target", t),
                             _item(anchor2, "target", t)],
            "boundary_items": {
                "below": [_item("Einstiegsaufgabe mit Bild", "below", max(1, t - 2))],
                "within": [_item("Schwierigste erlaubte Aufgabe", "target", t)],
                "above": [_item("Aufgabe mit negativen Zahlen", "above", t + 1)],
            },
        }

    def _diagnostiker(self, meta):
        c = meta["concept"]
        t = c["target_grade"]
        cid = c["id"]
        ctx = "Wer die Wette verliert, zahlt 5 Euro." if cid == "MA.BRUECHE.ERWEITERN" else "Beim Kuchenbacken"
        choice = {"type": "choice", "options": [{"text": "ein Viertel", "correct": True},
                                                {"text": "ein Drittel", "misconception": "F1"}]}
        return {
            "concept_id": cid,
            "misconceptions": [
                {"key": "F1", "description": "Nenner werden addiert",
                 "diagnostic_item": _item("1/2 + 1/3 = ?", "target", t, "5/6 (typisch falsch: 2/5)",
                                          {"type": "fraction", "value": "5/6"},
                                          [{"answer": "2/5", "misconception": "F1", "feedback": "Nenner nicht addieren."}]),
                 "remediation_hint": "Nenner sagt, in wie viele Teile geteilt wird – er ändert sich beim Addieren nicht."},
            ],
            "diagnostic_items": [_item("Welcher Teil ist gefärbt?", "below", max(1, t - 2), "ein Viertel", choice),
                                 _item(f"{ctx}: Wie viel ist 3 · 4?", "target", t, "12", _num(12),
                                       [{"answer": "7", "misconception": "F1"}])],
            "exit_items": [_item("Abschlussaufgabe 1: 2 + 3", "target", t, "5", _num(5)),
                           _item("Abschlussaufgabe 2: 6 − 1", "target", t, "5", _num(5)),
                           _item("Abschlussaufgabe 3: 1/4 + 1/4", "target", t, "1/2", {"type": "fraction", "value": "1/2"})],
        }

    def _visual_didaktiker(self, meta):
        c = meta["concept"]
        t, cid = c["target_grade"], c["id"]
        alt = "Beispieldarstellung für " + c["title"]
        if cid == "MA.TEILBARKEIT.KGV":
            return {"concept_id": cid, "visual_need": "none", "rationale": "Wird über Vielfachen-Listen erklärt.",
                    "explanations": [], "visual_items": []}
        if cid.startswith("MA.BRUECHE"):
            # erster Versuch bei BEGRIFF mit unechtem Bruch -> Schema-Prüfung schlägt an, zweiter Versuch korrekt
            num = 5 if (cid == "MA.BRUECHE.BEGRIFF" and meta.get("attempt") == 0) else 3
            step1 = {"type": "fraction_bar", "alt": alt, "bars": [{"numerator": 1, "denominator": 2},
                                                                  {"numerator": 1, "denominator": 3}]}
            step2 = {"type": "fraction_bar", "alt": alt, "bars": [{"numerator": num, "denominator": 4 if num == 5 else 6},
                                                                  {"numerator": 2, "denominator": 6}]}
            item_visual = {"type": "number_line", "alt": "Zahlenstrahl von 0 bis 1 in Vierteln mit Fragezeichen",
                           "start": 0, "end": 1, "major_step": 1, "minor_divisions": 4, "label_format": "fraction",
                           "marks": [{"value": "3/4", "question": True}]}
            return {"concept_id": cid, "visual_need": "essential", "rationale": "Brüche brauchen Anschauung.",
                    "explanations": [{"key": "V1", "purpose": "Gleich große Teile herstellen", "level": "target",
                                      "for_misconception": "F1",
                                      "steps": [{"visual": step1, "caption": "Die Stücke sind verschieden groß."},
                                                {"visual": step2, "caption": "In Sechstel geteilt sind sie gleich groß."}]}],
                    "visual_items": [{"use": "exit", "prompt": "Welche Zahl steht beim Fragezeichen?", "solution": "3/4",
                                      "level": "target", "grade": t, "interaction": "input", "visual": item_visual,
                                      "answer": {"type": "fraction", "value": "3/4"},
                                      "distractors": [{"answer": "3/5", "misconception": "F1"}]}]}
        if cid.startswith("MA.ALGEBRA"):
            v = {"type": "balance_scale", "alt": "Waage: x + x + 3 links, 7 rechts, im Gleichgewicht",
                 "left": ["x", "x", "3"], "right": ["7"]}
            return {"concept_id": cid, "visual_need": "helpful", "rationale": "Waagemodell für Gleichungen.",
                    "explanations": [{"key": "V1", "purpose": "Gleichung als Waage", "steps": [
                        {"visual": v, "caption": "Links und rechts ist gleich viel."}]}],
                    "visual_items": []}
        v = {"type": "number_line", "alt": alt, "start": 0, "end": 20, "major_step": 5, "minor_divisions": 5,
             "jumps": [{"start": 0, "end": 5, "label": "+5"}, {"start": 5, "end": 10, "label": "+5"}]}
        return {"concept_id": cid, "visual_need": "helpful", "rationale": "Zahlenstrahl zur Orientierung.",
                "explanations": [{"key": "V1", "purpose": "Sprünge am Zahlenstrahl", "level": "target",
                                  "steps": [{"visual": v, "caption": "Wir springen in Fünferschritten."}]}],
                "visual_items": []}

    def _kritiker(self, meta):
        block = meta.get("block", {})
        if block.get("id") == "MA.BRUECHE" and meta.get("round") == 1:
            return {"issues": [{"concept_id": "MA.BRUECHE.ADD_UNGL", "type": "level_mismatch",
                                "description": "Grenzaufgabe 'within' ist zu leicht für die Obergrenze.",
                                "suggested_fix": "Gemischte Zahlen als schwierigste erlaubte Aufgabe verwenden.",
                                "route_to": "niveau_kalibrierer"}]}
        return {"issues": []}

    def _curriculum_agent(self, meta):
        p = meta.get("payload", {})
        a = p["anfrage"]
        topic, g, code = a["thema"], a["klasse"], a["fachkuerzel"]
        low = topic.lower()
        if "raumfahrt" in low:
            return {"decision": "out_of_scope", "reason": "Kein Schulstoff dieses Fachs."}
        words = [w for w in re.split(r"\W+", low) if len(w) > 4]
        for c in p.get("kandidaten", []):
            if any(w[:5] in c["title"].lower() for w in words):
                return {"decision": "existing", "reason": "Titel passt", "concept_ids": [c["id"]],
                        "search_terms": [topic]}
        blocks = [b for b in p.get("bloecke", []) if b["grade_min"] <= g <= b["grade_max"]]
        slug = re.sub(r"[^A-Z0-9]+", "_", topic.upper().replace("Ä", "AE").replace("Ö", "OE").replace("Ü", "UE")
                      .replace("ß", "SS")).strip("_")[:20]
        out = {"decision": "new", "reason": "Thema fehlt", "search_terms": [topic],
               "likely_next": [] if "zins" in low else ["Zinsen berechnen"]}
        if blocks:
            bid = blocks[0]["id"]
            out["block_id"] = bid
        else:
            bid = f"{code}.NEU"
            out["new_block"] = {"id": bid, "title": f"Neu: {topic}", "description": "vom Curriculum-Agenten angelegt",
                                "grade_min": max(1, g - 1), "grade_max": g, "typical_grade": g}
        pre = [c[0] for c in p.get("alle_konzepte", []) if c[2] < g and c[3] == "approved"][-1:]
        out["concepts"] = [{"id": f"{bid}.{slug}", "title": topic, "description": f"Das Kind kann: {topic}.",
                            "first_contact_grade": max(1, g - 1), "target_grade": g, "prerequisites": pre}]
        return out

    def _kinderrechts_inspektor(self, meta):
        content = json.dumps(meta.get("payload", {}).get("inhalt", {}), ensure_ascii=False)
        findings = []
        if "Nutella" in content:
            findings.append({"location": "anchor_items[1].prompt", "rule": "Werbung / Marken",
                             "severity": "block", "reason": "Echte Marke in einer Aufgabe.",
                             "requirement": "Marke durch neutrales Lebensmittel ersetzen."})
        if "Wette" in content:
            findings.append({"location": "diagnostic_items[1].prompt", "rule": "Glücksspiel / Wetten",
                             "severity": "block", "reason": "Wetten um Geld sind für Kinder ungeeignet.",
                             "requirement": "Kontext ohne Wette und ohne Geldeinsatz formulieren."})
        released = {o.get("rule") for o in meta.get("payload", {}).get("vom_menschen_freigegeben", [])}
        findings = [f for f in findings if f["rule"] not in released]
        return {"decision": "rejected" if findings else "approved", "findings": findings,
                "summary": "Mock-Prüfung"}
