"""MySQL Gold 저장소의 연결 설정을 한 곳에서 관리한다."""

from __future__ import annotations

from typing import Any

from django.db import connections


def get_mysql_connection(alias: str = "mysql") -> Any:
    """settings.py의 환경변수 기반 MySQL 연결을 보장하고 반환한다."""

    connection = connections[alias]
    connection.ensure_connection()
    return connection


def ping_mysql(alias: str = "mysql") -> None:
    """MySQL 연결을 확인한다."""

    connection = get_mysql_connection(alias)
    connection.ensure_connection()


def close_mysql_connection(alias: str = "mysql") -> None:
    """지정한 Django 데이터베이스 연결을 닫는다."""

    connections[alias].close()
