"""정규화 결과의 분기와 검토 승인·재처리를 한 곳에서 관리한다."""

from __future__ import annotations

from collections import Counter, defaultdict
from copy import deepcopy
import re
import unicodedata
from typing import Any, Iterable, Mapping

from .mongo_storage import ReviewQueueRepository
from .gold_quality import load_gold_rules
from .transform import (
    load_domain_rule,
    load_field_mapping,
    load_rule_version,
    normalize_bronze_record,
)


# 자동 처리하지 않고 검토로 보내는 도메인 오류 코드다.
DOMAIN_ERROR_CODES = {
    "UNKNOWN_ACTIVE_STATUS",
    "UNREGISTERED_ORGANIZATION_LEVEL",
    "INVALID_MANAGER_NAME",
    "UNREGISTERED_REFERENCE_VALUE",
    "UNKNOWN_MANAGER_ID",
    "DUPLICATE_ID",
    "PARENT_AREA_MISSING",
    "PARENT_AREA_NOT_FOUND",
    "PARENT_AREA_SELF_REFERENCE",
    "PARENT_AREA_CYCLE",
    "ROOT_PARENT_CONFLICT",
    "SILVER_EXISTING_CONFLICT",
}


def _failure_stage(issues: list[dict[str, Any]]) -> str:
    """오류 코드로 정규화 단계와 후보 검증 단계를 구분한다."""

    codes = {
        issue.get("error_code")
        for issue in issues
        if isinstance(issue, Mapping)
    }
    if codes & DOMAIN_ERROR_CODES:
        return "CANDIDATE_VALIDATION"
    if codes:
        return "NORMALIZATION"
    return "UNKNOWN"


def _add_issue(
    issues: list[dict[str, Any]],
    field: str,
    error_code: str,
    value: Any,
) -> None:
    """같은 오류를 한 건에 여러 번 추가하지 않는다."""

    if any(issue.get("error_code") == error_code for issue in issues):
        return
    issues.append({"field": field, "error_code": error_code, "value": value})


def _add_warning(
    warnings: list[dict[str, Any]],
    field: str,
    warning_code: str,
    value: Any,
    scope: str = "RECORD",
    blocks_gold: Iterable[str] | None = None,
) -> None:
    """같은 경고를 한 건에 여러 번 추가하지 않는다."""

    if any(
        warning.get("warning_code") == warning_code
        and warning.get("field") == field
        for warning in warnings
    ):
        return
    warnings.append({
        "field": field,
        "warning_code": warning_code,
        "value": value,
        "scope": scope,
        "severity": "WARNING",
        "blocks_silver": False,
        "blocks_gold": list(blocks_gold or []),
    })


def _rule_fields(value: Any, default: tuple[str, ...]) -> tuple[str, ...]:
    """YAML의 필드 목록을 안전하게 문자열 튜플로 바꾼다."""

    if not isinstance(value, list):
        return default
    fields = tuple(str(field) for field in value if isinstance(field, str))
    return fields or default


def _is_missing(value: Any) -> bool:
    """Gold 필수값 검사에서 NULL과 빈 문자열을 같은 누락으로 본다."""

    return value is None or (isinstance(value, str) and not value.strip())


