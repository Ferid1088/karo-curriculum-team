"""Devin-Provider: asynchrone Sessions über die dokumentierte v1-API.

Kein echter API-Aufruf — die HTTP-Schicht ist `FakeHTTP`, das Sessions in
einem dict hält (die "Gegenstelle" lebt in `remote`, damit ein neuer
Provider dieselben Sessions sieht — genau wie ein Neustart des Workers).
Für durchgehende Läufe erzeugt `structured_output` derselbe MockProvider,
der die übrige Suite speist: das Ergebnis ist also immer schema- und
vertragsgültig.
"""
from __future__ import annotations

import json
import os
import threading
import time

import pytest

from kcteam.providers.base import Completion, ProviderError, ProviderPending
from kcteam.providers.devin import DevinProvider
from kcteam.providers.mock import MockProvider

URL = os.environ.get("TEST_DATABASE_URL")
needs_db = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL nicht gesetzt")


class Resp:
    def __init__(self, status=200, body=None, headers=None):
        self.status_code, self._body, self.headers = status, body or {}, headers or {}

    def json(self):
        return self._body


class FakeHTTP:
    """Steht an der Stelle von httpx.Client: beantwortet v1-Aufrufe aus `remote`.

    `remote` gehört dem Test und überlebt den Provider — so, wie die echte
    API die Sessions hält. `queue` legt einzelne vorgegebene Antworten oder
    Ausnahmen vorweg (Fehlerfälle). `output_fn` berechnet structured_output
    für fertige Sessions.
    """

    def __init__(self, remote=None, queue=(), output_fn=None):
        import itertools
        self.remote = remote if remote is not None else {}
        self.queue = list(queue)
        self.output_fn = output_fn
        # Wie die echte API: eine vergessene Session-ID wird nie neu vergeben.
        self._seq = itertools.count(1)
        self.calls: list[tuple] = []
        self.get_meta = lambda: {}      # TestDevin setzt hier das meta des laufenden Aufrufs
        self._lock = threading.Lock()

    def request(self, method, path, json=None):  # noqa: A002 – httpx-Signatur
        with self._lock:
            self.calls.append((method, path))
            if self.queue:
                item = self.queue.pop(0)
                if isinstance(item, Exception):
                    raise item
                return item
            if method == "POST" and path == "/sessions":
                sid = f"devin-{next(self._seq)}"
                self.remote[sid] = {"status": "working", "out": None, "prompt": json["prompt"],
                                    "schema": json.get("structured_output_schema"),
                                    "meta": dict(self.get_meta()), "nudge": None}
                return Resp(200, {"session_id": sid, "url": f"https://app.devin.ai/sessions/{sid}"})
            if method == "GET":
                s = self.remote.get(path.rsplit("/", 1)[-1])
                if s is None:
                    return Resp(404, {"detail": "unknown session"})
                out = s["out"]
                if s["status"] == "finished" and out is None and self.output_fn:
                    out = self.output_fn(s)
                return Resp(200, {"status_enum": s["status"], "structured_output": out})
            if method == "POST" and path.endswith("/message"):
                sid = path.split("/")[-2]
                if sid not in self.remote:
                    return Resp(404, {"detail": "unknown session"})
                self.remote[sid]["nudge"] = json["message"]
                return Resp(200, {})
        raise AssertionError(f"unerwarteter Aufruf: {method} {path}")

    def posts(self, path_suffix="/sessions"):
        return [c for c in self.calls if c[0] == "POST" and c[1] == path_suffix]


class DevinFake(DevinProvider):
    """DevinProvider, der den HTTP-Verkehr an `FakeHTTP` gibt und das `meta`
    des gerade laufenden Aufrufs weitergibt — die echte API sieht nur den
    Prompt, das Fake braucht meta, um Mock-Antworten zu bauen."""

    def __init__(self, settings=None, remote=None, output_fn=None, queue=()):
        super().__init__(settings or {})
        self._local = threading.local()
        self._http = FakeHTTP(remote=remote, queue=queue, output_fn=output_fn)
        self._http.get_meta = lambda: getattr(self._local, "meta", {})

    def complete(self, *, meta=None, **kw):
        self._local.meta = meta or {}      # threadlokal: _parallel ruft uns nebenläufig
        return super().complete(meta=meta, **kw)


