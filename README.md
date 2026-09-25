# Karo Curriculum Team

Ein Team aus KI-Agenten, das für ein Schulfach die **Wissensbasis für Karo** erstellt und in **PostgreSQL / Supabase** ablegt – so, dass Karo sie direkt nutzen kann.

Du gibst nur **Fach** und **Klasse (z. B. 6) oder Klassenbereich (z. B. 4–7)** an. Für jedes Fach der Klassen 1–13 gibt es ein **eigenes Team** mit fachspezifischen Expertenanweisungen, Beispielen und Regeln (35 Fächer, siehe unten). Das Team erstellt Themenblöcke, zerlegt sie in kleine Konzepte mit Voraussetzungen, legt pro Konzept Unter- und Obergrenze sowie Zielniveau fest, schreibt Diagnose- und Abschlussaufgaben, entwirft **visuelle Erklärungen** – und der **Kinderrechts-Inspektor** prüft jeden Schritt mit Veto-Recht.

Keine Trennung nach Bundesländern: Die Klassenstufe ist die *typische* Zuordnung über mehrere Lehrpläne; wo die Länder deutlich abweichen, steht `varies = true`.

---

## Ein Team pro Fach

`kcteam subjects` zeigt alle Fächer. Gängige Namen werden erkannt (z. B. „Bio“, „Geografie“, „Gemeinschaftskunde“, „HSU“, „Werte und Normen“).

| Fächergruppe | Fächer |
|---|---|
| Mathematik | Mathematik (1–13) |
| Deutsch | Deutsch (1–13) |
| Sachunterricht | Sachunterricht (1–4) |
| Moderne Fremdsprachen | Englisch, Französisch, Spanisch, Italienisch, Russisch, Türkisch, Niederländisch, Polnisch, Chinesisch |
| Alte Sprachen | Latein, Altgriechisch |
| Naturwissenschaften | Biologie, Chemie, Physik, Naturwissenschaften (integriert) |
| Informatik | Informatik |
| Gesellschaftswissenschaften | Geschichte, Erdkunde, Politik (Sozialkunde, Gemeinschaftskunde, PoWi …), Wirtschaft, Pädagogik, Psychologie |
| Religion/Ethik | Ev., kath., islamische, jüdische Religion, Ethik (Werte und Normen, LER, Praktische Philosophie), Philosophie |
| Ästhetik, Sport, Technik | Kunst, Musik, Sport (Theorie), Technik |

Jede Fächergruppe hat ein Profil in `kcteam/subjects/families/*.yaml`, jedes Fach Ergänzungen in `kcteam/subjects/registry.yaml`:

- **Expertenanweisungen für jede der 8 Rollen** (inkl. Curriculum-Agent), z. B. Basiskonzepte und belegte Schülervorstellungen in Biologie, Beutelsbacher Konsens in Politik, NS-Zeit-Regeln in Geschichte, Urheberrecht bei Lesetexten in Deutsch
- **Niveau-Achse:** Klasse, bei Sprachen **Lernjahr + GER-Stufe** (typischer Beginn hinterlegt, z. B. Spanisch Kl. 6). Oberstufe mit **gA/eA** (Grund-/Leistungskurs).
- **Messbare Schwierigkeitsparameter**, passende **Antwortformate** und **Visual-Typen**
- **Sensible Themen** mit Regeln für den Inspektor, z. B. Sexualerziehung, Experimente, Religion, Politik
- **Was nicht in die App gehört**, z. B. Singen, praktischer Sport, Werkstatt, Audio-Hörverstehen
- **Ein Beispiel aus dem Fach** als Format-Orientierung

Fächer ohne Profil laufen mit einem allgemeinen Profil (`--allow-unknown`). Profile lassen sich ohne Programmieren in den YAML-Dateien anpassen.

---

## Das Team

| Rolle | Aufgabe |
|---|---|
| **Curriculum-Analyst** | Themenblöcke für das Fach über den Klassenbereich (mit Websuche, typische Klassenstufe) |
| **Fachdidaktiker** | Zerlegt jeden Block in prüfbare Konzepte und baut die Voraussetzungskanten |
| **Niveau-Kalibrierer** | Pro Konzept: below / target / above, Kann-Aussagen, messbare Schwierigkeitsparameter, Anker- und Grenzaufgaben |
| **Diagnostiker** | Fehlvorstellungen mit Diagnoseaufgaben, Einstiegs-Diagnose, Abschlussaufgaben (immer genau auf Zielniveau) |
| **Visual-Didaktiker** | Entscheidet, ob ein Konzept eine Darstellung braucht (essential / helpful / none), entwirft Erklärungen als Bildfolgen mit kurzen Sätzen und visuelle Aufgaben – als Daten aus dem Visual-Katalog, keine Videos |
| **Kritiker** | Prüft jeden Block fachlich (auch die Visuals) und schickt Mängel an die zuständige Rolle zurück |
| **Integrator** | Automatische Prüfung ohne KI: IDs, Kreise im Graphen, Niveau-Konsistenz, Pflichtfelder |
| **Kinderrechts-Inspektor** | Prüft **jede Station** (Themen, Titel, Aufgaben, Beispiele …) auf Altersgerechtheit, Kinderrechte, Werbung, Datenschutz, Stereotype. **Sein Wort ist bindend.** |
| **Curriculum-Agent** | Läuft als Dienst für Karo: ordnet Arbeitsblätter vorhandenen Konzepten zu oder lässt fehlende sofort erzeugen (siehe unten) |

