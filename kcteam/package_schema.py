"""COMPLETE_TOPIC_PACKAGE – der Produktvertrag der Curriculum Factory.

Ein Thema ist nicht vollständig, weil es eine Erklärung und fünf Fragen hat.
Vollständig ist es, wenn Karo damit eine ganze Lernreise deterministisch
führen kann – von der Diagnose bis zur Meisterschaft, ohne einen
Modellaufruf zur Laufzeit.

Feldnamen englisch (Vertrag), Inhalte deutsch (für das Kind).
Jede Komponente trägt entweder Inhalt oder explizit NOT_APPLICABLE + Grund –
nie ein stilles Fehlen.
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from .answers import AnswerSpec, Distractor

#: Vertragsfassung des kompletten Pakets. Steigt, wenn Pflichtfelder
#: dazukommen oder sich Bedeutungen aendern.
PACKAGE_CONTRACT_VERSION = "ctp-v1"

# ---------------------------------------------------------------- Enums

PackageStatus = Literal[
    "CATALOG_ONLY",     # Thema steht im Katalog, noch nichts gebaut
    "BUILDING",         # Fabrik arbeitet gerade
    "PARTIAL",          # teilweise vorhanden, ehrlich gekennzeichnet
    "READY_CORE",       # Kernreise moeglich, Randkomponenten fehlen noch
    "READY_COMPLETE",   # alle Pflichtkomponenten + alle Qualitaetstore
    "OUTDATED",         # Quelle/Vertrag neuer – Neuaufbau noetig
    "REVIEW_REQUIRED",  # Mensch muss entscheiden
    "FAILED",           # Bau gescheitert
    "BLOCKED",          # gesperrt (Inspektor/Mensch)
]

ComponentState = Literal["COMPLETE", "PARTIAL", "NOT_APPLICABLE", "MISSING"]

TaskRole = Literal[
    "DIAGNOSTIC", "MISCONCEPTION_PROBE", "PREREQUISITE_PROBE",
    "WORKED", "GUIDED", "SCAFFOLDED", "INDEPENDENT", "TRANSFER",
    "MASTERY_CHECK", "SPACED_REVIEW", "EXAM_PRACTICE", "EXAM_SIMULATION",
    "CLARIFICATION",
]
TASK_ROLES: tuple[str, ...] = TaskRole.__args__  # type: ignore[attr-defined]

Outcome = Literal[
    "CORRECT", "PARTIAL", "INCORRECT", "MISCONCEPTION", "UNKNOWN",
    "REPEATED_WRONG", "REPEATED_UNKNOWN", "MASTERED",
    "PREREQUISITE_FAILED", "PREREQUISITE_MASTERED",
]

#: Fehlerarten (PART 30): die Diagnose unterscheidet Ursache, nicht nur falsch.
ErrorType = Literal[
    "CARELESS", "PROCEDURAL", "CONCEPTUAL_MISCONCEPTION",
    "PREREQUISITE_GAP", "LANGUAGE_MISUNDERSTANDING",
    "PARTIAL_KNOWLEDGE", "UNKNOWN",
]

#: Was die Laufzeit nach einem Ergebnis tun kann (PART 24). Geordnet:
#: die Liste ist die Reihenfolge, in der die Engine es versucht.
ActionType = Literal[
    "NEXT_TASK",            # naechste Aufgabe derselben Rolle/Ebene
    "ADVANCE_ROLE",         # naechste Aufgabenrolle auf derselben Ebene
    "ADVANCE_LEVEL",        # eine Ebene hoeher
    "MASTERY_EVALUATE",     # Meisterschafts-Nachweis auswerten
    "SHOW_HINT",            # naechste Stufe der Hinweisleiter
    "GUIDED_STEP",          # kleiner gefuehrter Teilschritt
    "WORKED_EXAMPLE",       # vorgerechnetes/vorgefuehrtes Beispiel
    "SIMPLER_PARALLEL",     # einfachere Parallelaufgabe, gleiches Ziel
    "ALTERNATE_EXPLANATION",  # andere Erklaerklaerung (Modus wechseln)
    "REPAIR_EXPLANATION",   # fehlvorstellungs-spezifische Erklaerung
    "GUIDED_REPAIR",        # gefuehrte Reparatur der Fehlvorstellung
    "REPAIR_CHECK",         # eigenstaendige Pruefung: Fehlvorstellung geloest?
    "CLARIFICATION",        # UNKNOWN -> deterministische Rueckfrage
    "PREREQUISITE_PROBE",   # testet die naechste Voraussetzung
    "PREREQUISITE_DETOUR",  # Umweg in die Voraussetzung (mit Rueckkehr!)
    "RETURN_TO_TARGET",     # vom Umweg zurueck ans Ziel
    "SPACED_REVIEW",        # Wiederholungsmaterial anbieten
    "CHILD_CHOICE",         # Kind waehlt: Pause / genug / naechstes Thema
    "COMPLETE",             # Paket zu Ende gelernt
]

VisualKind = Literal[
    "DETERMINISTIC_DIAGRAM", "DATA_CHART", "ILLUSTRATIVE_IMAGE", "NO_VISUAL_NEEDED",
]

ExplanationMode = Literal[
    "rule", "intuitive", "example", "worked", "visual", "analogy",
    "misconception_specific",
]

PrereqKind = Literal["necessary", "useful", "parallel", "extension"]

_ID_SEG = __import__("re").compile(r"^[A-Z0-9][A-Z0-9_]*$")


def _cid(v: str, what: str = "ID") -> str:
    v = v.strip().upper()
    if not all(_ID_SEG.match(p) for p in v.split(".")):
        raise ValueError(f"{what} '{v}': nur A-Z, 0-9, _ und Punkte erlaubt")
    return v


# ---------------------------------------------------------------- 1+2: Ausrichtung & Identitaet
class CatalogRef(BaseModel):
    """Verweis auf den freigegebenen Katalogeintrag (Herkunft des Themas)."""
    item_id: str
    path: list[str] = Field(default_factory=list,
                            description="Titelpfad im Katalog, z. B. [Fach, Bereich, Thema]")
    framework: str = ""
    region: str = ""
    school_type: str = ""
    grade: int = Field(0, ge=0, le=13)
    source: str = ""
    source_version: str = ""
    source_reference: str = ""


class ConceptIdentity(BaseModel):
    concept_id: str
    title: str
    description: str = ""
    subject_code: str = ""
    target_grade: int = Field(ge=1, le=13)

    @field_validator("concept_id")
    @classmethod
    def _id(cls, v: str) -> str:
        return _cid(v, "concept_id")


# ---------------------------------------------------------------- 3+4+5: Kompetenzen, Voraussetzungen, Leiter
class PrerequisiteEdge(BaseModel):
    """Voraussetzung mit Art. `detour` traegt den Rueckweg (PART 26):
    jeder Umweg kennt Ziel, Bedingung und Wiedereinstieg – Sackgassen
    sind verboten."""
    target_id: str
    kind: PrereqKind = "necessary"
    # Rueckweg-Angaben, nur fuer 'necessary'/'useful' gefuellt
    return_condition: str = Field("mastery",
        description="wann gilt der Umweg als geschafft (z. B. mastery | core)")
    resume_level: int = Field(0, ge=0, description="Ebene, auf der das Ziel nach dem Umweg weitergeht")


class PrerequisiteGraph(BaseModel):
    """Rekursiv: jede Voraussetzung darf selbst wieder welche haben."""
    edges: list[PrerequisiteEdge] = Field(default_factory=list)


class LevelSpec(BaseModel):
    """Eine Sprosse der Kompetenzleiter (PART 20).

    Level 0 = die tiefste sinnvolle Voraussetzung DIESES Konzepts –
    nicht „Klasse 1". Die Leiter darf je Thema unterschiedlich lang sein.
    """
    level_id: int = Field(ge=0, description="0..N, 0 = unterster sinnvoller Einstieg")
    goal: str
    required_knowledge: list[str] = Field(default_factory=list)
    observable_evidence: list[str] = Field(default_factory=list)
    allowed_task_types: list[TaskRole] = Field(default_factory=list)
    typical_misconceptions: list[str] = Field(default_factory=list,
                                              description="IDs aus dem Fehlvorstellungs-Modell")
    entry_criteria: str = ""
    exit_criteria: str = ""
    next_level: int | None = Field(None, description="naechste Ebene; None = oberste")
    fallback_level: int | None = Field(None, description="wohin bei PREREQUISITE_FAILED; None = Voraussetzungs-Umweg")


# ---------------------------------------------------------------- 6+7: Diagnose & Fehlvorstellungen
class MisconceptionModel(BaseModel):
    """Eine echte, plausible Fehlvorstellung mit Reparatur (PART 29)."""
    misconception_id: str = Field(description="z. B. F1")
    description: str
    likely_cause: str = ""
    observable_answer_patterns: list[str] = Field(default_factory=list)
    disambiguating_probe: str | None = Field(None, description="task_id der Rueckfrage, die sie sicher unterscheidet")
    repair_explanation: str = ""
    guided_repair: list[str] = Field(default_factory=list, description="task_ids, GUIDED")
    independent_check: list[str] = Field(default_factory=list, description="task_ids, die ohne Vorlage geloest werden")
    resolution_evidence: str = Field("", description="woran man erkennt, dass die Vorstellung geloest ist")


# ---------------------------------------------------------------- Aufgaben (10-14, 18, 21)
class HintLadder(BaseModel):
    """Hinweisleiter (PART 36): vorsichtig -> konkret, nie sofort die Loesung."""
    hints: list[str] = Field(min_length=1, max_length=4,
                             description="Stufe 1 leichter Wink, 2 Strategie, 3 Teilstruktur, 4 vorgefuehrter Schritt")
    then: Literal["guided_problem", "reveal"] = "guided_problem"


class TaskSpec(BaseModel):
    """Eine konkrete Aufgabe mit deterministischem Antwortvertrag."""
    task_id: str
    role: TaskRole
    level_id: int = Field(ge=0)
    prompt: str
    answer: AnswerSpec | None = None
    solution: str = ""
    worked_steps: list[str] = Field(default_factory=list,
                                    description="bei WORKED/GUIDED: die vorgefuehrten Schritte")
    hints: HintLadder | None = None
    distractors: list[Distractor] = Field(default_factory=list)
    difficulty: dict[str, Any] = Field(default_factory=dict,
                                       description="mehrdimensional: schritte, abstraktion, sprache, distraktoren …")
    misconception: str | None = Field(None, description="welche Fehlvorstellung diese Aufgabe sondiert")
    prerequisite_id: str | None = Field(None, description="bei PREREQUISITE_PROBE: welche Voraussetzung")
    visual_ref: str | None = None
    error_types: list[ErrorType] = Field(default_factory=list,
                                         description="welche Fehlerart ein falsches Ergebnis hier typischerweise hat")
    clarification_for: str | None = Field(None, description="bei CLARIFICATION: task_id der unklaren Aufgabe")
    accessibility_text: str | None = None


# ---------------------------------------------------------------- 20: Vorlagen (vorlage DSL)
class ParamDomain(BaseModel):
    """Wertebereich eines Vorlagen-Parameters."""
    type: Literal["int_range", "decimal_range", "set", "fractions", "pairs"]
    min: float | None = None
    max: float | None = None
    step: float | None = None
    values: list[Any] | None = None
    max_denominator: int | None = None     # fractions


class TaskTemplate(BaseModel):
    """Deterministische Aufgaben-Vorlage (PART 38): erzeugt gepruefte Varianten
    zur Laufzeit, ohne dass ein Modell noch einmal aufgerufen wird."""
    template_id: str
    role: TaskRole
    level_id: int = Field(ge=0)
    prompt_template: str = Field(description="Text mit {param}-Platzhaltern")
    parameters: dict[str, ParamDomain] = Field(default_factory=dict)
    constraints: list[str] = Field(default_factory=list,
                                   description="sichere Ausdruecke ueber die Parameter, die wahr sein muessen")
    solution: str = Field(description="Ausdruck ueber die Parameter -> richtige Antwort")
    answer_type: str = Field("number", description="Antworttyp des erzeugten AnswerSpec")
    distractor_exprs: dict[str, str] = Field(default_factory=dict,
                                           description="Fehlvorstellungs-Key -> Ausdruck fuer die typische falsche Antwort")
    difficulty: dict[str, Any] = Field(default_factory=dict)
    variant_exclusions: list[str] = Field(default_factory=list,
                                          description="Ausdruecke: Parameterkombis, die nicht vorkommen duerfen")
    visual_dependencies: list[str] = Field(default_factory=list)
    known_misconceptions: list[str] = Field(default_factory=list)
    max_variants: int = Field(20, ge=1, le=500)


# ---------------------------------------------------------------- 9: Erklaervarianten
class ExplanationVariant(BaseModel):
    mode: ExplanationMode
    text: str
    level_id: int | None = None
    for_misconception: str | None = None


# ---------------------------------------------------------------- 22: Visuelle Assets
class VisualAsset(BaseModel):
    """PART 53-55: visueller Bedarf ist eine Entscheidung, nie Dekoration."""
    asset_id: str
    visual_type: VisualKind
    learning_goal: str = ""
    what_child_should_notice: str = ""
    structured_data: dict[str, Any] = Field(default_factory=dict)
    labels: list[str] = Field(default_factory=list)
    interaction: str | None = None
    accessibility_text: str = ""
    age_tone: str = ""
    fallback_text: str = ""
    image_ref: str | None = Field(None, description="bei ILLUSTRATIVE_IMAGE: Referenz auf das gepruefte Bild")


# ---------------------------------------------------------------- 23+25: Uebergangsregeln / kompilierte Politik
class TransitionRule(BaseModel):
    """(level, rolle, ergebnis) -> geordnete Aktionen (PART 23/24)."""
    level_id: int
    task_role: TaskRole
    outcome: Outcome
    actions: list[ActionType] = Field(min_length=1)
    misconception: str | None = Field(None, description="nur bei outcome=MISCONCEPTION: welche")


class DetourPlan(BaseModel):
    """Umweg in eine Voraussetzung – mit explizitem Rueckweg (PART 26)."""
    target_before_detour: str
    prerequisite_path: list[str] = Field(min_length=1)
    return_condition: str = "mastery"
    return_target: str = ""
    resume_level: int = 0


class JourneyPolicy(BaseModel):
    """Das kompilierte Ergebnis des Lernreise-Architekten + Compilers.

    Beantwortet deterministisch: Zustand + Ergebnis -> naechste Aktion.
    """
    rules: list[TransitionRule] = Field(default_factory=list)
    detours: list[DetourPlan] = Field(default_factory=list)
    role_order: list[TaskRole] = Field(
        default=["WORKED", "GUIDED", "SCAFFOLDED", "INDEPENDENT", "TRANSFER", "MASTERY_CHECK"],
        description="Standardreihenfolge der Aufgabenrollen pro Ebene")
    max_attempts_per_task: int = 3
    max_unknown_per_task: int = 2
    max_resume_cycles: int = Field(3, description="so viele fruchtlose "
                                   "Sitzungszyklen, bis die Pause-Frage verbindlich "
                                   "wird (terminal) – das Thema bleibt trotzdem offen")
    max_no_progress: int = Field(14, description="nach so vielen aufeinanderfolgenden "
                                                 "Aktionen ohne Fortschritt bietet die Engine CHILD_CHOICE")


# ---------------------------------------------------------------- 26+27: Qualitaet & Manifest
class ComponentManifest(BaseModel):
    component: str
    state: ComponentState
    reason: str = Field("", description="Pflicht bei PARTIAL und NOT_APPLICABLE")
    count: int = 0


class CompletenessManifest(BaseModel):
    """Maschinenlesbare Abdeckung (PART 67): pro Komponente ein Zustand."""
    items: list[ComponentManifest] = Field(default_factory=list)
    coverage: dict[str, float] = Field(default_factory=dict,
                                       description="Abdeckung je Dimension (PART 108), 0.0-1.0")

    def state_of(self, component: str) -> ComponentState:
        for it in self.items:
            if it.component == component:
                return it.state
        return "MISSING"


class QualityEvidence(BaseModel):
    subject_validation: str = Field("PENDING", description="PASS|FAIL|PENDING")
    validator: str = "PENDING"
    template_check: str = "PENDING"
    dead_end_check: str = "PENDING"
    prerequisite_cycles: str = "PENDING"
    return_paths: str = "PENDING"
    rubric_adversarial: str = "PENDING"
    visual_qa: str = "PENDING"
    control: str = Field("PENDING", description="Vollstaendigkeits-Kontrolleur: "
                         "PASS|REPAIR_REQUIRED|BLOCKED|PENDING")
    simulations: dict[str, str] = Field(default_factory=dict)
    critic_findings: int = 0
    notes: list[str] = Field(default_factory=list)


class Provenance(BaseModel):
    topic_version: int = 1
    content_version: int = 1
    contract_version: str = PACKAGE_CONTRACT_VERSION
    prompt_version: str = "1"
    agent_role: str = ""
    provider: str = ""
    model: str = ""
    git_sha: str = ""
    created_at: str = ""
    supersedes: str | None = None
    run_id: str | None = Field(None, description="Fabrik-Lauf (package_stages.run_id)")
    bulk_job: str | None = Field(None, description="Sammelauftrag, der den Bau ausloeste")


# ---------------------------------------------------------------- Das Paket
SECTIONS: tuple[str, ...] = (
    "catalog_alignment", "concept_identity", "target_competencies",
    "prerequisite_graph", "competency_ladder", "diagnostic_probes",
    "misconception_model", "teaching_strategies", "explanations",
    "worked_examples", "guided_practice", "independent_practice",
    "transfer_practice", "mastery_checks", "spaced_review",
    "prerequisite_detours", "clarification_material", "exam_practice",
    "answer_contracts", "task_templates", "hint_ladders", "visual_assets",
    "illustrative_images", "accessibility_text", "transition_mappings",
    "quality_evidence", "coverage_manifest", "provenance",
)


class CompleteTopicPackage(BaseModel):
    """PART 1: Ein Thema ist fertig, wenn Karo damit die ganze Reise fahren kann."""
    # 1. curriculare Ausrichtung
    catalog_alignment: CatalogRef
    # 2. Identitaet
    concept_identity: ConceptIdentity
    # 3. Zielkompetenzen
    target_competencies: list[str] = Field(min_length=1)
    # 4. Voraussetzungsgraph (rekursiv)
    prerequisite_graph: PrerequisiteGraph = Field(default_factory=PrerequisiteGraph)
    # 5. Kompetenzleiter 0..N
    competency_ladder: list[LevelSpec] = Field(min_length=1)
    # 6.-14. Aufgabenbestand nach Rolle (in tasks mit role abgelegt)
    tasks: list[TaskSpec] = Field(default_factory=list)
    # 7. Fehlvorstellungen mit Reparatur
    misconception_model: list[MisconceptionModel] = Field(default_factory=list)
    # 8. Unterrichtsstrategie je Ebene
    teaching_strategies: dict[str, list[str]] = Field(default_factory=dict,
                                                      description="level_id -> geordnete Strategien (HOOK, EXPLANATION, …)")
    # 9. Erklaervarianten (PART 32: paedagogisch verschieden, nicht Paraphrasen)
    explanations: list[ExplanationVariant] = Field(default_factory=list)
    # 20. deterministische Generatoren
    task_templates: list[TaskTemplate] = Field(default_factory=list)
    # 22-24. visuelle Assets (DETERMINISTIC_DIAGRAM | DATA_CHART | ILLUSTRATIVE_IMAGE | NO_VISUAL_NEEDED)
    visual_assets: list[VisualAsset] = Field(default_factory=list)
    visual_need: VisualKind = "NO_VISUAL_NEEDED"
    visual_na_reason: str = ""
    # 25. kompilierte Politik
    journey_policy: JourneyPolicy = Field(default_factory=JourneyPolicy)
    # 26-28. Nachweis, Abdeckung, Herkunft
    quality_evidence: QualityEvidence = Field(default_factory=QualityEvidence)
    coverage_manifest: CompletenessManifest = Field(default_factory=CompletenessManifest)
    provenance: Provenance = Field(default_factory=Provenance)

    @model_validator(mode="after")
    def _check(self):
        ids = [t.task_id for t in self.tasks]
        if len(ids) != len(set(ids)):
            raise ValueError("doppelte task_id im Paket")
        tids = set(ids)
        level_ids = {l.level_id for l in self.competency_ladder}
        if len(level_ids) != len(self.competency_ladder):
            raise ValueError("doppelte level_id in der Kompetenzleiter")
        for l in self.competency_ladder:
            if l.next_level is not None and l.next_level not in level_ids:
                raise ValueError(f"level {l.level_id}: next_level {l.next_level} existiert nicht")
            if l.fallback_level is not None and l.fallback_level not in level_ids:
                raise ValueError(f"level {l.level_id}: fallback_level {l.fallback_level} existiert nicht")
        for t in self.tasks:
            if t.level_id not in level_ids:
                raise ValueError(f"Aufgabe {t.task_id}: level_id {t.level_id} existiert nicht")
            if t.role == "PREREQUISITE_PROBE" and not t.prerequisite_id:
                raise ValueError(f"Aufgabe {t.task_id}: PREREQUISITE_PROBE braucht prerequisite_id")
            if t.role == "CLARIFICATION" and not t.clarification_for:
                raise ValueError(f"Aufgabe {t.task_id}: CLARIFICATION braucht clarification_for")
            if t.clarification_for and t.clarification_for not in tids:
                raise ValueError(f"Aufgabe {t.task_id}: clarification_for '{t.clarification_for}' unbekannt")
        known_mis = {m.misconception_id for m in self.misconception_model}
        for m in self.misconception_model:
            for ref in (*m.guided_repair, *m.independent_check):
                if ref not in tids:
                    raise ValueError(f"Fehlvorstellung {m.misconception_id}: Aufgabe '{ref}' unbekannt")
            if m.disambiguating_probe and m.disambiguating_probe not in tids:
                raise ValueError(f"Fehlvorstellung {m.misconception_id}: probe '{m.disambiguating_probe}' unbekannt")
        for t in self.tasks:
            if t.misconception and t.misconception not in known_mis:
                raise ValueError(f"Aufgabe {t.task_id}: Fehlvorstellung '{t.misconception}' unbekannt")
        for tmpl in self.task_templates:
            if tmpl.level_id not in level_ids:
                raise ValueError(f"Vorlage {tmpl.template_id}: level_id {tmpl.level_id} existiert nicht")
        return self

    # ---------------- Zugriffe fuer die Laufzeit ----------------
    def tasks_for(self, role: str, level_id: int | None = None) -> list[TaskSpec]:
        return [t for t in self.tasks
                if t.role == role and (level_id is None or t.level_id == level_id)]

    def level(self, level_id: int) -> LevelSpec | None:
        for l in self.competency_ladder:
            if l.level_id == level_id:
                return l
        return None

    def top_level(self) -> int:
        return max(l.level_id for l in self.competency_ladder)

    def bottom_level(self) -> int:
        return min(l.level_id for l in self.competency_ladder)