def _call(p, **kw):
    kw.setdefault("system", "SYSTEM")
    kw.setdefault("user", "USER")
    kw.setdefault("model", "devin")
    kw.setdefault("meta", {"role": "niveau_kalibrierer", "entity_id": "MA.X.Y", "attempt": 0})
    return p.complete(**kw)


# ---------------------------------------------------------------- ohne Datenbank
def test_fehlender_schluessel_bricht_frueh_ab(monkeypatch):
    monkeypatch.delenv("DEVIN_API_KEY", raising=False)
    p = DevinProvider({})
    ok, msg = p.available()
    assert not ok and "DEVIN_API_KEY" in msg
    with pytest.raises(ProviderError) as exc:
        _call(p)
    assert exc.value.retryable is False and "DEVIN_API_KEY" in str(exc.value)


def test_auth_header_und_base_url(monkeypatch):
    monkeypatch.setenv("DEVIN_API_KEY", "apk_user_testkey")
    p = DevinProvider({})
    try:
        assert p.client.headers["authorization"] == "Bearer apk_user_testkey"
        assert str(p.client.base_url).rstrip("/") == "https://api.devin.ai/v1"
    finally:
        p._http.close()


def test_session_wird_nur_einmal_erzeugt_und_pending():
    remote = {}
    p = DevinFake({"poll_seconds": 5}, remote=remote)
    with pytest.raises(ProviderPending) as exc:
        _call(p)
    assert exc.value.wait_seconds == 5 and exc.value.session_id == "devin-1"
    # zweiter Aufruf mit demselben Auftrag: Status holen, nicht neu anlegen
    with pytest.raises(ProviderPending):
        _call(p)
    assert p._http.posts() == [("POST", "/sessions")]   # genau ein Create
    assert [sid for sid in remote] == ["devin-1"]


def test_prompt_verbietet_repo_arbeit_und_traegt_schema():
    p = DevinFake({})
    meta = {"role": "diagnostiker", "entity_id": "X", "json_schema": {"type": "object"}}
    with pytest.raises(ProviderPending):
        _call(p, meta=meta)
    s = next(iter(p._http.remote.values()))
    assert "keine Git-Aktionen" in s["prompt"] and "structured output" in s["prompt"]
    assert "SYSTEM" in s["prompt"] and "USER" in s["prompt"]
    assert s["schema"] == {"type": "object"}


def test_fertige_session_liefert_completion():
    p = DevinFake({})
    with pytest.raises(ProviderPending):
        _call(p)
    p._http.remote["devin-1"].update(status="finished", out={"konzept_id": "MA.X.Y", "werte": [1]})
    comp = _call(p)
    assert isinstance(comp, Completion) and json.loads(comp.text)["konzept_id"] == "MA.X.Y"
    # ein weiterer Aufruf derselben Anfrage holt das Ergebnis erneut — keine neue Session
    assert _call(p).text == comp.text
    assert len(p._http.posts()) == 1


def test_finished_ohne_output_ist_dauerhaft_fehler():
    p = DevinFake({})
    with pytest.raises(ProviderPending):
        _call(p)
    p._http.remote["devin-1"]["status"] = "finished"     # kein structured_output
    with pytest.raises(ProviderError) as exc:
        _call(p)
    assert exc.value.retryable is False and "structured_output" in str(exc.value)
    assert p._mem[next(iter(p._mem))]["status"] == "failed"


