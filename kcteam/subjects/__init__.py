"""Fachprofile: für jedes Fach ein eigenes Team.

Das Register (registry.yaml) kennt alle Fächer der Klassen 1–13 mit Aliassen. Jedes Fach gehört zu einer
Fächergruppe (families/*.yaml) mit Expertenanweisungen für jede Rolle, Parametern, Antwortformaten,
Visual-Typen, sensiblen Themen und Beispielen. Fachspezifische Ergänzungen stehen im Register.
"""
from __future__ import annotations

import difflib
import re
from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field

from ..agents import ROLES

DIR = Path(__file__).parent


class SensitiveTopic(BaseModel):
    topic: str
    rule: str


class FamilyProfile(BaseModel):
    id: str
    label: str
    level_axis: Literal["grade", "learning_year"] = "grade"
    overview: str
    structure: str
    roles: dict[str, str]
    difficulty_parameters: dict[str, str]
    answer_types: list[str]
    visual_types: list[str]
    sensitive_topics: list[SensitiveTopic] = Field(default_factory=list)
    not_in_app: str | None = None
    min_auto_checkable: int = 2
    example: str = ""


class SubjectProfile(BaseModel):
    id: str
    name: str
    code: str = Field(pattern=r"^[A-Z]{2,4}$")
    aliases: list[str] = Field(default_factory=list)
    family: str
    grades: tuple[int, int]
    start_grade: int | None = None
    notes: str | None = None
    role_extra: dict[str, str] = Field(default_factory=dict)


def _norm(s: str) -> str:
    s = s.strip().lower().replace("ä", "ae").replace("ö", "oe").replace("ü", "ue").replace("ß", "ss")
    return re.sub(r"[^a-z0-9]", "", s)


@lru_cache(maxsize=1)
def load_all() -> tuple[dict[str, SubjectProfile], dict[str, FamilyProfile]]:
    reg = yaml.safe_load((DIR / "registry.yaml").read_text(encoding="utf-8"))
    subjects = {s["id"]: SubjectProfile.model_validate(s) for s in reg["subjects"]}
    families = {}
    for f in sorted((DIR / "families").glob("*.yaml")):
        fam = FamilyProfile.model_validate(yaml.safe_load(f.read_text(encoding="utf-8")))
        families[fam.id] = fam
    return subjects, families


def resolve(name: str) -> SubjectProfile | None:
    subjects, _ = load_all()
    key = _norm(name)
    for s in subjects.values():
        if key in {_norm(s.id), _norm(s.name), _norm(s.code), *(_norm(a) for a in s.aliases)}:
            return s
    return None


def suggestions(name: str, n: int = 3) -> list[str]:
    subjects, _ = load_all()
    names = {}
    for s in subjects.values():
        for label in [s.name, *s.aliases]:
            names[_norm(label)] = s.name
    hits = difflib.get_close_matches(_norm(name), list(names), n=n * 2, cutoff=0.6)
    return list(dict.fromkeys(names[h] for h in hits))[:n]


