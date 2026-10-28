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