def test_blockiert_bekommt_eine_nachricht_dann_fehler():
    p = DevinFake({})
    with pytest.raises(ProviderPending):
        _call(p)
    p._http.remote["devin-1"]["status"] = "blocked"
    with pytest.raises(ProviderPending):
        _call(p)                                        # einmal genudged, weiter pending
    assert p._http.remote["devin-1"]["nudge"] is not None
    with pytest.raises(ProviderError) as exc:
        _call(p)                                        # immer noch blocked -> aufgeben
    assert exc.value.retryable is False


def test_blockiert_mit_output_gilt_als_fertig():
    p = DevinFake({})
    with pytest.raises(ProviderPending):
        _call(p)
    p._http.remote["devin-1"].update(status="blocked", out={"ok": True})
    assert json.loads(_call(p).text) == {"ok": True}


def test_abgelaufene_session_wird_einmal_neu_gestartet():
    p = DevinFake({"max_session_restarts": 1})
    with pytest.raises(ProviderPending):
        _call(p)
    p._http.remote["devin-1"]["status"] = "expired"
    with pytest.raises(ProviderPending):
        _call(p)                                        # restart -> devin-2
    assert "devin-2" in p._http.remote
    p._http.remote["devin-2"]["status"] = "expired"
    with pytest.raises(ProviderError) as exc:
        _call(p)                                        # keine zweite Chance
    assert exc.value.retryable is False


# ------------------------------------------------------------ 404 = missing_remote
def test_404_beim_ersten_poll_startet_neu_statt_zu_fehlern():
    remote = {}
    p = DevinFake({"max_session_restarts": 1}, remote=remote)
    with pytest.raises(ProviderPending):
        _call(p)
    del remote["devin-1"]                        # die Gegenstelle hat sie vergessen
    with pytest.raises(ProviderPending) as exc:
        _call(p)                                 # kein Fail: bounded restart -> devin-2
    assert "devin-2" in remote
    assert exc.value.session_id == "devin-2"
    assert len(p._http.posts()) == 2             # genau zwei Creates


def test_404_nach_erfolgreichen_polls_startet_neu():
    remote = {}
    p = DevinFake({"max_session_restarts": 1}, remote=remote)
    with pytest.raises(ProviderPending):
        _call(p)
    with pytest.raises(ProviderPending):
        _call(p)                                 # mehrere erfolgreiche Polls
    with pytest.raises(ProviderPending):
        _call(p)
    del remote["devin-1"]
    with pytest.raises(ProviderPending):
        _call(p)
    assert "devin-2" in remote and len(p._http.posts()) == 2


def test_404_restart_ist_begrenzt_und_faellt_dann_sauber():
    remote = {}
    p = DevinFake({"max_session_restarts": 1}, remote=remote)
    with pytest.raises(ProviderPending):
        _call(p)
    del remote["devin-1"]
    with pytest.raises(ProviderPending):
        _call(p)                                 # restart -> devin-2
    del remote["devin-2"]
    with pytest.raises(ProviderError) as exc:
        _call(p)                                 # Cap erreicht: dauerhafter Fehler
    assert exc.value.retryable is False
    assert len(p._http.posts()) == 2             # kein drittes Create


def test_404_restart_ist_idempotent_keine_doppelsession():
    remote = {}
    p = DevinFake({"max_session_restarts": 2}, remote=remote)
    with pytest.raises(ProviderPending):
        _call(p)
    del remote["devin-1"]
    with pytest.raises(ProviderPending):
        _call(p)                                 # restart -> devin-2
    with pytest.raises(ProviderPending):
        _call(p)                                 # gleicher Auftrag: poll, kein Create
    with pytest.raises(ProviderPending):
        _call(p)
    assert len(p._http.posts()) == 2
    assert set(remote) == {"devin-2"}            # genau eine aktive Session


def test_404_beim_nudge_einer_blockierten_session():
    remote = {}
    p = DevinFake({"max_session_restarts": 1}, remote=remote)
    with pytest.raises(ProviderPending):
        _call(p)
    remote["devin-1"]["status"] = "blocked"
    del remote["devin-1"]                        # blockiert UND remote weg
    with pytest.raises(ProviderPending):
        _call(p)                                 # missing_remote -> restart
    assert "devin-2" in remote and len(p._http.posts()) == 2


