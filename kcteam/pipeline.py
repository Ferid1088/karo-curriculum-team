"""Die Orchestrierung des Teams.

Ablauf pro Fach:
  1. Curriculum-Analyst  -> Themenblöcke             -> Inspektor (Veto)
  pro Block:
  2. Fachdidaktiker      -> Konzepte + Kanten        -> Integrator -> Inspektor (Veto)
  pro Konzept (parallel):
  3. Niveau-Kalibrierer  -> Grenzen + Aufgaben       -> Integrator -> Inspektor (Veto)
  4. Diagnostiker        -> Fehlvorstellungen, Tests -> Integrator -> Inspektor (Veto)
  5. Visual-Didaktiker   -> Bildfolgen, Bildaufgaben -> Integrator -> Inspektor (Veto)
  pro Block:
  6. Kritiker            -> fachliche Mängel         -> zurück an die zuständige Rolle
  7. Schlussprüfung      -> Inspektor prüft das komplette Konzept -> approved (für Karo sichtbar)

Der Inspektor hat das letzte Wort unter den Agenten. Nach `max_inspector_rounds` Ablehnungen
wird der Inhalt gesperrt und landet in der Warteschlange für Menschen.
"""
from __future__ import annotations

import json
import re
import threading
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable

from .agents import AgentFailed, AgentRunner, BudgetExhausted, compact
from .providers.base import ProviderPending
from .integrator import check_calibration, check_diagnostics, check_graph, check_visuals
from .schemas import (Calibration, ConceptDraft, ConceptGraph, CriticReport, CurriculumMap, Diagnostics,
                      Finding, InspectorVerdict, VisualSet)

# Zuordnung der Fundstellen (erstes Pfadsegment) zu den Rollen, die korrigieren
ROUTE = {
    "title": "meta", "description": "meta", "titel": "meta", "beschreibung": "meta",
    "levels": "cal", "can_do": "cal", "difficulty_parameters": "cal", "anchor_items": "cal", "boundary_items": "cal",
    "misconceptions": "diag", "diagnostic_items": "diag", "exit_items": "diag",
    "visuals": "vis", "visual_need": "vis", "explanations": "vis", "visual_items": "vis", "rationale": "vis",
    # Klassenstufen, Kanten, Einordnung: gehören dem Fachdidaktiker (Meta-Korrektur), nicht allen Stationen
    "first_contact_grade": "meta", "target_grade": "meta", "prerequisites": "meta", "track": "meta",
    "learning_year": "meta", "cefr": "meta", "id": "meta", "status": "meta", "varies": "meta", "gesamt": "all",
}
# welcher Teil des abgelehnten Gesamtentwurfs geht an welche Rolle zurück
SLICES = {
    "meta": ("id", "title", "description", "first_contact_grade", "target_grade", "track", "learning_year", "cefr",
             "prerequisites"),
    "cal": ("levels", "can_do", "difficulty_parameters", "anchor_items", "boundary_items"),
    "diag": ("misconceptions", "diagnostic_items", "exit_items"),
    "vis": ("visual_need", "visuals"),
}
STAGE_ORDER = {"structured": 0, "calibrated": 1, "diagnosed": 2, "visualized": 3, "approved": 4}
STEP_ROLES = {"calibration": "niveau_kalibrierer", "diagnostics": "diagnostiker", "visuals": "visual_didaktiker"}
DRAFT_EXCERPT = 15000


class SubjectBlocked(RuntimeError):
    pass


# ---------------------------------------------------------------- Rückmeldungen pro Rolle
def fb_load(raw: str | None) -> dict[str, str]:
    """pending_feedback: Text eines Menschen (gilt für alle) oder JSON {rolle: text, 'all': text}."""
    if not raw:
        return {}
    if raw.lstrip().startswith("{"):
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                return {k: str(v) for k, v in data.items() if v}
        except json.JSONDecodeError:
            pass
    return {"all": raw}


def fb_for(fbd: dict[str, str], role: str) -> str | None:
    parts = [fbd.get("all"), fbd.get(role)]
    text = "\n\n".join(p for p in parts if p)
    return text or None


def join_fb(*parts: str | None) -> str | None:
    text = "\n\n".join(p for p in parts if p)
    return text or None


def format_findings(verdict: InspectorVerdict) -> str:
    lines = ["Der Kinderrechts-Inspektor hat abgelehnt (seine Entscheidung ist bindend). Auflagen:"]
    for f in verdict.findings:
        lines.append(f"- [{f.severity}] {f.location}: {f.rule} – {f.reason} → Auflage: {f.requirement}")
    if verdict.summary:
        lines.append(f"Zusammenfassung: {verdict.summary}")
    return "\n".join(lines)


def route_findings(verdict: InspectorVerdict | None) -> set[str]:
    """Welche Rollen müssen korrigieren? meta = Titel/Beschreibung, cal/diag/vis = Stationen, all = alles."""
    targets: set[str] = set()
    for f in (verdict.findings if verdict else []):
        if f.severity != "block":      # Hinweise (warn) lösen keine Neuerzeugung aus
            continue
        loc = re.sub(r"^(inhalt|content)\s*[.\[]?", "", f.location.strip().lower())
        first = re.split(r"[.\[\s:]", loc, maxsplit=1)[0]
        targets.add(ROUTE.get(first, "all"))
    return targets or {"all"}


def draft_excerpt(content: dict, keys=None) -> str:
    part = content if keys is None else {k: content.get(k) for k in keys if k in content}
    text = json.dumps(part, ensure_ascii=False, separators=(",", ":"))
    return text if len(text) <= DRAFT_EXCERPT else text[:DRAFT_EXCERPT] + " …(gekürzt)"


def compact_visuals(v: dict | None) -> dict | None:
    """Kurzfassung der Visuals für den Kritiker (ohne vollständige Specs)."""
    if not v:
        return v
    return {
        "visual_need": v.get("visual_need"), "rationale": v.get("rationale"),
        "explanations": [{"key": e.get("key"), "purpose": e.get("purpose"), "level": e.get("level"),
                          "for_misconception": e.get("for_misconception"),
                          "steps": [{"type": (s.get("visual") or {}).get("type"), "caption": s.get("caption"),
                                     "alt": (s.get("visual") or {}).get("alt")} for s in e.get("steps", [])]}
                         for e in v.get("explanations", [])],
        "visual_items": [{"use": i.get("use"), "prompt": i.get("prompt"), "solution": i.get("solution"),
                          "level": i.get("level"), "type": (i.get("visual") or {}).get("type"),
                          "answer": i.get("answer")} for i in v.get("visual_items", [])],
    }


#: Freiraum, den Aufgabe, Rollenregeln und Antwort-Schema im Inspektor-Auftrag
#: brauchen — der geprüfte Inhalt allein darf höchstens so gross werden
#: (Devin-Grenze: 29.500 Zeichen Gesamtvorgabe).
_INHALT_BUDGET = 20_000

