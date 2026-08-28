"""목록 화면의 간단한 페이지 계산."""

from __future__ import annotations


PAGE_SIZE = 20


def parse_page(value: object) -> int:
    try:
        page = int(value)
    except (TypeError, ValueError):
        return 1
    return max(page, 1)


def make_pagination(page: int, total: int) -> dict[str, object]:
    total_pages = max((total + PAGE_SIZE - 1) // PAGE_SIZE, 1)
    page = min(max(page, 1), total_pages)
    return {
        "page": page,
        "total": total,
        "total_pages": total_pages,
        "has_previous": page > 1,
        "has_next": page < total_pages,
        "previous_page": page - 1,
        "next_page": page + 1,
    }