def test_404_nach_fertigem_ergebnis_loest_keinen_neubau_aus():
    """`finished` ist ein Endzustand: das lokale Ergebnis gilt, auch wenn
    Devin die Session laengst entsorgt hat — kein zweiter Build."""
    remote = {}
    p = DevinFake({}, remote=remote)
    with pytest.raises(ProviderPending):
        _call(p)
    remote["devin-1"].update(status="finished", out={"konzept_id": "X"})
    comp = _call(p)
    del remote["devin-1"]
    again = _call(p)
    assert again.text == comp.text
    assert len(p._http.posts()) == 1             # genau ein Create, kein Neubau
    gets = [c for c in p._http.calls if c[0] == "GET"]
    assert len(gets) == 1                        # und kein zweiter Poll


def test_404_nach_worker_neustart_wird_recovered():
    """Worker-Neustart: ein NEUER Provider sieht denselben `remote`-Stand —
    die lokale Session-Row (hier `_mem`, in Betrieb `provider_sessions`)
    bleibt, die Remote ist weg: bounded restart, nicht zwei aktive."""
    remote = {}
    p1 = DevinFake({"max_session_restarts": 1}, remote=remote)
    with pytest.raises(ProviderPending):
        _call(p1)
    p2 = DevinFake({"max_session_restarts": 1}, remote=remote)
    p2._mem = p1._mem                            # „Datenbank“ überlebt den Worker
    del remote["devin-1"]                        # remote ist die Session verloren
    with pytest.raises(ProviderPending):
        _call(p2)
    assert len(remote) == 1 and len(p2._http.posts()) == 1


def test_maximales_session_alter_begrenzt_warten():
    p = DevinFake({"max_session_seconds": 1})
    with pytest.raises(ProviderPending):
        _call(p)
    p._mem[next(iter(p._mem))]["created_ts"] = time.time() - 10
    with pytest.raises(ProviderError) as exc:
        _call(p)
    assert exc.value.retryable is False and "älter" in str(exc.value)


@pytest.mark.parametrize("resp, retryable, limited", [
    (Resp(429, headers={"retry-after": "30"}), True, True),
    (Resp(500), True, False),
    (Resp(503), True, False),
    (Resp(401), False, False),
    (Resp(403), False, False),
    (Resp(422), False, False),
])
def test_http_fehler_werden_eingeordnet(resp, retryable, limited):
    p = DevinFake({}, queue=(resp, resp))          # zweite Antwort: 422-Fallback ohne Schema
    with pytest.raises(ProviderError) as exc:
        _call(p)
    assert exc.value.retryable is retryable and exc.value.rate_limited is limited
    if limited:
        assert exc.value.retry_after == 30


def test_timeout_ist_voruebergehend():
    import httpx
    p = DevinFake({}, queue=(httpx.TimeoutException("t"),))
    with pytest.raises(ProviderError) as exc:
        _call(p)
    assert exc.value.retryable is True


def test_422_faellt_auf_prompt_schema_zurueck():
    remote = {}
    p = DevinFake({}, remote=remote,
                  queue=(Resp(422),))
    meta = {"role": "r", "json_schema": {"type": "object"}}
    with pytest.raises(ProviderPending):
        _call(p, meta=meta)
    assert "devin-1" in remote                        # zweiter Create ohne Schema klappte


def test_ueberlanger_prompt_ohne_api_schema_ist_fehler():
    """Die API nimmt keine ueberlangen Prompts: lieber sauber scheitern
    als den Auftrag abzuschneiden — die Lektionsvorgabe bleibt ganz."""
    p = DevinFake({})
    system = "## JSON-Schema deiner Antwort\n```json\n" + "x" * 40_000 + "\n```"
    with pytest.raises(ProviderError) as exc:
        _call(p, system=system, meta={"role": "r"})    # kein json_schema → kein Kompromiss
    assert exc.value.retryable is False


