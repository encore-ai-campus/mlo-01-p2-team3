"""Bronze에서 Silver까지의 품질 현황을 간단한 JSON으로 만든다."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from typing import Any

from .mongo_storage import get_mongo_database


def _ids(collection, query: dict[str, Any]) -> set[str]:
    """조회 결과에서 비어 있지 않은 bronze_id만 모은다."""

    return {
        str(row["bronze_id"])
        for row in collection.find(query, {"_id": 0, "bronze_id": 1})
        if row.get("bronze_id") is not None
    }


def _code_counts(
    collection,
    query: dict[str, Any],
    list_name: str,
    code_name: str,
) -> dict[str, int]:
    """검토 문서의 지정된 오류 또는 경고 코드만 집계한다."""

    counts: Counter[str] = Counter()
    fields = {"_id": 0, list_name: 1}
    for row in collection.find(query, fields):
        for item in row.get(list_name, []):
            if item.get(code_name):
                counts[item[code_name]] += 1
    return dict(sorted(counts.items()))


def _lineage_warning_counts(rows: list[dict[str, Any]]) -> tuple[int, dict[str, int]]:
    """Silver에 저장된 경고를 계보 링크에서 집계한다."""

    warning_rows: dict[str, dict[str, Any]] = {}
    for row in rows:
        bronze_id = row.get("bronze_id")
        codes = row.get("warning_codes") or []
        if bronze_id is None or not codes:
            continue
        # 같은 Bronze가 재실행으로 여러 번 연결되어도 한 번만 센다.
        warning_rows[str(bronze_id)] = row

    counts: Counter[str] = Counter()
    for row in warning_rows.values():
        for code in row.get("warning_codes", []):
            if code:
                counts[str(code)] += 1
    return len(warning_rows), dict(sorted(counts.items()))


def _percent(value: int, total: int) -> float:
    return round(value * 100 / total, 2) if total else 0.0


def build_silver_quality_report(database=None) -> dict[str, Any]:
    """현재 MongoDB 상태를 읽어 Silver 품질 보고서를 반환한다."""

    # PyMongo Database는 bool() 평가를 지원하지 않는다.
    db = database if database is not None else get_mongo_database()
    bronze = db.hr_bronze_raw_records
    silver = db.hr_silver_standard_records
    reviews = db.hr_review_queue
    lineage = db.hr_lineage_links

    bronze_count = bronze.count_documents({})
    silver_count = silver.count_documents({})
    hard_query = {"blocks_silver": True}
    hard_count = reviews.count_documents(hard_query)
    # Gold 단계에서 명시적으로 만든 검토만 Gold 검토로 센다. 예전
    # Silver 경고 문서는 더 이상 검토 큐의 대상이 아니다.
    gold_review_query = {"review_stage": "GOLD"}
    gold_review_count = reviews.count_documents(gold_review_query)

    lineage_rows = list(lineage.find({}))
    accepted_ids = {
        str(row["bronze_id"])
        for row in lineage_rows
        if row.get("bronze_id") is not None
    }
    silver_warning_count, silver_warning_codes = _lineage_warning_counts(
        lineage_rows
    )
    hard_ids = _ids(reviews, hard_query)
    processed_ids = accepted_ids | hard_ids
    unprocessed_count = max(bronze_count - len(processed_ids), 0)
    overlap_count = len(accepted_ids & hard_ids)
    judged_count = len(accepted_ids) + len(hard_ids)

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "counts": {
            "bronze": bronze_count,
            "silver": silver_count,
            "accepted_bronze": len(accepted_ids),
            "hard_review": hard_count,
            "silver_warning": silver_warning_count,
            "gold_review": gold_review_count,
            # 이전 필드와의 호환을 위해 남기되, 의미는 Gold 검토 건수다.
            "warning_review": gold_review_count,
            "unprocessed_bronze": unprocessed_count,
        },
        "rates": {
            "silver_processing_rate": _percent(len(accepted_ids), judged_count),
            "quarantine_rate": _percent(len(hard_ids), judged_count),
            "warning_rate": _percent(silver_warning_count, len(accepted_ids)),
        },
        "error_code_counts": _code_counts(
            reviews, hard_query, "issues", "error_code"
        ),
        "warning_code_counts": silver_warning_codes,
        "reconciliation": {
            "bronze_equals_processed_plus_unprocessed": (
                bronze_count == len(processed_ids) + unprocessed_count
            ),
            "silver_and_hard_review_overlap": overlap_count,
            "silver_document_count_matches_lineage": silver_count == len(accepted_ids),
        },
        "quality_target": {
            "silver_processing_rate_minimum": 95.0,
            "passed": _percent(len(accepted_ids), judged_count) >= 95.0,
        },
    }
