"""Regressionstests für die zweite Review-Runde: freigegebene Inhalte bleiben erhalten, Sitzungen hängen nie,
Kritiker-Ausfälle blockieren nichts, Wartezeiten sind abbrechbar."""
from __future__ import annotations

import os
import threading
import time

import pytest

from kcteam import review
from kcteam.agents import AgentRunner, RunStopped
from kcteam.config import load_config
from kcteam.pipeline import Pipeline, route_findings
from kcteam.providers import make_provider
from kcteam.providers.base import Completion, Provider, ProviderError
from kcteam.providers.mock import MockProvider
from kcteam.schemas import Finding, InspectorVerdict

URL = os.environ.get("TEST_DATABASE_URL")
needs_db = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL nicht gesetzt")


# ---------------- ohne Datenbank ----------------
def _verdict(*findings):
    return InspectorVerdict(decision="rejected", findings=[Finding(**f) for f in findings], summary="")


def test_route_findings_ignores_warnings_and_routes_grades_to_meta():
    v = _verdict({"location": "target_grade", "rule": "r", "severity": "block", "reason": "x", "requirement": "y"},
                 {"location": "anchor_items[0]", "rule": "r", "severity": "warn", "reason": "x", "requirement": "y"})
    assert route_findings(v) == {"meta"}
    v = _verdict({"location": "inhalt.exit_items[2].prompt", "rule": "r", "severity": "block", "reason": "x",
                  "requirement": "y"})
    assert route_findings(v) == {"diag"}


class _NullDB:
    def log_call(self, *a, **k):
        pass


class _AlwaysLimited(Provider):
    def __init__(self):
        super().__init__(name="limited", settings={})

    def complete(self, **kw) -> Completion:
        raise ProviderError("429", rate_limited=True, retryable=True, retry_after=10_000)


def test_backoff_is_capped_and_stops_immediately():
    cfg = load_config()
    runner = AgentRunner(cfg=cfg, provider=_AlwaysLimited(), db=_NullDB(), run_id="x")
    threading.Timer(0.3, runner.stop.set).start()
    t0 = time.time()
    with pytest.raises(RunStopped):
        runner._complete(role="kritiker", system="s", prompt="p", model="m", web_search=False, meta={},
                         entity_id="e")
    assert time.time() - t0 < 3
    # gemeinsame Pause gilt für alle Worker und ist gedeckelt
    assert runner._cooldown_until - time.time() <= float(cfg.p("max_retry_wait", 300)) * 1.2


# ---------------- mit Datenbank ----------------
@pytest.fixture(scope="module")
def env():
    from kcteam.db import DB
    os.environ["KARO_SPEC_PATH"] = "/nonexistent"
    cfg = load_config()
    cfg.pipeline["parallel_concepts"] = 3
    db = DB(URL)
    db.query("DROP SCHEMA IF EXISTS karo CASCADE; DROP SCHEMA IF EXISTS curriculum CASCADE; "
             "DROP SCHEMA IF EXISTS learner CASCADE;")
    db.migrate()
    _run(cfg, db)
    return cfg, db


def _run(cfg, db, provider=None, subject="Mathematik", grades=(1, 10)):
    provider = provider or make_provider("mock", cfg)
    run_id = db.start_run(subject, grades, "mock")
    pipe = Pipeline(cfg=cfg, provider=provider, db=db, run_id=run_id, log=lambda *_: None)
    stats = pipe.run(subject, grades)
    db.finish_run(run_id, "finished", stats)
    return stats


def _approved(db, bid):
    return {c["id"] for c in db.concepts(block_id=bid, light=True) if c["status"] == "approved"}


@needs_db
def test_block_retry_keeps_approved_concepts(env):
    cfg, db = env
    before = _approved(db, "MA.BRUECHE")
    assert before
    db.enqueue_human("block", "MA.BRUECHE", "graph", "Struktur nachschärfen")
    q = [r for r in review.list_open(db) if r["entity_id"] == "MA.BRUECHE" and r["stage"] == "graph"][0]
    review.retry(db, q["id"], "Reihenfolge prüfen")
    assert db.one("SELECT status FROM curriculum.topic_blocks WHERE id='MA.BRUECHE'")["status"] == "pending"
    _run(cfg, db)
    assert before <= _approved(db, "MA.BRUECHE")
    # der Fachdidaktiker bekam die bestehende Struktur zum Überarbeiten
    call = db.one("""SELECT count(*) AS n FROM curriculum.agent_calls WHERE role='fachdidaktiker'
                     AND entity_id='MA.BRUECHE' AND run_id=(SELECT id FROM curriculum.runs
                                                            ORDER BY started_at DESC LIMIT 1)""")
    assert call["n"] >= 1


