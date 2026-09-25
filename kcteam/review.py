"""Menschliche Prüfung: Ein Mensch steht über dem Inspektor und kann gesperrte Inhalte
freigeben, endgültig verwerfen oder mit einem Hinweis neu bearbeiten lassen."""
from __future__ import annotations

import json

from .schemas import Calibration, ConceptGraph, CurriculumMap, Diagnostics, VisualSet


def list_open(db) -> list[dict]:
    return db.query("""SELECT id, kind, entity_type, entity_id, stage, reason, created_at, urgent
                       FROM curriculum.human_queue WHERE status='open' ORDER BY urgent DESC, created_at""")


def show(db, qid: int) -> dict | None:
    return db.one("SELECT * FROM curriculum.human_queue WHERE id=%s", (qid,))


def _resolve(db, qid: int, resolution: str, who: str) -> None:
    db.query("""UPDATE curriculum.human_queue SET status='resolved', resolution=%s, resolved_by=%s,
                resolved_at=now() WHERE id=%s""", (resolution, who, qid))


def approve(db, qid: int, note: str, who: str = "human") -> str:
    """Gibt den gesperrten Inhalt so frei, wie er ist (übersteuert das Veto)."""
    q = show(db, qid)
    if not q or q["status"] != "open":
        raise ValueError(f"Eintrag {qid} nicht offen")
    content = (q["payload"] or {}).get("content") or {}
    et, eid, stage = q["entity_type"], q["entity_id"], q["stage"]
    msg = "erledigt"
    if q["kind"] == "veto":
        if et == "concept" and stage == "calibration":
            db.save_calibration(eid, Calibration.model_validate({k: v for k, v in content.items() if k != "title"}))
            msg = f"{eid}: Kalibrierung übernommen – nächster Lauf macht mit der Diagnostik weiter"
        elif et == "concept" and stage == "diagnostics":
            db.save_diagnostics(eid, Diagnostics.model_validate({k: v for k, v in content.items() if k != "title"}))
            msg = f"{eid}: Diagnostik übernommen – nächster Lauf macht die Schlussprüfung"
        elif et == "concept" and stage == "visuals":
            row = db.concept(eid)
            db.save_visuals(eid, VisualSet.model_validate(q["payload"]["raw"]),
                            keep_status=bool(row and row["status"] == "approved"))
            msg = f"{eid}: Visuals übernommen"
        elif et == "concept" and stage == "final":
            db.set_concept_status(eid, "approved")
            msg = f"{eid}: freigegeben – für Karo sichtbar"
        elif et == "block" and stage == "graph":
            graph = ConceptGraph.model_validate(q["payload"]["raw"])
            code = db.one("SELECT subject_code FROM curriculum.topic_blocks WHERE id=%s", (eid,))["subject_code"]
            db.save_graph(code, eid, graph.concepts, keep_approved=True)
            db.set_block_status(eid, "structured")
            msg = f"{eid}: Struktur übernommen – nächster Lauf bearbeitet die Konzepte"
        elif et == "curriculum":
            cmap = CurriculumMap.model_validate(q["payload"]["raw"])
            subj = db.get_subject(eid)
            db.save_subject(cmap.subject_code, eid, (subj["grade_min"], subj["grade_max"]) if subj else
                            (min(b.grade_min for b in cmap.blocks), max(b.grade_max for b in cmap.blocks)), "mapped")
            for b in cmap.blocks:
                db.save_block(cmap.subject_code, b)
            msg = f"{eid}: Themenlandkarte übernommen"
        elif et == "request":
            from .ondemand import admin_approve_request, request_id
            msg = admin_approve_request(db, request_id(eid), (q["payload"] or {}).get("raw"),
                                        ((q["payload"] or {}).get("verdict") or {}).get("findings") or [], who, note)
        elif et == "export":
            from .lessons import admin_approve_export, export_id
            pl = q["payload"] or {}
            msg = admin_approve_export(db, export_id(eid), pl.get("raw") or pl.get("content"))
        elif stage in ("calibration", "diagnostics", "visuals", "final") and et != "concept":
            raise ValueError(f"Unerwarteter Eintrag {et}/{stage}")
    verdict = (q["payload"] or {}).get("verdict") or {}
    if et == "concept" and verdict.get("findings"):
        released = [{**f, "released_by": who, "note": note} for f in verdict["findings"]]
        db.query("""UPDATE curriculum.concepts SET human_overrides = human_overrides || %s::jsonb WHERE id=%s""",
                 (json.dumps(released, ensure_ascii=False), eid))
    db.log_review(None, et, eid, stage, "human", "override_approve", 1, verdict.get("findings", []), note)
    _resolve(db, qid, "approved: " + (note or ""), who)
    return msg