#: Reserve im Inspektions-Auftrag: Wiederholungs-Anhang, Teil-Hinweis und
#: die Huelle der Nutzdaten. Die Groessen-Rechnung liegt beim Anbieter —
#: statisch 20k war schon einmal ~600 Zeichen ueber der Grenze (EXP-205).
_PRUEF_RESERVE = 2_200


def _inhalt_teile(content: dict, budget: int = _INHALT_BUDGET) -> list[dict]:
    """Prüfinhalt in Portionen schneiden, die in einen Auftrag passen.

    Ein fertiges Konzept trägt Kalibrierung und Diagnostik zusammen leicht
    über die Vorgaben-Grenze — der Auftrag wurde dann als zu gross
    zurückgemeldet, ohne dass je geprüft wurde. Abgeschnitten wird nichts:
    geprüft wird in Teilen, an ganzen Schlüsselfeldern entlang, damit
    Fundstellen wie `diagnostic_items[1]` weiter stimmen.
    """
    if len(compact(content)) <= budget:
        return [content]
    teile: list[dict] = []
    for key, wert in content.items():
        groesse = len(compact(teile[-1])) if teile else 0
        if teile and groesse + len(compact(wert)) > budget:
            teile.append({})
        if not teile:
            teile.append({})
        teile[-1][key] = wert
    return teile


