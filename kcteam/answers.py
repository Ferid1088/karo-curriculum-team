"""Strukturierte Antworten – damit Karo jede Antwort automatisch auswerten kann.

Jede Diagnose- und Abschlussaufgabe hat ein `answer`-Objekt. Typische falsche Antworten sind
als `distractors` hinterlegt und zeigen auf eine Fehlvorstellung (F1, F2 …). So erkennt die
Datenbank nicht nur „falsch“, sondern auch „falsch, weil …“.
"""
from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field, model_validator


class ChoiceOption(BaseModel):
    text: str = Field(max_length=200)
    correct: bool = False
    misconception: str | None = Field(None, description="Key der Fehlvorstellung, wenn diese Option sie verrät")


class AnswerNumber(BaseModel):
    type: Literal["number"]
    value: float
    tolerance: float = Field(0, ge=0)
    unit: str | None = Field(None, description="z. B. cm, kg – nur zur Anzeige")


class AnswerFraction(BaseModel):
    type: Literal["fraction"]
    value: str = Field(description="z. B. '5/6' oder '1 1/2'")
    accept_equivalent: bool = Field(True, description="10/12 gilt auch als richtig für 5/6")
    accept_decimal: bool = False
    require_reduced: bool = Field(False, description="nur vollständig gekürzt richtig")


class AnswerChoice(BaseModel):
    type: Literal["choice"]
    options: list[ChoiceOption] = Field(min_length=2, max_length=6)
    multiple: bool = False

    @model_validator(mode="after")
    def _check(self):
        n = sum(o.correct for o in self.options)
        if n == 0:
            raise ValueError("choice: mindestens eine Option muss correct=true sein")
        if not self.multiple and n > 1:
            raise ValueError("choice: bei multiple=false genau eine richtige Option")
        return self


class AnswerText(BaseModel):
    type: Literal["text"]
    accepted: list[str] = Field(min_length=1, max_length=12, description="alle richtigen Schreibweisen")
    case_sensitive: bool = Field(False, description="True z. B. bei Groß-/Kleinschreibung als Lernziel")
    ignore_punctuation: bool = True


class AnswerOrder(BaseModel):
    type: Literal["order"]
    items: list[str] = Field(min_length=2, max_length=10, description="in richtiger Reihenfolge; Karo mischt")


class AnswerMatch(BaseModel):
    type: Literal["match"]
    pairs: list[tuple[str, str]] = Field(min_length=2, max_length=10, description="richtige Paare; Karo mischt rechts")


class AnswerMark(BaseModel):
    """Auf einer Darstellung markieren: Zahl/Bruch auf dem Zahlenstrahl oder ein Teil (part_id) einer Abbildung."""
    type: Literal["mark"]
    value: str = Field(description="Zahl, Bruch oder part_id")
    tolerance: float = Field(0, ge=0)


class RubricCriterion(BaseModel):
    criterion: str = Field(max_length=200)
    points: int = Field(ge=1, le=10)


class AnswerFreeText(BaseModel):
    """Offene Antwort ohne lokale Regel. Nicht automatisch prüfbar: Karo bewertet extern
    (Lehrkraft/Redaktion) anhand des Rasters und meldet das Ergebnis zurück.
    Für lokal prüfbare Erklärungen stattdessen `concept_rubric` verwenden."""
    type: Literal["free_text"]
    rubric: list[RubricCriterion] = Field(min_length=1, max_length=8)
    pass_points: int = Field(ge=1)
    sample_answer: str = Field(max_length=1500)
    max_words: int | None = None

    @model_validator(mode="after")
    def _check(self):
        if self.pass_points > sum(c.points for c in self.rubric):
            raise ValueError("free_text: pass_points größer als die erreichbare Punktzahl")
        return self


class ConceptSpec(BaseModel):
    """Ein gefordertes Konzept: kanonische Formulierung + akzeptierte Ausdrucksvarianten.

    `accepted` trägt die semantischen Varianten, die das Curriculum explizit
    freigibt (Synonyme, Umstellungen) — die Laufzeit erfindet keine dazu.
    """
    concept: str = Field(max_length=300)
    accepted: list[str] = Field(default_factory=list, max_length=10,
                                description="gleichwertige Ausdrucksformen")
    key: str | None = Field(None, max_length=40)
    hint: str | None = Field(None, max_length=300,
                             description="gezielte Nachhilfe, wenn genau dieses Konzept fehlt")


