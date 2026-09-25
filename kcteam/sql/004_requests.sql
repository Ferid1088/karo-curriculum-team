-- Curriculum-Agent auf Abruf: Karo findet zu einem Arbeitsblatt kein passendes Konzept -> Auftrag an das Team.
--
--   karo.resolve_topic(fach, klasse, thema, stichworte, aufgaben, tenant)
--     Stufe 0/1 sofort in der Datenbank (ohne KI): passendes Konzept (auch auf anderem Niveau) -> zurück.
--     Sonst: Auftrag anlegen (gleiche Anfragen werden zusammengelegt) + nächstbeste Konzepte als Übergang.
--   karo.request_status(auftrag)   Stand eines Auftrags, nur freigegebene Konzepte
--
-- Der Dienst `kcteam serve` hört auf NOTIFY 'kcteam_requests', arbeitet Aufträge ab und meldet
-- NOTIFY 'karo_topic_ready' (JSON {request_id, status, concepts}).

-- --------------------------------------------------------------------------- Textsuche
DO $$
DECLARE s text;
BEGIN
    BEGIN
        CREATE EXTENSION IF NOT EXISTS pg_trgm;
    EXCEPTION WHEN OTHERS THEN
        RAISE NOTICE 'pg_trgm nicht verfügbar – einfache Wortsuche wird verwendet';
    END;
    SELECT n.nspname INTO s FROM pg_extension e JOIN pg_namespace n ON n.oid = e.extnamespace
     WHERE e.extname = 'pg_trgm';
    IF s IS NOT NULL THEN
        EXECUTE format($f$CREATE OR REPLACE FUNCTION curriculum._sim(a text, b text) RETURNS real
                          LANGUAGE sql IMMUTABLE PARALLEL SAFE AS 'SELECT %I.word_similarity(a, b)'$f$, s);
    ELSE
        -- Ersatz: Anteil gemeinsamer Wortanfänge (4 Zeichen)
        CREATE OR REPLACE FUNCTION curriculum._sim(a text, b text) RETURNS real
        LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $f$
            SELECT CASE WHEN position(left(a, 4) IN coalesce(b, '')) > 0 THEN
                        least(1.0, 0.5 + 0.5 * length(left(a, 4))::real / greatest(length(a), 1)) ELSE 0 END::real
        $f$;
    END IF;
END $$;

-- Kleinschreibung, Umlaute auf den Grundbuchstaben (Brüche ~ Bruch), nur Buchstaben/Ziffern
CREATE OR REPLACE FUNCTION curriculum._fold(t text) RETURNS text
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
    SELECT trim(regexp_replace(translate(replace(lower(coalesce(t, '')), 'ß', 'ss'), 'äöüàáâéèêíóôúç', 'aouaaaeeeioouc'),
                               '[^a-z0-9]+', ' ', 'g'))
$$;

CREATE OR REPLACE FUNCTION curriculum._words(t text) RETURNS text[]
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
    SELECT coalesce(array_agg(DISTINCT w ORDER BY w), '{}')
    FROM unnest(string_to_array(curriculum._fold(t), ' ')) w
    WHERE length(w) >= 3 AND w NOT IN (
        'und', 'oder', 'der', 'die', 'das', 'den', 'dem', 'des', 'ein', 'eine', 'einer', 'einen', 'mit', 'von',
        'fuer', 'zum', 'zur', 'auf', 'aus', 'bei', 'nach', 'wie', 'was', 'ist', 'sind', 'klasse', 'aufgabe',
        'aufgaben', 'arbeitsblatt', 'uebung', 'uebungen', 'thema', 'seite', 'test', 'rechne', 'loese')
$$;

-- Grobe Wortstämme für den Fingerabdruck (Rabatte ~ Rabatten, Brüche ~ Bruch)
CREATE OR REPLACE FUNCTION curriculum._stems(p_words text[]) RETURNS text[]
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
    SELECT coalesce(array_agg(DISTINCT s ORDER BY s), '{}')
    FROM (SELECT CASE WHEN length(w) > 5 THEN regexp_replace(w, '(ungen|ung|en|er|es|e|n|s)$', '') ELSE w END AS s
          FROM unnest(p_words) w) x
