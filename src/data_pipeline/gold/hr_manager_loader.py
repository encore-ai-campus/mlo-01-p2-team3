"""Silver 담당자 문서를 MySQL ``hr_manager``로 변환·저장한다."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from typing import Any
import re
import unicodedata


HR_MANAGER_TABLE = "hr_manager"
HR_MANAGER_COLUMNS = (
    "manager_id",
    "manager_name",
    "department_name",
    "position_name",
    "manager_hire_at",
    "manager_active_yn",
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


def _comparison_key(value: Any) -> Any:
    """공백·대소문자만 다른 값은 같은 값으로 비교한다."""

    if not isinstance(value, str):
        return value
    value = unicodedata.normalize("NFKC", value).strip()
    return re.sub(r"\s+", "", value).casefold()


def _unique_value(
    records: Iterable[Mapping[str, Any]],
    field: str,
) -> tuple[Any, str]:
    """필드의 유일값·누락·실제 충돌을 구분한다."""

    values: dict[Any, Any] = {}
    for record in records:
        value = record.get(field)
        if value is None or (isinstance(value, str) and not value.strip()):
            continue
        key = _comparison_key(value)
        try:
            values.setdefault(key, value)
        except TypeError:
            values.setdefault(repr(key), value)
    if not values:
        return None, "missing"
    if len(values) > 1:
        return None, "conflict"
    return next(iter(values.values())), "ok"


def _display_value(field: str, value: Any) -> Any:
    if not isinstance(value, str):
        return value
    value = unicodedata.normalize("NFKC", value).strip()
    if field == "manager_name":
        return re.sub(r"\s+", "", value)
    return value


def build_hr_manager_rows(
    records: Iterable[Mapping[str, Any]],
) -> list[tuple[Any, ...]]:
    """담당자별 한 행을 만든다.

    이름·재직 상태는 유일하게 확인될 때만 적재한다. 직급·입사일·부서명
    같은 선택값이 충돌하면 그 필드만 NULL로 두고, 어느 값도 임의로 고르지
    않는다. 필수값의 실제 충돌은 검토 대상으로 남기기 위해 예외를 낸다.
    """

    groups: dict[Any, list[Mapping[str, Any]]] = {}
    for record in records:
        if not isinstance(record, Mapping):
            raise ValueError("GOLD_RECORD_INVALID: Silver 문서는 객체여야 합니다.")
        manager_id = _required(record, "manager_id")
        groups.setdefault(_comparison_key(manager_id), []).append(record)

    rows: dict[Any, tuple[Any, ...]] = {}
    for manager_key, group in groups.items():
        manager_id, id_state = _unique_value(group, "manager_id")
        if id_state != "ok":
            raise ValueError("GOLD_REQUIRED_VALUE_MISSING: manager_id")

        manager_name, name_state = _unique_value(group, "manager_name")
        active, active_state = _unique_value(group, "manager_active_yn")
        if name_state == "missing":
            raise ValueError("GOLD_REQUIRED_VALUE_MISSING: manager_name")
        if active_state == "missing":
            raise ValueError("GOLD_REQUIRED_VALUE_MISSING: manager_active_yn")
        if name_state == "conflict" or active_state == "conflict":
            raise ValueError(f"GOLD_MANAGER_CONFLICT: manager_id={manager_id}")

        active = str(active).strip().upper()
        if active not in {"Y", "N"}:
            raise ValueError(
                "GOLD_MANAGER_ACTIVE_INVALID: manager_active_yn은 Y 또는 N이어야 합니다."
            )

        optional_values: dict[str, Any] = {}
        for field in ("department_name", "position_name", "manager_hire_at"):
            value, state = _unique_value(group, field)
            # 선택 정보 충돌은 해당 컬럼만 NULL로 처리한다.
            optional_values[field] = (
                _display_value(field, value) if state == "ok" else None
            )

        rows[manager_key] = (
            _display_value("manager_id", manager_id),
            _display_value("manager_name", manager_name),
            _nullable(optional_values["department_name"]),
            _nullable(optional_values["position_name"]),
            _mysql_datetime(_nullable(optional_values["manager_hire_at"])),
            active,
        )
    return list(rows.values())


def create_hr_manager_table(connection: Any) -> None:
    statement = """
        CREATE TABLE IF NOT EXISTS hr_manager (
            manager_id VARCHAR(64) NOT NULL,
            manager_name VARCHAR(255) NOT NULL,
            department_name VARCHAR(255) NULL,
            position_name VARCHAR(255) NULL,
            manager_hire_at DATETIME NULL,
            manager_active_yn CHAR(1) NOT NULL,
            PRIMARY KEY (manager_id),
            CONSTRAINT chk_hr_manager_active
                CHECK (manager_active_yn IN ('Y', 'N'))
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """
    with connection.cursor() as cursor:
        cursor.execute(statement)


def upsert_hr_manager_rows(
    connection: Any,
    rows: list[tuple[Any, ...]],
) -> None:
    if not rows:
        return
    columns = ", ".join(HR_MANAGER_COLUMNS)
    placeholders = ", ".join(["%s"] * len(HR_MANAGER_COLUMNS))
    updates = ", ".join(
        f"{column}=VALUES({column})" for column in HR_MANAGER_COLUMNS[1:]
    )
    statement = (
        f"INSERT INTO {HR_MANAGER_TABLE} ({columns}) VALUES ({placeholders}) "
        f"ON DUPLICATE KEY UPDATE {updates}"
    )
    with connection.cursor() as cursor:
        cursor.executemany(statement, rows)


def save_hr_manager(
    connection: Any,
    records: Iterable[Mapping[str, Any]],
) -> int:
    rows = build_hr_manager_rows(records)
    create_hr_manager_table(connection)
    upsert_hr_manager_rows(connection, rows)
    return len(rows)
