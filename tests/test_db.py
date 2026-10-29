import pymysql
import pytest

from src.api import db


def test_init_db_retry_succeeds_after_transient_failure(monkeypatch):
    attempts = []

    def init():
        attempts.append(1)
        if len(attempts) == 1:
            raise pymysql.OperationalError(2003, "not ready")

    monkeypatch.setattr(db, "init_db", init)
    monkeypatch.setattr(db.time, "sleep", lambda _seconds: None)

    db.init_db_with_retry(max_attempts=3, retry_delay=0)

    assert len(attempts) == 2


def test_init_db_retry_raises_after_limit(monkeypatch):
    def init():
        raise pymysql.OperationalError(2003, "not ready")

    monkeypatch.setattr(db, "init_db", init)
    monkeypatch.setattr(db.time, "sleep", lambda _seconds: None)

    with pytest.raises(RuntimeError, match="after 2 attempts"):
        db.init_db_with_retry(max_attempts=2, retry_delay=0)


def test_is_ready_handles_query_failure(monkeypatch):
    monkeypatch.setattr(db, "query", lambda _sql: [{"ready": 1}])
    assert db.is_ready() is True

    def fail(_sql):
        raise pymysql.OperationalError(2003, "offline")

    monkeypatch.setattr(db, "query", fail)
    assert db.is_ready() is False


def test_connection_pool_is_created_once_and_closed(monkeypatch):
    db.close_pool()
    created = []

    class FakePool:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.closed = False
            created.append(self)

        def close(self):
            self.closed = True

    monkeypatch.setattr(db, "PooledDB", FakePool)

    first = db.get_pool()
    second = db.get_pool()

    assert first is second
    assert len(created) == 1
    assert first.kwargs["maxconnections"] == db.POOL["maxconnections"]

    db.close_pool()
    assert first.closed is True


def test_pool_uses_dictionary_cursor():
    assert db.MYSQL["cursorclass"] is pymysql.cursors.DictCursor


def test_init_db_migrates_legacy_tokens_to_sha256(monkeypatch):
    statements = []
    monkeypatch.setattr(db, "_ensure_column", lambda *_args: None)
    monkeypatch.setattr(db, "_ensure_index", lambda *_args: None)
    monkeypatch.setattr(
        db,
        "execute",
        lambda sql, args=(): statements.append(sql) or 1,
    )

    db.init_db()

    assert any(
        "SHA2(token, 256)" in sql and "is_hashed = 1" in sql
        for sql in statements
    )
