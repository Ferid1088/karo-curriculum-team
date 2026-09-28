"""Optional contract test against the actual consumer, not a copied JSON fixture.

Requires KARO_APP_PATH and a disposable TEST_DATABASE_URL. Reuses the service's
mock-provider fixture; never calls a paid model or touches the production DB.
"""
import os

import pytest

from test_api import env as _api_fixture

env = _api_fixture  # isolated PostgreSQL + actual HTTP API + mock agent

pytestmark = pytest.mark.skipif(
    not os.environ.get("KARO_APP_PATH") or not os.environ.get("TEST_DATABASE_URL"),
    reason="KARO_APP_PATH and disposable TEST_DATABASE_URL required")


def test_real_consumer_format_roundtrip(env, tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(os.environ["KARO_APP_PATH"])
    monkeypatch.setenv("KARO_DATA_DIR", str(tmp_path / "karo"))
    monkeypatch.setenv("KARO_CURRICULUM_URL", "http://127.0.0.1:8088")
    monkeypatch.setenv("KARO_CURRICULUM_KEY", env["key"])
    from app import config, db, jobs
    from app.adaptiv import curriculum_dienst as bridge, store, unterricht
    db.init()
    store.init()
    paths = []

    def transport(cfg, method, path, body=None):
        paths.append((method, path))
        response = env["api"].request(method, path, json=body,
                                      headers={"Authorization": "Bearer " + env["key"]})
        assert response.status_code in (200, 202), response.text
        return response.json()

    monkeypatch.setattr(bridge, "request", transport)
    topic = "Ungleichnamige Brüche addieren"
    payload = {}
    for _ in range(12):
        try:
            result = bridge.prepare(config.load(), payload, topic, "Mathematik", 6)
            break
        except jobs.Deferred as pending:
            payload = pending.payload
            env["agent"].process_next()
    else:
        pytest.fail("Real Karo format never became usable")
    assert store.konzept(result["konzept_id"])["quelle"] == "curriculum"
    session = unterricht.starte(result["konzept_id"], topic)
    assert unterricht.bildschirm(session)["art"] == "anker"
    assert paths[0] == ("POST", "/v1/lessons")
    assert not any(path.endswith("/reject") for _, path in paths)
    before = env["prov"].calls
    assert bridge.prepare(config.load(), payload, topic, "Mathematik", 6) == result
    assert env["prov"].calls == before