```
Fach ─► Curriculum-Analyst ─► [Inspektor] ─► Themenblöcke
          pro Block:   Fachdidaktiker ─► Integrator ─► [Inspektor]
          pro Konzept: Kalibrierer    ─► Integrator ─► [Inspektor]
                       Diagnostiker   ─► Integrator ─► [Inspektor]
                       Visual-Didakt. ─► Integrator ─► [Inspektor]
          pro Block:   Kritiker ─► Korrekturen an die zuständige Rolle
                       Schlussprüfung [Inspektor] ─► approved ─► sichtbar für Karo
```

**Veto-Regeln**
- Lehnt der Inspektor ab, geht seine Rückmeldung (Fundstelle, Regel, Auflage) automatisch an den Agenten, der den Inhalt erstellt hat.
- Nach **3 Ablehnungen** wird der Inhalt gesperrt und landet in der Warteschlange für Menschen.
- **Über dem Inspektor steht ein Mensch** (pädagogische Fachkraft): Er kann freigeben, verwerfen oder mit Hinweis neu bearbeiten lassen. Freigaben werden protokolliert, und der Inspektor meldet genau diese Punkte danach nicht erneut.
- Karo sieht **nur** freigegebene Konzepte.

---

## Klasse oder Klassenbereich wählen

```bash
kcteam run -s Mathematik -g 6        # nur Klasse 6
kcteam run -s Mathematik -g 4-7      # Klassen 4 bis 7
```

- Bearbeitet werden alle Konzepte, deren **Zielklasse** im gewählten Bereich liegt.
- Zusätzlich zieht das Team automatisch die **Voraussetzungen aus tieferen Klassen** nach (z. B. Einmaleins für Brüche in Klasse 6). Karo kann ein Kind also auch von Klasse 3 aus zum Ziel führen. Abschalten mit `pipeline.include_prerequisites: false`.
- Konzepte höherer Klassen bleiben unbearbeitet und werden bei einem späteren Lauf (z. B. `-g 7`) ergänzt. Fertiges wird nicht neu erzeugt.
- Die Themenlandkarte umfasst immer die ganze Schulzeit (Standard Klasse 1–10), damit IDs stabil bleiben.

---

## Visuelle Erklärungen (ohne Video)

Der Visual-Didaktiker liefert **Darstellungen als Daten** (JSON) aus einem festen Katalog. Beim Speichern wird daraus ein **SVG vorgerendert** und mit abgelegt. Karo kann also das SVG direkt anzeigen oder aus den Daten eigene interaktive Komponenten bauen, zum Beispiel mit den Zahlen vom Arbeitsblatt des Kindes.

| Bereich | Typen |
|---|---|
| Mathematik | `fraction_bar`, `fraction_circle`, `number_line`, `place_value_chart`, `area_model`, `coordinate_plane`, `geometry`, `balance_scale`, `bar_chart`, `table` |
| Sprache | `sentence_parts`, `word_parts`, `syllables`, `table` |
| Sachfächer | `cycle`, `flow_diagram`, `timeline`, `bar_chart`, `table` |
| Naturwiss./Sachfächer | `labeled_diagram` (beschriftete Schemazeichnung, auch als Aufgabe „Welche Nummer ist …?“) |
| Notlösung | `freeform_svg` (wird streng bereinigt: keine Skripte, keine externen Inhalte) |

- **Erklärungen** sind Schrittfolgen: pro Schritt eine Darstellung und ein kurzer Satz. Sie gibt es auf Ziel- und auf Einstiegsniveau, und gezielt zu einzelnen Fehlvorstellungen.
- **Visuelle Aufgaben** gibt es für Diagnose, Übung und Abschluss, mit Interaktionsart (`view`, `select`, `mark`, `drag`, `input`).
- **Geprüft werden:** fachliche Werte (z. B. Zähler ≤ Nenner, Markierungen im Bereich, keine Kreise im Ablauf), ob jede Darstellung zeichenbar ist, Niveaus, Alt-Texte, und der Inhalt durch den Inspektor.
- **Barrierearm:** farbenblind-sichere Palette, Alt-Text als `<title>` im SVG.

```bash
kcteam catalog                   # HTML-Galerie aller Typen -> data/export/visual-katalog.html
kcteam preview -b MA.BRUECHE     # so sieht ein Block mit seinen Visuals aus
kcteam preview -c MA.BRUECHE.ADD_UNGL
```

Bereits freigegebene Konzepte ohne Visuals werden beim nächsten Lauf automatisch nachgerüstet und bleiben währenddessen für Karo sichtbar. Visuals ganz abschalten: `pipeline.visuals: false`.

---

## Schnellstart (Docker)

Voraussetzung: Docker Desktop läuft. Python, Node oder uv brauchst du **nicht** – alles steckt im Image.

