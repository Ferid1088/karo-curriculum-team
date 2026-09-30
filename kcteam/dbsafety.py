"""Schutz vor versehentlicher Zerstörung einer Datenbank.

Anlass: Eine Testsuite, die im Fixture `DROP SCHEMA … CASCADE` ausführt,
wurde über `TEST_DATABASE_URL` auf die laufende Entwicklungsdatenbank
gerichtet. Ein Tippfehler in einer Umgebungsvariablen genügte, um die
Konzeptlandkarte zu löschen. Eine Warnung hätte niemand gelesen; deshalb
bricht hier alles hart ab.

Vier Bedingungen, die **gleichzeitig** erfüllt sein müssen, bevor
irgendetwas zerstörerisch werden darf:

=====================================  =========================================
``APP_ENV=test``                       die Absicht der Umgebung
Datenbankname endet auf ``_test``      **nicht** aus der URL, sondern aus
                                       ``SELECT current_database()`` — eine URL
                                       kann auf etwas anderes zeigen, als sie
                                       verspricht (Pooler, Alias, ``search_path``)
``system_identity.environment='test'`` die Datenbank selbst sagt, was sie ist;
                                       diese Zeile steht in ``public`` und
                                       überlebt jedes Schema-Löschen
``ALLOW_DESTRUCTIVE_DB_RESET=YES``     der bewusste Griff zum Schalter
=====================================  =========================================

Fehlt eine davon: Abbruch mit `DestructiveOperationRefused`. Kein Fallback,
keine Rückfrage, kein "nur diesmal".

Die Schichten sind absichtlich redundant. Jede einzelne hätte den Unfall
verhindert, und jede kann für sich genommen falsch konfiguriert sein.
"""
from __future__ import annotations

import os
import re

#: Nur ein Datenbankname mit dieser Endung gilt als Testdatenbank.
TEST_SUFFIX = "_test"
#: Der Schalter, ohne den nichts Zerstörerisches läuft.
KILL_SWITCH = "ALLOW_DESTRUCTIVE_DB_RESET"
KILL_SWITCH_VALUE = "YES"
#: Tabelle in `public`: sie überlebt `DROP SCHEMA curriculum CASCADE` und ist
#: damit die einzige Aussage, die nach einem Reset noch trägt.
IDENTITY_TABLE = "public.system_identity"

IDENTITY_DDL = """
CREATE TABLE IF NOT EXISTS public.system_identity (
    id              integer PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    environment     text NOT NULL CHECK (environment IN ('test', 'dev', 'production')),
    database_role   text NOT NULL,
    stamped_at      timestamptz NOT NULL DEFAULT now(),
    note            text
)
"""


class DestructiveOperationRefused(RuntimeError):
    """Eine zerstörerische Operation wurde abgelehnt. Immer ein harter Fehler."""


def _fehler(grund: str, *, datenbank: str | None = None) -> DestructiveOperationRefused:
    wo = f" (verbunden mit „{datenbank}“)" if datenbank else ""
    return DestructiveOperationRefused(
        f"Zerstörerische Datenbankoperation abgelehnt{wo}: {grund}\n"
        f"Erlaubt nur, wenn ALLE vier Bedingungen zutreffen: APP_ENV=test · "
        f"Datenbankname endet auf „{TEST_SUFFIX}“ · {IDENTITY_TABLE}.environment='test' · "
        f"{KILL_SWITCH}={KILL_SWITCH_VALUE}."
    )


# --------------------------------------------------------------------------
# Schicht 1: die Zeichenkette, bevor überhaupt verbunden wird
# --------------------------------------------------------------------------

def database_name_from_url(url: str) -> str:
    """Der Datenbankname aus einer Verbindungszeichenkette — ohne Verbindung.

    Nur für die erste Absage. Verbindlich ist immer `current_database()`:
    eine URL kann lügen, eine offene Verbindung nicht.
    """
    if not url:
        return ""
    ohne_query = url.split("?", 1)[0].split("#", 1)[0]
    treffer = re.search(r"/([^/]*)$", ohne_query)
    return treffer.group(1) if treffer else ""


def looks_like_test_database(name: str) -> bool:
    return bool(name) and name.lower().endswith(TEST_SUFFIX)


