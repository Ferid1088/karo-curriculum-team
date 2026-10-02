"""Request-Korrelation: der Dienst nimmt Karos X-Request-Id an.

Braucht keine Datenbank — die Middleware sitzt aussen und beantwortet auch
die 503 des /health-Endpunkts ohne Datenbankverbindung mit ihrer ID.
"""
from __future__ import annotations

import logging

from fastapi.testclient import TestClient


def _api():
    from kcteam.api import create_app

    return TestClient(create_app())


def test_fremde_request_id_wird_uebernommen_und_gespiegelt():
    api = _api()
    r = api.get("/health", headers={"x-request-id": "job-000000007"})
    assert r.headers["x-request-id"] == "job-000000007"


def test_ohne_header_entsteht_eine_neue_id():
    api = _api()
    eins = api.get("/health").headers["x-request-id"]
    zwei = api.get("/health").headers["x-request-id"]
    assert eins and zwei and eins != zwei


def test_verdaechtige_id_wird_ersetzt():
    api = _api()
    r = api.get("/health", headers={"x-request-id": 'bad "id x'})
    rid = r.headers["x-request-id"]
    assert rid != 'bad "id x' and len(rid) == 16


def test_request_id_steht_in_der_logzeile():
    api = _api()

    eintraege = []

    class Sammler(logging.Handler):
        def emit(self, record):
            eintraege.append(record)

    logger = logging.getLogger("kcteam.http")
    sammler = Sammler()
    logger.addHandler(sammler)
    try:
        api.post("/v1/resolve", headers={"x-request-id": "karo-suche-42"},
                 json={})
    finally:
        logger.removeHandler(sammler)

    abgeschlossen = [e for e in eintraege
                     if "request_completed" in e.getMessage()
                     or "request_failed" in e.getMessage()]
    assert abgeschlossen
    assert all(e.request_id == "karo-suche-42" for e in abgeschlossen)


def test_fehler_wird_mit_id_geloggt():
    api = _api()

    async def kaputt(request):
        raise RuntimeError("absichtlich")

    api.app.add_route("/sonde-kaputt", kaputt)
    api.raise_server_exceptions = False

    eintraege = []

    class Sammler(logging.Handler):
        def emit(self, record):
            eintraege.append(record)

    logger = logging.getLogger("kcteam.http")
    sammler = Sammler()
    logger.addHandler(sammler)
    try:
        r = api.get("/sonde-kaputt", headers={"x-request-id": "karo-fehler-9"})
    finally:
        logger.removeHandler(sammler)

    assert r.status_code == 500
    assert r.headers["x-request-id"] == "karo-fehler-9"
    fehl = [e for e in eintraege if "request_failed" in e.getMessage()]
    assert len(fehl) == 1
    assert fehl[0].request_id == "karo-fehler-9"
    assert fehl[0].exc_info and fehl[0].exc_info[0] is RuntimeError