def reject(db, qid: int, note: str, who: str = "human") -> str:
    """Verwirft den gesperrten Inhalt. Hinweise (Fehler, fehlende Voraussetzung, Kritik) werden nur geschlossen."""
    q = show(db, qid)
    if not q or q["status"] != "open":
        raise ValueError(f"Eintrag {qid} nicht offen")
    et, eid, stage, kind = q["entity_type"], q["entity_id"], q["stage"], q["kind"]
    msg = f"{eid}: Hinweis geschlossen"
    if kind == "veto":
        row = db.concept(eid) if et == "concept" else None
        if et == "concept" and stage == "visuals" and row and row["status"] == "approved":
            # nachgerüstete Visuals verworfen: Konzept bleibt sichtbar, ohne Visuals, und wird nicht erneut versucht
            db.query("""UPDATE curriculum.concepts SET visuals='{"rejected_by_human": true}'::jsonb,
                        visual_need='none' WHERE id=%s""", (eid,))
            db.query("DELETE FROM curriculum.visual_explanations WHERE concept_id=%s", (eid,))
            db.query("DELETE FROM curriculum.items WHERE concept_id=%s AND visual IS NOT NULL", (eid,))
            msg = f"{eid}: Visuals verworfen, Konzept bleibt freigegeben"
        elif et == "concept":
            db.set_concept_status(eid, "retired")
            msg = f"{eid}: verworfen"
        elif et == "block":
            db.set_block_status(eid, "blocked")
            msg = f"{eid}: Block bleibt gesperrt"
        elif et == "curriculum":
            msg = f"{eid}: Themenlandkarte bleibt gesperrt"
    if et == "request":
        from .ondemand import request_id
        db.finish_request(request_id(eid), "rejected", reason_code="rejected_by_human", message=note or None)
        msg = f"{eid}: Auftrag abgelehnt"
    if et == "export":
        from .lessons import export_id, finish_export
        finish_export(db, export_id(eid), "blocked", reason_code="rejected_by_human", message=note or None)
        msg = f"{eid}: Lektion abgelehnt"
    db.log_review(None, et, eid, stage, "human", "override_reject", 1, [], note)
    _resolve(db, qid, "rejected: " + (note or ""), who)
    return msg


