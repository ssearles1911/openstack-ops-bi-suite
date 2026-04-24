"""Thin MariaDB access layer, region-aware.

Every query executes against exactly one (region, database) pair. Callers that
need to aggregate across regions do so in Python.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

import pymysql
import pymysql.cursors

from .config import Region


def _conn_params(region: Region, database: Optional[str]) -> Dict[str, Any]:
    return {
        "host": region.host,
        "port": region.port,
        "user": region.user,
        "password": region.password,
        "database": database,
        "charset": "utf8mb4",
        "cursorclass": pymysql.cursors.DictCursor,
    }


def connect(region: Region, database: Optional[str] = None) -> pymysql.connections.Connection:
    return pymysql.connect(**_conn_params(region, database))


def query(
    region: Region,
    database: str,
    sql: str,
    args: Sequence[Any] = (),
) -> List[Dict[str, Any]]:
    conn = connect(region, database)
    try:
        with conn.cursor() as cur:
            cur.execute(sql, args)
            return list(cur.fetchall())
    finally:
        conn.close()
