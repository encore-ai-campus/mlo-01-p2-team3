"""Silver 조직 문서를 MySQL ``hr_area``로 변환·저장한다."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from typing import Any
import re
import unicodedata


HR_AREA_TABLE = "hr_area"
HR_AREA_COLUMNS = (
    "area_id",
    "area_name",
    "parent_area_id",
    "parent_area_name",
    "top_area_id",
    "top_area_name",
    "top_area_level",
    "registered_at",
)


def _required(record: Mapping[str, Any], field: str) -> Any:
    value = record.get(field)
    if value is None or (isinstance(value, str) and not value.strip()):
        raise ValueError(f"GOLD_REQUIRED_VALUE_MISSING: {field}")
    return value


def _nullable(value: Any) -> Any:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    return value


def _mysql_datetime(value: Any) -> Any:
    if not isinstance(value, datetime):
        return value
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    # Gold 날짜는 초 단위만 저장한다(마이크로초는 사용하지 않음).
    return value.replace(microsecond=0)


def _id_key(value: Any) -> str | None:
    """조직 ID 비교에서 앞뒤·내부 공백과 대소문자 차이를 무시한다."""

    if value in (None, ""):
        return None
    normalized = unicodedata.normalize("NFKC", str(value)).strip()
    return re.sub(r"\s+", "", normalized).casefold()


def build_hr_area_rows(records: Iterable[Mapping[str, Any]]) -> list[tuple[Any, ...]]:
    """조직 ID별 한 행을 만들고 API 표준 레벨을 그대로 보존한다."""

    rows: dict[Any, tuple[Any, ...]] = {}
    for record in records:
        if not isinstance(record, Mapping):
            raise ValueError("GOLD_RECORD_INVALID: Silver 문서는 객체여야 합니다.")
        area_id = _required(record, "area_id")
        top_area_id = _required(record, "top_area_id")
        parent_id = _nullable(record.get("parent_area_id"))
        parent_name = _nullable(record.get("parent_area_name"))
        is_root = _id_key(area_id) == _id_key(top_area_id)
        if is_root:
            parent_id = None
            parent_name = None
        # top_area_level은 Silver에서 표준화한 API 값이다. Gold의 조직
        # 구분(TOP/SUB)은 화면·피처에서 ID 관계로 별도 계산한다.
        top_area_level = _required(record, "top_area_level")
        row = (
            area_id,
            _required(record, "area_name"),
            parent_id,
            parent_name,
            top_area_id,
            _required(record, "top_area_name"),
            top_area_level,
            _mysql_datetime(_nullable(record.get("area_registered_at"))),
        )
        previous = rows.get(area_id)
        if previous is not None and previous != row:
            raise ValueError(f"GOLD_AREA_CONFLICT: area_id={area_id}")
        rows[area_id] = row
    return list(rows.values())


def create_hr_area_table(connection: Any) -> None:
    statement = """
        CREATE TABLE IF NOT EXISTS hr_area (
            area_id VARCHAR(64) NOT NULL,
            area_name VARCHAR(255) NOT NULL,
            parent_area_id VARCHAR(64) NULL,
            parent_area_name VARCHAR(255) NULL,
            top_area_id VARCHAR(64) NOT NULL,
            top_area_name VARCHAR(255) NOT NULL,
            top_area_level VARCHAR(32) NOT NULL,
            registered_at DATETIME NULL,
            PRIMARY KEY (area_id),
            CONSTRAINT chk_hr_area_top_parent_null
                CHECK (area_id <> top_area_id OR parent_area_id IS NULL)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """
    with connection.cursor() as cursor:
        cursor.execute(statement)


def upsert_hr_area_rows(connection: Any, rows: list[tuple[Any, ...]]) -> None:
    if not rows:
        return
    columns = ", ".join(HR_AREA_COLUMNS)
    placeholders = ", ".join(["%s"] * len(HR_AREA_COLUMNS))
    updates = ", ".join(f"{column}=VALUES({column})" for column in HR_AREA_COLUMNS[1:])
    statement = (
        f"INSERT INTO {HR_AREA_TABLE} ({columns}) VALUES ({placeholders}) "
        f"ON DUPLICATE KEY UPDATE {updates}"
    )
    with connection.cursor() as cursor:
        cursor.executemany(statement, rows)


def save_hr_area(connection: Any, records: Iterable[Mapping[str, Any]]) -> int:
    rows = build_hr_area_rows(records)
    create_hr_area_table(connection)
    upsert_hr_area_rows(connection, rows)
    return len(rows)