def test_ueberlanger_prompt_verliert_nur_die_schema_kopie():
    """Trägt meta das Schema ohnehin als API-Feld, darf die Prompt-Kopie
    entfallen — das Ergebnis wird weiter maschinell erzwungen."""
    remote = {}
    p = DevinFake({}, remote=remote)
    system = ("SYSTEM\n\n## JSON-Schema deiner Antwort\n```json\n" + "x" * 40_000 +
              "\n```\n\nHINWEIS")
    meta = {"role": "r", "json_schema": {"type": "object"}}
    with pytest.raises(ProviderPending):
        _call(p, system=system, meta=meta)
    s = remote["devin-1"]
    assert s["schema"] == {"type": "object"}          # API-Feld blieb
    assert "x" * 10 not in s["prompt"]                # Prompt-Kopie ist weg
    assert "HINWEIS" in s["prompt"]                   # der Rest bleibt
    assert len(s["prompt"]) <= 29_500


def test_secret_erscheint_nie_im_log(caplog):
    import logging
    p = DevinFake({})
    with caplog.at_level(logging.DEBUG, logger="kcteam.devin"):
        with pytest.raises(ProviderPending):
            _call(p)
        p._http.remote["devin-1"].update(status="finished", out={"a": 1})
        _call(p)
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "apk_user" not in text and "Authorization" not in text and "Bearer" not in text
    assert "devin_request_started" in text and "devin_request_completed" in text


# ---------------------------------------------------------------- mit Datenbank
@pytest.fixture(scope="module")
def env():
    """Eigene, isolierte Umgebung: geleerte Test-DB + Curriculum via Mock,
    danach arbeitet der Agent mit dem Devin-Provider."""
    from kcteam.config import load_config
    from kcteam.db import DB
    from kcteam.ondemand import CurriculumAgent
    from kcteam.pipeline import Pipeline
    from kcteam.providers import make_provider
    os.environ["KARO_SPEC_PATH"] = "/nonexistent"
    cfg = load_config()
    cfg.pipeline["parallel_concepts"] = 3
    db = DB(URL)
    from tools.reset_test_db import reset_schemas
    reset_schemas(db, mit_backup=False)
    run_id = db.start_run("Mathematik", (1, 10), "mock")
    Pipeline(cfg=cfg, provider=make_provider("mock", cfg), db=db, run_id=run_id,
             log=lambda *_: None).run("Mathematik", (1, 10))
    remote = {}
    mock = MockProvider({})

    def output(s):   # die "Devin"-Antwort: dasselbe gültige Objekt wie der Mock
        return getattr(mock, "_" + s["meta"]["role"])(s["meta"])

    prov = DevinFake({"poll_seconds": 0}, remote=remote, output_fn=output)
    agent = CurriculumAgent(cfg=cfg, provider=prov, db=db, log=lambda *_: None)
    return cfg, db, agent, prov


def _resolve(db, topic, grade, subject="Mathematik", tasks=(), tenant="schule-a"):
    from psycopg.types.json import Jsonb
    with db.tx() as cur:
        cur.execute("SELECT karo.resolve_topic(%s,%s,%s,%s,%s,%s) AS r",
                    (subject, grade, topic, [], Jsonb(list(tasks)), tenant))
        return cur.fetchone()["r"]


