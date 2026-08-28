"""조직 조회와 조직도에 필요한 업무 로직."""

from __future__ import annotations

from .pagination import PAGE_SIZE, make_pagination, parse_page


class AreaService:
    def __init__(self, repository):
        self.repository = repository

    def search_areas(
        self,
        keyword: str = "",
        top_area_id: str = "",
        organization_type: str = "",
        active: str = "",
        page: object = 1,
    ) -> dict[str, object]:
        page_number = parse_page(page)
        rows, total = self.repository.find_areas(
            keyword=keyword.strip(),
            top_area_id=top_area_id.strip(),
            organization_type=organization_type.strip().upper(),
            active=active.strip().upper(),
            limit=PAGE_SIZE,
            offset=(page_number - 1) * PAGE_SIZE,
        )
        return {
            "areas": rows,
            "filters": {
                "keyword": keyword,
                "top_area_id": top_area_id,
                "organization_type": organization_type,
                "active": active,
            },
            "pagination": make_pagination(page_number, total),
        }

    def get_area(self, area_id: str):
        return self.repository.find_area(area_id)

    def export_areas(
        self,
        keyword: str = "",
        top_area_id: str = "",
        organization_type: str = "",
        active: str = "",
    ) -> list[dict[str, object]]:
        """CSV용 조직 데이터를 페이지 제한 없이 조회한다."""

        return self.repository.export_areas(
            keyword=keyword.strip(),
            top_area_id=top_area_id.strip(),
            organization_type=organization_type.strip().upper(),
            active=active.strip().upper(),
        )

    def get_tree(self, top_area_id: str = "") -> dict[str, object]:
        rows = self.repository.find_tree(top_area_id.strip())
        nodes = {}
        for row in rows:
            node = dict(row)
            node["children"] = []
            nodes[node["area_id"]] = node

        roots = []
        for node in nodes.values():
            parent_id = node.get("parent_area_id")
            # 부모가 없거나 자기 자신이면 최상위 노드로 표시한다.
            if not parent_id or parent_id == node["area_id"] or parent_id not in nodes:
                roots.append(node)
            else:
                nodes[parent_id]["children"].append(node)
        return {"tree": roots, "top_area_id": top_area_id, "area_count": len(rows)}