def assert_test_url(url: str, *, zweck: str = "Tests") -> str:
    """Bricht ab, wenn diese URL nicht eindeutig auf eine Testdatenbank zeigt.

    Läuft vor dem Verbindungsaufbau, damit eine falsch gesetzte Variable gar
    nicht erst eine Sitzung auf der falschen Datenbank öffnet.
    """
    name = database_name_from_url(url)
    if not looks_like_test_database(name):
        raise _fehler(
            f"{zweck} dürfen nur gegen eine Testdatenbank laufen. Der Name „{name or '(leer)'}“ "
            f"endet nicht auf „{TEST_SUFFIX}“.", datenbank=name or None)
    return url


# --------------------------------------------------------------------------
# Schicht 2 bis 4: die offene Verbindung, die Identität, der Schalter
# --------------------------------------------------------------------------

def app_env() -> str:
    return (os.environ.get("APP_ENV") or "").strip().lower()


def kill_switch_set() -> bool:
    return (os.environ.get(KILL_SWITCH) or "").strip().upper() == KILL_SWITCH_VALUE


def current_database(db) -> str:
    """Die Datenbank, mit der wirklich gesprochen wird."""
    zeile = db.query("SELECT current_database() AS name")
    return (zeile[0]["name"] if zeile else "") or ""


def read_identity(db) -> dict | None:
    """Die Selbstauskunft der Datenbank — oder None, wenn sie nie gestempelt wurde."""
    vorhanden = db.query(
        "SELECT to_regclass(%s) IS NOT NULL AS da", (IDENTITY_TABLE,))
    if not vorhanden or not vorhanden[0]["da"]:
        return None
    zeilen = db.query(
        f"SELECT environment, database_role, stamped_at, note FROM {IDENTITY_TABLE} WHERE id = 1")
    return dict(zeilen[0]) if zeilen else None


def stamp_identity(db, environment: str, database_role: str, note: str | None = None) -> None:
    """Schreibt die Selbstauskunft. Nur der sichere Reset-Pfad ruft das auf."""
    db.query(IDENTITY_DDL)
    db.query(f"""INSERT INTO {IDENTITY_TABLE} (id, environment, database_role, note)
                 VALUES (1, %s, %s, %s)
                 ON CONFLICT (id) DO UPDATE SET environment = EXCLUDED.environment,
                     database_role = EXCLUDED.database_role, note = EXCLUDED.note,
                     stamped_at = now()""", (environment, database_role, note))


def assert_destructive_allowed(db, *, operation: str = "Datenbank-Reset") -> str:
    """Alle vier Schichten. Gibt den Namen der Datenbank zurück oder bricht ab.

    Reihenfolge mit Absicht: erst die billigen Prüfungen aus der Umgebung,
    dann die teure über die offene Verbindung. Wer den Schalter vergisst,
    soll das erfahren, bevor irgendetwas die Datenbank anfasst.
    """
    if app_env() != "test":
        raise _fehler(f"{operation} verlangt APP_ENV=test (gesetzt: „{app_env() or '(leer)'}“).")
    if not kill_switch_set():
        raise _fehler(f"{operation} verlangt {KILL_SWITCH}={KILL_SWITCH_VALUE}.")

    name = current_database(db)
    if not looks_like_test_database(name):
        raise _fehler(
            f"die tatsächlich verbundene Datenbank heißt „{name}“ und endet nicht auf "
            f"„{TEST_SUFFIX}“.", datenbank=name)

    identitaet = read_identity(db)
    if identitaet is None:
        raise _fehler(
            f"in dieser Datenbank fehlt {IDENTITY_TABLE}. Eine Datenbank ohne Selbstauskunft "
            f"gilt nie als Testdatenbank — eine frische Testdatenbank legt "
            f"`python -m tools.reset_test_db --create` an.", datenbank=name)
    if (identitaet.get("environment") or "").lower() != "test":
        raise _fehler(
            f"{IDENTITY_TABLE}.environment ist „{identitaet.get('environment')}“, nicht „test“.",
            datenbank=name)
    return name


def guard_pytest_url(url: str) -> str:
    """Für conftest.py: dieselbe Absage, aber mit passender Erklärung."""
    return assert_test_url(url, zweck="pytest")