def _gold_preflight_issues(
    normalized_records: list[dict[str, Any]],
    existing_records: Iterable[Mapping[str, Any]] | None = None,
    blocked_indexes: set[int] | None = None,
) -> dict[int, list[dict[str, Any]]]:
    """Silver 저장 전에 Gold 필수 컬럼 누락을 확인한다.

    Gold 로더에서 다시 실패시키지 않도록 Gold 품질 게이트의 필수 필드를
    읽어 같은 배치에서 미리 검토 큐로 보낸다. 값 보완이나 임의 선택은
    하지 않고, 원래 값은 검토 문서에 그대로 남긴다.
    """

    rules = load_gold_rules()
    tables = rules.get("tables", {})
    targets = (
        ("hr_area", "GOLD_AREA_REQUIRED_FIELD_MISSING"),
        ("hr_manager", "GOLD_MANAGER_REQUIRED_FIELD_MISSING"),
        ("hr_area_manager_assignment", "GOLD_ASSIGNMENT_REQUIRED_FIELD_MISSING"),
    )
    issues_by_index: dict[int, list[dict[str, Any]]] = defaultdict(list)

    # Bronze에 ID만 존재하는 부모는 Gold FK/조직 목록의 기준으로 삼지
    # 않는다. 이번 배치에서 Gold 필수값을 갖춘 조직과 기존 Silver 조직만
    # 실제로 사용할 수 있는 부모로 인정한다.
    area_table = tables.get("hr_area", {})
    area_required = area_table.get("required_fields", [])
    if not isinstance(area_required, list):
        area_required = []
    ready_area_ids = {
        _comparison_value(record.get("area_id"))
        for index, item in enumerate(normalized_records)
        for record in [item.get("record", {})]
        if isinstance(record, Mapping)
        and (blocked_indexes is None or index not in blocked_indexes)
        and not item.get("issues")
        and not any(
            warning.get("blocks_gold")
            for warning in item.get("warnings", [])
            if isinstance(warning, Mapping)
        )
        and record.get("area_id")
        and not any(_is_missing(record.get(field)) for field in area_required)
    }
    for record in existing_records or []:
        if not isinstance(record, Mapping):
            continue
        if record.get("area_id") and not any(
            _is_missing(record.get(field)) for field in area_required
        ):
            ready_area_ids.add(_comparison_value(record.get("area_id")))

    relationship = load_domain_rule("organization_relationship")
    allow_declared_top = bool(
        relationship.get("allow_declared_top_reference", False)
    )
    for index, item in enumerate(normalized_records):
        record = item.get("record", {})
        if not isinstance(record, Mapping):
            continue
        for target, error_code in targets:
            table = tables.get(target, {})
            fields = table.get("required_fields", [])
            if not isinstance(fields, list):
                continue
            missing = [field for field in fields if _is_missing(record.get(field))]
            if missing:
                issues_by_index[index].append({
                    "field": missing[0],
                    "error_code": error_code,
                    "value": {"target": target, "missing": missing},
                    "gold_targets": [target],
                })

        # 조직 필수값이 있는 하위 부서라도 부모가 Gold 대상이 아니면
        # Gold에서 관계를 만들 수 없으므로 Silver 단계에서 검토한다.
        area_id = record.get("area_id")
        top_id = record.get("top_area_id")
        parent_id = record.get("parent_area_id")
        if (
            not _is_missing(area_id)
            and not _is_missing(top_id)
            and _comparison_value(area_id) != _comparison_value(top_id)
            and not _is_missing(parent_id)
            and _comparison_value(parent_id) != _comparison_value(area_id)
            and _comparison_value(parent_id) not in ready_area_ids
            and not (
                allow_declared_top
                and _comparison_value(parent_id) == _comparison_value(top_id)
            )
        ):
            issues_by_index[index].append({
                "field": "parent_area_id",
                "error_code": "GOLD_PARENT_AREA_NOT_READY",
                "value": parent_id,
                "gold_targets": [
                    "hr_area",
                    "hr_area_manager_assignment",
                    "area_manager_features",
                ],
            })
    return issues_by_index


def _comparison_value(value: Any) -> Any:
    """충돌 비교에서만 문자열 공백·대소문자 차이를 제거한다."""

    if not isinstance(value, str):
        return value
    normalized = unicodedata.normalize("NFKC", value).strip()
    return re.sub(r"\s+", "", normalized).casefold()


