"""Betriebsgrenzen auswerten — `kcteam doctor` zeigt sie, siehe config.yaml.

Jeder Befund ist ein Hinweis an einen Menschen, kein Automatismus: nichts
hier sperrt Material oder schaltet den Dienst ab. Wer mischt sich selbst
die Schwellen, verliert sie beim naechsten Update — sie stehen in der
Konfiguration, nicht im Code.
"""
from __future__ import annotations

DEFAULTS = {
    "stuck_session_minutes": 150,
    "pending_export_minutes": 120,
    "validation_reject_rate": 0.30,
    "missing_remote_rate": 0.20,
    "runtime_ai_calls": 0,
}


def grenzen(cfg) -> dict:
    """Schwellen aus der Konfiguration — fehlt ein Wert, gilt der
    eingebaute Standard (derselbe, der in config.yaml dokumentiert ist)."""
    roh = getattr(cfg, "alerts", None) or {}
    out = dict(DEFAULTS)
    for k in out:
        if isinstance(roh, dict) and roh.get(k) is not None:
            try:
                out[k] = float(roh[k])
            except (TypeError, ValueError):
                pass
    return out


def pruefen(db, cfg) -> list[dict]:
    """Befunde gegen die konfigurierten Grenzen. Wirft nie — ein kaputter
    Grenz-Check darf den Dienst nicht ausfallen lassen."""
    g = grenzen(cfg)
    fundstellen: list[dict] = []
    try:
        stuck = db.query(
            """SELECT call_key, session_id, role, entity_id,
                      EXTRACT(EPOCH FROM (now()-updated_at))/60 AS minuten
                 FROM curriculum.provider_sessions
                WHERE status='working'
                  AND updated_at < now() - make_interval(mins => %s)""",
            (int(g["stuck_session_minutes"]),))
        for s in stuck:
            fundstellen.append({
                "art": "stuck_session", "schwere": "warn",
                "text": f"provider_session {s['session_id']} "
                        f"({s['role']}, {s['entity_id']}) seit "
                        f"{int(s['minuten'])} min ohne Fortschritt"})
    except Exception:
        pass
    try:
        pending = db.query(
            """SELECT count(*) AS n FROM curriculum.lesson_exports
                WHERE status IN ('waiting','queued','running')
                  AND created_at < now() - make_interval(mins => %s)""",
            (int(g["pending_export_minutes"]),))
        n = (pending or [{}])[0].get("n") or 0
        if n:
            fundstellen.append({
                "art": "pending_exports", "schwere": "warn",
                "text": f"{n} Exporte warten laenger als "
                        f"{int(g['pending_export_minutes'])} min"})
    except Exception:
        pass
    try:
        letzte = db.query(
            """SELECT status FROM curriculum.lesson_exports
                ORDER BY id DESC LIMIT 50""")
        if len(letzte) >= 10:
            verloren = sum(1 for r in letzte if r["status"] == "failed")
            quote = verloren / len(letzte)
            if quote > g["validation_reject_rate"]:
                fundstellen.append({
                    "art": "validation_rejects", "schwere": "warn",
                    "text": f"{verloren}/{len(letzte)} der letzten Exporte "
                            f"verworfen ({quote:.0%} > "
                            f"{g['validation_reject_rate']:.0%})"})
    except Exception:
        pass
    try:
        sessions = db.query(
            """SELECT missing_remote FROM curriculum.provider_sessions
                ORDER BY created_at DESC LIMIT 50""")
        if len(sessions) >= 10:
            verloren = sum(1 for r in sessions if (r["missing_remote"] or 0) > 0)
            quote = verloren / len(sessions)
            if quote > g["missing_remote_rate"]:
                fundstellen.append({
                    "art": "missing_remote", "schwere": "warn",
                    "text": f"{verloren}/{len(sessions)} der letzten Sessions "
                            f"remote verloren ({quote:.0%} > "
                            f"{g['missing_remote_rate']:.0%})"})
    except Exception:
        pass
    return fundstellen
