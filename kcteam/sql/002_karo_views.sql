-- Schema "karo": die Schnittstelle, die Karo liest.
-- Enthält ausschließlich vom Kinderrechts-Inspektor (bzw. einem Menschen) freigegebene Konzepte.
-- Achtung: karo.items und karo.concept_bundle enthalten Lösungen – nur für Karos Backend, nie direkt ans Kind.
-- Für Kinder: karo.item_for_child (ohne Lösungen).

-- Sichten werden neu angelegt, damit sich Spalten ändern dürfen (CREATE OR REPLACE kann keine Spalten entfernen).
-- Ohne CASCADE: Hat Karo eigene Sichten darauf gebaut, bricht die Migration mit klarer Meldung ab, statt sie
-- stillschweigend zu löschen.
DROP VIEW IF EXISTS karo.diagnostic_readiness;
DROP VIEW IF EXISTS karo.visual_explanations;
DROP VIEW IF EXISTS karo.items;
DROP VIEW IF EXISTS karo.misconceptions;
DROP VIEW IF EXISTS karo.prerequisites;
DROP VIEW IF EXISTS karo.concepts;
DROP VIEW IF EXISTS karo.topic_blocks;
DROP VIEW IF EXISTS karo.subjects;

CREATE OR REPLACE VIEW karo.subjects AS
SELECT code, name, grade_min, grade_max
FROM curriculum.subjects;

CREATE OR REPLACE VIEW karo.topic_blocks AS
SELECT b.id, b.subject_code, b.title, b.description, b.grade_min, b.grade_max,
       b.typical_grade, b.varies, b.variance_note
FROM curriculum.topic_blocks b
WHERE EXISTS (SELECT 1 FROM curriculum.concepts c WHERE c.block_id = b.id AND c.status = 'approved');

CREATE OR REPLACE VIEW karo.concepts AS
SELECT c.id, c.subject_code, c.block_id, b.title AS block_title,
       c.title, c.description, c.first_contact_grade, c.target_grade, c.varies, c.sort_order,
       c.levels, c.can_do, c.difficulty_parameters,
       c.calibration -> 'boundary_items' AS boundary_items,
       c.version, c.updated_at, c.visual_need, c.track, c.learning_year, c.cefr
FROM curriculum.concepts c
JOIN curriculum.topic_blocks b ON b.id = c.block_id
WHERE c.status = 'approved';

CREATE OR REPLACE VIEW karo.prerequisites AS
SELECT p.concept_id, p.prerequisite_id,
       EXISTS (SELECT 1 FROM curriculum.concepts x
               WHERE x.id = p.prerequisite_id AND x.status = 'approved') AS prerequisite_available
FROM curriculum.concept_prerequisites p
JOIN curriculum.concepts c ON c.id = p.concept_id AND c.status = 'approved';

CREATE OR REPLACE VIEW karo.misconceptions AS
SELECT m.id, m.concept_id, m.key, m.description, m.remediation_hint
FROM curriculum.misconceptions m
JOIN curriculum.concepts c ON c.id = m.concept_id AND c.status = 'approved';

CREATE OR REPLACE VIEW karo.items AS
SELECT i.id, i.concept_id, i.kind, i.level, i.grade, i.prompt, i.solution,
       i.representation, i.misconception_id, i.sort_order,
       i.visual, i.visual_svg, i.interaction, (i.visual IS NOT NULL) AS is_visual,
       i.answer, i.distractors, i.auto_checkable
FROM curriculum.items i
JOIN curriculum.concepts c ON c.id = i.concept_id AND c.status = 'approved';

CREATE OR REPLACE VIEW karo.visual_explanations AS
SELECT v.id, v.concept_id, v.key, v.purpose, v.level, v.misconception_id, v.steps, v.sort_order
FROM curriculum.visual_explanations v
JOIN curriculum.concepts c ON c.id = v.concept_id AND c.status = 'approved';

