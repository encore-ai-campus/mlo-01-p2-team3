"""기존 메타데이터를 현재 저장 규칙으로 맞추는 일회성 명령이다.

기본 실행은 상태만 확인한다. ``--write``를 붙이면

* 기존 페이지의 cursor 원문을 SHA-256으로 바꾸고 원문을 제거하고
* ``hr_gold_load_batch``에 현재 필요한 이력 컬럼을 추가한다.

Gold 계보는 Gold를 다시 적재할 때 함께 연결하므로, 이 명령은 Gold 행을
변경하지 않는다.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django

django.setup()

from data_pipeline.gold.hr_gold_load_batch_loader import (  # noqa: E402
    LOAD_BATCH_TABLE,
    create_load_batch_table,
)
from data_pipeline.mongo_storage import (  # noqa: E402
    PipelinePageRepository,
    get_mongo_database,
)
from data_pipeline.mysql_storage import (  # noqa: E402
    close_mysql_connection,
    get_mysql_connection,
)


def _legacy_page_count(database: Any) -> int:
    """cursor 원문이 아직 남은 페이지 수를 반환한다."""

    return database[PipelinePageRepository.COLLECTION_NAME].count_documents({
        "$or": [
            {"cursor": {"$exists": True}},
            {"next_cursor": {"$exists": True}},
        ]
    })


def _missing_batch_columns(connection: Any) -> list[str]:
    """현재 Gold 이력 테이블에 없는 필드를 찾는다."""

    required = {
        "source_silver_count",
        "skipped_count",
        "started_at",
        "finished_at",
        "report_json",
    }
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT COLUMN_NAME
            FROM information_schema.COLUMNS
            WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s
            """,
            (LOAD_BATCH_TABLE,),
        )
        existing = {str(row[0]) for row in cursor.fetchall()}
    return sorted(required - existing)


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="기존 메타데이터 정리")
    parser.add_argument(
        "--write",
        action="store_true",
        help="페이지 cursor를 해시로 변경하고 Gold 이력 컬럼을 추가",
    )
    return parser.parse_args()


def main() -> int:
    args = _arguments()
    database = get_mongo_database()
    pages = database[PipelinePageRepository.COLLECTION_NAME]
    result: dict[str, Any] = {
        "status": "CHECK_ONLY",
        "legacy_page_count": _legacy_page_count(database),
    }

    connection = get_mysql_connection("mysql")
    try:
        result["missing_gold_batch_columns"] = _missing_batch_columns(connection)
        if args.write:
            result["page_migration"] = (
                PipelinePageRepository(pages).migrate_legacy_cursor_fields()
            )
            # CREATE와 ALTER를 한 함수에서 처리해 신규/기존 DB를 모두 지원한다.
            result["added_gold_batch_columns"] = create_load_batch_table(connection)
            result["status"] = "UPDATED"
    finally:
        close_mysql_connection("mysql")

    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
