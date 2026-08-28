"""Gold 조직 테이블을 읽는 저장소."""

from __future__ import annotations

from typing import Any

from .base import fetch_all, fetch_one


class AreaRepository:
    """``hr_area``와 현재 조직 담당자 연결을 조회한다."""

    _FROM = """
        FROM hr_area a
        LEFT JOIN hr_area_manager_assignment ama
            ON a.area_id = ama.area_id
        LEFT JOIN hr_manager m
            ON ama.manager_id = m.manager_id
    """

    @staticmethod
    def _where(
        keyword: str = "",
        top_area_id: str = "",
        organization_type: str = "",
        active: str = "",
    ) -> tuple[str, list[Any]]:
        conditions: list[str] = []
        params: list[Any] = []

        if keyword:
            token = f"%{keyword}%"
            conditions.append(
                "(a.area_id LIKE %s OR a.area_name LIKE %s "
                "OR a.parent_area_name LIKE %s OR a.top_area_name LIKE %s "
                "OR m.manager_id LIKE %s OR m.manager_name LIKE %s)"
            )
            params.extend([token] * 6)
        if top_area_id:
            conditions.append("a.top_area_id = %s")
            params.append(top_area_id)
        if organization_type:
            conditions.append("a.top_area_level = %s")
            params.append(organization_type)
        if active in {"Y", "N"}:
            conditions.append("m.manager_active_yn = %s")
            params.append(active)

        return (" WHERE " + " AND ".join(conditions)) if conditions else "", params

    @staticmethod
    def _select() -> str:
        return """
            SELECT
                a.area_id,
                a.area_name,
                a.parent_area_id,
                a.parent_area_name,
                a.top_area_id,
                a.top_area_name,
                a.top_area_level,
                a.registered_at AS area_registered_at,
                m.manager_id,
                m.manager_name,
                m.department_name,
                m.position_name,
                m.manager_active_yn,
                m.manager_hire_at
        """

    def find_areas(
        self,
        keyword: str = "",
        top_area_id: str = "",
        organization_type: str = "",
        active: str = "",
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        where, params = self._where(keyword, top_area_id, organization_type, active)
        total_row = fetch_one(
            f"SELECT COUNT(DISTINCT a.area_id) AS total {self._FROM} {where}",
            params,
        )
        rows = fetch_all(
            f"{self._select()} {self._FROM} {where} "
            "ORDER BY a.top_area_id, a.parent_area_id, a.area_id LIMIT %s OFFSET %s",
            [*params, limit, offset],
        )
        return rows, int((total_row or {}).get("total") or 0)

    def export_areas(
        self,
        keyword: str = "",
        top_area_id: str = "",
        organization_type: str = "",
        active: str = "",
    ) -> list[dict[str, Any]]:
        """검색 조건에 맞는 조직을 페이지 제한 없이 반환한다."""

        where, params = self._where(keyword, top_area_id, organization_type, active)
        return fetch_all(
            f"{self._select()} {self._FROM} {where} "
            "ORDER BY a.top_area_id, a.parent_area_id, a.area_id",
            params,
        )

    def find_area(self, area_id: str) -> dict[str, Any] | None:
        return fetch_one(
            f"{self._select()} {self._FROM} WHERE a.area_id = %s LIMIT 1",
            [area_id],
        )

    def find_tree(self, top_area_id: str = "") -> list[dict[str, Any]]:
        where = "WHERE a.top_area_id = %s" if top_area_id else ""
        params = [top_area_id] if top_area_id else []
        rows = fetch_all(
            f"""
            SELECT
                a.area_id, a.area_name, a.parent_area_id,
                a.parent_area_name, a.top_area_id, a.top_area_name,
                a.top_area_level, m.manager_id, m.manager_name,
                m.manager_active_yn
            {self._FROM} {where}
            ORDER BY a.top_area_id, a.parent_area_id, a.area_id
            """,
            params,
        )
        return rows

    def get_metrics(self) -> dict[str, Any]:
        return fetch_one(
            f"""
            SELECT
                COUNT(DISTINCT a.area_id) AS total_areas,
                COUNT(DISTINCT CASE WHEN a.area_id = a.top_area_id THEN a.area_id END) AS top_areas,
                COUNT(DISTINCT CASE WHEN ama.area_id IS NULL THEN a.area_id END) AS unassigned_areas
            {self._FROM}
            """
        ) or {"total_areas": 0, "top_areas": 0, "unassigned_areas": 0}