def retry(db, qid: int, note: str, who: str = "human") -> str:
    """Setzt den Inhalt zurück; der Hinweis geht beim nächsten Lauf als verbindliche Rückmeldung an das Team."""
    q = show(db, qid)
    if not q or q["status"] != "open":
        raise ValueError(f"Eintrag {qid} nicht offen")
    et, eid, stage = q["entity_type"], q["entity_id"], q["stage"]
    fb = f"Hinweis einer pädagogischen Fachkraft (verbindlich): {note}" if note else None
    hint = ""
    if et == "concept":
        row = db.concept(eid)
        if stage == "visuals" and row and row["status"] == "approved":
            # nachgerüstete Visuals: Konzept bleibt sichtbar, Visuals werden beim nächsten Lauf neu entworfen
            db.query("UPDATE curriculum.concepts SET visuals=NULL, pending_feedback=%s WHERE id=%s", (fb, eid))
        elif row and row["status"] == "approved" and q["kind"] == "missing_prerequisite":
            # fehlende Voraussetzung ist ein Strukturthema: Block neu strukturieren (freigegebene Konzepte bleiben)
            reason = f"{q['reason']}. " + (fb or "")
            db.query("UPDATE curriculum.topic_blocks SET status='pending', pending_feedback=%s WHERE id=%s",
                     (reason, row["block_id"]))
            hint = " (Block wird neu strukturiert, das Konzept bleibt sichtbar)"
        elif row and row["status"] == "approved":
            # Kritik/Nacharbeit an einem freigegebenen Konzept: im Hintergrund überarbeiten, bleibt sichtbar
            issues = (q["payload"] or {}).get("issues") or []
            per_role: dict = {}
            for i in issues:
                per_role.setdefault(i.get("route_to") or "all", []).append(
                    f"- [{i.get('type')}] {i.get('description')} → Vorschlag: {i.get('suggested_fix')}")
            fbd = {k: "Rückmeldung des Kritikers:\n" + "\n".join(v) for k, v in per_role.items()}
            fbd["all"] = "\n\n".join(x for x in [fb, None if issues else q["reason"]] if x) or q["reason"]
            fbd["rework"] = "1"
            db.query("UPDATE curriculum.concepts SET pending_feedback=%s WHERE id=%s",
                     (json.dumps(fbd, ensure_ascii=False), eid))
            hint = " (Überarbeitung im Hintergrund, das Konzept bleibt sichtbar)"
        else:
            back = {"calibration": "structured", "diagnostics": "calibrated", "visuals": "diagnosed",
                    "final": "visualized"}.get(stage, "structured")
            db.set_concept_status(eid, back, pending_feedback=fb)
        db.query("""UPDATE curriculum.topic_blocks SET status='reviewed'
                    WHERE id=(SELECT block_id FROM curriculum.concepts WHERE id=%s) AND status='done'""", (eid,))
    elif et == "block" and stage == "graph":
        db.query("UPDATE curriculum.topic_blocks SET status='pending', pending_feedback=%s WHERE id=%s", (fb, eid))
    elif et == "block":
        hint = " (Block wird beim nächsten Lauf erneut bearbeitet)"
        db.query("UPDATE curriculum.topic_blocks SET status='structured' WHERE id=%s AND status='blocked'", (eid,))
    elif et == "request":
        from .ondemand import request_id
        db.query("""UPDATE curriculum.topic_requests SET status='queued', human_note=%s, result_concepts='{}',
                      finished_at=NULL, reason_code=NULL, next_attempt_at=now(), priority=10, attempts=0,
                      updated_at=now() WHERE id=%s""", (note or None, request_id(eid)))
        db.query("SELECT pg_notify('kcteam_requests', %s)", (str(request_id(eid)),))
        hint = " (Auftrag neu eingereiht)"
    elif et == "export":
        from .lessons import export_id
        db.query("""UPDATE curriculum.lesson_exports SET status='queued', reason_code='retry', message=%s, attempts=0,
                      next_attempt_at=now(), finished_at=NULL, updated_at=now() WHERE id=%s""",
                 (note or None, export_id(eid)))
        hint = " (Lektion wird neu geschrieben)"
    elif et == "curriculum":
        db.query("UPDATE curriculum.subjects SET status='mapped' WHERE lower(name)=lower(%s)", (eid,))
        hint = " (Themenlandkarte: nächsten Lauf mit --refresh-map starten)"
    db.log_review(None, et, eid, stage, "human", "retry", 1, [], note)
    _resolve(db, qid, "retry: " + (note or ""), who)
    return f"{eid}: wird beim nächsten Lauf neu bearbeitet{hint}"


def export_subject(db, subject_code: str, out_dir: str) -> list[str]:
    """Schreibt pro Themenblock eine JSON-Datei mit allen freigegebenen Konzepten (für Karo als Datei-Alternative)."""
    from pathlib import Path
    out = Path(out_dir) / subject_code
    out.mkdir(parents=True, exist_ok=True)
    written = []
    blocks = db.query("SELECT * FROM karo.topic_blocks WHERE subject_code=%s ORDER BY grade_min, id", (subject_code,))
    for b in blocks:
        ids = [r["id"] for r in db.query("SELECT id FROM karo.concepts WHERE block_id=%s ORDER BY sort_order, id",
                                         (b["id"],))]
        concepts = [db.one("SELECT karo.concept_bundle(%s) AS b", (i,))["b"] for i in ids]
        data = {**{k: b[k] for k in b}, "concepts": concepts}
        path = out / f"{b['id']}.json"
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        written.append(str(path))
    return written
