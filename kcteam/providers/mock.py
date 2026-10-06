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


def _fill(schema: dict, defs: dict | None = None, _tiefe: int = 0):
    """Minimales Objekt, das ein JSON-Schema erfüllt.

    Löst $ref gegen $defs auf und wählt bei anyOf/oneOf den ersten Zweig –
    das reicht für Pydantic-generierte Schemas der Fabrikrollen."""
    if _tiefe > 12:
        return "Beispieltext"
    defs = defs if defs is not None else schema.get("$defs", {})
    if "$ref" in schema:
        name = schema["$ref"].rsplit("/", 1)[-1]
        return _fill(defs.get(name, {"type": "string"}), defs, _tiefe + 1)
    for kombi in ("anyOf", "oneOf", "allOf"):
        if kombi in schema:
            for zweig in schema[kombi]:
                wert = _fill(zweig, defs, _tiefe + 1)
                if wert is not None or zweig.get("type") != "null":
                    return wert
            return None
    if "const" in schema:
        return schema["const"]
    if "enum" in schema:
        return schema["enum"][0]
    t = schema.get("type")
    if isinstance(t, list):
        t = next((x for x in t if x != "null"), t[0])
    if t == "object" or "properties" in schema:
        props = schema.get("properties") or {}
        out = {}
        for k in schema.get("required", list(props)):
            v = _fill(props.get(k, {"type": "string"}), defs, _tiefe + 1)
            if v is not None:
                out[k] = v
        return out
    if t == "array":
        n = schema.get("minItems", 1)
        return [_fill(schema.get("items") or {"type": "string"}, defs, _tiefe + 1) for _ in range(min(n, 2))]
    if t == "integer":
        return schema.get("minimum", 1)
    if t == "number":
        return schema.get("minimum", 1)
    if t == "boolean":
        return True
    if t == "null":
        return None
    return "Beispieltext"


