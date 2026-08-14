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
import os
from typing import Any, Dict, List, Tuple

import pymysql

MYSQL = {
    "host": os.getenv("MYSQL_HOST", "127.0.0.1"),
    "port": int(os.getenv("MYSQL_PORT", "3306")),
    "user": os.getenv("MYSQL_USER", "root"),
    "password": os.getenv("MYSQL_PASSWORD", ""),
    "database": os.getenv("MYSQL_DB", "sre_agent"),
    "charset": "utf8mb4",
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


def init_db() -> None:
    """Create all tables if they do not exist yet."""
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