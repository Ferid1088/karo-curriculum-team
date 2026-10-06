"""Die Fabrik: baut aus einem Katalog-Thema ein COMPLETE_TOPIC_PACKAGE.

Stufen sind fortsetzbar (package_stages): kommt ein Lauf in die Knie, laeuft
der naechste dort weiter, wo es aufhoerte. Jede Stufe liefert strukturiertes
JSON, wird deterministisch geprueft und kann gezielt neu gebaut werden, ohne
den Rest anzufassen (PART 80-83).

Ablauf pro Thema (PART 58): Geruest -> Fehlvorstellungen -> Strategien ->
Erklaerungen -> Visuals -> Aufgaben -> Vorlagen -> Reise-Politik kompilieren
-> Sackgassen- und Simulationspruefung -> Fachkontrolle -> Nachhaltigkeit ->
Kritik -> Inspektor -> Manifest -> Freigabetor.
"""
from __future__ import annotations

import time
import uuid
from typing import Any, Literal

from pydantic import BaseModel, Field

from . import completeness, taskgen
from .agents import AgentRunner, AgentFailed, BudgetExhausted
from .journey import check_deadends, compile_policy
from .package_schema import (
    PACKAGE_CONTRACT_VERSION, CompleteTopicPackage, DetourPlan,
    ExplanationVariant, JourneyPolicy, LevelSpec, MisconceptionModel,
    PrerequisiteGraph, TaskSpec, TaskTemplate, TransitionRule, VisualAsset,
    VisualKind,
)
from .simulate_pkg import simulate_all, simulation_findings

# ---------------------------------------------------------------- Stufen und Rollen-Schemas

STAGE_ORDER: tuple[str, ...] = (
    "skeleton", "misconceptions", "strategies", "explanations",
    "visuals", "images", "tasks", "templates", "journey",
    "subject_check", "retention", "rubrics", "critic", "inspector",
    "simulate", "control", "manifest",
)


class SkeletonOut(BaseModel):
    target_competencies: list[str] = Field(min_length=1)
    prerequisite_graph: PrerequisiteGraph = Field(default_factory=PrerequisiteGraph)
    competency_ladder: list[LevelSpec] = Field(min_length=1)
    teaching_strategies: dict[str, list[str]] = Field(default_factory=dict)


class MisconceptionsOut(BaseModel):
    misconception_model: list[MisconceptionModel] = Field(default_factory=list)


class StrategiesOut(BaseModel):
    teaching_strategies: dict[str, list[str]] = Field(default_factory=dict)


class ExplanationsOut(BaseModel):
    explanations: list[ExplanationVariant] = Field(min_length=1)


class VisualsOut(BaseModel):
    visual_assets: list[VisualAsset] = Field(default_factory=list)
    visual_need: VisualKind = "NO_VISUAL_NEEDED"
    visual_na_reason: str = ""


class TasksOut(BaseModel):
    # min_length: eine leere Aufgabenliste ist kein "fertig", sondern ein
    # stilles Scheitern — die Stufe gilt dann als fehlgeschlagen und geht
    # in die Reparaturschleife, statt ein halbes Paket durchzuwinken.
    tasks: list[TaskSpec] = Field(min_length=1)


class TemplatesOut(BaseModel):
    task_templates: list[TaskTemplate] = Field(default_factory=list)


class JourneyOut(BaseModel):
    rules: list[TransitionRule] = Field(default_factory=list)
    detours: list[DetourPlan] = Field(default_factory=list)
    role_order: list[str] = Field(default_factory=list)
    max_attempts_per_task: int = 3
    max_unknown_per_task: int = 2
    max_resume_cycles: int = 3
    max_no_progress: int = 14


class IssuesOut(BaseModel):
    """Fachkontrolle / Kritik: Befunde mit Zieladresse."""
    issues: list[dict[str, Any]] = Field(default_factory=list)
    verdict: str = "PASS"          # PASS|FAIL beim Fachexperten/Inspektor


class CriticOut(BaseModel):
    issues: list[dict[str, Any]] = Field(default_factory=list)


class RubricContractOut(BaseModel):
    """Bewertungsvertrag fuer eine Aufgabe (PART 46-49): der Rubrik-Ingenieur
    liefert Konzepte, Varianten, Fehlvorstellungen, Widersprueche,
    Nichtwissen-Marker und Normalisierung – die Laufzeit braucht kein Modell."""
    task_id: str
    required_concepts: list[Any] = Field(min_length=1)
    optional_concepts: list[Any] = Field(default_factory=list)
    min_required: int | None = None
    partial_min: int = 1
    misconceptions: list[dict[str, Any]] = Field(default_factory=list)
    contradictions: list[dict[str, Any]] = Field(default_factory=list)
    unknown_markers: list[str] = Field(default_factory=list)
    normalization: list[str] = Field(default_factory=list)
    clarification: dict[str, Any] | None = None
    sample_answer: str | None = None


class RubricsOut(BaseModel):
    rubrics: list[RubricContractOut] = Field(default_factory=list)


class ControlFinding(BaseModel):
    """Ein Befund des Vollstaendigkeits-Kontrolleurs – immer mit Zieladresse
    (`stage`), damit die Fabrik lokal repariert statt neu baut (PART 72)."""
    component: str
    severity: str = "minor"        # blocker|major|minor
    detail: str = ""
    stage: str = ""                # zustaendige Fabrikstufe, z. B. 'rubrics'


