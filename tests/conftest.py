"""Sperre für die gesamte Testsuite: niemals gegen eine Nicht-Testdatenbank.

Diese Datei lädt pytest vor jedem Test. Zeigt `TEST_DATABASE_URL` auf etwas,
das nicht eindeutig eine Testdatenbank ist, bricht die Sammlung ab — ohne
Test, ohne Verbindung, ohne Warnung, die jemand überliest.

Anlass war ein realer Schaden: `TEST_DATABASE_URL` zeigte auf die laufende
Entwicklungsdatenbank, die Fixtures löschten dort die Schemata.
"""
from __future__ import annotations

import os

import pytest

from kcteam import dbsafety

URL = os.environ.get("TEST_DATABASE_URL")


def pytest_configure(config):
    """Vor dem ersten Test: entweder eine echte Testdatenbank oder gar keine."""
    if URL:
        try:
            dbsafety.guard_pytest_url(URL)
        except dbsafety.DestructiveOperationRefused as exc:
            # UsageError statt roher Ausnahme: pytest bricht genauso hart ab,
            # zeigt aber die Begründung statt eines INTERNALERROR-Stapels.
            raise pytest.UsageError(str(exc)) from None
        # Die Fixtures löschen Schemata. Die Erlaubnis dafür setzt sich die
        # Testsuite NICHT selbst: ein Schalter, den der Prozess bei Bedarf
        # umlegt, ist keiner. Wer Datenbanktests fährt, setzt beides bewusst
        # in seiner Umgebung — sonst läuft hier nichts Zerstörerisches.
        fehlend = [name for name, wert in (
            ("APP_ENV=test", dbsafety.app_env() == "test"),
            (f"{dbsafety.KILL_SWITCH}={dbsafety.KILL_SWITCH_VALUE}", dbsafety.kill_switch_set()),
        ) if not wert]
        if fehlend:
            raise pytest.UsageError(
                "Datenbanktests leeren Schemata. Dafür fehlt: " + ", ".join(fehlend) + ".\n"
                "Bewusst setzen, zum Beispiel:\n"
                f"  APP_ENV=test {dbsafety.KILL_SWITCH}={dbsafety.KILL_SWITCH_VALUE} pytest")


@pytest.fixture(scope="session")
def test_database_url() -> str:
    if not URL:
        pytest.skip("TEST_DATABASE_URL nicht gesetzt")
    return URL


@pytest.fixture(scope="session")
def fresh_db(test_database_url):
    """Eine geleerte, migrierte Testdatenbank — über den einzigen sicheren Pfad."""
    from kcteam.db import DB
    from tools.reset_test_db import reset_schemas

    db = DB(test_database_url)
    reset_schemas(db, mit_backup=False)
    return db
