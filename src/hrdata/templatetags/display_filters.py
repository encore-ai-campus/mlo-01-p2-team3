"""대시보드 표시용 문자열 필터."""

from __future__ import annotations

import re
import unicodedata

from django import template


register = template.Library()


# 실제 데이터에서 확인된 반복 단어만 화면에서 정리한다.
# 저장값과 검색값은 변경하지 않고, 표시할 때만 적용한다.
_REPEATED_AREA_PARTS = (
    "프로세스",
    "서비스",
    "시스템",
    "운영",
    "분석",
    "관리",
)


def _remove_repeated_area_parts(value: str) -> str:
    """연속으로 두 번 붙은 승인 단어를 한 번만 표시한다."""

    result = value
    for part in _REPEATED_AREA_PARTS:
        repeated = part + part
        while repeated in result:
            result = result.replace(repeated, part)
    return result


@register.filter
def compact(value: object) -> str:
    """화면에서만 공백을 제거한다. 원본·Gold 값은 변경하지 않는다."""

    if value is None:
        return ""
    normalized = unicodedata.normalize("NFKC", str(value))
    return re.sub(r"\s+", "", normalized)


@register.filter
def area_display(value: object) -> str:
    """조직명을 읽기 쉽게 표시한다.

    숫자 suffix는 실제 조직 번호일 수 있으므로 보존하고, suffix 앞의
    구분 공백은 한 칸으로 표시한다. 반복 단어 제거는 화면에만 적용한다.
    """

    if value is None:
        return ""

    normalized = unicodedata.normalize("NFKC", str(value)).strip()
    if not normalized:
        return ""

    # ``자산관리관리 20``처럼 마지막 숫자와 그 앞의 공백은 보존한다.
    suffix_match = re.fullmatch(r"(.*?)(?:\s+)(\d+)", normalized)
    if suffix_match:
        body = suffix_match.group(1)
        suffix = f" {suffix_match.group(2)}"
    else:
        body = normalized
        suffix = ""

    body = re.sub(r"\s+", "", body)
    body = _remove_repeated_area_parts(body)
    return body + suffix
