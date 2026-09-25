"""Datenmodelle für alles, was die Agenten liefern.

Feldnamen sind englisch (für Karos Code), Inhalte deutsch.
Niveaus: below = unter dem Zielniveau (Einstieg/Aufholen), target = Zielniveau der Klassenstufe,
above = schon darüber (gehört in eine höhere Klasse).
"""
from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from .answers import AnswerSpec, Distractor

Level = Literal["below", "target", "above"]
ID_PART = re.compile(r"^[A-Z0-9_]+$")


def _check_id(value: str, parts: int | None = None) -> str:
    value = value.strip().upper()
    segs = value.split(".")
    if any(not ID_PART.match(s) for s in segs):
        raise ValueError(f"ID '{value}' darf nur A-Z, 0-9, _ und Punkte enthalten")
    if parts is not None and len(segs) != parts:
        raise ValueError(f"ID '{value}' muss {parts} Teile haben (getrennt durch Punkte)")
    return value


class Source(BaseModel):
    title: str
    url: str | None = None


# ---------- Curriculum-Analyst ----------
class TopicBlock(BaseModel):
    id: str = Field(description="FACH.BLOCK, z. B. MA.BRUECHE")
    title: str
    description: str
    grade_min: int = Field(ge=1, le=13, description="erste Klassenstufe, in der der Block typischerweise vorkommt")
    grade_max: int = Field(ge=1, le=13)
    typical_grade: int = Field(ge=1, le=13, description="Klassenstufe, in der der Kern typischerweise sitzen soll (Median über Lehrpläne)")
    varies: bool = Field(False, description="True, wenn die Länder deutlich unterschiedlich zuordnen")
    variance_note: str | None = None
    sources: list[Source] = Field(default_factory=list)

    @field_validator("id")
    @classmethod
    def _id(cls, v: str) -> str:
        return _check_id(v, 2)


class CurriculumMap(BaseModel):
    subject_code: str = Field(description="Kürzel, 2-4 Großbuchstaben, z. B. MA")
    blocks: list[TopicBlock]

    @field_validator("subject_code")
    @classmethod
    def _code(cls, v: str) -> str:
        return _check_id(v, 1)


# ---------- Fachdidaktiker ----------
class ConceptDraft(BaseModel):
    id: str = Field(description="FACH.BLOCK.KONZEPT, z. B. MA.BRUECHE.ADD_UNGL")
    title: str
    description: str
    first_contact_grade: int = Field(ge=1, le=13)
    target_grade: int = Field(ge=1, le=13, description="Klasse, in der das Konzept sicher beherrscht werden soll")
    varies: bool = False
    prerequisites: list[str] = Field(default_factory=list,
                                     description="IDs direkter Voraussetzungen (auch aus anderen Blöcken, nur im selben Fach)")
    order: int = 0
    track: Literal["all", "gA", "eA"] = Field("all", description="Oberstufe: gA = grundlegendes, eA = erhöhtes "
                                              "Anforderungsniveau (Grund-/Leistungskurs); sonst 'all'")
    learning_year: int | None = Field(None, ge=1, le=13, description="nur Fremdsprachen: Lernjahr")
    cefr: Literal["pre-A1", "A1", "A2", "B1", "B2", "C1"] | None = Field(None, description="nur Fremdsprachen: GER-Niveau")

    @field_validator("id")
    @classmethod
    def _id(cls, v: str) -> str:
        return _check_id(v, 3)

    @field_validator("prerequisites")
    @classmethod
    def _pre(cls, v: list[str]) -> list[str]:
        return [_check_id(x, 3) for x in v]


class ConceptGraph(BaseModel):
    block_id: str
    concepts: list[ConceptDraft]


# ---------- Niveau-Kalibrierer ----------
class Item(BaseModel):
    prompt: str
    solution: str
    level: Level
    grade: int = Field(ge=1, le=13)
    representation: str | None = Field(None, description="z. B. bild, symbolisch, text, sachaufgabe")
    answer: AnswerSpec | None = Field(None, description="Pflicht bei Diagnose- und Abschlussaufgaben: auswertbare Antwort")
    distractors: list[Distractor] = Field(default_factory=list,
                                          description="typische falsche Antworten mit zugehöriger Fehlvorstellung")