class MockProvider(Provider):
    def __init__(self, settings: dict):
        super().__init__(name="mock", settings=settings)
        self.critic_calls = 0

    def complete(self, *, system, user, model, web_search=False, meta=None) -> Completion:
        meta = meta or {}
        role = meta.get("role")
        handler = getattr(self, f"_{role}", self._generic)
        data = handler(meta)
        return Completion(text="```json\n" + json.dumps(data, ensure_ascii=False) + "\n```",
                          input_tokens=len(system + user) // 4, output_tokens=200, model="mock")

    def _generic(self, meta):
        """Rueckfall fuer Rollen ohne eigenen Mock: das mitgeschickte
        JSON-Schema minimal erfuellen. Fuer Pipeline- und Fabrikrollen, deren
        Verhalten die e2e-Ueberpruefung nicht nachspielt."""
        schema = meta.get("json_schema") or {}
        if schema:
            return _fill(schema)
        return {"ok": True}

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
        topic, code = a["thema"], a["fachkuerzel"]
        # Synthetic on-demand examples have a fixed curriculum level. The
        # production matcher intentionally receives no requesting child's class.
        g = 7
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

    def _lektionsautor(self, meta):
        """Lektion im Format des Abnehmers. Für das Karo-Format eine vollständige, nachgerechnete Lektion,
        sonst ein Objekt, das das mitgeschickte Schema minimal erfüllt. „Wette“ im Thema -> Inspektor lehnt ab."""
        p = meta.get("payload", {})
        schema = meta.get("json_schema") or {}
        fb = meta.get("feedback") or ""
        # Kuratiertes Konzept (Vertrag 1.5): die Lektion ist schon verfasst.
        # Der Mock liefert sie unveraendert — geprueft wird sie danach
        # genauso wie jede generierte Lektion.
        if isinstance(p.get("lektion_entwurf"), dict):
            return p["lektion_entwurf"]
        if "fehlertypen" not in (schema.get("properties") or {}):
            return _fill(schema)
        k = p["konzept"]
        topic = p.get("thema_des_abnehmers") or k["title"]
        wette = "Wette" in topic and ("immer" in topic or "Glücksspiel" not in fb)
        reg = {r["component"] for r in meta.get("registry") or []}
        comp = "GenericStepFlow" if "GenericStepFlow" in reg or not reg else sorted(reg)[0]
        vis = {"component": comp, "parameters": {"schritte": ["Schau genau hin", "Rechne Schritt für Schritt"]}
               if comp == "GenericStepFlow" else {}, "animation": "none"}
        mis = p.get("fehlvorstellungen") or []
        mis = (mis + [{"key": "verwechselt", "beschreibung": "verwechselt die Rechenart",
                       "bekannte_falsche_antworten": ["1"]}] * 2)[:max(2, min(4, len(mis)))]

        def aufgabe(a, b, rolle, extra=""):
            d = {"frage": f"Was ist {a} + {b}?{extra}", "loesung": str(a + b), "tipps": ["Zähle weiter."]}
            if rolle in ("gefuehrt", "selbststaendig"):
                d["typischer_fehler"] = str(a * b if a * b != a + b else a + b + 1)
            if rolle in ("vorhersage", "transfer"):
                d["optionen"] = [str(a + b), str(a + b + 1)]
                d["aufloesung"] = f"{a} + {b} = {a + b}, weil man {b} weiterzählt."
            return d

        fehler = []
        for i, m in enumerate(mis):
            fehler.append({
                "key": re.sub(r"[^a-z0-9]+", "-", str(m["key"]).lower()).strip("-") + (f"-{i}" if i else ""),
                "label": str(m["beschreibung"])[:60] or "Fehlvorstellung",
                "beschreibung": str(m["beschreibung"]),
                "antworten": [str(a) for a in (m.get("bekannte_falsche_antworten") or ["1"])][:6] or ["1"],
                "erklaerung": {"haken": "Viele Kinder rechnen hier zu schnell.",
                               "erkenntnis": "Beim Zusammenzählen wird die Menge größer und nie kleiner.",
                               "regel": "2 + 3 = 5",
                               "bild": {"zeigt": "zwei Gruppen von Punkten", "bewegt": "die Gruppen rücken zusammen",
                                        "bleibt_gleich": "die Anzahl aller Punkte bleibt immer gleich"},
                               "aufgabe": {"frage": "Was ist 2 + 2?", "loesung": "4"}},
                "visualisierung": vis,
                "aufgaben": {"vorhersage": aufgabe(1, 2, "vorhersage"),
                             "beispiel": aufgabe(2, 3, "beispiel"),
                             "gefuehrt": aufgabe(3, 4, "gefuehrt"),
                             "selbststaendig": aufgabe(4, 5, "selbststaendig"),
                             "transfer": aufgabe(5, 6, "transfer", " Stell dir 5 Äpfel und 6 Birnen vor." if not wette
                                                 else " Wette mit deinem Freund um Geld.")},
            })
        hilfe = {ph: {"text": f"Noch einmal anders erklärt ({ph.lower()}): Schritt für Schritt.", "visualisierung": vis}
                 for ph in ("HOOK", "RULE", "WORKED_EXAMPLE", "GUIDED_TASK", "INDEPENDENT_TASK", "ADAPTATION")}
        slug = re.sub(r"[^a-z0-9]+", "-", k["id"].lower()).strip("-")
        return {
            "konzept": {"konzept_key": slug, "thema_key": slug.split("-")[1] if "-" in slug else slug,
                        "label": k["title"], "klasse_von": k['first_contact_grade'],
                        "klasse_bis": k['target_grade'], "stichworte": [k["title"]]},
            "erstkontakt": {"anker": "Du hast 2 Stifte und bekommst 1 dazu. Wie viele hast du?",
                            "erste_aufgabe": {"frage": "Was ist 2 + 1?", "loesung": "3"},
                            "benennung": k["title"]},
            "fehlertypen": fehler,
            "hilfe": hilfe,
            "faq": [{"frage": "Warum wird es mehr?", "antwort": "Weil etwas dazukommt."},
                    {"frage": "Darf ich zählen?", "antwort": "Ja, Zählen hilft am Anfang."}],
        }

    # ---------------- Fabrik-Rollen (PART 58): ein stimmiges Minipaket,
    # damit der e2e-Schnitt offline laeuft. Inhaltlich ein Bruch-Thema;
    # das Thema kommt aus meta["payload"]["thema"].
    def _thema(self, meta):
        return (meta.get("payload") or {}).get("thema") or {"id": "T", "title": "Thema",
                                                            "subject_code": "MA", "grade": 6}

    def _katalog_rechercheur(self, meta):
        scope = (meta.get("payload") or {}).get("auswahl") or {}
        subs = scope.get("subjects") or ["MA"]
        grades = scope.get("grades") or [6]
        items = []
        for s in subs:
            for g in grades:
                items.append({
                    "id": f"DE.{s}.{g}.GRUNDLAGEN", "kind": "topic",
                    "title": f"{s} Klasse {g}: Grundlagen", "description": "Mock-Recherche",
                    "subject_code": s, "grade": g, "framework": scope.get("framework", "de-kmk"),
                    "country": "DE", "region": scope.get("region", ""),
                    "school_type": scope.get("school_type", ""), "path": [s, f"Klasse {g}"],
                    "sort_order": g, "origin": "agent_research",
                    "source": "KMK-Bildungsstandards (Mock)", "source_version": "2018",
                    "source_reference": "Abschnitt Grundwissen", "confidence": 0.6})
        return {"items": items, "summary": "Mock-Recherche ueber die Auswahl"}

    def _kompetenz_architekt(self, meta):
        t = self._thema(meta)
        return {"target_competencies": [f"Das Kind kann {t['title']} sicher anwenden."],
                "prerequisite_graph": {"edges": [
                    {"target_id": f"{t['subject_code']}.GRUNDLAGEN.ZAHLEN", "kind": "necessary",
                     "return_condition": "mastery", "resume_level": 0}]},
                "competency_ladder": [
                    {"level_id": 0, "goal": "Grundidee verstehen", "exit_criteria": "2x richtig",
                     "next_level": 1, "allowed_task_types": ["WORKED", "GUIDED", "INDEPENDENT"]},
                    {"level_id": 1, "goal": f"{t['title']} sicher", "next_level": None,
                     "allowed_task_types": ["GUIDED", "INDEPENDENT", "TRANSFER", "MASTERY_CHECK"]}],
                "teaching_strategies": {"0": ["WORKED", "GUIDED"], "1": ["INDEPENDENT", "TRANSFER"]}}

    def _fehlvorstellungs_analytiker(self, meta):
        return {"misconception_model": [
            {"misconception_id": "F1", "description": "ueblicher Anfaengerfehler zu diesem Thema",
             "likely_cause": "uebereiltes Rechnen", "repair_explanation": "Schritt fuer Schritt.",
             "observable_answer_patterns": ["2/5"],
             "guided_repair": ["G1"], "independent_check": ["I1"],
             "resolution_evidence": "I1 ohne Hilfe richtig"}]}

    def _didaktik_designer(self, meta):
        return {"teaching_strategies": {"0": ["WORKED", "GUIDED", "INDEPENDENT"],
                                        "1": ["GUIDED", "INDEPENDENT", "TRANSFER", "MASTERY_CHECK"]}}

    def _erklaerautor(self, meta):
        t = self._thema(meta)
        return {"explanations": [
            {"mode": "rule", "text": f"Regel zu {t['title']}: Schritt fuer Schritt."},
            {"mode": "intuitive", "text": "Stell dir eine Pizza vor, die du teilst."},
            {"mode": "example", "text": "Zum Beispiel: 2 + 3 = 5."},
            {"mode": "misconception_specific", "text": "F1 repariert: nicht einfach ueberall addieren.",
             "for_misconception": "F1"}]}

    def _visueller_lerndesigner(self, meta):
        return {"visual_need": "DETERMINISTIC_DIAGRAM",
                "visual_na_reason": "",
                "visual_assets": [
                    {"asset_id": "V1", "visual_type": "DETERMINISTIC_DIAGRAM",
                     "learning_goal": "Teile sehen",
                     "what_child_should_notice": "gleich grosse Teile",
                     "structured_data": {"type": "fraction_bar",
                                          "bars": [{"numerator": 1, "denominator": 2}]},
                     "accessibility_text": "Ein halb gefuellter Balken"},
                    {"asset_id": "IMG1", "visual_type": "ILLUSTRATIVE_IMAGE",
                     "learning_goal": "Motivation: Brueche im Alltag",
                     "accessibility_text": "Kuchen, der in gleiche Stuecke geteilt wird",
                     "fallback_text": "Stell dir einen Kuchen in vier Teilen vor."}]}

    def _aufgaben_designer(self, meta):
        def t(tid, role, lvl, prompt, value, **kw):
            d = {"task_id": tid, "role": role, "level_id": lvl, "prompt": prompt,
                 "answer": {"type": "fraction", "value": value}, "solution": value}
            d.update(kw)
            return d
        return {"tasks": [
            t("W1", "WORKED", 0, "Vorgefuehrt: 1/2 = ?/4", "2/4"),
            t("D1", "MISCONCEPTION_PROBE", 1, "Was ist 1/2 + 1/3?", "5/6",
              misconception="F1",
              distractors=[{"answer": "2/5", "misconception": "F1",
                            "feedback": "Zaehler und Nenner einzeln addiert."}]),
            t("G1", "GUIDED", 0, "1/3 = ?/6", "2/6", hints={"hints": ["Wie kommt 3 auf 6?"]}),
            t("I1", "INDEPENDENT", 0, "2/5 = ?/10", "4/10"),
            t("G2", "GUIDED", 1, "1/2 + 1/4", "3/4",
              hints={"hints": ["Hauptnenner?", "1/2 = 2/4"]},
              distractors=[{"answer": "2/6", "misconception": "F1"}]),
            t("I2", "INDEPENDENT", 1, "1/3 + 1/6", "1/2",
              distractors=[{"answer": "2/9", "misconception": "F1"}]),
            t("TR1", "TRANSFER", 1, "Ein halber Becher + ein Viertel Becher", "3/4"),
            t("M1", "MASTERY_CHECK", 1, "2/3 + 1/6", "5/6"),
            t("M2", "MASTERY_CHECK", 1, "1/5 + 3/10", "1/2"),
            t("P1", "PREREQUISITE_PROBE", 1, "1/4 + 1/4", "1/2",
              prerequisite_id="MA.GRUNDLAGEN.ZAHLEN"),
            t("R1", "SPACED_REVIEW", 1, "1/2 + 1/3", "5/6"),
            {"task_id": "E_FTK1", "role": "INDEPENDENT", "level_id": 1,
             "prompt": "Erklaere in eigenen Worten, warum 1/2 + 1/3 nicht 2/5 ist.",
             "answer": {"type": "free_text",
                        "rubric": [{"criterion": "Hauptnenner genannt", "points": 2}],
                        "pass_points": 1,
                        "sample_answer": "Man muss erst auf den Hauptnenner bringen."},
             "solution": "Nenner muessen gleich sein."}]}

    def _vorlagen_ingenieur(self, meta):
        return {"task_templates": [
            {"template_id": "T_GLEICHN", "role": "GUIDED", "level_id": 0,
             "prompt_template": "Erweitere 1/{a} auf Zwanzigstel?",
             "parameters": {"a": {"type": "int_range", "min": 2, "max": 10}},
             "constraints": ["20 % a == 0"],
             "solution": "fr(20 // a, 20)", "answer_type": "fraction",
             "max_variants": 4},
            {"template_id": "T_ADD", "role": "INDEPENDENT", "level_id": 1,
             "prompt_template": "Was ist 1/{a} + 1/{b}?",
             "parameters": {"a": {"type": "int_range", "min": 2, "max": 9},
                            "b": {"type": "int_range", "min": 2, "max": 9}},
             "constraints": ["a != b"], "solution": "fr(1, a) + fr(1, b)",
             "answer_type": "fraction",
             "distractor_exprs": {"F1": "fr(2, a+b)"}, "max_variants": 20}]}

    def _lernreise_architekt(self, meta):
        return {"rules": [], "detours": [],
                "role_order": ["WORKED", "GUIDED", "INDEPENDENT", "TRANSFER", "MASTERY_CHECK"],
                "max_attempts_per_task": 3, "max_unknown_per_task": 2, "max_no_progress": 14}

    def _fachexperte(self, meta):
        return {"verdict": "PASS", "issues": []}

    def _pruefungs_designer(self, meta):
        return {"tasks": [
            {"task_id": "E1", "role": "EXAM_PRACTICE", "level_id": 1,
             "prompt": "Klassenarbeit: 3/8 + 1/4", "answer": {"type": "fraction", "value": "5/8"},
             "solution": "5/8"}]}

    def _curriculum_kritiker(self, meta):
        return {"issues": []}

    def _vollstaendigkeits_kontrolleur(self, meta):
        payload = meta.get("payload", {})
        if "manifest" in payload:
            # Fabrik-Endkontrolle: PASS, wenn die Simulationen sauber sind.
            sims = payload.get("simulationen", {})
            bad = [p for p, o in sims.items() if o in ("DEAD_END", "STEP_LIMIT")]
            if bad:
                return {"verdict": "REPAIR_REQUIRED",
                        "findings": [{"component": "journey",
                                      "severity": "blocker",
                                      "detail": f"Simulationen fehlerhaft: {bad}",
                                      "stage": "journey"}]}
            return {"verdict": "PASS", "findings": []}
        return {"items": [], "coverage": {}}

    def _rubrik_ingenieur(self, meta):
        """Rubrik-Vertrag fuer Aufgaben ohne haerten Antworttyp: scannt die
        bisherigen Aufgaben und liefert Konzept-Rubriken."""
        payload = meta.get("payload", {})
        bp = payload.get("bisheriges_paket", {})
        tasks = list((bp.get("tasks") or {}).get("tasks", []))
        tasks += bp.get("tasks_retention") or []
        rubrics = []
        for t in tasks:
            ans = t.get("answer")
            if ans is None or ans.get("type") in ("free_text", "text",
                                                  "concept_rubric"):
                rubrics.append({
                    "task_id": t["task_id"],
                    "required_concepts": [
                        {"concept": "Hauptnenner",
                         "accepted": ["gleichnamig", "gemeinsamer Nenner"]}],
                    "optional_concepts": [],
                    "partial_min": 1,
                    "misconceptions": [{"patterns": ["oben plus oben"],
                                        "misconception": "F1"}],
                    "contradictions": [],
                    "unknown_markers": ["keine ahnung", "weiss nicht"],
                    "normalization": ["lower", "umlauts", "punctuation",
                                      "articles", "typo"],
                    "clarification": {
                        "prompt": "Was meinst du genau?",
                        "options": [
                            {"text": "Ich soll den Nenner erklaeren", "correct": True},
                            {"text": "Ich soll den Zaehler erklaeren",
                             "correct": False, "misconception": None}]}})
        return {"rubrics": rubrics}

    def _bildprompt_designer(self, meta):
        asset = (meta.get("payload", {}) or {}).get("asset", {})
        goal = asset.get("learning_goal", "Lernmotiv")
        return {"prompt": f"Kindgerechte sachliche Illustration, flacher "
                          f"Stil, ohne Text im Bild: {goal}",
                "negative_prompt": "Text, Marken, Gewalt, realistische Menschen",
                "alt_text": f"Illustration: {goal}"}

    def _visueller_inspektor(self, meta):
        return {"approved": True, "findings": [], "alt_text_ok": True}

    def _lernsimulator(self, meta):
        return {"profile": "AVERAGE", "event_sequence": [], "expected_outcome": "MASTERED",
                "observed_problem": ""}

    def _kinderrechts_inspektor(self, meta):
        payload = meta.get("payload", {})
        if "bisheriges_paket" in payload or "thema" in payload:
            # Fabrik-Kontext: Stufe 'inspector' erwartet IssuesOut (verdict/issues)
            content = json.dumps(payload, ensure_ascii=False)
            issues = []
            if "Nutella" in content or "Wette" in content:
                issues.append({"where": "tasks", "problem": "Marken/Gluecksspiel im Kontext",
                               "severity": "blocker", "fix_vorschlag": "neutralen Kontext waehlen"})
            return {"verdict": "FAIL" if issues else "PASS", "issues": issues}
        content = json.dumps(payload.get("inhalt", {}), ensure_ascii=False)
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
