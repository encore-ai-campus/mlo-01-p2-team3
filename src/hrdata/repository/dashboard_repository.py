"""Gold 적재 이력 조회."""

from __future__ import annotations

from typing import Any

from django.db import DatabaseError

from .base import fetch_one


class DashboardRepository:
    """현재 프로젝트의 ``hr_gold_load_batch``만 읽는다."""

    def find_latest_batch(self) -> dict[str, Any] | None:
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
