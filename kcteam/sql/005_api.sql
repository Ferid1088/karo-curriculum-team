-- Curriculum-Service über HTTP (kcteam api): Schlüssel pro Installation, Idempotenz, Lektionsformate für
-- Abnehmer wie Karo, Webhooks. Dazu eine schnellere Sofortsuche (vorberechnete, gefaltete Suchtexte).

-- --------------------------------------------------------------------------- Abnehmer (API-Schlüssel)
CREATE TABLE IF NOT EXISTS curriculum.api_clients (
    id             serial PRIMARY KEY,
    name           text NOT NULL UNIQUE,              -- z. B. karo-familie
    tenant         text NOT NULL,                     -- Einrichtung: bestimmt Tageslimit & Zuordnung
    key_prefix     text NOT NULL,                     -- erste Zeichen des Schlüssels, zum Wiedererkennen
    key_hash       text NOT NULL UNIQUE,              -- sha256 des Schlüssels; der Schlüssel selbst wird nie gespeichert
    webhook_url    text,
    webhook_secret text,
    active         boolean NOT NULL DEFAULT true,
    created_at     timestamptz NOT NULL DEFAULT now(),
    last_used_at   timestamptz
);

CREATE TABLE IF NOT EXISTS curriculum.api_idempotency (
    client_id   int NOT NULL REFERENCES curriculum.api_clients(id) ON DELETE CASCADE,
    key         text NOT NULL,
    endpoint    text NOT NULL,
    status_code int NOT NULL,
    response    jsonb NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (client_id, key)
);
ALTER TABLE curriculum.api_idempotency ADD COLUMN IF NOT EXISTS request_hash text;
CREATE INDEX IF NOT EXISTS api_idempotency_created_idx ON curriculum.api_idempotency(created_at);

-- --------------------------------------------------------------------------- Lektionsformate (Exporte)
-- Ein Abnehmer schickt sein Format mit (JSON-Schema, Darstellungs-Register, Formatregeln). Der Dienst schreibt
-- daraus eine Lektion zu einem freigegebenen Konzept, prüft sie und hält sie pro Konzeptversion vor.
CREATE TABLE IF NOT EXISTS curriculum.lesson_exports (
    id              bigserial PRIMARY KEY,
    client_id       int REFERENCES curriculum.api_clients(id) ON DELETE SET NULL,
    format_id       text NOT NULL,
    format_hash     text NOT NULL,                    -- sha256(schema + register + regeln): anderes Format = neuer Export
    format_spec     jsonb NOT NULL,                   -- {schema, registry, instructions}
    grade           int NOT NULL CHECK (grade BETWEEN 1 AND 13),
    topic           text,
    concept_id      text REFERENCES curriculum.concepts(id),
    concept_version int,
    request_id      bigint REFERENCES curriculum.topic_requests(id),   -- wartet auf einen Auftrag (neues Thema)
    status          text NOT NULL DEFAULT 'queued'
                    CHECK (status IN ('waiting', 'queued', 'running', 'ready', 'failed', 'blocked', 'unavailable')),
    lesson          jsonb,
    reason_code     text,
    message         text,
    attempts        int NOT NULL DEFAULT 0,
    next_attempt_at timestamptz NOT NULL DEFAULT now(),
    heartbeat_at    timestamptz,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    finished_at     timestamptz
);
CREATE INDEX IF NOT EXISTS lesson_exports_queue_idx ON curriculum.lesson_exports(status, next_attempt_at);
CREATE INDEX IF NOT EXISTS lesson_exports_request_idx ON curriculum.lesson_exports(request_id) WHERE status = 'waiting';
-- Suche nach einem gültigen bzw. laufenden Export (Doppelte verhindert eine Advisory-Sperre in lessons.py)
DROP INDEX IF EXISTS curriculum.lesson_exports_live_uq;
CREATE INDEX IF NOT EXISTS lesson_exports_live_idx
    ON curriculum.lesson_exports(concept_id, concept_version, grade, format_hash)
    WHERE status IN ('queued', 'running', 'ready') AND concept_id IS NOT NULL;

-- eigene Fassung für einen Abnehmer, der die geteilte verworfen hat
ALTER TABLE curriculum.lesson_exports ADD COLUMN IF NOT EXISTS forked_for int
    REFERENCES curriculum.api_clients(id) ON DELETE SET NULL;

-- Welcher Abnehmer hat welche Fassung verworfen (Obergrenze gegen endloses Neuschreiben)
CREATE TABLE IF NOT EXISTS curriculum.lesson_export_rejections (
    export_id  bigint NOT NULL REFERENCES curriculum.lesson_exports(id) ON DELETE CASCADE,
    client_id  int NOT NULL REFERENCES curriculum.api_clients(id) ON DELETE CASCADE,
    reason     text,
    -- Warum verworfen wurde, entscheidet ueber die Folgen:
    --   'content'  = fachlich falsch. Zaehlt gegen den Abnehmer; nach
    --                KCTEAM_MAX_CLIENT_REJECTS wird nicht mehr neu geschrieben.
    --   'contract' = Format, Version oder Pflichtfeld passt nicht. Das sagt
    --                nichts ueber den Inhalt und darf ein Thema nicht sperren;
    --                es gehoert auf den Tisch eines Menschen.
    reason_code text NOT NULL DEFAULT 'content'
                CHECK (reason_code IN ('content', 'contract')),
    -- Mit welcher Vertragsfassung der Abnehmer damals sprach. Aendert sie
    -- sich, sind alte Ablehnungen gegenstandslos.
    contract_version text,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (export_id, client_id)
);
ALTER TABLE curriculum.lesson_export_rejections
    ADD COLUMN IF NOT EXISTS reason_code text NOT NULL DEFAULT 'content';
