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


def karo(env, tmp_path, monkeypatch):
    """Karo mit eigener Datenbank, das über den Testclient mit dem Dienst spricht."""
    monkeypatch.syspath_prepend(os.environ["KARO_APP_PATH"])
    monkeypatch.setenv("KARO_DATA_DIR", str(tmp_path / "karo"))
    monkeypatch.setenv("KARO_CURRICULUM_URL", "http://127.0.0.1:8088")
    monkeypatch.setenv("KARO_CURRICULUM_KEY", env["key"])
    from app import db
    from app.adaptiv import curriculum_dienst as bridge, store
    db.init()
    store.init()
    paths = []

    def transport(cfg, method, path, body=None):
        paths.append((method, path, body))
        response = env["api"].request(method, path, json=body,
                                      headers={"Authorization": "Bearer " + env["key"]})
        assert response.status_code in (200, 202), response.text
        return response.json()

    monkeypatch.setattr(bridge, "request", transport)
    return bridge, paths


def test_real_consumer_format_roundtrip(env, tmp_path, monkeypatch):
    bridge, calls = karo(env, tmp_path, monkeypatch)
    from app import config, jobs
    from app.adaptiv import store, unterricht
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
    # Vor der ersten Anfrage steht der Versionsabgleich.
    assert [(m, p) for m, p, _ in calls][:2] == [("GET", "/v1/meta"), ("POST", "/v1/lessons")]
    assert not any(p.endswith("/reject") for _, p, _ in calls)
    before = env["prov"].calls
    assert bridge.prepare(config.load(), payload, topic, "Mathematik", 6) == result
    assert env["prov"].calls == before


def test_inhaltsbestellung_laeuft_ende_zu_ende(env, tmp_path, monkeypatch):
    """Der Luecken-Weg, nicht der Themen-Weg: Karo meldet, was fehlt, der
    Dienst baut es aus dem kuratierten Entwurf, Karo holt es ab.

    requested -> queued -> ready -> imported -> fulfilled — und die
    naechste Lernrunde unterrichtet genau das gelieferte Konzept.
    """
    bridge, calls = karo(env, tmp_path, monkeypatch)
    from app import config
    from app.adaptiv import store, unterricht
    from kcteam import slices as kc_slices
    kc_slices.seed_slices(env["db"])

    zeile = store.inhalt_anfordern(
        "biologie", "BI.PFLANZEN.FOTOSYNTHESE", "lektion", "fehlt",
        kontext={"titel": "Fotosynthese", "klasse": 6})
    assert zeile["status"] == "offen"

    cfg = config.load()
    # Erster Lauf: Bestellung raus, Auftragsnummer verknuepft.
    assert bridge.anfragen_bedienen(cfg) == {"bedient": 0}
    auftrag = store.inhalt_anfragen("offen")[0]
    assert auftrag["external_ref"], "die Dienst-Nummer fehlt"
    gebucht = [b for m, p, b in calls if p == "/v1/lessons"]
    assert gebucht and gebucht[0]["concept_id"] == "BI.PFLANZEN.FOTOSYNTHESE"
    assert gebucht[0]["request_reason"] == "fehlt"

    # Der Dienst arbeitet; Karo holt ab, wenn fertig.
    while env["agent"].process_export():
        pass
    assert bridge.anfragen_bedienen(cfg) == {"bedient": 1}

    erfuellt = store.inhalt_anfragen("erfuellt")
    assert len(erfuellt) == 1 and erfuellt[0]["konzept_id"], erfuellt
    assert not any(p.endswith("/reject") for _, p, _ in calls)

    konzept = store.konzept(erfuellt[0]["konzept_id"])
    assert konzept["quelle"] == "curriculum" and konzept["fach"] == "biologie"
    # Die naechste Lernrunde benutzt die Lieferung direkt — Anker steht.
    sitzung = unterricht.starte(konzept["id"], "Fotosynthese")
    assert unterricht.bildschirm(sitzung)["art"] == "anker"
    # … und kennt ihre Voraussetzungen (Umweg im selben Thema moeglich).
    voraus = store.voraussetzungen(konzept["id"])
    assert {v["voraussetzung"] for v in voraus} >= {
        "BI.PFLANZEN.BEDUERFNISSE", "BI.PFLANZEN.AUFBAU"}

    # Idempotent: ein zweiter Lauf bestellt nichts erneut.
    assert bridge.anfragen_bedienen(cfg) == {"bedient": 0}
    assert not store.inhalt_anfragen("offen")


