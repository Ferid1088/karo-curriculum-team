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


_QUOTA = ("limit", "quota", "kontingent", "429", "rate-limit", "rate limit",
          "resets")
_AUTH = ("401", "403", "ungültig", "unauthorized", "schlüssel")
_TRANSPORT = ("timeout", "timed out", "connection", "verbindung", "502", "503",
              "504", "econnreset")
_PROMPT_GROSS = ("über der grenze", "ueber der grenze", "zeichen — über")
_INSPECTOR = ("blocked_by_inspector", "inspektor")


def _fehlerklasse(message: str | None) -> str:
    """Warum ein Export scheiterte — nur 'content' ist ein fachlicher Befund.

    Alles andere sind Betriebszustaende des Anbieters (Kontingent, Zugang,
    Uebertragung, zu grosse Vorgabe): sie gehoeren in ihr eigenes Bild,
    nicht in die Verwerfungsquote, mit der Materialqualitaet gemessen wird.
    """
    m = (message or "").lower()
    if any(t in m for t in _QUOTA):
        return "provider_quota"
    if any(t in m for t in _AUTH):
        return "provider_auth"
    if any(t in m for t in _PROMPT_GROSS):
        return "prompt_too_large"
    if any(t in m for t in _TRANSPORT):
        return "provider_transport"
    if "lektion passt nicht zum format" in m:
        return "contract_invalid"
    if any(t in m for t in _INSPECTOR):
        return "inspector_reject"
    if "format" in m or "verworfen" in m or "inspektor" in m or "prüfung" in m:
        return "content"
    # Unbekannt zaehlt vorsichtig als fachlich: lieber einmal zu viel
    # geprueft als ein Inhaltsproblem unter Betriebsrauschen versteckt.
    return "content"


#: Klassen, die etwas ueber die Materialqualitaet sagen — der Rest ist
#: Betrieb (Kontingent, Zugang, Uebertragung, Vorgabengroesse).
CONTENT_KLASSEN = frozenset({"content", "contract_invalid", "inspector_reject"})


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
            """SELECT status, message FROM curriculum.lesson_exports
                ORDER BY id DESC LIMIT 50""")
        if len(letzte) >= 10:
            verloren = sum(1 for r in letzte if r["status"] == "failed")
            inhaltlich = sum(1 for r in letzte
                             if r["status"] == "failed"
                             and _fehlerklasse(r.get("message")) in CONTENT_KLASSEN)
            quote = verloren / len(letzte)
            # Was zaehlt, ist die fachliche Verwerfungsquote — Kontingent-
            # und Zugangsfehler des Anbieters sind ein anderes Problem,
            # sie machen keine Lektion schlechter.
            if inhaltlich / len(letzte) > g["validation_reject_rate"]:
                fundstellen.append({
                    "art": "validation_rejects", "schwere": "warn",
                    "text": f"{inhaltlich}/{len(letzte)} der letzten Exporte "
                            f"fachlich verworfen "
                            f"({inhaltlich / len(letzte):.0%} > "
                            f"{g['validation_reject_rate']:.0%}; "
                            f"{verloren} mit allen Ursachen)"})
    except Exception:
        pass
    try:
        # Der Schaden vom 4.10.: ein zweiter Agent mit altem Code schrieb
        # gleichzeitig mit dem echten in dieselbe Queue. Zwei lebende
        # Instanzen desselben Dienstes in einer Umgebung sind kein
        # Feature — verschiedene Staende machen es zum Notfall.
        from . import version
        aktive = db.query(
            """SELECT instance, hostname, git_sha, environment,
                      extract(epoch FROM now() - last_seen)::int AS alter_s
                 FROM curriculum.service_heartbeat
                WHERE service='agent'
                  AND last_seen > now() - make_interval(secs => %s)""",
            (version.FRISCH_SEKUNDEN,))
        umgebungen: dict[str, list] = {}
        for r in aktive:
            umgebungen.setdefault(r["environment"], []).append(r)
        for umgebung, instanzen in umgebungen.items():
            if len(instanzen) < 2:
                continue
            wer = ", ".join(f"{i['instance']} ({i['git_sha'][:12]}, "
                            f"{i['alter_s']}s alt)" for i in instanzen)
            vermischt = len({i["git_sha"] for i in instanzen}) > 1
            fundstellen.append({
                "art": "multiple_active_curriculum_workers",
                "schwere": "hoch" if vermischt else "warn",
                "text": f"{len(instanzen)} Agenten aktiv in „{umgebung}“: {wer}"
                        + (" — verschiedene Staende, einer schreibt alt"
                           if vermischt else "")})
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