class SubjectTeam:
    """Das Team für ein Fach: liefert pro Rolle den fachspezifischen Prompt-Abschnitt."""

    def __init__(self, subject: SubjectProfile, family: FamilyProfile, generic: bool = False):
        self.subject = subject
        self.family = family
        self.generic = generic

    @classmethod
    def for_subject(cls, name: str) -> "SubjectTeam":
        subjects, families = load_all()
        prof = resolve(name)
        if prof is None:
            code = (re.sub(r"[^A-Z]", "", _norm(name).upper()) or "XX")[:3].ljust(2, "X")
            taken = {s.code for s in subjects.values()}
            base = code
            for i in range(2, 10):
                if code not in taken:
                    break
                code = f"{base[:2]}X{'ABCDEFGH'[i - 2]}"
            prof = SubjectProfile(id=_norm(name) or "fach", name=name.strip(), code=code,
                                  family="allgemein", grades=(1, 13))
            return cls(prof, families["allgemein"], generic=True)
        return cls(prof, families[prof.family])

    # ---------------- Eigenschaften
    @property
    def name(self) -> str:
        return self.subject.name

    @property
    def code(self) -> str:
        return self.subject.code

    @property
    def grades(self) -> tuple[int, int]:
        return tuple(self.subject.grades)

    @property
    def min_auto_checkable(self) -> int:
        return self.family.min_auto_checkable

    def members(self) -> list[str]:
        return [f"{label} {self.name}" if r != "kinderrechts_inspektor" else f"{label} (Veto)"
                for r, label in ROLES.items()]

    def learning_year_hint(self) -> str:
        if self.family.level_axis != "learning_year":
            return "Niveau-Achse: Klassenstufe."
        s = self.subject.start_grade or self.grades[0]
        return (f"Niveau-Achse: Lernjahr + GER-Stufe. Typischer Beginn: Klasse {s}. target_grade = {s} + Lernjahr − 1; "
                "learning_year (und bei modernen Fremdsprachen cefr) bei jedem Konzept angeben.")

    def check_grades(self, grades: tuple[int, int]) -> str | None:
        lo, hi = self.grades
        if grades[1] < lo or grades[0] > hi:
            return (f"{self.name} wird typischerweise in Klasse {lo}–{hi} unterrichtet. "
                    f"Bitte eine Klasse in diesem Bereich wählen.")
        return None

    # ---------------- Prompt-Abschnitt
    def section(self, role: str) -> str:
        f, s = self.family, self.subject
        rolename = ROLES.get(role, role)
        lines = [f"## Fachprofil: {s.name} – du bist {rolename} im Team {s.name}",
                 f"Fach: {s.name} (Kürzel {s.code}), typischerweise Klasse {s.grades[0]}–{s.grades[1]}. "
                 f"Fächergruppe: {f.label}.",
                 self.learning_year_hint(),
                 f"Überblick: {f.overview.strip()}",
                 f"Struktur: {f.structure.strip()}"]
        if s.notes:
            lines.append(f"Besonderheiten {s.name}: {s.notes.strip()}")
        lines.append(f"### Fachspezifisch für deine Rolle\n{f.roles.get(role, '').strip()}")
        if s.role_extra.get(role):
            lines.append(s.role_extra[role].strip())
        if role in ("niveau_kalibrierer", "diagnostiker", "visual_didaktiker", "kritiker"):
            params = "\n".join(f"- {k}: {v}" for k, v in f.difficulty_parameters.items())
            lines.append(f"### Schwierigkeitsparameter (Vorschläge)\n{params}")
        if role in ("diagnostiker", "visual_didaktiker", "niveau_kalibrierer"):
            lines.append("Geeignete Antwortformate: " + ", ".join(f.answer_types)
                         + f". Mindestens {f.min_auto_checkable} automatisch auswertbare Aufgaben pro Zweck.")
        if role in ("visual_didaktiker", "kritiker"):
            lines.append("Bevorzugte Visual-Typen: " + ", ".join(f.visual_types))
        if f.sensitive_topics and role in ("kinderrechts_inspektor", "kritiker", "fachdidaktiker", "diagnostiker",
                                           "niveau_kalibrierer", "visual_didaktiker", "curriculum_agent",
                                           "lektionsautor"):
            lines.append("### Sensible Themen\n" + "\n".join(f"- {t.topic}: {t.rule}" for t in f.sensitive_topics))
        if f.not_in_app:
            lines.append(f"Nicht in der App möglich (keine Konzepte/Aufgaben dazu erzeugen): {f.not_in_app}")
        if f.example and role in ("fachdidaktiker", "niveau_kalibrierer", "diagnostiker", "lektionsautor"):
            lines.append(f"### Beispiel aus dem Fach (Format-Orientierung)\n{f.example.strip()}")
        if self.generic:
            lines.append("Hinweis: Für dieses Fach gibt es kein eigenes Profil – besonders sorgfältig recherchieren.")
        return "\n\n".join(lines)


def all_subjects() -> list[SubjectProfile]:
    return list(load_all()[0].values())