def test_a_version_gap_defers_and_never_rejects(env, tmp_path, monkeypatch):
    """Beide Seiten absichtlich auf verschiedenen Vertragsfassungen.

    Der Auftrag muss warten und ein Mensch muss es erfahren. Was er nicht
    darf: eine Ablehnung schicken. Zwei davon und der Dienst liefert dieses
    Thema nie wieder aus — wegen eines Versionsunterschieds, der über den
    Inhalt nichts aussagt.
    """
    bridge, calls = karo(env, tmp_path, monkeypatch)
    from app import config, db, jobs
    monkeypatch.setattr(bridge, "CONTRACT_VERSION", "karo-adaptiv-v9.9")
    topic = "Ungleichnamige Brüche addieren"

    for _ in range(3):
        with pytest.raises(jobs.Deferred) as warten:
            bridge.prepare(config.load(), {}, topic, "Mathematik", 6)
        assert warten.value.seconds >= 60

    wege = [(m, p) for m, p, _ in calls]
    assert wege == [("GET", "/v1/meta")] * 3          # es ging nie eine Anfrage raus
    assert not any(p.endswith("/reject") for _, p in wege)

    with db.tx() as c:
        meldungen = c.execute("SELECT text, anzahl FROM betriebsmeldung "
                              "WHERE bereich='lehrplan-dienst'").fetchall()
    assert len(meldungen) == 1 and meldungen[0]["anzahl"] == 3
    assert "karo-adaptiv-v9.9" in meldungen[0]["text"]
    assert env["api"].get("/v1/meta").json()["contract_version"] in meldungen[0]["text"]

    # Dienst und Karo wieder auf derselben Fassung: es läuft sofort weiter.
    monkeypatch.setattr(bridge, "CONTRACT_VERSION", env["api"].get("/v1/meta").json()["contract_version"])
    payload = {}
    for _ in range(12):
        try:
            result = bridge.prepare(config.load(), payload, topic, "Mathematik", 6)
            break
        except jobs.Deferred as pending:
            payload = pending.payload
            env["agent"].process_next()
    else:
        pytest.fail("Nach dem Versionsabgleich kam trotzdem nichts an")
    assert result["quelle"] == "curriculum"


