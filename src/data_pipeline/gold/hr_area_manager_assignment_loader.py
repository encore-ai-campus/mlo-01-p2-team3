"""조직별 현재 담당자 배정을 MySQL에 저장한다."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any


ASSIGNMENT_TABLE = "hr_area_manager_assignment"
ASSIGNMENT_COLUMNS = ("area_id", "manager_id")


def _required(record: Mapping[str, Any], field: str) -> Any:
    value = record.get(field)
    if value is None or (isinstance(value, str) and not value.strip()):
        raise ValueError(f"GOLD_REQUIRED_VALUE_MISSING: {field}")
    return value


def build_assignment_rows(
    records: Iterable[Mapping[str, Any]],
) -> list[tuple[Any, ...]]:
    """조직당 한 담당자만 만들며 충돌값을 임의로 고르지 않는다."""

    rows: dict[Any, tuple[Any, ...]] = {}
    for record in records:
        if not isinstance(record, Mapping):
            raise ValueError("GOLD_RECORD_INVALID: Silver 문서는 객체여야 합니다.")
        area_id = _required(record, "area_id")
        row = (area_id, _required(record, "manager_id"))
        previous = rows.get(area_id)
        if previous is not None and previous != row:
            raise ValueError(f"GOLD_ASSIGNMENT_CONFLICT: area_id={area_id}")
        rows[area_id] = row
    return list(rows.values())


def create_assignment_table(connection: Any) -> None:
    statement = """
        CREATE TABLE IF NOT EXISTS hr_area_manager_assignment (
            area_id VARCHAR(64) NOT NULL,
            manager_id VARCHAR(64) NOT NULL,
            PRIMARY KEY (area_id),
            CONSTRAINT fk_assignment_area
                FOREIGN KEY (area_id) REFERENCES hr_area(area_id),
            CONSTRAINT fk_assignment_manager
                FOREIGN KEY (manager_id) REFERENCES hr_manager(manager_id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """
    with connection.cursor() as cursor:
        cursor.execute(statement)


def upsert_assignment_rows(
    connection: Any,
    rows: list[tuple[Any, ...]],
) -> None:
    if not rows:
        return
    columns = ", ".join(ASSIGNMENT_COLUMNS)
    placeholders = ", ".join(["%s"] * len(ASSIGNMENT_COLUMNS))
    statement = (
        f"INSERT INTO {ASSIGNMENT_TABLE} ({columns}) VALUES ({placeholders}) "
        "ON DUPLICATE KEY UPDATE manager_id=VALUES(manager_id)"
    )
    with connection.cursor() as cursor:
        cursor.executemany(statement, rows)


def save_assignments(
    connection: Any,
    records: Iterable[Mapping[str, Any]],
) -> int:
    rows = build_assignment_rows(records)
    create_assignment_table(connection)
    upsert_assignment_rows(connection, rows)
    return len(rows)