```bash
cd karo-curriculum-team
cp .env.example .env                     # 1. einmalig: Zugangsdaten eintragen (siehe unten)
docker compose build                     # 2. einmalig (und nach jedem Update): Image bauen
docker compose up -d postgres            # 3. Datenbank starten (bleibt im Volume pgdata erhalten)
docker compose run --rm kcteam           # 4. Team starten – fragt nach Fach und Klassen
```

In `.env` mindestens:
```
KCTEAM_PROVIDER=claude_token
CLAUDE_CODE_OAUTH_TOKEN=<Token>          # einmalig auf deinem Rechner: claude setup-token
DATABASE_URL=postgresql://karo:karo@postgres:5432/karo   # im Container heißt die DB "postgres", NICHT localhost
```
Probelauf ohne KI-Kosten: `docker compose run --rm -e KCTEAM_PROVIDER=mock kcteam run -s Mathematik -g 5-6`.

Curriculum-Agent für Karo dauerhaft starten: `docker compose up -d curriculum-agent` (Logs: `docker compose logs -f curriculum-agent`).

Die `.env` kommt **nicht** ins Image (`.dockerignore`) – der Token steht nur zur Laufzeit im Container.

Weitere Befehle:

```bash
docker compose run --rm kcteam run -s Mathematik -g 3-8 -b "Brüche" -p claude_token
docker compose run --rm kcteam status
docker compose run --rm kcteam review list
docker compose run --rm kcteam review show 12
docker compose run --rm kcteam review approve 12 -n "geprüft, ok" --who "Name"
docker compose run --rm kcteam review retry 12 -n "Beispiel ohne Geld formulieren"
docker compose run --rm kcteam review reject 12 -n "Thema ungeeignet"
docker compose run --rm kcteam export -s Mathematik      # JSON-Dateien nach ./data/export
docker compose run --rm kcteam providers                  # welche Zugänge eingerichtet sind
docker compose run --rm -e KCTEAM_PROVIDER=mock kcteam run -s Mathematik   # Probelauf ohne KI-Kosten
```

Ohne Docker: siehe „Ohne Docker (mit uv)“ weiter unten.

**Abbrechen ist sicher.** Jeder Zwischenstand liegt in der Datenbank. Ein neuer Lauf macht genau dort weiter (auch nach Budget-Ende oder erreichtem Abo-Limit), bereits Fertiges wird nicht noch einmal bezahlt.

---

## Ohne Docker (mit uv)

