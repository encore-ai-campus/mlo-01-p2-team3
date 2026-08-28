"""조직·담당자 관계에서 간단한 Gold 피처를 만든다."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from typing import Any
import re
import unicodedata


FEATURE_TABLE = "area_manager_features"
FEATURE_COLUMNS = (
    "area_id",
    "manager_id",
    "top_area_id",
    "organization_type",
    "has_parent",
    "manager_active_yn",
    "generated_at",
)


def _required(record: Mapping[str, Any], field: str) -> Any:
    value = record.get(field)
    if value is None or (isinstance(value, str) and not value.strip()):
        raise ValueError(f"GOLD_FEATURE_REQUIRED_VALUE_MISSING: {field}")
    return value


def _mysql_datetime(value: datetime) -> datetime:
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


def build_feature_rows(
    records: Iterable[Mapping[str, Any]],
    generated_at: datetime | None = None,
) -> list[tuple[Any, ...]]:
    """Silver 한 건에서 현재 조직·담당자 피처 한 행을 만든다."""

    generated = _mysql_datetime(generated_at or datetime.now(timezone.utc))
    rows: dict[Any, tuple[Any, ...]] = {}
    for record in records:
        if not isinstance(record, Mapping):
            raise ValueError("GOLD_FEATURE_RECORD_INVALID: Silver 문서는 객체여야 합니다.")

        area_id = _required(record, "area_id")
        manager_id = _required(record, "manager_id")
        top_area_id = _required(record, "top_area_id")
        active = _required(record, "manager_active_yn")
        if active not in {"Y", "N"}:
            raise ValueError(
                "GOLD_FEATURE_ACTIVE_INVALID: manager_active_yn은 Y 또는 N이어야 합니다."
            )

        is_root = _id_key(area_id) == _id_key(top_area_id)
        row = (
            area_id,
            manager_id,
            top_area_id,
            "TOP" if is_root else "SUB",
            0 if is_root else int(bool(record.get("parent_area_id"))),
            active,
            generated,
        )
        previous = rows.get(area_id)
        if previous is not None and previous[:6] != row[:6]:
            raise ValueError(f"GOLD_FEATURE_CONFLICT: area_id={area_id}")
        rows[area_id] = row
    return list(rows.values())


def create_feature_table(connection: Any) -> None:
    """피처 테이블이 없을 때만 생성한다."""

    statement = """
        CREATE TABLE IF NOT EXISTS area_manager_features (
            area_id VARCHAR(64) NOT NULL,
            manager_id VARCHAR(64) NOT NULL,
            top_area_id VARCHAR(64) NOT NULL,
            organization_type VARCHAR(16) NOT NULL,
            has_parent TINYINT(1) NOT NULL,
            manager_active_yn CHAR(1) NOT NULL,
            generated_at DATETIME NOT NULL,
            PRIMARY KEY (area_id),
            CONSTRAINT fk_features_area
                FOREIGN KEY (area_id) REFERENCES hr_area(area_id),
            CONSTRAINT fk_features_manager
                FOREIGN KEY (manager_id) REFERENCES hr_manager(manager_id),
            CONSTRAINT chk_features_type
                CHECK (organization_type IN ('TOP', 'SUB')),
            CONSTRAINT chk_features_active
                CHECK (manager_active_yn IN ('Y', 'N'))
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """
    with connection.cursor() as cursor:
        cursor.execute(statement)


def upsert_feature_rows(connection: Any, rows: list[tuple[Any, ...]]) -> None:
    if not rows:
        return
    columns = ", ".join(FEATURE_COLUMNS)
    placeholders = ", ".join(["%s"] * len(FEATURE_COLUMNS))
    updates = ", ".join(
        f"{column}=VALUES({column})" for column in FEATURE_COLUMNS[1:]
    )
    statement = (
        f"INSERT INTO {FEATURE_TABLE} ({columns}) VALUES ({placeholders}) "
        f"ON DUPLICATE KEY UPDATE {updates}"
    )
    with connection.cursor() as cursor:
        cursor.executemany(statement, rows)


def save_features(
    connection: Any,
    records: Iterable[Mapping[str, Any]],
    generated_at: datetime | None = None,
) -> int:
    """피처를 만들고 MySQL에 일괄 저장한다."""

    rows = build_feature_rows(records, generated_at=generated_at)
    create_feature_table(connection)
    upsert_feature_rows(connection, rows)
    return len(rows)
