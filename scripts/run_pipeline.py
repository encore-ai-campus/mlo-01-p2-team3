"""API 원본 수집을 시작하는 실행 파일."""

from __future__ import annotations

import os
import json
import re
import sys
from pathlib import Path


# 스크립트를 어느 위치에서 실행해도 프로젝트의 src를 찾도록 설정한다.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
sys.path.insert(0, str(SRC_DIR))

# MongoDB 연결 설정이 Django settings를 사용하므로 먼저 Django를 준비한다.
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django

django.setup()

from data_pipeline.pipeline import PipelineService
from data_pipeline.mongo_storage import PipelinePageRepository, PipelineRunRepository
from data_pipeline.raw_csv import RawCsvRepository
from data_pipeline.raw_archive import RawArchiveRepository
from data_pipeline.transform import load_rule_version


def _safe_error_code(error: Exception) -> str:
    """예외의 원문 메시지 대신 짧은 오류 코드만 반환한다."""

    prefix = str(error).split(":", 1)[0].strip()
    if re.fullmatch(r"[A-Z][A-Z0-9_]{2,80}", prefix):
        return prefix
    return type(error).__name__.upper()


def main() -> int:
    """API에서 받은 원본을 Bronze에 저장한다."""

    try:
        # 주기 실행을 고려해 공개 시각 전에는 대기하지 않고 종료한다.
        result = PipelineService(
            run_repository=PipelineRunRepository(),
            page_repository=PipelinePageRepository(),
            raw_csv_repository=RawCsvRepository(),
            raw_archive_repository=RawArchiveRepository(
                rule_version=load_rule_version()
            ),
        ).collect(
            wait_for_refresh=False,
            scheduler_mode=True,
        )
        # 실행 요약만 로그에 남긴다. 원문·API 키·cursor는 기록하지 않는다.
        log_fields = (
            "status",
            "run_id",
            "pages",
            "saved_rows",
            "deduplicated_rows",
            "api_item_count",
            "next_refresh_at",
        )
        safe_result = {
            key: result[key] for key in log_fields if key in result
        }
        print(json.dumps(safe_result, ensure_ascii=False))
        return 0
    except Exception as error:
        # 원문이 포함될 수 있는 예외 메시지는 기록하지 않는다.
        print(json.dumps({
            "status": "FAILED",
            "error_type": type(error).__name__,
            "error_code": _safe_error_code(error),
        }, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