Voraussetzung: [uv](https://docs.astral.sh/uv/). Python musst du nicht selbst installieren – uv holt Python 3.12 automatisch (das Team braucht mindestens 3.11; das macOS-Python 3.9 reicht nicht).

```bash
cd karo-curriculum-team
uv sync                                   # legt .venv an und installiert alles (einmalig)
uv run kcteam providers                   # welche KI-Zugänge sind eingerichtet?
uv run kcteam run -s Mathematik -g 5-6    # Team starten
```

Was du dafür zusätzlich brauchst:
- **Postgres:** am einfachsten nur die Datenbank per Docker starten – `docker compose up -d postgres` – und in `.env` **`localhost`** statt `postgres` eintragen: `DATABASE_URL=postgresql://karo:karo@localhost:5432/karo`
- **Claude-Abo (`claude_token`):** die Claude-CLI installieren (`npm install -g @anthropic-ai/claude-code`), dann `claude setup-token` und den Token als `CLAUDE_CODE_OAUTH_TOKEN` in `.env` eintragen.
- **Karo-Spec:** `KARO_SPEC_PATH` in `.env` auf eine lokale Datei oder einen Ordner setzen (sonst läuft das Team ohne).
- Tests: `uv sync --extra dev` und `TEST_DATABASE_URL=… uv run pytest -q`

Hinweis: `requirements.txt` ist eine Paketliste, kein Programm – `python requirements.txt` funktioniert nicht. Mit uv brauchst du sie gar nicht.

## KI-Zugang wählen

In `.env` den gewünschten Zugang eintragen und `KCTEAM_PROVIDER` setzen, oder beim Start auswählen.

| Provider | Was du brauchst | Hinweis |
|---|---|---|
| `claude_token` *(Standard)* | Claude Pro/Max-Abo. Im eigenen Terminal `claude setup-token` ausführen, den Token als `CLAUDE_CODE_OAUTH_TOKEN` eintragen | Läuft über die claude CLI (ist im Docker-Image). Es gelten die Nutzungslimits deines Abos |
| `anthropic_api` | `ANTHROPIC_API_KEY` | Abrechnung pro Token |
| `openrouter` | `OPENROUTER_API_KEY` | Beliebige Modelle; Websuche über `:online` |
| `openai` | `OPENAI_API_KEY` | Für GPT-Codex-Modelle (Responses API) |
| `mock` | nichts | Testmodus, kostenlos, erzeugt Beispieldaten |

Modelle stellst du in `config.yaml` ein, auch **pro Rolle** (`role_models`). Der Inspektor und der Kritiker sollten das stärkste Modell bekommen. Die eingetragenen Modellnamen bitte vor dem ersten Lauf mit der aktuellen Modellliste des Anbieters abgleichen.

**Aufwand:** etwa 8–10 KI-Aufrufe pro Konzept (mit Visuals) (plus Überarbeitungen). Mathematik Klasse 1–10 mit einigen hundert Konzepten braucht also grob 1.500–3.000 Aufrufe. `pipeline.max_agent_calls` begrenzt einen Lauf. Zum Ausprobieren erst einen Block laufen lassen (`-b Brüche`).

---

## Datenbank für Karo – Stufe 1 und Stufe 2

Das Team arbeitet **immer in Postgres** (Inspektor, Prüfprotokoll, Warteschlange, Aufträge). Wie Karo an die Daten kommt, hängt von der Stufe ab:

| | Stufe 1 – Test (Karo-MVP, SQLite) | Stufe 2 – Produkt (Postgres EU) |
|---|---|---|
| Arbeitsdatenbank des Teams | lokaler Postgres-Container (Volume `pgdata`) | Supabase oder Neon, EU-Region |
| Was Karo liest | `curriculum.db` in Karos Volume – Kopie aller **freigegebenen** Inhalte | dieselbe Datenbank direkt (`karo.*`) |
| Einbinden in Karo | `ATTACH DATABASE '/data/curriculum.db' AS cur;` | Rollen `karo_reader` / `karo_app` |
| Adaptive Diagnose, Curriculum-Agent | nein – das MVP diagnostiziert selbst | ja (`start_diagnosis`, `resolve_topic` …) |
| Umbau an Karo | keiner | Postgres-Anschluss |

### Stufe 1 einrichten
In `.env`:
```
DATABASE_URL=postgresql://karo:karo@postgres:5432/karo
KARO_SQLITE_EXPORT=true
KARO_EXPORT_VOLUME=<Volume, das Karo unter /data einbindet>   # docker volume ls
```
Nach jedem Lauf (`kcteam run`) und jedem Auftrag des Curriculum-Agenten wird `curriculum.db` neu geschrieben – **atomar**: Karo sieht immer einen vollständigen Stand, auch wenn es die Datei gerade liest. Von Hand: `kcteam export-sqlite` (optional `-s Mathematik`).

Inhalt: `subjects`, `topic_blocks`, `concepts` (Niveaus, Kann-Aussagen, Parameter, Grenzaufgaben), `concept_prerequisites` (nur zwischen freigegebenen Konzepten; `missing_prerequisites` zählt, was noch fehlt), `misconceptions`, `items` (mit auswertbarer `answer` und `distractors`), `visual_explanations` (mit fertigem SVG), `items_for_child` (ohne Lösungen), Volltextsuche `concept_search` (Umlaute egal) und in `meta` die fertige Lernpfad-Abfrage:
```sql
SELECT id FROM cur.concept_search WHERE concept_search MATCH 'bruche addieren';
SELECT value FROM cur.meta WHERE key = 'learning_path_sql';   -- Parameter :targets, :mastered (JSON-Listen)
```
Keine Kinderdaten, keine Aufträge, kein Prüfprotokoll in der Datei.

### Stufe 2 einrichten
In `.env` `DATABASE_URL` auf die EU-Datenbank setzen, `KARO_SQLITE_EXPORT=false`, und ohne lokalen Postgres starten: `docker compose run --rm --no-deps kcteam`.
- **Supabase:** Session-Pooler (Port **5432**) oder direkte Verbindung – **nicht** den Transaktions-Pooler (6543). Dort funktionieren LISTEN/NOTIFY und die Fach-Sperre nicht.
- **Neon:** direkte Adresse **ohne** `-pooler`.
- Immer `?sslmode=require` anhängen. `kcteam` warnt beim Start, wenn etwas davon nicht stimmt.

Das Schema wird bei jedem Start automatisch angelegt bzw. aktualisiert.

- `curriculum.*` – Arbeitsdaten des Teams, Prüfprotokoll (`reviews`), Warteschlange für Menschen (`human_queue`), Kosten (`agent_calls`)
- `karo.*` – **die Schnittstelle für Karo**, nur freigegebene Inhalte
- `learner.*` – pseudonyme Diagnosedaten der Kinder (nur über Funktionen erreichbar)

**Rollen** (werden bei jedem Start angelegt; `PUBLIC` bekommt nichts):

| Rolle | Für | Darf |
|---|---|---|
| `karo_reader` | Karos Backend (Inhalte) | `karo.*` lesen inkl. Lösungen, `concept_bundle`, `learning_path`, `find_concepts` |
| `karo_app` | Karos Backend (Diagnose) | `start_diagnosis`, `next_step`, `record_response`, `item_for_child`, `forget_learner`, `resolve_topic`, `request_status` – **keine Lösungen** |
| `karo_reviewer` | Fachkraft / Bewertungsdienst | `review_response`, `record_external_result`, `purge_learner_data` |
| `karo_owner` | intern (ohne Login) | besitzt die SECURITY-DEFINER-Funktionen |

Ein Login bekommen die Rollen über `KARO_READER_PASSWORD`, `KARO_APP_PASSWORD`, `KARO_REVIEWER_PASSWORD`. Wird ein Passwort später gesetzt oder geändert, gilt es ab dem nächsten Start.

**Wichtig:** Diese Rollen sind für **Karos Server**, nie für das Gerät des Kindes. Die Datenbank prüft nicht, *welches* Kind anfragt – Karos Backend muss sicherstellen, dass `learner_ref` und `tenant` zum angemeldeten Kind bzw. zur Einrichtung gehören.

### Karo-Spec vom Volume

Beim Lauf wird Karos Spezifikation vom Volume gelesen (`KARO_SPEC_PATH`, Standard `/karo`, gemountet aus dem Volume `KARO_VOLUME`). Eine Datei oder alle `.md` in einem Ordner. Sie geht als Kontext an alle Agenten. Fehlt sie, läuft das Team trotzdem.

---

## Diagnostik – für jedes Ergebnis und jedes Niveau

Jede Diagnose- und Abschlussaufgabe hat eine **auswertbare Antwort** (Zahl, Bruch, Auswahl, Text, Reihenfolge, Zuordnung, Markieren; Freitext mit Bewertungsraster). Typische falsche Antworten sind einer **Fehlvorstellung** zugeordnet. Die Datenbank wertet selbst aus und steuert die Diagnose:

```sql
-- 1. Sitzung starten (Kind pseudonym, keine Namen)
SELECT karo.start_diagnosis('kind-4711', 'MA', 6, ARRAY['MA.BRUECHE.ADD_UNGL']);
-- 2. nächster Schritt: {"action":"ask", "item":{prompt, visual_svg, choices …}}  (ohne Lösung)
SELECT karo.next_step('<session>');
-- 3. Antwort speichern -> Auswertung + nächster Schritt
SELECT karo.record_response('<session>', 'MA.BRUECHE.ADD_UNGL.DIAG_2', '"12"');
-- ... bis {"action":"result"}: Lernplan
```

Antwortformate für `record_response`: Zahl/Text als JSON-String (`'"3/4"'`, `'"12,5 cm"'`), Auswahl als Index (`'2'`) oder `'{"index": 2}'`, Mehrfachauswahl als Liste (`'[0,2]'`), Freitext als `'{"text": "…"}'`, überspringen mit `'null'`. Dieselbe Antwort zweimal zu senden ist unschädlich.

Weitere Funktionen:
- `karo.review_response(response_id, 'correct'|'partial'|'incorrect', score, misconception_id)` – Freitext bewerten. Rückgabe `{updated, needs_followup, result}`; bei `needs_followup = true` ruft Karo wieder `next_step` auf.
- `karo.record_external_result(session, item, answer, outcome, …)` – Ergebnis eintragen, das außerhalb bewertet wurde.
- `karo.forget_learner(learner_ref, tenant)` – alle Daten eines Kindes löschen (Recht auf Löschung).
- `karo.purge_learner_data(tage)` – löscht Sitzungen, Lernstände und inaktive Kennungen älter als `retention_days` (in `curriculum.diagnostic_policy`); regelmäßig per Cron aufrufen.
- Wird ein Konzept während einer laufenden Sitzung zurückgezogen, springt die Diagnose automatisch zur nächsten freigegebenen Aufgabe.

**Was die Diagnose macht**
- Sie beginnt beim Ziel. Zwei richtige Aufgaben auf Zielniveau bedeuten: beherrscht. Dann ist sie fertig, und starke Kinder bekommen **Aufgaben über dem Ziel** und **Folgekonzepte**.
- Bei Fehlern prüft sie die **direkten Voraussetzungen**, dann deren Voraussetzungen, bei Bedarf **bis Klasse 1**. Konzepte, die das Kind beherrscht, beenden den Abstieg.
- **Fehlvorstellungen** werden erkannt (z. B. „2/5“ bei 1/2 + 1/3), und die passende Bildfolge wird mitgeliefert.
- **Jedes Ergebnis ist abgedeckt:** richtig, falsch, Fehlvorstellung, übersprungen, Freitext (`needs_review`, später mit `karo.review_response` bewerten). Auch fehlende oder nicht freigegebene Konzepte tauchen auf (`unavailable`).
- **Ergebnis:** Lernplan in Lernreihenfolge mit **Einstiegspunkten**, Zustand pro Konzept, Fehlvorstellungen, passenden Erklärungen, Hinweis „unter Klasse-1-Niveau“, nicht geprüfte Konzepte.
- **Lernstände werden gespeichert** (`learner.mastery`) und in der nächsten Sitzung übernommen.
- **Regeln anpassbar** in `curriculum.diagnostic_policy`, z. B. wie viele richtige Aufgaben „beherrscht“ bedeuten.

```bash
kcteam check -s Mathematik                     # sind alle Konzepte diagnostik-bereit?
kcteam simulate -c MA.BRUECHE.ADD_UNGL --child weak          # alles falsch -> bis ganz nach unten
kcteam simulate -c MA.BRUECHE.ADD_UNGL --child strong        # alles richtig -> sofort fertig
kcteam simulate -c MA.BRUECHE.ADD_UNGL --child misconception
kcteam simulate -c MA.BRUECHE.ADD_UNGL --child gap:MA.TEILBARKEIT.KGV   # genau eine Lücke
```

Datenschutz: In `learner.*` stehen nur eine pseudonyme Kennung, die Klasse, Antworten und Lernstände. Karo greift über die Funktionen im Schema `karo` darauf zu.

---

## Curriculum-Agent – wenn Karo nichts Passendes findet

Karo ruft für jedes Arbeitsblatt **eine** Funktion auf. Ob etwas passt, entscheidet der Curriculum-Agent, nicht Karo:

```sql
SELECT karo.resolve_topic(
  'Mathematik', 7, 'Prozentrechnung mit Rabatten',      -- Fach, Klasse des Kindes, Thema vom Arbeitsblatt
  ARRAY['Rabatt','Prozent'],                             -- Stichworte (optional)
  '["Ein Pullover kostet 40 € …", "20 % von 80 €"]',    -- Aufgaben vom Blatt als Text, OHNE Namen (optional, max. 10)
  'schule-123');                                         -- Einrichtung (für das Tageslimit)
```

| Antwort `status` | Bedeutung | Was Karo tut |
|---|---|---|
| `found` | passende Konzepte (sofort, ohne KI) | normal diagnostizieren |
| `other_level` | gibt es, aber in einer anderen Klasse; `level_hint` = `below` (mit den Einstiegsaufgaben arbeiten) oder `review` (Wiederholung) | Konzept nutzen |
| `ordered` | Auftrag angelegt (`request_id`); gleiche Anfragen werden zusammengelegt (`joined`) | mit `candidates` bzw. deren Voraussetzungen weiterarbeiten, später Stand abfragen |
| `unavailable` | Thema wurde kürzlich gesperrt/abgelehnt – wird nicht erneut beauftragt | bei vorhandenen Inhalten bleiben |
| `limit` | Tageslimit der Einrichtung erreicht | wie `unavailable` |

Stand abfragen: `SELECT karo.request_status(42);` → `queued` · `running` · **`ready`** (Konzepte sind freigegeben und nutzbar, Visuals folgen) · `done` · `blocked` · `rejected` · `failed`. Es werden nur freigegebene Konzepte zurückgegeben. Statt abzufragen kann Karo auf `LISTEN karo_topic_ready` hören (JSON mit `request_id`, `status`, `concepts`).

**Was der Agent tut** (Dienst: `docker compose --profile agent up -d`)
1. **Zuordnen** – ein KI-Aufruf entscheidet anhand der Aufgaben: vorhandenes Konzept (häufigster Fall; der Suchbegriff wird gelernt, damit die nächste Anfrage ohne KI gefunden wird), neues Konzept (+ höchstens zwei fehlende Voraussetzungen) oder kein Schulstoff. Neue Konzepte prüft der Kinderrechts-Inspektor – sein Veto gilt.
2. **Schnellspur** – Kalibrierung, Diagnostik, Schlussprüfung → `ready`. Das dauert typischerweise 1–2 Minuten; wartende Kinder haben Vorrang vor Stapelläufen.
3. **Vervollständigen** im Hintergrund (Konzept bleibt sichtbar): Visuals, Kritiker mit automatischer Nacharbeit, fehlende Voraussetzungen, Vorab-Auftrag für das wahrscheinlich nächste Thema → `done`.

**Schutz**
- Arbeitsblatt-Text ist fremder Inhalt: Er wird nur als Daten behandelt (nie als Anweisung), persönliche Daten werden zusätzlich entfernt, und die Aufgaben werden nach Abschluss gelöscht.
- Erzeugte Aufgaben dürfen nicht wörtlich vom Arbeitsblatt stammen (Urheberrecht) – das wird automatisch geprüft.
- Pro Fach arbeitet immer nur einer (Stapellauf *oder* Agent). Hängende Aufträge werden nach einem Absturz fortgesetzt.
- Gesperrte Aufträge landen als **dringend** in `review list`; `review approve` legt die Konzepte an und startet die Schnellspur, `review retry --note …` gibt der Zuordnung einen Hinweis.
- Schwellen, Tageslimit usw.: `curriculum.request_policy`. Welche Themen fehlen am häufigsten: `kcteam demand`.

```bash
kcteam request -s Mathematik -g 7 -t "Prozentrechnung" --task "20 % von 80 €"   # wie Karo anfragen
kcteam serve --once                                                           # Aufträge abarbeiten
kcteam requests                                                               # Stand
```

---

## Curriculum-Service über HTTP – so nutzt Karo den Agenten

Der Agent bleibt ein **eigenständiger Dienst**: Karo kennt weder seine Datenbank noch seine Agenten, nur eine kleine HTTP-API. Andere Apps können ihn genauso nutzen.

```
Karo ──HTTP──▶ curriculum-api (127.0.0.1:8088) ──▶ Postgres ◀── curriculum-agent (KI-Team)
                 Sofortsuche, fertige Lektionen          Aufträge, Lektionen schreiben
                 (nur Datenbank, ~5–10 ms)               (im Hintergrund, mit Inspektor)
```

Starten und Karo anmelden:
```bash
docker compose --profile agent up -d                        # API + Agent
docker compose run --rm kcteam api-client add --name karo   # Schlüssel kc_… (wird nur einmal angezeigt)
```
In Karo: `KARO_CURRICULUM_KEY=kc_…` in `.env`, dann mit `docker-compose.curriculum.yml` starten (siehe Karos README). Karo erreicht den Dienst im Docker-Netz `karo-net` als `http://curriculum-api:8088`.

| Aufruf | Zweck |
|---|---|
| `POST /v1/lessons` | **Lektion im Format des Abnehmers.** Karo schickt Fach, Klasse, Thema und sein Format (JSON-Schema, Komponentenregister, Formatregeln). Antwort `ready` + Lektion · `202 pending` + `retry_after` · `unavailable` + Grund |
| `GET /v1/lessons/{id}` | Stand einer Lektion |
| `POST /v1/lessons/{id}/reject` | Abnehmer verwirft die Lektion (eigene Prüfung) → wird mit dem Grund neu geschrieben; nach `KCTEAM_MAX_CLIENT_REJECTS` an einen Menschen |
| `POST /v1/resolve` | Thema suchen/bestellen (wie `karo.resolve_topic`), optional mit Konzept-Bundle |
| `GET /v1/requests/{id}` · `GET /v1/concepts/{id}` (ETag) · `POST /v1/learning-path` | Stand, Konzept, Lernpfad |

Doku zum Ausprobieren: `http://127.0.0.1:8088/docs`. Anmeldung mit `Authorization: Bearer kc_…`; der Schlüssel bestimmt die Einrichtung (Tageslimit, Nachfrage). POST-Aufrufe dürfen `Idempotency-Key` mitschicken.

**Lektionen im fremden Format.** Der **Lektionsautor** schreibt die Lektion auf Grundlage des geprüften Konzepts (Niveaus, Fehlvorstellungen mit bekannten falschen Antworten, Beispielaufgaben, Voraussetzungen) genau im mitgeschickten Schema. Geprüft wird automatisch: Schema, kein HTML/SVG/Skript, nur Komponenten und Parameter aus dem Register – dann das Veto des Kinderrechts-Inspektors. Die Lektion wird pro *Konzeptversion × Klasse × Format* vorgehalten: das nächste Kind bekommt sie ohne KI-Aufruf. Ist das Thema neu, wartet die Lektion auf den Auftrag und wird geschrieben, sobald das Konzept freigegeben ist – noch vor dem Vervollständigen im Hintergrund.

**Latenz** (gemessen, 2 CPU-Kerne): Sofortsuche p50 4 ms / p95 6 ms nacheinander, p95 ≈ 40 ms bei 10 gleichzeitigen Anfragen mit 4 Prozessen; fertige Lektion aus Sicht von Karo ≈ 9 ms. `kcteam bench` misst das bei dir. Mehr Abonnenten: `KCTEAM_API_WORKERS` erhöhen, der Agent skaliert getrennt davon.

**Webhooks** (für andere Abnehmer; Karo fragt im Job-Takt nach): `kcteam api-client add --name app --webhook https://…` – Ereignisse `lesson.finished` und `request.updated`, signiert mit `X-Curriculum-Signature: t=…,v1=<HMAC-SHA256>` (Prüfen: `kcteam.webhooks.verify`).

```bash
kcteam api                          # API ohne Docker (127.0.0.1:8088)
kcteam api-client list | revoke --name karo
kcteam bench -n 500 -c 10           # Latenz messen
```

---

## Datenbank-Browser (nur lesen)

Eine kleine Oberfläche für **beide** Datenbanken: das Curriculum (Postgres) und Karos Katalog (SQLite).

```bash
# ADMIN_PASSWORD in .env setzen, dann
docker compose --profile admin up -d      # http://127.0.0.1:8090  (Benutzer: admin)
```

- **Curriculum:** Fächer → Themenblöcke → Konzepte (Suche, Filter nach Stand), Konzeptseite mit Voraussetzungen, Fehlvorstellungen, Bild-Erklärungen, Aufgaben und Prüfprotokoll; Aufträge, Lektionen für Abnehmer, offene menschliche Prüfungen (dringende zuerst), Nachfrage, Abnehmer.
- **Karo:** Lernkatalog mit Herkunft (`curriculum` = vom Dienst geliefert), Konzeptseite mit Fehlertypen, erkennbaren falschen Antworten, Aufgaben und Hilfe.
- **Alle Tabellen** beider Datenbanken mit Suche und Seiten.

Sicherheit: startet nicht ohne Passwort, nur auf 127.0.0.1, Postgres-Sitzungen `READ ONLY`, Karos Datei `mode=ro` und schreibgeschützt gemountet, Spalten mit Geheimnissen (Passwort, Token, Schlüssel-Hash …) werden nie angezeigt, keine externen Skripte (strenge CSP). Entscheidungen der menschlichen Prüfung bleiben bewusst im Terminal (`kcteam review …`).

---

## So nutzt Karo die Daten

**1. Arbeitsblatt → Konzepte finden** (Fach + Klasse + Stichwort aus dem Arbeitsblatt):
```sql
SELECT * FROM karo.find_concepts('MA', 6, 'Brüche');
```

**2. Lernpfad berechnen** – vom Ziel rückwärts bis zu dem, was das Kind schon kann (Ergebnis des Diagnosetests):
```sql
SELECT * FROM karo.learning_path(
  ARRAY['MA.BRUECHE.ADD_UNGL'],                        -- Ziel(e) vom Arbeitsblatt
  ARRAY['MA.BRUECHE.BEGRIFF','MA.ZAHLEN.EINMALEINS']   -- schon beherrscht
);
-- concept_id | title | target_grade | depth | available
-- Reihenfolge = Lernreihenfolge (tiefste Voraussetzung zuerst, Ziel zuletzt)
-- available = false: Konzept noch nicht freigegeben -> Lücke im Pfad
```

**3. Alles zu einem Konzept** (Niveaus, Grenzen, Fehlvorstellungen, Aufgaben, Visuals) als ein JSON:
```sql
SELECT karo.concept_bundle('MA.BRUECHE.ADD_UNGL');
```

**4. Karos Schleife**
- Diagnose: `items` mit `kind IN ('diagnostic','misconception')` – zeigen, ob das Kind tiefer einsteigen muss und welche Fehlvorstellung vorliegt.
- Erklärung zur Fehlvorstellung: `misconceptions.remediation_hint` und die passende Bildfolge aus `karo.visual_explanations` (`misconception_id`)
- Visuelle Aufgaben: `karo.items` mit `is_visual = true` (SVG in `visual_svg`, Daten in `visual`, Art der Interaktion in `interaction`)
- Neue Übungsaufgaben live erzeugen: `difficulty_parameters` + `boundary_items` (`within` = schwierigste erlaubte, `above` = schon zu schwer) als Leitplanken an den Generator geben.
- Abschluss: `items` mit `kind = 'exit'` – immer genau auf Zielniveau.

Wichtig: Inhalte, die Karo **zur Laufzeit** erzeugt, sollten ebenfalls durch eine schlanke Inspektor-Prüfung gehen. Der Prompt dafür liegt in `kcteam/prompts/kinderrechts_inspektor.md`.

Als Datei-Alternative: `kcteam export -s Mathematik` schreibt pro Themenblock eine JSON-Datei mit denselben Bundles.

### IDs

`FACH.BLOCK.KONZEPT`, z. B. `MA.BRUECHE.ADD_UNGL`. Aufgaben: `…ANCHOR_1`, `…BOUNDARY_ABOVE_1`, `…DIAG_2`, `…F1.DIAG`, `…EXIT_3`. Die IDs bleiben über Läufe stabil. Ändert sich ein Konzept inhaltlich, steigt `version`.

---

## Tests

```bash
TEST_DATABASE_URL=postgresql://user@localhost:5432/testdb pytest -q
```
Achtung: Die Tests löschen die Schemas `curriculum`, `karo` und `learner` in dieser Datenbank. Sie decken den ganzen Ablauf mit dem Mock-Provider ab: Veto mit Korrektur, Sperre nach 3 Runden, menschliche Freigabe, erneuter Lauf ohne Mehrkosten, Lernpfad, Nur-Lese-Rolle, Export, alle Visual-Typen, SVG-Sicherheit, Klassenauswahl, Nachrüsten von Visuals, alle Fachprofile, Antwortauswertung und simulierte Kinder auf allen Niveaus (stark, schwach, Fehlvorstellung, einzelne Lücke, Überspringen, Freitext, Wiederkehr), Rollen und Löschung, hängende Sitzungen, Schutz freigegebener Konzepte bei Kritik und Neustrukturierung, Kritiker-Ausfall, abbrechbare Wartezeiten sowie der Curriculum-Agent (finden, anderes Niveau, beauftragen, zusammenlegen, auch gleichzeitig, Veto und menschliche Freigabe, Kopierschutz, Datenschutz, Limit, Wiederaufnahme nach Absturz) die SQLite-Kopie für das Karo-MVP, die HTTP-API (Schlüssel, Idempotenz, Lektionen im Karo-Format inkl. Veto, Wartezustand und Verwerfen durch den Abnehmer, Webhook-Signatur) und der Datenbank-Browser (Passwort, nur lesen, Geheimnisse ausgeblendet).

## Anpassen

- Rollen-Prompts: `kcteam/prompts/*.md`, der gemeinsame Rahmen steht in `_common.md`
- Grenzen und Parallelität: `config.yaml` → `pipeline` (u. a. `provider_retries`, `max_retry_wait`, `critic_chunk`)
- Datenmodell: `kcteam/schemas.py` (Agenten-Ausgaben), `kcteam/sql/*.sql` (Datenbank)

## Hinweise

- Noch nicht im Visual-Katalog: **Karten** (Erdkunde, Geschichte), **Notensystem** (Musik). Bis dahin arbeiten die Teams schematisch. Audio (Hörverstehen, Aussprache) wird nicht erzeugt.

- **Freigegebene Inhalte bleiben sichtbar.** Kritik oder `review retry` an einem freigegebenen Konzept führt zu einer Überarbeitung im Hintergrund; übernommen wird sie erst, wenn Inspektor und Schlussprüfung zustimmen. Strukturänderungen an einem Block behalten freigegebene Konzepte bei.
- Bei einem Rate-Limit pausieren alle Worker gemeinsam; Strg+C beendet auch laufende Wartezeiten sofort.
- Die fachliche und rechtliche Qualität hängt vom Modell ab. Eine pädagogische Fachkraft sollte Stichproben prüfen (`curriculum.reviews` enthält jede Entscheidung).
- Der Inspektor prüft **Inhalte**. Die rechtliche Prüfung der App selbst (Einwilligung der Eltern, DSGVO, Umgang mit hochgeladenen Arbeitsblättern) ersetzt er nicht.