class Pipeline:
    def __init__(self, *, cfg, provider, db, run_id: str, karo_spec: str = "", log: Callable[[str], None] = print,
                 team=None):
        self.cfg, self.db, self.run_id, self.log = cfg, db, run_id, log
        self.team = team
        if hasattr(provider, "bind_db"):
            provider.bind_db(db)   # asynchrone Anbieter (Devin) halten ihre Sessions in der Datenbank
        self.agents = AgentRunner(cfg=cfg, provider=provider, db=db, run_id=run_id, karo_spec=karo_spec, team=team)
        self.stats: Counter = Counter()
        self._lock = threading.Lock()
        self.max_rounds = int(cfg.p("max_inspector_rounds", 3))
        self.grades: tuple[int, int] = tuple(cfg.p("default_grades", [1, 13]))
        self.scoped: set[str] = set()
        self.workers = max(1, int(cfg.p("parallel_concepts", 4)))
        self._pool: ThreadPoolExecutor | None = None
        self.skip_visuals = False    # Schnellspur (Curriculum-Agent): Visuals werden danach nachgerüstet
        self.item_guard: Callable[[dict, Any], list[str]] | None = None   # z. B. "nichts vom Arbeitsblatt kopieren"

    def _stat(self, key: str, n: int = 1) -> None:
        with self._lock:
            self.stats[key] += n

    @property
    def pool(self) -> ThreadPoolExecutor:
        if self._pool is None:   # ein Pool für den ganzen Lauf: feste Threads, feste DB-Verbindungen
            self._pool = ThreadPoolExecutor(max_workers=self.workers, thread_name_prefix="kcteam")
        return self._pool

    def shutdown(self) -> None:
        self.agents.stop.set()
        if self._pool is not None:
            self._pool.shutdown(wait=True, cancel_futures=True)
            self._pool = None

    def _parallel(self, fn: Callable[[Any], None], items: list[dict], stage: str) -> None:
        if not items:
            return
        futures = {self.pool.submit(fn, it): it["id"] for it in items}
        try:
            pending = None
            for fut in as_completed(futures):
                exc = fut.exception()
                if isinstance(exc, ProviderPending):
                    # Der Anbieter arbeitet noch. Andere Konzepte duerfen
                    # zu Ende laufen — ihre Fortschritte sind gespeichert —
                    # dann wird der ganze Auftrag zurueckgestellt.
                    pending = pending or exc
                    continue
                if isinstance(exc, BudgetExhausted):
                    raise exc
                if exc:
                    cid = futures[fut]
                    st = getattr(exc, "stage", None) or stage
                    self._stat("errors")
                    self.log(f"   ⚠ {cid}: {exc}")
                    self.db.enqueue_human("concept", cid, st, str(exc)[:1000], kind="error")
            if pending:
                raise pending
        except BaseException:
            self.agents.stop.set()
            for f in futures:
                f.cancel()
            raise

    # ================================================================ Einstieg
    def run(self, subject: str, grades: tuple[int, int], block_filter: list[str] | None = None,
            refresh_map: bool = False) -> dict[str, Any]:
        self.grades = grades
        if self.team is None:
            from .subjects import SubjectTeam
            self.team = self.agents.team = SubjectTeam.for_subject(subject)
        subject = self.team.name
        try:
            code = self.ensure_curriculum(subject, grades, refresh_map)
            blocks = [b for b in self.db.blocks(code)
                      if b["grade_max"] >= grades[0] and b["grade_min"] <= grades[1]]
            if block_filter:
                wanted = [w.lower() for w in block_filter]
                blocks = [b for b in blocks if any(w in b["title"].lower() or w in b["id"].lower() for w in wanted)]
            self.log(f"→ {len(blocks)} Themenblöcke zu bearbeiten")
            for b in blocks:
                self.process_block(code, b)
            if self.cfg.p("include_prerequisites", True):
                self.fill_prerequisites(code)
            self.report_missing_prerequisites(code)
        finally:
            if self._pool is not None:
                self._pool.shutdown(wait=True, cancel_futures=True)
                self._pool = None
        return dict(self.stats)

    # ================================================================ Inspektor-Tor
    def inspect(self, entity_type: str, entity_id: str, stage: str, grade_hint: str, content: dict,
                round_: int) -> InspectorVerdict:
        overrides = []
        if entity_type == "concept":
            row = self.db.one("SELECT human_overrides FROM curriculum.concepts WHERE id=%s", (entity_id,))
            overrides = (row or {}).get("human_overrides") or []
        task = (f"Prüfe den folgenden Inhalt (Station: {stage}) für Kinder der Klassenstufe {grade_hint}. "
                "Prüfe jedes Feld. Entscheide approved oder rejected und begründe jeden Befund mit Fundstelle und Auflage. "
                "Fundstellen relativ zu 'inhalt' angeben (z. B. anchor_items[1].prompt).")
        # Das statische 20k-Budget zaehlt nur den Inhalt — der Auftrag drum
        # herum (System, Aufgabe, Wiederholungs-Anhang) geht mit in die
        # Anbietergrenze. Gemessen, nicht geschaetzt: derselbe Rechenfehler
        # wie bei der Lektionsgrundlage.
        budget = _INHALT_BUDGET
        limit = getattr(self.agents.provider, "prompt_limit", None)
        if limit:
            fest = (int(getattr(self.agents.provider, "prompt_overhead", 0) or 0)
                    + len(self.agents.system_prompt("kinderrechts_inspektor", InspectorVerdict))
                    + len(task) + _PRUEF_RESERVE)
            budget = min(_INHALT_BUDGET, max(4_000, limit - fest))
        teile = _inhalt_teile(content, budget)
        fundstellen: list[Finding] = []
        abgelehnt = False
        zeilen: list[str] = []
        for nummer, teil in enumerate(teile, 1):
            hinweis = "" if len(teile) == 1 else f" (Teil {nummer} von {len(teile)} derselben Prüfung)"
            v = self.agents.call("kinderrechts_inspektor", task + hinweis,
                                 {"station": stage, "klassenstufe": grade_hint, "inhalt": teil,
                                  "vom_menschen_freigegeben": overrides},
                                 InspectorVerdict, entity_id=entity_id, meta={"stage": stage}, stage=stage)
            fundstellen += v.findings
            abgelehnt = abgelehnt or v.decision == "rejected"
            if v.summary:
                zeilen.append(v.summary)
        verdict = InspectorVerdict(decision="rejected" if abgelehnt else "approved",
                                 findings=fundstellen, summary=" | ".join(zeilen))
        if any(f.severity == "block" for f in verdict.findings):
            verdict.decision = "rejected"
        if verdict.decision == "rejected" and not any(f.severity == "block" for f in verdict.findings):
            # Eine Ablehnung ist bindend – auch ohne ausformulierten Befund. Nie still in eine Freigabe verwandeln.
            verdict.findings.append(Finding(location="gesamt", rule="Ablehnung durch den Inspektor", severity="block",
                                            reason=verdict.summary or "ohne nähere Begründung",
                                            requirement="Inhalt so überarbeiten, dass die Bedenken ausgeräumt sind."))
        self.db.log_review(self.run_id, entity_type, entity_id, stage, "kinderrechts_inspektor",
                           verdict.decision, round_, [f.model_dump() for f in verdict.findings], verdict.summary)
        return verdict

    def gated(self, *, entity_type: str, entity_id: str, stage: str, grade_hint: str,
              produce: Callable[[str | None, InspectorVerdict | None], Any],
              to_content: Callable[[Any], dict] = lambda o: o.model_dump(),
              initial_feedback: str | None = None, include_draft: bool = True) -> tuple[Any, bool]:
        """Erzeugen -> Inspektor -> ggf. überarbeiten. Die ursprüngliche Rückmeldung (Mensch/Kritiker) bleibt in
        jeder Runde erhalten, und der abgelehnte Entwurf wird mitgeschickt, damit die Überarbeitung gezielt ist."""
        feedback, verdict, obj = initial_feedback, None, None
        for rnd in range(1, self.max_rounds + 1):
            obj = produce(feedback, verdict)
            content = to_content(obj)
            verdict = self.inspect(entity_type, entity_id, stage, grade_hint, content, rnd)
            if verdict.decision == "approved":
                self._stat("inspector_approved")
                return obj, True
            self._stat("inspector_rejected")
            self.log(f"   ✋ Inspektor lehnt ab: {entity_id} [{stage}] Runde {rnd}/{self.max_rounds}")
            feedback = join_fb(initial_feedback, format_findings(verdict),
                               f"Dein abgelehnter Entwurf (so wurde er geprüft):\n```json\n{draft_excerpt(content)}\n```"
                               if include_draft else None)
        self._stat("blocked_for_human")
        self.log(f"   ⛔ gesperrt, an Menschen übergeben: {entity_id} [{stage}]")
        self.db.enqueue_human(entity_type, entity_id, stage,
                              f"{self.max_rounds}× vom Kinderrechts-Inspektor abgelehnt",
                              {"content": to_content(obj), "verdict": verdict.model_dump() if verdict else None,
                               "raw": obj.model_dump() if hasattr(obj, "model_dump") else None})
        return obj, False

    def with_integrator(self, call: Callable[[str | None], Any], check: Callable[[Any], list[str]],
                        entity_type: str, entity_id: str, stage: str, feedback: str | None) -> Any:
        fb, errors = feedback, []
        for attempt in range(1, 4):
            obj = call(fb)
            errors = check(obj)
            if not errors:
                return obj
            self._stat("integrator_rejected")
            self.db.log_review(self.run_id, entity_type, entity_id, stage, "integrator", "issues", attempt,
                               [{"error": e} for e in errors])
            fb = join_fb(feedback, "Die automatische Strukturprüfung hat Fehler gefunden:\n- " + "\n- ".join(errors))
        raise AgentFailed(f"{entity_id} [{stage}]: Strukturfehler nach 3 Versuchen: {errors[:5]}", stage=stage)

    # ================================================================ 1. Curriculum
    def _free_code(self, code: str, subject: str) -> str:
        """Fachkürzel darf nicht mit einem anderen Fach kollidieren (nur relevant für Fächer ohne Profil)."""
        for i in range(1, 100):
            cand = code if i == 1 else f"{code[:3]}{i}"
            row = self.db.one("SELECT name FROM curriculum.subjects WHERE code=%s", (cand,))
            if not row or row["name"].lower() == subject.lower():
                return cand
        raise SubjectBlocked(f"Kein freies Fachkürzel für {subject}")

    def ensure_curriculum(self, subject: str, grades: tuple[int, int], refresh: bool) -> str:
        existing = self.db.get_subject(subject)
        if existing and existing["status"] == "blocked" and not refresh:
            raise SubjectBlocked(f"Die Themenlandkarte für {subject} ist gesperrt – siehe `review list`.")
        if existing and not refresh and existing["grade_min"] <= grades[0] and existing["grade_max"] >= grades[1] \
                and self.db.blocks(existing["code"]):
            self.log(f"✓ Themenlandkarte für {subject} vorhanden ({existing['code']})")
            return existing["code"]

        code = existing["code"] if existing else self._free_code(self.team.code, subject)
        d0, d1 = self.team.grades
        span = (min(grades[0], d0, existing["grade_min"] if existing else d0),
                max(grades[1], d1, existing["grade_max"] if existing else d1))
        old_blocks = [{"id": b["id"], "title": b["title"], "grade_min": b["grade_min"], "grade_max": b["grade_max"]}
                      for b in (self.db.blocks(code) if existing else [])]
        self.log(f"→ Curriculum-Analyst erstellt die Themenlandkarte: {subject}, Klasse {span[0]}–{span[1]}")
        task = (f"Erstelle die Themenlandkarte für das Fach „{subject}“, Klassen {span[0]} bis {span[1]}. "
                f"Verwende das Fachkürzel {code}" + (" und behalte die IDs bestehender Blöcke bei." if old_blocks else "."))
        payload = {"fach": subject, "klassen": list(span), "fachkuerzel": code, "bestehende_bloecke": old_blocks}

        def check(cmap: CurriculumMap) -> list[str]:
            errs = []
            if cmap.subject_code != code:
                errs.append(f"subject_code muss {code} sein.")
            ids = [b.id for b in cmap.blocks]
            if len(ids) != len(set(ids)):
                errs.append("Doppelte Block-IDs.")
            if not cmap.blocks:
                errs.append("Keine Blöcke geliefert.")
            for b in cmap.blocks:
                if not b.id.startswith(code + "."):
                    errs.append(f"{b.id} muss mit '{code}.' beginnen.")
                if not (b.grade_min <= b.typical_grade <= b.grade_max):
                    errs.append(f"{b.id}: typical_grade muss zwischen grade_min und grade_max liegen.")
            covered = {g for b in cmap.blocks for g in range(b.grade_min, b.grade_max + 1)}
            gaps = [g for g in range(span[0], span[1] + 1) if g not in covered]
            if gaps:
                errs.append(f"Klassenstufen ohne Themenblock: {gaps}")
            return errs

        def produce(fb, _verdict):
            return self.with_integrator(
                lambda f: self.agents.call("curriculum_analyst", task, payload, CurriculumMap, entity_id=subject,
                                           feedback=f, web_search=True, stage="curriculum",
                                           meta={"subject": subject, "grades": list(span)}),
                check, "curriculum", subject, "curriculum", fb)

        cmap, ok = self.gated(entity_type="curriculum", entity_id=subject, stage="curriculum",
                              grade_hint=f"{span[0]}–{span[1]}", produce=produce,
                              to_content=lambda m: {"fach": subject, "bloecke": [
                                  {"id": b.id, "title": b.title, "description": b.description} for b in m.blocks]})
        if not ok:
            if existing:   # Erweiterung gesperrt: bestehende Landkarte bleibt unverändert nutzbar
                self.log(f"⛔ Erweiterung der Themenlandkarte gesperrt – bestehende Karte ({existing['grade_min']}–"
                         f"{existing['grade_max']}) wird weiter genutzt")
                return existing["code"]
            self.db.save_subject(code, subject, span, "blocked", self.team.subject.id)
            raise SubjectBlocked(f"Themenlandkarte für {subject} vom Inspektor gesperrt – siehe `review list`.")
        self.db.save_subject(code, subject, span, "mapped", self.team.subject.id)
        for b in cmap.blocks:
            self.db.save_block(code, b)
        self.log(f"✓ {len(cmap.blocks)} Themenblöcke gespeichert ({code})")
        return code

    # ================================================================ 2. Block
    def process_block(self, code: str, block: dict, scope: set[str] | None = None) -> None:
        """scope=None: Konzepte nach gewählter Klasse/Klassenbereich (+ Voraussetzungen).
        scope=Menge: genau diese Konzepte (+ ihre Voraussetzungen) – für das Nachziehen tieferer Klassen."""
        bid = block["id"]
        if block["status"] == "blocked":
            self.log(f"⏭  {bid} ist gesperrt (wartet auf Menschen)")
            return
        self.log(f"\n■ Block {bid} – {block['title']} (Kl. {block['grade_min']}–{block['grade_max']})")
        try:
            if block["status"] == "pending":
                current = self._current_structure(bid)
                # Block mit vorhandenen Konzepten: überarbeiten statt neu zerlegen – freigegebene bleiben erhalten
                ok, _ = self.structure_block(code, block, feedback=block.get("pending_feedback"),
                                             current=current or None, keep_approved=bool(current))
                if not ok:
                    self.db.set_block_status(bid, "blocked")
                    return
                self.db.query("UPDATE curriculum.topic_blocks SET pending_feedback=NULL, status='structured' "
                              "WHERE id=%s", (bid,))
                block["status"] = "structured"

            ids = self.scope_ids(code, bid, scope)
            with self._lock:
                self.scoped |= ids
            concepts = self.db.concepts(block_id=bid, light=True)
            skipped = [c for c in concepts if c["id"] not in ids and c["status"] != "approved"]
            if skipped:
                self.log(f"   (außerhalb der Auswahl, später: {len(skipped)} Konzepte)")

            self._parallel(self.process_concept, [c for c in concepts if c["id"] in ids
                                                  and c["status"] in ("structured", "calibrated", "diagnosed")],
                           "processing")
            self.backfill_visuals(bid, ids)
            self._parallel(self.rework_approved,
                           [c for c in self.db.concepts(block_id=bid, light=True)
                            if c["id"] in ids and c["status"] == "approved"
                            and "rework" in fb_load(c.get("pending_feedback"))], "rework")

            fresh = [c for c in self.db.concepts(block_id=bid, light=True)
                     if c["id"] in ids and c["status"] == "visualized"]
            if fresh:
                for rnd in range(1, int(self.cfg.p("max_critic_rounds", 1)) + 1):
                    try:
                        if not self.critic_round(code, block, rnd, ids):
                            break
                    except AgentFailed as exc:   # Kritiker-Ausfall darf die Schlussprüfung nicht verhindern
                        self._stat("errors")
                        self.log(f"   ⚠ Kritiker {bid}: {exc}")
                        self.db.enqueue_human("block", bid, "critic", str(exc)[:1000], kind="error")
                        break
                if block["status"] in ("structured", "pending"):
                    self.db.set_block_status(bid, "reviewed")

            self.finalize_block(bid, ids)
            states = Counter(c["status"] for c in self.db.concepts(block_id=bid, light=True))
            if set(states) <= {"approved", "blocked"}:
                self.db.set_block_status(bid, "done")
            self.log(f"✓ {bid}: " + ", ".join(f"{k}={v}" for k, v in sorted(states.items())))
        except (BudgetExhausted, ProviderPending):
            raise
        except Exception as exc:  # noqa: BLE001 – ein Block darf den Lauf nicht beenden
            stage = getattr(exc, "stage", None) or "graph"
            self._stat("errors")
            self.log(f"   ⚠ {bid} [{stage}]: {exc}")
            self.db.enqueue_human("block", bid, stage, f"{exc.__class__.__name__}: {exc}"[:1000], kind="error")

    def _current_structure(self, bid: str) -> list[dict]:
        return [{"id": c["id"], "title": c["title"], "description": c["description"],
                 "first_contact_grade": c["first_contact_grade"], "target_grade": c["target_grade"],
                 "varies": c["varies"], "prerequisites": self.db.prerequisites(c["id"]),
                 "order": c["sort_order"], "status": c["status"]}
                for c in self.db.concepts(block_id=bid, light=True, with_description=True)
                if c["status"] != "retired"]

    @staticmethod
    def prerequisite_closure(start: set[str], edges: list[tuple[str, str]]) -> set[str]:
        pre: dict[str, list[str]] = defaultdict(list)
        for a, b in edges:
            pre[a].append(b)
        seen: set[str] = set()
        stack = list(start)
        while stack:
            for p in pre.get(stack.pop(), []):
                if p not in seen:
                    seen.add(p)
                    stack.append(p)
        return seen

    def scope_ids(self, code: str, bid: str, scope: set[str] | None) -> set[str]:
        concepts = {c["id"]: c for c in self.db.concepts(block_id=bid, light=True)}
        lo, hi = self.grades
        if scope is None:
            core = {cid for cid, c in concepts.items() if lo <= c["target_grade"] <= hi}
        else:
            core = scope & set(concepts)
        if self.cfg.p("include_prerequisites", True):
            core |= self.prerequisite_closure(core, self.db.all_edges(code))
        return core & set(concepts)

    def fill_prerequisites(self, code: str) -> None:
        """Voraussetzungen aus tieferen Klassen bzw. anderen Blöcken nachziehen, damit Lernpfade lückenlos sind."""
        for _ in range(6):
            needed = self.prerequisite_closure(set(self.scoped), self.db.all_edges(code)) - self.scoped
            if not needed:
                return
            by_block: dict[str, set[str]] = defaultdict(set)
            for cid in needed:
                by_block[".".join(cid.split(".")[:2])].add(cid)
            progressed = False
            for bid, ids in sorted(by_block.items()):
                block = self.db.one("SELECT * FROM curriculum.topic_blocks WHERE id=%s", (bid,))
                if not block or block["status"] == "blocked":
                    self.scoped |= ids  # nicht auflösbar -> wird als fehlende Voraussetzung gemeldet
                    continue
                self.log(f"→ Voraussetzungen nachziehen: {bid} ({len(ids)} Konzepte)")
                before = len(self.scoped)
                self.process_block(code, block, scope=ids)
                self.scoped |= ids
                progressed = progressed or len(self.scoped) > before
            if not progressed:
                return

    def structure_block(self, code: str, block: dict, feedback: str | None = None,
                        current: list[dict] | None = None, keep_approved: bool = False) -> tuple[bool, dict[str, str]]:
        bid = block["id"]
        # Nur Konzepte, die als Voraussetzung in Frage kommen (Zielklasse bis Blockende), kompakt
        others = {c["id"]: c for c in self.db.concepts(subject_code=code, light=True)
                  if c["block_id"] != bid and c["target_grade"] <= block["grade_max"]}
        edges = self.db.all_edges(code)
        # keep_approved: freigegebene Konzepte des Blocks bleiben – ihre Kanten zählen für die Kreisprüfung
        kept = {c["id"]: c for c in self.db.concepts(block_id=bid, light=True)
                if c["status"] == "approved"} if keep_approved else {}
        other_edges = [(a, b) for a, b in edges if not a.startswith(bid + ".") or a in kept]
        known = {**others, **kept}
        block_info = {k: block[k] for k in ("id", "title", "description", "grade_min", "grade_max",
                                             "typical_grade", "varies", "variance_note")}
        expected = sorted({b for a, b in edges if b.startswith(bid + ".") and not a.startswith(bid + ".")})
        payload = {
            "block": block_info,
            "von_anderen_bloecken_erwartete_ids": expected,
            "bekannte_konzepte_anderer_bloecke": [[c["id"], c["title"], c["target_grade"]] for c in others.values()],
            "aktuelle_struktur": current,
        }
        task = (f"Zerlege den Themenblock {bid} „{block['title']}“ in Konzepte mit direkten Voraussetzungen."
                if not current else f"Überarbeite die Struktur des Themenblocks {bid} gemäß Rückmeldung.")
        if keep_approved and current:
            task += (" Bereits freigegebene Konzepte (status approved) behalten ID, Titel und Klassenstufen; "
                     "ergänze oder korrigiere nur Kanten und nicht freigegebene Konzepte.")

        def produce(fb, _verdict):
            return self.with_integrator(
                lambda f: self.agents.call("fachdidaktiker", task, payload, ConceptGraph, entity_id=bid, stage="graph",
                                           feedback=f, meta={"block": block_info, "current": current}),
                lambda g: check_graph(g, block, known, other_edges)[0],
                "block", bid, "graph", fb)

        graph, ok = self.gated(entity_type="block", entity_id=bid, stage="graph",
                               grade_hint=f"{block['grade_min']}–{block['grade_max']}", produce=produce,
                               initial_feedback=feedback,
                               to_content=lambda g: {"block": block["title"], "konzepte": [
                                   {"id": c.id, "title": c.title, "description": c.description,
                                    "klasse": c.target_grade} for c in g.concepts]})
        if not ok:
            return False, {}
        graph.block_id = bid
        changes = self.db.save_graph(code, bid, graph.concepts, keep_approved=keep_approved)
        missing = check_graph(graph, block, known, other_edges)[1]
        if missing:
            self.db.log_review(self.run_id, "block", bid, "graph", "integrator", "ok", 1,
                               [{"missing_prerequisite": m} for m in missing],
                               "Voraussetzungen in noch nicht bearbeiteten Blöcken")
        self.log(f"   ✓ Struktur: {len(graph.concepts)} Konzepte"
                 + (f", {len(changes)} neu/geändert" if changes else ""))
        return True, changes

    # ================================================================ 3.–5. Konzepte
    def _concept_payload(self, c: dict) -> dict:
        pre = self.db.query("""SELECT p.prerequisite_id AS id, x.title, x.target_grade
                               FROM curriculum.concept_prerequisites p
                               LEFT JOIN curriculum.concepts x ON x.id = p.prerequisite_id
                               WHERE p.concept_id=%s ORDER BY 1""", (c["id"],))
        block = self.db.one("SELECT id, title FROM curriculum.topic_blocks WHERE id=%s", (c["block_id"],))
        return {"konzept": {k: c.get(k) for k in ("id", "title", "description", "first_contact_grade",
                                                  "target_grade", "varies", "track", "learning_year", "cefr")},
                "block": block,
                "voraussetzungen": [{"id": p["id"], "title": p["title"] or "(noch nicht angelegt)",
                                     "target_grade": p["target_grade"]} for p in pre]}

    def process_concept(self, c_or_id) -> None:
        cid = c_or_id["id"] if isinstance(c_or_id, dict) else c_or_id
        c = self.db.concept(cid)
        fbd = fb_load(c.get("pending_feedback"))
        steps = [("structured", "calibration", self.calibrate), ("calibrated", "diagnostics", self.diagnose),
                 ("diagnosed", "visuals", self.visualize)]
        for status, stage, fn in steps:
            if c["status"] != status:
                continue
            if not fn(c, fb_for(fbd, STEP_ROLES[stage])):
                self.db.set_concept_status(cid, "blocked", keep_feedback=True)
                return
            fbd.pop(STEP_ROLES[stage], None)          # Rolle hat ihre Rückmeldung umgesetzt
            c = self.db.concept(cid)
        # alle Stationen durchlaufen: Rückmeldungen sind umgesetzt (die Schlussprüfung prüft das Ergebnis ohnehin)
        self.db.query("UPDATE curriculum.concepts SET pending_feedback=NULL WHERE id=%s", (cid,))

    def _guard(self, c: dict, obj: Any) -> list[str]:
        return self.item_guard(c, obj) if self.item_guard else []

    def _calibration_call(self, c: dict, fb: str | None) -> Calibration:
        payload = self._concept_payload(c)
        if c.get("calibration"):
            payload["bisherige_kalibrierung"] = c["calibration"]
        return self.with_integrator(
            lambda f: self.agents.call("niveau_kalibrierer",
                                       f"Kalibriere das Konzept {c['id']} für das Zielniveau Klasse {c['target_grade']}.",
                                       payload, Calibration, entity_id=c["id"], feedback=f, stage="calibration",
                                       meta={"concept": c}),
            lambda cal: check_calibration(c, cal) + self._guard(c, cal), "concept", c["id"], "calibration", fb)

    def _diagnostics_call(self, c: dict, fb: str | None) -> Diagnostics:
        payload = self._concept_payload(c)
        payload["kalibrierung"] = c["calibration"]
        if c.get("diagnostics"):
            payload["bisherige_diagnostik"] = c["diagnostics"]
        return self.with_integrator(
            lambda f: self.agents.call("diagnostiker",
                                       f"Erstelle Fehlvorstellungen, Diagnose- und Abschlussaufgaben für {c['id']} "
                                       f"(Zielniveau Klasse {c['target_grade']}).",
                                       payload, Diagnostics, entity_id=c["id"], feedback=f, stage="diagnostics",
                                       meta={"concept": c}),
            lambda d: check_diagnostics(c, d, self.team.min_auto_checkable if self.team else 2) + self._guard(c, d),
            "concept", c["id"], "diagnostics", fb)

    def calibrate(self, c: dict, feedback: str | None = None) -> bool:
        cal, ok = self.gated(entity_type="concept", entity_id=c["id"], stage="calibration",
                             grade_hint=str(c["target_grade"]), initial_feedback=feedback,
                             produce=lambda fb, _v: self._calibration_call(c, fb),
                             to_content=lambda x: {"title": c["title"], **x.model_dump()})
        if ok:
            self.db.save_calibration(c["id"], cal)
            self._stat("calibrated")
        return ok

    def diagnose(self, c: dict, feedback: str | None = None) -> bool:
        diag, ok = self.gated(entity_type="concept", entity_id=c["id"], stage="diagnostics",
                              grade_hint=str(c["target_grade"]), initial_feedback=feedback,
                              produce=lambda fb, _v: self._diagnostics_call(c, fb),
                              to_content=lambda x: {"title": c["title"], **x.model_dump()})
        if ok:
            self.db.save_diagnostics(c["id"], diag)
            self._stat("diagnosed")
        return ok

    def _visual_call(self, c: dict, fb: str | None) -> VisualSet:
        payload = self._concept_payload(c)
        payload["kalibrierung"] = {k: (c.get("calibration") or {}).get(k)
                                   for k in ("levels", "can_do", "difficulty_parameters")}
        diag = c.get("diagnostics") or {}
        payload["fehlvorstellungen"] = [{"key": m["key"], "description": m["description"],
                                         "remediation_hint": m["remediation_hint"]}
                                        for m in diag.get("misconceptions", [])]
        if c.get("visuals") and not c["visuals"].get("rejected_by_human"):
            payload["bisherige_visuals"] = c["visuals"]
        keys = {m["key"].upper() for m in diag.get("misconceptions", [])}
        return self.with_integrator(
            lambda f: self.agents.call("visual_didaktiker",
                                       f"Entwirf die visuellen Erklärungen und visuellen Aufgaben für {c['id']} "
                                       f"(Zielniveau Klasse {c['target_grade']}).",
                                       payload, VisualSet, entity_id=c["id"], feedback=f, stage="visuals",
                                       meta={"concept": c}),
            lambda v: check_visuals(c, v, keys), "concept", c["id"], "visuals", fb)

    def visualize(self, c: dict, feedback: str | None = None, keep_status: bool = False) -> bool:
        if not self.cfg.p("visuals", True) or self.skip_visuals:
            self.db.query("UPDATE curriculum.concepts SET status='visualized' WHERE id=%s AND status='diagnosed'",
                          (c["id"],))
            return True
        vset, ok = self.gated(entity_type="concept", entity_id=c["id"], stage="visuals",
                              grade_hint=str(c["target_grade"]), initial_feedback=feedback,
                              produce=lambda fb, _v: self._visual_call(c, fb),
                              to_content=lambda x: {"title": c["title"], **x.model_dump()})
        if ok:
            self.db.save_visuals(c["id"], vset, keep_status=keep_status)
            self._stat("visualized")
        return ok

    def _backfill_one(self, light: dict) -> None:
        c = self.db.concept(light["id"])
        fb = fb_for(fb_load(c.get("pending_feedback")), "visual_didaktiker")
        if self.visualize(c, fb, keep_status=True):
            self.db.query("UPDATE curriculum.concepts SET pending_feedback=NULL WHERE id=%s", (c["id"],))

    def backfill_visuals(self, bid: str, ids: set[str]) -> None:
        """Freigegebene Konzepte ohne Visuals nachrüsten (bleiben währenddessen sichtbar).
        Konzepte mit offener Prüfung durch einen Menschen werden übersprungen – kein Endlos-Wiederholen."""
        if not self.cfg.p("visuals", True):
            return
        todo = self.db.query("""SELECT c.id FROM curriculum.concepts c
                                WHERE c.block_id=%s AND c.id = ANY(%s) AND c.status='approved' AND c.visuals IS NULL
                                  AND NOT EXISTS (SELECT 1 FROM curriculum.human_queue q WHERE q.status='open'
                                                  AND q.entity_type='concept' AND q.entity_id=c.id
                                                  AND q.stage='visuals')""", (bid, list(ids)))
        if todo:
            self.log(f"   → Visuals nachrüsten: {len(todo)} Konzepte")
            self._parallel(self._backfill_one, todo, "visuals")

    # ================================================================ 6. Kritiker
    def _bundle(self, c: dict, compact: bool = False) -> dict:
        cal, diag = c.get("calibration") or {}, c.get("diagnostics") or {}
        return {
            "id": c["id"], "title": c["title"], "description": c["description"], "status": c.get("status"),
            "first_contact_grade": c["first_contact_grade"], "target_grade": c["target_grade"],
            "track": c.get("track"), "learning_year": c.get("learning_year"), "cefr": c.get("cefr"),
            "prerequisites": self.db.prerequisites(c["id"]),
            "levels": c.get("levels"), "can_do": c.get("can_do"),
            "difficulty_parameters": c.get("difficulty_parameters"),
            "anchor_items": cal.get("anchor_items"), "boundary_items": cal.get("boundary_items"),
            "misconceptions": diag.get("misconceptions"), "diagnostic_items": diag.get("diagnostic_items"),
            "exit_items": diag.get("exit_items"),
            "visual_need": c.get("visual_need"),
            "visuals": compact_visuals(c.get("visuals")) if compact else c.get("visuals"),
        }

    @staticmethod
    def _brief(c: dict, prereqs: list[str]) -> dict:
        return {"id": c["id"], "title": c["title"], "status": "approved",
                "first_contact_grade": c["first_contact_grade"], "target_grade": c["target_grade"],
                "prerequisites": prereqs}

    def critic_round(self, code: str, block: dict, rnd: int, ids: set[str],
                     rework_ids: set[str] | None = None) -> bool:
        """Gibt True zurück, wenn Korrekturen gemacht wurden (dann ggf. weitere Runde).
        Geprüft werden nur neue Konzepte (visualized); freigegebene gehen kompakt als Kontext mit.
        Mängel an freigegebenen Konzepten gehen an einen Menschen – sie werden nie still überschrieben.
        rework_ids: freigegebene Konzepte aus der Schnellspur – werden voll geprüft und bei Mängeln im Hintergrund
        überarbeitet (bleiben dabei sichtbar)."""
        rework_ids = rework_ids or set()
        bid = block["id"]
        concepts = [c for c in self.db.concepts(block_id=bid)
                    if c["id"] in ids and c["status"] in ("visualized", "approved")]
        fresh = [c for c in concepts if c["status"] == "visualized" or c["id"] in rework_ids]
        if not fresh:
            return False
        fresh_ids = {c["id"] for c in fresh}
        approved = [self._brief(c, self.db.prerequisites(c["id"])) for c in concepts
                    if c["status"] == "approved" and c["id"] not in fresh_ids]
        self.log(f"   → Kritiker prüft {bid} (Runde {rnd})")
        chunk = max(1, int(self.cfg.p("critic_chunk", 8)))
        issues = []
        for i in range(0, len(fresh), chunk):   # große Blöcke in Portionen, damit der Kontext reicht
            part = fresh[i:i + chunk]
            report = self.agents.call("kritiker", f"Prüfe den Themenblock {bid} fachlich.",
                                      {"block": {k: block[k] for k in ("id", "title", "grade_min", "grade_max")},
                                       "konzepte": [self._bundle(c, compact=True) for c in part],
                                       "freigegebene_konzepte_kontext": approved},
                                      CriticReport, entity_id=bid, stage="critic", meta={"block": block, "round": rnd})
            issues += report.issues
        self.db.log_review(self.run_id, "block", bid, "critic", "kritiker", "issues" if issues else "ok",
                           rnd, [i.model_dump() for i in issues])
        if not issues:
            self.log("   ✓ Kritiker: keine Mängel")
            return False
        self._stat("critic_issues", len(issues))
        self.log(f"   ✎ Kritiker: {len(issues)} Mängel")

        def fmt(items) -> str:
            return "Der Kritiker hat fachliche Mängel gefunden:\n" + "\n".join(
                f"- [{i.type}] {i.concept_id or bid}: {i.description} → Vorschlag: {i.suggested_fix}" for i in items)

        status0 = {c["id"]: c["status"] for c in concepts}
        # Mängel an freigegebenen Konzepten (auch strukturelle): an einen Menschen
        on_approved: dict[str, list] = defaultdict(list)
        rest = []
        rework: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
        for i in issues:
            if i.concept_id in rework_ids and status0.get(i.concept_id) == "approved" and i.route_to != "fachdidaktiker":
                rework[i.concept_id][i.route_to].append(i)
            elif status0.get(i.concept_id) == "approved":
                on_approved[i.concept_id].append(i)
            else:
                rest.append(i)
        for cid, lst in on_approved.items():
            self.db.enqueue_human("concept", cid, "critic", fmt(lst)[:1000], kind="critic",
                                  payload={"issues": [x.model_dump() for x in lst]})

        struct = [i for i in rest if i.route_to == "fachdidaktiker" or i.concept_id not in status0]
        struct_ids = {id(i) for i in struct}
        per_concept: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
        for i in rest:
            if id(i) not in struct_ids:
                per_concept[i.concept_id][i.route_to].append(i)

        changed = False
        for cid, routes in rework.items():   # Schnellspur-Konzepte: im Hintergrund nacharbeiten
            fbd = {role: fmt(lst) for role, lst in routes.items()}
            fbd["rework"] = "1"
            self.db.query("UPDATE curriculum.concepts SET pending_feedback=%s WHERE id=%s",
                          (json.dumps(fbd, ensure_ascii=False), cid))
        if rework:
            self._parallel(self.rework_approved, [{"id": cid} for cid in rework], "rework")
            changed = True
        if struct:
            ok, _ = self.structure_block(code, block, feedback=fmt(struct), current=self._current_structure(bid),
                                         keep_approved=True)
            changed = ok
            if not ok:
                self.log(f"   ⛔ überarbeitete Struktur von {bid} gesperrt – bisherige Struktur bleibt")
        # Status nach einer evtl. Neustrukturierung neu lesen: nichts wiederbeleben, nie eine Station überspringen
        now = {c["id"]: c for c in self.db.concepts(block_id=bid, light=True)}
        for cid, routes in per_concept.items():
            row = now.get(cid)
            if not row or row["status"] not in STAGE_ORDER or row["status"] == "approved":
                continue
            fbd = {role: fmt(lst) for role, lst in routes.items() if lst}
            fbd.update({k: v for k, v in fb_load(row.get("pending_feedback")).items() if k == "all"})
            wanted = ("structured" if routes.get("niveau_kalibrierer") else
                      "calibrated" if routes.get("diagnostiker") else "diagnosed")
            start = min(wanted, row["status"], key=STAGE_ORDER.__getitem__)
            self.db.set_concept_status(cid, start, pending_feedback=json.dumps(fbd, ensure_ascii=False))
            changed = True
        if changed:
            new_ids = self.scope_ids(code, bid, ids) | ids
            self._parallel(self.process_concept,
                           [c for c in self.db.concepts(block_id=bid, light=True) if c["id"] in new_ids
                            and c["status"] in ("structured", "calibrated", "diagnosed")], "processing")
        return changed

    # ================================================================ Nacharbeit an freigegebenen Konzepten
    def rework_approved(self, light: dict) -> None:
        """Überarbeitung (z. B. nach Kritik + `review retry`), während das Konzept für Karo sichtbar bleibt.
        Gespeichert wird erst, wenn alle Stationen und die Schlussprüfung bestanden sind."""
        cid = light["id"]
        c = self.db.concept(cid)
        fbd = fb_load(c.get("pending_feedback"))
        fbd.pop("rework", None)
        roles = set(fbd) - {"all"}
        need_cal = not roles or "niveau_kalibrierer" in roles or "fachdidaktiker" in roles
        need_diag = need_cal or "diagnostiker" in roles
        need_vis = (need_diag or "visual_didaktiker" in roles) and self.cfg.p("visuals", True)
        work = dict(c)
        new: dict[str, Any] = {}
        self.log(f"   → Nacharbeit (bleibt sichtbar): {cid}")

        def step(stage, call, key, role):
            obj, ok = self.gated(entity_type="concept", entity_id=cid, stage=stage,
                                 grade_hint=str(c["target_grade"]), initial_feedback=fb_for(fbd, role),
                                 produce=lambda fb, _v: call(work, fb),
                                 to_content=lambda x: {"title": c["title"], **x.model_dump()})
            if ok:
                new[stage] = obj
                work[key] = obj.model_dump()
            return ok

        ok = True
        if need_cal:
            ok = step("calibration", self._calibration_call, "calibration", "niveau_kalibrierer")
        if ok and need_diag:
            ok = step("diagnostics", self._diagnostics_call, "diagnostics", "diagnostiker")
        if ok and need_vis:
            ok = step("visuals", self._visual_call, "visuals", "visual_didaktiker")
        if ok:
            bundle = self._bundle(work)
            if "calibration" in new:
                bundle.update({k: work["calibration"].get(k) for k in SLICES["cal"]})
            if "visuals" in new:
                bundle["visual_need"] = work["visuals"].get("visual_need")
            verdict = self.inspect("concept", cid, "final", str(c["target_grade"]), bundle, 1)
            ok = verdict.decision == "approved"
        if not ok:   # altes, freigegebenes Material bleibt; der Mensch sieht die Sperre in der Warteschlange
            self.log(f"   ⛔ Nacharbeit an {cid} nicht bestanden – bisherige Fassung bleibt sichtbar")
            self.db.query("UPDATE curriculum.concepts SET pending_feedback=NULL WHERE id=%s", (cid,))
            return
        if "calibration" in new:
            self.db.save_calibration(cid, new["calibration"], keep_status=True)
        if "diagnostics" in new:
            self.db.save_diagnostics(cid, new["diagnostics"], keep_status=True)
        if "visuals" in new:
            self.db.save_visuals(cid, new["visuals"], keep_status=True)
        self.db.query("UPDATE curriculum.concepts SET pending_feedback=NULL WHERE id=%s", (cid,))
        self._stat("reworked")
        self.log(f"   ✅ Nacharbeit übernommen: {cid}")

    # ================================================================ 7. Schlussprüfung
    def finalize_block(self, bid: str, ids: set[str] | None = None) -> None:
        todo = [c for c in self.db.concepts(block_id=bid, light=True)
                if c["status"] == "visualized" and (ids is None or c["id"] in ids)]
        if todo:
            self.log(f"   → Schlussprüfung durch den Inspektor: {len(todo)} Konzepte")
            self._parallel(self.finalize_concept, todo, "final")

    def finalize_concept(self, light: dict) -> None:
        cid = light["id"]
        state = {"c": self.db.concept(cid), "content": None}
        human_fb = fb_for(fb_load(state["c"].get("pending_feedback")), "all")

        def sliced(fb: str, part: str) -> str:
            """Auflagen + nur der eigene Teil des abgelehnten Entwurfs (statt dreimal das ganze Bündel)."""
            if not state["content"]:
                return fb
            return join_fb(fb, f"Dein Teil des abgelehnten Entwurfs:\n```json\n"
                               f"{draft_excerpt(state['content'], SLICES[part])}\n```")

        def produce(fb, verdict):
            c = state["c"]
            if fb:  # Auflagen des Inspektors (oder eines Menschen) an die zuständigen Rollen
                targets = route_findings(verdict) if verdict else {"all"}
                regrade = False
                if "meta" in targets:
                    draft = self.agents.call(
                        "fachdidaktiker", f"Überarbeite Titel, Beschreibung und Einordnung des Konzepts {cid}.",
                        {"konzept": {k: c.get(k) for k in ("id", "title", "description", "first_contact_grade",
                                                           "target_grade", "varies", "track", "learning_year",
                                                           "cefr")},
                         "prerequisites": self.db.prerequisites(cid), "order": c["sort_order"]},
                        ConceptDraft, entity_id=cid, feedback=sliced(fb, "meta"), stage="final",
                        meta={"concept": c, "meta_fix": True})
                    draft.id = cid
                    regrade = self.db.update_concept_meta(draft)   # neue Klassenstufe -> alles neu kalibrieren
                    c = self.db.concept(cid)
                full = regrade or "all" in targets or "cal" in targets
                if full:
                    self.db.save_calibration(cid, self._calibration_call(c, sliced(fb, "cal")), keep_status=True)
                    c = self.db.concept(cid)
                if full or "diag" in targets:
                    self.db.save_diagnostics(cid, self._diagnostics_call(c, sliced(fb, "diag")), keep_status=True)
                    c = self.db.concept(cid)
                if (full or "diag" in targets or "vis" in targets) and self.cfg.p("visuals", True):
                    self.db.save_visuals(cid, self._visual_call(c, sliced(fb, "vis")), keep_status=True)
                state["c"] = self.db.concept(cid)
            state["content"] = self._bundle(state["c"])
            return state["c"]

        _, ok = self.gated(entity_type="concept", entity_id=cid, stage="final",
                           grade_hint=str(state["c"]["target_grade"]), produce=produce,
                           initial_feedback=human_fb, include_draft=False, to_content=lambda _c: state["content"])
        if ok:
            self.db.set_concept_status(cid, "approved")
            self.db.resolve_errors("concept", cid)
            self._stat("approved")
            self.log(f"   ✅ freigegeben: {cid}")
        else:
            self.db.set_concept_status(cid, "blocked", keep_feedback=True)

    # ================================================================ Abschluss
    def report_missing_prerequisites(self, code: str) -> None:
        rows = self.db.query("""
            SELECT p.concept_id, p.prerequisite_id
            FROM curriculum.concept_prerequisites p
            JOIN curriculum.concepts c ON c.id = p.concept_id AND c.subject_code = %s AND c.status <> 'retired'
            LEFT JOIN curriculum.concepts x ON x.id = p.prerequisite_id AND x.status <> 'retired'
            LEFT JOIN curriculum.topic_blocks b ON b.id = split_part(p.prerequisite_id, '.', 1) || '.'
                                                         || split_part(p.prerequisite_id, '.', 2)
            WHERE x.id IS NULL AND (b.id IS NULL OR b.status IN ('reviewed', 'done'))""", (code,))
        for r in rows:
            self.db.enqueue_human("concept", r["concept_id"], "graph",
                                  f"Voraussetzung {r['prerequisite_id']} existiert nicht", kind="missing_prerequisite",
                                  payload={"prerequisite_id": r["prerequisite_id"]})
        # erledigte Meldungen automatisch schließen
        self.db.query("""UPDATE curriculum.human_queue q SET status='resolved', resolution='automatisch: behoben',
                                resolved_by='system', resolved_at=now()
                         WHERE q.status='open' AND q.kind='missing_prerequisite'
                           AND EXISTS (SELECT 1 FROM curriculum.concepts x
                                       WHERE x.id = q.payload->>'prerequisite_id' AND x.status <> 'retired')""")
        if rows:
            self._stat("missing_prerequisites", len(rows))
            self.log(f"⚠ {len(rows)} Voraussetzungen zeigen auf nicht existierende Konzepte (siehe review list)")