@needs_db
def test_session_ueberlebt_neustart_des_providers(env):
    cfg, db, _agent, _prov = env
    remote = {}
    p1 = DevinFake({"poll_seconds": 0}, remote=remote)
    p1.bind_db(db)
    with pytest.raises(ProviderPending):
        _call(p1, user="persistierter Auftrag")
    row = db.one("SELECT * FROM curriculum.provider_sessions ORDER BY created_at DESC LIMIT 1")
    assert row["session_id"] == "devin-1" and row["status"] == "working" \
        and row["role"] == "niveau_kalibrierer" and row["entity_id"] == "MA.X.Y"
    # „Neustart": neuer Provider, gleiche Datenbank, gleiche Gegenstelle
    p2 = DevinFake({"poll_seconds": 0}, remote=remote)
    p2.bind_db(db)
    remote["devin-1"].update(status="finished", out={"fertig": True})
    assert json.loads(_call(p2, user="persistierter Auftrag").text) == {"fertig": True}
    assert p2._http.posts() == []                      # keine zweite Session
    assert db.one("SELECT status FROM curriculum.provider_sessions WHERE call_key=%s",
                  (row["call_key"],))["status"] == "finished"


@needs_db
def test_auftrag_wartet_bis_devin_fertig_ist(env):
    """Gemockter End-to-End-Lauf: Auftrag -> Devin-Session(s) -> deferred ->
    fertige Ausgaben -> Validierung -> Speicherung."""
    cfg, db, agent, prov = env
    db.query("DELETE FROM curriculum.provider_sessions")   # Fake-IDs sind nur pro Gegenstelle eindeutig
    r = _resolve(db, "Prozentrechnung mit Rabatten", 7,
                 tasks=("Name: Lisa Müller\nBerechne 20 % von 80 €",))
    assert r["status"] == "ordered"
    rid = r["request_id"]

    seen = set()
    for _ in range(200):
        # Devin "arbeitet" zwischen zwei Worker-Läufen fertig
        for sid, s in prov._http.remote.items():
            if s["status"] == "working":
                s["status"] = "finished"
                seen.add(sid)
        progressed = agent.process_next()
        row = db.one("SELECT status FROM curriculum.topic_requests WHERE id=%s", (rid,))
        if row["status"] == "done" and not progressed:
            break
    st = db.one("SELECT status FROM curriculum.topic_requests WHERE id=%s", (rid,))
    assert st["status"] == "done"

    # gleicher Aufruf -> gleiche Session: ein Create je Session, keine Duplikate
    assert len(prov._http.posts()) == len(prov._http.remote)
    sessions = db.query("""SELECT * FROM curriculum.provider_sessions
                          WHERE provider='devin' AND session_id = ANY(%s)""",
                      (list(prov._http.remote),))
    assert len(sessions) == len(prov._http.remote)
    # Keine Session ist dauerhaft kaputt. Eine darf noch „working" sein: sie
    # wurde angelegt, nachdem der Auftrag das Ergebnis nicht mehr brauchte —
    # der nächste Lauf pollt sie aus derselben Zeile weiter (Recovery).
    assert all(s["status"] in ("finished", "working") for s in sessions)
    assert sum(s["status"] == "finished" for s in sessions) >= len(sessions) - 1
    # jedes erzeugte Ergebnis wurde verarbeitet und geloggt
    assert db.one("SELECT 1 AS x FROM curriculum.agent_calls WHERE provider='devin' "
                  "AND role='curriculum_agent'")
    # Datenschutz: geschrubbter Auftrag, kein Kindname in irgendeinem Prompt
    assert not any("Lisa" in s["prompt"] for s in prov._http.remote.values())
    # alle Sessions tragen die Schutz-Preamble und wurden unlisted angelegt
    assert all("ARBEITSREGELN" in s["prompt"] for s in prov._http.remote.values())


