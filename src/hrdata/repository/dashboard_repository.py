"""Gold 적재 이력 조회."""

from __future__ import annotations

from typing import Any

from django.db import DatabaseError

from .base import fetch_one


class DashboardRepository:
    """현재 프로젝트의 ``hr_gold_load_batch``만 읽는다."""

    def find_latest_batch(self) -> dict[str, Any] | None:
        # UUID인 load_batch_id의 사전순은 실행 순서가 아니므로
        # started_at을 기준으로 최신 배치를 선택한다.
        try:
            return fetch_one(
                """
                SELECT load_batch_id, run_id, rule_version, input_hash,
                       loaded_count, loaded_count AS loaded_row_count,
                       source_silver_count, skipped_count,
                       started_at, finished_at, report_json,
                       status, report_hash
                FROM hr_gold_load_batch
                WHERE status IN ('SUCCEEDED', 'SUCCESS')
                ORDER BY started_at DESC, load_batch_id DESC
                LIMIT 1
                """
            )
        except DatabaseError:
            # 새 컬럼을 아직 추가하지 않은 기존 DB도 화면에서 읽을 수 있게
            # 구 스키마로 한 번 더 조회한다.
            return fetch_one(
                """
                SELECT load_batch_id, run_id, rule_version, input_hash,
                       loaded_count, loaded_count AS loaded_row_count,
                       status, report_hash
                FROM hr_gold_load_batch
                WHERE status IN ('SUCCEEDED', 'SUCCESS')
                ORDER BY load_batch_id DESC
                LIMIT 1
                """
            )

    @staticmethod
    def _count_table(table_name: str) -> int | None:
        """Gold 테이블의 현재 행 수를 읽는다."""

        try:
            row = fetch_one(f"SELECT COUNT(*) AS row_count FROM {table_name}")
        except DatabaseError:
            return None
        return int((row or {}).get("row_count") or 0)

    def get_table_counts(self) -> dict[str, int | None]:
        """대시보드에 표시할 Gold 테이블별 현재 행 수."""

        return {
            "hr_area": self._count_table("hr_area"),
            "hr_manager": self._count_table("hr_manager"),
            "hr_area_manager_assignment": self._count_table(
                "hr_area_manager_assignment"
            ),
            "area_manager_features": self._count_table("area_manager_features"),
        }
