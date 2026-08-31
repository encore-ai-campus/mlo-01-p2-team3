"""Silver 데이터를 Gold에 넣기 전 품질 게이트를 점검한다."""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from functools import lru_cache
from pathlib import Path
import re
from typing import Any
import unicodedata

import yaml

from .mongo_storage import get_mongo_database

RULE_PATH = Path(__file__).resolve().parent / "rules" / "gold_quality_gates.yaml"
DOMAIN_RULE_PATH = Path(__file__).resolve().parent / "rules" / "domains.yaml"


@lru_cache(maxsize=1)
def load_gold_rules() -> dict[str, Any]:
    with RULE_PATH.open(encoding="utf-8") as file:
        return yaml.safe_load(file) or {}


@lru_cache(maxsize=1)
def load_domain_rules() -> dict[str, Any]:
    """Silver에서 승인한 조직 관계 예외 규칙만 읽는다."""

    with DOMAIN_RULE_PATH.open(encoding="utf-8") as file:
        return yaml.safe_load(file) or {}


def _is_declared_top_reference(
    record: dict[str, Any],
    area_ids: set[Any],
    domain_rules: dict[str, Any],
) -> bool:
    """부모 문서가 없어도 parent와 top이 같은 참조를 허용한다."""

    relationship = domain_rules.get("organization_relationship", {})
    if not relationship.get("allow_declared_top_reference", False):
        return False

    parent_id = record.get("parent_area_id")
    top_id = record.get("top_area_id")
    parent_key = _comparison_value(parent_id)
    top_key = _comparison_value(top_id)
    return (
        parent_key not in (None, "")
        and parent_key == top_key
        and parent_key not in area_ids
    )


def _missing(record: dict[str, Any], fields: list[str]) -> list[str]:
    return [name for name in fields if record.get(name) in (None, "")]


def _comparison_value(value: Any) -> Any:
    """Gold 중복 비교에서만 공백·대소문자 차이를 무시한다."""

    if not isinstance(value, str):
        return value
    value = unicodedata.normalize("NFKC", value).strip()
    return re.sub(r"\s+", "", value).casefold()


def _unique_value(records: Iterable[Mapping[str, Any]], field: str) -> tuple[Any, str]:
    """한 필드의 값이 하나인지 확인한다.

    반환 상태는 ``ok``(하나로 확정), ``missing``(값 없음), ``conflict``
    (서로 다른 값 여러 개)다. 충돌 시에는 어느 값도 선택하지 않는다.
    """

    values: dict[Any, Any] = {}
    for record in records:
        value = record.get(field)
        if value is None or (isinstance(value, str) and not value.strip()):
            continue
        key = _comparison_value(value)
        try:
            values.setdefault(key, value)
        except TypeError:
            # 이 필드는 보통 스칼라이지만, 예외 입력도 임의 선택하지 않는다.
            values.setdefault(repr(key), value)
    if not values:
        return None, "missing"
    if len(values) > 1:
        return None, "conflict"
    return next(iter(values.values())), "ok"


def _clean_manager_value(field: str, value: Any) -> Any:
    """Gold에 넣을 관리자 필드의 표시값을 가볍게 정리한다."""

    if not isinstance(value, str):
        return value
    value = unicodedata.normalize("NFKC", value).strip()
    if field == "manager_name":
        return re.sub(r"\s+", "", value)
    return value