$$;

-- Übereinstimmung 0..1: Mittel über die Suchwörter, Titel/Suchbegriffe voll, Beschreibung abgeschwächt
CREATE OR REPLACE FUNCTION curriculum._score(p_words text[], p_title text, p_description text, p_terms text[])
RETURNS real
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
    SELECT CASE WHEN cardinality(p_words) = 0 THEN 0::real ELSE (
        SELECT avg(greatest(
                   curriculum._sim(w, curriculum._fold(p_title || ' ' || array_to_string(coalesce(p_terms, '{}'), ' '))),
                   0.8 * curriculum._sim(w, curriculum._fold(p_description))))::real
        FROM unnest(p_words) w) END
$$;

-- --------------------------------------------------------------------------- Tabellen
ALTER TABLE curriculum.concepts ADD COLUMN IF NOT EXISTS search_terms text[] NOT NULL DEFAULT '{}';
ALTER TABLE curriculum.human_queue ADD COLUMN IF NOT EXISTS urgent boolean NOT NULL DEFAULT false;

CREATE TABLE IF NOT EXISTS curriculum.request_policy (
    id               int PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    match_found      real NOT NULL DEFAULT 0.6,   -- ab hier gilt ein Konzept als passend
    match_candidate  real NOT NULL DEFAULT 0.3,   -- ab hier ist es ein Kandidat für die KI-Zuordnung
    grade_window     int  NOT NULL DEFAULT 1,     -- ±Klassen, die noch als "gleiches Niveau" gelten
    daily_limit      int  NOT NULL DEFAULT 50,    -- neue Aufträge pro Einrichtung und Tag
    max_tasks        int  NOT NULL DEFAULT 10,    -- Aufgaben vom Arbeitsblatt pro Anfrage
    reorder_after_days int NOT NULL DEFAULT 14    -- gesperrte/abgelehnte Themen erst danach erneut beauftragen
);
INSERT INTO curriculum.request_policy(id) VALUES (1) ON CONFLICT DO NOTHING;

