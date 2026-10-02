"""Auswahl des KI-Zugangs."""
from __future__ import annotations

from .anthropic_api import AnthropicAPIProvider
from .base import Completion, Provider, ProviderError, ProviderPending
from .claude_token import ClaudeTokenProvider
from .devin import DevinProvider
from .mock import MockProvider
from .openai_compat import OpenAIProvider, OpenRouterProvider

PROVIDERS = {
    "claude_token": (ClaudeTokenProvider, "Claude-Abo-Token (claude setup-token)"),
    "anthropic_api": (AnthropicAPIProvider, "Claude API (API-Key)"),
    "openrouter": (OpenRouterProvider, "OpenRouter (beliebige Modelle)"),
    "openai": (OpenAIProvider, "OpenAI / GPT-Codex"),
    "devin": (DevinProvider, "Devin API (asynchron, DEVIN_API_KEY)"),
    "mock": (MockProvider, "Testmodus ohne KI (kostenlos)"),
}


def make_provider(name: str, cfg) -> Provider:
    if name not in PROVIDERS:
        raise ValueError(f"Unbekannter Provider '{name}'. Möglich: {', '.join(PROVIDERS)}")
    cls, _ = PROVIDERS[name]
    return cls(cfg.provider_settings(name))


__all__ = ["PROVIDERS", "make_provider", "Provider", "ProviderError", "ProviderPending", "Completion"]
