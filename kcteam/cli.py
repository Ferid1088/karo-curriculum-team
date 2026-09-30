"""Kommandozeile.

  python -m kcteam run                  fragt nach Fach, Klassen, Provider und startet das Team
  python -m kcteam run --subject Mathematik --grades 3-8 --blocks Brüche --provider claude_token
  python -m kcteam status [--subject Mathematik]
  python -m kcteam review list | show ID | approve ID | reject ID | retry ID  [--note "..."]
  python -m kcteam review unblock-export MA.BRUECHE 5 karo-adaptiv-v1   Thema wieder freigeben
  python -m kcteam doctor                     laufen alle Dienste auf demselben Stand?
  python -m kcteam export --subject Mathematik
  python -m kcteam preview -b MA.BRUECHE   HTML-Vorschau der Visuals
  python -m kcteam catalog             HTML-Galerie aller Visual-Typen
  python -m kcteam subjects            alle Fächer mit eigenem Team
  python -m kcteam check -s Biologie   ist die Datenbank diagnostik-bereit?
  python -m kcteam simulate -c MA.BRUECHE.ADD_UNGL --child weak   Diagnostik vorführen
  python -m kcteam providers           zeigt, welche KI-Zugänge eingerichtet sind
  python -m kcteam init-db             legt Schema und Karo-Sichten an (passiert bei run automatisch)
  python -m kcteam serve               Curriculum-Agent: arbeitet Karos Aufträge ab (Dienst)
  python -m kcteam export-sqlite       Stufe 1: freigegebene Inhalte als SQLite-Datei für das Karo-MVP
  python -m kcteam request -s Mathematik -g 7 -t "Prozentrechnung" --task "20 % von 80 €"   wie Karo anfragen
  python -m kcteam requests            Aufträge und ihr Stand
  python -m kcteam demand              häufige Lücken (Nachfrage) – Grundlage für Vorab-Läufe
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import psycopg

from . import lessons, review, version
from .agents import BudgetExhausted, RateLimited
from .config import load_config, load_karo_spec
from .db import DB
from .pipeline import Pipeline, SubjectBlocked
from .providers import PROVIDERS, make_provider


def _ask(prompt: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default else ""
    try:
        val = input(f"{prompt}{suffix}: ").strip()
    except EOFError:
        val = ""
    return val or (default or "")


def _parse_grades(text: str) -> tuple[int, int]:
    text = text.replace("–", "-").replace(" ", "")
    if "-" in text:
        a, b = text.split("-", 1)
        g = (int(a), int(b))
    else:
        g = (int(text), int(text))
    if not (1 <= g[0] <= g[1] <= 13):
        raise ValueError("Klassen müssen zwischen 1 und 13 liegen, z. B. 3-8")
    return g


def _available_providers(cfg) -> list[tuple[str, str, bool, str]]:
    out = []
    for name, (_cls, label) in PROVIDERS.items():
        ok, msg = make_provider(name, cfg).available()
        out.append((name, label, ok, msg))
    return out


def cmd_providers(cfg, _args) -> int:
    print("KI-Zugänge:")
    for name, label, ok, msg in _available_providers(cfg):
        mark = "✓" if ok else "✗"
        default = "  (Standard)" if name == cfg.provider else ""
        print(f"  {mark} {name:14} {label}{default}  – {msg}")
    return 0


def cmd_init_db(cfg, _args) -> int:
    DB(cfg.database_url).migrate(force=True)
    print("✓ Schema curriculum + karo eingerichtet")
    return 0


def _choose_team(subject: str, interactive: bool, allow_unknown: bool):
    from .subjects import SubjectTeam, suggestions
    team = SubjectTeam.for_subject(subject)
    if team.generic:
        sug = suggestions(subject)
        hint = f" Meintest du: {', '.join(sug)}?" if sug else ""
        print(f"⚠ Für „{subject}“ gibt es kein eigenes Fachprofil.{hint}")
        if interactive:
            if sug and _ask(f"„{sug[0]}“ verwenden? (j/n)", "j").lower() in ("j", "ja", "y"):
                return SubjectTeam.for_subject(sug[0])
            if _ask("Mit allgemeinem Profil fortfahren? (j/n)", "n").lower() not in ("j", "ja", "y"):
                return None
        elif not allow_unknown:
            print("  Abbruch. Mit --allow-unknown trotzdem mit allgemeinem Profil starten.")
            return None
    return team


def cmd_run(cfg, args) -> int:
    subject = args.subject
    interactive = not subject
    if interactive:
        print("Karo Curriculum Team\n")
        subject = _ask("Für welches Fach soll das Team die Datenbank erstellen? (z. B. Mathematik, Biologie, Spanisch)")
        if not subject:
            print("Kein Fach angegeben.")
            return 1
    team = _choose_team(subject, interactive, args.allow_unknown)
    if team is None:
        return 1
    print(f"\nTeam {team.name} ({team.family.label}, typischerweise Klasse {team.grades[0]}–{team.grades[1]}):")
    for m in team.members():
        print(f"  • {m}")
    print(f"  {team.learning_year_hint()}")
    d0, d1 = team.grades
    grades = _parse_grades(args.grades or (
        _ask("\nWelche Klasse oder welcher Klassenbereich? (z. B. 6 oder 4-7)", f"{d0}-{d1}") if interactive else f"{d0}-{d1}"))
    problem = team.check_grades(grades)
    if problem:
        print(f"✗ {problem}")
        return 1
    subject = team.name
    blocks = args.blocks
    if interactive and blocks is None:
        b = _ask("Nur bestimmte Themenblöcke? (Stichworte mit Komma, leer = alle)", "")
        blocks = b or None
    block_filter = [x.strip() for x in blocks.split(",")] if blocks else None

    provider_name = args.provider
    if interactive and not provider_name:
        opts = _available_providers(cfg)
        print("\nKI-Zugang wählen:")
        for i, (name, label, ok, msg) in enumerate(opts, 1):
            print(f"  {i}) {name:14} {label}" + ("" if ok else f"   [nicht eingerichtet: {msg}]"))
        default_idx = next((str(i) for i, o in enumerate(opts, 1) if o[0] == cfg.provider), "1")
        while True:
            choice = _ask("Nummer", default_idx)
            if choice.isdigit() and 1 <= int(choice) <= len(opts):
                provider_name = opts[int(choice) - 1][0]
                break
            if choice in PROVIDERS:
                provider_name = choice
                break
            print(f"  Bitte eine Zahl von 1 bis {len(opts)} eingeben.")
    provider_name = provider_name or cfg.provider
    provider = make_provider(provider_name, cfg)
    ok, msg = provider.available()
    if not ok:
        print(f"✗ Provider {provider_name} nicht nutzbar: {msg}")
        return 1

    for w in DB.connection_warnings(cfg.database_url):
        print(f"⚠ {w}")
    db = DB(cfg.database_url)
    db.migrate()
    spec = load_karo_spec(cfg)
    klasse = f"Klasse {grades[0]}" if grades[0] == grades[1] else f"Klassen {grades[0]}–{grades[1]}"
    print(f"\n▶ Fach: {subject} | {klasse} | Blöcke: {', '.join(block_filter) if block_filter else 'alle'}"
          f" | Provider: {provider_name} | Karo-Spec: {'geladen (' + str(len(spec)) + ' Zeichen)' if spec else 'nicht gefunden'}")
    if interactive and _ask("Starten? (j/n)", "j").lower() not in ("j", "ja", "y", "yes"):
        return 0

    run_id = db.start_run(subject, grades, provider_name)
    pipe = Pipeline(cfg=cfg, provider=provider, db=db, run_id=run_id, karo_spec=spec, team=team)
    started = time.time()
    status, error = "finished", None
    try:
        # nie gleichzeitig mit dem Curriculum-Agenten am selben Fach arbeiten
        with db.subject_lock(team.code, on_wait=lambda: print("… wartet: Curriculum-Agent bearbeitet gerade "
                                                                 f"{subject}")):
            pipe.run(subject, grades, block_filter, refresh_map=args.refresh_map)
    except RateLimited as exc:
        status, error = "rate_limited", str(exc)
        print(f"\n⏸ {exc}\n  Später erneut starten – der nächste Lauf macht an derselben Stelle weiter.")
    except BudgetExhausted as exc:
        status, error = "budget_exhausted", str(exc)
        print(f"\n⏸ {exc}. Der nächste Lauf macht an derselben Stelle weiter.")
    except SubjectBlocked as exc:
        status, error = "failed", str(exc)
        print(f"\n⛔ {exc}")
    except KeyboardInterrupt:
        status, error = "failed", "abgebrochen"
        print("\n⏸ Abgebrochen – laufende KI-Aufrufe werden noch beendet, dann stoppt das Team.")
        pipe.shutdown()
        print("  Der nächste Lauf macht an derselben Stelle weiter.")
    except Exception as exc:  # noqa: BLE001
        status, error = "failed", repr(exc)
        pipe.shutdown()
        db.finish_run(run_id, status, {**pipe.stats, **db.run_stats(run_id)}, error)
        raise
    stats = {**pipe.stats, **db.run_stats(run_id), "seconds": int(time.time() - started)}
    db.finish_run(run_id, status, stats, error)
    from .sqlite_export import auto_export
    auto_export(cfg, db)            # Stufe 1: freigegebene Inhalte als SQLite in Karos Volume
    print("\nErgebnis: " + json.dumps(stats, ensure_ascii=False))
    open_items = review.list_open(db)
    if open_items:
        print(f"→ {len(open_items)} Einträge warten auf menschliche Prüfung: python -m kcteam review list")
    db.close_all()
    return 0 if status == "finished" else 2


def cmd_status(cfg, args) -> int:
    db = DB(cfg.database_url)
    db.migrate()
    subjects = db.query("SELECT * FROM curriculum.subjects ORDER BY name")
    if args.subject:
        subjects = [s for s in subjects if s["name"].lower() == args.subject.lower() or s["code"] == args.subject.upper()]
    if not subjects:
        print("Noch keine Fächer in der Datenbank.")
    for s in subjects:
        print(f"\n{s['name']} ({s['code']}), Klassen {s['grade_min']}–{s['grade_max']}")
        rows = db.query("""SELECT b.id, b.title, b.status,
                              count(c.*) FILTER (WHERE c.status='approved') AS approved,
                              count(c.*) FILTER (WHERE c.status='blocked')  AS blocked,
                              count(c.*) FILTER (WHERE c.status NOT IN ('approved','blocked','retired')) AS open
                           FROM curriculum.topic_blocks b LEFT JOIN curriculum.concepts c ON c.block_id=b.id
                           WHERE b.subject_code=%s GROUP BY b.id ORDER BY b.grade_min, b.id""", (s["code"],))
        for r in rows:
            print(f"  {r['id']:28} {r['status']:10} ✅ {r['approved']:3}  ⛔ {r['blocked']:3}  … {r['open']:3}  {r['title']}")
    last = db.one("SELECT * FROM curriculum.runs ORDER BY started_at DESC LIMIT 1")
    if last:
        print(f"\nLetzter Lauf: {last['started_at']:%Y-%m-%d %H:%M} {last['status']} {json.dumps(last['stats'], ensure_ascii=False)}")
    n = len(review.list_open(db))
    print(f"Offene menschliche Prüfungen: {n}")
    return 0


def _abnehmer_pruefbar() -> int:
    """0, wenn jeder eingetragene Abnehmer geprueft werden kann; sonst 1.

    Startsperre fuer API und Agent: ein Dienst, der Karos Pruefung nicht laden
    kann, liefert Karo nichts — und das faellt erst auf, wenn eine Familie
    wartet. Der Browser (`admin`) startet trotzdem: mit ihm sieht man nach,
    was los ist.
    """
    from . import consumers
    gruende = consumers.startsperre()
    for g in gruende:
        print(f"✗ {g}")
    if gruende:
        print("\nDer Dienst startet nicht, weil er fuer diese Abnehmer nichts ausliefern koennte.")
        print("Siehe README, Abschnitt „Vertrag ändern“.")
    return 1 if gruende else 0


def cmd_doctor(cfg, args) -> int:
    """Laufen alle Dienste, und alle aus demselben Bild?

    Der Anlass: die API lief auf dem neuen Image, Agent und Browser noch auf
    dem alten. Von aussen war das nicht zu sehen — die API meldete ihren Stand,
    die anderen gar nichts. Hinterher liess sich nicht sagen, welcher Code eine
    Lektion geschrieben hatte. Dieser Befehl bricht ab, statt es hinzunehmen.
    """
    db = DB(cfg.database_url)
    db.migrate()
    print(version.startmeldung("cli"))
    from . import consumers
    gesperrt = consumers.startsperre()
    for g in gesperrt:
        print(f"  ✗ {g}")
    ok, zeilen = version.befund(db, tuple(args.dienst) if args.dienst else ("api", "agent", "admin"))
    ok = ok and not gesperrt
    for z in zeilen:
        print("  " + z)
    if not ok:
        print("\n✗ Die Dienste laufen nicht auf demselben Stand.")
        print("  Neu bauen und hochfahren:  ./build.sh && docker compose "
              '--profile agent --profile admin up -d')
        return 1
    print("\n✓ Alle Dienste laufen auf demselben Stand.")
    return 0


def cmd_review(cfg, args) -> int:
    db = DB(cfg.database_url)
    db.migrate()
    if args.action == "unblock-export":
        if args.id is None or len(args.rest) != 2:
            print("Aufruf: kcteam review unblock-export <konzept> <klasse> <format>")
            return 1
        n = lessons.unblock_export(db, args.id.upper(), int(args.rest[0]), args.rest[1], args.client)
        print(f"{n} Verwerfung(en) aufgehoben." if n else "Keine Verwerfung gefunden – nichts zu tun.")
        return 0
    if args.action == "list":
        rows = review.list_open(db)
        if not rows:
            print("Keine offenen Einträge.")
        for r in rows:
            print(f"#{r['id']:<5} {r['kind']:22} {r['entity_type']:10} {r['entity_id']:30} {r['stage']:12} {r['reason']}")
        return 0
    if args.id is None:
        print("Bitte eine ID angeben.")
        return 1
    ident = int(args.id)
    if args.action == "show":
        q = review.show(db, ident)
        if not q:
            print("Nicht gefunden.")
            return 1
        payload = dict(q["payload"] or {})
        payload.pop("raw", None)
        print(json.dumps({**{k: v for k, v in q.items() if k != "payload"}, "payload": payload},
                         ensure_ascii=False, indent=2, default=str))
        return 0
    fn = {"approve": review.approve, "reject": review.reject, "retry": review.retry}[args.action]
    print(fn(db, ident, args.note or "", args.who))
    return 0


def cmd_export(cfg, args) -> int:
    db = DB(cfg.database_url)
    s = db.get_subject(args.subject) or db.one("SELECT * FROM curriculum.subjects WHERE code=%s", (args.subject.upper(),))
    if not s:
        print("Fach nicht gefunden.")
        return 1
    files = review.export_subject(db, s["code"], args.out or cfg.export_dir)
    print(f"✓ {len(files)} Dateien geschrieben nach {args.out or cfg.export_dir}/{s['code']}")
    return 0


def _html_page(title: str, body: str) -> str:
    return (f"<!doctype html><html lang='de'><head><meta charset='utf-8'><title>{title}</title>"
            "<style>body{font-family:Helvetica,Arial,sans-serif;background:#eef1f5;color:#1f2933;margin:24px}"
            "h1{font-size:22px}h2{font-size:18px;margin-top:32px}h3{font-size:15px;margin:18px 0 6px}"
            ".row{display:flex;flex-wrap:wrap;gap:14px;align-items:flex-start}"
            ".card{background:#fff;border-radius:12px;padding:10px;box-shadow:0 1px 3px #0002;max-width:540px}"
            ".cap{font-size:14px;margin:6px 4px 2px}.meta{color:#5b6472;font-size:13px}svg{max-width:100%;height:auto}"
            "</style></head><body>" + body + "</body></html>")


def cmd_catalog(cfg, args) -> int:
    from html import escape as esc
    from .visuals.examples import EXAMPLES
    from .visuals.render import render
    body = "<h1>Karo Visual-Katalog</h1><p class='meta'>Alle Darstellungstypen mit Beispiel. " \
           "Die JSON-Spec darunter ist das, was in der Datenbank liegt.</p><div class='row'>"
    for name, spec in EXAMPLES.items():
        import json as _j
        body += (f"<div class='card'><h3>{name}</h3>{render(spec)}"
                 f"<details><summary class='meta'>JSON</summary><pre style='font-size:11px;white-space:pre-wrap'>"
                 f"{esc(_j.dumps(spec, ensure_ascii=False, indent=1))}</pre></details></div>")
    out = Path(args.out or Path(cfg.export_dir) / "visual-katalog.html")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(_html_page("Karo Visual-Katalog", body + "</div>"), encoding="utf-8")
    print(f"✓ Katalog geschrieben: {out}")
    return 0


def cmd_preview(cfg, args) -> int:
    """HTML-Vorschau der Visuals eines Konzepts oder Blocks (so, wie Karo sie aus der DB bekommt)."""
    from html import escape as esc
    db = DB(cfg.database_url)
    if args.concept:
        concepts = [c for c in [db.concept(args.concept.upper())] if c]
    else:
        concepts = db.concepts(block_id=args.block.upper())
    if not concepts:
        print("Nichts gefunden.")
        return 1
    body = f"<h1>Visuals: {esc(args.concept or args.block)}</h1>"
    for c in concepts:
        body += (f"<h2>{esc(c['id'])} – {esc(c['title'])}</h2><p class='meta'>Zielklasse {c['target_grade']} · "
                 f"Status {c['status']} · visual_need: {c.get('visual_need') or '–'}</p>")
        for e in db.query("SELECT * FROM curriculum.visual_explanations WHERE concept_id=%s ORDER BY sort_order",
                          (c["id"],)):
            mis = f" · löst {e['misconception_id']}" if e["misconception_id"] else ""
            body += f"<h3>{esc(e['key'])}: {esc(e['purpose'])} <span class='meta'>({e['level']}{mis})</span></h3><div class='row'>"
            for i, st in enumerate(e["steps"], 1):
                body += f"<div class='card'>{st['svg']}<div class='cap'><b>{i}.</b> {esc(st['caption'])}</div></div>"
            body += "</div>"
        items = db.query("SELECT * FROM curriculum.items WHERE concept_id=%s AND visual IS NOT NULL ORDER BY sort_order",
                         (c["id"],))
        if items:
            body += "<h3>Visuelle Aufgaben</h3><div class='row'>"
            for it in items:
                body += (f"<div class='card'>{it['visual_svg']}<div class='cap'><b>{esc(it['prompt'])}</b></div>"
                         f"<div class='meta'>{it['kind']} · {it['level']} · Kl. {it['grade']} · {it['interaction']} · "
                         f"Lösung: {esc(it['solution'])}</div></div>")
            body += "</div>"
    out = Path(args.out or Path(cfg.export_dir) / "preview" / f"{(args.concept or args.block).upper()}.html")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(_html_page("Karo Vorschau", body), encoding="utf-8")
    print(f"✓ Vorschau geschrieben: {out}")
    return 0


def cmd_subjects(cfg, args) -> int:
    from .subjects import load_all
    subjects, families = load_all()
    by_fam: dict[str, list] = {}
    for s in subjects.values():
        by_fam.setdefault(s.family, []).append(s)
    for fam_id, items in by_fam.items():
        print(f"\n{families[fam_id].label}")
        for s in items:
            extra = f", Beginn Kl. {s.start_grade}" if s.start_grade else ""
            print(f"  {s.name:24} ({s.code}) Klasse {s.grades[0]}–{s.grades[1]}{extra}   auch: {', '.join(s.aliases[:4])}")
    print(f"\n{len(subjects)} Fächer. Andere Fächer laufen mit einem allgemeinen Profil (--allow-unknown).")
    return 0


def cmd_check(cfg, args) -> int:
    from .subjects import resolve
    db = DB(cfg.database_url)
    db.migrate()
    prof = resolve(args.subject)
    code = prof.code if prof else args.subject.upper()
    rows = db.query("SELECT * FROM karo.diagnostic_readiness WHERE subject_code=%s ORDER BY target_grade, concept_id",
                    (code,))
    if not rows:
        print("Keine freigegebenen Konzepte für dieses Fach.")
        return 1
    ready = [r for r in rows if r["ready"]]
    print(f"{len(ready)} von {len(rows)} freigegebenen Konzepten sind diagnostik-bereit.")
    for r in rows:
        if not r["ready"]:
            gaps = []
            if r["target_probes"] < 2:
                gaps.append(f"nur {r['target_probes']} auswertbare Ziel-Diagnoseaufgaben")
            if r["below_probes"] < 1 and r["target_grade"] > 1:
                gaps.append("keine Einstiegs-Diagnoseaufgabe")
            if r["exit_items"] < 2:
                gaps.append(f"nur {r['exit_items']} auswertbare Abschlussaufgaben")
            if r["missing_prerequisites"]:
                gaps.append(f"{r['missing_prerequisites']} Voraussetzung(en) fehlen/nicht freigegeben")
            print(f"  ✗ {r['concept_id']:36} Kl. {r['target_grade']:2}  " + "; ".join(gaps))
    blocked = db.query("SELECT id FROM curriculum.concepts WHERE subject_code=%s AND status NOT IN ('approved','retired')",
                       (code,))
    if blocked:
        print(f"Noch nicht freigegeben: {len(blocked)} Konzepte (status / review list).")
    return 0 if len(ready) == len(rows) else 2


def cmd_simulate(cfg, args) -> int:
    from .simulate import run_child
    db = DB(cfg.database_url)
    db.migrate()
    targets = [c.upper() for c in args.concept]
    first = db.concept(targets[0])
    if not first:
        print("Konzept nicht gefunden.")
        return 1
    print(f"Simuliertes Kind: {args.child} · Ziel: {', '.join(targets)}\n")
    out = run_child(db, learner=f"sim-{args.child}-{time.time_ns()}", subject_code=first["subject_code"],
                    grade=args.grade or first["target_grade"], targets=targets, profile=args.child, log=print)
    res = out["result"]
    print(f"\nErgebnis nach {len(out['asked'])} Aufgaben:")
    print(f"  Ziel erreicht: {'ja' if res['targets_mastered'] else 'nein'}")
    if res["plan"]:
        print("  Lernplan (in dieser Reihenfolge):")
        for p in res["plan"]:
            flags = " ← Einstieg" if p["entry_point"] else ""
            mis = f"  Fehlvorstellungen: {', '.join(p['misconceptions'])}" if p["misconceptions"] else ""
            print(f"    Kl. {p['target_grade']:2}  {p['concept_id']:36} {p['state']:13}{flags}{mis}")
    if res["enrichment"]:
        print("  Förderung nach oben: " + ", ".join(e["concept_id"] for e in res["enrichment"]))
    return 0


def cmd_serve(cfg, args) -> int:
    import signal
    from .ondemand import CurriculumAgent
    provider_name = args.provider or cfg.provider
    provider = make_provider(provider_name, cfg)
    ok, msg = provider.available()
    if not ok:
        print(f"✗ Provider {provider_name} nicht nutzbar: {msg}")
        return 1
    for w in DB.connection_warnings(cfg.database_url):
        print(f"⚠ {w}")
    db = DB(cfg.database_url)
    db.migrate()
    agent = CurriculumAgent(cfg=cfg, provider=provider, db=db, karo_spec=load_karo_spec(cfg))

    def stop(*_):
        print("\n⏸ Curriculum-Agent wird beendet (laufender Auftrag wird später fortgesetzt) …")
        agent.shutdown()
    signal.signal(signal.SIGTERM, stop)
    if _abnehmer_pruefbar():
        return 1
    print(version.startmeldung("agent"))
    version.puls(lambda: db, "agent")
    print(f"▶ Curriculum-Agent läuft (Provider {provider_name}). Wartet auf Aufträge von Karo …")
    try:
        agent.serve(once=args.once)
    except KeyboardInterrupt:
        stop()
    db.close_all()
    return 0


def cmd_export_sqlite(cfg, args) -> int:
    from .sqlite_export import export_sqlite
    from .subjects import resolve
    db = DB(cfg.database_url)
    db.migrate()
    out = args.out or (cfg.raw.get("sqlite_export") or {}).get("path") or str(Path(cfg.export_dir) / "curriculum.db")
    codes = None
    if args.subject:
        codes = []
        for s in args.subject:
            prof = resolve(s)
            codes.append(prof.code if prof else s.upper())
    counts = export_sqlite(db, out, codes)
    print(f"✓ {out}: " + ", ".join(f"{k}={v}" for k, v in counts.items()))
    print("  In Karo: ATTACH DATABASE '/data/curriculum.db' AS cur;")
    return 0


def cmd_request(cfg, args) -> int:
    db = DB(cfg.database_url)
    db.migrate()
    from psycopg.types.json import Jsonb
    res = db.one("SELECT karo.resolve_topic(%s,%s,%s,%s,%s,%s) AS r",
                 (args.subject, args.grade, args.topic, args.keyword or [], Jsonb(args.task or []), args.tenant))["r"]
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0


def cmd_requests(cfg, args) -> int:
    db = DB(cfg.database_url)
    db.migrate()
    rows = db.query("""SELECT id, status, stage, subject, grade, topic, requested_count, source, result_concepts,
                              reason_code, created_at FROM curriculum.topic_requests
                       ORDER BY id DESC LIMIT %s""", (args.limit,))
    if not rows:
        print("Keine Aufträge.")
    for r in rows:
        extra = ", ".join(r["result_concepts"]) or r["reason_code"] or r["stage"] or ""
        print(f"#{r['id']:<5} {r['status']:9} {r['subject']:14} Kl.{r['grade']:>2}  {r['topic'][:40]:40} "
              f"{r['requested_count']:>3}×  {r['source']:8} {extra}")
    return 0


def cmd_demand(cfg, args) -> int:
    db = DB(cfg.database_url)
    db.migrate()
    rows = db.query("""SELECT * FROM curriculum.demand_report WHERE last_seen > now() - make_interval(days => %s)
                       ORDER BY misses DESC, requests DESC LIMIT %s""", (args.days, args.limit))
    if not rows:
        print("Noch keine Anfragen.")
    print(f"{'Fach':10} {'Kl.':>3}  {'Thema':42} {'Anfragen':>8} {'Lücken':>7} {'anderes Niveau':>15}")
    for r in rows:
        print(f"{r['subject']:10} {r['grade']:>3}  {r['topic'][:42]:42} {r['requests']:>8} {r['misses']:>7} "
              f"{r['other_level']:>15}")
    return 0


def cmd_rerender(cfg, args) -> int:
    from .sqlite_export import auto_export
    db = DB(cfg.database_url)
    db.migrate()
    steps, items = db.rerender_visuals()
    print(f"✓ neu gezeichnet: {steps} Bildschritte, {items} visuelle Aufgaben")
    auto_export(cfg, db, print)
    return 0


def cmd_api(cfg, args) -> int:
    import uvicorn
    db = DB(cfg.database_url)
    db.migrate()
    db.close_all()
    if _abnehmer_pruefbar():
        return 1
    print(version.startmeldung("api"))
    print(f"▶ Curriculum-Service auf http://{args.host}:{args.port}  (Doku: /docs)")
    uvicorn.run("kcteam.api:create_app", factory=True, host=args.host, port=args.port, workers=args.workers,
                log_level="warning", access_log=False, proxy_headers=False)
    return 0


def cmd_api_client(cfg, args) -> int:
    from .api import add_client, list_clients, revoke_client
    db = DB(cfg.database_url)
    db.migrate()
    if args.action == "add":
        if not args.name:
            raise ValueError("--name fehlt")
        row, key = add_client(db, args.name, args.tenant or args.name, args.webhook, key=args.key)
        print(f"✓ Abnehmer {row['name']} (Einrichtung {row['tenant']}) angelegt.")
        print(f"  Schlüssel (wird nur jetzt angezeigt):  {key}")
        if row["webhook_secret"]:
            print(f"  Webhook-Geheimnis:                   {row['webhook_secret']}")
        print("  In Karo: KARO_CURRICULUM_KEY=<Schlüssel> setzen.")
    elif args.action == "revoke":
        print("✓ gesperrt" if revoke_client(db, args.name) else "✗ nicht gefunden")
    else:
        for r in list_clients(db):
            print(f"#{r['id']:<3} {r['name']:20} {r['tenant']:16} {r['key_prefix']}…  "
                  f"{'aktiv' if r['active'] else 'gesperrt':8} zuletzt: {r['last_used_at'] or '–'}")
    return 0


def cmd_admin(cfg, args) -> int:
    import uvicorn
    if not os.environ.get("ADMIN_PASSWORD"):
        print("✗ ADMIN_PASSWORD ist nicht gesetzt – die Oberfläche startet nicht ohne Passwort.")
        return 1
    print(version.startmeldung("admin"))
    print(f"▶ Datenbank-Browser auf http://{args.host}:{args.port}  (Benutzer: admin)")
    uvicorn.run("kcteam.admin:create_app", factory=True, host=args.host, port=args.port, log_level="warning")
    return 0


def cmd_bench(cfg, args) -> int:
    """Latenz der Sofortsuche messen (so, wie Karo den Dienst aufruft)."""
    import statistics
    from concurrent.futures import ThreadPoolExecutor

    import httpx
    topics = args.topic or ["Brüche addieren", "Ungleichnamige Brüche", "Einmaleins", "Terme", "Prozentrechnung"]
    headers = {"Authorization": f"Bearer {args.key or os.environ.get('KARO_CURRICULUM_KEY', '')}"}
    with httpx.Client(base_url=args.url, headers=headers, timeout=10) as http:
        def one(i):
            t = time.perf_counter()
            r = http.post("/v1/resolve", json={"subject": args.subject, "grade": args.grade,
                                                "topic": topics[i % len(topics)]})
            return (time.perf_counter() - t) * 1000, r.status_code
        one(0)
        t0 = time.perf_counter()
        with ThreadPoolExecutor(args.concurrency) as ex:
            res = list(ex.map(one, range(args.n)))
        wall = time.perf_counter() - t0
    ms = sorted(r[0] for r in res)
    errors = sum(1 for r in res if r[1] >= 400)
    p = lambda q: ms[min(len(ms) - 1, int(q * len(ms)))]  # noqa: E731
    print(f"{args.n} Anfragen, {args.concurrency} parallel: p50 {statistics.median(ms):.1f} ms · p95 {p(0.95):.1f} ms · "
          f"p99 {p(0.99):.1f} ms · {args.n / wall:.0f}/s · Fehler {errors}")
    return 0 if p(0.95) < args.target and not errors else 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="kcteam", description="Karo Curriculum Team")
    parser.add_argument("--config", help="Pfad zu config.yaml")
    sub = parser.add_subparsers(dest="cmd")

    p = sub.add_parser("run", help="Team für ein Fach laufen lassen")
    p.add_argument("--subject", "-s", help="Fach, z. B. Mathematik")
    p.add_argument("--grades", "-g", help="eine Klasse (z. B. 6) oder ein Bereich (z. B. 4-7)")
    p.add_argument("--blocks", "-b", help="nur Themenblöcke mit diesen Stichworten (Komma)")
    p.add_argument("--provider", "-p", choices=list(PROVIDERS), help="KI-Zugang")
    p.add_argument("--refresh-map", action="store_true", help="Themenlandkarte neu erstellen")
    p.add_argument("--allow-unknown", action="store_true", help="auch Fächer ohne eigenes Profil zulassen")

    sub.add_parser("subjects", help="alle Fächer mit eigenem Team anzeigen")

    p = sub.add_parser("check", help="Ist die Datenbank für die Diagnostik bereit?")
    p.add_argument("--subject", "-s", required=True)

    p = sub.add_parser("simulate", help="Diagnostik mit einem simulierten Kind vorführen")
    p.add_argument("--concept", "-c", required=True, action="append", help="Zielkonzept (mehrfach möglich)")
    p.add_argument("--child", default="weak", help="strong | weak | misconception | skipper | gap:ID,ID")
    p.add_argument("--grade", type=int, default=None)

    p = sub.add_parser("status", help="Stand der Datenbank")
    p.add_argument("--subject", "-s")

    p = sub.add_parser("doctor", help="laufen alle Dienste auf demselben Stand?")
    p.add_argument("--dienst", "-d", action="append",
                   help="nur diese Dienste prüfen (mehrfach angebbar)")

    p = sub.add_parser("review", help="menschliche Prüfung")
    p.add_argument("action", choices=["list", "show", "approve", "reject", "retry", "unblock-export"])
    p.add_argument("id", nargs="?", help="ID des Eintrags – bei unblock-export: das Konzept")
    p.add_argument("rest", nargs="*", help="bei unblock-export: Klasse und Format")
    p.add_argument("--client", help="nur die Verwerfungen dieses Abnehmers aufheben")
    p.add_argument("--note", "-n", help="Begründung / Hinweis ans Team")
    p.add_argument("--who", default="human", help="Name der prüfenden Person")

    p = sub.add_parser("export", help="freigegebene Inhalte als JSON-Dateien")
    p.add_argument("--subject", "-s", required=True)
    p.add_argument("--out", "-o")

    p = sub.add_parser("preview", help="HTML-Vorschau der Visuals eines Konzepts oder Blocks")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--concept", "-c")
    g.add_argument("--block", "-b")
    p.add_argument("--out", "-o")

    p = sub.add_parser("catalog", help="HTML-Galerie aller Visual-Typen")
    p.add_argument("--out", "-o")

    p = sub.add_parser("export-sqlite", help="Stufe 1: freigegebene Inhalte als SQLite-Datei für das Karo-MVP")
    p.add_argument("--subject", "-s", action="append", help="nur dieses Fach (mehrfach möglich)")
    p.add_argument("--out", "-o", help="Zieldatei (Standard: sqlite_export.path aus config.yaml)")

    p = sub.add_parser("serve", help="Curriculum-Agent als Dienst: arbeitet Karos Aufträge ab")
    p.add_argument("--provider", "-p", choices=list(PROVIDERS))
    p.add_argument("--once", action="store_true", help="nur vorhandene Aufträge abarbeiten, dann beenden")

    p = sub.add_parser("request", help="Thema anfragen wie Karo (karo.resolve_topic)")
    p.add_argument("--subject", "-s", required=True)
    p.add_argument("--grade", "-g", type=int, required=True)
    p.add_argument("--topic", "-t", required=True)
    p.add_argument("--keyword", "-k", action="append")
    p.add_argument("--task", action="append", help="Aufgabe vom Arbeitsblatt (mehrfach möglich, ohne Namen)")
    p.add_argument("--tenant", default="default")

    p = sub.add_parser("requests", help="Aufträge an den Curriculum-Agenten")
    p.add_argument("--limit", type=int, default=30)

    p = sub.add_parser("demand", help="Nachfrage: welche Themen fehlen am häufigsten?")
    p.add_argument("--days", type=int, default=90)
    p.add_argument("--limit", type=int, default=30)

    sub.add_parser("rerender-visuals", help="alle gespeicherten Bilder mit dem aktuellen Renderer neu zeichnen")

    p = sub.add_parser("api", help="Curriculum-Service über HTTP (für Karo und andere Abnehmer)")
    p.add_argument("--host", default=os.environ.get("KCTEAM_API_HOST", "127.0.0.1"))
    p.add_argument("--port", type=int, default=int(os.environ.get("KCTEAM_API_PORT", "8088")))
    p.add_argument("--workers", type=int, default=int(os.environ.get("KCTEAM_API_WORKERS", "1")))

    p = sub.add_parser("api-client", help="Abnehmer (API-Schlüssel) verwalten")
    p.add_argument("action", choices=["add", "list", "revoke"])
    p.add_argument("--name")
    p.add_argument("--tenant", help="Einrichtung (Tageslimit, Nachfrage); Standard: der Name")
    p.add_argument("--webhook", help="URL für Webhooks (optional)")
    p.add_argument("--key", help="eigenen Schlüssel vorgeben (kc_…, mind. 32 Zeichen), sonst wird einer erzeugt")

    p = sub.add_parser("admin", help="Datenbank-Browser (nur lesen) für Curriculum und Karo")
    p.add_argument("--host", default=os.environ.get("ADMIN_HOST", "127.0.0.1"))
    p.add_argument("--port", type=int, default=int(os.environ.get("ADMIN_PORT", "8090")))

    p = sub.add_parser("bench", help="Latenz des Curriculum-Service messen")
    p.add_argument("--url", default=os.environ.get("KARO_CURRICULUM_URL", "http://127.0.0.1:8088"))
    p.add_argument("--key")
    p.add_argument("--subject", default="Mathematik")
    p.add_argument("--grade", type=int, default=6)
    p.add_argument("--topic", action="append")
    p.add_argument("-n", type=int, default=300)
    p.add_argument("--concurrency", "-c", type=int, default=10)
    p.add_argument("--target", type=float, default=50, help="Ziel für p95 in ms")

    sub.add_parser("providers", help="eingerichtete KI-Zugänge anzeigen")
    sub.add_parser("init-db", help="Datenbankschema anlegen")

    args = parser.parse_args(argv)
    cfg = load_config(args.config)
    cmd = args.cmd or "run"
    if cmd == "run" and not hasattr(args, "subject"):
        args = parser.parse_args(["run"])
    handlers = {"run": cmd_run, "status": cmd_status, "review": cmd_review, "export": cmd_export,
                "doctor": cmd_doctor,
                "providers": cmd_providers, "init-db": cmd_init_db, "preview": cmd_preview,
                "catalog": cmd_catalog, "subjects": cmd_subjects, "check": cmd_check, "simulate": cmd_simulate,
                "serve": cmd_serve, "request": cmd_request, "export-sqlite": cmd_export_sqlite, "requests": cmd_requests, "demand": cmd_demand,
                "api": cmd_api, "rerender-visuals": cmd_rerender, "api-client": cmd_api_client, "admin": cmd_admin, "bench": cmd_bench}
    try:
        return handlers[cmd](cfg, args)
    except psycopg.OperationalError as exc:
        host = cfg.database_url.split('@')[-1]
        print(f"✗ Keine Verbindung zur Datenbank ({host}).\n  {str(exc).strip().splitlines()[0]}")
        if host.startswith("postgres:") or host.startswith("postgres/"):
            print("  Läuft der Datenbank-Container?  docker compose ps -a   /   docker compose up -d postgres\n"
                  "  Logs: docker compose logs postgres   (Port belegt? -> POSTGRES_HOST_PORT=5433 in .env)")
        elif "localhost" in host or "127.0.0.1" in host:
            print("  Im Docker-Container heißt die Datenbank 'postgres', nicht 'localhost' "
                  "(DATABASE_URL=postgresql://karo:karo@postgres:5432/karo).")
        else:
            print("  DATABASE_URL in .env prüfen.")
        return 1
    except ValueError as exc:
        print(f"✗ {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
