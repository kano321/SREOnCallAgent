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
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

import pymysql
from dbutils.pooled_db import PooledDB, PooledDBError

logger = logging.getLogger(__name__)

MYSQL = {
    "host": os.getenv("MYSQL_HOST", "127.0.0.1"),
    "port": int(os.getenv("MYSQL_PORT", "3306")),
    "user": os.getenv("MYSQL_USER", "root"),
    "password": os.getenv("MYSQL_PASSWORD", ""),
    "database": os.getenv("MYSQL_DB", "sre_agent"),
    "charset": "utf8mb4",
    "cursorclass": pymysql.cursors.DictCursor,
    "connect_timeout": int(os.getenv("MYSQL_CONNECT_TIMEOUT", "5")),
    "read_timeout": int(os.getenv("MYSQL_READ_TIMEOUT", "10")),
    "write_timeout": int(os.getenv("MYSQL_WRITE_TIMEOUT", "10")),
}

POOL = {
    "mincached": int(os.getenv("MYSQL_POOL_MIN_CACHED", "1")),
    "maxcached": int(os.getenv("MYSQL_POOL_MAX_CACHED", "5")),
    "maxconnections": int(os.getenv("MYSQL_POOL_MAX_CONNECTIONS", "10")),
    "blocking": True,
    "ping": 1,
}

_pool: Optional[PooledDB] = None
_pool_lock = threading.Lock()


def get_pool() -> PooledDB:
    """Create the connection pool lazily so imports never contact MySQL."""
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                _pool = PooledDB(
                    creator=pymysql,
                    mincached=POOL["mincached"],
                    maxcached=POOL["maxcached"],
                    maxconnections=POOL["maxconnections"],
                    blocking=POOL["blocking"],
                    ping=POOL["ping"],
                    **MYSQL,
                )
    return _pool


def conn() -> Any:
    """Borrow one connection from the pool."""
    return get_pool().connection()


def close_pool() -> None:
    """Close all pooled connections during application shutdown."""
    global _pool
    with _pool_lock:
        if _pool is not None:
            _pool.close()
            _pool = None


def query(sql: str, args: Tuple = ()) -> List[Dict[str, Any]]:
    """Run a SELECT and return all rows as a list of dicts."""
    c = conn()
    try:
        with c.cursor() as cur:
            cur.execute(sql, args)
            rows = cur.fetchall()
    finally:
        c.close()
    return list(rows)


def execute(sql: str, args: Tuple = ()) -> int:
    """Run an INSERT/UPDATE/DELETE, commit, return affected rows."""
    c = conn()
    try:
        with c.cursor() as cur:
            affected = cur.execute(sql, args)
        c.commit()
    finally:
        c.close()
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


def _ensure_column(table: str, column: str, definition: str) -> None:
    """Add a nullable/defaulted column to an existing table when missing."""
    rows = query(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_schema = %s AND table_name = %s AND column_name = %s LIMIT 1",
        (MYSQL["database"], table, column),
    )
    if not rows:
        execute(f"ALTER TABLE `{table}` ADD COLUMN `{column}` {definition}")


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
            created_at INT         NOT NULL,
            is_hashed  TINYINT(1)  NOT NULL DEFAULT 0
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
            owner_username VARCHAR(64) NOT NULL DEFAULT 'legacy',
            created_at  DATETIME     NOT NULL,
            payload     LONGTEXT     NOT NULL
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """
    )
    _ensure_column("tokens", "is_hashed", "TINYINT(1) NOT NULL DEFAULT 0")
    _ensure_column(
        "reports",
        "owner_username",
        "VARCHAR(64) NOT NULL DEFAULT 'legacy'",
    )
    execute(
        "UPDATE tokens SET token = SHA2(token, 256), is_hashed = 1 "
        "WHERE is_hashed = 0"
    )
    _ensure_index("tokens", "idx_tokens_username", "username")
    _ensure_index("reports", "idx_reports_owner", "owner_username")
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
        except (pymysql.MySQLError, PooledDBError) as exc:
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
    except Exception:
        return False
