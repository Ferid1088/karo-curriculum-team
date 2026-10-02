"""Gemeinsame Schnittstelle für alle KI-Zugänge."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any

_RATE = re.compile(r"(rate.?limit|429|too many requests|usage limit|quota|overloaded|529|limit reached|"
                   r"resets? at|credit balance)", re.I)
_TRANSIENT = re.compile(r"(timeout|timed out|connection|temporar|502|503|504|500|internal server|"
                        r"service unavailable|reset by peer|EOF)", re.I)


class ProviderError(RuntimeError):
    def __init__(self, msg: str, *, retryable: bool | None = None, rate_limited: bool | None = None,
                 retry_after: float | None = None):
        super().__init__(msg)
        self.rate_limited = bool(_RATE.search(msg)) if rate_limited is None else rate_limited
        self.retryable = (self.rate_limited or bool(_TRANSIENT.search(msg))) if retryable is None else retryable
        self.retry_after = retry_after


class ProviderPending(Exception):
    """Der Anbieter arbeitet noch — der Auftrag wird zurückgestellt, nicht wiederholt.

    Asynchrone Anbieter (Devin) geben sofort eine Session-Kennung zurück und
    liefern das Ergebnis erst Minuten später. Diese Ausnahme ist kein Fehler:
    sie verbraucht keinen Auftragsversuch, sondern sagt dem Worker „lege den
    Auftrag zurück und frag mich in `wait_seconds` wieder". Beim nächsten Lauf
    findet derselbe Aufruf seine Session über den gespeicherten Schlüssel wieder.
    """

    def __init__(self, msg: str, *, wait_seconds: float | None = None, session_id: str | None = None):
        super().__init__(msg)
        self.wait_seconds = wait_seconds
        self.session_id = session_id


@dataclass
class Completion:
    text: str
    input_tokens: int = 0
    output_tokens: int = 0
    model: str = ""
    truncated: bool = False
    raw: Any = None


@dataclass
class Provider:
    """Basisklasse. `meta` enthält strukturierte Infos zum Auftrag (Rolle, Aufgabe, IDs);
    echte Provider ignorieren sie, der Mock-Provider nutzt sie."""

    name: str = "base"
    settings: dict[str, Any] = field(default_factory=dict)
    required_env: tuple[str, ...] = ()

    def available(self) -> tuple[bool, str]:
        missing = [v for v in self.required_env if not os.environ.get(v)]
        if missing:
            return False, "fehlt: " + ", ".join(missing)
        return True, "ok"

    @property
    def web_search_enabled(self) -> bool:
        return bool(self.settings.get("web_search", False))

    def complete(
        self,
        *,
        system: str,
        user: str,
        model: str,
        web_search: bool = False,
        meta: dict[str, Any] | None = None,
    ) -> Completion:  # pragma: no cover - abstract
        raise NotImplementedError