def build_gold_quality_preview(database=None) -> dict[str, Any]:
    """MySQL에 쓰지 않고 현재 Silver의 Gold 적재 가능 건수를 계산한다."""

    # PyMongo Database는 bool()을 지원하지 않으므로 None 여부만 확인한다.
    db = database if database is not None else get_mongo_database()
    rules = load_gold_rules()
    domain_rules = load_domain_rules()
    records = list(db.hr_silver_standard_records.find({}, {"_id": 0}))
    # 조직 ID 비교는 Silver와 같은 기준(공백·대소문자 무시)을 사용한다.
    area_ids = {
        _comparison_value(row.get("area_id"))
        for row in records
        if row.get("area_id")
    }

    bronze_to_area = {
        str(row["bronze_id"]): row.get("silver_key", {}).get("area_id")
        for row in db.hr_lineage_links.find(
            {}, {"_id": 0, "bronze_id": 1, "silver_key.area_id": 1}
        )
        if row.get("bronze_id") is not None
    }
    blocked: dict[str, set[str]] = defaultdict(set)
    for review in db.hr_review_queue.find(
        {}, {"_id": 0, "bronze_id": 1, "blocks_gold": 1,
            "blocks_silver": 1, "review_stage": 1, "failure_stage": 1}
    ):
        # Silver의 단순 경고는 Gold 차단 근거로 사용하지 않는다. Gold
        # 단계에서 명시적으로 만든 검토만 Gold 적재를 막을 수 있다.
        if (
            review.get("review_stage") != "GOLD"
            and review.get("failure_stage") != "GOLD"
        ):
            continue
        if review.get("blocks_silver", False):
            continue
        area_id = bronze_to_area.get(str(review.get("bronze_id")))
        area_key = _comparison_value(area_id)
        if not area_key:
            continue
        for target in review.get("blocks_gold", []):
            blocked[target].add(area_key)

    table_rules = rules["tables"]
    failures: Counter[str] = Counter()
    area_candidates: set[str] = set()
    declared_top_reference_count = 0
    manager_rows: dict[str, set[tuple[Any, ...]]] = defaultdict(set)
    assignment_rows: list[tuple[str, str]] = []

    # 관리자 고유 충돌은 필수 식별·상태 값만으로 판단한다. 부서명은
    # 배정 정보이고, 선택 속성 충돌은 partial 적재에서 NULL로 처리한다.
    manager_policy = domain_rules.get("manager_relationship", {})
    configured_required = manager_policy.get(
        "required_fields", ["manager_name", "manager_active_yn"]
    )
    if not isinstance(configured_required, list):
        configured_required = ["manager_name", "manager_active_yn"]
    manager_attributes = tuple(
        dict.fromkeys(
            [
                *configured_required,
            ]
        )
    )

    for record in records:
        area_id = record.get("area_id")
        area_key = _comparison_value(area_id)
        area_missing = _missing(record, table_rules["hr_area"]["required_fields"])
        if area_missing:
            failures["AREA_REQUIRED_FIELD_MISSING"] += 1
        else:
            parent_id = record.get("parent_area_id")
            parent_key = _comparison_value(parent_id)
            top_key = _comparison_value(record.get("top_area_id"))
            is_root = area_key == top_key
            declared_top_reference = _is_declared_top_reference(
                record, area_ids, domain_rules
            )
            if declared_top_reference:
                declared_top_reference_count += 1
            bad_child = not is_root and not declared_top_reference and (
                not parent_key or parent_key == area_key or parent_key not in area_ids
            )
            if bad_child:
                failures["INVALID_CHILD_PARENT"] += 1
            elif area_key in blocked["*"] or area_key in blocked["hr_area"]:
                failures["AREA_BLOCKED_BY_REVIEW"] += 1
            else:
                area_candidates.add(area_id)

        manager_missing = _missing(
            record, table_rules["hr_manager"]["required_fields"]
        )
        manager_id = record.get("manager_id")
        manager_blocked = (
            area_key in blocked["*"] or area_key in blocked["hr_manager"]
        )
        if manager_missing:
            failures["MANAGER_REQUIRED_FIELD_MISSING"] += 1
        elif manager_blocked:
            failures["MANAGER_BLOCKED_BY_REVIEW"] += 1
        else:
            manager_rows[_comparison_value(manager_id)].add(
                tuple(
                    _comparison_value(record.get(field))
                    for field in manager_attributes
                )
            )

        assignment_missing = _missing(
            record,
            table_rules["hr_area_manager_assignment"]["required_fields"],
        )
        assignment_blocked = (
            area_key in blocked["*"]
            or area_key in blocked["hr_area_manager_assignment"]
        )
        if assignment_missing:
            failures["ASSIGNMENT_REQUIRED_FIELD_MISSING"] += 1
        elif assignment_blocked:
            failures["ASSIGNMENT_BLOCKED_BY_REVIEW"] += 1
        else:
            assignment_rows.append((area_id, manager_id))

    conflicting_managers = {
        manager_id for manager_id, values in manager_rows.items() if len(values) > 1
    }
    if conflicting_managers:
        failures["MANAGER_ATTRIBUTE_CONFLICT"] = len(conflicting_managers)

    valid_managers = set(manager_rows) - conflicting_managers
    valid_assignments = {
        (area_id, manager_id)
        for area_id, manager_id in assignment_rows
        if (
            area_id in area_candidates
            and _comparison_value(manager_id) in valid_managers
        )
    }

    gold_warning_counts: dict[str, int] = {}
    # 선택 필드 충돌은 Gold 적재를 막지 않고 NULL로 처리하므로 경고로
    # 별도 보고한다. Gold 실패 건수(failures)와 섞지 않는다.
    try:
        optional_conflicts = build_gold_partial_records(db).get(
            "optional_manager_conflicts", 0
        )
    except Exception:
        # 품질 미리보기 자체가 경고 집계 때문에 실패하지 않도록 한다.
        optional_conflicts = 0
    if optional_conflicts:
        gold_warning_counts["OPTIONAL_MANAGER_ATTRIBUTE_CONFLICT"] = int(
            optional_conflicts
        )

    return {
        "rule_version": rules["rule_version"],
        "source_silver_count": len(records),
        "eligible_counts": {
            "hr_area": len(area_candidates),
            "hr_manager": len(valid_managers),
            "hr_area_manager_assignment": len(valid_assignments),
        },
        "accepted_relationship_counts": {
            "declared_top_reference": declared_top_reference_count,
        },
        "gate_failure_counts": dict(sorted(failures.items())),
        "gold_warning_counts": gold_warning_counts,
        "quality_gate_passed": not failures,
        "writes_performed": False,
    }


