"""Gold 적재 실행 이력 ``hr_gold_load_batch``를 관리한다."""

from __future__ import annotations

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


def build_load_batch_row(
    load_batch_id: Any,
    run_id: Any,
    rule_version: Any,
    input_hash: Any,
    loaded_count: Any,
    status: Any,
    report_hash: Any = None,
) -> tuple[Any, ...]:
    if not isinstance(loaded_count, int) or isinstance(loaded_count, bool) or loaded_count < 0:
        raise ValueError("GOLD_LOAD_BATCH_COUNT_INVALID: loaded_count는 0 이상 정수여야 합니다.")
    status_text = _required_text(status, "status").upper()
    if status_text not in LOAD_BATCH_STATUSES:
        raise ValueError("GOLD_LOAD_BATCH_STATUS_INVALID: status가 올바르지 않습니다.")
    return (
        _required_text(load_batch_id, "load_batch_id"),
        _required_text(run_id, "run_id"),
        _required_text(rule_version, "rule_version"),
        _required_sha256(input_hash, "input_hash"),
        loaded_count,
        status_text,
        _optional_sha256(report_hash, "report_hash"),
    )


def create_load_batch_table(connection: Any) -> None:
    statement = """
        CREATE TABLE IF NOT EXISTS hr_gold_load_batch (
            load_batch_id VARCHAR(64) NOT NULL,
            run_id VARCHAR(64) NOT NULL,
            rule_version VARCHAR(64) NOT NULL,
            input_hash CHAR(64) NOT NULL,
            loaded_count INT UNSIGNED NOT NULL DEFAULT 0,
            status VARCHAR(16) NOT NULL,
            report_hash CHAR(64) NULL,
            PRIMARY KEY (load_batch_id),
            CONSTRAINT chk_hr_gold_load_batch_status
                CHECK (status IN ('RUNNING', 'SUCCEEDED', 'FAILED'))
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """
    with connection.cursor() as cursor:
        cursor.execute(statement)


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
) -> None:
    row = build_load_batch_row(
        load_batch_id=load_batch_id,
        run_id="batch-update",
        rule_version="batch-update",
        input_hash="0" * 64,
        loaded_count=loaded_count,
        status=status,
        report_hash=report_hash,
    )
    statement = """
        UPDATE hr_gold_load_batch
        SET loaded_count = %s,
            status = %s,
            report_hash = %s
        WHERE load_batch_id = %s
    """
    with connection.cursor() as cursor:
        cursor.execute(statement, (row[4], row[5], row[6], row[0]))

