"""Webhooks: meldet Abnehmern, wenn ein Auftrag oder eine Lektion fertig ist (statt Nachfragen).

Signatur wie bei Stripe: `X-Curriculum-Signature: t=<unix>,v1=<hex hmac_sha256(secret, f"{t}.{body}")>`.
Der Abnehmer prüft sie mit `verify()` und verwirft Nachrichten, die älter als 5 Minuten sind.
Karo braucht das nicht (sein Job-Takt fragt ohnehin alle paar Sekunden nach) – andere Abnehmer schon.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import threading
import time
from typing import Callable

import httpx
import psycopg
from psycopg.types.json import Jsonb

RETRIES = (0, 5, 30, 120)


def sign(secret: str, body: bytes, t: int | None = None) -> str:
    t = int(time.time()) if t is None else t
    mac = hmac.new(secret.encode(), f"{t}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={t},v1={mac}"


def verify(secret: str, body: bytes, header: str, tolerance: int = 300) -> bool:
    try:
        parts = dict(p.split("=", 1) for p in header.split(","))
        t = int(parts["t"])
    except (ValueError, KeyError):
        return False
    if abs(time.time() - t) > tolerance:
        return False
    return hmac.compare_digest(sign(secret, body, t), header)


class Dispatcher:
    """Hört auf die NOTIFY-Kanäle und liefert an die eingetragenen Abnehmer aus (eigener Thread)."""

    def __init__(self, db, log: Callable[[str], None] = print, client: httpx.Client | None = None):
        self.db, self.log = db, log
        self.http = client or httpx.Client(timeout=5.0)
        self.stop = threading.Event()
        self._t: threading.Thread | None = None

    def start(self) -> None:
        self._t = threading.Thread(target=self._run, daemon=True, name="webhooks")
        self._t.start()

    def shutdown(self) -> None:
        self.stop.set()

    def _run(self) -> None:
        while not self.stop.is_set():
            try:
                with psycopg.connect(self.db.url, autocommit=True) as conn:
                    conn.execute("LISTEN karo_topic_ready")
                    conn.execute("LISTEN kcteam_export_ready")
                    while not self.stop.is_set():
                        for n in conn.notifies(timeout=5, stop_after=1):
                            self.handle(n.channel, n.payload)
            except Exception as exc:  # noqa: BLE001 – der Dispatcher darf den Dienst nicht beenden
                self.log(f"⚠ Webhooks: {exc!r}")
                self.stop.wait(5)

    def targets(self, channel: str, data: dict) -> list[dict]:
        if channel == "kcteam_export_ready":
            return self.db.query("""SELECT c.* FROM curriculum.api_clients c
                                    JOIN curriculum.lesson_export_clients l ON l.client_id=c.id
                                    WHERE l.export_id=%s AND c.active AND c.webhook_url IS NOT NULL""",
                                 (data.get("export_id"),))
        return self.db.query("""SELECT DISTINCT c.* FROM curriculum.api_clients c
                                JOIN curriculum.topic_demand t ON t.tenant=c.tenant
                                WHERE t.request_id=%s AND c.active AND c.webhook_url IS NOT NULL""",
                             (data.get("request_id"),))

    def handle(self, channel: str, payload: str) -> None:
        try:
            data = json.loads(payload)
        except ValueError:
            return
        event = "lesson.finished" if channel == "kcteam_export_ready" else "request.updated"
        for c in self.targets(channel, data):
            body = {"event": event, "data": {k: v for k, v in data.items() if k != "client_id"}}
            threading.Thread(target=self.deliver, args=(c, body), daemon=True).start()

    def deliver(self, c: dict, body: dict) -> bool:
        raw = json.dumps(body, ensure_ascii=False).encode()
        row = self.db.one("""INSERT INTO curriculum.webhook_deliveries(client_id, event, payload)
                             VALUES (%s,%s,%s) RETURNING id""", (c["id"], body["event"], Jsonb(body)))
        err, code = None, None
        for i, wait in enumerate(RETRIES, 1):
            if self.stop.wait(wait):
                break
            try:
                r = self.http.post(c["webhook_url"], content=raw, headers={
                    "Content-Type": "application/json",
                    "X-Curriculum-Event": body["event"],
                    "X-Curriculum-Signature": sign(c["webhook_secret"] or "", raw)})
                code, err = r.status_code, None if r.status_code < 300 else r.text[:200]
            except httpx.HTTPError as exc:
                code, err = None, repr(exc)[:200]
            self.db.query("""UPDATE curriculum.webhook_deliveries SET attempts=%s, status_code=%s, error=%s,
                               delivered_at=CASE WHEN %s THEN now() END WHERE id=%s""",
                          (i, code, err, err is None, row["id"]))
            if err is None:
                return True
        return False
