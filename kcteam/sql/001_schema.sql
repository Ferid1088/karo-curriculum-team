-- Karo Curriculum Team – Datenbankschema (PostgreSQL 14+ / Supabase)
-- Schema "curriculum": Arbeitsdaten des Teams inkl. Audit.
-- Schema "karo":       das, was Karo liest – nur freigegebene Inhalte.
-- Idempotent: kann mehrfach ausgeführt werden.

CREATE SCHEMA IF NOT EXISTS curriculum;
CREATE SCHEMA IF NOT EXISTS karo;

CREATE TABLE IF NOT EXISTS curriculum.schema_meta (key text PRIMARY KEY, value text NOT NULL);

CREATE TABLE IF NOT EXISTS curriculum.schema_version (
    version    int PRIMARY KEY,
    applied_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS curriculum.subjects (
    code        text PRIMARY KEY,              -- z. B. MA
    name        text NOT NULL UNIQUE,          -- z. B. Mathematik
    grade_min   int  NOT NULL,
    grade_max   int  NOT NULL,
    status      text NOT NULL DEFAULT 'mapped',  -- mapped | blocked
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS curriculum.runs (
    id          uuid PRIMARY KEY,
    subject     text,
    grade_min   int,
    grade_max   int,
    provider    text NOT NULL,
    status      text NOT NULL DEFAULT 'running',  -- running | finished | failed | budget_exhausted
    stats       jsonb NOT NULL DEFAULT '{}'::jsonb,
    error       text,
    started_at  timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz
);

CREATE TABLE IF NOT EXISTS curriculum.topic_blocks (
    id             text PRIMARY KEY,           -- MA.BRUECHE
    subject_code   text NOT NULL REFERENCES curriculum.subjects(code) ON DELETE CASCADE,
    title          text NOT NULL,
    description    text NOT NULL DEFAULT '',
    grade_min      int  NOT NULL,
    grade_max      int  NOT NULL,
    typical_grade  int  NOT NULL,
    varies         boolean NOT NULL DEFAULT false,
    variance_note  text,
    sources        jsonb NOT NULL DEFAULT '[]'::jsonb,
    status         text NOT NULL DEFAULT 'pending',  -- pending | structured | reviewed | done | blocked
    pending_feedback text,
    updated_at     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS topic_blocks_subject_idx ON curriculum.topic_blocks(subject_code);

CREATE TABLE IF NOT EXISTS curriculum.concepts (
    id                    text PRIMARY KEY,    -- MA.BRUECHE.ADD_UNGL
    block_id              text NOT NULL REFERENCES curriculum.topic_blocks(id) ON DELETE CASCADE,
    subject_code          text NOT NULL REFERENCES curriculum.subjects(code) ON DELETE CASCADE,
    title                 text NOT NULL,
    description           text NOT NULL DEFAULT '',
    first_contact_grade   int  NOT NULL,
    target_grade          int  NOT NULL,
    varies                boolean NOT NULL DEFAULT false,
    sort_order            int  NOT NULL DEFAULT 0,
    levels                jsonb,               -- {below, target, above}
    can_do                jsonb,               -- {below:[], target:[], above:[]}
    difficulty_parameters jsonb,               -- messbare Grenzen des Zielniveaus
    calibration           jsonb,               -- vollständige Ausgabe des Kalibrierers
    diagnostics           jsonb,               -- vollständige Ausgabe des Diagnostikers
    status                text NOT NULL DEFAULT 'structured',
        -- structured | calibrated | diagnosed | visualized | approved | blocked | retired
    visual_need           text,                -- essential | helpful | none
    track                 text NOT NULL DEFAULT 'all',  -- Oberstufe: all | gA | eA
    learning_year         int,                 -- Fremdsprachen: Lernjahr
    cefr                  text,                -- Fremdsprachen: GER-Niveau
    visuals               jsonb,               -- vollständige Ausgabe des Visual-Didaktikers
    pending_feedback      text,                -- Hinweis eines Menschen für den nächsten Lauf
    human_overrides       jsonb NOT NULL DEFAULT '[]'::jsonb,  -- Befunde, die ein Mensch ausdrücklich freigegeben hat
    version               int  NOT NULL DEFAULT 1,
    created_at            timestamptz NOT NULL DEFAULT now(),
    updated_at            timestamptz NOT NULL DEFAULT now(),
    CHECK (first_contact_grade <= target_grade)
);
CREATE INDEX IF NOT EXISTS concepts_block_idx   ON curriculum.concepts(block_id);
CREATE INDEX IF NOT EXISTS concepts_subject_idx ON curriculum.concepts(subject_code, target_grade);
CREATE INDEX IF NOT EXISTS concepts_status_idx  ON curriculum.concepts(status);

-- Voraussetzungskanten. prerequisite_id ohne Fremdschlüssel, weil Kanten auf Konzepte
-- in noch nicht bearbeiteten Blöcken zeigen dürfen (Integrator meldet fehlende).
CREATE TABLE IF NOT EXISTS curriculum.concept_prerequisites (
    concept_id      text NOT NULL REFERENCES curriculum.concepts(id) ON DELETE CASCADE,
    prerequisite_id text NOT NULL,
    PRIMARY KEY (concept_id, prerequisite_id),
    CHECK (concept_id <> prerequisite_id)
);
CREATE INDEX IF NOT EXISTS prereq_reverse_idx ON curriculum.concept_prerequisites(prerequisite_id);

CREATE TABLE IF NOT EXISTS curriculum.misconceptions (
    id               text PRIMARY KEY,         -- MA.BRUECHE.ADD_UNGL.F1
    concept_id       text NOT NULL REFERENCES curriculum.concepts(id) ON DELETE CASCADE,
    key              text NOT NULL,
    description      text NOT NULL,
    remediation_hint text NOT NULL
);
CREATE INDEX IF NOT EXISTS misconceptions_concept_idx ON curriculum.misconceptions(concept_id);

CREATE TABLE IF NOT EXISTS curriculum.items (
    id               text PRIMARY KEY,         -- MA.BRUECHE.ADD_UNGL.EXIT_1
    concept_id       text NOT NULL REFERENCES curriculum.concepts(id) ON DELETE CASCADE,
    kind             text NOT NULL,
    level            text NOT NULL CHECK (level IN ('below','target','above')),
    grade            int  NOT NULL,
    prompt           text NOT NULL,
    solution         text NOT NULL,
    representation   text,
    misconception_id text REFERENCES curriculum.misconceptions(id) ON DELETE CASCADE,
    sort_order       int  NOT NULL DEFAULT 0,
    answer           jsonb,                    -- auswertbare Antwort (Typ + richtige Lösung)
    distractors      jsonb NOT NULL DEFAULT '[]'::jsonb,  -- typische falsche Antworten -> Fehlvorstellung
    auto_checkable   boolean NOT NULL DEFAULT false,
    visual           jsonb,                    -- Visual-Spec (nur bei visuellen Aufgaben)
    visual_svg       text,                     -- vorgerendertes SVG
    interaction      text                      -- view | select | mark | drag | input
);

-- Visuelle Erklärungen: Schrittfolgen aus Darstellung + kurzem Erklärsatz
CREATE TABLE IF NOT EXISTS curriculum.visual_explanations (
    id               text PRIMARY KEY,         -- MA.BRUECHE.ADD_UNGL.V1
    concept_id       text NOT NULL REFERENCES curriculum.concepts(id) ON DELETE CASCADE,
    key              text NOT NULL,
    purpose          text NOT NULL,
    level            text NOT NULL,
    misconception_id text,                     -- z. B. MA.BRUECHE.ADD_UNGL.F1
    steps            jsonb NOT NULL,           -- [{caption, visual, svg}]
    sort_order       int  NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS visual_expl_concept_idx ON curriculum.visual_explanations(concept_id);
CREATE INDEX IF NOT EXISTS items_concept_idx ON curriculum.items(concept_id, kind);

-- Jede Prüfentscheidung (Inspektor, Kritiker, Integrator, Mensch) – vollständiges Audit.
CREATE TABLE IF NOT EXISTS curriculum.reviews (
    id           bigserial PRIMARY KEY,
    run_id       uuid,
    entity_type  text NOT NULL,                -- curriculum | block | concept
    entity_id    text NOT NULL,
    stage        text NOT NULL,                -- curriculum | graph | calibration | diagnostics | final | critic | integrator
    reviewer     text NOT NULL,                -- kinderrechts_inspektor | kritiker | integrator | human
    decision     text NOT NULL,                -- approved | rejected | issues | ok | override_approve | override_reject | retry
    round        int  NOT NULL DEFAULT 1,
    findings     jsonb NOT NULL DEFAULT '[]'::jsonb,
    note         text,
    created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS reviews_entity_idx ON curriculum.reviews(entity_type, entity_id);

-- Warteschlange für Menschen: gesperrte Inhalte nach 3 Inspektor-Ablehnungen, fehlende Voraussetzungen usw.
CREATE TABLE IF NOT EXISTS curriculum.human_queue (
    id           bigserial PRIMARY KEY,
    entity_type  text NOT NULL,
    entity_id    text NOT NULL,
    stage        text NOT NULL,
    kind         text NOT NULL DEFAULT 'veto',  -- veto | missing_prerequisite | error
    reason       text NOT NULL,
    payload      jsonb NOT NULL DEFAULT '{}'::jsonb,
    status       text NOT NULL DEFAULT 'open',  -- open | resolved
    resolution   text,
    resolved_by  text,
    created_at   timestamptz NOT NULL DEFAULT now(),
    resolved_at  timestamptz
);
CREATE INDEX IF NOT EXISTS human_queue_open_idx ON curriculum.human_queue(status);

CREATE TABLE IF NOT EXISTS curriculum.agent_calls (
    id            bigserial PRIMARY KEY,
    run_id        uuid,
    role          text NOT NULL,
    provider      text NOT NULL,
    model         text,
    entity_id     text,
    input_tokens  int NOT NULL DEFAULT 0,
    output_tokens int NOT NULL DEFAULT 0,
    duration_ms   int NOT NULL DEFAULT 0,
    ok            boolean NOT NULL,
    error         text,
    created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS agent_calls_run_idx ON curriculum.agent_calls(run_id);

-- Nachrüsten für bestehende Datenbanken
ALTER TABLE curriculum.concepts ADD COLUMN IF NOT EXISTS human_overrides jsonb NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE curriculum.topic_blocks ADD COLUMN IF NOT EXISTS pending_feedback text;
ALTER TABLE curriculum.concepts ADD COLUMN IF NOT EXISTS visual_need text;
ALTER TABLE curriculum.concepts ADD COLUMN IF NOT EXISTS track text NOT NULL DEFAULT 'all';
ALTER TABLE curriculum.concepts ADD COLUMN IF NOT EXISTS learning_year int;
ALTER TABLE curriculum.concepts ADD COLUMN IF NOT EXISTS cefr text;
ALTER TABLE curriculum.items ADD COLUMN IF NOT EXISTS answer jsonb;
ALTER TABLE curriculum.items ADD COLUMN IF NOT EXISTS distractors jsonb NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE curriculum.items ADD COLUMN IF NOT EXISTS auto_checkable boolean NOT NULL DEFAULT false;
ALTER TABLE curriculum.subjects ADD COLUMN IF NOT EXISTS profile text;
ALTER TABLE curriculum.concepts ADD COLUMN IF NOT EXISTS visuals jsonb;
ALTER TABLE curriculum.items ADD COLUMN IF NOT EXISTS visual jsonb;
ALTER TABLE curriculum.items ADD COLUMN IF NOT EXISTS visual_svg text;
ALTER TABLE curriculum.items ADD COLUMN IF NOT EXISTS interaction text;
DO $$BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'items_kind_check'
                   AND pg_get_constraintdef(oid) LIKE '%practice%') THEN
        ALTER TABLE curriculum.items DROP CONSTRAINT IF EXISTS items_kind_check;
        ALTER TABLE curriculum.items ADD CONSTRAINT items_kind_check
            CHECK (kind IN ('anchor','boundary','diagnostic','misconception','exit','practice'));
    END IF;
END$$;

INSERT INTO curriculum.schema_version(version) VALUES (1) ON CONFLICT DO NOTHING;
