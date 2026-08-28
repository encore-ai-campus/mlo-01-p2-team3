"""Gold 담당자 테이블을 읽는 저장소."""

from __future__ import annotations

from typing import Any

from .base import fetch_all, fetch_one


class ManagerRepository:
    """``hr_manager``와 담당 조직 연결을 조회한다."""

    _FROM = """
        FROM hr_manager m
        LEFT JOIN hr_area_manager_assignment ama
            ON m.manager_id = ama.manager_id
        LEFT JOIN hr_area a
            ON ama.area_id = a.area_id
    """

    @staticmethod
    def _where(keyword: str = "", active: str = "", department: str = "") -> tuple[str, list[Any]]:
        conditions: list[str] = []
        params: list[Any] = []
        if keyword:
            token = f"%{keyword}%"
            conditions.append(
                "(m.manager_id LIKE %s OR m.manager_name LIKE %s "
                "OR m.department_name LIKE %s OR m.position_name LIKE %s)"
            )
            params.extend([token] * 4)
        if active in {"Y", "N"}:
            conditions.append("m.manager_active_yn = %s")
            params.append(active)
        if department:
            conditions.append("m.department_name = %s")
            params.append(department)
        return (" WHERE " + " AND ".join(conditions)) if conditions else "", params

    def find_managers(
        self,
        keyword: str = "",
        active: str = "",
        department: str = "",
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        where, params = self._where(keyword, active, department)
        total_row = fetch_one(
            f"SELECT COUNT(DISTINCT m.manager_id) AS total {self._FROM} {where}",
            params,
        )
        rows = fetch_all(
            f"""
            SELECT m.manager_id, m.manager_name, m.department_name,
                   m.position_name, m.manager_active_yn, m.manager_hire_at,
                   COUNT(DISTINCT a.area_id) AS managed_area_count
            {self._FROM} {where}
            GROUP BY m.manager_id, m.manager_name, m.department_name,
                     m.position_name, m.manager_active_yn, m.manager_hire_at
            ORDER BY m.manager_name, m.manager_id
            LIMIT %s OFFSET %s
            """,
            [*params, limit, offset],
        )
        return rows, int((total_row or {}).get("total") or 0)

    def export_managers(
        self,
        keyword: str = "",
        active: str = "",
        department: str = "",
    ) -> list[dict[str, Any]]:
        """검색 조건에 맞는 담당자를 페이지 제한 없이 반환한다."""

        where, params = self._where(keyword, active, department)
        return fetch_all(
            f"""
            SELECT m.manager_id, m.manager_name, m.department_name,
                   m.position_name, m.manager_active_yn, m.manager_hire_at,
                   COUNT(DISTINCT a.area_id) AS managed_area_count
            {self._FROM} {where}
            GROUP BY m.manager_id, m.manager_name, m.department_name,
                     m.position_name, m.manager_active_yn, m.manager_hire_at
            ORDER BY m.manager_name, m.manager_id
            """,
            params,
        )

    def find_manager(self, manager_id: str) -> dict[str, Any] | None:
        return fetch_one(
            """
            SELECT manager_id, manager_name, department_name, position_name,
                   manager_active_yn, manager_hire_at
            FROM hr_manager WHERE manager_id = %s LIMIT 1
            """,
            [manager_id],
        )

    def find_managed_areas(self, manager_id: str) -> list[dict[str, Any]]:
        return fetch_all(
            """
            SELECT a.area_id, a.area_name, a.parent_area_name,
                   a.top_area_name, a.top_area_level
            FROM hr_area_manager_assignment ama
            INNER JOIN hr_area a ON ama.area_id = a.area_id
            WHERE ama.manager_id = %s
            ORDER BY a.top_area_id, a.area_id
            """,
            [manager_id],
        )

    def get_metrics(self) -> dict[str, Any]:
        return fetch_one(
            """
            SELECT COUNT(*) AS total_managers,
                   COALESCE(SUM(CASE WHEN manager_active_yn = 'Y' THEN 1 ELSE 0 END), 0)
                       AS active_managers
            FROM hr_manager
            """
        ) or {"total_managers": 0, "active_managers": 0}