CREATE TABLE IF NOT EXISTS curriculum.topic_requests (
    id              bigserial PRIMARY KEY,
    tenant          text NOT NULL DEFAULT 'default',
    subject         text NOT NULL,                 -- wie von Karo übergeben (Name oder Kürzel)
    subject_code    text,
    grade           int NOT NULL CHECK (grade BETWEEN 1 AND 13),
    topic           text NOT NULL,
    keywords        text[] NOT NULL DEFAULT '{}',
    tasks           jsonb NOT NULL DEFAULT '[]',   -- anonymisierte Aufgaben; wird nach Abschluss geleert
    fingerprint     text NOT NULL,
    source          text NOT NULL DEFAULT 'karo',  -- karo | prefetch | admin
    priority        int NOT NULL DEFAULT 10,
    status          text NOT NULL DEFAULT 'queued'
                    CHECK (status IN ('queued', 'running', 'ready', 'done', 'blocked', 'rejected', 'failed')),
    stage           text,
    requested_count int NOT NULL DEFAULT 1,
    candidates      jsonb NOT NULL DEFAULT '[]',
    result_concepts text[] NOT NULL DEFAULT '{}',
    reason_code     text,
    message         text,
    human_note      text,                          -- Hinweis einer Fachkraft (review retry) für die Zuordnung
    attempts        int NOT NULL DEFAULT 0,
    next_attempt_at timestamptz NOT NULL DEFAULT now(),
    heartbeat_at    timestamptz,
    run_id          uuid,
    created_at      timestamptz NOT NULL DEFAULT now(),
    started_at      timestamptz,
    finished_at     timestamptz,
    updated_at      timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE curriculum.topic_requests ADD COLUMN IF NOT EXISTS human_note text;
CREATE INDEX IF NOT EXISTS topic_requests_queue_idx ON curriculum.topic_requests(status, priority DESC, created_at);
CREATE INDEX IF NOT EXISTS topic_requests_fp_idx ON curriculum.topic_requests(fingerprint, status);
-- höchstens ein offener Auftrag pro Fingerabdruck (auch bei gleichzeitigen Anfragen)
CREATE UNIQUE INDEX IF NOT EXISTS topic_requests_open_fp_uq ON curriculum.topic_requests(fingerprint)
    WHERE status IN ('queued', 'running', 'ready');
CREATE INDEX IF NOT EXISTS topic_requests_tenant_idx ON curriculum.topic_requests(tenant, created_at);

-- Jede Anfrage (auch erfolgreiche) – Grundlage für das Vorab-Füllen häufiger Lücken. Keine Kinderdaten.
CREATE TABLE IF NOT EXISTS curriculum.topic_demand (
    id           bigserial PRIMARY KEY,
    created_at   timestamptz NOT NULL DEFAULT now(),
    tenant       text NOT NULL,
    subject      text NOT NULL,
    subject_code text,
    grade        int NOT NULL,
    topic        text NOT NULL,
    outcome      text NOT NULL,     -- found | other_level | ordered | joined | known_blocked | limit
    concept_id   text,
    score        real,
    request_id   bigint
);
CREATE INDEX IF NOT EXISTS topic_demand_idx ON curriculum.topic_demand(subject_code, outcome, created_at);

CREATE OR REPLACE VIEW curriculum.demand_report AS
SELECT coalesce(d.subject_code, d.subject) AS subject, d.grade, lower(d.topic) AS topic,
       count(*) AS requests,
       count(*) FILTER (WHERE d.outcome IN ('ordered', 'joined', 'known_blocked', 'limit')) AS misses,
       count(*) FILTER (WHERE d.outcome = 'other_level') AS other_level,
       max(d.created_at) AS last_seen
FROM curriculum.topic_demand d
GROUP BY 1, 2, 3;

CREATE OR REPLACE FUNCTION curriculum._notify_request() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.status = 'queued' THEN
        PERFORM pg_notify('kcteam_requests', NEW.id::text);
    END IF;
    RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS topic_requests_notify ON curriculum.topic_requests;
CREATE TRIGGER topic_requests_notify AFTER INSERT ON curriculum.topic_requests
    FOR EACH ROW EXECUTE FUNCTION curriculum._notify_request();

-- --------------------------------------------------------------------------- Suche (auch für den Dienst)
CREATE OR REPLACE FUNCTION curriculum.match_concepts(p_code text, p_words text[], p_limit int DEFAULT 10,
                                                     p_include_unapproved boolean DEFAULT false)
RETURNS TABLE(concept_id text, title text, target_grade int, status text, score real)
LANGUAGE sql STABLE AS $$
    SELECT c.id, c.title, c.target_grade, c.status,
           curriculum._score(p_words, c.title, c.description, c.search_terms) AS score
    FROM curriculum.concepts c
    WHERE c.subject_code = p_code AND c.status <> 'retired'
      AND (p_include_unapproved OR c.status = 'approved')
    ORDER BY score DESC, c.target_grade, c.id
    LIMIT p_limit
$$;

-- --------------------------------------------------------------------------- Schnittstelle für Karo
CREATE OR REPLACE FUNCTION karo.resolve_topic(p_subject text, p_grade int, p_topic text,
                                              p_keywords text[] DEFAULT '{}', p_tasks jsonb DEFAULT '[]',
                                              p_tenant text DEFAULT 'default')
RETURNS jsonb
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = curriculum, pg_temp
AS $$
DECLARE
    pol     curriculum.request_policy;
    v_code  text;
    v_ten   text := coalesce(nullif(trim(p_tenant), ''), 'default');
    words   text[];
    same    jsonb;
    other   jsonb;
    cands   jsonb;
    fp      text;
    req     record;
    v_id    bigint;
    v_tasks jsonb;
BEGIN
    IF p_grade IS NULL OR p_grade NOT BETWEEN 1 AND 13 THEN RAISE EXCEPTION 'Klasse muss zwischen 1 und 13 liegen'; END IF;
    IF coalesce(trim(p_topic), '') = '' THEN RAISE EXCEPTION 'Thema fehlt'; END IF;
    IF length(p_topic) > 300 THEN RAISE EXCEPTION 'Thema zu lang (max. 300 Zeichen)'; END IF;
    IF coalesce(trim(p_subject), '') = '' THEN RAISE EXCEPTION 'Fach fehlt'; END IF;
    SELECT * INTO pol FROM curriculum.request_policy WHERE id = 1;

    SELECT code INTO v_code FROM curriculum.subjects
     WHERE code = upper(trim(p_subject)) OR lower(name) = lower(trim(p_subject)) LIMIT 1;
    words := curriculum._words(p_topic || ' ' || array_to_string(coalesce(p_keywords, '{}'), ' '));

    IF v_code IS NOT NULL AND cardinality(words) > 0 THEN
        WITH m AS (SELECT * FROM curriculum.match_concepts(v_code, words, 20))
        SELECT jsonb_agg(jsonb_build_object('concept_id', concept_id, 'title', title, 'target_grade', target_grade,
                                            'score', round(score::numeric, 2))
                         ORDER BY score DESC, abs(target_grade - p_grade)) FILTER (WHERE abs(target_grade - p_grade) <= pol.grade_window AND score >= pol.match_found),
               jsonb_agg(jsonb_build_object('concept_id', concept_id, 'title', title, 'target_grade', target_grade,
                                            'score', round(score::numeric, 2),
                                            'level_hint', CASE WHEN target_grade > p_grade THEN 'below' ELSE 'review' END)
                         ORDER BY score DESC, abs(target_grade - p_grade)) FILTER (WHERE abs(target_grade - p_grade) > pol.grade_window AND score >= pol.match_found),
               jsonb_agg(jsonb_build_object('concept_id', concept_id, 'title', title, 'target_grade', target_grade,
                                            'score', round(score::numeric, 2))
                         ORDER BY score DESC, abs(target_grade - p_grade)) FILTER (WHERE score >= pol.match_candidate AND score < pol.match_found)
          INTO same, other, cands
        FROM m;

        IF same IS NOT NULL THEN        -- Stufe 0: passt
            INSERT INTO curriculum.topic_demand(tenant, subject, subject_code, grade, topic, outcome, concept_id, score)
            VALUES (v_ten, p_subject, v_code, p_grade, p_topic, 'found', same->0->>'concept_id', (same->0->>'score')::real);
            RETURN jsonb_build_object('status', 'found', 'concepts', same);
        END IF;
        IF other IS NOT NULL THEN       -- Stufe 1: gibt es, aber in einer anderen Klasse -> Niveau-Stufen nutzen
            INSERT INTO curriculum.topic_demand(tenant, subject, subject_code, grade, topic, outcome, concept_id, score)
            VALUES (v_ten, p_subject, v_code, p_grade, p_topic, 'other_level', other->0->>'concept_id',
                    (other->0->>'score')::real);
            RETURN jsonb_build_object('status', 'other_level', 'concepts', other,
                   'hint', 'Konzept liegt in einer anderen Klasse: level_hint below = mit den Einstiegs-Aufgaben arbeiten, '
                           'review = Wiederholung eines früheren Themas.');
        END IF;
    END IF;

    -- Stufe 2: Auftrag. Gleiche Anfragen zusammenlegen (Fingerabdruck aus Fach, Klasse, Suchwörtern).
    fp := coalesce(v_code, curriculum._fold(p_subject)) || ':' || p_grade || ':'
          || array_to_string(curriculum._stems(words), ' ');
    IF cardinality(words) = 0 THEN
        fp := fp || curriculum._fold(p_topic);
    END IF;

    <<join>>
    LOOP
    SELECT * INTO req FROM curriculum.topic_requests
     WHERE fingerprint = fp AND status IN ('queued', 'running', 'ready')
     ORDER BY id DESC LIMIT 1 FOR UPDATE;
    IF FOUND THEN
        UPDATE curriculum.topic_requests SET requested_count = requested_count + 1,
               priority = greatest(priority, 10), updated_at = now() WHERE id = req.id;
        INSERT INTO curriculum.topic_demand(tenant, subject, subject_code, grade, topic, outcome, request_id)
        VALUES (v_ten, p_subject, v_code, p_grade, p_topic, 'joined', req.id);
        RETURN jsonb_build_object('status', 'ordered', 'request_id', req.id, 'joined', true,
                                  'request_status', req.status, 'candidates', coalesce(cands, '[]'::jsonb));
    END IF;

    SELECT * INTO req FROM curriculum.topic_requests
     WHERE fingerprint = fp AND status IN ('blocked', 'rejected')
       AND finished_at > now() - make_interval(days => pol.reorder_after_days)
     ORDER BY id DESC LIMIT 1;
    IF FOUND THEN                     -- kürzlich gesperrt/abgelehnt: nicht erneut beauftragen
        INSERT INTO curriculum.topic_demand(tenant, subject, subject_code, grade, topic, outcome, request_id)
        VALUES (v_ten, p_subject, v_code, p_grade, p_topic, 'known_blocked', req.id);
        RETURN jsonb_build_object('status', 'unavailable', 'request_id', req.id, 'reason_code', req.reason_code,
                                  'candidates', coalesce(cands, '[]'::jsonb));
    END IF;

    IF (SELECT count(*) FROM curriculum.topic_requests
         WHERE tenant = v_ten AND source = 'karo' AND created_at > now() - interval '1 day') >= pol.daily_limit THEN
        INSERT INTO curriculum.topic_demand(tenant, subject, subject_code, grade, topic, outcome)
        VALUES (v_ten, p_subject, v_code, p_grade, p_topic, 'limit');
        RETURN jsonb_build_object('status', 'limit', 'candidates', coalesce(cands, '[]'::jsonb),
                                  'hint', 'Tageslimit für neue Aufträge erreicht.');
    END IF;

    -- Aufgaben begrenzen: nur Texte, höchstens max_tasks, je 500 Zeichen
    SELECT coalesce(jsonb_agg(left(t, 500)), '[]'::jsonb) INTO v_tasks
    FROM (SELECT value AS t FROM jsonb_array_elements_text(
                 CASE WHEN jsonb_typeof(p_tasks) = 'array' THEN p_tasks ELSE '[]'::jsonb END)
          WHERE trim(value) <> '' LIMIT pol.max_tasks) x;

    INSERT INTO curriculum.topic_requests(tenant, subject, subject_code, grade, topic, keywords, tasks, fingerprint,
                                          candidates)
    VALUES (v_ten, trim(p_subject), v_code, p_grade, trim(p_topic), coalesce(p_keywords, '{}'), v_tasks, fp,
            coalesce(cands, '[]'::jsonb))
    ON CONFLICT (fingerprint) WHERE status IN ('queued', 'running', 'ready') DO NOTHING
    RETURNING id INTO v_id;
    EXIT join WHEN v_id IS NOT NULL;   -- sonst hat eine gleichzeitige Anfrage gewonnen -> ihr anschließen
    END LOOP join;
    INSERT INTO curriculum.topic_demand(tenant, subject, subject_code, grade, topic, outcome, request_id)
    VALUES (v_ten, p_subject, v_code, p_grade, p_topic, 'ordered', v_id);
    RETURN jsonb_build_object('status', 'ordered', 'request_id', v_id, 'joined', false,
                              'candidates', coalesce(cands, '[]'::jsonb),
                              'hint', 'Bis der Auftrag fertig ist, mit den Kandidaten bzw. deren Voraussetzungen arbeiten.');
END $$;

CREATE OR REPLACE FUNCTION karo.request_status(p_request bigint) RETURNS jsonb
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = curriculum, pg_temp
AS $$
    SELECT jsonb_build_object(
        'request_id', r.id, 'status', r.status, 'stage', r.stage, 'reason_code', r.reason_code, 'message', r.message,
        'updated_at', r.updated_at,
        'concepts', coalesce((SELECT jsonb_agg(jsonb_build_object('concept_id', c.id, 'title', c.title,
                                                                  'target_grade', c.target_grade) ORDER BY c.target_grade)
                              FROM curriculum.concepts c
                              WHERE c.id = ANY(r.result_concepts) AND c.status = 'approved'), '[]'::jsonb))
    FROM curriculum.topic_requests r WHERE r.id = p_request
$$;
