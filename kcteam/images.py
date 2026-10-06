"""Bildpipeline (PART 53-57, Phasen 8-13 der Schleife).

Nur fuer ILLUSTRATIVE_IMAGE: Praezisionsvisuals (Zahlenstrahl, Geometrie,
Diagramme, Tabellen, Zeitstrahlen …) laufen weiter ueber den deterministischen
Renderer in kcteam/visuals – ein Bildmodell darf keine Geometrie oder Texte
halluzinieren, die fachlich falsch sein koennten.

Ablauf je Asset:
  visueller_lerndesigner (Paket) -> bildprompt_designer (Prompt)
  -> ImageProvider.generate() -> visueller_inspektor (Multimodal)
  -> curriculum.image_assets -> VisualAsset.image_ref

Die Domaenenlogik kennt nur `ImageProvider.generate()` – kein Providername
steht in der Pipeline. Der erste reale Adapter ist OpenAI-kompatibel
(openai/openrouter /images/generations); der Mock erzeugt deterministische
SVG-Platzhalter fuer Tests ohne Netz und ohne Kosten.
"""
from __future__ import annotations

import hashlib
import json
import os
import urllib.request
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, Field


# ---------------------------------------------------------------- Provider-Vertrag
class ImageResult(BaseModel):
    provider: str
    model: str
    data: bytes
    mime: str = "image/png"


class ImageProvider(Protocol):
    name: str

    def generate(self, prompt: str, *, negative: str = "") -> ImageResult:
        ...


class MockImageProvider:
    """Deterministischer Platzhalter: ein SVG mit dem Prompt-Hash. Fuer
    Tests und Offline-Laeufe – niemals als 'echtes' Bild ausgeben."""
    name = "mock"

    def generate(self, prompt: str, *, negative: str = "") -> ImageResult:
        h = hashlib.sha256(prompt.encode()).hexdigest()[:8]
        svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64" '
               f'role="img"><title>mock {h}</title>'
               f'<rect width="64" height="64" fill="#dde"/><text x="8" y="36" '
               f'font-size="10">mock:{h}</text></svg>')
        return ImageResult(provider=self.name, model="mock-svg",
                           data=svg.encode(), mime="image/svg+xml")


class OpenAIImagesProvider:
    """OpenAI-kompatible Bild-API (/images/generations). Deckt openai und
    openrouter-Style Endpunkte ab; Konfiguration ueber cfg.image_provider."""
    name = "openai_images"

    def __init__(self, *, base_url: str, model: str, api_key: str,
                 timeout: int = 120):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self._key = api_key
        self.timeout = timeout

    def generate(self, prompt: str, *, negative: str = "") -> ImageResult:
        full = prompt + (f" Vermeide: {negative}" if negative else "")
        body = json.dumps({"model": self.model, "prompt": full,
                           "n": 1, "size": "1024x1024",
                           "response_format": "b64_json"}).encode()
        req = urllib.request.Request(
            f"{self.base_url}/images/generations", data=body,
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self._key}"})
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            payload = json.loads(resp.read())
        import base64
        data = payload["data"][0]
        if "b64_json" in data:
            return ImageResult(provider=self.name, model=self.model,
                               data=base64.b64decode(data["b64_json"]))
        if "url" in data:
            with urllib.request.urlopen(data["url"], timeout=self.timeout) as r:
                return ImageResult(provider=self.name, model=self.model,
                                   data=r.read())
        raise RuntimeError("Bild-API: weder b64_json noch url in der Antwort")


def get_image_provider(cfg) -> ImageProvider:
    """Provider aus der Konfiguration: `image_provider:`-Block oder
    capabilities.image_generation. Ohne Konfiguration: Mock."""
    spec = (cfg.p("image_provider", None) or {})
    kind = spec.get("provider") or spec.get("kind") or ""
    if kind in ("openai", "openai_images", "openrouter"):
        key = os.environ.get(spec.get("api_key_env", "OPENAI_API_KEY"), "")
        return OpenAIImagesProvider(
            base_url=spec.get("base_url", "https://api.openai.com/v1"),
            model=spec.get("model", "gpt-image-1"), api_key=key)
    return MockImageProvider()


# ---------------------------------------------------------------- Rollen-Schemas
class ImagePromptOut(BaseModel):
    prompt: str = Field(min_length=10, max_length=2000)
    negative_prompt: str = ""
    alt_text: str = Field(default="", max_length=500)


class ImageInspectionOut(BaseModel):
    """Der visuelle Inspektor lehnt bei Unsicherheit ab (PART 55)."""
    approved: bool
    findings: list[str] = Field(default_factory=list)
    alt_text_ok: bool = True


