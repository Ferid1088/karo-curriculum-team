-- Asynchrone Anbieter: das Ergebnis einer fertigen Session lokal festhalten.
--
-- Bisher lag `structured_output` nur bei Devin: ein Worker-Neustart zwischen
-- Ergebnis und Job-Abschluss musste die Session erneut pollen. Ist sie in
-- der Zwischenzeit remote verschwunden (404), zwang das einen Neubau —
-- obwohl die Arbeit laengst bezahlt und geliefert war. Seit der Cache die
-- Antwort selbst traegt, ist 'finished' ein Endzustand: dasselbe Ergebnis
-- wird zurueckgegeben, ohne je wieder an den Anbieter zu gehen.
ALTER TABLE curriculum.provider_sessions
    ADD COLUMN IF NOT EXISTS result jsonb,
    ADD COLUMN IF NOT EXISTS missing_remote int NOT NULL DEFAULT 0;