class ControlOut(BaseModel):
    """Endkontrolle: kein Inhalt, nur Befund. PASS -> Kandidat fuer
    READY_COMPLETE; REPAIR_REQUIRED -> lokale Reparatur; BLOCKED -> Stopp."""
    verdict: Literal["PASS", "REPAIR_REQUIRED", "BLOCKED"] = "PASS"
    findings: list[ControlFinding] = Field(default_factory=list)


#: Welche Rolle eine Stufe bedient und welches Schema sie fuellt.
STAGE_ROLE: dict[str, tuple[str, type[BaseModel]]] = {
    "skeleton":      ("kompetenz_architekt", SkeletonOut),
    "misconceptions": ("fehlvorstellungs_analytiker", MisconceptionsOut),
    "strategies":    ("didaktik_designer", StrategiesOut),
    "explanations":  ("erklaerautor", ExplanationsOut),
    "visuals":       ("visueller_lerndesigner", VisualsOut),
    "tasks":         ("aufgaben_designer", TasksOut),
    "templates":     ("vorlagen_ingenieur", TemplatesOut),
    "journey":       ("lernreise_architekt", JourneyOut),
    "subject_check": ("fachexperte", IssuesOut),
    "retention":     ("pruefungs_designer", TasksOut),
    "rubrics":       ("rubrik_ingenieur", RubricsOut),
    "critic":        ("curriculum_kritiker", CriticOut),
    "inspector":     ("kinderrechts_inspektor", IssuesOut),
    "control":       ("vollstaendigkeits_kontrolleur", ControlOut),
}

#: Reparaturschleife: wo ein Befund hin muss (PART 72). Fallback = Kritiker.
ROUTE_TO_STAGE: dict[str, str] = {
    "kompetenz_architekt": "skeleton", "lernreise_architekt": "journey",
    "fachexperte": "tasks", "didaktik_designer": "strategies",
    "erklaerautor": "explanations", "aufgaben_designer": "tasks",
    "vorlagen_ingenieur": "templates", "fehlvorstellungs_analytiker": "misconceptions",
    "visueller_lerndesigner": "visuals", "pruefungs_designer": "retention",
    "curriculum_kritiker": "critic", "rubrik_ingenieur": "rubrics",
    "kinderrechts_inspektor": "inspector",
    "vollstaendigkeits_kontrolleur": "control",
}

#: Schluesselwoerter deterministischer Befunde -> Reparaturstufe (PART 72).
FINDING_ROUTES: tuple[tuple[str, str], ...] = (
    ("Vorlage", "templates"), ("Rubrik", "rubrics"),
    ("Sackgasse", "journey"), ("Umweg", "journey"), ("Rueckweg", "journey"),
    ("Rolle", "journey"), ("Fehlvorstellung", "misconceptions"),
    ("Erklaerung", "explanations"), ("Visual", "visuals"), ("Bild", "visuals"),
    ("Simulation", "journey"), ("Profil", "journey"), ("Klaerung", "tasks"),
)


