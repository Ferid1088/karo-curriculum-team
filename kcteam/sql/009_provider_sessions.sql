-- Asynchrone Anbieter: welcher Modellaufruf laeuft bei welcher Session?
--
-- Devin arbeitet nicht synchron: der Aufruf legt eine Session an und das
-- Ergebnis kommt erst spaeter. Wuerde die Zuordnung nur im Prozess leben,
-- legte ein Neustart des Workers dieselbe Session noch einmal an — doppelt
-- bezahlt, doppelt gelaufen. Der Schluessel ist der Fingerabdruck des
-- Aufrufs (Anbieter, Modell, Prompts): derselbe Auftrag erzeugt denselben
-- Prompt und findet seine Session wieder; ein ueberarbeiteter Prompt nach
-- einer Inspektor-Rueckmeldung ist bewusst eine neue Session.
CREATE TABLE IF NOT EXISTS curriculum.provider_sessions (
    call_key    text PRIMARY KEY,          -- sha256(provider|model|system|user)
    provider    text NOT NULL,
    session_id  text NOT NULL,
    role        text,
    entity_id   text,
    status      text NOT NULL DEFAULT 'working',   -- working | finished | failed
    nudged      boolean NOT NULL DEFAULT false,    -- "bitte JSON liefern" schon geschickt?
    restarts    int NOT NULL DEFAULT 0,            -- abgelaufene/fehlgeschlagene Sessions neu gestartet
    detail      text,                              -- letzter Status oder Fehlergrund
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now()
);
