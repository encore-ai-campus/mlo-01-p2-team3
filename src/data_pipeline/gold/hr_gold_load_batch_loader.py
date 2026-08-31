"""Gold 적재 실행 이력 ``hr_gold_load_batch``를 관리한다."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
import json
import re
from typing import Any


LOAD_BATCH_TABLE = "hr_gold_load_batch"
LOAD_BATCH_COLUMNS = (
    "load_batch_id",
    "run_id",
    "rule_version",
    "input_hash",
    "loaded_count",
    "status",
    "report_hash",
    "source_silver_count",
    "skipped_count",
    "started_at",
    "finished_at",
    "report_json",
)
LOAD_BATCH_STATUSES = {"RUNNING", "SUCCEEDED", "FAILED"}
SHA256_PATTERN = re.compile(r"^[0-9a-fA-F]{64}$")


def _required_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"GOLD_LOAD_BATCH_REQUIRED_VALUE_MISSING: {field}")
    return value.strip()


def _required_sha256(value: Any, field: str) -> str:
    text = _required_text(value, field)
    if SHA256_PATTERN.fullmatch(text) is None:
        raise ValueError(f"GOLD_LOAD_BATCH_HASH_INVALID: {field}")
    return text.lower()


def _optional_sha256(value: Any, field: str) -> str | None:
    return None if value is None else _required_sha256(value, field)


def _count(value: Any, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(
            f"GOLD_LOAD_BATCH_COUNT_INVALID: {field}는 0 이상 정수여야 합니다."
        )
    return value


def _mysql_datetime(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, datetime):
        raise ValueError("GOLD_LOAD_BATCH_DATETIME_INVALID: datetime이 필요합니다.")
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value.replace(microsecond=0)


def _report_json(value: Mapping[str, Any] | str | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def build_load_batch_row(
    load_batch_id: Any,
    run_id: Any,
    rule_version: Any,
    input_hash: Any,
    loaded_count: Any,
    status: Any,
    report_hash: Any = None,
    source_silver_count: Any = 0,
    skipped_count: Any = 0,
    started_at: datetime | None = None,
    finished_at: datetime | None = None,
    report: Mapping[str, Any] | str | None = None,
) -> tuple[Any, ...]:
    status_text = _required_text(status, "status").upper()
    if status_text not in LOAD_BATCH_STATUSES:
        raise ValueError("GOLD_LOAD_BATCH_STATUS_INVALID: status가 올바르지 않습니다.")
    return (
        _required_text(load_batch_id, "load_batch_id"),
        _required_text(run_id, "run_id"),
        _required_text(rule_version, "rule_version"),
        _required_sha256(input_hash, "input_hash"),
        _count(loaded_count, "loaded_count"),
        status_text,
        _optional_sha256(report_hash, "report_hash"),
        _count(source_silver_count, "source_silver_count"),
        _count(skipped_count, "skipped_count"),
        _mysql_datetime(started_at),
        _mysql_datetime(finished_at),
        _report_json(report),
    )


def create_load_batch_table(connection: Any) -> list[str]:
    """Gold 실행 이력 테이블을 만들고 누락된 컬럼을 추가한다."""

    statement = """
        CREATE TABLE IF NOT EXISTS hr_gold_load_batch (
            load_batch_id VARCHAR(64) NOT NULL,
            run_id VARCHAR(64) NOT NULL,
            rule_version VARCHAR(64) NOT NULL,
            input_hash CHAR(64) NOT NULL,
            loaded_count INT UNSIGNED NOT NULL DEFAULT 0,
            status VARCHAR(16) NOT NULL,
            report_hash CHAR(64) NULL,
            source_silver_count INT UNSIGNED NOT NULL DEFAULT 0,
            skipped_count INT UNSIGNED NOT NULL DEFAULT 0,
            started_at DATETIME NULL,
            finished_at DATETIME NULL,
            report_json JSON NULL,
            PRIMARY KEY (load_batch_id),
            CONSTRAINT chk_hr_gold_load_batch_status
                CHECK (status IN ('RUNNING', 'SUCCEEDED', 'FAILED'))
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """
    with connection.cursor() as cursor:
        cursor.execute(statement)
        # 이미 생성된 프로젝트 DB에도 새 이력 필드를 추가한다.
        cursor.execute(
            """
            SELECT COLUMN_NAME
            FROM information_schema.COLUMNS
            WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s
            """,
            (LOAD_BATCH_TABLE,),
        )
        existing = {str(row[0]) for row in cursor.fetchall()}
        columns = {
            "source_silver_count":
                "INT UNSIGNED NOT NULL DEFAULT 0",
            "skipped_count": "INT UNSIGNED NOT NULL DEFAULT 0",
            "started_at": "DATETIME NULL",
            "finished_at": "DATETIME NULL",
            "report_json": "JSON NULL",
        }
        added: list[str] = []
        for name, definition in columns.items():
            if name not in existing:
                cursor.execute(
                    f"ALTER TABLE {LOAD_BATCH_TABLE} ADD COLUMN {name} {definition}"
                )
                added.append(name)
        return added


def insert_load_batch(connection: Any, row: tuple[Any, ...]) -> None:
    columns = ", ".join(LOAD_BATCH_COLUMNS)
    placeholders = ", ".join(["%s"] * len(LOAD_BATCH_COLUMNS))
    statement = f"INSERT INTO {LOAD_BATCH_TABLE} ({columns}) VALUES ({placeholders})"
    with connection.cursor() as cursor:
        cursor.execute(statement, row)


def update_load_batch(
    connection: Any,
    load_batch_id: Any,
    loaded_count: Any,
    status: Any,
    report_hash: Any = None,
    source_silver_count: Any | None = None,
    skipped_count: Any | None = None,
    finished_at: datetime | None = None,
    report: Mapping[str, Any] | str | None = None,
) -> None:
    source_count = 0 if source_silver_count is None else source_silver_count
    skipped = 0 if skipped_count is None else skipped_count
    row = build_load_batch_row(
        load_batch_id=load_batch_id,
        run_id="batch-update",
        rule_version="batch-update",
        input_hash="0" * 64,
        loaded_count=loaded_count,
        status=status,
        report_hash=report_hash,
        source_silver_count=source_count,
        skipped_count=skipped,
        finished_at=finished_at,
        report=report,
    )
    assignments = [
        "loaded_count = %s",
        "status = %s",
        "report_hash = %s",
    ]
    values: list[Any] = [row[4], row[5], row[6]]
    if finished_at is not None:
        assignments.append("finished_at = %s")
        values.append(row[10])
    if source_silver_count is not None:
        assignments.append("source_silver_count = %s")
        values.append(row[7])
    if skipped_count is not None:
        assignments.append("skipped_count = %s")
        values.append(row[8])
    if report is not None:
        assignments.append("report_json = %s")
        values.append(row[11])
    statement = (
        f"UPDATE {LOAD_BATCH_TABLE} SET {', '.join(assignments)} "
        "WHERE load_batch_id = %s"
    )
    values.append(row[0])
    with connection.cursor() as cursor:
        cursor.execute(statement, values)