class Factory:
    """Baut Pakete. Nichts hier fragt zur Laufzeit ein Modell – die
    Denkarbeit passiert nur beim Bau."""

    def __init__(self, *, cfg, provider, db, team=None, run_id: str | None = None):
        self.cfg = cfg
        self.db = db
        self.provider = provider
        self.run_id = run_id or str(uuid.uuid4())
        self.runner = AgentRunner(cfg=cfg, provider=provider, db=db,
                                  run_id=self.run_id, team=team)

    # ------------------------------------------------------------ DB-Helfer
    def _upsert_package(self, item: dict, status: str = "BUILDING") -> None:
        self.db.query(
            """INSERT INTO curriculum.complete_packages(topic_id, status, contract_version)
               VALUES (%s,%s,%s)
               ON CONFLICT (topic_id) DO UPDATE SET status=EXCLUDED.status, updated_at=now()""",
            (item["id"], status, PACKAGE_CONTRACT_VERSION))

    def _stage_get(self, topic_id: str, stage: str) -> dict | None:
        r = self.db.one(
            "SELECT status, result FROM curriculum.package_stages WHERE topic_id=%s AND stage=%s",
            (topic_id, stage))
        return r if r else None

    def _stage_set(self, topic_id: str, stage: str, status: str,
                   result: dict | None = None, detail: str = "") -> None:
        from psycopg.types.json import Jsonb
        self.db.query(
            """INSERT INTO curriculum.package_stages(topic_id, stage, status, result, detail, run_id)
               VALUES (%s,%s,%s,%s,%s,%s)
               ON CONFLICT (topic_id, stage) DO UPDATE SET status=EXCLUDED.status,
                   result=EXCLUDED.result, detail=EXCLUDED.detail, run_id=EXCLUDED.run_id,
                   updated_at=now()""",
            (topic_id, stage, status, Jsonb(result) if result is not None else None,
             detail[:2000], self.run_id))

    def invalidate_stages(self, topic_id: str, stages: list[str]) -> None:
        self.db.query(
            """UPDATE curriculum.package_stages SET status='invalidated', updated_at=now()
               WHERE topic_id=%s AND stage = ANY(%s)""", (topic_id, stages))

    # ------------------------------------------------------------ Stufenlauf
    def _run_stage(self, pkg_acc: dict, item: dict, stage: str, *,
                   feedback: str | None = None, extra_task: str = "") -> BaseModel:
        """Ruft die Rolle der Stufe auf, prueft gegen das Schema, legt das
        Ergebnis ab. Wirft AgentFailed bei dauerhaft ungueltigen Antworten."""
        role, schema = STAGE_ROLE[stage]
        topic_id = item["id"]
        self._stage_set(topic_id, stage, "running")
        payload = {"thema": {"id": item["id"], "title": item["title"],
                             "description": item.get("description", ""),
                             "subject_code": item["subject_code"], "grade": item.get("grade"),
                             "path": item.get("path") or []},
                   "bisheriges_paket": _compact_acc(pkg_acc, stage)}
        task = f"Baue die Stufe '{stage}' fuer das Thema '{item['title']}' ({item['subject_code']}, Klasse {item.get('grade')})."
        if extra_task:
            task += " " + extra_task
        try:
            out = self.runner.call(role, task, payload, schema,
                                   entity_id=topic_id, feedback=feedback,
                                   stage=stage)
        except AgentFailed as exc:
            self._stage_set(topic_id, stage, "failed", detail=str(exc))
            raise
        self._stage_set(topic_id, stage, "done", result=out.model_dump(mode="json"))
        return out

    def _concept_id(self, item: dict) -> str:
        """Welcher `curriculum.concepts`-Schluessel gehoert zum Katalogthema?

        Liegt schon ein freigegebenes Konzept zum Thema vor, muss das Paket
        denselben Schluessel tragen — sonst findet die Brücke
        (`karo_bridge.find_package`) das Paket beim Lektionsauftrag nicht.
        Reihenfolge: exakte id, dann Titel+Fach, dann der Slug als neuer
        Schluessel."""
        row = self.db.one("SELECT id FROM curriculum.concepts WHERE id=%s",
                          (item["id"],))
        if row:
            return row["id"]
        row = self.db.one("""SELECT id FROM curriculum.concepts
                             WHERE subject_code=%s AND lower(title)=lower(%s)
                               AND status='approved'
                             ORDER BY id LIMIT 1""",
                          (item["subject_code"], item["title"]))
        if row:
            return row["id"]
        return f"{item['subject_code']}.{slug(item['title'])}"

    # ------------------------------------------------------------ Der Bau
    def build_topic(self, item: dict, *, mode: str = "missing",
                    components: list[str] | None = None,
                    seed: int = 0) -> dict:
        """Baut/vervollstaendigt das Paket eines Katalog-Themas.

        mode: 'missing' (nur fehlende Stufen), 'repair' (fehlgeschlagene +
        invalidierte), 'regenerate' (nur die genannten Stufen),
        'full_rebuild' (alles neu)."""
        topic_id = item["id"]
        self._upsert_package(item, "BUILDING")
        acc = self._load_acc(topic_id)          # Ergebnisse fertiger Stufen
        target = set(STAGE_ORDER if mode in ("missing", "repair") else components or STAGE_ORDER)
        if mode == "regenerate" and components:
            self.invalidate_stages(topic_id, components)
            target = set(components) | {"simulate", "manifest"}
        if mode == "full_rebuild":
            self.invalidate_stages(topic_id, list(STAGE_ORDER))
            target = set(STAGE_ORDER)

        failures: list[str] = []
        for stage in STAGE_ORDER:
            if stage not in target or stage in ("simulate", "control", "manifest"):
                continue
            done = self._stage_get(topic_id, stage)
            if mode == "missing" and done and done["status"] == "done":
                continue
            if mode == "repair" and done and done["status"] == "done":
                continue
            if stage == "images":
                try:
                    self._run_images(acc, item)
                except (AgentFailed, BudgetExhausted) as exc:
                    failures.append(f"images: {exc}")
                continue
            if stage not in STAGE_ROLE:
                continue
            try:
                out = self._run_stage(acc, item, stage)
            except BudgetExhausted:
                raise                      # fortsetzbar: Stufen bleiben stehen
            except AgentFailed as exc:
                failures.append(f"{stage}: {exc}")
                continue
            self._merge(acc, stage, out)

        pkg = self._assemble(item, acc)
        # -------- deterministische Qualitaetstore (PART 62-66)
        qe = pkg.quality_evidence
        qe.template_check, tpl_findings = self._check_templates(pkg)
        rubric_findings = self._check_rubrics(pkg)
        qe.rubric_adversarial = "PASS" if not rubric_findings else "FAIL"
        self._apply_journey(pkg, acc)
        pkg.journey_policy = compile_policy(pkg)
        qe.dead_end_check = "PASS" if not (dd := check_deadends(pkg)) else "FAIL"
        qe.return_paths = "PASS" if all(d.return_target or d.resume_level is not None
                                        for d in pkg.journey_policy.detours) else "FAIL"
        qe.prerequisite_cycles = "PASS" if not self._prereq_cycle(pkg) else "FAIL"
        qe.subject_validation = acc.get("subject_check_verdict", "PENDING")
        qe.validator = acc.get("inspector_verdict", "PENDING")
        qe.critic_findings = len(acc.get("critic_issues", []))
        # abgelehnte/fehlgeschlagene Illustrationen mindern die Vollstaendigkeit
        # (Manifest: illustrative_images=PARTIAL), blockieren aber nicht die
        # Kernreise – kein FAIL-Blocker.
        qe.visual_qa = "PENDING" if acc.get("image_findings") else "PASS"
        qe.notes += tpl_findings + rubric_findings + dd \
            + list(acc.get("image_findings", [])) \
            + list(acc.get("rubric_apply_errors", []))

        # -------- Reparaturschleife ueber deterministische Befunde (PART 72)
        rounds = int(self.cfg.p("factory_repair_rounds", 3))
        findings = tpl_findings + rubric_findings + dd
        for runde in range(rounds):
            if not findings:
                break
            feedback = "\n".join(f"- {f}" for f in findings)
            repaired = self._repair(acc, item, findings, feedback)
            if not repaired:
                break
            pkg = self._assemble(item, acc)
            qe = pkg.quality_evidence
            qe.template_check, tpl_findings = self._check_templates(pkg)
            rubric_findings = self._check_rubrics(pkg)
            qe.rubric_adversarial = "PASS" if not rubric_findings else "FAIL"
            self._apply_journey(pkg, acc)
            pkg.journey_policy = compile_policy(pkg)
            dd = check_deadends(pkg)
            qe.dead_end_check = "PASS" if not dd else "FAIL"
            findings = tpl_findings + rubric_findings + dd
            qe.notes += findings

        # -------- adversariale Simulation (PART 94-99): kein Modell noetig
        sim = simulate_all(pkg, seed=seed)
        qe.simulations = {p: r.outcome for p, r in sim.items()}
        sim_findings = simulation_findings(sim)
        self._stage_set(topic_id, "simulate", "done",
                        result={"outcomes": qe.simulations, "findings": sim_findings})
        qe.notes += sim_findings

        # -------- Herkunft setzen, bevor Manifest + Tor laufen (PART 38)
        pkg.provenance.provider = self.provider.name
        pkg.provenance.created_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        pkg.provenance.agent_role = "factory"
        pkg.provenance.contract_version = PACKAGE_CONTRACT_VERSION
        pkg.provenance.bulk_job = acc.get("bulk_job_id")
        pkg.provenance.run_id = self.run_id
        pkg.provenance.git_sha = _git_sha()

        # -------- Vollstaendigkeits-Kontrolleur (PART 67-69): letzte Stufe.
        # Erzeugt keinen Inhalt, bewertet Manifest + Simulation + Befunde.
        # REPAIR_REQUIRED -> nur die genannten Stufen erneut (lokale Reparatur).
        ctrl_rounds = 0
        while True:
            pkg.coverage_manifest = completeness.manifest(pkg)
            ctrl = self._run_control(item, acc, pkg)
            qe.control = ctrl.verdict
            if ctrl.verdict != "REPAIR_REQUIRED" or ctrl_rounds >= 1:
                break
            ctrl_rounds += 1
            stages = self._control_routes(ctrl)
            if not stages:
                qe.notes.append("Kontrolleur meldet REPAIR_REQUIRED ohne "
                                "routbare Befunde")
                break
            for st in stages:
                if st in ("simulate", "control", "manifest") or st not in STAGE_ROLE:
                    continue
                try:
                    out = self._run_stage(acc, item, st,
                                          feedback=self._control_feedback(ctrl, st))
                    self._merge(acc, st, out)
                except (AgentFailed, BudgetExhausted) as exc:
                    qe.notes.append(f"Kontrolle-Reparatur {st}: {exc}")
            pkg = self._assemble(item, acc)
            qe = pkg.quality_evidence
            self._apply_journey(pkg, acc)
            pkg.journey_policy = compile_policy(pkg)
            dd = check_deadends(pkg)
            qe.dead_end_check = "PASS" if not dd else "FAIL"
            sim = simulate_all(pkg, seed=seed)
            qe.simulations = {p: r.outcome for p, r in sim.items()}
            self._stage_set(topic_id, "simulate", "done",
                            result={"outcomes": qe.simulations,
                                    "findings": simulation_findings(sim)})

        # -------- Manifest + Freigabetor
        pkg.coverage_manifest = completeness.manifest(pkg)
        status, blocking = completeness.release_gate(pkg)
        if failures:
            status = "PARTIAL" if status.startswith("READY") else status
            blocking += failures
        if acc.get("inspector_verdict") == "FAIL":
            status = "BLOCKED"

        from psycopg.types.json import Jsonb
        self.db.query(
            """UPDATE curriculum.complete_packages SET status=%s, content=%s, manifest=%s,
                      coverage=%s, fail_reason=%s, concept_id=%s, updated_at=now()
               WHERE topic_id=%s""",
            (status, Jsonb(pkg.model_dump(mode="json")),
             Jsonb(pkg.coverage_manifest.model_dump(mode="json")),
             Jsonb(pkg.coverage_manifest.coverage),
             "; ".join(blocking)[:2000] or None,
             pkg.concept_identity.concept_id, topic_id))
        self._stage_set(topic_id, "manifest", "done",
                        result={"status": status, "blocking": blocking})
        return {"topic_id": topic_id, "status": status, "blocking": blocking,
                "simulations": qe.simulations, "failures": failures}

    # ------------------------------------------------------------ Zusammenfuehren
    def _load_acc(self, topic_id: str) -> dict:
        """Alle fertigen Stufenergebnisse eines Themas laden (fortsetzbar)."""
        acc: dict[str, Any] = {}
        for r in self.db.query(
                "SELECT stage, result FROM curriculum.package_stages "
                "WHERE topic_id=%s AND status='done'", (topic_id,)):
            res = r["result"]
            if res is None:
                continue
            stage = r["stage"]
            if stage == "retention":
                acc["tasks_retention"] = res.get("tasks", [])
            elif stage in ("subject_check", "inspector"):
                acc[f"{stage}_verdict"] = res.get("verdict", "PENDING")
                acc[f"{stage}_issues"] = res.get("issues", [])
            elif stage == "critic":
                acc["critic_issues"] = res.get("issues", [])
            elif stage == "control":
                acc["control_verdict"] = res.get("verdict", "PENDING")
                acc["control_findings"] = res.get("findings", [])
            else:
                acc[stage_key(stage)] = res
        return acc

    def _merge(self, acc: dict, stage: str, out: BaseModel) -> None:
        data = out.model_dump(mode="json")
        if stage == "retention":
            acc.setdefault("tasks_retention", []).extend(data.get("tasks", []))
        elif stage in ("subject_check", "inspector"):
            acc[stage + "_verdict"] = data.get("verdict", "PENDING")
            acc.setdefault(stage + "_issues", []).extend(data.get("issues", []))
        elif stage == "critic":
            acc["critic_issues"] = data.get("issues", [])
        elif stage == "control":
            acc["control_verdict"] = data.get("verdict", "PENDING")
            acc["control_findings"] = data.get("findings", [])
        else:
            acc[stage_key(stage)] = data

    def _assemble(self, item: dict, acc: dict) -> CompleteTopicPackage:
        """Stufenergebnisse -> ein Paket. Fehlende Stufen bleiben leer –
        das Manifest kennzeichnet sie ehrlich."""
        from .package_schema import CatalogRef, ConceptIdentity
        sk = acc.get("skeleton", {})
        mis = acc.get("misconceptions", {})
        strat = acc.get("strategies", {})
        expl = acc.get("explanations", {})
        vis = acc.get("visuals", {})
        tasks = list(acc.get("tasks", {}).get("tasks", []))
        tasks += acc.get("tasks_retention", [])
        cid = self._concept_id(item)
        pkg = CompleteTopicPackage(
            catalog_alignment=CatalogRef(
                item_id=item["id"], path=item.get("path") or [],
                framework=item.get("framework", ""), region=item.get("region", ""),
                school_type=item.get("school_type", ""), grade=item.get("grade") or 0,
                source=item.get("source", ""), source_version=item.get("source_version", ""),
                source_reference=item.get("source_reference", "")),
            concept_identity=ConceptIdentity(
                concept_id=cid, title=item["title"], subject_code=item["subject_code"],
                description=item.get("description", ""),
                target_grade=item.get("grade") or 1),
            target_competencies=sk.get("target_competencies") or [item["title"]],
            prerequisite_graph=sk.get("prerequisite_graph") or {},
            competency_ladder=sk.get("competency_ladder") or [
                {"level_id": 0, "goal": item["title"], "next_level": None}],
            tasks=[TaskSpec(**t) if isinstance(t, dict) else t for t in tasks],
            misconception_model=[MisconceptionModel(**m) if isinstance(m, dict) else m
                                 for m in mis.get("misconception_model", [])],
            teaching_strategies=strat.get("teaching_strategies", {}),
            explanations=[ExplanationVariant(**e) if isinstance(e, dict) else e
                          for e in expl.get("explanations", [])],
            task_templates=[TaskTemplate(**t) if isinstance(t, dict) else t
                            for t in acc.get("templates", {}).get("task_templates", [])],
            visual_assets=[VisualAsset(**a) if isinstance(a, dict) else a
                           for a in vis.get("visual_assets", [])],
            visual_need=_norm_visual_need(vis),
            visual_na_reason=vis.get("visual_na_reason", ""),
        )
        self._apply_rubrics(pkg, acc)
        return pkg

    @staticmethod
    def _apply_rubrics(pkg: CompleteTopicPackage, acc: dict) -> None:
        """Rubrik-Vertraege in Aufgaben einbauen: Freitext-/Textantworten
        werden zu concept_rubric, vorhandene Rubriken um neue Felder
        ergaenzt. Der Vertrag traegt die Bewertung, nicht das Modell."""
        from .answers import AnswerConceptRubric
        contracts = {r["task_id"]: r for r in acc.get("rubrics", {}).get("rubrics", [])
                     if isinstance(r, dict) and r.get("task_id")}
        for t in pkg.tasks:
            contract = contracts.get(t.task_id)
            if not contract:
                continue
            data = {k: v for k, v in contract.items()
                    if k != "task_id" and v not in (None, [], "")}
            cur = t.answer
            cur_spec = cur.model_dump() if hasattr(cur, "model_dump") else (cur or {})
            try:
                if cur_spec.get("type") == "concept_rubric":
                    merged = dict(cur_spec)
                    for k, v in data.items():
                        if merged.get(k) in (None, [], "") or k in (
                                "misconceptions", "contradictions"):
                            merged[k] = v
                    t.answer = AnswerConceptRubric(**merged)
                elif cur_spec.get("type") in ("free_text", "text") or cur is None:
                    if data.get("required_concepts"):
                        t.answer = AnswerConceptRubric(type="concept_rubric", **data)
            except Exception as exc:
                acc.setdefault("rubric_apply_errors", []).append(
                    f"Rubrik {t.task_id}: {exc}")

    def _apply_journey(self, pkg: CompleteTopicPackage, acc: dict) -> None:
        """Vorentwurf des Lernreise-Architekten in die Politik uebernehmen –
        der Compiler schliesst danach Luecken (PART 62)."""
        j = acc.get("journey") or {}
        pol = pkg.journey_policy
        pol.rules = [TransitionRule(**r) if isinstance(r, dict) else r
                     for r in j.get("rules", [])]
        pol.detours = [DetourPlan(**d) if isinstance(d, dict) else d
                       for d in j.get("detours", [])]
        if j.get("role_order"):
            pol.role_order = j["role_order"]
        for f in ("max_attempts_per_task", "max_unknown_per_task",
                  "max_resume_cycles", "max_no_progress"):
            if j.get(f):
                setattr(pol, f, int(j[f]))

    # ------------------------------------------------------------ Deterministische Pruefungen
    def _check_templates(self, pkg: CompleteTopicPackage) -> tuple[str, list[str]]:
        findings: list[str] = []
        for t in pkg.task_templates:
            findings += taskgen.validate_template(t, sample=200)
            if taskgen.variant_capacity(t) < 1:
                findings.append(f"Vorlage {t.template_id}: keine erzeugbare Variante")
        return ("PASS" if not findings else "FAIL"), findings

    def _prereq_cycle(self, pkg: CompleteTopicPackage) -> list[str]:
        """Zyklus im internen Voraussetzungsgraph (Kanten, die auf bekannte
        Konzept-IDs zeigen)."""
        graph: dict[str, list[str]] = {pkg.concept_identity.concept_id:
                                       [e.target_id for e in pkg.prerequisite_graph.edges]}
        seen: set[str] = set()
        stack: set[str] = set()

        def dfs(n: str) -> list[str] | None:
            stack.add(n)
            for m in graph.get(n, []):
                if m == n or m in stack:
                    return [n, m]
                if m not in seen and m in graph:
                    r = dfs(m)
                    if r:
                        return r
            stack.discard(n)
            seen.add(n)
            return None
        return dfs(pkg.concept_identity.concept_id) or []

    def _check_rubrics(self, pkg: CompleteTopicPackage) -> list[str]:
        """Adversariale Batterie auf jede concept_rubric-Antwort (Phase 3):
        der Vertrag muss Grenzfaelle ohne Modell richtig einordnen."""
        from .rubrics import run_battery
        findings: list[str] = []
        for t in pkg.tasks:
            a = t.answer
            spec = a.model_dump() if hasattr(a, "model_dump") else (a or {})
            if spec.get("type") == "concept_rubric":
                findings += run_battery(spec, task_id=t.task_id)
        return findings

    def _run_control(self, item: dict, acc: dict,
                     pkg: CompleteTopicPackage) -> ControlOut:
        """Der Vollstaendigkeits-Kontrolleur sieht den IST-Stand: Manifest,
        Simulationen, Qualitaetsnachweise, Stufenfehler. Erzeugt keinen
        Inhalt – nur PASS / REPAIR_REQUIRED / BLOCKED mit Zieladressen."""
        topic_id = item["id"]
        self._stage_set(topic_id, "control", "running")
        qe = pkg.quality_evidence
        payload = {
            "thema": {"id": item["id"], "title": item["title"],
                      "subject_code": item["subject_code"], "grade": item.get("grade")},
            "manifest": pkg.coverage_manifest.model_dump(mode="json"),
            "simulationen": qe.simulations,
            "nachweise": {f: getattr(qe, f) for f in
                          ("dead_end_check", "template_check", "return_paths",
                           "prerequisite_cycles", "subject_validation",
                           "validator", "rubric_adversarial", "visual_qa")},
            "kritiker_befunde": acc.get("critic_issues", []),
            "hinweise": qe.notes[-20:],
        }
        try:
            out = self.runner.call(
                "vollstaendigkeits_kontrolleur",
                f"Kontrolliere das Paket zu '{item['title']}' vollstaendig. "
                "Bewerte, ob ein Kind aus jedem plausiblen Wissensstand "
                "gefuehrt werden kann. Melde PASS, REPAIR_REQUIRED oder "
                "BLOCKED; jeder Befund braucht seine zustaendige Stufe.",
                payload, ControlOut, entity_id=topic_id, stage="control")
        except AgentFailed as exc:
            self._stage_set(topic_id, "control", "failed", detail=str(exc))
            return ControlOut(verdict="BLOCKED",
                              findings=[ControlFinding(component="control",
                                                       severity="blocker",
                                                       detail=str(exc))])
        self._stage_set(topic_id, "control", "done",
                        result=out.model_dump(mode="json"))
        return out

    def _run_images(self, acc: dict, item: dict) -> None:
        """Bildpipeline als Fabrikstufe (PART 53-57): nur ILLUSTRATIVE_IMAGE-
        Assets laufen hier – Praezisionsvisuals bleiben beim deterministischen
        Renderer. Fortsetzbar ueber package_stages."""
        topic_id = item["id"]
        self._stage_set(topic_id, "images", "running")
        from . import images as img
        try:
            res = img.generate_image_assets(self, item, acc)
        except Exception as exc:
            self._stage_set(topic_id, "images", "failed", detail=str(exc))
            raise AgentFailed(f"images: {exc}") from exc
        if res.get("findings"):
            acc.setdefault("image_findings", []).extend(res["findings"])
        acc["images"] = res
        self._stage_set(topic_id, "images", "done", result=res)

    @staticmethod
    def _control_routes(ctrl: ControlOut) -> list[str]:
        """Befunde des Kontrolleurs -> eindeutige Reparaturstufen."""
        return list(dict.fromkeys(f.stage for f in ctrl.findings if f.stage))

    @staticmethod
    def _control_feedback(ctrl: ControlOut, stage: str) -> str:
        return "\n".join(f"- [{f.severity}] {f.component}: {f.detail}"
                         for f in ctrl.findings if f.stage == stage)

    def _repair(self, acc: dict, item: dict, findings: list[str],
                feedback: str) -> bool:
        """Deterministische Befunde -> betroffene Stufe neu aufrufen (PART 72).
        Lokal, nicht das ganze Paket. Gibt True, wenn etwas geaendert wurde."""
        stages = list(dict.fromkeys(
            stage for key, stage in FINDING_ROUTES
            if any(key in f for f in findings)))
        repaired = False
        for stage in stages:
            try:
                out = self._run_stage(acc, item, stage, feedback=feedback)
                self._merge(acc, stage, out)
                repaired = True
            except (AgentFailed, BudgetExhausted):
                continue
        return repaired


