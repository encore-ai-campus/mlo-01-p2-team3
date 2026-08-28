"""Bronze payload의 매핑·정규화·규칙 버전을 한 곳에서 관리한다.

필드명 매핑은 ``domains.yaml``의 계약을 따르고, 값 정규화는 같은 YAML과
``date_formats.yaml``을 읽는다. 판단할 수 없는 값은 추정하지 않고 이슈로
반환해 검토 단계에서 처리한다.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from functools import lru_cache
import re
from pathlib import Path
import unicodedata
from typing import Any

import yaml


RULES_DIR = Path(__file__).resolve().parent / "rules"
RULES_PATH = RULES_DIR / "domains.yaml"
DOMAIN_RULES_PATH = RULES_PATH
DATE_RULES_PATH = RULES_DIR / "date_formats.yaml"


def _read_yaml(path: Path) -> dict[str, Any]:
    """YAML 파일을 읽는다. 파일이 바뀌었을 때만 다시 파싱한다."""

    modified_ns = path.stat().st_mtime_ns
    return _read_yaml_cached(path, modified_ns)


@lru_cache(maxsize=4)
def _read_yaml_cached(path: Path, modified_ns: int) -> dict[str, Any]:
    """같은 파일 버전의 YAML은 메모리에서 재사용한다."""

    with path.open("r", encoding="utf-8") as file:
        value = yaml.safe_load(file) or {}
    if not isinstance(value, dict):
        raise ValueError(f"NORMALIZATION_RULES_INVALID: {path.name}")
    return value


def _load_rules() -> dict[str, Any]:
    """domains.yaml을 읽는다."""

    return _read_yaml(RULES_PATH)


def load_domain_rule(name: str) -> dict[str, Any]:
    """domains.yaml의 특정 규칙 묶음을 반환한다."""

    value = _load_rules().get(name, {})
    return value if isinstance(value, dict) else {}


def load_field_mapping() -> dict[str, str]:
    """YAML의 원본 필드 → Silver 필드 매핑을 반환한다."""

    mapping = _load_rules().get("contract", {}).get("field_mapping")
    if not isinstance(mapping, dict) or not mapping:
        raise ValueError("MAPPING_RULES_MISSING: 필드 매핑 규칙이 없습니다.")
    if not all(
        isinstance(source, str) and isinstance(target, str)
        for source, target in mapping.items()
    ):
        raise ValueError("MAPPING_RULES_INVALID: 필드명은 문자열이어야 합니다.")
    return mapping


def extract_payload(record: Mapping[str, Any]) -> Mapping[str, Any]:
    """Bronze envelope에서 업무 원문 payload만 꺼낸다."""

    if not isinstance(record, Mapping):
        raise ValueError("MAPPING_RECORD_INVALID: 원문 레코드는 객체여야 합니다.")
    payload = record.get("payload")
    if not isinstance(payload, Mapping):
        raise ValueError("MAPPING_PAYLOAD_INVALID: payload 객체가 없습니다.")
    return payload


def map_record(record: Mapping[str, Any]) -> dict[str, Any]:
    """Bronze 한 건의 payload를 Silver 필드명으로 변환한다."""

    payload = extract_payload(record)
    mapping = load_field_mapping()
    expected_fields = set(mapping)
    payload_fields = set(payload)
    missing = sorted(expected_fields - payload_fields)
    extra = sorted(payload_fields - expected_fields)
    if missing or extra:
        raise ValueError(
            f"MAPPING_PAYLOAD_SCHEMA_MISMATCH: missing={missing}, extra={extra}"
        )

    result: dict[str, Any] = {}
    for source_field, silver_field in mapping.items():
        value = payload[source_field]
        if value is not None and not isinstance(value, str):
            raise ValueError(
                f"MAPPING_PAYLOAD_TYPE_INVALID: {source_field}는 문자열 또는 NULL이어야 합니다."
            )
        result[silver_field] = value
    return result


def normalize_bronze_record(record: Mapping[str, Any]) -> dict[str, Any]:
    """Bronze 원문 한 건을 메모리에서 매핑·정규화한다.

    이 함수는 Bronze 문서나 별도 후보 컬렉션에 쓰지 않는다. 반환된 임시
    결과는 검증 단계로만 전달하고, 검증 후 Silver 또는 검토 큐에서 최종
    저장한다.
    """

    return normalize_record(map_record(record))


def map_records(records: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """여러 Bronze 원문을 같은 규칙으로 매핑한다."""

    return [map_record(record) for record in records]


def _clean(value: Any, null_tokens: set[str]) -> Any:
    """문자열의 유니코드와 앞뒤 공백만 정리한다."""

    if value is None:
        return None
    if not isinstance(value, str):
        return value
    cleaned = unicodedata.normalize("NFKC", value).strip()
    normalized_nulls = {_lookup_key(token) for token in null_tokens}
    return None if _lookup_key(cleaned) in normalized_nulls else cleaned


def _lookup_key(value: Any) -> str:
    """승인된 별칭을 공백·대소문자 차이 없이 비교한다."""

    normalized = unicodedata.normalize("NFKC", str(value)).strip()
    return re.sub(r"\s+", " ", normalized).casefold()


def _apply_whitespace_policy(value: Any, policy: Mapping[str, Any]) -> Any:
    """필드 정책에 따라 내부 공백을 정리한다.

    기본값은 앞뒤 공백만 정리한다. 이름처럼 내부 공백도 의미가 없는
    필드만 YAML에서 ``remove_all``을 명시한다.
    """

    if value is None or not isinstance(value, str):
        return value
    mode = str(policy.get("whitespace", "trim_outer")).lower()
    if mode == "remove_all":
        return re.sub(r"\s+", "", value)
    if mode == "collapse_internal":
        return re.sub(r"\s+", " ", value).strip()
    return value


def _issue(field: str, code: str, value: Any) -> dict[str, Any]:
    return {"field": field, "error_code": code, "value": value}


def _warning(
    field: str,
    code: str,
    value: Any,
    scope: str = "RECORD",
    blocks_gold: list[str] | None = None,
) -> dict[str, Any]:
    """Silver 저장을 막지 않는 품질 경고를 만든다."""

    return {
        "field": field,
        "warning_code": code,
        "value": value,
        "scope": scope,
        "severity": "WARNING",
        "blocks_silver": False,
        "blocks_gold": list(blocks_gold or []),
    }


def _normalize_identifier(
    field: str,
    value: Any,
    pattern: str,
    error_code: str,
    issues: list[dict[str, Any]],
    action: str = "REVIEW_REQUIRED",
    warnings: list[dict[str, Any]] | None = None,
    warning_code: str | None = None,
    warning_scope: str = "RECORD",
    warning_blocks_gold: list[str] | None = None,
) -> Any:
    """ID의 구분 기호를 제거하고 형식을 확인한다."""

    if value is None:
        if action.upper().startswith("WARNING") and warnings is not None:
            warnings.append(_warning(
                field,
                warning_code or error_code,
                value,
                scope=warning_scope,
                blocks_gold=warning_blocks_gold,
            ))
        else:
            issues.append(_issue(field, error_code, value))
        return None
    normalized = re.sub(r"[-_\s]", "", str(value).upper())
    if not re.fullmatch(pattern, normalized):
        if action.upper().startswith("WARNING") and warnings is not None:
            warnings.append(_warning(
                field,
                warning_code or error_code,
                value,
                scope=warning_scope,
                blocks_gold=warning_blocks_gold,
            ))
            return None
        issues.append(_issue(field, error_code, value))
    return normalized


def _parse_datetime(
    field: str,
    value: Any,
    formats: dict[str, str],
    allowed_formats: list[str],
    null_tokens: set[str],
    sentinel_tokens: set[str],
    sentinel_warning_code: str,
    required: bool,
    issues: list[dict[str, Any]],
    warnings: list[dict[str, Any]],
) -> datetime | None:
    """정의된 형식만 읽고 UTC datetime으로 반환한다."""

    cleaned = _clean(value, null_tokens)
    if cleaned is None:
        if required:
            issues.append(_issue(field, "REQUIRED_VALUE_MISSING", value))
        return None
    if str(cleaned) in sentinel_tokens:
        warnings.append(_warning(field, sentinel_warning_code, value))
        return None
    if isinstance(cleaned, datetime):
        parsed = cleaned
    else:
        parsed = None
        for format_name in allowed_formats:
            format_value = formats.get(format_name)
            if not format_value:
                continue
            try:
                parsed = datetime.strptime(str(cleaned), format_value)
                break
            except ValueError:
                continue
        if parsed is None:
            issues.append(_issue(field, "INVALID_DATETIME_FORMAT", value))
            return None

    source_zone = timezone(timedelta(hours=9))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=source_zone)
    return parsed.astimezone(timezone.utc)


def normalize_record(record: dict[str, Any]) -> dict[str, Any]:
    """매핑된 한 건을 형식·도메인 규칙에 따라 표준화한다."""

    if not isinstance(record, dict):
        raise ValueError("NORMALIZATION_RECORD_INVALID: 정규화 대상은 객체여야 합니다.")

    domains = _read_yaml(DOMAIN_RULES_PATH)
    date_rules = _read_yaml(DATE_RULES_PATH)
    null_tokens = {str(token) for token in domains.get("null_tokens", [])}
    field_policies = domains.get("field_policies", {})
    result: dict[str, Any] = {}
    for field, value in record.items():
        policy = field_policies.get(field, {})
        field_null_tokens = null_tokens | {
            str(token) for token in policy.get("null_tokens", [])
        }
        result[field] = _apply_whitespace_policy(
            _clean(value, field_null_tokens), policy
        )
    issues: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []

    identifiers = domains.get("identifiers", {})
    organization = identifiers.get("organization_id", {})
    for field in organization.get("silver_fields", []):
        result[field] = _normalize_identifier(
            field,
            result.get(field),
            organization.get("pattern", r".*"),
            "INVALID_ORGANIZATION_ID",
            issues,
        )

    # 최상위 조직은 YAML 정책에 따라 부모가 없어도 정상이다. ID 형식 검사가
    # 먼저 실행되면서 추가한 parent_area_id NULL 오류만 이 경우 제거한다.
    relationship = domains.get("organization_relationship", {})
    if (
        relationship.get("root_parent_null_allowed")
        and result.get("area_id") == result.get("top_area_id")
        and result.get("parent_area_id") is None
    ):
        issues = [
            issue
            for issue in issues
            if not (
                issue.get("field") == "parent_area_id"
                and issue.get("error_code") == "INVALID_ORGANIZATION_ID"
                and issue.get("value") is None
            )
        ]
    employee = identifiers.get("employee_id", {})
    manager_id = employee.get("silver_field", "manager_id")
    manager_policy = field_policies.get("manager_id", {})
    manager_value = result.get(manager_id)
    manager_warning_scope = str(manager_policy.get("warning_scope", "MANAGER"))
    manager_blocks_gold = [
        str(value) for value in manager_policy.get("warning_blocks_gold", [])
    ]
    if manager_value is not None and _lookup_key(manager_value) in {
        _lookup_key(token) for token in manager_policy.get("unknown_tokens", [])
    }:
        warnings.append(_warning(
            manager_id,
            str(manager_policy.get("unknown_error_code", "UNKNOWN_MANAGER_ID")),
            manager_value,
            scope=manager_warning_scope,
            blocks_gold=manager_blocks_gold,
        ))
        result[manager_id] = None
    else:
        result[manager_id] = _normalize_identifier(
            manager_id,
            manager_value,
            employee.get("pattern", r".*"),
            "INVALID_EMPLOYEE_ID",
            issues,
            action=str(employee.get("invalid_action", "REVIEW_REQUIRED")),
            warnings=warnings,
            warning_code=str(
                employee.get("invalid_warning_code", "INVALID_EMPLOYEE_ID")
            ),
            warning_scope=str(employee.get("warning_scope", "MANAGER")),
            warning_blocks_gold=[
                str(value) for value in employee.get("warning_blocks_gold", [])
            ],
        )

    status_rule = domains.get("status_domains", {}).get("manager_active_yn", {})
    status_field = status_rule.get("silver_field", "manager_active_yn")
    status_value = result.get(status_field)
    status_mappings = status_rule.get("mappings", {})
    status_result = None
    if status_value is not None:
        status_key = _lookup_key(status_value)
        for output, values in status_mappings.items():
            if status_key in {_lookup_key(item) for item in values}:
                status_result = output
                break
        if status_result is None:
            status_code = str(
                status_rule.get("unknown_error_code", "UNKNOWN_STATUS")
            )
            if str(status_rule.get("unknown_action", "REVIEW_REQUIRED")).upper().startswith("WARNING"):
                warnings.append(_warning(
                    status_field,
                    status_code,
                    status_value,
                    scope=str(status_rule.get("warning_scope", "MANAGER")),
                    blocks_gold=[
                        str(value)
                        for value in status_rule.get("warning_blocks_gold", [])
                    ],
                ))
            else:
                issues.append(_issue(status_field, status_code, status_value))
    else:
        if str(status_rule.get("required_action", "REVIEW_REQUIRED")).upper().startswith("WARNING"):
            warnings.append(_warning(
                status_field,
                "REQUIRED_VALUE_MISSING",
                None,
                scope=str(status_rule.get("warning_scope", "MANAGER")),
                blocks_gold=[
                    str(value)
                    for value in status_rule.get("warning_blocks_gold", [])
                ],
            ))
        else:
            issues.append(_issue(status_field, "REQUIRED_VALUE_MISSING", None))
    result[status_field] = status_result

    level_rule = domains.get("organization_level", {})
    level_field = level_rule.get("silver_field", "top_area_level")
    level_value = result.get(level_field)
    if level_value is not None:
        allowed_levels = {str(item) for item in level_rule.get("allowlist", [])}
        standard_level = None
        level_key = _lookup_key(level_value)
        for canonical, aliases in level_rule.get("mappings", {}).items():
            canonical_value = str(canonical)
            if canonical_value not in allowed_levels:
                continue
            alias_values = {_lookup_key(item) for item in aliases}
            alias_values.add(_lookup_key(canonical_value))
            if level_key in alias_values:
                standard_level = canonical_value
                break
        if standard_level is None:
            allowed_by_key = {
                _lookup_key(item): item for item in allowed_levels
            }
            standard_level = allowed_by_key.get(level_key)
        if standard_level is None:
            issues.append(_issue(
                level_field,
                level_rule.get(
                    "unknown_error_code",
                    "UNREGISTERED_ORGANIZATION_LEVEL",
                ),
                level_value,
            ))
        else:
            result[level_field] = standard_level
    elif level_rule.get("allowlist_required"):
        issues.append(_issue(
            level_field,
            level_rule.get(
                "unknown_error_code",
                "UNREGISTERED_ORGANIZATION_LEVEL",
            ),
            level_value,
        ))

    format_map = {
        item["name"]: item["strptime"]
        for item in date_rules.get("input_formats", [])
        if isinstance(item, dict) and "name" in item and "strptime" in item
    }
    date_null_tokens = null_tokens | {
        str(token) for token in date_rules.get("invalid_tokens", [])
    }
    sentinel_tokens = {
        str(token) for token in date_rules.get("sentinel_tokens", [])
    }
    sentinel_warning_code = str(
        date_rules.get("sentinel_warning_code", "SOURCE_SENTINEL_DATE")
    )
    for field, policy in date_rules.get("field_policy", {}).items():
        silver_field = {
            "mgr_hire_dtm": "manager_hire_at",
            "area_reg_dtm": "area_registered_at",
            "top_area_reg_dtm": "top_area_registered_at",
        }.get(field, field)
        if silver_field not in result:
            continue
        result[silver_field] = _parse_datetime(
            silver_field,
            result.get(silver_field),
            format_map,
            policy.get("formats", []),
            date_null_tokens,
            sentinel_tokens,
            sentinel_warning_code,
            bool(policy.get("required", False)),
            issues,
            warnings,
        )

    name_policy = field_policies.get("manager_name", {})
    if name_policy.get("required") and result.get("manager_name") is None:
        if str(name_policy.get("required_action", "REVIEW_REQUIRED")).upper().startswith("WARNING"):
            warnings.append(_warning(
                "manager_name",
                "REQUIRED_VALUE_MISSING",
                None,
                scope=str(name_policy.get("warning_scope", "MANAGER")),
                blocks_gold=[
                    str(value)
                    for value in name_policy.get("warning_blocks_gold", [])
                ],
            ))
        else:
            issues.append(_issue("manager_name", "REQUIRED_VALUE_MISSING", None))
    manager_name = result.get("manager_name")
    if manager_name is not None and _lookup_key(manager_name) in {
        _lookup_key(value) for value in name_policy.get("review_tokens", [])
    }:
        name_code = str(
            name_policy.get("review_error_code", "INVALID_MANAGER_NAME")
        )
        if str(name_policy.get("review_action", "REVIEW_REQUIRED")).upper().startswith("WARNING"):
            warnings.append(_warning(
                "manager_name",
                name_code,
                manager_name,
                scope=str(name_policy.get("warning_scope", "MANAGER")),
                blocks_gold=[
                    str(value)
                    for value in name_policy.get("warning_blocks_gold", [])
                ],
            ))
            result["manager_name"] = None
        else:
            issues.append(_issue("manager_name", name_code, manager_name))

    for field in ("department_name", "position_name"):
        value = result.get(field)
        policy = field_policies.get(field, {})
        reference_tokens = {
            _lookup_key(token) for token in policy.get("reference_tokens", [])
        }
        reference_mappings = {
            _lookup_key(source): target
            for source, target in policy.get("reference_mappings", {}).items()
        }
        value_key = _lookup_key(value) if value is not None else None
        if value is None or value_key not in reference_tokens:
            continue

        mapped_value = reference_mappings.get(value_key)
        action = str(policy.get("reference_action", "REVIEW_REQUIRED")).upper()
        if mapped_value is not None and action == "WARNING":
            result[field] = mapped_value
            warnings.append(_warning(
                field,
                str(policy.get("reference_warning_code", "OTHER_REFERENCE_VALUE")),
                value,
            ))
        else:
            issues.append(_issue(field, "UNREGISTERED_REFERENCE_VALUE", value))

    processing_status = domains.get("processing_status", {})
    if issues:
        status = str(processing_status.get("review_required", "REVIEW_REQUIRED"))
    elif warnings:
        status = str(processing_status.get("warning", "PASS_WITH_WARNING"))
    else:
        status = str(processing_status.get("pass", "PASS"))

    return {
        "record": result,
        "issues": issues,
        "warnings": warnings,
        "status": status,
    }


def normalize_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """여러 건을 순서대로 표준화한다."""

    return [normalize_record(record) for record in records]


def load_rule_version() -> str:
    """domains.yaml의 rule_version을 반환한다."""

    version = _load_rules().get("rule_version")
    if not isinstance(version, str) or not version.strip():
        raise ValueError("RULE_VERSION_MISSING: domains.yaml에 rule_version이 없습니다.")
    return version.strip()
