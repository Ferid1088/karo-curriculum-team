-- 015: Bildpipeline – generierte Illustrationen mit Vertrag und Inspektion.
-- Jedes Asset traegt Herkunft (provider/model), Zweck und Pruefergebnis.

CREATE TABLE IF NOT EXISTS curriculum.image_assets (
    asset_id          text PRIMARY KEY,
    topic_id          text NOT NULL,
    concept_id        text,
    learning_purpose  text NOT NULL DEFAULT '',
    visual_type       text NOT NULL DEFAULT 'ILLUSTRATIVE_IMAGE',
    provider          text NOT NULL,
    model             text NOT NULL,
    prompt_version    text NOT NULL DEFAULT '1',
    content_version   int  NOT NULL DEFAULT 1,
    status            text NOT NULL DEFAULT 'pending',   -- pending|approved|rejected|failed
    inspection_result jsonb,
    alt_text          text NOT NULL DEFAULT '',
    storage_ref       text,                              -- lokaler Pfad / URL, kein Secret
    created_at        timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS image_assets_topic_idx
    ON curriculum.image_assets(topic_id, status);

INSERT INTO curriculum.schema_version(version) VALUES (15) ON CONFLICT DO NOTHING;