def stage_key(stage: str) -> str:
    return {"misconceptions": "misconceptions"}.get(stage, stage)


_VISUAL_KINDS = {"DETERMINISTIC_DIAGRAM", "DATA_CHART",
                 "ILLUSTRATIVE_IMAGE", "NO_VISUAL_NEEDED"}


def _norm_visual_need(vis: dict) -> str:
    """`visual_need` der Stufe auf die Paket-Enum bringen.

    Stufen-Ergebnisse, die vor der Enum-Vorgabe liefen, sagten z. B.
    „REQUIRED" — die Paket-Enum will die ART des Bedarfs, nicht dessen
    Vorhandensein. Deterministisch abgeleitet: staerkster vorhandener
    Asset-Typ, sonst NO_VISUAL_NEEDED. Kein Modellaufruf, kein Eingriff
    in den Inhalt."""
    v = vis.get("visual_need")
    if v in _VISUAL_KINDS:
        return v
    arten = {a.get("visual_type") for a in vis.get("visual_assets", [])
             if a.get("visual_type") in _VISUAL_KINDS}
    if "ILLUSTRATIVE_IMAGE" in arten:
        return "ILLUSTRATIVE_IMAGE"
    if "DATA_CHART" in arten:
        return "DATA_CHART"
    if "DETERMINISTIC_DIAGRAM" in arten:
        return "DETERMINISTIC_DIAGRAM"
    return "NO_VISUAL_NEEDED"


