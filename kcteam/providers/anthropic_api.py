"""Claude API mit ANTHROPIC_API_KEY – mit Prompt-Caching für die großen, gleichbleibenden System-Prompts."""
from __future__ import annotations

from .base import Completion, Provider, ProviderError


class AnthropicAPIProvider(Provider):
    def __init__(self, settings: dict):
        super().__init__(name="anthropic_api", settings=settings, required_env=("ANTHROPIC_API_KEY",))
        self._client = None

    @property
    def client(self):
        if self._client is None:
            import anthropic
            self._client = anthropic.Anthropic(max_retries=0)   # Wiederholungen steuert der AgentRunner
        return self._client

    def complete(self, *, system, user, model, web_search=False, meta=None) -> Completion:
        import anthropic
        messages = [{"role": "user", "content": user}]
        kwargs = dict(
            model=model,
            max_tokens=int((meta or {}).get("max_tokens") or self.settings.get("max_tokens", 16000)),
            # System-Prompt ist pro Rolle identisch -> Cache spart bei tausenden Aufrufen den Großteil der Kosten
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        )
        if web_search and self.web_search_enabled:
            kwargs["tools"] = [{"type": "web_search_20250305", "name": "web_search", "max_uses": 8}]
        text, tin, tout, truncated = "", 0, 0, False
        try:
            for _ in range(4):   # pause_turn (lange Websuche) fortsetzen
                with self.client.messages.stream(messages=messages, **kwargs) as stream:
                    msg = stream.get_final_message()
                u = msg.usage
                tin += (u.input_tokens or 0) + (getattr(u, "cache_read_input_tokens", 0) or 0) \
                    + (getattr(u, "cache_creation_input_tokens", 0) or 0)
                tout += u.output_tokens or 0
                text += "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
                if msg.stop_reason == "pause_turn":
                    messages = messages + [{"role": "assistant", "content": msg.content}]
                    continue
                truncated = msg.stop_reason == "max_tokens"
                break
        except anthropic.RateLimitError as exc:
            ra = None
            try:
                ra = float(exc.response.headers.get("retry-after"))
            except (AttributeError, TypeError, ValueError):
                pass
            raise ProviderError(f"Anthropic API Rate-Limit: {exc}", rate_limited=True, retryable=True,
                                retry_after=ra) from exc
        except anthropic.APIStatusError as exc:
            raise ProviderError(f"Anthropic API {exc.status_code}: {exc}",
                                retryable=exc.status_code >= 500 or exc.status_code == 529) from exc
        except anthropic.APIConnectionError as exc:
            raise ProviderError(f"Anthropic API Verbindung: {exc}", retryable=True) from exc
        return Completion(text=text, input_tokens=tin, output_tokens=tout, model=model, truncated=truncated)
