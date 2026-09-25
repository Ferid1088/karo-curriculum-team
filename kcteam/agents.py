"""Agenten-Aufrufe: Prompt bauen, KI fragen, JSON prüfen, protokollieren."""
from __future__ import annotations

import json
import random
import threading
import time
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from .providers.base import Provider, ProviderError

PROMPT_DIR = Path(__file__).parent / "prompts"
T = TypeVar("T", bound=BaseModel)

ROLES = {
    "curriculum_analyst": "Curriculum-Analyst",
    "fachdidaktiker": "Fachdidaktiker",
    "niveau_kalibrierer": "Niveau-Kalibrierer",
    "diagnostiker": "Diagnostiker",
    "visual_didaktiker": "Visual-Didaktiker",
    "kritiker": "Kritiker",
    "kinderrechts_inspektor": "Kinderrechts-Inspektor",
    "curriculum_agent": "Curriculum-Agent",
}
# Rollen, für die Karos Spezifikation relevant ist (spart Tokens bei den anderen)
SPEC_ROLES = {"curriculum_analyst", "fachdidaktiker", "diagnostiker", "visual_didaktiker", "curriculum_agent"}
ROLE_STAGE = {"curriculum_analyst": "curriculum", "fachdidaktiker": "graph", "niveau_kalibrierer": "calibration",
              "diagnostiker": "diagnostics", "visual_didaktiker": "visuals", "kritiker": "critic",
              "kinderrechts_inspektor": "inspection", "curriculum_agent": "match"}


class BudgetExhausted(RuntimeError):
    """Lauf anhalten (Budget, Nutzungslimit, Abbruch). Der nächste Lauf macht an derselben Stelle weiter."""


class RateLimited(BudgetExhausted):
    pass


class RunStopped(BudgetExhausted):
    pass


class AgentFailed(RuntimeError):
    def __init__(self, msg: str, stage: str | None = None):
        super().__init__(msg)
        self.stage = stage


def extract_json(text: str) -> Any:
    """Holt das erste JSON-Objekt aus einer Antwort (toleriert ```json-Zäune und Vor-/Nachtext)."""
    start = text.find("{")
    if start < 0:
        raise ValueError("keine JSON-Objekt-Klammer gefunden")
    decoder = json.JSONDecoder()
    while start >= 0:
        try:
            obj, _ = decoder.raw_decode(text[start:])
            return obj
        except json.JSONDecodeError:
            start = text.find("{", start + 1)
    raise ValueError("kein gültiges JSON gefunden")


def compact(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), default=str)