def slug(title: str) -> str:
    import re
    import unicodedata
    s = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode().upper()
    return re.sub(r"[^A-Z0-9]+", "_", s).strip("_")[:40] or "THEMA"


def _git_sha() -> str:
    """Herkunftsnachweis: welcher Code das Paket gebaut hat (PART 38)."""
    import subprocess
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                              capture_output=True, text=True,
                              timeout=5).stdout.strip()
    except Exception:
        return ""


def _compact_acc(acc: dict, stage: str) -> dict:
    """Was die Rolle vom bisherigen Paket sieht – kompakt, kein Ballast."""
    keep = {
        "tasks": ["skeleton", "misconceptions"],
        "rubrics": ["skeleton", "misconceptions", "tasks", "tasks_retention"],
        "templates": ["skeleton", "misconceptions"],
        "journey": ["skeleton", "misconceptions", "explanations"],
        "subject_check": list(acc.keys()),
        "critic": list(acc.keys()),
        "inspector": list(acc.keys()),
        "explanations": ["skeleton", "misconceptions"],
        "visuals": ["skeleton"],
        "strategies": ["skeleton", "misconceptions"],
        "retention": ["skeleton", "misconceptions"],
    }.get(stage, ["skeleton"])
    return {k: acc[k] for k in keep if k in acc}


