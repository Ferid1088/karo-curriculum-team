-- Diagnostik: funktioniert für jedes Kind, jedes Ergebnis und jedes Niveau.
--
-- Ablauf für Karos Backend (Rolle karo_app):
--   1. session := karo.start_diagnosis('kind-123', 'MA', 6, ARRAY['MA.BRUECHE.ADD_UNGL'])
--   2. step    := karo.next_step(session)          -> {"action":"ask", "item": {...}}  oder  {"action":"result", ...}
--   3. step    := karo.record_response(session, step.item.id, '"5/6"')   -> Auswertung + nächster Schritt
--   4. wiederholen bis action = "result": Lernplan (Einstiegspunkte, Fehlvorstellungen, Förderung nach oben)
--
-- Regeln: Beherrscht das Kind ein Konzept, gelten alle seine Voraussetzungen als beherrscht. Scheitert es, werden
-- die direkten Voraussetzungen geprüft – bei Bedarf bis Klasse 1. Ziele werden immer geprüft.
-- Freitext: needs_review, später karo.review_response (Rolle karo_reviewer).
-- Datenschutz: pseudonyme Kennung, keine Namen; karo.forget_learner löscht alles zu einem Kind,
-- karo.purge_learner_data löscht alte Sitzungen, Lernstände und inaktive Kennungen.

CREATE SCHEMA IF NOT EXISTS learner;

-- --------------------------------------------------------------------------- Regeln (anpassbar)
CREATE TABLE IF NOT EXISTS curriculum.diagnostic_policy (id int PRIMARY KEY DEFAULT 1 CHECK (id = 1));
ALTER TABLE curriculum.diagnostic_policy
    ADD COLUMN IF NOT EXISTS min_correct_target          int     NOT NULL DEFAULT 2,
    ADD COLUMN IF NOT EXISTS fail_incorrect_target       int     NOT NULL DEFAULT 2,
    ADD COLUMN IF NOT EXISTS fail_on_below_incorrect     boolean NOT NULL DEFAULT true,
    ADD COLUMN IF NOT EXISTS max_items_per_concept       int     NOT NULL DEFAULT 5,
    ADD COLUMN IF NOT EXISTS max_items_per_session       int     NOT NULL DEFAULT 60,
    ADD COLUMN IF NOT EXISTS skipped_counts_as_incorrect boolean NOT NULL DEFAULT true,
    ADD COLUMN IF NOT EXISTS descend_on_misconception    boolean NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS use_previous_mastery        boolean NOT NULL DEFAULT true,
    ADD COLUMN IF NOT EXISTS retention_days              int     NOT NULL DEFAULT 730;
INSERT INTO curriculum.diagnostic_policy(id) VALUES (1) ON CONFLICT DO NOTHING;

