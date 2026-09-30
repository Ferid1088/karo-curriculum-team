-- Wer zuerst drankommt, und was ein Thema bisher gekostet hat.
--
-- Vorher lief die Schlange nach Eingang. Bei siebzehn Prüfungsthemen hiess
-- das: das Thema fuer die Arbeit am Freitag wartete hinter dem fuer die Arbeit
-- in drei Wochen. Ein Datum, bis wann es gebraucht wird, macht daraus eine
-- Reihenfolge, die sich jemandem erklaeren laesst.

ALTER TABLE curriculum.topic_requests ADD COLUMN IF NOT EXISTS needed_by date;
ALTER TABLE curriculum.lesson_exports ADD COLUMN IF NOT EXISTS needed_by date;

-- Die Schlange: erst was ein Datum hat und frueher gebraucht wird, dann der Rest.
DROP INDEX IF EXISTS curriculum.topic_requests_queue_idx;
CREATE INDEX IF NOT EXISTS topic_requests_queue_idx
    ON curriculum.topic_requests(status, needed_by NULLS LAST, priority DESC, created_at);
DROP INDEX IF EXISTS curriculum.lesson_exports_queue_idx;
CREATE INDEX IF NOT EXISTS lesson_exports_queue_idx
    ON curriculum.lesson_exports(status, needed_by NULLS LAST, next_attempt_at);

-- Zu welchem Thema ein Modellaufruf gehoerte. Ohne das steht im Protokoll
-- „EXP-412" und niemand kann sagen, was ein Thema gekostet hat.
CREATE OR REPLACE FUNCTION curriculum.topic_of(p_entity text) RETURNS text
LANGUAGE sql STABLE AS $$
    SELECT CASE
        WHEN p_entity LIKE 'REQ-%' THEN
            (SELECT r.topic FROM curriculum.topic_requests r
              WHERE r.id = substring(p_entity from 5)::bigint)
        WHEN p_entity LIKE 'EXP-%' THEN
            (SELECT coalesce(e.topic, r.topic) FROM curriculum.lesson_exports e
               LEFT JOIN curriculum.topic_requests r ON r.id = e.request_id
              WHERE e.id = substring(p_entity from 5)::bigint)
    END
$$;

ALTER TABLE curriculum.agent_calls ADD COLUMN IF NOT EXISTS topic text;
CREATE INDEX IF NOT EXISTS agent_calls_topic_idx ON curriculum.agent_calls(topic, created_at);

-- Was ein Thema bisher an Modellaufrufen gekostet hat — die Zahl, ohne die
-- „steuern" nur ein Wort ist.
CREATE OR REPLACE VIEW curriculum.topic_cost AS
SELECT a.topic,
       count(*)                                          AS calls,
       count(*) FILTER (WHERE NOT a.ok)                  AS failed,
       sum(a.input_tokens + a.output_tokens)             AS tokens,
       min(a.created_at)                                 AS first_call,
       max(a.created_at)                                 AS last_call
  FROM curriculum.agent_calls a
 WHERE a.topic IS NOT NULL
 GROUP BY a.topic;