class MisconceptionSpec(BaseModel):
    """Typische Fehlvorstellung: Formulierungen, die sie verraten.

    Ein Treffer schlägt jede noch so vollständige Antwort — „richtiger Kern
    plus Fehlvorstellung" ist ein Widerspruch, kein Treffer.
    """
    patterns: list[str] = Field(min_length=1, max_length=10)
    misconception: str | None = Field(None, max_length=60,
                                      description="Key der Fehlvorstellung (F1 …)")
    feedback: str | None = Field(None, max_length=300)


class ClarificationTask(BaseModel):
    """UNKNOWN → deterministisch bewertbare Folgeaufgabe (kein Runtime-AI):
    „Welche Aussage meinst du?" als Auswahl."""
    prompt: str = Field(max_length=500)
    options: list[ChoiceOption] = Field(min_length=2, max_length=6)

    @model_validator(mode="after")
    def _check(self):
        if not any(o.correct for o in self.options):
            raise ValueError("clarification: mindestens eine Option muss correct=true sein")
        return self


class AnswerConceptRubric(BaseModel):
    """Lokal prüfbarer Freitext: Begriffsabdeckung statt Stringvergleich.

    correct (alle bzw. `min_required` Konzepte) · partial (`partial_min`
    Treffer) · misconception (bekannte Fehlvorstellung, Vorrang vor Treffern)
    · unknown (nichts Einzuordnendes → `clarification`, niemals falsch).

    Bewertungsreihenfolge (deterministisch):
    unknown_markers → misconception → contradiction → Konzeptabdeckung.
    """
    type: Literal["concept_rubric"]
    required_concepts: list[ConceptSpec | str] = Field(min_length=1, max_length=8)
    optional_concepts: list[ConceptSpec | str] = Field(default_factory=list, max_length=6)
    min_required: int | None = Field(None, ge=1,
                                     description="Schwelle für 'correct'; fehlt: alle")
    partial_min: int = Field(1, ge=1,
                             description="ab so vielen Treffern gilt 'partial'")
    misconceptions: list[MisconceptionSpec] = Field(default_factory=list, max_length=8)
    contradictions: list[MisconceptionSpec] = Field(
        default_factory=list, max_length=8,
        description="Formulierungen, die dem Konzept aktiv widersprechen → incorrect")
    unknown_markers: list[str] = Field(
        default_factory=lambda: ["weiß nicht", "weiss nicht", "keine ahnung",
                                 "kein plan", "verstehe nicht", "?"],
        max_length=12,
        description="Ausdruecke des Nichtwissens → unknown statt falsch")
    normalization: list[str] = Field(
        default_factory=lambda: ["lower", "umlauts", "punctuation", "articles", "typo"],
        max_length=8,
        description="verfuegbar: lower, umlauts, punctuation, articles, typo")
    clarification: ClarificationTask | None = None
    sample_answer: str | None = Field(None, max_length=1500)
    max_words: int | None = None


AnswerSpec = Annotated[
    Union[AnswerNumber, AnswerFraction, AnswerChoice, AnswerText, AnswerOrder,
          AnswerMatch, AnswerMark, AnswerFreeText, AnswerConceptRubric],
    Field(discriminator="type"),
]
AUTO_CHECKABLE = {"number", "fraction", "choice", "text", "order", "match", "mark",
                  "concept_rubric"}


class Distractor(BaseModel):
    answer: str = Field(description="typische falsche Antwort, genau so, wie ein Kind sie eingeben würde")
    misconception: str | None = Field(None, description="Key der Fehlvorstellung (F1 …)")
    feedback: str | None = Field(None, max_length=300, description="kurzer Hinweis für das Kind")


def is_auto_checkable(answer) -> bool:
    return answer is not None and answer.type in AUTO_CHECKABLE
