"""
MySQL persistence layer for the SRE On-Call Agent.

Connection settings come from environment variables (with local defaults):

    MYSQL_HOST      default 127.0.0.1
    MYSQL_PORT      default 3306
    MYSQL_USER      default root
    MYSQL_PASSWORD  default (empty)
    MYSQL_DB        default sre_agent

Tables are created automatically at startup via init_db().
"""
import logging
import os
import time
from typing import Any, Dict, List, Optional, Tuple

import pymysql

logger = logging.getLogger(__name__)

MYSQL = {
    "host": os.getenv("MYSQL_HOST", "127.0.0.1"),
    "port": int(os.getenv("MYSQL_PORT", "3306")),
    "user": os.getenv("MYSQL_USER", "root"),
    "password": os.getenv("MYSQL_PASSWORD", ""),
    "database": os.getenv("MYSQL_DB", "sre_agent"),
    "charset": "utf8mb4",
    "connect_timeout": int(os.getenv("MYSQL_CONNECT_TIMEOUT", "5")),
    "read_timeout": int(os.getenv("MYSQL_READ_TIMEOUT", "10")),
    "write_timeout": int(os.getenv("MYSQL_WRITE_TIMEOUT", "10")),
}


def conn() -> pymysql.Connection:
    """Open a new connection; rows come back as dicts (DictCursor)."""
    return pymysql.connect(
        host=MYSQL["host"],
        port=MYSQL["port"],
        user=MYSQL["user"],
        password=MYSQL["password"],
        database=MYSQL["database"],
        charset=MYSQL["charset"],
        connect_timeout=MYSQL["connect_timeout"],
        read_timeout=MYSQL["read_timeout"],
        write_timeout=MYSQL["write_timeout"],
        cursorclass=pymysql.cursors.DictCursor,
    )


def query(sql: str, args: Tuple = ()) -> List[Dict[str, Any]]:
    """Run a SELECT and return all rows as a list of dicts."""
    with conn() as c:
        with c.cursor() as cur:
            cur.execute(sql, args)
            rows = cur.fetchall()
    return list(rows)


def execute(sql: str, args: Tuple = ()) -> int:
    """Run an INSERT/UPDATE/DELETE, commit, return affected rows."""
    with conn() as c:
        with c.cursor() as cur:
            affected = cur.execute(sql, args)
        c.commit()
    return affected


def _ensure_index(table: str, index: str, columns: str) -> None:
    """Add a missing index without failing on databases created before it existed."""
    rows = query(
        "SELECT 1 FROM information_schema.statistics "
        "WHERE table_schema = %s AND table_name = %s AND index_name = %s LIMIT 1",
        (MYSQL["database"], table, index),
    )
    if not rows:
        execute(f"CREATE INDEX `{index}` ON `{table}` ({columns})")


def init_db() -> None:
    """Create all tables and indexes if they do not exist yet."""
    execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id            INT          AUTO_INCREMENT PRIMARY KEY,
            username      VARCHAR(64)  UNIQUE NOT NULL,
            password_hash VARCHAR(64)  NOT NULL,
            salt          VARCHAR(32)  NOT NULL,
            question      VARCHAR(200) NOT NULL,
            answer_hash   VARCHAR(64)  NOT NULL,
            created_at    DATETIME     NOT NULL
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """
    )
    execute(
        """
        CREATE TABLE IF NOT EXISTS tokens (
            token      CHAR(64)    PRIMARY KEY,
            username   VARCHAR(64) NOT NULL,
            created_at INT         NOT NULL
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """
    )
    execute(
        """
        CREATE TABLE IF NOT EXISTS reports (
            id          INT          AUTO_INCREMENT PRIMARY KEY,
            incident_id VARCHAR(64)  NOT NULL,
            service     VARCHAR(64),
            severity    VARCHAR(32),
            created_at  DATETIME     NOT NULL,
            payload     LONGTEXT     NOT NULL
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """
    )
    _ensure_index("tokens", "idx_tokens_username", "username")
    _ensure_index("reports", "idx_reports_incident_id", "incident_id")
    _ensure_index("reports", "idx_reports_service", "service")
    _ensure_index("reports", "idx_reports_severity", "severity")
    _ensure_index("reports", "idx_reports_created_at", "created_at")


def init_db_with_retry(
    max_attempts: Optional[int] = None,
    retry_delay: Optional[float] = None,
) -> None:
    """Initialize the schema, tolerating a database that is still starting."""
    attempts = max_attempts or int(os.getenv("DB_INIT_MAX_ATTEMPTS", "30"))
    delay = (
        retry_delay
        if retry_delay is not None
        else float(os.getenv("DB_INIT_RETRY_SECONDS", "2"))
    )
    last_error: Optional[Exception] = None

    for attempt in range(1, attempts + 1):
        try:
            init_db()
            logger.info("Database schema is ready")
            return
        except pymysql.MySQLError as exc:
            last_error = exc
            logger.warning(
                "Database is not ready (attempt %s/%s): %s",
                attempt,
                attempts,
                exc,
            )
            if attempt < attempts:
                time.sleep(delay)

    raise RuntimeError(
        f"Database initialization failed after {attempts} attempts"
    ) from last_error


def is_ready() -> bool:
    """Return whether a simple database query can be completed."""
    try:
        query("SELECT 1 AS ready")
        return True
    except pymysql.MySQLError:
        return False