# ---------------------------------------------------------------- Sammelauftraege (PART 77-83)
def estimate_job(topics: list[dict], cfg) -> dict:
    """Kostenvorschau: Rollenaufrufe und grobe Token/Kosten-Schaetzung.
    Zeigt vor der Bestaetigung, was der Lauf ungefaehr kostet (PART 103)."""
    calls_per = len([s for s in STAGE_ORDER if s in STAGE_ROLE])
    budgets = {r: cfg.token_budget_for(r) or 0 for r, _ in STAGE_ROLE.values()}
    tok_per = sum(budgets.values()) or calls_per * 12000
    price_in = float(cfg.raw.get("cost_per_mtok_in", 3.0))
    price_out = float(cfg.raw.get("cost_per_mtok_out", 15.0))
    n = len(topics)
    est_out = n * tok_per
    est_in = est_out * 2      # Auftragsdaten groesser als die Ausgabe
    return {"topics": n, "calls_estimated": n * calls_per,
            "tokens_in_estimated": int(est_in), "tokens_out_estimated": int(est_out),
            "cost_usd_estimated": round(est_in / 1e6 * price_in + est_out / 1e6 * price_out, 2)}


def create_bulk_job(db, scope, topics: list[dict], *, mode: str = "missing",
                    confirmed_by: str | None = None, estimate: dict | None = None) -> int:
    """Sammelauftrag anlegen. Ohne `confirmed_by` bleibt er 'planned' –
    die Fabrik rührt ihn erst nach menschlicher Bestaetigung an (PART 103)."""
    from psycopg.types.json import Jsonb
    scope_d = scope.__dict__ if hasattr(scope, "__dict__") else dict(scope)
    job = db.one(
        """INSERT INTO curriculum.bulk_jobs(scope, mode, estimate, confirmed_by, status)
           VALUES (%s,%s,%s,%s,%s) RETURNING id""",
        (Jsonb(scope_d), mode, Jsonb(estimate or {}), confirmed_by,
         "planned" if not confirmed_by else "running"))
    for t in topics:
        db.query("""INSERT INTO curriculum.bulk_job_items(job_id, topic_id, status)
                    VALUES (%s,%s,'pending') ON CONFLICT DO NOTHING""",
                 (job["id"], t["id"]))
    return job["id"]


