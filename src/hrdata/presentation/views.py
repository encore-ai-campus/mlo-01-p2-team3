"""HTTP 요청을 서비스 계층으로 전달하는 화면 컨트롤러."""

from __future__ import annotations

import csv
from datetime import date, datetime
from urllib.parse import quote

from django.db import DatabaseError
from django.http import Http404, HttpResponse
from django.shortcuts import render

from hrdata.repository.area_repository import AreaRepository
from hrdata.repository.dashboard_repository import DashboardRepository
from hrdata.repository.manager_repository import ManagerRepository
from hrdata.service.area_service import AreaService
from hrdata.service.dashboard_service import DashboardService
from hrdata.service.manager_service import ManagerService
from hrdata.templatetags.display_filters import area_display, compact


area_service = AreaService(AreaRepository())
manager_service = ManagerService(ManagerRepository())
dashboard_service = DashboardService(
    area_repository=AreaRepository(),
    manager_repository=ManagerRepository(),
    dashboard_repository=DashboardRepository(),
)


def _database_error(request, error):
    return render(request, "hrdata/error.html", {"error": str(error)})


def _csv_cell(value: object, formatter=None) -> str:
    """CSV 셀 값을 문자열로 만들고 날짜는 초 단위까지만 표시한다."""

    if value is None:
        return ""
    if formatter is not None:
        return formatter(value)
    if isinstance(value, (datetime, date)):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    return str(value)


def _csv_response(
    filename: str,
    headers: list[str],
    rows: list[list[object]],
) -> HttpResponse:
    """한글이 깨지지 않는 CSV 응답을 만든다."""

    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = (
        f"attachment; filename*=UTF-8''{quote(filename)}"
    )
    response.write("\ufeff")
    writer = csv.writer(response, lineterminator="\r\n")
    writer.writerow(headers)
    writer.writerows(rows)
    return response


def dashboard(request):
    return render(request, "hrdata/dashboard.html", dashboard_service.get_context())


def area_list(request):
    try:
        context = area_service.search_areas(
            keyword=request.GET.get("keyword", ""),
            top_area_id=request.GET.get("top_area_id", ""),
            organization_type=request.GET.get("organization_type", ""),
            active=request.GET.get("active", ""),
            page=request.GET.get("page", 1),
        )
    except DatabaseError as error:
        return _database_error(request, error)
    return render(request, "hrdata/area_list.html", context)


def area_export(request):
    """조직 검색 결과 또는 전체 조직을 별도 열로 CSV 내려받는다."""

    export_all = request.GET.get("all") == "1"
    try:
        rows = area_service.export_areas(
            keyword="" if export_all else request.GET.get("keyword", ""),
            top_area_id="" if export_all else request.GET.get("top_area_id", ""),
            organization_type=(
                "" if export_all else request.GET.get("organization_type", "")
            ),
            active="" if export_all else request.GET.get("active", ""),
        )
    except DatabaseError as error:
        return _database_error(request, error)

    headers = [
        "조직코드",
        "조직명",
        "부모조직코드",
        "부모조직명",
        "최상위조직코드",
        "최상위조직명",
        "조직구분",
        "담당자코드",
        "담당자명",
        "담당자부서",
        "직급",
        "재직여부",
        "담당자입사일",
        "조직등록일",
    ]
    csv_rows = [
        [
            _csv_cell(row.get("area_id"), compact),
            _csv_cell(row.get("area_name"), area_display),
            _csv_cell(row.get("parent_area_id"), compact),
            _csv_cell(row.get("parent_area_name"), area_display),
            _csv_cell(row.get("top_area_id"), compact),
            _csv_cell(row.get("top_area_name"), area_display),
            # 조직 구분은 API의 top_area_level이 아니라 Gold에서 ID 관계로
            # 계산한 organization_type(TOP/SUB)을 내보낸다.
            _csv_cell(row.get("organization_type"), compact),
            _csv_cell(row.get("manager_id"), compact),
            _csv_cell(row.get("manager_name"), compact),
            _csv_cell(row.get("department_name"), compact),
            _csv_cell(row.get("position_name"), compact),
            _csv_cell(row.get("manager_active_yn"), compact),
            _csv_cell(row.get("manager_hire_at")),
            _csv_cell(row.get("area_registered_at")),
        ]
        for row in rows
    ]
    filename = "조직_전체.csv" if export_all else "조직_검색결과.csv"
    return _csv_response(filename, headers, csv_rows)


def area_detail(request, area_id):
    try:
        area = area_service.get_area(area_id)
    except DatabaseError as error:
        return _database_error(request, error)
    if area is None:
        raise Http404("조직을 찾을 수 없습니다.")
    return render(request, "hrdata/area_detail.html", {"area": area})


def organization_tree(request):
    try:
        context = area_service.get_tree(request.GET.get("top_area_id", ""))
    except DatabaseError as error:
        return _database_error(request, error)
    return render(request, "hrdata/organization_tree.html", context)


def manager_list(request):
    try:
        context = manager_service.search_managers(
            keyword=request.GET.get("keyword", ""),
            active=request.GET.get("active", ""),
            department=request.GET.get("department", ""),
            page=request.GET.get("page", 1),
        )
    except DatabaseError as error:
        return _database_error(request, error)
    return render(request, "hrdata/manager_list.html", context)


def manager_export(request):
    """담당자 검색 결과 또는 전체 담당자를 별도 열로 CSV 내려받는다."""

    export_all = request.GET.get("all") == "1"
    try:
        rows = manager_service.export_managers(
            keyword="" if export_all else request.GET.get("keyword", ""),
            active="" if export_all else request.GET.get("active", ""),
            department="" if export_all else request.GET.get("department", ""),
        )
    except DatabaseError as error:
        return _database_error(request, error)

    headers = [
        "담당자코드",
        "담당자명",
        "부서",
        "직급",
        "재직여부",
        "담당조직수",
        "입사일",
    ]
    csv_rows = [
        [
            _csv_cell(row.get("manager_id"), compact),
            _csv_cell(row.get("manager_name"), compact),
            _csv_cell(row.get("department_name"), compact),
            _csv_cell(row.get("position_name"), compact),
            _csv_cell(row.get("manager_active_yn"), compact),
            _csv_cell(row.get("managed_area_count")),
            _csv_cell(row.get("manager_hire_at")),
        ]
        for row in rows
    ]
    filename = "담당자_전체.csv" if export_all else "담당자_검색결과.csv"
    return _csv_response(filename, headers, csv_rows)


def manager_detail(request, manager_id):
    try:
        context = manager_service.get_manager(manager_id)
    except DatabaseError as error:
        return _database_error(request, error)
    if context is None:
        raise Http404("담당자를 찾을 수 없습니다.")
    return render(request, "hrdata/manager_detail.html", context)
