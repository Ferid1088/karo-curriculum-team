"""Curriculum-Lückenbericht: der Graph prüft sich selbst.

`karo.diagnostic_readiness` beantwortet eine Frage: ist ein Konzept für die
Diagnose bereit? Dieser Bericht beantwortet die weiteren: hat jedes Konzept
seine Voraussetzungskette bis zu einem freigegebenen Level 0, seine
Kalibrierung, Fehlvorstellungen mit Abhilfe, Erklärvarianten, Aufgaben je
Rolle, auswertbare Antworttypen und eine Alternative, wenn die erste
Darstellung nicht trägt?

Er läuft über `curriculum.*`, nicht über die `karo.*`-Sichten: auch ein noch
nicht freigegebenes Konzept ist eine Lücke, wenn darauf gelernt werden soll —
und ein Kantenziel, das es gar nicht gibt, ist eine andere Lücke als eines,
das nur noch nicht fertig ist.

Aufruf: `curriculum_gap_report(db)` -> dict; `format_report` für die
Konsole; `GET /v1/gap-report` liefert denselben Bericht.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any

#: Rollen, die eine vollständige Lektion je Fehlvorstellung mitbringt
#: (Vertrag `karo-adaptiv-v1`).
_ROLLEN = ("vorhersage", "beispiel", "gefuehrt", "selbststaendig", "transfer")


#: Eine Lektion im Entwurfsstadium ist kein Graphbruch: der Export-Worker
#: baut sie bei der ersten Anfrage — dafuer ist `lesson_draft` nur die
#: kuratierte Abkuerzung. Der Befund bleibt im Bericht sichtbar, blockiert
#: `ok` aber nicht — er zaehlt als `pending_lessons`.
_PENDING_LEKTION = "keine verfasste Lektion (lesson_draft leer)"


def _lade(db) -> dict:
    konzepte = {c["id"]: c for c in db.query(
        "SELECT * FROM curriculum.concepts WHERE status <> 'retired'")}
    # Kanten stillgelegter Konzepte sind Archivdaten, keine Befunde.
    kanten = [k for k in db.query(
        "SELECT concept_id, prerequisite_id "
        "FROM curriculum.concept_prerequisites")
        if k["concept_id"] in konzepte]
    items: dict[str, list] = defaultdict(list)
    for i in db.query("SELECT id, concept_id, kind, level, answer, auto_checkable "
                      "FROM curriculum.items"):
        items[i["concept_id"]].append(i)
    mis: dict[str, list] = defaultdict(list)
    for m in db.query("SELECT id, concept_id, remediation_hint "
                      "FROM curriculum.misconceptions"):
        mis[m["concept_id"]].append(m)
    visual = {r["concept_id"] for r in db.query(
        "SELECT DISTINCT concept_id FROM curriculum.visual_explanations")}
    return {"konzepte": konzepte, "kanten": kanten, "items": items,
            "misconceptions": mis, "visual": visual}


def _zyklen(kanten: list[dict]) -> list[list[str]]:
    """Alle Kreise im Voraussetzungsgraphen (Knotenliste je Kreis)."""
    from .integrator import find_cycle
    graph = [(k["concept_id"], k["prerequisite_id"]) for k in kanten]
    gefunden, rest = [], list(graph)
    while True:
        kreis = find_cycle(rest)
        if not kreis:
            return gefunden
        gefunden.append(kreis)
        # Die Kanten des Kreises herausnehmen, damit derselbe Kreis nicht
        # zweimal gemeldet wird — andere Kreise bleiben auffindbar.
        paare = set(zip(kreis, kreis[1:]))
        rest = [e for e in rest if e not in paare]


def _level0_luecken(cid: str, kanten_von: dict, konzepte: dict,
                    besucht: set | None = None) -> bool:
    """True, wenn die Voraussetzungskette an keinem freigegebenen Fuß endet.

    Ein Konzept ohne Voraussetzungen ist selbst der Fuß (Level 0) — es muss
    nur freigegeben sein. Kette ohne solchen Fuß heißt: ein Kind kann nie
    tief genug absteigen.
    """
    besucht = besucht or set()
    if cid in besucht:
        return False                       # Kreis zählt nicht als Fuß
    besucht.add(cid)
    eintrag = konzepte.get(cid)
    if eintrag is None or eintrag["status"] != "approved":
        return True                        # hängt an fehlendem/unfertigem Ende
    vor = kanten_von.get(cid) or []
    if not vor:
        return False                       # freigegebener Fuß gefunden
    return all(_level0_luecken(v, kanten_von, konzepte, besucht) for v in vor)


def _lektion_luecken(draft: Any) -> list[str]:
    """Lücken innerhalb einer verfassten Lektion (karo-adaptiv-v1)."""
    if not isinstance(draft, dict):
        return [_PENDING_LEKTION]
    luecken = []
    fehlertypen = draft.get("fehlertypen")
    if not fehlertypen:
        return ["Lektion ohne Fehlvorstellungen"]
    for i, f in enumerate(fehlertypen, 1):
        wo = f"fehlertyp[{i}] ({f.get('key') or '?'})"
        if not (f.get("erklaerung") or {}).get("erkenntnis"):
            luecken.append(f"{wo}: Erklärung ohne Erkenntnis")
        if not f.get("visualisierung"):
            luecken.append(f"{wo}: keine Visualisierung")
        if not f.get("visualisierung_alternativ"):
            luecken.append(f"{wo}: keine alternative Darstellung")
        aufgaben = f.get("aufgaben") or {}
        fehlend = [r for r in _ROLLEN if not (aufgaben.get(r) or {}).get("frage")]
        if fehlend:
            luecken.append(f"{wo}: Aufgaben fehlen: {', '.join(fehlend)}")
        for rolle, a in aufgaben.items():
            if isinstance(a, dict) and a.get("antwort_art") == "begriffe" \
                    and not (a.get("rubrik") or {}).get("begriffe"):
                luecken.append(f"{wo}: {rolle} ist Begriffsaufgabe ohne Rubrik")
    return luecken


def _kalibrierung_luecken(c: dict, items: list[dict]) -> list[str]:
    luecken = []
    for feld in ("levels", "can_do", "difficulty_parameters"):
        if not c.get(feld):
            luecken.append(f"{feld} fehlt")
    cal = c.get("calibration") or {}
    for seite in ("below", "within", "above"):
        if not ((cal.get("boundary_items") or {}).get(seite)):
            luecken.append(f"boundary_items.{seite} fehlt")
    if sum(1 for i in items if i["kind"] == "anchor") < 2:
        luecken.append("weniger als 2 Ankeraufgaben")
    return luecken


def _diagnose_luecken(c: dict, items: list[dict], mis: list[dict]) -> list[str]:
    luecken = []
    if not mis:
        luecken.append("keine Fehlvorstellungen")
    elif any(not m.get("remediation_hint") for m in mis):
        luecken.append("Fehlvorstellung ohne Abhilfe")
    arten = defaultdict(int)
    for i in items:
        arten[i["kind"]] += 1
        if i["kind"] in ("diagnostic", "misconception", "exit") and not i["answer"]:
            luecken.append(f"{i['id']}: Aufgabe ohne auswertbare Antwort")
    if arten["diagnostic"] + arten["misconception"] < 2:
        luecken.append("weniger als 2 Diagnoseaufgaben")
    if arten["exit"] < 2:
        luecken.append("weniger als 2 Abschlussaufgaben")
    if c["target_grade"] > 1 and not any(
            i["kind"] == "diagnostic" and i["level"] == "below" for i in items):
        luecken.append("keine Einstiegs-Diagnoseaufgabe (below)")
    return luecken


def curriculum_gap_report(db) -> dict:
    """Der vollständige Lückenbericht über alle nicht-stillgelegten Konzepte."""
    daten = _lade(db)
    konzepte, kanten = daten["konzepte"], daten["kanten"]
    kanten_von: dict[str, list[str]] = defaultdict(list)
    for k in kanten:
        kanten_von[k["concept_id"]].append(k["prerequisite_id"])

    graph_luecken: list[dict] = []
    for kreis in _zyklen(kanten):
        graph_luecken.append({"type": "cycle", "detail": " → ".join(kreis)})
    for k in kanten:
        ziel = konzepte.get(k["prerequisite_id"])
        if ziel is None:
            graph_luecken.append({
                "type": "missing_prerequisite",
                "concept_id": k["concept_id"],
                "detail": f"{k['prerequisite_id']} existiert nicht"})
        elif (ziel["status"] != "approved"
              and konzepte[k["concept_id"]]["status"] == "approved"):
            # Eine Kante im unveroeffentlichten Bestand ist Backlog; nur
            # vom freigegebenen Konzept aus bricht sie den bedienten Pfad.
            graph_luecken.append({
                "type": "unapproved_prerequisite",
                "concept_id": k["concept_id"],
                "detail": f"{k['prerequisite_id']} ist {ziel['status']}"})

    konzept_luecken: list[dict] = []
    for cid, c in sorted(konzepte.items()):
        luecken = []
        if c["status"] != "approved":
            luecken.append(f"nicht freigegeben (status {c['status']})")
        luecken += _kalibrierung_luecken(c, daten["items"].get(cid, []))
        luecken += _diagnose_luecken(c, daten["items"].get(cid, []),
                                     daten["misconceptions"].get(cid, []))
        if cid not in daten["visual"] and not c.get("lesson_draft"):
            luecken.append("keine visuelle Erklärung")
        luecken += _lektion_luecken(c.get("lesson_draft"))
        if c["status"] == "approved" and kanten_von.get(cid):
            if _level0_luecken(cid, kanten_von, konzepte):
                luecken.append("kein freigegebenes Level 0 erreichbar")
        if luecken:
            konzept_luecken.append({
                "concept_id": cid, "subject_code": c["subject_code"],
                "status": c["status"], "title": c["title"], "gaps": luecken})

    subjects: dict[str, dict] = {}
    for c in konzepte.values():
        s = subjects.setdefault(c["subject_code"], {
            "concepts": 0, "approved": 0, "with_gaps": 0})
        s["concepts"] += 1
        s["approved"] += c["status"] == "approved"
    for k in konzept_luecken:
        subjects[k["subject_code"]]["with_gaps"] += 1

    # Drei ehrliche Signale statt einem „ok", das nicht sagt, was es meint:
    #
    #   serving_ok        — kann der Dienst ausliefern, was Kinder sehen?
    #                       Jeder freigegebene Graphpfad endet an einem
    #                       freigegebenen Fuß, jedes freigegebene Konzept
    #                       hat bedienbaren Inhalt.
    #   pipeline_complete — ist die Erzeugung durch, also auch das Backlog
    #                       leer und keine Lektion mehr ausstehend?
    #   ok                — Alias fuer serving_ok (Rueckwaertskompatibilitaet).
    #
    # Dazu gehoert die Klassifizierung des Ausstehenden:
    #   missing_serving_material  freigegeben, aber Inhalt bricht (Luecke
    #                             jenseits „Lektion noch nicht geschrieben")
    #   pending_optional_material freigegeben und nutzbar, Lektion baut der
    #                             Worker bei erster Anfrage
    #   backlog_material          noch nicht freigegeben (Pipeline laeuft)
    def _serving_bruch(g: dict) -> bool:
        if g["type"] == "cycle":
            return any((konzepte.get(i) or {}).get("status") == "approved"
                       for i in g["detail"].split(" → "))
        return (konzepte.get(g.get("concept_id") or "") or {}
                ).get("status") == "approved"

    ausstehend = [k for k in konzept_luecken
                  if k["status"] == "approved" and k["gaps"] == [_PENDING_LEKTION]]
    fehlend = [k for k in konzept_luecken
               if k["status"] == "approved" and k not in ausstehend]
    backlog = [k for k in konzept_luecken if k["status"] != "approved"]
    serving_luecken = [g for g in graph_luecken if _serving_bruch(g)]
    backlog_luecken = [g for g in graph_luecken if not _serving_bruch(g)]

    serving_ok = not fehlend and not serving_luecken
    return {
        "subjects": subjects,
        "graph_gaps": graph_luecken,
        "concept_gaps": konzept_luecken,
        "pending_lessons": [k["concept_id"] for k in ausstehend],
        "pending": {
            "missing_serving_material": [k["concept_id"] for k in fehlend],
            "pending_optional_material": [k["concept_id"] for k in ausstehend],
            "backlog_material": [k["concept_id"] for k in backlog],
            "backlog_graph_gaps": backlog_luecken,
        },
        "summary": {
            "concepts": len(konzepte),
            "approved": sum(1 for c in konzepte.values()
                            if c["status"] == "approved"),
            "backlog": sum(1 for c in konzepte.values()
                           if c["status"] != "approved"),
            "concepts_with_gaps": len(konzept_luecken),
            "pending_lessons": len(ausstehend),
            "graph_gaps": len(graph_luecken),
            "missing_serving_material": len(fehlend),
            "backlog_material": len(backlog),
            "serving_ok": serving_ok,
            "pipeline_complete": not konzept_luecken and not graph_luecken,
            "ok": serving_ok,
        },
    }


def format_report(report: dict) -> str:
    """Der Bericht als Konsole-Text: je Fach eine Zeile, dann die Lücken."""
    zeilen = ["curriculum_gap_report", "=" * 40]
    for code, s in sorted(report["subjects"].items()):
        zeilen.append(
            f"{code:6} {s['approved']:3}/{s['concepts']:3} freigegeben"
            + (f"  ✗ {s['with_gaps']} mit Lücken" if s["with_gaps"] else "  ✓"))
    if report["graph_gaps"]:
        zeilen.append("\nGraph:")
        for g in report["graph_gaps"]:
            zeilen.append(f"  ✗ [{g['type']}] "
                          f"{g.get('concept_id') or ''} {g['detail']}".rstrip())
    if report["concept_gaps"]:
        zeilen.append("\nKonzepte:")
        for k in report["concept_gaps"]:
            zeilen.append(f"  ✗ {k['concept_id']} ({k['status']})")
            for luecke in k["gaps"]:
                zeilen.append(f"      - {luecke}")
    s = report["summary"]
    zeilen.append(f"\nserving_ok:        {'✓' if s['serving_ok'] else '✗'}"
                  f"  ({s['missing_serving_material']} freigegebene mit Lücken, "
                  f"{len(report['pending']['backlog_graph_gaps'])} Backlog-Graphlücken)")
    zeilen.append(f"pipeline_complete: {'✓' if s['pipeline_complete'] else '✗'}"
                  f"  ({s['pending_lessons']} Lektionen ausstehend, "
                  f"{s['backlog_material']} Konzepte im Backlog)")
    return "\n".join(zeilen)
