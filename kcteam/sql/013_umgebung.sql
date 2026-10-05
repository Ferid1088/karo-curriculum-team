-- Queue-Namespace: ein Worker claimt nur die Auftraege seiner Umgebung.
--
-- Der Ghost vom 4.10. war nicht nur unsichtbar, er war auch nicht
-- aufgehalten: jeder Prozess mit der Datenbank-URL konnte die
-- Produktions-Schlange leer arbeiten, egal welche Umgebung er meinte.
-- Die Herzschlag-Trennung zeigt so einen Fremdkoerper jetzt — dieser
-- Namespace haelt ihn erst gar nicht an die Auftraege.
--
-- Die Umgebung reist mit der Verbindung: jeder Prozess setzt beim
-- Verbinden die GUC kcteam.env aus seiner KCTEAM_ENV; Zeilen, die
-- ueber diese Verbindung entstehen (egal ob aus Python oder aus der
-- SQL-Funktion karo.resolve_topic), tragen sie im Standardwert.
-- Wer nichts setzt — ein nacktes psql etwa — landet bei „produktion",
-- denn die einzige Umgebung, deren Schutz zaehlt, ist die echte.
ALTER TABLE curriculum.lesson_exports
    ADD COLUMN IF NOT EXISTS environment text NOT NULL DEFAULT 'produktion';
ALTER TABLE curriculum.lesson_exports
    ALTER COLUMN environment SET DEFAULT
    coalesce(nullif(current_setting('kcteam.env', true), ''), 'produktion');

ALTER TABLE curriculum.topic_requests
    ADD COLUMN IF NOT EXISTS environment text NOT NULL DEFAULT 'produktion';
ALTER TABLE curriculum.topic_requests
    ALTER COLUMN environment SET DEFAULT
    coalesce(nullif(current_setting('kcteam.env', true), ''), 'produktion');
