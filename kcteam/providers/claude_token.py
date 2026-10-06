"""Claude-Abo-Token (CLAUDE_CODE_OAUTH_TOKEN) über die claude CLI im Print-Modus.

Token erzeugen:  claude setup-token   (einmalig im eigenen Terminal, mit Claude Pro/Max-Abo)
Der System-Prompt geht als Datei an die CLI (--system-prompt-file), die Aufgabe über stdin –
so gibt es keine Längenprobleme mit Kommandozeilen-Argumenten.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time

from .base import Completion, Provider, ProviderError


class ClaudeTokenProvider(Provider):
    def __init__(self, settings: dict):
        # Das OAuth-Token ist NUR in nicht-interaktiven Umgebungen (Docker/CI)
        # nötig: sonst authentifiziert sich die CLI über ihre eigene Anmeldung
        # (Schlüsselbund). Erzwingen, dass beides fehlt, würde eine funktio-
        # nierende Installation als "nicht verfügbar" melden.
        super().__init__(name="claude_token", settings=settings)

    def available(self) -> tuple[bool, str]:
        ok, msg = super().available()
        if not ok:
            return ok, msg
        if not shutil.which("claude"):
            return False, "claude CLI nicht installiert (npm i -g @anthropic-ai/claude-code)"
        if not os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
            return True, "ok (CLI-Anmeldung; kein Token gesetzt – Docker braucht claude setup-token)"
        return True, "ok"

    def complete(self, *, system, user, model, web_search=False, meta=None) -> Completion:
        tools = "WebSearch,WebFetch" if (web_search and self.web_search_enabled) else ""
        with tempfile.NamedTemporaryFile("w", suffix=".md", encoding="utf-8", delete=False) as f:
            f.write(system)
            sys_file = f.name
        cmd = ["claude", "-p", "--output-format", "json", "--system-prompt-file", sys_file, "--tools", tools]
        if tools:
            cmd += ["--allowedTools", tools]
        if model:
            cmd += ["--model", model]
        env = dict(os.environ)
        env.pop("ANTHROPIC_API_KEY", None)  # sonst würde die CLI den API-Key statt des Abos nutzen
        started = time.time()
        try:
            proc = subprocess.run(cmd, input=user, capture_output=True, text=True, env=env,
                                  timeout=int(self.settings.get("timeout_s", 600)))
        except subprocess.TimeoutExpired as exc:
            raise ProviderError(f"claude CLI Timeout nach {exc.timeout}s", retryable=True) from exc
        except OSError as exc:
            raise ProviderError(f"claude CLI nicht ausführbar: {exc}", retryable=False) from exc
        finally:
            try:
                os.unlink(sys_file)
            except OSError:
                pass
        out = proc.stdout.strip()
        if proc.returncode != 0 and not out:
            raise ProviderError(f"claude CLI Fehler ({proc.returncode}): {proc.stderr.strip()[:500]}")
        try:
            data = json.loads(out)
        except json.JSONDecodeError as exc:
            raise ProviderError(f"claude CLI lieferte kein JSON: {out[:300]} {proc.stderr[:200]}") from exc
        if data.get("is_error"):
            raise ProviderError(f"claude CLI: {str(data.get('result'))[:500]}")
        usage = data.get("usage") or {}
        return Completion(
            text=data.get("result") or "",
            input_tokens=sum(int(usage.get(k, 0) or 0) for k in
                             ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")),
            output_tokens=int(usage.get("output_tokens", 0) or 0),
            model=model,
            truncated=str(data.get("stop_reason", "")) == "max_tokens",
            raw={"duration_ms": int((time.time() - started) * 1000), "cost_usd": data.get("total_cost_usd")},
        )
