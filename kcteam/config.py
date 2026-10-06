"""Konfiguration laden: config.yaml + Umgebungsvariablen (${VAR:-default})."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

_ENV_PATTERN = re.compile(r"\$\{([A-Z0-9_]+)(?::-([^}]*))?\}")
ROOT = Path(__file__).resolve().parent.parent


def _expand(value: Any) -> Any:
    if isinstance(value, str):
        return _ENV_PATTERN.sub(lambda m: os.environ.get(m.group(1)) or (m.group(2) or ""), value)
    if isinstance(value, dict):
        return {k: _expand(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand(v) for v in value]
    return value


def load_dotenv(path: Path) -> None:
    """Minimaler .env-Leser (setzt nur Variablen, die noch nicht gesetzt sind)."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip().strip('"').strip("'")
        if val and key not in os.environ:
            os.environ[key] = val


@dataclass
class Config:
    raw: dict[str, Any]
    provider: str
    database_url: str
    karo_spec_path: str
    karo_spec_max_chars: int
    pipeline: dict[str, Any] = field(default_factory=dict)
    export_dir: str = "/data/export"
    alerts: dict[str, Any] = field(default_factory=dict)

    def provider_settings(self, name: str) -> dict[str, Any]:
        return dict(self.raw.get("providers", {}).get(name, {}))

    def model_for(self, provider: str, role: str) -> str:
        from . import capabilities
        return capabilities.model_for(self, provider, role)

    def token_budget_for(self, role: str) -> int | None:
        from . import capabilities
        return capabilities.token_budget_for(self, role)

    def p(self, key: str, default: Any = None) -> Any:
        return self.pipeline.get(key, default)


def load_config(path: str | os.PathLike | None = None) -> Config:
    load_dotenv(ROOT / ".env")
    cfg_path = Path(path or os.environ.get("KCTEAM_CONFIG") or ROOT / "config.yaml")
    raw = _expand(yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {})
    return Config(
        raw=raw,
        provider=os.environ.get("KCTEAM_AI_PROVIDER") or raw.get("provider") or "claude_token",
        database_url=(raw.get("database") or {}).get("url", ""),
        karo_spec_path=raw.get("karo_spec_path", "/karo"),
        karo_spec_max_chars=int(raw.get("karo_spec_max_chars", 12000)),
        pipeline=raw.get("pipeline", {}) or {},
        export_dir=raw.get("export_dir", "/data/export"),
        alerts=raw.get("alerts", {}) or {},
    )


def load_karo_spec(cfg: Config) -> str:
    """Liest die Karo-Spezifikation vom Volume (Datei oder alle .md in einem Ordner)."""
    p = Path(cfg.karo_spec_path)
    texts: list[str] = []
    if p.is_file():
        texts.append(p.read_text(encoding="utf-8", errors="ignore"))
    elif p.is_dir():
        for f in sorted(p.rglob("*.md"))[:20]:
            texts.append(f"# Datei: {f.name}\n" + f.read_text(encoding="utf-8", errors="ignore"))
    text = "\n\n".join(texts).strip()
    return text[: cfg.karo_spec_max_chars]
