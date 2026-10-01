-- Die Pause, die alle Prozesse sehen.
--
-- Vorher lag das Wissen „Kontingent erschoepft" im Prozess: ein Cooldown in
-- einer Python-Variablen. Der Agent startete neu und klopfte wieder, die API
-- wusste ohnehin nichts davon. Im Protokoll standen 262 Aufrufe fuer ein
-- Thema, 261 davon gegen dieselbe geschlossene Tuer.
--
-- Eine Zeile je Anbieter: wann die Pause endet, warum sie begann, und wie
-- lang die letzte war (fuer die Verdopplung, wenn der Anbieter keine
-- Reset-Zeit nennt).
CREATE TABLE IF NOT EXISTS curriculum.provider_pause (
    provider     text PRIMARY KEY,
    bis          timestamptz NOT NULL,
    grund        text NOT NULL,
    erkannt_am   timestamptz NOT NULL DEFAULT now(),
    minuten      int,                 -- Laenge dieser Pause, NULL bei Reset-Angabe
    -- Nach dem Ende genau EIN Probeaufruf. Diese Spalte haelt fest, dass er
    -- schon laeuft, damit nicht mehrere Worker gleichzeitig proben.
    probe_seit   timestamptz,
    probe_am     timestamptz,
    aufrufe_verhindert int NOT NULL DEFAULT 0
);