@needs_db
def test_lektion_export_ueber_devin_mit_vertragspruefung(env):
    """Der Lektions-Weg (karo_contract-Prüfung inklusive) läuft über Devin."""
    import json as _json
    from pathlib import Path
    from kcteam import lessons
    cfg, db, agent, prov = env
    karo = _json.loads((Path(__file__).parent / "fixtures" / "karo_format.json").read_text())
    spec = lessons.validate_spec(karo)
    row = lessons.request_export(db, client_id=None, spec=spec, grade=6,
                                 concept_id="MA.BRUECHE.BEGRIFF")
    eid = row["id"]
    for _ in range(60):
        if not agent.process_next():
            break
        if db.one("SELECT status FROM curriculum.lesson_exports WHERE id=%s", (eid,))["status"] \
                not in ("queued", "running"):
            break
        for s in prov._http.remote.values():
            if s["status"] == "working":
                s["status"] = "finished"
    out = db.one("SELECT status, lesson FROM curriculum.lesson_exports WHERE id=%s", (eid,))
    assert out["status"] == "ready"
    import jsonschema
    jsonschema.validate(out["lesson"], karo["schema"])     # Vertrag erfüllt
    assert db.one("SELECT 1 AS x FROM curriculum.agent_calls WHERE provider='devin' "
                  "AND role='lektionsautor'")


# --------------------------------------------------------------------------
# Vorgaben-Grenze: die Schema-Kopie im Prompt
# --------------------------------------------------------------------------

class _DBNull:
    """Minimum fuer call_json ohne Postgres: Pause-Store und Aufruf-Log."""
    url = "postgresql://unused"

    def query(self, *a, **k):
        return []

    def one(self, *a, **k):
        return None

    def log_call(self, *a, **k):
        pass

    def log_review(self, *a, **k):
        pass


class _Aufzeichner(MockProvider):
    """Merkt sich den System-Prompt statt eine Rolle zu spielen."""

    def __init__(self, native: bool):
        super().__init__({})
        self.structured_output_native = native
        self.systeme: list[str] = []

    def complete(self, *, system, user, model, web_search=False, meta=None):
        self.systeme.append(system)
        return Completion(text='{"ok": true}', model="t")


def _runner(provider):
    from kcteam.agents import AgentRunner
    from kcteam.config import load_config
    return AgentRunner(cfg=load_config(), provider=provider, db=_DBNull(), run_id="t")


def test_call_json_ohne_schemakopie_bei_nativem_structured_output():
    """Devin erzwingt das Schema über das API-Feld — die Prompt-Kopie von
    Karos Lektionsformat (~29k Zeichen) hätte den Auftrag allein schon über
    die Grenze geschoben. Andere Anbieter brauchen sie weiter im Text."""
    prov = _Aufzeichner(native=True)
    _runner(prov).call_json("lektionsautor", "t", {}, {"type": "object"},
                            lambda x: x, entity_id="E")
    assert "JSON-Schema deiner Antwort" not in prov.systeme[0]

    prov2 = _Aufzeichner(native=False)
    _runner(prov2).call_json("lektionsautor", "t", {}, {"type": "object"},
                             lambda x: x, entity_id="E")
    assert "JSON-Schema deiner Antwort" in prov2.systeme[0]


def test_grounding_bleibt_im_budget_und_unverstuemmelt():
    """Viele Fehlvorstellungen + Beispiele: die Grundlage wird an ganzen
    Einträgen verkleinert, nie mitten im Text abgeschnitten."""
    from kcteam import lessons
    gross = {
        "konzept": {"id": "X", "title": "t" * 500},
        "fehlvorstellungen": [
            {"key": f"F{i}", "beschreibung": "b" * 400, "abhilfe": "a" * 200,
             "bekannte_falsche_antworten": ["x" * 80] * 8}
            for i in range(8)],
        "aufgaben_beispiele": [{"art": "diagnostic", "niveau": "mitte",
                                "aufgabe": "f" * 300, "loesung": "l"}
                               for _ in range(10)],
    }
    klein = lessons._im_budget(gross)
    from kcteam.agents import compact
    assert len(compact(klein)) <= lessons._GROUNDING_BUDGET
    # Ganze Einträge verkleinert — kein Feld halbiert, kein Text abgeschnitten
    assert all(len(m["beschreibung"]) == 400 for m in klein["fehlvorstellungen"])
    assert len(klein["aufgaben_beispiele"]) <= 10
    json.loads(compact(klein))   # bleibt gültiges JSON


