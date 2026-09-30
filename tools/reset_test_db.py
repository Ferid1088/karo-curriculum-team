"""Der einzige Weg, eine Testdatenbank vollständig zurückzusetzen.

    python -m tools.reset_test_db            # bestehende Testdatenbank leeren
    python -m tools.reset_test_db --create   # Testdatenbank anlegen und stempeln
    python -m tools.reset_test_db --no-backup

Vorher laufen alle vier Prüfungen aus `kcteam.dbsafety`. Es gibt keinen
zweiten Pfad: Testfixtures rufen `reset_schemas()` aus dieser Datei auf,
niemand schreibt `DROP SCHEMA` noch einmal selbst hin.

Vor dem Löschen entsteht ein `pg_dump` unter `backups/`. Fehlt das Verzeichnis,
wird es angelegt; fehlt `pg_dump`, läuft der Reset trotzdem — ein Backup ist
eine Hilfe, keine Bedingung. Die Bedingungen stehen oben.
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import shutil
import subprocess
import sys
from pathlib import Path

from kcteam import dbsafety
from kcteam.db import DB

#: Diese Schemata legt `DB.migrate()` an — und nur diese darf der Reset löschen.
SCHEMAS = ("karo", "curriculum", "learner")
BACKUP_DIR = Path(os.environ.get("KCTEAM_BACKUP_DIR") or "backups")


def _url() -> str:
    url = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL") or ""
    if not url:
        raise SystemExit("TEST_DATABASE_URL ist nicht gesetzt.")
    return url


def backup(url: str, name: str) -> Path | None:
    """pg_dump vor dem Löschen. Scheitert nie den Reset."""
    if not shutil.which("pg_dump"):
        print("… pg_dump nicht gefunden — Reset läuft ohne Sicherung weiter.")
        return None
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    ziel = BACKUP_DIR / f"{name}-{dt.datetime.now():%Y%m%d-%H%M%S}.sql"
    try:
        with ziel.open("wb") as datei:
            subprocess.run(["pg_dump", "--no-owner", "--no-privileges", url],
                           stdout=datei, stderr=subprocess.PIPE, check=True, timeout=300)
    except (subprocess.SubprocessError, OSError) as exc:
        print(f"… Sicherung fehlgeschlagen ({exc}) — Reset läuft weiter.")
        ziel.unlink(missing_ok=True)
        return None
    print(f"✓ Sicherung: {ziel}")
    return ziel


def reset_schemas(db, *, mit_backup: bool = True, url: str | None = None) -> str:
    """Leert die Testdatenbank — nach allen Prüfungen aus `dbsafety`.

    Das ist die Funktion, die auch die Testfixtures benutzen. Sie ist der
    einzige Ort im Projekt, an dem noch `DROP SCHEMA … CASCADE` steht.
    """
    name = dbsafety.assert_destructive_allowed(db, operation="Reset der Testdatenbank")
    identitaet = dbsafety.read_identity(db) or {}
    if mit_backup and url:
        backup(url, name)
    db.query("; ".join(f"DROP SCHEMA IF EXISTS {s} CASCADE" for s in SCHEMAS))
    db.migrate()
    # Der Stempel steht in `public` und überlebt das Löschen — hier nur
    # auffrischen, falls jemand ihn versehentlich mitgelöscht hat.
    dbsafety.stamp_identity(db, "test", identitaet.get("database_role") or "curriculum",
                            note="zurückgesetzt durch tools.reset_test_db")
    return name


def create_and_stamp(url: str) -> str:
    """Legt eine frische Testdatenbank an und stempelt sie.

    Nur hier darf eine Datenbank ohne Selbstauskunft angefasst werden — und
    auch das nur, wenn sie leer ist. Eine Datenbank mit Inhalt wird nie
    nachträglich zur Testdatenbank erklärt.
    """
    if dbsafety.app_env() != "test":
        raise dbsafety.DestructiveOperationRefused("--create verlangt APP_ENV=test.")
    if not dbsafety.kill_switch_set():
        raise dbsafety.DestructiveOperationRefused(
            f"--create verlangt {dbsafety.KILL_SWITCH}={dbsafety.KILL_SWITCH_VALUE}.")
    dbsafety.assert_test_url(url, zweck="--create")

    db = DB(url)
    name = dbsafety.current_database(db)
    if not dbsafety.looks_like_test_database(name):
        raise dbsafety.DestructiveOperationRefused(
            f"verbunden mit „{name}“ — das ist keine Testdatenbank.")
    if dbsafety.read_identity(db) is None:
        belegt = db.query(
            "SELECT count(*) AS n FROM information_schema.schemata WHERE schema_name = ANY(%s)",
            (list(SCHEMAS),))
        if belegt and belegt[0]["n"]:
            raise dbsafety.DestructiveOperationRefused(
                f"„{name}“ enthält bereits Curriculum-Schemata, trägt aber keine "
                f"{dbsafety.IDENTITY_TABLE}. Eine gewachsene Datenbank wird nicht "
                f"nachträglich zur Testdatenbank erklärt.")
        dbsafety.stamp_identity(db, "test", "curriculum",
                                note="angelegt durch tools.reset_test_db --create")
        print(f"✓ „{name}“ als Testdatenbank gestempelt.")
    db.migrate()
    return name


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Setzt ausschließlich eine Testdatenbank zurück.")
    p.add_argument("--create", action="store_true",
                   help="leere Datenbank als Testdatenbank stempeln und migrieren")
    p.add_argument("--no-backup", action="store_true", help="ohne pg_dump")
    args = p.parse_args(argv)

    url = _url()
    try:
        if args.create:
            name = create_and_stamp(url)
        else:
            dbsafety.assert_test_url(url, zweck="Reset")
            name = reset_schemas(DB(url), mit_backup=not args.no_backup, url=url)
    except dbsafety.DestructiveOperationRefused as exc:
        print(f"\nABBRUCH\n{exc}\n", file=sys.stderr)
        return 2
    print(f"✓ Testdatenbank „{name}“ steht bereit.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