ALTER TABLE curriculum.lesson_export_rejections
    ADD COLUMN IF NOT EXISTS contract_version text;
ALTER TABLE curriculum.lesson_export_rejections
    DROP CONSTRAINT IF EXISTS lesson_export_rejections_reason_code_check;
ALTER TABLE curriculum.lesson_export_rejections
    ADD CONSTRAINT lesson_export_rejections_reason_code_check
    CHECK (reason_code IN ('content', 'contract'));

-- Wer hat welchen Export angefragt (Exporte werden über Abnehmer hinweg geteilt; lesen darf nur, wer angefragt hat)
CREATE TABLE IF NOT EXISTS curriculum.lesson_export_clients (
    export_id  bigint NOT NULL REFERENCES curriculum.lesson_exports(id) ON DELETE CASCADE,
    client_id  int NOT NULL REFERENCES curriculum.api_clients(id) ON DELETE CASCADE,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (export_id, client_id)
);
CREATE INDEX IF NOT EXISTS lesson_export_clients_client_idx ON curriculum.lesson_export_clients(client_id);

CREATE OR REPLACE FUNCTION curriculum._notify_export() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.status = 'queued' AND (TG_OP = 'INSERT' OR OLD.status IS DISTINCT FROM 'queued') THEN
        PERFORM pg_notify('kcteam_requests', 'export:' || NEW.id);
    END IF;
    IF NEW.status IN ('ready', 'failed', 'blocked', 'unavailable')
       AND (TG_OP = 'INSERT' OR OLD.status IS DISTINCT FROM NEW.status) THEN
        PERFORM pg_notify('kcteam_export_ready', json_build_object('export_id', NEW.id, 'status', NEW.status,
                                                                  'client_id', NEW.client_id)::text);
    END IF;
    RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS lesson_exports_notify ON curriculum.lesson_exports;
CREATE TRIGGER lesson_exports_notify AFTER INSERT OR UPDATE OF status ON curriculum.lesson_exports
    FOR EACH ROW EXECUTE FUNCTION curriculum._notify_export();

-- --------------------------------------------------------------------------- Webhooks
CREATE TABLE IF NOT EXISTS curriculum.webhook_deliveries (
    id           bigserial PRIMARY KEY,
    client_id    int REFERENCES curriculum.api_clients(id) ON DELETE CASCADE,
    event        text NOT NULL,
    payload      jsonb NOT NULL,
    status_code  int,
    attempts     int NOT NULL DEFAULT 0,
    error        text,
    created_at   timestamptz NOT NULL DEFAULT now(),
    delivered_at timestamptz
);

-- --------------------------------------------------------------------------- schnellere Sofortsuche
-- Titel + gelernte Suchbegriffe und Beschreibung werden einmal beim Schreiben gefaltet, nicht bei jeder Anfrage.
ALTER TABLE curriculum.concepts ADD COLUMN IF NOT EXISTS search_fold text NOT NULL DEFAULT '';
ALTER TABLE curriculum.concepts ADD COLUMN IF NOT EXISTS description_fold text NOT NULL DEFAULT '';

CREATE OR REPLACE FUNCTION curriculum._concepts_fold() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    NEW.search_fold := curriculum._fold(NEW.title || ' ' || array_to_string(coalesce(NEW.search_terms, '{}'), ' '));
    NEW.description_fold := curriculum._fold(NEW.description);
    RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS concepts_fold ON curriculum.concepts;
CREATE TRIGGER concepts_fold BEFORE INSERT OR UPDATE OF title, description, search_terms ON curriculum.concepts
    FOR EACH ROW EXECUTE FUNCTION curriculum._concepts_fold();
UPDATE curriculum.concepts
   SET search_fold = curriculum._fold(title || ' ' || array_to_string(coalesce(search_terms, '{}'), ' ')),
       description_fold = curriculum._fold(description)
 WHERE search_fold = '' OR search_fold IS DISTINCT FROM
       curriculum._fold(title || ' ' || array_to_string(coalesce(search_terms, '{}'), ' '));

CREATE INDEX IF NOT EXISTS concepts_subject_status_idx ON curriculum.concepts(subject_code, status);

CREATE OR REPLACE FUNCTION curriculum._score_folded(p_words text[], p_title_fold text, p_desc_fold text)
RETURNS real
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
    SELECT CASE WHEN cardinality(p_words) = 0 THEN 0::real ELSE (
        SELECT avg(greatest(curriculum._sim(w, p_title_fold), 0.8 * curriculum._sim(w, p_desc_fold)))::real
        FROM unnest(p_words) w) END
$$;

CREATE OR REPLACE FUNCTION curriculum.match_concepts(p_code text, p_words text[], p_limit int DEFAULT 10,
                                                     p_include_unapproved boolean DEFAULT false)
RETURNS TABLE(concept_id text, title text, target_grade int, status text, score real)
LANGUAGE sql STABLE AS $$
    SELECT c.id, c.title, c.target_grade, c.status,
           curriculum._score_folded(p_words, c.search_fold, c.description_fold) AS score
    FROM curriculum.concepts c
    WHERE c.subject_code = p_code AND c.status <> 'retired'
      AND (p_include_unapproved OR c.status = 'approved')
    ORDER BY score DESC, c.target_grade, c.id
    LIMIT p_limit
$$;
