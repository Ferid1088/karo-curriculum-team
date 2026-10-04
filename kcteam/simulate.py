"""Simulierte Kinder für die Diagnostik – zum Testen und zum Vorführen.

Profile:
  strong        beantwortet alles richtig
  weak          beantwortet alles falsch (Karo muss bis ganz nach unten gehen)
  misconception wählt, wo möglich, die typische falsche Antwort (Fehlvorstellung)
  gap:<ID,...>  kann alles außer den genannten Konzepten (und allem, was darauf aufbaut)
  skipper       überspringt jede Aufgabe
"""
from __future__ import annotations

import json
from typing import Any

from psycopg.types.json import Jsonb


def correct_answer(answer: dict) -> Any:
    t = answer["type"]
    if t in ("number", "fraction", "mark"):
        return answer["value"]
    if t == "text":
        return answer["accepted"][0]
    if t == "choice":
        idx = [i for i, o in enumerate(answer["options"]) if o.get("correct")]
        return idx if answer.get("multiple") else idx[0]
    if t == "order":
        return answer["items"]
    if t == "match":
        return answer["pairs"]
    if t == "concept_rubric":
        # Alle geforderten Konzepte nennen — optional dazu, wie ein gutes Kind.
        parts = [c if isinstance(c, str) else c["concept"]
                 for c in answer["required_concepts"]]
        parts += [c if isinstance(c, str) else c["concept"]
                  for c in answer.get("optional_concepts") or []]
        return " ".join(parts)
    return None


def wrong_answer(answer: dict) -> Any:
    t = answer["type"]
    if t == "choice":
        wrong = [i for i, o in enumerate(answer["options"]) if not o.get("correct")]
        return wrong[-1] if wrong else 99
    if t == "order":
        return list(reversed(answer["items"]))
    if t == "match":
        pairs = answer["pairs"]
        return [[pairs[i][0], pairs[(i + 1) % len(pairs)][1]] for i in range(len(pairs))]
    if t == "concept_rubric":
        # Ein schwaches Kind nennt ein Stueck Wahrheit — das ist 'partial',
        # nicht 'unknown'. Ohne Konzepte gar nichts Belastbares sagen.
        first = answer["required_concepts"][0]
        return first if isinstance(first, str) else first["concept"]
    return "999999"


def misconception_answer(item: dict) -> Any:
    for d in item.get("distractors") or []:
        if d.get("misconception"):
            return d["answer"]
    a = item["answer"]
    if a["type"] == "choice":
        for i, o in enumerate(a["options"]):
            if o.get("misconception"):
                return i
    if a["type"] == "concept_rubric":
        for m in a.get("misconceptions") or []:
            if m.get("patterns"):
                return m["patterns"][0]
    return wrong_answer(a)


def run_child(db, *, learner: str, subject_code: str, grade: int, targets: list[str], profile: str,
              known: list[str] | None = None, log=None) -> dict:
    gaps: set[str] = set()
    if profile.startswith("gap:"):
        gaps = {x.strip().upper() for x in profile[4:].split(",") if x.strip()}
        affected = set(gaps)
        for _ in range(20):  # alles, was (transitiv) auf einer Lücke aufbaut, kann das Kind auch nicht
            rows = db.query("SELECT concept_id FROM curriculum.concept_prerequisites WHERE prerequisite_id = ANY(%s)",
                            (list(affected),))
            new = {r["concept_id"] for r in rows} - affected
            if not new:
                break
            affected |= new
        gaps = affected
    sid = db.one("SELECT karo.start_diagnosis(%s,%s,%s,%s,%s) AS s",
                 (learner, subject_code, grade, targets, known or []))["s"]
    step = db.one("SELECT karo.next_step(%s) AS s", (sid,))["s"]
    asked = []
    while step["action"] == "ask":
        item_id = step["item"]["id"]
        item = db.one("SELECT * FROM curriculum.items WHERE id=%s", (item_id,))
        a = item["answer"]
        if profile == "skipper":
            given = None
        elif a["type"] == "free_text":
            given = {"text": "Meine Begründung"}
        elif profile == "strong" or (gaps and item["concept_id"] not in gaps):
            given = correct_answer(a)
        elif profile == "misconception":
            given = misconception_answer(item)
        else:
            given = wrong_answer(a)
        res = db.one("SELECT karo.record_response(%s,%s,%s) AS r", (sid, item_id, Jsonb(given)))["r"]
        asked.append({"item": item_id, "answer": given, "outcome": res["outcome"],
                      "misconception": res.get("misconception_id"), "state": res["concept_state"]})
        if log:
            log(f"  {item_id:40} {json.dumps(given, ensure_ascii=False)[:30]:32} → {res['outcome']:13} "
                f"({res['concept_state']})")
        step = res["next"]
    return {"session": str(sid), "asked": asked, "result": step}
