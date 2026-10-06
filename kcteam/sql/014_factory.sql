-- Curriculum Factory (Master-Phase): Katalog, Komplettpakete, Sammelauftraege.
--
-- Der Katalog beantwortet: WAS soll gelernt werden? Er ist zweistufig –
-- recherchiert landet nie direkt im freigegebenen Bestand, sondern als
-- Change-Set mit Diff, das ein Mensch freigibt (PART 9/10).
--
-- Das Paket (COMPLETE_TOPIC_PACKAGE) traegt alles, was Karo fuer eine
-- deterministische Lernreise braucht. package_stages macht den Bau
-- fortsetzbar: ein Neustart laeuft dort weiter, wo es aufhoerte.

-- ---------------------------------------------------------------- Katalog
CREATE TABLE IF NOT EXISTS curriculum.catalog_items (
    id              text PRIMARY KEY,           -- stabiler Schluessel, z. B. DE-BY-GYM.MA.7.BRUECHE.ADD
    parent_id       text REFERENCES curriculum.catalog_items(id) ON DELETE SET NULL,
    kind            text NOT NULL,              -- subject|domain|topic|subtopic|competency|atomic_concept
    title           text NOT NULL,
    description     text NOT NULL DEFAULT '',
    -- Geltungsbereich: fuer welchen Lehrplan dieser Eintrag gilt
    framework       text NOT NULL DEFAULT 'de-kmk',  -- Lehrplanwerk (KMK-Bildungsstandards, Land-Lehrplan …)
    country         text NOT NULL DEFAULT 'DE',
    region          text NOT NULL DEFAULT '',        -- Bundesland, leer = bundesweit
    school_type     text NOT NULL DEFAULT '',        -- GS|GY|GES|RS| … , leer = alle
    grade           int,                             -- Thema und feiner: Klassenstufe
    subject_code    text NOT NULL,
    path            text[] NOT NULL DEFAULT '{}',    -- Titelpfad fuer Anzeige/Suche
    sort_order      int  NOT NULL DEFAULT 0,
    status          text NOT NULL DEFAULT 'active',  -- active|deprecated|deactivated
    -- Herkunft (PART 8): woher kommt dieser Eintrag, wer hat ihn gesehen?
    origin          text NOT NULL DEFAULT 'manual',  -- manual|agent_research|import
    source          text NOT NULL DEFAULT '',
    source_version  text NOT NULL DEFAULT '',
    source_reference text NOT NULL DEFAULT '',
    retrieved_at    timestamptz,
    agent_role      text,
    provider        text,
    model           text,
    prompt_version  text,
    confidence      numeric,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS catalog_items_scope_idx
    ON curriculum.catalog_items(framework, region, school_type, subject_code, grade);
CREATE INDEX IF NOT EXISTS catalog_items_parent_idx ON curriculum.catalog_items(parent_id);
CREATE INDEX IF NOT EXISTS catalog_items_status_idx ON curriculum.catalog_items(status);

-- Recherche-Ergebnis: ein Satz vorgeschlagener Aenderungen, noch nicht wirksam.
CREATE TABLE IF NOT EXISTS curriculum.catalog_change_sets (
    id           bigserial PRIMARY KEY,
    scope        jsonb NOT NULL,           -- {framework, region, school_type, grades[], subjects[]}
    status       text NOT NULL DEFAULT 'staged',   -- staged|approved|rejected|applied|superseded
    provider     text,
    model        text,
    agent_role   text,
    run_id       uuid,
    summary      text,
    created_at   timestamptz NOT NULL DEFAULT now(),
    decided_at   timestamptz,
    decided_by   text
);

-- Eine Aenderung im Set. Niemals loeschen – DEPRECATE stattdessen (PART 10).
CREATE TABLE IF NOT EXISTS curriculum.catalog_changes (
    id            bigserial PRIMARY KEY,
    change_set_id bigint NOT NULL REFERENCES curriculum.catalog_change_sets(id) ON DELETE CASCADE,
    op            text NOT NULL,           -- ADD|RENAME|UPDATE|MOVE|SPLIT|MERGE|DEPRECATE|UNCHANGED
    item_id       text,                    -- Ziel im Bestand (NULL bei ADD)
    proposed      jsonb NOT NULL,          -- der vorgeschlagene Eintrag (catalog_items-Form)
    diff_note     text NOT NULL DEFAULT '',
    decision      text NOT NULL DEFAULT 'pending',  -- pending|approved|edited|rejected
    edited        jsonb,                   -- vom Menschen nachbearbeitete Fassung
    decided_by    text,
    UNIQUE (change_set_id, op, item_id, proposed)
);
CREATE INDEX IF NOT EXISTS catalog_changes_set_idx ON curriculum.catalog_changes(change_set_id);

-- ---------------------------------------------------------------- Komplettpakete
CREATE TABLE IF NOT EXISTS curriculum.complete_packages (
    topic_id         text PRIMARY KEY,     -- Verweis auf catalog_items.id (Thema)
    concept_id       text,                 -- zugeordnetes Konzept, falls vorhanden
    status           text NOT NULL DEFAULT 'CATALOG_ONLY',
        -- CATALOG_ONLY|BUILDING|PARTIAL|READY_CORE|READY_COMPLETE|OUTDATED|REVIEW_REQUIRED|FAILED|BLOCKED
    content          jsonb,                -- das COMPLETE_TOPIC_PACKAGE selbst
    manifest         jsonb,                -- CompletenessManifest (PART 67)
    coverage         jsonb,                -- Abdeckung je Dimension (PART 108)
    topic_version    int  NOT NULL DEFAULT 1,
    content_version  int  NOT NULL DEFAULT 1,
    contract_version text NOT NULL DEFAULT 'ctp-v1',
    fail_reason      text,
    updated_at       timestamptz NOT NULL DEFAULT now(),
    created_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS complete_packages_status_idx ON curriculum.complete_packages(status);

-- Fortsetzbarer Bau (PART 80): je Thema eine Zeile pro Stufe; eine fertige
-- Stufe laeuft nicht noch einmal, ausser sie wurde gezielt entwertet.
CREATE TABLE IF NOT EXISTS curriculum.package_stages (
    topic_id    text NOT NULL REFERENCES curriculum.complete_packages(topic_id) ON DELETE CASCADE,
    stage       text NOT NULL,             -- catalog|competency|journey|misconceptions|didactics|explanations|
                                         -- assessments|templates|rubrics|visuals|retention|compile|
                                         -- deadend|simulate|manifest
    status      text NOT NULL DEFAULT 'pending',  -- pending|running|done|failed|invalidated
    result      jsonb,
    detail      text,
    run_id      uuid,
    updated_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (topic_id, stage)
);

-- ---------------------------------------------------------------- Sammelauftraege
CREATE TABLE IF NOT EXISTS curriculum.bulk_jobs (
    id            bigserial PRIMARY KEY,
    scope         jsonb NOT NULL,          -- Auswahl des Menschen (PART 3/77)
    mode          text NOT NULL DEFAULT 'missing',
        -- missing|repair|regenerate_outdated|regenerate_component|full_rebuild (PART 83)
    status        text NOT NULL DEFAULT 'planned',  -- planned|running|paused|done|failed|cancelled
    estimate      jsonb NOT NULL DEFAULT '{}'::jsonb,  -- Rollenaufrufe, Tokens, Kosten (Vorschau)
    confirmed_by  text,                    -- ohne Bestaetigung laeuft nichts (PART 103)
    stats         jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at    timestamptz NOT NULL DEFAULT now(),
    started_at    timestamptz,
    finished_at   timestamptz
);

CREATE TABLE IF NOT EXISTS curriculum.bulk_job_items (
    id          bigserial PRIMARY KEY,
    job_id      bigint NOT NULL REFERENCES curriculum.bulk_jobs(id) ON DELETE CASCADE,
    topic_id    text NOT NULL,
    status      text NOT NULL DEFAULT 'pending',   -- pending|running|done|failed|skipped
    attempts    int  NOT NULL DEFAULT 0,
    detail      text,
    updated_at  timestamptz NOT NULL DEFAULT now(),
    UNIQUE (job_id, topic_id)
);
CREATE INDEX IF NOT EXISTS bulk_job_items_job_idx ON curriculum.bulk_job_items(job_id, status);

-- Karo liest nur fertige Pakete.
CREATE OR REPLACE VIEW karo.complete_packages AS
SELECT p.topic_id, p.concept_id, p.status, p.content, p.manifest, p.coverage,
       p.topic_version, p.content_version, p.contract_version, p.updated_at
FROM curriculum.complete_packages p
WHERE p.status IN ('READY_CORE', 'READY_COMPLETE');

INSERT INTO curriculum.schema_version(version) VALUES (14) ON CONFLICT DO NOTHING;
