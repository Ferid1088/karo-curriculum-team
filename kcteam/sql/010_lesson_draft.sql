-- Vertrag 1.5: kuratierte Lektions-Entwürfe am Konzept.
--
-- Die Agenten schreiben Lektionen zur Laufzeit aus dem geprüften Konzept.
-- Kuratierte Slices kommen dagegen mit einer fertig verfassten Lektion im
-- Format des Abnehmers: sie ist das Ergebnis derselben Qualitätsarbeit, nur
-- schon geschrieben. Der Export-Worker liefert sie unverändert aus — vorher
-- läuft sie durch dieselbe Format- und Abnehmerprüfung wie jede andere
-- Lektion (`lessons.generate` → `check_lesson` + `consumer_findings`).
ALTER TABLE curriculum.concepts
    ADD COLUMN IF NOT EXISTS lesson_draft jsonb;