def run_bulk_job(factory: Factory, job_id: int, *, limit: int | None = None) -> dict:
    """Abarbeiten – fortsetzbar: bereits erledigte Themen werden
    uebersprungen; fehlgeschlagene bleiben mit Grund stehen."""
    db = factory.db
    job = db.one("SELECT * FROM curriculum.bulk_jobs WHERE id=%s", (job_id,))
    if not job:
        raise ValueError(f"Sammelauftrag {job_id} unbekannt")
    if job["status"] == "planned":
        raise RuntimeError("Sammelauftrag ist nicht bestaetigt (confirmed_by fehlt)")
    db.query("UPDATE curriculum.bulk_jobs SET status='running', started_at=coalesce(started_at, now()) WHERE id=%s",
             (job_id,))
    scope = job["scope"]
    items = db.query(
        """SELECT i.* FROM curriculum.bulk_job_items i
           WHERE i.job_id=%s AND i.status IN ('pending','failed')
           ORDER BY i.id""", (job_id,))
    done = failed = skipped = 0
    for row in items[:limit] if limit else items:
        topic = db.one("SELECT * FROM curriculum.catalog_items WHERE id=%s", (row["topic_id"],))
        if not topic:
            db.query("UPDATE curriculum.bulk_job_items SET status='skipped', detail='Thema nicht im Katalog' WHERE id=%s",
                     (row["id"],))
            skipped += 1
            continue
        db.query("UPDATE curriculum.bulk_job_items SET status='running', attempts=attempts+1 WHERE id=%s",
                 (row["id"],))
        # Job-Modi auf Bau-Modi abbilden (PART 83)
        jmode = job["mode"]
        bmode = {"missing": "missing", "repair": "repair",
                 "regenerate_outdated": "repair", "regenerate_component": "regenerate",
                 "full_rebuild": "full_rebuild"}.get(jmode, "missing")
        components = (scope.get("components") or "").split(",") if jmode == "regenerate_component" else None
        try:
            res = factory.build_topic(topic, mode=bmode,
                                      components=[c for c in (components or []) if c])
            st = "done" if res["status"] in ("READY_CORE", "READY_COMPLETE") else "failed"
            db.query("UPDATE curriculum.bulk_job_items SET status=%s, detail=%s WHERE id=%s",
                     (st, "; ".join(res["blocking"])[:500], row["id"]))
            done += st == "done"
            failed += st == "failed"
        except BudgetExhausted:
            db.query("UPDATE curriculum.bulk_job_items SET status='pending' WHERE id=%s", (row["id"],))
            db.query("UPDATE curriculum.bulk_jobs SET status='paused' WHERE id=%s", (job_id,))
            return {"done": done, "failed": failed, "skipped": skipped, "paused": True}
        except Exception as exc:  # noqa: BLE001 — ein Thema darf den Sammelauftrag nicht killen
            db.query("UPDATE curriculum.bulk_job_items SET status='failed', detail=%s WHERE id=%s",
                     (str(exc)[:500], row["id"]))
            failed += 1
    db.query("""UPDATE curriculum.bulk_jobs SET status='done', finished_at=now(),
                stats=%s WHERE id=%s""",
             (__import__("psycopg.types.json", fromlist=["Jsonb"]).Jsonb(
                 {"done": done, "failed": failed, "skipped": skipped}), job_id))
    return {"done": done, "failed": failed, "skipped": skipped, "paused": False}
