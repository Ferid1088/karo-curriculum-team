"""Modell-Faehigkeiten statt Modellnamen (PART 15-17).

Die Domaenenlogik kennt nur Faehigkeiten (reasoning_xhigh, writing_high …).
Die Konfiguration bildet Rolle -> Faehigkeit -> konkretes Modell ab; kein
OpenRouter-Modellname steht in der Curriculum-Logik.

Die Zuordnung ist eine Anfangshypothese (PART 89): nach dem Benchmarking
werden schwache Rollen hochgestuft und teure, die nichts bringen,
heruntergestuft – in der config.yaml, nicht im Code.
"""
from __future__ import annotations

CAPABILITIES = (
    "reasoning_xhigh",   # haerteste Denkarbeit: Vollstaendigkeit, Reise-Reparatur
    "reasoning_high",    # Strukturierung: Kompetenzen, Graphen, Vorlagen
    "reasoning_balanced",# Alltagsarbeit mit Verstand
    "writing_high",      # saubere, kindgerechte Prosa
    "high_volume",       # viele kleine Laeufe (Simulation)
    "multimodal_high",   # Bilder beurteilen
    "image_generation",  # Bilder erzeugen
)

#: Logische Rollen der Fabrik -> Faehigkeit (PART 14/16).
ROLE_CAPABILITY: dict[str, str] = {
    "katalog_rechercheur":             "reasoning_high",
    "kompetenz_architekt":             "reasoning_high",
    "lernreise_architekt":             "reasoning_xhigh",
    "fachexperte":                     "reasoning_high",
    "fehlvorstellungs_analytiker":     "reasoning_balanced",
    "didaktik_designer":               "writing_high",
    "erklaerautor":                    "writing_high",
    "aufgaben_designer":               "reasoning_balanced",
    "vorlagen_ingenieur":              "reasoning_high",
    "rubrik_ingenieur":                "reasoning_high",
    "visueller_lerndesigner":          "reasoning_balanced",
    "bildprompt_designer":             "writing_high",
    "visueller_inspektor":             "multimodal_high",
    "pruefungs_designer":              "reasoning_balanced",
    "lernsimulator":                   "high_volume",
    "curriculum_kritiker":             "reasoning_high",
    "vollstaendigkeits_kontrolleur":   "reasoning_xhigh",
    "bild_generierung":                "image_generation",
    # bestehende Rollen bleiben nutzbar (KEEP/EXTEND, keine Parallelwelt)
    "curriculum_analyst":              "reasoning_high",
    "fachdidaktiker":                  "reasoning_high",
    "niveau_kalibrierer":              "reasoning_balanced",
    "diagnostiker":                    "reasoning_balanced",
    "visual_didaktiker":               "reasoning_balanced",
    "kritiker":                        "reasoning_high",
    "kinderrechts_inspektor":          "reasoning_xhigh",
    "curriculum_agent":                "reasoning_high",
    "lektionsautor":                   "writing_high",
}

#: Tokenbudget je Rolle (PART 17) – Anfangswerte, konfigurierbar in
#: config.yaml unter `role_token_budget`.
DEFAULT_TOKEN_BUDGET: dict[str, int] = {
    "katalog_rechercheur":           16000,
    "kompetenz_architekt":           18000,
    "lernreise_architekt":           18000,
    "fachexperte":                   10000,
    "fehlvorstellungs_analytiker":   12000,
    "didaktik_designer":             16000,
    "erklaerautor":                  16000,
    "aufgaben_designer":             20000,   # gechunked, pro Ebene/Rollen-Batch
    "vorlagen_ingenieur":            16000,
    "rubrik_ingenieur":              16000,
    "visueller_lerndesigner":         8000,
    "bildprompt_designer":            8000,
    "visueller_inspektor":            8000,
    "pruefungs_designer":            12000,
    "lernsimulator":                  6000,   # pro Simulations-Batch
    "curriculum_kritiker":           12000,
    "vollstaendigkeits_kontrolleur": 12000,
}

#: Anfangsbasis OpenRouter (PART 16) – als config.yaml-Defaults referenziert,
#: NICHT fest verdrahtet. Nach dem Benchmarking wird angepasst.
OPENROUTER_BASELINE: dict[str, str] = {
    "reasoning_xhigh":    "openai/gpt-5.6-sol-pro",
    "reasoning_high":     "openai/gpt-5.6-sol",
    "reasoning_balanced": "openai/gpt-5.6-terra",
    "writing_high":       "anthropic/claude-sonnet-5.5",
    "high_volume":        "openai/gpt-5.6-luna",
    "multimodal_high":    "anthropic/claude-sonnet-5.5",
    "image_generation":   "openai/gpt-5-image",
}


def capability_for(role: str) -> str | None:
    return ROLE_CAPABILITY.get(role)


def model_for(cfg, provider: str, role: str) -> str:
    """Rolle -> Faehigkeit -> Modell.

    Kette: role_models[provider][role] (explizit) > capabilities[provider][cap]
    > providers[provider].default_model.
    """
    per_role = (cfg.raw.get("role_models", {}) or {}).get(provider, {}) or {}
    if role in per_role:
        return per_role[role]
    cap = capability_for(role)
    cap_models = (cfg.raw.get("capabilities", {}) or {}).get(provider, {}) or {}
    if cap and cap in cap_models:
        return cap_models[cap]
    return cfg.provider_settings(provider).get("default_model", "")


def token_budget_for(cfg, role: str) -> int | None:
    """Maximale Ausgabe-Tokens je Rolle; None = Provider-Standard."""
    budgets = (cfg.raw.get("role_token_budget", {}) or {})
    if role in budgets:
        return int(budgets[role])
    return DEFAULT_TOKEN_BUDGET.get(role)