class AgentRunner:
    def __init__(self, *, cfg, provider: Provider, db, run_id: str, karo_spec: str = "", team=None):
        self.cfg = cfg
        self.team = team
        self.provider = provider
        self.db = db
        self.run_id = run_id
        self.karo_spec = karo_spec
        self.stop = threading.Event()
        self._cooldown_until = 0.0     # gemeinsame Pause aller Worker nach einem Rate-Limit
        self._calls = 0
        self._lock = threading.Lock()
        self._prompt_cache: dict[tuple, str] = {}
        self._common = (PROMPT_DIR / "_common.md").read_text(encoding="utf-8")
        self._role_prompts = {r: (PROMPT_DIR / f"{r}.md").read_text(encoding="utf-8") for r in ROLES}

    @property
    def calls(self) -> int:
        return self._calls

    def _count(self) -> None:
        if self.stop.is_set():
            raise RunStopped("Lauf wurde angehalten")
        with self._lock:
            self._calls += 1
            if self._calls > int(self.cfg.p("max_agent_calls", 3000)):
                self.stop.set()
                raise BudgetExhausted(f"Budget von {self.cfg.p('max_agent_calls')} Agenten-Aufrufen erreicht")

    def system_prompt(self, role: str, schema: type[BaseModel]) -> str:
        """Pro Rolle und Schema identisch -> einmal bauen, danach aus dem Zwischenspeicher (und Prompt-Caching
        beim Anbieter greift)."""
        key = (role, schema.__name__, id(self.team))
        if key not in self._prompt_cache:
            parts = [self._common, self._role_prompts[role]]
            if self.team is not None:
                parts.append(self.team.section(role))
            if self.karo_spec and role in SPEC_ROLES:
                parts.append("## Kontext: Karo-Spezifikation (Auszug)\n" + self.karo_spec)
            if role == "visual_didaktiker":
                from .visuals.examples import EXAMPLES
                sample = {k: EXAMPLES[k] for k in ("fraction_bar", "number_line", "labeled_diagram", "sentence_parts")}
                parts.append("## Beispiele für Katalog-Darstellungen (Format-Orientierung)\n```json\n"
                             + compact(sample) + "\n```")
            parts.append("## JSON-Schema deiner Antwort\n```json\n" + compact(schema.model_json_schema()) + "\n```")
            self._prompt_cache[key] = "\n\n".join(parts)
        return self._prompt_cache[key]

    def _complete(self, *, role, system, prompt, model, web_search, meta, entity_id):
        """Anbieteraufruf mit Wiederholung bei vorübergehenden Fehlern (exponentiell + Zufall, retry-after)."""
        retries = int(self.cfg.p("provider_retries", 5))
        for attempt in range(retries + 1):
            if self.stop.is_set():
                raise RunStopped("Lauf wurde angehalten")
            pause = self._cooldown_until - time.time()
            if pause > 0 and self.stop.wait(pause):
                raise RunStopped("Lauf wurde angehalten")
            started = time.time()
            try:
                return self.provider.complete(system=system, user=prompt, model=model,
                                              web_search=web_search, meta=meta), started
            except ProviderError as exc:
                self.db.log_call(self.run_id, role, self.provider.name, model, entity_id, 0, 0,
                                 int((time.time() - started) * 1000), False, str(exc))
                if not exc.retryable or attempt == retries:
                    if exc.rate_limited:
                        self.stop.set()
                        raise RateLimited(f"Nutzungslimit/Rate-Limit des Anbieters erreicht: {exc}") from exc
                    raise
                cap = float(self.cfg.p("max_retry_wait", 300))
                wait = min(cap, exc.retry_after or (2 ** attempt) * (10 if exc.rate_limited else 2))
                wait *= 0.8 + 0.4 * random.random()
                if exc.rate_limited:   # alle Worker halten an, statt weiter auf das Limit zu laufen
                    with self._lock:
                        self._cooldown_until = max(self._cooldown_until, time.time() + wait)
                if self.stop.wait(wait):   # Abbruch (Strg+C, Budget) beendet das Warten sofort
                    raise RunStopped("Lauf wurde angehalten")
        raise ProviderError("unerreichbar")  # pragma: no cover

    def call(
        self,
        role: str,
        task: str,
        payload: dict[str, Any],
        schema: type[T],
        *,
        entity_id: str | None = None,
        feedback: str | None = None,
        web_search: bool = False,
        meta: dict[str, Any] | None = None,
        stage: str | None = None,
    ) -> T:
        stage = stage or ROLE_STAGE.get(role)
        system = self.system_prompt(role, schema)
        user = f"## Auftrag\n{task}\n\n## Daten\n```json\n{compact(payload)}\n```"
        if feedback:
            user += ("\n\n## Rückmeldung, die du vollständig umsetzen musst\n" + feedback +
                     "\n\nGib das vollständige, überarbeitete JSON-Objekt zurück.")
        model = self.cfg.model_for(self.provider.name, role)
        meta = {**(meta or {}), "role": role, "entity_id": entity_id, "feedback": feedback, "payload": payload}

        last_error = ""
        attempts = 1 + int(self.cfg.p("max_json_retries", 2))
        for attempt in range(attempts):
            self._count()
            prompt = user
            if last_error:
                prompt += ("\n\n## Deine letzte Antwort war ungültig\n" + last_error[:1500] +
                           "\nAntworte nur mit gültigem JSON nach Schema.")
            try:
                comp, started = self._complete(role=role, system=system, prompt=prompt, model=model,
                                               web_search=web_search, meta={**meta, "attempt": attempt},
                                               entity_id=entity_id)
            except ProviderError as exc:
                raise AgentFailed(f"{ROLES[role]}: Anbieterfehler: {exc}", stage=stage) from exc
            ms = int((time.time() - started) * 1000)
            try:
                if comp.truncated:
                    raise ValueError("Die Antwort wurde wegen Längenbegrenzung abgeschnitten. Antworte kompakter: "
                                     "weniger und kürzere Aufgaben, keine unnötigen Felder.")
                obj = schema.model_validate(extract_json(comp.text))
            except (ValueError, ValidationError) as exc:
                last_error = str(exc)
                self.db.log_call(self.run_id, role, self.provider.name, model, entity_id,
                                 comp.input_tokens, comp.output_tokens, ms, False, "ungültiges JSON: " + last_error)
                continue
            self.db.log_call(self.run_id, role, self.provider.name, model, entity_id,
                             comp.input_tokens, comp.output_tokens, ms, True)
            return obj
        raise AgentFailed(f"{ROLES[role]} lieferte nach {attempts} Versuchen kein gültiges Ergebnis: {last_error[:500]}",
                          stage=stage)
