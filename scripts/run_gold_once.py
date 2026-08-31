"""Silver 사전검증 통과 데이터를 Gold 품질 게이트 뒤 MySQL에 적재한다.

기본 실행은 검사만 한다. 실제 MySQL 변경은 ``--write``를 붙인 경우에만
수행한다.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django

django.setup()

from django.conf import settings  # noqa: E402
from django.db import transaction  # noqa: E402

from data_pipeline.gold_quality import (  # noqa: E402
    build_gold_partial_records,
    build_gold_quality_preview,
    load_gold_rules,
)
from data_pipeline.gold import (  # noqa: E402
    area_manager_features_loader,
    hr_area_loader,
    hr_area_manager_assignment_loader,
    hr_gold_load_batch_loader,
    hr_manager_loader,
)
from data_pipeline.mongo_storage import (  # noqa: E402
    LineageRepository,
    get_mongo_database,
)
from data_pipeline.mysql_storage import get_mysql_connection  # noqa: E402


SILVER_COLLECTION = "hr_silver_standard_records"
MYSQL_ALIAS = "mysql"


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Silver → Gold 1회 실행")
    parser.add_argument(
        "--write",
        action="store_true",
        help="품질 게이트 통과(또는 --partial) 시 실제 MySQL에 저장",
    )
    parser.add_argument(
        "--partial",
        action="store_true",
        help="게이트 실패 시에도 유효한 테이블 행만 분리해 저장",
    )
    parser.add_argument(
        "--run-id",
        help="Gold 적재 이력에 연결할 원천 실행 ID(없으면 Gold 실행 ID 생성)",
    )
    return parser.parse_args()


def _sha256_json(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _ensure_mysql_database() -> None:
    """MySQL 데이터베이스가 없을 때만 서버 연결로 생성한다."""

    import MySQLdb

    config = settings.DATABASES[MYSQL_ALIAS]
    connection = MySQLdb.connect(
        host=config.get("HOST") or "127.0.0.1",
        user=config.get("USER") or "",
        passwd=config.get("PASSWORD") or "",
        port=int(config.get("PORT") or 3306),
        charset="utf8mb4",
    )
    database_name = str(config["NAME"]).replace("`", "``")
    cursor = connection.cursor()
    try:
        cursor.execute(
            f"CREATE DATABASE IF NOT EXISTS `{database_name}` "
            "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
        )
    finally:
        cursor.close()
        connection.close()


def _source_run_id(database, requested: str | None) -> str:
    if requested:
        return requested
    runs = sorted({
        str(row.get("bronze_run_id"))
        for row in database.hr_lineage_links.find(
            {}, {"_id": 0, "bronze_run_id": 1}
        )
        if row.get("bronze_run_id")
    })
    return runs[0] if len(runs) == 1 else f"gold_{uuid4().hex[:16]}"


def _key_text(value: object) -> str | None:
    """계보 키 비교에 사용할 간단한 문자열 표현을 만든다."""

    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _build_gold_lineage_keys(
    all_records: list[dict],
    area_records: list[dict],
    manager_records: list[dict],
    assignment_records: list[dict],
    feature_records: list[dict],
) -> tuple[
    dict[str, dict[str, dict[str, str]]],
    dict[str, str],
]:
    """Gold 키와 이번 적재에 해당하는 Bronze ID를 함께 만든다."""

    area_ids = {
        key
        for key in (_key_text(row.get("area_id")) for row in area_records)
        if key
    }
    manager_ids = {
        key
        for key in (_key_text(row.get("manager_id")) for row in manager_records)
        if key
    }
    assignment_keys = {
        (_key_text(row.get("area_id")), _key_text(row.get("manager_id")))
        for row in assignment_records
    }
    feature_area_ids = {
        key
        for key in (_key_text(row.get("area_id")) for row in feature_records)
        if key
    }

    links: dict[str, dict[str, dict[str, str]]] = {}
    bronze_ids: dict[str, str] = {}
    for record in all_records:
        area_id = _key_text(record.get("area_id"))
        if not area_id:
            continue
        manager_id = _key_text(record.get("manager_id"))
        gold_key: dict[str, dict[str, str]] = {}
        if area_id in area_ids:
            gold_key["hr_area"] = {"area_id": area_id}
        if manager_id in manager_ids:
            gold_key["hr_manager"] = {"manager_id": manager_id}
        if (area_id, manager_id) in assignment_keys:
            gold_key["hr_area_manager_assignment"] = {
                "area_id": area_id,
                "manager_id": manager_id,
            }
        if area_id in feature_area_ids:
            gold_key["area_manager_features"] = {"area_id": area_id}
        if gold_key:
            links[area_id] = gold_key
            bronze_id = _key_text(record.get("bronze_id"))
            if bronze_id:
                bronze_ids[area_id] = bronze_id
    return links, bronze_ids


def load_gold_tier(
    source_run_id: str | None = None,
    partial: bool = False,
) -> dict:
    """Silver를 Gold에 저장한다.

    Silver는 Silver 자체의 오류만 검토 큐로 분리한다. Gold 적재 가능 여부는
    여기서 최종 스키마·PK/FK 품질 게이트로 별도 검증한다.

    기본값은 기존처럼 전체 게이트가 통과해야 저장한다. ``partial=True``면
    정상으로 판정된 조직·관리자·배정·피처만 각각 저장하고 나머지는
    Silver와 검토 큐에 남긴다.
    """

    database = get_mongo_database()
    preview = build_gold_quality_preview(database)
    result = {
        "status": "QUALITY_GATE_FAILED"
        if not preview["quality_gate_passed"]
        else "READY",
        "rule_version": preview["rule_version"],
        "quality_preview": preview,
        "partial": partial,
        "writes_performed": False,
    }
    if not preview["quality_gate_passed"] and not partial:
        return result

    all_records = list(database[SILVER_COLLECTION].find({}, {"_id": 0}))
    if partial:
        selected = build_gold_partial_records(database)
        area_records = selected["area_records"]
        manager_records = selected["manager_records"]
        assignment_records = selected["assignment_records"]
        feature_records = selected["feature_records"]
    else:
        area_records = manager_records = assignment_records = feature_records = all_records
        selected = {
            "source_silver_count": len(all_records),
            "skipped_counts": {},
            "optional_manager_conflicts": 0,
        }

    input_hash = _sha256_json(all_records)
    load_batch_id = str(uuid4())
    run_id = _source_run_id(database, source_run_id)
    started_at = datetime.now(timezone.utc)
    _ensure_mysql_database()
    connection = get_mysql_connection(MYSQL_ALIAS)

    hr_gold_load_batch_loader.create_load_batch_table(connection)
    hr_gold_load_batch_loader.insert_load_batch(
        connection,
        hr_gold_load_batch_loader.build_load_batch_row(
            load_batch_id=load_batch_id,
            run_id=run_id,
            rule_version=load_gold_rules()["rule_version"],
            input_hash=input_hash,
            loaded_count=0,
            status="RUNNING",
            source_silver_count=len(all_records),
            started_at=started_at,
        ),
    )

    try:
        with transaction.atomic(using=MYSQL_ALIAS):
            area_count = hr_area_loader.save_hr_area(connection, area_records)
            manager_count = hr_manager_loader.save_hr_manager(
                connection, manager_records
            )
            assignment_count = hr_area_manager_assignment_loader.save_assignments(
                connection, assignment_records
            )
            feature_count = area_manager_features_loader.save_features(
                connection,
                feature_records,
                generated_at=datetime.now(timezone.utc),
            )
        report = {
            "silver_count": len(all_records),
            "area_count": area_count,
            "manager_count": manager_count,
            "assignment_count": assignment_count,
            "feature_count": feature_count,
            "partial": partial,
            "skipped_counts": selected.get("skipped_counts", {}),
            "optional_manager_conflicts": selected.get(
                "optional_manager_conflicts", 0
            ),
        }
        # MySQL 적재가 끝난 뒤 Bronze–Silver 계보에 Gold 키를 연결한다.
        # 두 저장소는 하나의 트랜잭션으로 묶을 수 없으므로 연결 건수와
        # 오류를 적재 보고서에 함께 남긴다.
        try:
            gold_lineage_keys, bronze_id_by_area = _build_gold_lineage_keys(
                all_records,
                area_records,
                manager_records,
                assignment_records,
                feature_records,
            )
            report["lineage_candidate_count"] = len(gold_lineage_keys)
            report["lineage_linked_count"] = LineageRepository(
                database.hr_lineage_links
            ).attach_gold(
                load_batch_id=load_batch_id,
                gold_keys_by_area=gold_lineage_keys,
                rule_version=load_gold_rules()["rule_version"],
                bronze_id_by_area=bronze_id_by_area,
            )
            report["lineage_unlinked_count"] = max(
                report["lineage_candidate_count"]
                - report["lineage_linked_count"],
                0,
            )
            report["lineage_status"] = (
                "PASS"
                if report["lineage_unlinked_count"] == 0
                else "WARNING"
            )
        except Exception as lineage_error:
            report["lineage_linked_count"] = 0
            report["lineage_unlinked_count"] = report.get(
                "lineage_candidate_count", 0
            )
            report["lineage_status"] = "FAILED"
            report["lineage_error"] = (
                f"{type(lineage_error).__name__}: {lineage_error}"
            )
        skipped_total = sum(
            int(value)
            for value in report.get("skipped_counts", {}).values()
            if isinstance(value, (int, float))
        )
        hr_gold_load_batch_loader.update_load_batch(
            connection,
            load_batch_id=load_batch_id,
            loaded_count=sum(
                report[field]
                for field in (
                    "area_count",
                    "manager_count",
                    "assignment_count",
                    "feature_count",
                )
            ),
            # 적재 이력 테이블은 기존 상태값(RUNNING/SUCCEEDED/FAILED)을
            # 유지한다. 부분 적재 여부와 제외 건수는 report에 기록한다.
            status="SUCCEEDED",
            report_hash=_sha256_json(report),
            source_silver_count=len(all_records),
            skipped_count=skipped_total,
            finished_at=datetime.now(timezone.utc),
            report=report,
        )
        status = (
            "SUCCEEDED_WITH_QUARANTINE"
            if partial and any(report["skipped_counts"].values())
            else "SUCCEEDED"
        )
        result.update({
            "status": status,
            "load_batch_id": load_batch_id,
            "run_id": run_id,
            "counts": report,
            "writes_performed": True,
        })
        return result
    except Exception as error:
        hr_gold_load_batch_loader.update_load_batch(
            connection,
            load_batch_id=load_batch_id,
            loaded_count=0,
            status="FAILED",
            report_hash=_sha256_json({"error_type": type(error).__name__}),
            source_silver_count=len(all_records),
            finished_at=datetime.now(timezone.utc),
            report={"error_type": type(error).__name__, "error": str(error)},
        )
        raise


def main() -> int:
    args = _arguments()
    try:
        if args.write:
            result = load_gold_tier(args.run_id, partial=args.partial)
        else:
            result = {
                "status": "CHECK_ONLY",
                "quality_preview": build_gold_quality_preview(),
                "writes_performed": False,
            }
            if args.partial:
                selected = build_gold_partial_records()
                result["partial_selection"] = {
                    "source_silver_count": selected["source_silver_count"],
                    "eligible_counts": {
                        "hr_area": len(selected["area_records"]),
                        "hr_manager": len(selected["manager_records"]),
                        "hr_area_manager_assignment": len(
                            selected["assignment_records"]
                        ),
                        "area_manager_features": len(selected["feature_records"]),
                    },
                    "skipped_counts": selected["skipped_counts"],
                    "optional_manager_conflicts": selected[
                        "optional_manager_conflicts"
                    ],
                }
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return 0 if result["status"] in {
            "CHECK_ONLY",
            "SUCCEEDED",
            "SUCCEEDED_WITH_QUARANTINE",
            "READY",
        } else 2
    except Exception as error:
        print(json.dumps({
            "status": "FAILED",
            "error_type": type(error).__name__,
            "error_message": str(error),
            "writes_performed": False,
        }, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
