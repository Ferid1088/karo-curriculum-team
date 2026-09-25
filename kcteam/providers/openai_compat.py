"""OpenRouter (Chat Completions) und OpenAI / GPT-Codex (Responses API)."""
from __future__ import annotations

from .base import Completion, Provider, ProviderError


def _wrap(name: str, exc: Exception) -> ProviderError:
    status = getattr(exc, "status_code", None)
    retry_after = None
    try:
        retry_after = float(exc.response.headers.get("retry-after"))  # type: ignore[attr-defined]
    except (AttributeError, TypeError, ValueError):
        pass
    if status == 429:
        return ProviderError(f"{name} Rate-Limit: {exc}", rate_limited=True, retryable=True, retry_after=retry_after)
    if status is not None:
        return ProviderError(f"{name} {status}: {exc}", retryable=status >= 500)
    return ProviderError(f"{name}: {exc}")


class OpenRouterProvider(Provider):
    BASE_URL = "https://openrouter.ai/api/v1"

    def __init__(self, settings: dict):
        super().__init__(name="openrouter", settings=settings, required_env=("OPENROUTER_API_KEY",))
        self._client = None

    @property
    def client(self):
        if self._client is None:
            import os
            from openai import OpenAI
            self._client = OpenAI(base_url=self.BASE_URL, api_key=os.environ["OPENROUTER_API_KEY"], max_retries=0)
        return self._client

    def complete(self, *, system, user, model, web_search=False, meta=None) -> Completion:
        if web_search and self.web_search_enabled and not model.endswith(":online"):
            model = model + ":online"  # OpenRouter-Websuche
        try:
            resp = self.client.chat.completions.create(
                model=model,
                max_tokens=int(self.settings.get("max_tokens", 16000)),
                messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            )
        except Exception as exc:  # noqa: BLE001
            raise _wrap("OpenRouter", exc) from exc
        usage = resp.usage
        if not resp.choices:
            raise ProviderError("OpenRouter: leere Antwort", retryable=True)
        return Completion(
            truncated=resp.choices[0].finish_reason == "length",
            text=resp.choices[0].message.content or "",
            input_tokens=getattr(usage, "prompt_tokens", 0) or 0,
            output_tokens=getattr(usage, "completion_tokens", 0) or 0,
            model=model,
        )


class OpenAIProvider(Provider):
    """OpenAI inkl. Codex-Modellen (z. B. gpt-5-codex) über die Responses API."""

    def __init__(self, settings: dict):
        super().__init__(name="openai", settings=settings, required_env=("OPENAI_API_KEY",))
        self._client = None

    @property
    def client(self):
        if self._client is None:
            from openai import OpenAI
            self._client = OpenAI(max_retries=0)
        return self._client

    def complete(self, *, system, user, model, web_search=False, meta=None) -> Completion:
        kwargs = dict(model=model, instructions=system, input=user)
        if web_search and self.web_search_enabled:
            kwargs["tools"] = [{"type": "web_search"}]
        try:
            resp = self.client.responses.create(**kwargs)
        except Exception as exc:  # noqa: BLE001
            raise _wrap("OpenAI", exc) from exc
        usage = getattr(resp, "usage", None)
        return Completion(
            truncated=getattr(getattr(resp, "incomplete_details", None), "reason", None) == "max_output_tokens",
            text=resp.output_text or "",
            input_tokens=getattr(usage, "input_tokens", 0) or 0,
            output_tokens=getattr(usage, "output_tokens", 0) or 0,
            model=model,
        )