def test_a_broken_envelope_is_reported_as_contract_not_as_content(env, tmp_path, monkeypatch):
    """Der Dienst liefert eine Antwort, deren Hülle nicht zum Vertrag passt.

    Karo meldet das dreimal. Früher zählte jede Meldung gegen das Thema und
    nach zweien war es tot. Jetzt zählt keine, und die Lektion bleibt für
    andere Abnehmer unverändert lieferbar.
    """
    bridge, calls = karo(env, tmp_path, monkeypatch)
    from app import config, jobs
    from kcteam import lessons, review
    db = env["db"]
    topic = "Ungleichnamige Brüche addieren"

    echt = bridge._checked

    def kaputt(result, thema, grade, fach):
        # so, als hätte der Dienst die Klasseneinordnung weggelassen
        return echt({**result, "classification": {}}, thema, grade, fach)

    monkeypatch.setattr(bridge, "_checked", kaputt)
    payload, eid = {}, None
    for _ in range(12):
        with pytest.raises(jobs.Deferred) as warten:
            bridge.prepare(config.load(), payload, topic, "Mathematik", 6)
        payload = warten.value.payload
        env["agent"].process_next()
        if any(p.endswith("/reject") for _, p, _ in calls):
            eid = int([p for _, p, _ in calls if p.endswith("/reject")][-1].split("/")[3])
            if sum(1 for _, p, _ in calls if p.endswith("/reject")) >= 3:
                break
    assert eid, "Karo hat den Vertragsverstoß nie gemeldet"
    assert all(b.get("reason_code") == "contract" for _, p, b in calls if p.endswith("/reject"))

    verworfen = db.query("SELECT reason_code, contract_version FROM curriculum.lesson_export_rejections")
    assert verworfen and all(v["reason_code"] == "contract" for v in verworfen)
    assert all(v["contract_version"] == lessons.CONTRACT_VERSION for v in verworfen)
    # ein Mensch sieht es …
    assert any("Vertrag" in (x["reason"] or "") for x in review.list_open(db))
    # … und das Thema ist trotzdem nicht gesperrt: ohne den kaputten Umschlag
    # geht es sofort wieder durch.
    monkeypatch.setattr(bridge, "_checked", echt)
    for _ in range(12):
        try:
            result = bridge.prepare(config.load(), payload, topic, "Mathematik", 6)
            break
        except jobs.Deferred as pending:
            payload = pending.payload
            env["agent"].process_next()
    else:
        pytest.fail("Das Thema blieb nach Vertragsmeldungen gesperrt — genau der alte Fehler")
    assert result["quelle"] == "curriculum"


def test_die_wirkungsmeldung_kommt_echt_an_und_traegt_keine_kinddaten(env, tmp_path, monkeypatch):
    """Karo meldet eine wirkungslose Erklaerung — gegen den echten Dienst.

    Der Einzeltest auf jeder Seite haette den Fehler nicht gefunden, der hier
    auffiel: das Eingabemodell lag in `create_app`, und FastAPI hielt den
    Rumpf deshalb fuer einen Abfrageparameter. Jede Meldung wurde mit 422
    abgewiesen, ohne dass es irgendwo auffiel.
    """
    import json

    bridge, calls = karo(env, tmp_path, monkeypatch)
    from app import config, db as karo_db, jobs
    from app.adaptiv import store
    dienst_db = env["db"]

    payload = {}
    for _ in range(12):
        try:
            ergebnis = bridge.prepare(config.load(), payload, "Ungleichnamige Brüche addieren",
                                      "Mathematik", 6)
            break
        except jobs.Deferred as wartet:
            payload = wartet.payload
            env["agent"].process_next()
    else:
        pytest.fail("Ohne Lektion gibt es keine Erklaerung, deren Wirkung sich messen liesse")

    fehlertyp_id = store.fehlertypen(ergebnis["konzept_id"])[0]["id"]
    with karo_db.tx() as c:
        c.execute("""INSERT INTO lern_erklaerung
                       (fehlertyp_id, klasse, version, inhalt, visualisierung,
                        schwierigkeit, quelle, geprueft_am, aktiv,
                        ausgeliefert, folge_erfolge, created_at, updated_at)
                     VALUES (?,6,99,'{}','{}',1,'test',?,1,20,2,?,?)""",
                  (fehlertyp_id, karo_db.now(), karo_db.now(), karo_db.now()))

    cfg = config.load()
    assert bridge.wirkung_melden(cfg) == 1
    gesendet = [b for m, p, b in calls if p == "/v1/explanations/feedback"]
    assert gesendet, "die Meldung ging nie raus"
    erlaubt = {"erklaerung_id", "konzept_key", "fehler_key", "klasse",
               "ausgeliefert", "wirkte", "wirkquote"}
    assert all(set(b) == erlaubt for b in gesendet[0]["befunde"]), gesendet[0]

    eintrag = dienst_db.one("""SELECT * FROM curriculum.human_queue
                                WHERE kind='error' ORDER BY id DESC LIMIT 1""")
    assert eintrag and "2 von 20" in json.dumps(eintrag, ensure_ascii=False, default=str)

    # Zweiter Lauf: dieselbe Erklaerung geht nicht noch einmal raus.
    assert bridge.wirkung_melden(cfg) == 0