-- Lernpfad: vom Ziel rückwärts durch die Voraussetzungen, bis zu dem, was das Kind schon kann.
-- Ergebnis in Lernreihenfolge (tiefste Voraussetzung zuerst, Ziel zuletzt). depth = längster Weg zum Ziel.
-- available = false: Konzept ist (noch) nicht freigegeben oder fehlt – dann ohne Titel.
-- UNION (statt UNION ALL mit Pfad) hält die Laufzeit linear: jedes (Konzept, Tiefe) nur einmal.
CREATE OR REPLACE FUNCTION karo.learning_path(p_targets text[], p_mastered text[] DEFAULT '{}')
RETURNS TABLE (concept_id text, title text, target_grade int, depth int, available boolean)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = curriculum, pg_temp
AS $$
    WITH RECURSIVE known AS (SELECT coalesce(array_remove(p_mastered, NULL), '{}') AS m),
    walk(id, depth) AS (
        SELECT t, 0 FROM unnest(array_remove(p_targets, NULL)) AS t, known WHERE NOT (t = ANY (known.m))
        UNION
        SELECT p.prerequisite_id, w.depth + 1
        FROM walk w
        JOIN curriculum.concept_prerequisites p ON p.concept_id = w.id
        CROSS JOIN known
        WHERE NOT (p.prerequisite_id = ANY (known.m))
          AND w.depth < 30                                  -- Schutz gegen Kreise
    ),
    agg AS (SELECT id, max(depth) AS depth FROM walk GROUP BY id)
    SELECT a.id, CASE WHEN c.status = 'approved' THEN c.title END, c.target_grade, a.depth,
           coalesce(c.status = 'approved', false) AS available
    FROM agg a
    LEFT JOIN curriculum.concepts c ON c.id = a.id
    ORDER BY a.depth DESC, c.target_grade NULLS FIRST, c.sort_order, a.id;
$$;

-- Alles zu einem Konzept in einem JSON: Niveaus, Grenzen, Voraussetzungen, Fehlvorstellungen, Aufgaben.
CREATE OR REPLACE FUNCTION karo.concept_bundle(p_id text)
RETURNS jsonb
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = curriculum, pg_temp
AS $$
    SELECT jsonb_build_object(
        'id', c.id, 'subject_code', c.subject_code, 'block_id', c.block_id,
        'title', c.title, 'description', c.description,
        'first_contact_grade', c.first_contact_grade, 'target_grade', c.target_grade,
        'varies', c.varies, 'levels', c.levels, 'can_do', c.can_do, 'visual_need', c.visual_need,
        'track', c.track, 'learning_year', c.learning_year, 'cefr', c.cefr,
        'visual_explanations', coalesce((SELECT jsonb_agg(jsonb_build_object(
                                     'id', v.id, 'key', v.key, 'purpose', v.purpose, 'level', v.level,
                                     'misconception_id', v.misconception_id, 'steps', v.steps) ORDER BY v.sort_order)
                                 FROM curriculum.visual_explanations v WHERE v.concept_id = c.id), '[]'::jsonb),
        'difficulty_parameters', c.difficulty_parameters,
        'boundary_items', c.calibration -> 'boundary_items',
        'prerequisites', coalesce((SELECT jsonb_agg(p.prerequisite_id ORDER BY p.prerequisite_id)
                                   FROM curriculum.concept_prerequisites p WHERE p.concept_id = c.id), '[]'::jsonb),
        'misconceptions', coalesce((SELECT jsonb_agg(jsonb_build_object(
                                        'id', m.id, 'key', m.key, 'description', m.description,
                                        'remediation_hint', m.remediation_hint) ORDER BY m.key)
                                    FROM curriculum.misconceptions m WHERE m.concept_id = c.id), '[]'::jsonb),
        'items', coalesce((SELECT jsonb_agg(jsonb_build_object(
                               'id', i.id, 'kind', i.kind, 'level', i.level, 'grade', i.grade,
                               'prompt', i.prompt, 'solution', i.solution,
                               'representation', i.representation, 'misconception_id', i.misconception_id,
                               'interaction', i.interaction, 'visual', i.visual, 'visual_svg', i.visual_svg,
                               'answer', i.answer, 'distractors', i.distractors, 'auto_checkable', i.auto_checkable)
                               ORDER BY i.kind, i.sort_order)
                           FROM curriculum.items i WHERE i.concept_id = c.id), '[]'::jsonb),
        'version', c.version
    )
    FROM curriculum.concepts c
    WHERE c.id = p_id AND c.status = 'approved';
$$;

-- Konzepte zu einem Arbeitsblatt finden: Fach + Klasse + Suchbegriffe (Titel/Beschreibung).
CREATE OR REPLACE FUNCTION karo.find_concepts(p_subject_code text, p_grade int, p_query text DEFAULT NULL)
RETURNS TABLE (concept_id text, title text, block_title text, target_grade int, first_contact_grade int)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = curriculum, pg_temp
AS $$
    SELECT c.id, c.title, b.title, c.target_grade, c.first_contact_grade
    FROM curriculum.concepts c
    JOIN curriculum.topic_blocks b ON b.id = c.block_id
    WHERE c.status = 'approved'
      AND c.subject_code = upper(p_subject_code)
      AND p_grade BETWEEN c.first_contact_grade AND c.target_grade + 1
      AND (p_query IS NULL OR c.title ILIKE '%' || p_query || '%'
                          OR c.description ILIKE '%' || p_query || '%'
                          OR b.title ILIKE '%' || p_query || '%')
    ORDER BY abs(c.target_grade - p_grade), c.block_id, c.sort_order;
$$;

INSERT INTO curriculum.schema_version(version) VALUES (2) ON CONFLICT DO NOTHING;
