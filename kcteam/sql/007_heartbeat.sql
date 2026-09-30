-- Welcher Dienst laeuft gerade mit welchem Stand.
--
-- Drei Prozesse teilen sich eine Datenbank: API, Agent und Browser. Laufen sie
-- aus verschiedenen Images, ist das von aussen nicht zu sehen — die API meldet
-- ihren Stand unter /v1/meta, der Agent gar nichts. Genau so ist es passiert:
-- die API war neu, Agent und Browser liefen noch tagelang auf dem alten Bild,
-- und niemand konnte sagen, welcher Code eine Lektion geschrieben hatte.
CREATE TABLE IF NOT EXISTS curriculum.service_heartbeat (
    service    text PRIMARY KEY,          -- api | agent | admin | cli
    git_sha    text NOT NULL,
    contract_version text,
    pid        int,
    started_at timestamptz NOT NULL DEFAULT now(),
    last_seen  timestamptz NOT NULL DEFAULT now()
);