-- --------------------------------------------------------------------------- Lernende (pseudonym)
CREATE TABLE IF NOT EXISTS learner.learners (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    external_ref  text NOT NULL,                  -- Karos Kennung, KEIN Name
    grade         int,
    created_at    timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE learner.learners ADD COLUMN IF NOT EXISTS tenant text NOT NULL DEFAULT 'default';
ALTER TABLE learner.learners DROP CONSTRAINT IF EXISTS learners_external_ref_key;
CREATE UNIQUE INDEX IF NOT EXISTS learners_tenant_ref_idx ON learner.learners(tenant, external_ref);

CREATE TABLE IF NOT EXISTS learner.sessions (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    learner_id       uuid NOT NULL REFERENCES learner.learners(id) ON DELETE CASCADE,
    subject_code     text NOT NULL,
    grade            int,
    target_concepts  text[] NOT NULL,
    known_mastered   text[] NOT NULL DEFAULT '{}',
    status           text NOT NULL DEFAULT 'diagnosing',   -- diagnosing | planned | closed
    current_concept  text,
    result           jsonb,
    started_at       timestamptz NOT NULL DEFAULT now(),
    finished_at      timestamptz
);
ALTER TABLE learner.sessions ADD COLUMN IF NOT EXISTS current_item text;
ALTER TABLE learner.sessions ADD COLUMN IF NOT EXISTS needs_followup boolean NOT NULL DEFAULT false;
CREATE INDEX IF NOT EXISTS sessions_learner_idx ON learner.sessions(learner_id);

CREATE TABLE IF NOT EXISTS learner.responses (
    id               bigserial PRIMARY KEY,
    session_id       uuid NOT NULL REFERENCES learner.sessions(id) ON DELETE CASCADE,
    learner_id       uuid NOT NULL REFERENCES learner.learners(id) ON DELETE CASCADE,
    item_id          text NOT NULL,
    concept_id       text NOT NULL,
    item_level       text NOT NULL,
    answer           jsonb,
    outcome          text NOT NULL,
    misconception_id text,
    score            numeric,
    reviewed         boolean NOT NULL DEFAULT false,
    created_at       timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE learner.responses DROP CONSTRAINT IF EXISTS responses_outcome_check;
ALTER TABLE learner.responses ADD CONSTRAINT responses_outcome_check
    CHECK (outcome IN ('correct','incorrect','misconception','partial','skipped','needs_review'));
CREATE INDEX IF NOT EXISTS responses_session_idx ON learner.responses(session_id, concept_id);
CREATE UNIQUE INDEX IF NOT EXISTS responses_session_item_idx ON learner.responses(session_id, item_id);
CREATE INDEX IF NOT EXISTS responses_learner_concept_idx ON learner.responses(learner_id, concept_id);

CREATE TABLE IF NOT EXISTS learner.mastery (
    learner_id      uuid NOT NULL REFERENCES learner.learners(id) ON DELETE CASCADE,
    concept_id      text NOT NULL,
    state           text NOT NULL,
    evidence        int  NOT NULL DEFAULT 0,
    misconceptions  text[] NOT NULL DEFAULT '{}',
    updated_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (learner_id, concept_id)
);
ALTER TABLE learner.mastery DROP CONSTRAINT IF EXISTS mastery_state_check;
ALTER TABLE learner.mastery ADD CONSTRAINT mastery_state_check
    CHECK (state IN ('mastered','partial','not_mastered','misconception','untested','pending'));

-- Alte Funktionssignaturen entfernen (sonst entstünden Überladungen)
DROP FUNCTION IF EXISTS karo.start_diagnosis(text, text, int, text[], text[]);
DROP FUNCTION IF EXISTS karo.record_response(uuid, text, jsonb, text, numeric);
DROP FUNCTION IF EXISTS karo._concept_state(uuid, text);
DROP FUNCTION IF EXISTS karo._session_mastered(uuid);
DROP FUNCTION IF EXISTS karo._eligible_items(text);
DROP FUNCTION IF EXISTS karo._diagnosis_result(uuid);
DROP FUNCTION IF EXISTS karo._result(uuid);
DROP FUNCTION IF EXISTS karo.purge_learner_data(integer);

-- --------------------------------------------------------------------------- Zahlen und Texte normalisieren
-- Versteht: 12 · -3 · +5 · - 3 · 0,75 · .5 · 12. · 1.000 / 10.000 / 1 000 (Tausender) · 3/4 · -5/-6 · 1 1/2 · -0 5/6
-- · "12 cm" (eine Einheit aus Buchstaben). Nicht: "12 oder 7", "12-5", "12:3", "1.5e3".
CREATE OR REPLACE FUNCTION karo._num(p text) RETURNS numeric
LANGUAGE plpgsql IMMUTABLE AS $$
DECLARE s text; m text[]; neg boolean;
BEGIN
    IF p IS NULL THEN RETURN NULL; END IF;
    s := trim(regexp_replace(replace(replace(p, '−', '-'), chr(160), ' '), '\s+', ' ', 'g'));
    s := regexp_replace(s, '^\+\s*', '');
    s := regexp_replace(s, '^-\s+', '-');
    -- Einheit am Ende (nur Buchstaben/Symbole, keine Ziffern oder Rechenzeichen)
    m := regexp_match(s, '^(.*\d)\s*[A-Za-zÄÖÜäöüß%°€$²³µ]+\.?$');
    IF m IS NOT NULL THEN s := m[1]; END IF;
    -- Tausendertrenner
    IF s ~ '^-?\d{1,3}( \d{3})+(,\d+)?$' THEN s := replace(s, ' ', ''); END IF;
    IF s ~ '^-?\d{1,3}(\.\d{3})+(,\d+)?$' THEN s := replace(s, '.', ''); END IF;
    s := replace(s, ',', '.');
    -- gemischte Zahl
    m := regexp_match(s, '^(-?)(\d+) (\d+)\s*/\s*(\d+)$');
    IF m IS NOT NULL THEN
        IF m[4]::numeric = 0 THEN RETURN NULL; END IF;
        RETURN (CASE WHEN m[1] = '-' THEN -1 ELSE 1 END) * (m[2]::numeric + m[3]::numeric / m[4]::numeric);
    END IF;
    -- Bruch
    m := regexp_match(s, '^(-?\d+)\s*/\s*(-?\d+)$');
    IF m IS NOT NULL THEN
        IF m[2]::numeric = 0 THEN RETURN NULL; END IF;
        RETURN m[1]::numeric / m[2]::numeric;
    END IF;
    -- Dezimalzahl
    IF s ~ '^-?(\d+(\.\d*)?|\.\d+)$' THEN
        neg := s LIKE '-%';
        s := ltrim(s, '-');
        IF s LIKE '.%' THEN s := '0' || s; END IF;
        s := rtrim(s, '.');
        RETURN (CASE WHEN neg THEN -1 ELSE 1 END) * s::numeric;
    END IF;
    RETURN NULL;
END $$;

-- Text vergleichen: Unicode-normalisiert, Anführungszeichen/Klammern/Satzzeichen weg – aber Komma und Punkt
-- zwischen Ziffern bleiben (3,5 ≠ 35). Apostrophe (' ’) werden entfernt.
CREATE OR REPLACE FUNCTION karo._norm_text(p text, case_sensitive boolean DEFAULT false,
                                           ignore_punct boolean DEFAULT true) RETURNS text
LANGUAGE plpgsql IMMUTABLE AS $$
DECLARE s text;
BEGIN
    IF p IS NULL THEN RETURN NULL; END IF;
    s := normalize(p, NFC);
    s := translate(s, '‐‑–—', '----');
    IF NOT case_sensitive THEN s := lower(s); END IF;
    IF ignore_punct THEN
        s := regexp_replace(s, '[;:!?¡¿"„“”‚‘’''«»()\[\]]', '', 'g');
        s := regexp_replace(s, '(?<!\d)[.,]|[.,](?!\d)', '', 'g');
    END IF;
    RETURN trim(regexp_replace(s, '\s+', ' ', 'g'));
END $$;

-- Alle Voraussetzungen (transitiv) inklusive der Konzepte selbst
CREATE OR REPLACE FUNCTION karo._implied(p_concepts text[]) RETURNS text[]
LANGUAGE sql STABLE AS $$
    WITH RECURSIVE r(id) AS (
        SELECT unnest(coalesce(array_remove(p_concepts, NULL), '{}'))
        UNION
        SELECT cp.prerequisite_id FROM r JOIN curriculum.concept_prerequisites cp ON cp.concept_id = r.id
    )
    SELECT coalesce(array_agg(id), '{}') FROM r;
$$;

-- --------------------------------------------------------------------------- Antwort prüfen (intern)
CREATE OR REPLACE FUNCTION karo.check_answer(p_item text, p_answer jsonb)
RETURNS TABLE (outcome text, misconception_id text, score numeric, feedback text)
LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path = curriculum, pg_temp
AS $$
DECLARE
    it   record;
    a    jsonb;
    t    text;
    g    jsonb := p_answer;
    gt   text;
    ok   boolean := false;
    mk   text;
    fb   text;
    d    jsonb;
    sel  int[];
    corr int[];
    gnum numeric;
    cval numeric;
    tol  numeric;
    m    text[];
    gv   text[];
    empty boolean;
BEGIN
    SELECT i.* INTO it FROM curriculum.items i
      JOIN curriculum.concepts c ON c.id = i.concept_id AND c.status = 'approved'
     WHERE i.id = p_item;
    IF NOT FOUND THEN RAISE EXCEPTION 'Aufgabe % nicht gefunden oder nicht freigegeben', p_item; END IF;
    a := it.answer;

    -- Leere Antwort = übersprungen (vor allem anderen, auch bei Freitext)
    IF jsonb_typeof(g) = 'object' AND g ? 'value' THEN g := g->'value'; END IF;
    IF jsonb_typeof(g) = 'object' AND g ? 'text' THEN g := g->'text'; END IF;
    empty := g IS NULL OR jsonb_typeof(g) = 'null'
             OR (jsonb_typeof(g) = 'string' AND trim(g #>> '{}') IN ('', '…', '...'))
             OR (jsonb_typeof(g) = 'array' AND jsonb_array_length(g) = 0);
    IF empty THEN
        RETURN QUERY SELECT 'skipped'::text, NULL::text, 0::numeric, NULL::text; RETURN;
    END IF;
    IF a IS NULL OR a->>'type' = 'free_text' THEN
        RETURN QUERY SELECT 'needs_review'::text, NULL::text, NULL::numeric, NULL::text; RETURN;
    END IF;

    t  := a->>'type';
    gt := CASE WHEN jsonb_typeof(g) IN ('string', 'number') THEN g #>> '{}' ELSE g::text END;
    gnum := karo._num(gt);
    tol  := coalesce(karo._num(a->>'tolerance'), 0);

    IF t = 'number' THEN
        cval := karo._num(a->>'value');
        ok := gnum IS NOT NULL AND cval IS NOT NULL AND abs(gnum - cval) <= tol;
    ELSIF t = 'fraction' THEN
        cval := karo._num(a->>'value');
        ok := gnum IS NOT NULL AND cval IS NOT NULL AND abs(gnum - cval) < 1e-9;
        IF ok AND NOT coalesce((a->>'accept_decimal')::boolean, false) AND gt ~ '\d[.,]\d' THEN ok := false; END IF;
        IF ok AND NOT coalesce((a->>'accept_equivalent')::boolean, true) THEN
            ok := regexp_replace(gt, '\s', '', 'g') = regexp_replace(a->>'value', '\s', '', 'g');
        END IF;
        IF ok AND coalesce((a->>'require_reduced')::boolean, false) THEN
            m := regexp_match(regexp_replace(gt, '\s', '', 'g'), '(-?\d+)/(-?\d+)$');
            ok := m IS NULL OR gcd(abs(m[1]::bigint), abs(m[2]::bigint)) = 1;
        END IF;
    ELSIF t = 'mark' THEN
        cval := karo._num(a->>'value');
        IF cval IS NOT NULL THEN
            ok := gnum IS NOT NULL AND abs(gnum - cval) <= tol + 1e-9;
        ELSE
            ok := lower(trim(gt)) = lower(trim(a->>'value'));
        END IF;
    ELSIF t = 'text' THEN
        ok := EXISTS (SELECT 1 FROM jsonb_array_elements_text(a->'accepted') x
                      WHERE karo._norm_text(x, coalesce((a->>'case_sensitive')::boolean, false),
                                            coalesce((a->>'ignore_punctuation')::boolean, true))
                          = karo._norm_text(gt, coalesce((a->>'case_sensitive')::boolean, false),
                                            coalesce((a->>'ignore_punctuation')::boolean, true)));
    ELSIF t = 'choice' THEN
        -- Eindeutig: {"index": n} bzw. {"indices": [...]} – sonst zuerst Optionstext, dann Zahl als Index
        IF jsonb_typeof(p_answer) = 'object' AND p_answer ? 'indices' THEN
            sel := ARRAY(SELECT v::int FROM jsonb_array_elements_text(p_answer->'indices') v WHERE v ~ '^\d+$');
        ELSIF jsonb_typeof(p_answer) = 'object' AND p_answer ? 'index' AND (p_answer->>'index') ~ '^\d+$' THEN
            sel := ARRAY[(p_answer->>'index')::int];
        ELSE
            gv := CASE WHEN jsonb_typeof(g) = 'array' THEN ARRAY(SELECT jsonb_array_elements_text(g)) ELSE ARRAY[gt] END;
            sel := ARRAY(SELECT DISTINCT (ord - 1)::int
                         FROM jsonb_array_elements(a->'options') WITH ORDINALITY e(o, ord)
                         WHERE karo._norm_text(o->>'text') = ANY (ARRAY(SELECT karo._norm_text(v) FROM unnest(gv) v)));
            IF cardinality(sel) = 0 THEN
                sel := ARRAY(SELECT v::int FROM unnest(gv) v WHERE v ~ '^\d{1,3}$');
            END IF;
        END IF;
        corr := ARRAY(SELECT (ord - 1)::int FROM jsonb_array_elements(a->'options') WITH ORDINALITY e(o, ord)
                      WHERE coalesce((o->>'correct')::boolean, false));
        ok := cardinality(sel) > 0 AND sel <@ corr AND corr <@ sel;
        IF NOT ok THEN
            SELECT o->>'misconception' INTO mk
            FROM jsonb_array_elements(a->'options') WITH ORDINALITY e(o, ord)
            WHERE (ord - 1)::int = ANY (sel) AND o->>'misconception' IS NOT NULL
            ORDER BY ord LIMIT 1;
        END IF;
    ELSIF t = 'order' THEN
        ok := jsonb_typeof(g) = 'array' AND jsonb_array_length(g) = jsonb_array_length(a->'items')
              AND NOT EXISTS (SELECT 1 FROM jsonb_array_elements_text(g) WITH ORDINALITY x(v, i)
                              JOIN jsonb_array_elements_text(a->'items') WITH ORDINALITY y(v, i) USING (i)
                              WHERE karo._norm_text(x.v) <> karo._norm_text(y.v));
    ELSIF t = 'match' THEN
        ok := jsonb_typeof(g) = 'array' AND jsonb_array_length(g) = jsonb_array_length(a->'pairs')
              AND NOT EXISTS (SELECT 1 FROM jsonb_array_elements(a->'pairs') p
                              WHERE NOT EXISTS (SELECT 1 FROM jsonb_array_elements(g) q
                                                WHERE jsonb_typeof(q) = 'array'
                                                  AND karo._norm_text(q->>0) = karo._norm_text(p->>0)
                                                  AND karo._norm_text(q->>1) = karo._norm_text(p->>1)));
    END IF;

    IF ok THEN
        RETURN QUERY SELECT 'correct'::text, NULL::text, 1::numeric, NULL::text; RETURN;
    END IF;
    IF mk IS NULL THEN
        FOR d IN SELECT * FROM jsonb_array_elements(it.distractors) LOOP
            IF (gnum IS NOT NULL AND karo._num(d->>'answer') IS NOT NULL AND abs(gnum - karo._num(d->>'answer')) < 1e-9)
               OR karo._norm_text(d->>'answer') = karo._norm_text(gt) THEN
                mk := d->>'misconception'; fb := d->>'feedback';
                EXIT;
            END IF;
        END LOOP;
    END IF;
    IF mk IS NOT NULL THEN
        RETURN QUERY SELECT 'misconception'::text,
                            CASE WHEN mk LIKE '%.%' THEN upper(mk) ELSE it.concept_id || '.' || upper(mk) END,
                            0::numeric, fb;
        RETURN;
    END IF;
    RETURN QUERY SELECT 'incorrect'::text, NULL::text, 0::numeric, fb;
END $$;

-- --------------------------------------------------------------------------- Aufgabe fürs Kind (ohne Lösung)
CREATE OR REPLACE FUNCTION karo._shuffle(p_items text[], p_seed text) RETURNS text[]
LANGUAGE sql IMMUTABLE AS $$
    WITH s AS (SELECT array_agg(v ORDER BY md5(p_seed || v || i::text)) AS a
               FROM unnest(p_items) WITH ORDINALITY u(v, i))
    SELECT CASE WHEN cardinality(p_items) > 1 AND s.a = p_items
                THEN s.a[2:cardinality(s.a)] || s.a[1]      -- nie in Lösungsreihenfolge zeigen
                ELSE s.a END
    FROM s;
$$;

CREATE OR REPLACE FUNCTION karo.item_for_child(p_item text) RETURNS jsonb
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = curriculum, pg_temp
AS $$
    SELECT jsonb_build_object(
        'id', i.id, 'concept_id', i.concept_id, 'kind', i.kind, 'level', i.level, 'grade', i.grade,
        'prompt', i.prompt, 'interaction', coalesce(i.interaction, 'input'), 'visual_svg', i.visual_svg,
        'answer_type', i.answer->>'type', 'auto_checkable', i.auto_checkable,
        'unit', i.answer->>'unit', 'max_words', i.answer->'max_words',
        'multiple', coalesce((i.answer->>'multiple')::boolean, false),
        'answer_format', CASE i.answer->>'type'
            WHEN 'choice' THEN '{"index": n} bzw. {"indices": [n, ...]}'
            WHEN 'order' THEN '["erster", "zweiter", ...]'
            WHEN 'match' THEN '[["links", "rechts"], ...]'
            WHEN 'free_text' THEN '{"text": "..."}'
            ELSE '"Antwort als Text"' END,
        'choices', CASE WHEN i.answer->>'type' = 'choice' THEN
                     (SELECT jsonb_agg(jsonb_build_object('index', ord - 1, 'text', o->>'text') ORDER BY ord)
                      FROM jsonb_array_elements(i.answer->'options') WITH ORDINALITY e(o, ord)) END,
        'order_items', CASE WHEN i.answer->>'type' = 'order' THEN
                     to_jsonb(karo._shuffle(ARRAY(SELECT jsonb_array_elements_text(i.answer->'items')), i.id)) END,
        'match_left', CASE WHEN i.answer->>'type' = 'match' THEN
                     (SELECT jsonb_agg(p->>0) FROM jsonb_array_elements(i.answer->'pairs') p) END,
        'match_right', CASE WHEN i.answer->>'type' = 'match' THEN
                     to_jsonb(karo._shuffle(ARRAY(SELECT p->>1 FROM jsonb_array_elements(i.answer->'pairs') p), i.id)) END)
    FROM curriculum.items i
    JOIN curriculum.concepts c ON c.id = i.concept_id AND c.status = 'approved'
    WHERE i.id = p_item;
$$;

-- --------------------------------------------------------------------------- Zustände (in einer Abfrage)
-- Diagnoseaufgaben eines Konzepts: automatisch auswertbare zuerst, dann Ziel vor Fehlvorstellung vor Einstieg.
CREATE OR REPLACE FUNCTION karo._eligible(p_concepts text[]) RETURNS TABLE (concept_id text, item_id text, rank int)
LANGUAGE sql STABLE AS $$
    SELECT i.concept_id, i.id,
           (CASE WHEN i.auto_checkable THEN 0 ELSE 10000 END)
         + (CASE WHEN i.kind = 'diagnostic' AND i.level = 'target' THEN 0
                 WHEN i.kind = 'misconception' THEN 1000 ELSE 2000 END)
         + i.sort_order
    FROM curriculum.items i
    JOIN curriculum.concepts c ON c.id = i.concept_id AND c.status = 'approved'
    WHERE i.concept_id = ANY (p_concepts) AND i.kind IN ('diagnostic', 'misconception')
      AND i.level IN ('below', 'target');
$$;

-- Zustand je Konzept: mastered | not_mastered | misconception | partial | pending | untested | unknown
CREATE OR REPLACE FUNCTION karo._state_map(p_session uuid, p_concepts text[]) RETURNS jsonb
LANGUAGE sql STABLE AS $$
    WITH pol AS (SELECT * FROM curriculum.diagnostic_policy WHERE id = 1),
    agg AS (
        SELECT r.concept_id,
               count(*) FILTER (WHERE r.outcome = 'correct' AND r.item_level = 'target') AS ct,
               count(*) FILTER (WHERE r.item_level = 'target' AND (r.outcome = 'incorrect'
                                OR (r.outcome = 'skipped' AND pol.skipped_counts_as_incorrect))) AS it,
               count(*) FILTER (WHERE r.item_level = 'below' AND (r.outcome IN ('incorrect', 'misconception')
                                OR (r.outcome = 'skipped' AND pol.skipped_counts_as_incorrect))) AS ib,
               count(*) FILTER (WHERE r.outcome = 'misconception' AND r.item_level = 'target') AS mis,
               count(*) FILTER (WHERE r.outcome <> 'needs_review') AS n,
               count(*) FILTER (WHERE r.outcome = 'needs_review') AS pend
        FROM learner.responses r, pol
        WHERE r.session_id = p_session AND r.concept_id = ANY (p_concepts)
        GROUP BY r.concept_id),
    rem AS (
        SELECT e.concept_id, count(*) AS total,
               count(*) FILTER (WHERE NOT EXISTS (SELECT 1 FROM learner.responses x
                                                  WHERE x.session_id = p_session AND x.item_id = e.item_id)) AS remaining
        FROM karo._eligible(p_concepts) e GROUP BY e.concept_id)
    SELECT coalesce(jsonb_object_agg(c.id, CASE
               WHEN coalesce(a.mis, 0) > 0 AND coalesce(a.ct, 0) < pol.min_correct_target + 1 THEN 'misconception'
               WHEN coalesce(a.ct, 0) >= pol.min_correct_target AND a.ct > coalesce(a.it, 0) THEN 'mastered'
               WHEN coalesce(a.it, 0) >= pol.fail_incorrect_target
                    OR (pol.fail_on_below_incorrect AND coalesce(a.ib, 0) >= 1) THEN 'not_mastered'
               WHEN coalesce(rm.total, 0) = 0 THEN 'untested'
               WHEN coalesce(rm.remaining, 0) = 0 AND coalesce(a.pend, 0) > 0 THEN 'pending'
               WHEN coalesce(rm.remaining, 0) = 0 OR coalesce(a.n, 0) >= pol.max_items_per_concept THEN 'partial'
               ELSE 'unknown' END), '{}'::jsonb)
    FROM (SELECT DISTINCT unnest(p_concepts) AS id) c
    CROSS JOIN pol
    LEFT JOIN agg a ON a.concept_id = c.id
    LEFT JOIN rem rm ON rm.concept_id = c.id;
$$;

-- Beherrschte Konzepte (inkl. Vorwissen) und alles, was daraus folgt
CREATE OR REPLACE FUNCTION karo._known(p_session uuid) RETURNS text[]
LANGUAGE sql STABLE AS $$
    WITH s AS (SELECT * FROM learner.sessions WHERE id = p_session),
    answered AS (SELECT array_agg(DISTINCT concept_id) AS ids FROM learner.responses WHERE session_id = p_session),
    st AS (SELECT karo._state_map(p_session, coalesce(answered.ids, '{}')) AS m FROM answered)
    SELECT karo._implied(s.known_mastered || coalesce(
               (SELECT array_agg(k) FROM st, jsonb_each_text(st.m) e(k, v) WHERE v = 'mastered'), '{}'))
    FROM s;
$$;

-- --------------------------------------------------------------------------- Ergebnis / Lernplan
-- Pfad, Vorwissen und Zustände einmal berechnen und weiterreichen (statt mehrfach durch den Graphen zu laufen)
CREATE OR REPLACE FUNCTION karo._ctx(p_session uuid) RETURNS jsonb
LANGUAGE plpgsql STABLE AS $$
DECLARE s learner.sessions; known text[]; lp jsonb; ids text[];
BEGIN
    SELECT * INTO s FROM learner.sessions WHERE id = p_session;
    known := karo._known(p_session);
    SELECT coalesce(jsonb_agg(to_jsonb(l)), '[]'::jsonb), coalesce(array_agg(l.concept_id), '{}')
      INTO lp, ids FROM karo.learning_path(s.target_concepts, known) l;
    RETURN jsonb_build_object('known', to_jsonb(known), 'path', lp, 'ids', to_jsonb(ids),
                              'states', karo._state_map(p_session, ids));
END $$;

-- Nächste Diagnoseaufgabe bestimmen (oder NULL, wenn die Diagnose fertig ist)
CREATE OR REPLACE FUNCTION karo._next_item(p_session uuid, p_ctx jsonb) RETURNS jsonb
LANGUAGE plpgsql STABLE AS $$
DECLARE
    s learner.sessions; pol curriculum.diagnostic_policy; ids text[]; states jsonb; r record; st text; nxt text;
BEGIN
    SELECT * INTO s FROM learner.sessions WHERE id = p_session;
    SELECT * INTO pol FROM curriculum.diagnostic_policy WHERE id = 1;
    IF (SELECT count(*) FROM learner.responses WHERE session_id = p_session) >= pol.max_items_per_session THEN
        RETURN NULL;
    END IF;
    ids := ARRAY(SELECT jsonb_array_elements_text(p_ctx->'ids'));
    states := p_ctx->'states';
    FOR r IN SELECT * FROM jsonb_to_recordset(p_ctx->'path')
                    AS l(concept_id text, title text, target_grade int, depth int, available boolean)
             ORDER BY (l.concept_id = ANY (s.target_concepts)) DESC, l.depth, l.target_grade DESC, l.concept_id LOOP
        CONTINUE WHEN NOT r.available;
        st := coalesce(states->>r.concept_id, 'unknown');
        CONTINUE WHEN st <> 'unknown';
        -- Voraussetzungen nur prüfen, wenn ein darauf aufbauendes (freigegebenes) Konzept gescheitert ist
        IF NOT (r.concept_id = ANY (s.target_concepts)) AND NOT EXISTS (
            SELECT 1 FROM curriculum.concept_prerequisites cp
            JOIN curriculum.concepts pc ON pc.id = cp.concept_id AND pc.status = 'approved'
            WHERE cp.prerequisite_id = r.concept_id AND cp.concept_id = ANY (ids)
              AND (coalesce(states->>cp.concept_id, 'unknown') IN ('not_mastered', 'partial')
                   OR (pol.descend_on_misconception AND states->>cp.concept_id = 'misconception')))
        THEN
            CONTINUE;
        END IF;
        SELECT e.item_id INTO nxt FROM karo._eligible(ARRAY[r.concept_id]) e
         WHERE NOT EXISTS (SELECT 1 FROM learner.responses x WHERE x.session_id = p_session AND x.item_id = e.item_id)
         ORDER BY e.rank LIMIT 1;
        IF nxt IS NOT NULL THEN
            RETURN jsonb_build_object('concept_id', r.concept_id, 'item_id', nxt, 'title', r.title, 'depth', r.depth);
        END IF;
    END LOOP;
    RETURN NULL;
END $$;


CREATE OR REPLACE FUNCTION karo._result(p_session uuid, p_ctx jsonb DEFAULT NULL) RETURNS jsonb
LANGUAGE plpgsql STABLE AS $$
DECLARE
    s learner.sessions;
    ctx jsonb;
    known text[];
    states jsonb;
    plan jsonb;
    session_mastered jsonb;
    not_assessed jsonb;
    pending jsonb;
    enrichment jsonb := '[]'::jsonb;
    reached boolean;
BEGIN
    SELECT * INTO s FROM learner.sessions WHERE id = p_session;
    ctx := coalesce(p_ctx, karo._ctx(p_session));
    known := ARRAY(SELECT jsonb_array_elements_text(ctx->'known'));
    states := ctx->'states';
    reached := cardinality(s.target_concepts) > 0 AND s.target_concepts <@ known;

    WITH lp AS (
        SELECT l.*, coalesce(states->>l.concept_id, 'unknown') AS state
        FROM jsonb_to_recordset(ctx->'path') AS l(concept_id text, title text, target_grade int, depth int,
                                                  available boolean)
    ), inplan AS (
        SELECT * FROM lp WHERE NOT available OR state IN ('not_mastered', 'partial', 'misconception', 'untested', 'pending')
    )
    SELECT coalesce(jsonb_agg(jsonb_build_object(
               'concept_id', p.concept_id, 'title', p.title, 'target_grade', p.target_grade,
               'state', CASE WHEN p.available THEN p.state ELSE 'unavailable' END, 'available', p.available,
               'is_target', p.concept_id = ANY (s.target_concepts),
               'entry_point', NOT EXISTS (SELECT 1 FROM curriculum.concept_prerequisites cp
                                          JOIN inplan q ON q.concept_id = cp.prerequisite_id
                                          WHERE cp.concept_id = p.concept_id),
               'below_curriculum_floor', p.state = 'not_mastered' AND NOT EXISTS (
                                          SELECT 1 FROM curriculum.concept_prerequisites cp WHERE cp.concept_id = p.concept_id),
               'misconceptions', coalesce((SELECT jsonb_agg(DISTINCT r.misconception_id) FROM learner.responses r
                                           WHERE r.session_id = p_session AND r.concept_id = p.concept_id
                                             AND r.misconception_id IS NOT NULL), '[]'::jsonb),
               'explanations', coalesce((SELECT jsonb_agg(v.id ORDER BY (v.misconception_id IS NULL), v.sort_order)
                                         FROM curriculum.visual_explanations v
                                         WHERE v.concept_id = p.concept_id AND p.available
                                           AND (v.misconception_id IS NULL OR v.misconception_id IN (
                                                SELECT r.misconception_id FROM learner.responses r
                                                WHERE r.session_id = p_session AND r.concept_id = p.concept_id))),
                                        '[]'::jsonb)
           ) ORDER BY p.depth DESC, p.target_grade, p.concept_id), '[]'::jsonb)
      INTO plan FROM inplan p;

    SELECT coalesce(jsonb_agg(k ORDER BY k), '[]'::jsonb) INTO session_mastered
      FROM jsonb_each_text(karo._state_map(p_session, coalesce(
               (SELECT array_agg(DISTINCT concept_id) FROM learner.responses WHERE session_id = p_session), '{}'))) e(k, v)
     WHERE v = 'mastered';

    SELECT coalesce(jsonb_agg(l.concept_id), '[]'::jsonb) INTO not_assessed
      FROM jsonb_to_recordset(ctx->'path') AS l(concept_id text, available boolean)
     WHERE l.available AND coalesce(states->>l.concept_id, 'unknown') = 'unknown';

    SELECT coalesce(jsonb_agg(id), '[]'::jsonb) INTO pending
      FROM learner.responses WHERE session_id = p_session AND outcome = 'needs_review';

    IF reached THEN   -- Ziel erreicht: Förderung nach oben
        SELECT coalesce(jsonb_agg(DISTINCT jsonb_build_object('concept_id', c.id, 'title', c.title,
                                                              'target_grade', c.target_grade)), '[]'::jsonb)
          INTO enrichment
          FROM curriculum.concept_prerequisites cp
          JOIN curriculum.concepts c ON c.id = cp.concept_id AND c.status = 'approved'
         WHERE cp.prerequisite_id = ANY (s.target_concepts) AND NOT (c.id = ANY (known));
    END IF;

    RETURN jsonb_build_object(
        'session_id', p_session,
        'targets_mastered', reached,
        'mastered', session_mastered,
        'plan', plan,
        'entry_points', coalesce((SELECT jsonb_agg(e->'concept_id') FROM jsonb_array_elements(plan) e
                                  WHERE (e->>'entry_point')::boolean), '[]'::jsonb),
        'not_assessed', not_assessed,
        'enrichment', enrichment,
        'challenge_items', CASE WHEN reached THEN coalesce((
              SELECT jsonb_agg(i.id ORDER BY i.id) FROM curriculum.items i
              WHERE i.concept_id = ANY (s.target_concepts) AND i.kind = 'boundary' AND i.level = 'above'), '[]'::jsonb)
            ELSE '[]'::jsonb END,
        'items_answered', (SELECT count(*) FROM learner.responses WHERE session_id = p_session),
        'needs_review', pending,
        'needs_followup', s.needs_followup
    );
END $$;

-- Dauerhafte Lernstände schreiben (idempotent: Evidenz wird aus allen Antworten neu gezählt)
CREATE OR REPLACE FUNCTION karo._write_mastery(p_session uuid) RETURNS void
LANGUAGE sql VOLATILE AS $$
    WITH s AS (SELECT * FROM learner.sessions WHERE id = p_session),
    ids AS (SELECT array_agg(DISTINCT concept_id) AS a FROM learner.responses WHERE session_id = p_session),
    st AS (SELECT e.k AS concept_id, e.v AS state
           FROM ids, jsonb_each_text(karo._state_map(p_session, coalesce(ids.a, '{}'))) e(k, v))
    INSERT INTO learner.mastery(learner_id, concept_id, state, evidence, misconceptions, updated_at)
    SELECT s.learner_id, st.concept_id, st.state,
           (SELECT count(*) FROM learner.responses r WHERE r.learner_id = s.learner_id AND r.concept_id = st.concept_id
              AND r.outcome <> 'needs_review')::int,
           coalesce((SELECT array_agg(DISTINCT r.misconception_id) FROM learner.responses r
                     WHERE r.session_id = p_session AND r.concept_id = st.concept_id
                       AND r.misconception_id IS NOT NULL), '{}'),
           now()
    FROM s, st
    WHERE st.state <> 'unknown'
    ON CONFLICT (learner_id, concept_id) DO UPDATE
       SET state = EXCLUDED.state, evidence = EXCLUDED.evidence, misconceptions = EXCLUDED.misconceptions,
           updated_at = now();
$$;

-- --------------------------------------------------------------------------- Nächster Schritt
CREATE OR REPLACE FUNCTION karo.next_step(p_session uuid) RETURNS jsonb
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = curriculum, learner, pg_temp
AS $$
DECLARE
    s learner.sessions;
    ctx jsonb;
    nx jsonb;
    item jsonb;
    v_result jsonb;
BEGIN
    SELECT * INTO s FROM learner.sessions WHERE id = p_session FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION 'Sitzung % nicht gefunden', p_session; END IF;

    -- Nach einer Nachbewertung weiterprüfen, wenn das Kind wieder da ist
    IF s.status = 'planned' AND s.needs_followup THEN
        UPDATE learner.sessions SET status = 'diagnosing', needs_followup = false, finished_at = NULL
         WHERE id = p_session RETURNING * INTO s;
    END IF;

    IF s.status = 'diagnosing' AND s.current_item IS NOT NULL THEN
        -- Bereits gestellte Aufgabe erneut ausliefern (z. B. nach Neuladen) – außer sie ist nicht mehr freigegeben
        item := karo.item_for_child(s.current_item);
        IF item IS NOT NULL THEN
            RETURN jsonb_build_object('action', 'ask', 'session_id', p_session, 'concept_id', s.current_concept,
                                      'item', item);
        END IF;
        UPDATE learner.sessions SET current_item = NULL, current_concept = NULL WHERE id = p_session;
    END IF;

    IF s.status = 'diagnosing' THEN
        ctx := karo._ctx(p_session);
        nx := karo._next_item(p_session, ctx);
        IF nx IS NOT NULL THEN
            UPDATE learner.sessions SET current_concept = nx->>'concept_id', current_item = nx->>'item_id'
             WHERE id = p_session;
            RETURN jsonb_build_object('action', 'ask', 'session_id', p_session, 'concept_id', nx->>'concept_id',
                                      'concept_title', nx->>'title', 'depth', (nx->>'depth')::int,
                                      'item', karo.item_for_child(nx->>'item_id'));
        END IF;
    END IF;

    -- Diagnose beendet (einmalig speichern, danach nur noch ausliefern)
    IF s.status = 'diagnosing' OR s.result IS NULL THEN
        v_result := karo._result(p_session, ctx);
        UPDATE learner.sessions SET status = 'planned', result = v_result, finished_at = coalesce(finished_at, now()),
                                    current_concept = NULL, current_item = NULL
         WHERE id = p_session;
        PERFORM karo._write_mastery(p_session);
    ELSE
        v_result := s.result;
    END IF;
    RETURN jsonb_build_object('action', 'result') || v_result;
END $$;

-- --------------------------------------------------------------------------- Sitzung starten
CREATE OR REPLACE FUNCTION karo.start_diagnosis(p_learner_ref text, p_subject_code text, p_grade int,
                                                p_targets text[], p_known_mastered text[] DEFAULT '{}',
                                                p_tenant text DEFAULT 'default')
RETURNS uuid
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = curriculum, learner, pg_temp
AS $$
DECLARE
    lid uuid; sid uuid; known text[]; bad text[]; targets text[];
    pol curriculum.diagnostic_policy;
BEGIN
    SELECT * INTO pol FROM curriculum.diagnostic_policy WHERE id = 1;
    targets := ARRAY(SELECT DISTINCT upper(t) FROM unnest(coalesce(p_targets, '{}')) t WHERE t IS NOT NULL);
    IF cardinality(targets) = 0 THEN RAISE EXCEPTION 'Mindestens ein Zielkonzept angeben'; END IF;
    IF coalesce(trim(p_learner_ref), '') = '' THEN RAISE EXCEPTION 'Kennung des Kindes fehlt'; END IF;
    SELECT array_agg(t) INTO bad FROM unnest(targets) t
     WHERE NOT EXISTS (SELECT 1 FROM curriculum.concepts c WHERE c.id = t AND c.status = 'approved'
                       AND c.subject_code = upper(p_subject_code));
    IF bad IS NOT NULL THEN
        RAISE EXCEPTION 'Zielkonzepte nicht freigegeben, unbekannt oder aus einem anderen Fach: %', bad;
    END IF;
    INSERT INTO learner.learners(tenant, external_ref, grade) VALUES (coalesce(p_tenant, 'default'), p_learner_ref, p_grade)
    ON CONFLICT (tenant, external_ref) DO UPDATE SET grade = coalesce(EXCLUDED.grade, learner.learners.grade)
    RETURNING id INTO lid;
    known := coalesce(array_remove(p_known_mastered, NULL), '{}');
    IF pol.use_previous_mastery THEN
        known := known || coalesce((SELECT array_agg(concept_id) FROM learner.mastery
                                    WHERE learner_id = lid AND state = 'mastered'), '{}');
    END IF;
    INSERT INTO learner.sessions(learner_id, subject_code, grade, target_concepts, known_mastered)
    VALUES (lid, upper(p_subject_code), p_grade, targets, known)
    RETURNING id INTO sid;
    RETURN sid;
END $$;

-- --------------------------------------------------------------------------- Antwort speichern
CREATE OR REPLACE FUNCTION karo._record(p_session uuid, p_item text, p_answer jsonb, p_outcome text,
                                        p_score numeric, p_misconception text) RETURNS jsonb
LANGUAGE plpgsql VOLATILE AS $$
DECLARE
    s learner.sessions;
    it curriculum.items;
    prev learner.responses;
    res record;
    oc text; mid text; sc numeric; fb text;
BEGIN
    SELECT * INTO s FROM learner.sessions WHERE id = p_session FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION 'Sitzung % nicht gefunden', p_session; END IF;

    -- Idempotent: dieselbe Aufgabe zweimal gesendet -> erste Auswertung zurückgeben, nichts doppelt zählen
    SELECT * INTO prev FROM learner.responses WHERE session_id = p_session AND item_id = p_item;
    IF FOUND THEN
        RETURN jsonb_build_object('outcome', prev.outcome, 'misconception_id', prev.misconception_id,
                                  'duplicate', true, 'next', karo.next_step(p_session));
    END IF;
    IF s.status <> 'diagnosing' THEN RAISE EXCEPTION 'Sitzung % ist abgeschlossen', p_session; END IF;
    IF s.current_item IS DISTINCT FROM p_item THEN
        RAISE EXCEPTION 'Aufgabe % wurde in dieser Sitzung nicht gestellt (aktuell: %)', p_item, s.current_item;
    END IF;
    SELECT * INTO it FROM curriculum.items WHERE id = p_item;
    IF NOT EXISTS (SELECT 1 FROM curriculum.concepts c WHERE c.id = it.concept_id AND c.status = 'approved') THEN
        -- Aufgabe wurde inzwischen zurückgezogen: Antwort verwerfen, mit der nächsten Aufgabe weitermachen
        UPDATE learner.sessions SET current_item = NULL, current_concept = NULL WHERE id = p_session;
        RETURN jsonb_build_object('outcome', 'discarded', 'reason', 'Aufgabe nicht mehr freigegeben',
                                  'next', karo.next_step(p_session));
    END IF;

    IF p_outcome IS NOT NULL THEN
        IF p_outcome NOT IN ('correct', 'incorrect', 'misconception', 'partial', 'skipped') THEN
            RAISE EXCEPTION 'Ungültiges Ergebnis %', p_outcome;
        END IF;
        oc := p_outcome; sc := p_score; mid := p_misconception;
    ELSE
        SELECT * INTO res FROM karo.check_answer(p_item, p_answer);
        oc := res.outcome; mid := res.misconception_id; sc := res.score; fb := res.feedback;
    END IF;
    INSERT INTO learner.responses(session_id, learner_id, item_id, concept_id, item_level, answer, outcome,
                                  misconception_id, score, reviewed)
    VALUES (p_session, s.learner_id, p_item, it.concept_id, it.level, p_answer, oc, mid, sc, p_outcome IS NOT NULL);
    UPDATE learner.sessions SET current_item = NULL WHERE id = p_session;
    RETURN jsonb_build_object('outcome', oc, 'misconception_id', mid, 'feedback', fb,
                              'concept_state', karo._state_map(p_session, ARRAY[it.concept_id])->>it.concept_id,
                              'next', karo.next_step(p_session));
END $$;

-- Für das Kind: nur die Antwort, die Datenbank wertet aus
CREATE OR REPLACE FUNCTION karo.record_response(p_session uuid, p_item text, p_answer jsonb) RETURNS jsonb
LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path = curriculum, learner, pg_temp
AS $$ SELECT karo._record(p_session, p_item, p_answer, NULL, NULL, NULL); $$;

-- Für vertrauenswürdige Bewertung (KI/Lehrkraft bei Freitext): Ergebnis direkt setzen
CREATE OR REPLACE FUNCTION karo.record_external_result(p_session uuid, p_item text, p_answer jsonb, p_outcome text,
                                                       p_score numeric DEFAULT NULL,
                                                       p_misconception_id text DEFAULT NULL) RETURNS jsonb
LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path = curriculum, learner, pg_temp
AS $$ SELECT karo._record(p_session, p_item, p_answer, p_outcome, p_score, p_misconception_id); $$;

-- Nachträgliche Bewertung (Freitext). Gibt das neue Ergebnis zurück – NIE die nächste Kinderaufgabe.
-- Braucht das Kind danach weitere Aufgaben, wird needs_followup gesetzt; Karo ruft beim nächsten Besuch next_step.
CREATE OR REPLACE FUNCTION karo.review_response(p_response bigint, p_outcome text, p_score numeric DEFAULT NULL,
                                                p_misconception_id text DEFAULT NULL)
RETURNS jsonb
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = curriculum, learner, pg_temp
AS $$
DECLARE s learner.sessions; sid uuid; ctx jsonb; followup boolean; v_result jsonb;
BEGIN
    IF p_outcome NOT IN ('correct', 'incorrect', 'misconception', 'partial', 'skipped') THEN
        RAISE EXCEPTION 'Ungültiges Ergebnis %', p_outcome;
    END IF;
    UPDATE learner.responses SET outcome = p_outcome, score = p_score, misconception_id = p_misconception_id,
                                 reviewed = true
     WHERE id = p_response RETURNING session_id INTO sid;
    IF sid IS NULL THEN RAISE EXCEPTION 'Antwort % nicht gefunden', p_response; END IF;
    SELECT * INTO s FROM learner.sessions WHERE id = sid FOR UPDATE;
    IF s.status = 'diagnosing' THEN
        RETURN jsonb_build_object('updated', true, 'session_status', s.status);
    END IF;
    -- Abgeschlossene Sitzung: Ergebnis und Lernstände neu berechnen
    ctx := karo._ctx(sid);
    followup := karo._next_item(sid, ctx) IS NOT NULL;     -- würde die Diagnose jetzt noch etwas fragen?
    UPDATE learner.sessions SET needs_followup = followup WHERE id = sid;
    v_result := karo._result(sid, ctx);
    UPDATE learner.sessions SET result = v_result WHERE id = sid;
    PERFORM karo._write_mastery(sid);
    RETURN jsonb_build_object('updated', true, 'needs_followup', followup, 'result', v_result);
END $$;

-- --------------------------------------------------------------------------- Datenschutz
CREATE OR REPLACE FUNCTION karo.forget_learner(p_learner_ref text, p_tenant text DEFAULT 'default') RETURNS int
LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path = learner, pg_temp
AS $$
    WITH d AS (DELETE FROM learner.learners WHERE tenant = coalesce(p_tenant, 'default')
                                              AND external_ref = p_learner_ref RETURNING 1)
    SELECT count(*)::int FROM d;
$$;

CREATE OR REPLACE FUNCTION karo.purge_learner_data(p_days int DEFAULT NULL) RETURNS jsonb
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = curriculum, learner, pg_temp
AS $$
DECLARE cutoff timestamptz; ns int; nm int; nl int;
BEGIN
    cutoff := now() - make_interval(days => coalesce(p_days,
                         (SELECT retention_days FROM curriculum.diagnostic_policy WHERE id = 1)));
    DELETE FROM learner.sessions WHERE started_at < cutoff;
    GET DIAGNOSTICS ns = ROW_COUNT;
    DELETE FROM learner.mastery WHERE updated_at < cutoff;     -- veraltete Lernstände (auch aktiver Kinder)
    GET DIAGNOSTICS nm = ROW_COUNT;
    -- Aufgaben von Arbeitsblättern in Aufträgen (werden normalerweise schon bei Abschluss geleert)
    IF to_regclass('curriculum.topic_requests') IS NOT NULL THEN
        UPDATE curriculum.topic_requests SET tasks = '[]'::jsonb WHERE created_at < cutoff AND tasks <> '[]'::jsonb;
    END IF;
    -- Lernende ohne Aktivität seit der Frist: samt Lernständen löschen
    DELETE FROM learner.learners l
     WHERE l.created_at < cutoff
       AND NOT EXISTS (SELECT 1 FROM learner.sessions x WHERE x.learner_id = l.id)
       AND NOT EXISTS (SELECT 1 FROM learner.mastery m WHERE m.learner_id = l.id AND m.updated_at >= cutoff);
    GET DIAGNOSTICS nl = ROW_COUNT;
    RETURN jsonb_build_object('sessions_deleted', ns, 'mastery_deleted', nm, 'learners_deleted', nl);
END $$;

-- --------------------------------------------------------------------------- Bereitschaft der Daten
CREATE VIEW karo.diagnostic_readiness AS
SELECT c.id AS concept_id, c.subject_code, c.block_id, c.target_grade,
       count(i.*) FILTER (WHERE i.kind IN ('diagnostic', 'misconception') AND i.level = 'target' AND i.auto_checkable) AS target_probes,
       count(i.*) FILTER (WHERE i.kind = 'diagnostic' AND i.level = 'below') AS below_probes,
       count(i.*) FILTER (WHERE i.kind = 'misconception') AS misconception_probes,
       count(i.*) FILTER (WHERE i.kind = 'exit' AND i.auto_checkable) AS exit_items,
       (SELECT count(*) FROM curriculum.concept_prerequisites p
         WHERE p.concept_id = c.id
           AND NOT EXISTS (SELECT 1 FROM curriculum.concepts x WHERE x.id = p.prerequisite_id AND x.status = 'approved'))
         AS missing_prerequisites,
       (count(i.*) FILTER (WHERE i.kind IN ('diagnostic', 'misconception') AND i.level = 'target' AND i.auto_checkable) >= 2
        AND (count(i.*) FILTER (WHERE i.kind = 'diagnostic' AND i.level = 'below') >= 1 OR c.target_grade = 1)
        AND count(i.*) FILTER (WHERE i.kind = 'exit' AND i.auto_checkable) >= 2
        AND NOT EXISTS (SELECT 1 FROM curriculum.concept_prerequisites p
                         WHERE p.concept_id = c.id
                           AND NOT EXISTS (SELECT 1 FROM curriculum.concepts x
                                           WHERE x.id = p.prerequisite_id AND x.status = 'approved'))) AS ready
FROM curriculum.concepts c
LEFT JOIN curriculum.items i ON i.concept_id = c.id
WHERE c.status = 'approved'
GROUP BY c.id;

INSERT INTO curriculum.schema_version(version) VALUES (3) ON CONFLICT DO NOTHING;
