"""대시보드 첫 화면에 표시할 지표를 조합한다."""

from __future__ import annotations

from django.db import DatabaseError


class DashboardService:
    def __init__(self, area_repository, manager_repository, dashboard_repository):
        self.area_repository = area_repository
        self.manager_repository = manager_repository
        self.dashboard_repository = dashboard_repository

    def get_context(self) -> dict[str, object]:
        try:
            area_metrics = self.area_repository.get_metrics()
            manager_metrics = self.manager_repository.get_metrics()
            latest_batch = self.dashboard_repository.find_latest_batch()
            table_counts = self.dashboard_repository.get_table_counts()
        except DatabaseError as error:
            # 테이블이 아직 없거나 MySQL이 꺼져 있어도 화면은 안내 문구를 보여준다.
            return {
                "database_error": str(error),
                "metrics": {
                    "total_areas": 0,
                    "top_areas": 0,
                    "unassigned_areas": 0,
                    "total_managers": 0,
                    "active_managers": 0,
                },
                "latest_batch": None,
                "table_counts": {},
            }

        return {
            "metrics": {
                "total_areas": area_metrics.get("total_areas", 0),
                "top_areas": area_metrics.get("top_areas", 0),
                "unassigned_areas": area_metrics.get("unassigned_areas", 0),
                "total_managers": manager_metrics.get("total_managers", 0),
                "active_managers": manager_metrics.get("active_managers", 0),
            },
            "latest_batch": latest_batch,
            "table_counts": table_counts,
        }