class LevelDescriptions(BaseModel):
    below: str
    target: str
    above: str


class CanDo(BaseModel):
    below: list[str]
    target: list[str]
    above: list[str]


class BoundaryItems(BaseModel):
    below: list[Item]
    within: list[Item]
    above: list[Item]


class Calibration(BaseModel):
    concept_id: str
    levels: LevelDescriptions
    can_do: CanDo
    difficulty_parameters: dict[str, Any] = Field(description="messbare Grenzen für das Zielniveau, z. B. max_nenner, max_schritte")
    anchor_items: list[Item] = Field(min_length=2)
    boundary_items: BoundaryItems


# ---------- Diagnostiker ----------
class Misconception(BaseModel):
    key: str = Field(description="F1, F2, ...")
    description: str
    diagnostic_item: Item
    remediation_hint: str


class Diagnostics(BaseModel):
    concept_id: str
    misconceptions: list[Misconception]
    diagnostic_items: list[Item] = Field(min_length=2)
    exit_items: list[Item] = Field(min_length=2, description="Abschlussaufgaben, alle auf Zielniveau")


# ---------- Kinderrechts-Inspektor ----------
class Finding(BaseModel):
    location: str = Field(description="wo genau, z. B. title, anchor_items[1].prompt")
    rule: str
    severity: Literal["block", "warn"] = "block"
    reason: str
    requirement: str = Field(description="was geändert werden muss")


class InspectorVerdict(BaseModel):
    decision: Literal["approved", "rejected"]
    findings: list[Finding] = Field(default_factory=list)
    summary: str = ""

    @model_validator(mode="after")
    def _needs_findings(self):
        if self.decision == "rejected" and not self.findings:
            raise ValueError("Bei decision 'rejected' ist mindestens ein Befund (findings) mit Fundstelle, Regel, "
                             "Begründung und Auflage Pflicht.")
        return self


# ---------- Kritiker ----------
class CriticIssue(BaseModel):
    concept_id: str | None = None
    type: Literal["missing_prerequisite", "wrong_edge", "level_mismatch", "gap", "duplicate", "item_error", "other"]
    description: str
    suggested_fix: str
    route_to: Literal["fachdidaktiker", "niveau_kalibrierer", "diagnostiker", "visual_didaktiker"]


class CriticReport(BaseModel):
    issues: list[CriticIssue] = Field(default_factory=list)


# ---------- Curriculum-Agent (Abruf durch Karo) ----------
class TopicMatch(BaseModel):
    decision: Literal["existing", "new", "out_of_scope"] = Field(
        description="existing = vorhandene Konzepte passen; new = neue Konzepte nötig; out_of_scope = kein "
                    "Schulstoff dieses Fachs/dieser Klasse oder für Kinder ungeeignet")
    reason: str = Field(description="kurze Begründung (für das Protokoll, nicht für das Kind)")
    concept_ids: list[str] = Field(default_factory=list,
                                   description="bei existing: passende Konzept-IDs aus den Kandidaten (wichtigstes zuerst)")
    new_block: TopicBlock | None = Field(None, description="nur wenn kein bestehender Block passt")
    block_id: str | None = Field(None, description="bei new: Block, in den die neuen Konzepte gehören")
    concepts: list[ConceptDraft] = Field(default_factory=list, max_length=3,
                                         description="bei new: zuerst das angefragte Zielkonzept, danach höchstens zwei "
                                                     "fehlende Voraussetzungen")
    search_terms: list[str] = Field(default_factory=list, max_length=6,
                                    description="Suchbegriffe/Synonyme, unter denen das Thema künftig gefunden wird")
    likely_next: list[str] = Field(default_factory=list, max_length=3,
                                   description="Titel der Konzepte, die im Unterricht wahrscheinlich als Nächstes kommen")


# ---------- Visual-Didaktiker ----------
from .visuals.spec import VisualExplanation, VisualItem, VisualSet, VisualStep  # noqa: E402

__all__ = ["VisualExplanation", "VisualItem", "VisualSet", "VisualStep"]