# ---------------------------------------------------------------- Pipeline
def _store(cfg, asset_id: str, img: ImageResult) -> str:
    """Bytes -> lokaler Ablagepfad (kein Secret, nur Datei)."""
    root = Path(cfg.p("image_storage_dir", None)
                or os.environ.get("KCTEAM_IMAGE_DIR", "image_assets"))
    ext = "svg" if img.mime == "image/svg+xml" else "png"
    path = root / f"{asset_id}.{ext}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(img.data)
    return str(path)


def generate_image_assets(factory, item: dict, acc: dict) -> dict:
    """Fuer jedes VisualAsset mit visual_type=ILLUSTRATIVE_IMAGE ohne
    image_ref: Prompt -> Bild -> Inspektion -> Persistenz. Liefert
    {"assets": […], "findings": […]} fuer package_stages."""
    from psycopg.types.json import Jsonb

    cfg, db, runner = factory.cfg, factory.db, factory.runner
    provider = get_image_provider(cfg)
    vis = acc.get("visuals", {})
    assets = [a for a in vis.get("visual_assets", [])
              if a.get("visual_type") == "ILLUSTRATIVE_IMAGE" and not a.get("image_ref")]
    results, findings = [], []
    for a in assets:
        aid = a["asset_id"]
        # 1. Bildprompt-Designer: Kind-gerechter, sicherer Prompt
        prompt_out = runner.call(
            "bildprompt_designer",
            f"Formuliere einen sicheren Bildgenerierungs-Prompt fuer das "
            f"Lernasset '{aid}' (Zweck: {a.get('learning_goal','')}).",
            {"asset": a, "thema": {"id": item["id"], "title": item["title"],
                                   "subject_code": item["subject_code"]}},
            ImagePromptOut, entity_id=item["id"], stage="images")
        # 2. Provider erzeugt das Bild
        try:
            img = provider.generate(prompt_out.prompt,
                                    negative=prompt_out.negative_prompt)
        except Exception as exc:
            findings.append(f"{aid}: Bilderzeugung fehlgeschlagen: {exc}")
            db.query(
                """INSERT INTO curriculum.image_assets
                   (asset_id, topic_id, concept_id, learning_purpose, provider,
                    model, status, alt_text, created_at)
                   VALUES (%s,%s,%s,%s,%s,%s,'failed',%s,now())
                   ON CONFLICT (asset_id) DO UPDATE SET status='failed'""",
                (aid, item["id"], "", a.get("learning_goal", ""),
                 provider.name, getattr(provider, "model", "?"),
                 prompt_out.alt_text))
            continue
        ref = _store(cfg, aid, img)
        # 3. Visueller Inspektor: Unsicherheit = Ablehnung
        insp = runner.call(
            "visueller_inspektor",
            f"Pruefe das erzeugte Bild fuer Asset '{aid}' "
            f"(Zweck: {a.get('learning_goal','')}; Alt-Text: "
            f"{prompt_out.alt_text!r}). Bei Unsicherheit ueber fachliche "
            "Korrektheit: ablehnen.",
            {"asset": a, "alt_text": prompt_out.alt_text,
             "provider": img.provider, "model": img.model,
             "storage_ref": ref},
            ImageInspectionOut, entity_id=item["id"], stage="images")
        status = "approved" if insp.approved else "rejected"
        db.query(
            """INSERT INTO curriculum.image_assets
               (asset_id, topic_id, concept_id, learning_purpose, visual_type,
                provider, model, prompt_version, content_version, status,
                inspection_result, alt_text, storage_ref, created_at)
               VALUES (%s,%s,%s,%s,'ILLUSTRATIVE_IMAGE',%s,%s,'1',%s,%s,%s,%s,%s,now())
               ON CONFLICT (asset_id) DO UPDATE SET status=EXCLUDED.status,
                   inspection_result=EXCLUDED.inspection_result,
                   alt_text=EXCLUDED.alt_text, storage_ref=EXCLUDED.storage_ref,
                   provider=EXCLUDED.provider, model=EXCLUDED.model""",
            (aid, item["id"], item.get("concept_id") or "",
             a.get("learning_goal", ""), img.provider, img.model,
             1, status, Jsonb(insp.model_dump(mode="json")),
             prompt_out.alt_text or a.get("accessibility_text", ""), ref))
        if insp.approved:
            a["image_ref"] = aid
            a.setdefault("accessibility_text", prompt_out.alt_text)
        else:
            findings.append(f"{aid}: vom Inspektor abgelehnt "
                            f"({'; '.join(insp.findings) or 'ohne Detail'})")
        results.append({"asset_id": aid, "status": status,
                        "provider": img.provider, "model": img.model})
    return {"assets": results, "findings": findings}
