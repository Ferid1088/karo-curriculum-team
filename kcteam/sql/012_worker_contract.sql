-- Worker-Provenance und Session-Vertragsfassung.
--
-- Der Schaden vom 4.10.: ein zweiter Agent (lokaler Prozess, alter Code)
-- schrieb gleichzeitig mit dem Docker-Agent in dieselbe Queue. Beide
-- meldeten sich unter 'agent' — der eine Herzschlag ueberschrieb den
-- anderen, von aussen sah es aus wie ein Dienst. Jetzt traegt jede
-- Instanz ihre eigene Identitaet (Rechner:Prozess) und ihre Umgebung;
-- zwei Schreiber desselben Dienstes bleiben nebeneinander sichtbar.
ALTER TABLE curriculum.service_heartbeat
    ADD COLUMN IF NOT EXISTS instance text NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS hostname text,
    ADD COLUMN IF NOT EXISTS environment text;
ALTER TABLE curriculum.service_heartbeat
    DROP CONSTRAINT IF EXISTS service_heartbeat_pkey;
ALTER TABLE curriculum.service_heartbeat
    ADD CONSTRAINT service_heartbeat_pkey PRIMARY KEY (service, instance);

-- Wer hat den Export gebaut — Instanz, Lauf, Stand und Vertragsfassung
-- des klaemenden Workers. Damit laesst sich jede Lektion auf den Code
-- zurueckfuehren, der sie erzeugt hat; ueber run_id haengen die
-- agent_calls mit Anbieter und Modell je Rolle dran.
ALTER TABLE curriculum.lesson_exports
    ADD COLUMN IF NOT EXISTS claimed_by_run_id text,
    ADD COLUMN IF NOT EXISTS completed_by_run_id text,
    ADD COLUMN IF NOT EXISTS run_id text,
    ADD COLUMN IF NOT EXISTS provider text,
    ADD COLUMN IF NOT EXISTS generator_git_sha text,
    ADD COLUMN IF NOT EXISTS contract_version text;

-- Dasselbe fuer Themenauftraege: welcher Worker hat ihn bearbeitet.
ALTER TABLE curriculum.topic_requests
    ADD COLUMN IF NOT EXISTS claimed_by_run_id text,
    ADD COLUMN IF NOT EXISTS completed_by_run_id text;

-- Ergebnis-Cache nur unter demselben Vertrag wiederverwenden: ein Result
-- aus v1.x darf nie still als v1.y-Material dienen. NULL heisst „aus
-- einer Fassung vor diesem Feld" — legacy, nicht geraten.
ALTER TABLE curriculum.provider_sessions
    ADD COLUMN IF NOT EXISTS contract_version text;
