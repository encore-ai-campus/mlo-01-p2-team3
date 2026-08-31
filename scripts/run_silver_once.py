"""Bronze 원문을 정규화·검토하고 Silver에 저장하는 실행 파일.

Silver 단계에서는 Silver 자체의 정규화·도메인·관계 오류만 격리한다.
Gold 전용 경고와 품질 게이트는 Gold 단계의 보고서에서 별도로 처리한다.

기본 실행은 미리보기이며, 실제 저장은 ``--write``를 붙였을 때만 수행한다.
API를 호출하지 않고 이미 저장된 Bronze 문서만 읽는다.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# 스크립트를 어느 위치에서 실행해도 src 패키지를 찾도록 설정한다.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
sys.path.insert(0, str(SRC_DIR))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django

django.setup()

from data_pipeline.transform import (
    load_domain_rule,
    load_rule_version,
    normalize_bronze_record,
)
from data_pipeline.mongo_storage import (
    LineageRepository,
    ReviewQueueRepository,
    SilverRepository,
    get_mongo_database,
)
from data_pipeline.review import review_records


DEFAULT_BATCH_SIZE = 1000


def _parse_args() -> argparse.Namespace:
    """확인할 Bronze 건수와 실제 저장 여부를 받는다."""

    parser = argparse.ArgumentParser(
        description="Bronze → Silver 1회 확인(Silver 검증만 수행)"
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="읽을 Bronze 원문 건수(일반 실행 기본값: 1, --pending 기본값: 1000)",
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="Silver 검토를 통과한 결과를 MongoDB에 저장",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Bronze 전체를 읽어 저장 없이 점검",
    )
    parser.add_argument(
        "--latest",
        type=int,
        default=None,
        help="가장 최근 Bronze 원문부터 읽을 건수",
    )
    parser.add_argument(
        "--pending",
        action="store_true",
        help="Silver·검토 큐에 아직 기록되지 않은 Bronze만 읽는다",
    )
    parser.add_argument(
        "--drain",
        action="store_true",
        help="미처리 Bronze를 1,000건씩 모두 처리한다(--pending --write 전용)",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=DEFAULT_BATCH_SIZE,
        help="--drain에서 한 번에 처리할 건수(기본값: 1000)",
    )
    args = parser.parse_args()
    if args.limit is not None and args.limit < 1:
        parser.error("--limit은 1 이상이어야 합니다.")
    if args.latest is not None and args.latest < 1:
        parser.error("--latest는 1 이상이어야 합니다.")
    if args.all and args.write:
        parser.error("--all과 --write는 함께 사용할 수 없습니다.")
    if args.all and (args.latest is not None or args.pending):
        parser.error("--all은 --latest 또는 --pending과 함께 사용할 수 없습니다.")
    if args.latest is not None and args.pending:
        parser.error("--latest와 --pending은 함께 사용할 수 없습니다.")
    if args.batch_size < 1:
        parser.error("--batch-size는 1 이상이어야 합니다.")
    if args.drain and not args.pending:
        parser.error("--drain은 --pending과 함께 사용해야 합니다.")
    if args.drain and not args.write:
        parser.error("--drain은 --write와 함께 사용해야 합니다.")
    if args.drain and args.limit is not None:
        parser.error("--drain에서는 --limit 대신 --batch-size를 사용합니다.")
    return args


def _processed_bronze_ids(database: Any) -> set[str]:
    """이미 Silver 또는 검토 큐에 기록된 Bronze ID를 모은다."""

    processed: set[str] = set()
    for collection_name in ("hr_lineage_links", "hr_review_queue"):
        collection = database[collection_name]
        try:
            records = collection.find(
                {"bronze_id": {"$exists": True}},
                {"bronze_id": 1},
            )
        except TypeError:
            # projection을 지원하지 않는 테스트용 collection도 처리한다.
            records = collection.find({})
        for record in records:
            bronze_id = record.get("bronze_id") if isinstance(record, dict) else None
            if bronze_id is not None:
                processed.add(str(bronze_id))
    return processed


def _check_bronze(
    limit: int | None,
    latest: bool = False,
    pending: bool = False,
) -> dict[str, Any]:
    """Bronze를 읽어 정규화 결과와 매핑 오류를 만든다."""

    database = get_mongo_database()
    collection = database["hr_bronze_raw_records"]
    query = collection.find({})
    if latest:
        query = query.sort("_id", -1)
    elif pending:
        # 미처리 backlog는 오래 저장된 Bronze부터 순서대로 처리한다.
        query = query.sort("_id", 1)

    processed_ids = _processed_bronze_ids(database) if pending else set()

    normalized: list[dict[str, Any]] = []
    mapping_quarantine: list[dict[str, Any]] = []
    for bronze_record in query:
        if pending and str(bronze_record.get("_id")) in processed_ids:
            continue

        if limit is not None and len(normalized) + len(mapping_quarantine) >= limit:
            break

        source = {
            "bronze_id": str(bronze_record.get("_id")),
            "source_record_id": bronze_record.get("record_id"),
            "source_record_sha256": bronze_record.get("source_record_sha256"),
            "bronze_run_id": bronze_record.get("run_id"),
        }
        try:
            # Bronze 원문은 그대로 두고, 한 건씩 메모리에서만 변환한다.
            result = normalize_bronze_record(bronze_record)
            result.update(source)
            normalized.append(result)
        except (TypeError, ValueError) as error:
            # 매핑 자체가 불가능한 원문도 삭제하지 않고 검토 큐로 보낸다.
            error_text = str(error)
            mapping_quarantine.append({
                **source,
                "record": {},
                "issues": [{
                    "field": "record",
                    "error_code": error_text.split(":", 1)[0],
                    "value": error_text,
                }],
                "status": "REVIEW_REQUIRED",
                "failure_stage": "NORMALIZATION",
            })

    return {
        "bronze_read": len(normalized) + len(mapping_quarantine),
        "normalized": len(normalized),
        "mapping_or_normalization_failed": len(mapping_quarantine),
        "normalized_records": normalized,
        "mapping_quarantine": mapping_quarantine,
    }


def _load_known_area_ids(
    bronze_collection: Any,
    silver_collection: Any | None = None,
) -> set[str]:
    """Bronze와 승인된 Silver에서 부모 부서 존재 여부를 확인한다."""

    known: set[str] = set()
    try:
        records = bronze_collection.find({}, {"payload.area_no": 1})
    except TypeError:
        # 테스트용 가짜 collection처럼 projection을 지원하지 않는 경우다.
        records = bronze_collection.find({})
    for record in records:
        payload = record.get("payload") if isinstance(record, dict) else None
        area_id = payload.get("area_no") if isinstance(payload, dict) else None
        if isinstance(area_id, str) and area_id.strip():
            known.add(re.sub(r"[-_\s]", "", area_id.upper()))

    if silver_collection is not None:
        try:
            records = silver_collection.find({}, {"area_id": 1})
        except TypeError:
            records = silver_collection.find({})
        for record in records:
            area_id = record.get("area_id") if isinstance(record, dict) else None
            if isinstance(area_id, str) and area_id.strip():
                known.add(re.sub(r"[-_\s]", "", area_id.upper()))
    return known


def _lineage_links(
    accepted_items: list[dict[str, Any]],
    processing_run_id: str,
) -> list[dict[str, Any]]:
    """Silver에 저장한 레코드의 Bronze 연결 문서를 만든다."""

    now = datetime.now(timezone.utc)
    rule_version = load_rule_version()
    links: list[dict[str, Any]] = []
    for item in accepted_items:
        record = item.get("record", {})
        link: dict[str, Any] = {
            "processing_run_id": processing_run_id,
            "bronze_id": item.get("bronze_id"),
            "silver_collection": SilverRepository.COLLECTION_NAME,
            "silver_key": {"area_id": record.get("area_id")},
            "result": "SILVER_SAVED",
            "quality_status": item.get("status", "PASS"),
            "rule_version": rule_version,
            "processed_at": now,
        }
        warnings = list(item.get("warnings", []))
        if warnings:
            link["warning_codes"] = sorted({
                str(warning.get("warning_code"))
                for warning in warnings
                if warning.get("warning_code")
            })
            link["blocks_gold"] = sorted({
                target
                for warning in warnings
                for target in warning.get("blocks_gold", [])
            })
        for field in (
            "bronze_run_id",
            "source_record_id",
            "source_record_sha256",
        ):
            if item.get(field) is not None:
                link[field] = item[field]
        links.append(link)
    return links


def _load_existing_silver(
    collection: Any,
    normalized_records: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """현재 배치와 관련된 Silver만 조회한다."""

    area_ids: set[str] = set()
    manager_ids: set[str] = set()
    for item in normalized_records:
        record = item.get("record", {})
        if record.get("area_id"):
            area_ids.add(str(record["area_id"]))
        if record.get("manager_id"):
            manager_ids.add(str(record["manager_id"]))

    if not area_ids and not manager_ids:
        return []
    query_parts: list[dict[str, Any]] = []
    if area_ids:
        query_parts.append({"area_id": {"$in": list(area_ids)}})
    if manager_ids:
        query_parts.append({"manager_id": {"$in": list(manager_ids)}})
    return list(collection.find({"$or": query_parts}))


def _save_batch(
    result: dict[str, Any],
    database: Any,
    silver_repository: SilverRepository | None,
    known_area_ids: set[str],
    batch_no: int,
) -> tuple[int, int, int]:
    """정규화 결과 한 묶음을 Silver 또는 검토 큐에 저장한다."""

    silver_collection = (
        silver_repository.collection
        if silver_repository is not None
        else database[SilverRepository.COLLECTION_NAME]
    )
    # 현재 Silver 값과 비교해 변경·중복을 검토한다.
    existing_silver = _load_existing_silver(
        silver_collection,
        result["normalized_records"],
    )
    reviewed = review_records(
        result["normalized_records"],
        existing_records=existing_silver,
        known_area_ids=known_area_ids,
        # Gold 전용 조건은 Silver 검토 큐로 올리지 않는다.
        gold_preflight=False,
    )
    quarantine = reviewed["quarantine"] + result["mapping_quarantine"]
    processing_run_id = (
        f"silver_check_{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%f')}"
        f"_{batch_no:04d}"
    )

    if silver_repository is not None and reviewed["accepted"]:
        # 경고가 있는 행도 Silver에는 저장한다. 경고 상세는 계보 링크에
        # 기록하고, 검토 큐에는 실제 Silver 차단 오류만 넣는다.
        silver_repository.save_records(reviewed["accepted"])
        LineageRepository().save_links(
            _lineage_links(reviewed["accepted_items"], processing_run_id)
        )
    if silver_repository is not None:
        if quarantine:
            ReviewQueueRepository().enqueue(
                quarantine,
                run_id=processing_run_id,
            )

    return (
        reviewed["accepted_count"],
        len(quarantine),
        reviewed.get("warning_count", 0),
    )


def main() -> int:
    """정규화·검토 통과 건만 Silver에 저장한다."""

    args = _parse_args()
    latest = args.latest is not None
    if args.drain:
        # 전체 적재도 메모리에는 1,000건씩만 올린다.
        read_limit = args.batch_size
    elif args.all:
        read_limit = args.limit
    elif args.pending:
        # 일반 스케줄러 실행은 기본 1,000건만 처리한다.
        read_limit = args.limit or DEFAULT_BATCH_SIZE
    else:
        read_limit = args.latest or args.limit or 1

    database = get_mongo_database()
    silver_repository = SilverRepository() if args.write else None
    relationship_rules = load_domain_rule("organization_relationship")
    reference_sources = {
        str(source)
        for source in relationship_rules.get("reference_sources", [])
        if isinstance(source, str)
    }
    bronze_collection = database["hr_bronze_raw_records"]
    approved_silver = (
        database[SilverRepository.COLLECTION_NAME]
        if "approved_silver" in reference_sources
        else None
    )
    known_area_ids = _load_known_area_ids(
        bronze_collection,
        approved_silver,
    )
    total_read = 0
    total_accepted = 0
    total_quarantine = 0
    total_warnings = 0
    batch_no = 0

    while True:
        result = _check_bronze(
            read_limit,
            latest=latest,
            pending=args.pending,
        )
        if result["bronze_read"] == 0:
            break

        batch_no += 1
        accepted_count, quarantine_count, warning_count = _save_batch(
            result,
            database,
            silver_repository,
            known_area_ids,
            batch_no,
        )
        total_read += result["bronze_read"]
        total_accepted += accepted_count
        total_quarantine += quarantine_count
        total_warnings += warning_count
        print(
            f"배치 {batch_no}: 읽음 {result['bronze_read']}건, "
            f"정제 {accepted_count}건, 검토 {quarantine_count}건, "
            f"경고 {warning_count}건",
            flush=True,
        )

        # --drain이 아니면 한 번의 실행으로 한 배치만 처리한다.
        if not args.drain:
            break

    print(f"정제된 건수: {total_accepted}", flush=True)
    print(f"정제되지 않은 건수: {total_quarantine}", flush=True)
    print(f"경고 건수: {total_warnings}", flush=True)
    if args.drain:
        print(f"전체 읽은 건수: {total_read}", flush=True)
    # PowerShell 스케줄러는 한글 출력이 아닌 이 JSON 상태값으로 성공을
    # 판단한다. 키와 상태값은 인코딩 영향을 받지 않도록 영문만 사용한다.
    print(json.dumps({
        "status": "SUCCEEDED",
        "read_count": total_read,
        "accepted_count": total_accepted,
        "review_count": total_quarantine,
        "warning_count": total_warnings,
    }, ensure_ascii=True), flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        # Silver 오류가 원문 payload와 함께 로그에 남지 않도록 코드만 출력한다.
        print(json.dumps({
            "status": "FAILED",
            "error_type": type(error).__name__,
        }, ensure_ascii=False))
        raise SystemExit(1)