def build_gold_partial_records(database=None) -> dict[str, Any]:
    """정상 필드만 골라 Gold 테이블별 적재 목록을 만든다.

    전체 품질 게이트가 실패해도 원본이나 Silver를 삭제하지 않는다. 필수
    값이 하나로 확인되는 행만 적재하고, 선택 속성 충돌은 NULL로 만든다.
    관리자 이름·재직 상태가 없거나 서로 다르면 관리자와 배정을 제외한다.
    """

    db = database if database is not None else get_mongo_database()
    rules = load_gold_rules()
    records = list(db.hr_silver_standard_records.find({}, {"_id": 0}))
    table_rules = rules.get("tables", {})

    def required_fields(table: str, fallback: tuple[str, ...]) -> tuple[str, ...]:
        configured = table_rules.get(table, {}).get("required_fields", [])
        if not isinstance(configured, list) or not configured:
            return fallback
        return tuple(str(field) for field in configured)

    area_required = required_fields(
        "hr_area", ("area_id", "area_name", "top_area_id", "top_area_name", "top_area_level")
    )
    manager_required = required_fields(
        "hr_manager", ("manager_id", "manager_name", "manager_active_yn")
    )

    # 같은 키의 Silver 문서를 모아, 필수 필드는 유일한 값만 채택한다.
    area_groups: dict[Any, list[dict[str, Any]]] = defaultdict(list)
    manager_groups: dict[Any, list[dict[str, Any]]] = defaultdict(list)
    area_ids: set[Any] = set()
    for record in records:
        area_id = record.get("area_id")
        if area_id not in (None, ""):
            area_key = _comparison_value(area_id)
            area_groups[area_key].append(record)
            area_ids.add(area_key)
        manager_id = record.get("manager_id")
        if manager_id not in (None, ""):
            manager_groups[_comparison_value(manager_id)].append(record)

    area_records: list[dict[str, Any]] = []
    area_by_key: dict[Any, dict[str, Any]] = {}
    skipped_area = 0
    declared_top_reference_count = 0
    domain_rules = load_domain_rules()
    relationship = domain_rules.get("organization_relationship", {})
    allow_declared_top = bool(
        relationship.get("allow_declared_top_reference", False)
    )

    for area_key, group in area_groups.items():
        candidate = dict(group[0])
        valid = True
        for field in area_required:
            value, state = _unique_value(group, field)
            if state != "ok":
                valid = False
                break
            candidate[field] = value
        if not valid:
            skipped_area += 1
            continue

        area_id = candidate.get("area_id")
        top_id = candidate.get("top_area_id")
        parent_id = candidate.get("parent_area_id")
        is_root = _comparison_value(area_id) == _comparison_value(top_id)
        declared_top = (
            allow_declared_top
            and _comparison_value(parent_id) == _comparison_value(top_id)
            and _comparison_value(parent_id) not in area_ids
            and parent_id not in (None, "")
        )
        if declared_top:
            declared_top_reference_count += 1
        invalid_child = not is_root and not declared_top and (
            parent_id in (None, "")
            or _comparison_value(parent_id) == _comparison_value(area_id)
            or _comparison_value(parent_id) not in area_ids
        )
        root_parent_conflict = (
            is_root
            and parent_id not in (None, "")
            and _comparison_value(parent_id) != _comparison_value(area_id)
        )
        if invalid_child or root_parent_conflict:
            skipped_area += 1
            continue
        area_records.append(candidate)
        area_by_key[area_key] = candidate

    manager_records: list[dict[str, Any]] = []
    manager_by_key: dict[Any, dict[str, Any]] = {}
    skipped_manager = 0
    optional_manager_conflicts = 0
    manager_policy = domain_rules.get("manager_relationship", {})
    optional_manager_fields = tuple(
        str(field)
        for field in manager_policy.get(
            "optional_fields", ["position_name", "manager_hire_at"]
        )
    )
    # 부서명은 관리자 속성이 아니라 배정 레코드에서만 의미를 갖는다.
    optional_fields = tuple(dict.fromkeys((*optional_manager_fields, "department_name")))

    for manager_key, group in manager_groups.items():
        candidate = dict(group[0])
        valid = True
        for field in manager_required:
            value, state = _unique_value(group, field)
            if state != "ok":
                valid = False
                break
            candidate[field] = _clean_manager_value(field, value)
        active = candidate.get("manager_active_yn")
        if valid and str(active).strip().upper() not in {"Y", "N"}:
            valid = False
        if not valid:
            skipped_manager += 1
            continue
        candidate["manager_active_yn"] = str(active).strip().upper()
        for field in optional_fields:
            value, state = _unique_value(group, field)
            if state == "conflict":
                candidate[field] = None
                if field in optional_manager_fields:
                    optional_manager_conflicts += 1
            elif state == "ok":
                candidate[field] = _clean_manager_value(field, value)
            else:
                candidate[field] = None
        manager_records.append(candidate)
        manager_by_key[manager_key] = candidate

    # 유효한 조직·관리자만 배정으로 연결한다. 한 조직에 여러 관리자가
    # 남으면 임의 선택하지 않고 해당 조직의 배정을 제외한다.
    assignment_by_area: dict[Any, tuple[Any, Any]] = {}
    assignment_conflicts: set[Any] = set()
    for record in records:
        area_key = _comparison_value(record.get("area_id"))
        manager_key = _comparison_value(record.get("manager_id"))
        if area_key not in area_by_key or manager_key not in manager_by_key:
            continue
        pair = (area_by_key[area_key]["area_id"], manager_by_key[manager_key]["manager_id"])
        previous = assignment_by_area.get(area_key)
        if previous is not None and previous != pair:
            assignment_conflicts.add(area_key)
        else:
            assignment_by_area[area_key] = pair

    assignment_records = [
        {"area_id": area_id, "manager_id": manager_id}
        for area_key, (area_id, manager_id) in assignment_by_area.items()
        if area_key not in assignment_conflicts
    ]
    feature_records: list[dict[str, Any]] = []
    for assignment in assignment_records:
        area_key = _comparison_value(assignment["area_id"])
        manager_key = _comparison_value(assignment["manager_id"])
        area = area_by_key.get(area_key)
        manager = manager_by_key.get(manager_key)
        if area is None or manager is None:
            continue
        feature = dict(area)
        feature["manager_id"] = manager["manager_id"]
        feature["manager_active_yn"] = manager["manager_active_yn"]
        feature_records.append(feature)

    return {
        "source_silver_count": len(records),
        "area_records": area_records,
        "manager_records": manager_records,
        "assignment_records": assignment_records,
        "feature_records": feature_records,
        "accepted_relationship_counts": {
            "declared_top_reference": declared_top_reference_count,
        },
        "skipped_counts": {
            "hr_area": skipped_area,
            "hr_manager": skipped_manager,
            "hr_area_manager_assignment": len(assignment_conflicts),
            "area_manager_features": len(area_records) - len(feature_records),
        },
        "optional_manager_conflicts": optional_manager_conflicts,
    }
