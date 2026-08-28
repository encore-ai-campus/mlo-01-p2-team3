"""MySQL 읽기 쿼리를 딕셔너리 형태로 반환하는 공통 함수."""

from __future__ import annotations

from typing import Any

from django.db import connections


MYSQL_ALIAS = "mysql"


def fetch_all(sql: str, params: list[Any] | tuple[Any, ...] | None = None) -> list[dict[str, Any]]:
    """Gold MySQL에서 여러 행을 읽는다."""

    with connections[MYSQL_ALIAS].cursor() as cursor:
        cursor.execute(sql, params or [])
        columns = [column[0] for column in cursor.description or ()]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]


def fetch_one(sql: str, params: list[Any] | tuple[Any, ...] | None = None) -> dict[str, Any] | None:
    """Gold MySQL에서 첫 행을 읽는다."""

    rows = fetch_all(sql, params)
    return rows[0] if rows else None
