"""담당자 조회에 필요한 업무 로직."""

from __future__ import annotations

from .pagination import PAGE_SIZE, make_pagination, parse_page


class ManagerService:
    def __init__(self, repository):
        self.repository = repository

    def search_managers(
        self,
        keyword: str = "",
        active: str = "",
        department: str = "",
        page: object = 1,
    ) -> dict[str, object]:
        page_number = parse_page(page)
        rows, total = self.repository.find_managers(
            keyword=keyword.strip(),
            active=active.strip().upper(),
            department=department.strip(),
            limit=PAGE_SIZE,
            offset=(page_number - 1) * PAGE_SIZE,
        )
        return {
            "managers": rows,
            "filters": {
                "keyword": keyword,
                "active": active,
                "department": department,
            },
            "pagination": make_pagination(page_number, total),
        }

    def get_manager(self, manager_id: str):
        manager = self.repository.find_manager(manager_id)
        if manager is None:
            return None
        return {
            "manager": manager,
            "areas": self.repository.find_managed_areas(manager_id),
        }

    def export_managers(
        self,
        keyword: str = "",
        active: str = "",
        department: str = "",
    ) -> list[dict[str, object]]:
        """CSV용 담당자 데이터를 페이지 제한 없이 조회한다."""

        return self.repository.export_managers(
            keyword=keyword.strip(),
            active=active.strip().upper(),
            department=department.strip(),
        )