def _complete_unique_missing_values(
    normalized_records: list[dict[str, Any]],
    existing_records: Iterable[Mapping[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """같은 ID에서 하나로 확인되는 값만 누락 필드에 채운다.

    후보 값이 없거나 둘 이상이면 값을 선택하지 않는다. 이후의 기존 충돌
    검사가 해당 데이터를 검토 큐에 남긴다.
    """

    items = deepcopy(normalized_records)
    policy = load_domain_rule("missing_value_completion")
    if not policy.get("enabled", False):
        return items

    entities = policy.get("entities", {})
    if not isinstance(entities, Mapping):
        return items

    current_records = [item.get("record", {}) for item in items]
    reference_records = current_records + list(existing_records or [])

    for entity in entities.values():
        if not isinstance(entity, Mapping):
            continue
        key_field = entity.get("key_field")
        fields = _rule_fields(entity.get("fields"), ())
        if not isinstance(key_field, str) or not fields:
            continue

        candidates: dict[str, dict[str, set[Any]]] = defaultdict(
            lambda: defaultdict(set)
        )
        for record in reference_records:
            key_value = record.get(key_field)
            if key_value in (None, ""):
                continue
            for field in fields:
                value = record.get(field)
                if value not in (None, ""):
                    candidates[str(key_value)][field].add(value)

        for item in items:
            record = item.get("record", {})
            key_value = record.get(key_field)
            if key_value in (None, ""):
                continue
            for field in fields:
                if record.get(field) not in (None, ""):
                    continue
                values = candidates[str(key_value)][field]
                if len(values) != 1:
                    continue
                record[field] = deepcopy(next(iter(values)))
                # 누락값이 유일값으로 해결됐으므로 해당 필드의 기존 메시지를 제거한다.
                item["issues"] = [
                    issue
                    for issue in item.get("issues", [])
                    if issue.get("field") != field
                ]
                item["warnings"] = [
                    warning
                    for warning in item.get("warnings", [])
                    if warning.get("field") != field
                ]

    return items


def _organization_issues(
    normalized_records: list[dict[str, Any]],
    known_area_ids: Iterable[str] | None = None,
) -> dict[int, list[dict[str, Any]]]:
    """후보 부서의 부모·최상위 관계와 순환을 확인한다."""

    issues_by_index: dict[int, list[dict[str, Any]]] = defaultdict(list)
    relationship_policy = load_domain_rule("organization_relationship")
    allow_external_parent = bool(
        relationship_policy.get("allow_external_parent", False)
    )
    allow_declared_top_reference = bool(
        relationship_policy.get("allow_declared_top_reference", False)
    )
    known = {str(value) for value in (known_area_ids or []) if value}
    for item in normalized_records:
        record = item.get("record", {})
        area_id = record.get("area_id")
        if area_id:
            known.add(str(area_id))

    graph: dict[str, str] = {}
    graph_indexes: dict[str, list[int]] = defaultdict(list)
    for index, item in enumerate(normalized_records):
        record = item.get("record", {})
        required = {"area_id", "parent_area_id", "top_area_id"}
        if not required.issubset(record):
            # 단위 테스트용 축약 문서나 매핑 실패 문서는 기존 오류만 사용한다.
            continue

        area_id = record.get("area_id")
        parent_id = record.get("parent_area_id")
        top_id = record.get("top_area_id")
        if not area_id or not top_id:
            continue

        if str(area_id) == str(top_id):
            if parent_id not in (None, "", area_id):
                _add_issue(
                    issues_by_index[index],
                    "parent_area_id",
                    "ROOT_PARENT_CONFLICT",
                    parent_id,
                )
            continue

        if parent_id in (None, ""):
            _add_issue(
                issues_by_index[index],
                "parent_area_id",
                "PARENT_AREA_MISSING",
                parent_id,
            )
            continue
        if str(parent_id) == str(area_id):
            _add_issue(
                issues_by_index[index],
                "parent_area_id",
                "PARENT_AREA_SELF_REFERENCE",
                parent_id,
            )
            continue
        declared_top_reference = (
            str(parent_id) == str(top_id)
            and str(
                relationship_policy.get(
                    "declared_top_reference_condition",
                    "parent_area_id == top_area_id",
                )
            ).strip()
            == "parent_area_id == top_area_id"
        )
        if (
            str(parent_id) not in known
            and not allow_external_parent
            and not (allow_declared_top_reference and declared_top_reference)
        ):
            _add_issue(
                issues_by_index[index],
                "parent_area_id",
                "PARENT_AREA_NOT_FOUND",
                parent_id,
            )
            continue

        graph[str(area_id)] = str(parent_id)
        graph_indexes[str(area_id)].append(index)

    # area_id -> parent_area_id 연결을 따라가며 순환을 찾는다.
    for start in graph:
        chain: list[str] = []
        seen: dict[str, int] = {}
        current = start
        while current in graph:
            if current in seen:
                for cycle_id in chain[seen[current]:]:
                    for index in graph_indexes[cycle_id]:
                        _add_issue(
                            issues_by_index[index],
                            "parent_area_id",
                            "PARENT_AREA_CYCLE",
                            graph[cycle_id],
                        )
                break
            seen[current] = len(chain)
            chain.append(current)
            current = graph[current]

    return issues_by_index


def _manager_and_existing_issues(
    normalized_records: list[dict[str, Any]],
    existing_records: Iterable[Mapping[str, Any]] | None = None,
) -> tuple[
    dict[int, list[dict[str, Any]]],
    dict[int, list[dict[str, Any]]],
]:
    """관리자 충돌과 기존 Silver 값 변경을 확인한다.

    ``department_name``은 부서 배정 정보이므로 관리자 식별 충돌에 포함하지
    않는다. YAML에서 지정한 식별 필드만 저장을 막고, 일관성 필드는 경고로
    반환한다.
    """

    issues_by_index: dict[int, list[dict[str, Any]]] = defaultdict(list)
    warnings_by_index: dict[int, list[dict[str, Any]]] = defaultdict(list)
    policy = load_domain_rule("manager_relationship")
    identity_fields = _rule_fields(
        policy.get("identity_fields"),
        ("manager_name",),
    )
    consistency_fields = _rule_fields(
        policy.get("consistency_fields"),
        (),
    )
    required_fields = set(_rule_fields(
        policy.get("required_fields"),
        ("manager_name", "manager_active_yn"),
    ))
    optional_fields = set(_rule_fields(
        policy.get("optional_fields"),
        ("position_name", "manager_hire_at"),
    ))
    consistency_action = str(
        policy.get("consistency_action", "WARNING")
    ).upper()
    conflict_action = str(policy.get("conflict_action", "REVIEW_REQUIRED")).upper()
    conflict_warning_code = str(
        policy.get("conflict_warning_code", "MANAGER_ATTRIBUTE_CONFLICT")
    )
    conflict_scope = str(policy.get("conflict_scope", "MANAGER"))
    conflict_blocks_gold = _rule_fields(
        policy.get("conflict_blocks_gold"),
        ("hr_manager", "hr_area_manager_assignment"),
    )
    manager_groups: dict[
        str,
        list[tuple[int, tuple[Any, ...], tuple[Any, ...]]],
    ] = defaultdict(list)

    def add_manager_record(index: int, record: Mapping[str, Any]) -> None:
        manager_id = record.get("manager_id")
        if not manager_id:
            return
        identity_signature = tuple(
            _comparison_value(record.get(field)) for field in identity_fields
        )
        consistency_signature = tuple(
            _comparison_value(record.get(field)) for field in consistency_fields
        )
        manager_groups[str(manager_id)].append(
            (index, identity_signature, consistency_signature)
        )

    for index, item in enumerate(normalized_records):
        add_manager_record(index, item.get("record", {}))

    existing = list(existing_records or [])
    existing_by_area = {
        str(record.get("area_id")): record
        for record in existing
        if record.get("area_id")
    }
    for record in existing:
        # 기존 Silver는 현재 배치와의 비교 기준으로만 사용한다.
        add_manager_record(-1, record)

    for members in manager_groups.values():
        identity_signatures = {identity for _, identity, _ in members}
        if len(identity_signatures) > 1:
            for index, _, _ in members:
                if index < 0:
                    continue
                manager_id = normalized_records[index].get("record", {}).get(
                    "manager_id"
                )
                if conflict_action == "WARNING":
                    _add_warning(
                        warnings_by_index[index],
                        "manager_id",
                        conflict_warning_code,
                        manager_id,
                        scope=conflict_scope,
                        blocks_gold=conflict_blocks_gold,
                    )
                else:
                    _add_issue(
                        issues_by_index[index],
                        "manager_id",
                        "MANAGER_ATTRIBUTE_CONFLICT",
                        manager_id,
                    )

        # 선택 속성은 충돌 사유를 남기되 Gold 전체를 막지 않는다. 이름과
        # 재직 상태처럼 필수인 값의 충돌만 관리자·배정 적재를 막는다.
        for field_index, field in enumerate(consistency_fields):
            field_is_optional = field in optional_fields and field not in required_fields
            field_signatures = {
                consistency[field_index]
                for _, _, consistency in members
                # 선택 필드는 NULL/빈값과 실제 값의 조합을 충돌로 보지
                # 않는다. 실제 값이 둘 이상 다를 때만 경고한다.
                if not (field_is_optional and _is_missing(consistency[field_index]))
            }
            if len(field_signatures) <= 1:
                continue
            field_blocks_gold = [] if field_is_optional else conflict_blocks_gold
            warning_code = (
                "MANAGER_OPTIONAL_ATTRIBUTE_CONFLICT"
                if field_is_optional
                else "MANAGER_ATTRIBUTE_CONFLICT"
            )
            for index, _, _ in members:
                if index < 0:
                    continue
                manager_id = normalized_records[index].get("record", {}).get(
                    "manager_id"
                )
                if consistency_action == "REVIEW_REQUIRED":
                    _add_issue(
                        issues_by_index[index],
                        "manager_id",
                        "MANAGER_ATTRIBUTE_CONFLICT",
                        manager_id,
                    )
                elif consistency_action == "WARNING":
                    _add_warning(
                        warnings_by_index[index],
                        field,
                        warning_code,
                        normalized_records[index].get("record", {}).get(field),
                        scope=conflict_scope,
                        blocks_gold=field_blocks_gold,
                    )

    for index, item in enumerate(normalized_records):
        record = item.get("record", {})
        area_id = record.get("area_id")
        previous = existing_by_area.get(str(area_id)) if area_id else None
        if not previous:
            continue
        changed_fields = [
            field
            for field, value in record.items()
            if field in previous and previous[field] != value
        ]
        if changed_fields:
            _add_issue(
                issues_by_index[index],
                "area_id",
                "SILVER_EXISTING_CONFLICT",
                {"area_id": area_id, "fields": changed_fields},
            )

    return issues_by_index, warnings_by_index


def review_records(
    normalized_records: list[dict[str, Any]],
    identity_field: str = "area_id",
    existing_records: Iterable[Mapping[str, Any]] | None = None,
    known_area_ids: Iterable[str] | None = None,
    gold_preflight: bool = False,
) -> dict[str, Any]:
    """메모리의 정규화 결과를 검증하고 저장 목적별로 나눈다.

    이 함수는 저장소를 호출하지 않는다. 호출부가 ``accepted``만 Silver에,
    ``quarantine``만 검토 큐에 저장하므로 중간 후보 컬렉션이 필요 없다.
    ``gold_preflight=True``이면 Gold 적재를 막는 경고와 Gold 필수값 누락도
    Silver에 저장하지 않고 검토 큐로 분리한다.
    """

    # 현재 배치와 승인된 Silver에서 유일하게 확인되는 값만 먼저 보완한다.
    existing_records = list(existing_records or [])
    normalized_records = _complete_unique_missing_values(
        normalized_records,
        existing_records=existing_records,
    )

    values = [
        item.get("record", {}).get(identity_field)
        for item in normalized_records
    ]
    duplicate_values = {
        value
        for value, count in Counter(values).items()
        if value is not None and count > 1
    }

    organization_issues = _organization_issues(
        normalized_records,
        known_area_ids=known_area_ids,
    )
    existing_issues, manager_warnings = _manager_and_existing_issues(
        normalized_records,
        existing_records=existing_records,
    )
    gold_preflight_issues = (
        _gold_preflight_issues(
            normalized_records,
            existing_records=existing_records,
            blocked_indexes=(
                set(organization_issues)
                | set(existing_issues)
                | {
                    index
                    for index, item in enumerate(normalized_records)
                    if item.get("record", {}).get(identity_field) in duplicate_values
                }
            ),
        )
        if gold_preflight
        else {}
    )

    accepted: list[dict[str, Any]] = []
    accepted_items: list[dict[str, Any]] = []
    quarantine: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    warning_reviews: list[dict[str, Any]] = []
    for index, item in enumerate(normalized_records):
        record = item.get("record", {})
        issues = list(item.get("issues", []))
        issues.extend(organization_issues.get(index, []))
        issues.extend(existing_issues.get(index, []))
        preflight_issues = list(gold_preflight_issues.get(index, []))
        issues.extend(preflight_issues)
        record_warnings = list(item.get("warnings", []))
        record_warnings.extend(manager_warnings.get(index, []))
        gold_targets = {
            str(target)
            for issue in preflight_issues
            for target in issue.get("gold_targets", [])
        }
        gold_preflight_blocked = bool(preflight_issues)
        if gold_preflight:
            # Silver에는 저장할 수 있지만 Gold를 막는 경고는 같은 단계에서
            # 검토 대상으로 승격한다. 선택 정보 충돌(blocks_gold 없음)은
            # 기존처럼 Silver에 저장하고 Gold에서 NULL로 처리한다.
            for warning in record_warnings:
                blockers = [
                    str(target)
                    for target in warning.get("blocks_gold", [])
                    if target
                ]
                if not blockers:
                    continue
                gold_targets.update(blockers)
                gold_preflight_blocked = True
                warning_code = str(
                    warning.get("warning_code", "GOLD_PRECHECK_BLOCKED")
                )
                _add_issue(
                    issues,
                    str(warning.get("field", "record")),
                    f"GOLD_PRECHECK_{warning_code}",
                    warning.get("value"),
                )
        identity_value = record.get(identity_field)
        if identity_value in duplicate_values:
            issues.append({
                "field": identity_field,
                "error_code": "DUPLICATE_ID",
                "value": identity_value,
            })

        if issues:
            review_item = {
                "record": record,
                "issues": issues,
                "warnings": record_warnings,
                "status": "REVIEW_REQUIRED",
                # 한 컬렉션을 사용하되, Silver와 Gold 검토를 구분한다.
                "review_stage": "GOLD" if gold_preflight_blocked else "SILVER",
                "failure_stage": (
                    "GOLD_PREFLIGHT"
                    if gold_preflight_blocked
                    else item.get("failure_stage") or _failure_stage(issues)
                ),
                "scope": "RECORD",
                "severity": "ERROR",
                "blocks_silver": True,
                "blocks_gold": ["*"],
            }
            if gold_targets:
                review_item["gold_targets"] = sorted(gold_targets)
            if gold_preflight_blocked:
                review_item["gold_preflight"] = True
            for key in (
                "bronze_id",
                "bronze_run_id",
                "source_record_id",
                "source_record_sha256",
            ):
                if key in item:
                    review_item[key] = item[key]
            quarantine.append(review_item)
        else:
            accepted.append(record)
            accepted_item = deepcopy(item)
            accepted_item["warnings"] = record_warnings
            accepted_item["status"] = (
                "PASS_WITH_WARNING" if record_warnings else "PASS"
            )
            accepted_items.append(accepted_item)
            if record_warnings:
                warning_item = {
                    "record": record,
                    "warnings": record_warnings,
                    "status": "PASS_WITH_WARNING",
                    "failure_stage": "QUALITY_WARNING",
                    "scope": (
                        record_warnings[0].get("scope", "RECORD")
                        if len({warning.get("scope") for warning in record_warnings}) == 1
                        else "MULTIPLE"
                    ),
                    "severity": "WARNING",
                    "blocks_silver": False,
                    "blocks_gold": sorted({
                        target
                        for warning in record_warnings
                        for target in warning.get("blocks_gold", [])
                    }),
                }
                for key in (
                    "bronze_id",
                    "bronze_run_id",
                    "source_record_id",
                    "source_record_sha256",
                ):
                    if key in item:
                        warning_item[key] = item[key]
                warnings.append(warning_item)
                warning_reviews.append(warning_item)

    return {
        "accepted": accepted,
        "accepted_items": accepted_items,
        "quarantine": quarantine,
        "warnings": warnings,
        "warning_reviews": warning_reviews,
        "accepted_count": len(accepted),
        "quarantine_count": len(quarantine),
        # 한 레코드에 여러 선택 필드 경고가 있을 수 있으므로, 레코드 수가
        # 아니라 실제 경고 항목 수를 반환한다.
        "warning_count": sum(
            len(item.get("warnings", []))
            for item in warning_reviews
        ),
    }


class ReviewService:
    """검토 큐 조회와 승인·반려·재처리를 담당한다."""

    def __init__(
        self,
        repository: ReviewQueueRepository | None = None,
        bronze_repository: Any | None = None,
        silver_repository: Any | None = None,
    ) -> None:
        self.repository = repository or ReviewQueueRepository()
        self.bronze_repository = bronze_repository
        self.silver_repository = silver_repository

    def list_pending(self, limit: int = 100) -> list[dict[str, Any]]:
        """아직 담당자가 결정하지 않은 데이터를 조회한다."""

        return self.repository.list_pending(limit=limit)

    def decide(
        self,
        review_id: str,
        decision: str,
        reviewer: str,
        note: str | None = None,
        corrected_values: Mapping[str, Any] | None = None,
    ) -> bool:
        """검토 결과를 기록한다."""

        return self.repository.save_decision(
            review_id=review_id,
            decision=decision,
            reviewer=reviewer,
            note=note,
            corrected_values=corrected_values,
        )

    @staticmethod
    def _apply_corrections(
        bronze_record: Mapping[str, Any],
        corrected_values: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        """승인된 수정값을 Bronze 복사본의 payload에만 적용한다."""

        corrected = deepcopy(dict(bronze_record))
        values = dict(corrected_values or {})
        payload_value = corrected.get("payload")
        payload = dict(payload_value) if isinstance(payload_value, Mapping) else {}
        mapping = load_field_mapping()
        silver_to_source = {silver: source for source, silver in mapping.items()}

        if "payload" in values:
            replacement = values.pop("payload")
            if not isinstance(replacement, Mapping):
                raise ValueError("REPROCESS_PAYLOAD_INVALID: payload 수정값은 객체여야 합니다.")
            payload = dict(replacement)

        for field, value in values.items():
            source_field = field if field in mapping else silver_to_source.get(field)
            if source_field is None:
                raise ValueError(f"REPROCESS_FIELD_INVALID: 알 수 없는 수정 필드 {field}")
            payload[source_field] = value

        corrected["payload"] = payload
        return corrected

    def reprocess(self, review_id: str, write: bool = False) -> dict[str, Any]:
        """승인된 검토 건을 Bronze에서 다시 처리한다."""

        review = self.repository.find_by_id(review_id)
        if not review:
            return {"status": "NOT_FOUND", "review_id": review_id}
        if review.get("review_status") != ReviewQueueRepository.APPROVED_STATUS:
            return {"status": "NOT_APPROVED", "review_id": review_id}

        bronze_id = review.get("bronze_id")
        rule_version = load_rule_version()
        if not bronze_id:
            details = {"error_code": "BRONZE_REFERENCE_MISSING"}
            self.repository.mark_reprocessed(review_id, "FAILED", rule_version, details)
            return {"status": "FAILED", "review_id": review_id, **details}

        if self.bronze_repository is None:
            from .mongo_storage import BronzeRepository

            bronze_repository = BronzeRepository()
        else:
            bronze_repository = self.bronze_repository
        bronze = bronze_repository.find_by_bronze_id(bronze_id)
        if not bronze:
            details = {"error_code": "BRONZE_NOT_FOUND", "bronze_id": bronze_id}
            self.repository.mark_reprocessed(review_id, "FAILED", rule_version, details)
            return {"status": "FAILED", "review_id": review_id, **details}

        silver_repository = self.silver_repository
        if silver_repository is not None:
            list_existing = getattr(silver_repository, "list_records", None)
            existing_records = list_existing() if callable(list_existing) else []
        else:
            from .mongo_storage import SilverRepository, get_mongo_database

            silver_collection = get_mongo_database()[SilverRepository.COLLECTION_NAME]
            existing_records = list(silver_collection.find({}))
            # 실제 저장을 요청한 경우에만 인덱스를 준비한다.
            if write:
                silver_repository = SilverRepository()
        known_area_ids = {
            str(record.get("area_id"))
            for record in existing_records
            if record.get("area_id")
        }
        relationship_policy = load_domain_rule("organization_relationship")
        reference_sources = {
            str(source)
            for source in relationship_policy.get("reference_sources", [])
            if isinstance(source, str)
        }
        if "current_bronze_batch" in reference_sources:
            bronze_collection = getattr(bronze_repository, "collection", None)
            if bronze_collection is not None:
                try:
                    bronze_records = bronze_collection.find(
                        {}, {"payload.area_no": 1}
                    )
                except TypeError:
                    bronze_records = bronze_collection.find({})
                for bronze_record in bronze_records:
                    payload = bronze_record.get("payload")
                    area_id = payload.get("area_no") if isinstance(payload, Mapping) else None
                    if isinstance(area_id, str) and area_id.strip():
                        known_area_ids.add(
                            re.sub(r"[-_\s]", "", area_id.upper())
                        )

        try:
            corrected = self._apply_corrections(bronze, review.get("corrected_values"))
            normalized = normalize_bronze_record(corrected)
            normalized_area_id = normalized.get("record", {}).get("area_id")
            if normalized_area_id:
                known_area_ids.add(str(normalized_area_id))
            checked = review_records(
                [normalized],
                # 승인된 재처리는 명시적 갱신이므로 기존 값 충돌 검사를 건너뛴다.
                existing_records=None,
                known_area_ids=known_area_ids,
                # 재처리는 Silver 검증만 다시 수행한다. Gold 조건은 Gold
                # 단계에서 별도로 검사한다.
                gold_preflight=False,
            )
        except (TypeError, ValueError) as error:
            details = {
                "error_code": str(error).split(":", 1)[0],
                "message": str(error),
            }
            self.repository.mark_reprocessed(review_id, "FAILED", rule_version, details)
            return {"status": "FAILED", "review_id": review_id, **details}

        if checked["quarantine"]:
            details = {
                "error_code": "REPROCESS_VALIDATION_FAILED",
                "issues": checked["quarantine"][0].get("issues", []),
            }
            self.repository.mark_reprocessed(review_id, "FAILED", rule_version, details)
            return {"status": "FAILED", "review_id": review_id, **details}

        saved = 0
        status = "VALIDATED"
        if write:
            force_save = getattr(silver_repository, "force_save_records", None)
            if callable(force_save):
                saved = force_save(checked["accepted"])
            else:
                # 기존 테스트용 저장소와의 호환을 유지한다.
                saved = silver_repository.save_records(checked["accepted"])
            status = "SILVER_SAVED"

        details = {"bronze_id": bronze_id, "silver_saved": saved, "write": write}
        self.repository.mark_reprocessed(review_id, status, rule_version, details)
        return {"status": status, "review_id": review_id, **details}
