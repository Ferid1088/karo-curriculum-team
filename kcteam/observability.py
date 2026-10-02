"""Request-Korrelation fuer den Curriculum-Service.

Karo schickt auf jedem Aufruf `X-Request-Id` mit — bei Anfragen die ID der
laufenden HTTP-Anfrage, bei Hintergrundarbeit `job-<id>`. Wer den Header
hier annimmt, in der Antwort zurueckspiegelt und auf jede Logzeile der
Anfrage setzt, kann einen Vorgang ueber beide Dienste verfolgen:

    docker logs karo           →  "request_id": "abc…"   (JSON-Zeile)
    docker logs curriculum-api →  request_id=abc…        (Textzeile)

Fremde IDs werden nur uebernommen, wenn sie harmlos aussehen — sie landen
in Headern und Logzeilen, ein beliebiger String waere eine Injektionsflaeche.
"""
from __future__ import annotations

import contextvars
import logging
import re
import time
import uuid

from starlette.responses import JSONResponse

_MUSTER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{7,62}$")

request_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "kcteam_request_id", default="")

log = logging.getLogger("kcteam.http")

#: Gesundheitspruefungen jede halbe Minute — sie bekommen ihre ID wie jede
#: andere Anfrage (Antwort-Header), aber keine eigene Abschlusszeile.
_LEISE = ("/health",)


class _Filter(logging.Filter):
    """Setzt die laufende request_id auf jeden Logdatensatz."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id.get() or "-"
        return True


def install() -> None:
    """Ein Handler fuer den Dienst: Zeitstempel, Logger, request_id.

    Uvicorn konfiguriert nur seine eigenen Logger — die kcteam-Module
    landeten bisher unformatiert und ohne Korrelation auf stderr. Ein
    einziger Root-Handler aendert daran nur das Aussehen, nicht die
    Lautstaerke (root bleibt auf WARNING). Die Abschlusszeile geht ueber
    `kcteam.http` mit eigenem Handler, damit sie auf INFO sichtbar ist.
    """
    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s request_id=%(request_id)s %(message)s")
    filt = _Filter()

    root = logging.getLogger()
    if not root.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(fmt)
        handler.addFilter(filt)
        root.addHandler(handler)

    if not log.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(fmt)
        handler.addFilter(filt)
        log.addHandler(handler)
        log.setLevel(logging.INFO)
        log.propagate = False


class RequestObservability:
    """ASGI-Middleware: eine ID pro Anfrage, im Kontext und im Header.

    Ein ungefangener Fehler wird genau einmal geloggt — hier, mit der
    request_id — und als schlichtes 500-JSON beantwortet, statt erst durch
    die ServerErrorMiddleware noch einmal ohne Kontext zu laufen.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        eingehend = None
        for name, wert in scope.get("headers", []):
            if name == b"x-request-id":
                eingehend = wert.decode("latin-1")
                break
        rid = eingehend if eingehend and _MUSTER.match(eingehend) \
            else uuid.uuid4().hex[:16]
        token = request_id.set(rid)
        start = time.monotonic()
        status = [500]
        begonnen = [False]

        async def sende(message):
            if message["type"] == "http.response.start":
                begonnen[0] = True
                status[0] = message["status"]
                message["headers"] = list(message.get("headers", [])) + [
                    (b"x-request-id", rid.encode())]
            await send(message)

        pfad = scope.get("path", "")
        try:
            await self.app(scope, receive, sende)
        except Exception as exc:
            self._logge("request_failed", scope.get("method", ""), pfad,
                        status[0], start, exc)
            if begonnen[0]:
                raise      # Antwort laeuft schon — nur noch melden
            try:
                await JSONResponse({"detail": "interner Fehler"},
                                   status_code=500)(scope, receive, sende)
            except Exception:
                pass
        else:
            if not pfad.startswith(_LEISE):
                self._logge("request_completed", scope.get("method", ""),
                            pfad, status[0], start)
        finally:
            request_id.reset(token)

    @staticmethod
    def _logge(event: str, methode: str, pfad: str, status: int,
               start: float, exc: Exception | None = None) -> None:
        """Loggen darf die Anfrage nie brechen — ein defekter Logger schweigt."""
        try:
            schreiber = log.error if exc else log.info
            dauer = round((time.monotonic() - start) * 1000, 1)
            schreiber("%s %s %s -> %s %.1fms", event, methode, pfad, status,
                      dauer, exc_info=exc)
        except Exception:
            pass
