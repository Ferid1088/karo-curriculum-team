"""Thread churn must not exhaust the service's PostgreSQL connection budget."""
import threading

from kcteam.db import DB


def test_finished_workers_release_connections_without_closing_live_owner(monkeypatch):
    opened = []
    class Connection:
        closed = False
        broken = False
        def close(self):
            self.closed = True
        def execute(self, *a, **k):
            pass
    def connect(*args, **kwargs):
        conn = Connection()
        opened.append(conn)
        return conn
    monkeypatch.setattr("kcteam.db.psycopg.connect", connect)
    db = DB("test-only")
    main = db._conn()
    for _ in range(150):
        thread = threading.Thread(target=db._conn)
        thread.start()
        thread.join()
    assert db._conn() is main
    assert sum(not conn.closed for conn in opened) == 1
    assert len(db._all) == 1
    db.close_all()
    assert all(conn.closed for conn in opened)


def test_still_running_worker_is_not_closed(monkeypatch):
    class Connection:
        closed = False
        broken = False
        def close(self):
            self.closed = True
        def execute(self, *a, **k):
            pass
    monkeypatch.setattr("kcteam.db.psycopg.connect", lambda *a, **kw: Connection())
    db = DB("test-only")
    started, release = threading.Event(), threading.Event()
    worker_connection = []
    def worker():
        worker_connection.append(db._conn())
        started.set()
        release.wait(5)
    thread = threading.Thread(target=worker)
    thread.start()
    try:
        assert started.wait(5)
        db._conn()
        assert not worker_connection[0].closed
    finally:
        release.set()
        thread.join()
        db.close_all()