def test_grounding_unter_budget_bleibt_unberuehrt():
    from kcteam import lessons
    data = {"konzept": {"id": "X"}, "fehlvorstellungen": [],
            "aufgaben_beispiele": [{"aufgabe": "f"}]}
    assert lessons._im_budget(data) is data


def test_grundlage_budget_misst_den_festen_teil():
    """EXP-201 Regression: das statische Grundlage-Budget ignorierte den
    ~24k festen Auftragsteil (Praeambel, Rollen, Abnehmer-Format) und lag
    damit ueber der Anbietergrenze. Jetzt wird gemessen, was der Anbieter
    als Grenze nennt — der Rest gehoert der Grundlage."""
    from types import SimpleNamespace
    from kcteam import lessons
    prov = _Aufzeichner(native=True)
    prov.prompt_limit = 29_500
    prov.prompt_overhead = 1_000
    agents = _runner(prov)
    pipe = SimpleNamespace(agents=agents)
    spec = {"id": "karo-adaptiv-v1", "schema": {"type": "object"},
            "registry": [], "instructions": "i" * 6_000}
    extra = "## Format des Abnehmers\n" + "e" * 9_000
    budget = lessons._grundlage_budget(pipe, spec, extra, "t" * 150, "fb" * 1_000)
    system = agents.system_laenge("lektionsautor", spec["schema"], extra)
    wrapper = len("## Auftrag\n\n## Daten\n```json\n\n```")
    assert budget == 29_500 - (1_000 + system + 150 + 2_000 + wrapper
                               + lessons._RETRY_RESERVE)
    # Und ein Anbieter ohne Grenze bekommt den Rueckfall.
    prov2 = _Aufzeichner(native=True)
    pipe2 = SimpleNamespace(agents=_runner(prov2))
    assert lessons._grundlage_budget(pipe2, spec, extra, "t", None) \
        == lessons._GROUNDING_BUDGET


class _InspektorProv(_Aufzeichner):
    """Antwortet mit einem stets genehmigenden Inspektor-Urteil."""

    def complete(self, *, system, user, model, web_search=False, meta=None):
        self.systeme.append(system)
        return Completion(text='{"decision":"approved","findings":[],"summary":"ok"}', model="t")


def test_inspektion_teilt_nach_gemessener_grenze():
    """EXP-205 Regression: _INHALT_BUDGET=20k zaehlte nur den Inhalt —
    mit System-Auftrag (~10k) und Wiederholungs-Anhang lief ein einzelnes
    Teilpaket auf ~29,1k hart an die Anbietergrenze. Das Budget wird jetzt
    gemessen: Grenze minus Overhead, System, Aufgabe und Reserve."""
    from kcteam.pipeline import Pipeline, _INHALT_BUDGET, _PRUEF_RESERVE
    from kcteam.config import load_config
    prov = _InspektorProv(native=True)
    prov.prompt_limit = 29_500
    prov.prompt_overhead = 1_000
    pipe = Pipeline(cfg=load_config(), provider=prov, db=_DBNull(),
                    run_id="t", log=lambda *_: None)
    # ~19,8k Inhalt in zwei Feldern: unter dem alten 20k-Pauschalbudget ein
    # Teil — knapp ueber der wirklichen Restgroesse.
    content = {"feld_a": "a" * 9_900, "feld_b": "b" * 9_900}
    pipe.inspect("export", "E1", "final", "8", content, round_=1)
    assert len(prov.systeme) == 2          # dynamisch in zwei Teile zerlegt

    prov2 = _InspektorProv(native=True)    # ohne Grenze: alter Rückfall
    pipe2 = Pipeline(cfg=load_config(), provider=prov2, db=_DBNull(),
                     run_id="t", log=lambda *_: None)
    pipe2.inspect("export", "E2", "final", "8", content, round_=1)
    assert len(prov2.systeme) == 1         # 19k < 20k-Pauschalbudget