class _CriticOnApproved(MockProvider):
    """Meldet einen Mangel an einem freigegebenen Konzept – der darf es nicht verstecken."""
    target = ""

    def _kritiker(self, meta):
        return {"issues": [{"concept_id": self.target, "type": "wrong_edge", "description": "Test",
                            "suggested_fix": "Test", "route_to": "fachdidaktiker"}]}


@needs_db
def test_critic_issue_on_approved_concept_goes_to_human(env):
    cfg, db = env
    approved = sorted(_approved(db, "MA.BRUECHE"))
    assert len(approved) >= 2
    tid, other = approved[0], approved[1]
    _CriticOnApproved.target = tid
    # ein anderes Konzept im Block wieder in die Kritikerprüfung bringen
    db.set_concept_status(other, "visualized")
    _run(cfg, db, provider=_CriticOnApproved(cfg.provider_settings("mock")))
    assert db.concept(tid)["status"] == "approved"
    assert db.one("""SELECT 1 AS x FROM curriculum.human_queue WHERE status='open' AND kind='critic'
                     AND entity_id=%s""", (tid,))
    assert db.concept(other)["status"] == "approved"


class _CriticFails(MockProvider):
    def _kritiker(self, meta):
        raise ProviderError("Kritiker kaputt", retryable=False)


@needs_db
def test_critic_failure_does_not_block_finalization(env):
    cfg, db = env
    _run(cfg, db, provider=_CriticFails(cfg.provider_settings("mock")), subject="Chemie", grades=(7, 8))
    code = db.get_subject("Chemie")["code"]
    states = {c["status"] for c in db.concepts(subject_code=code, light=True)}
    assert "approved" in states and "visualized" not in states   # trotz Kritiker-Ausfall abgeschlossen
    assert db.one("""SELECT 1 AS x FROM curriculum.human_queue WHERE status='open' AND kind='error'
                     AND stage='critic'""")


@needs_db
def test_session_recovers_when_current_item_loses_approval(env):
    _cfg, db = env
    sid = db.one("SELECT karo.start_diagnosis(%s,%s,%s,%s,%s) AS s",
                 ("kind-r2", "MA", 6, ["MA.BRUECHE.ADD_UNGL"], []))["s"]
    step = db.one("SELECT karo.next_step(%s) AS s", (sid,))["s"]
    assert step["action"] == "ask"
    item = db.one("SELECT concept_id FROM curriculum.items WHERE id=%s", (step["item"]["id"],))
    db.query("UPDATE curriculum.concepts SET status='structured' WHERE id=%s", (item["concept_id"],))
    try:
        step2 = db.one("SELECT karo.next_step(%s) AS s", (sid,))["s"]
        assert step2["action"] != "ask" or step2["item"] is not None
        if step2["action"] == "ask":
            assert step2["item"]["id"] != step["item"]["id"]
    finally:
        db.query("UPDATE curriculum.concepts SET status='approved' WHERE id=%s", (item["concept_id"],))


@needs_db
def test_purge_removes_sessions_mastery_and_idle_learners(env):
    _cfg, db = env
    from kcteam.simulate import run_child
    run_child(db, learner="kind-alt", subject_code="MA", grade=6, targets=["MA.BRUECHE.ADD_UNGL"], profile="strong")
    lid = db.one("SELECT id FROM learner.learners WHERE external_ref='kind-alt'")["id"]
    assert db.one("SELECT count(*) AS n FROM learner.mastery WHERE learner_id=%s", (lid,))["n"] > 0
    db.query("UPDATE learner.sessions SET started_at=now()-interval '400 days' WHERE learner_id=%s", (lid,))
    db.query("UPDATE learner.mastery SET updated_at=now()-interval '400 days' WHERE learner_id=%s", (lid,))
    db.query("UPDATE learner.learners SET created_at=now()-interval '400 days' WHERE id=%s", (lid,))
    res = db.one("SELECT karo.purge_learner_data(365) AS r")["r"]
    assert res["sessions_deleted"] >= 1 and res["mastery_deleted"] >= 1 and res["learners_deleted"] >= 1
    assert not db.one("SELECT 1 AS x FROM learner.learners WHERE id=%s", (lid,))
