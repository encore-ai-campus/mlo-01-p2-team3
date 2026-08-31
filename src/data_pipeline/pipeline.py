"""API 수집과 실행 이력을 한 곳에서 관리하는 파이프라인 모듈.

이 파일은 API 클라이언트, Meta 계약 검사, API → Bronze 흐름을 담당한다.
기존 세부 모듈은 하위 호환용으로 이 모듈의 함수를 다시 내보낸다.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

import requests
import yaml
from dotenv import load_dotenv

from .mongo_storage import (
    BronzeRepository,
    ControlRepository,
    PipelinePageRepository,
    PipelineRunRepository,
)
from .raw_csv import RawCsvRepository
from .raw_archive import RawArchiveRepository
from .transform import load_rule_version as _load_rule_version


PROJECT_ROOT = Path(__file__).resolve().parents[2]
RULES_PATH = Path(__file__).resolve().parent / "rules" / "domains.yaml"
load_dotenv(PROJECT_ROOT / ".env")
PAGE_LIMIT = 1000
KST = timezone(timedelta(hours=9))


def _load_rules() -> dict[str, Any]:
    """domains.yaml을 읽는다."""

    with RULES_PATH.open("r", encoding="utf-8") as file:
        rules = yaml.safe_load(file) or {}
    if not isinstance(rules, dict):
        raise ValueError("RULES_INVALID: 도메인 규칙 형식이 올바르지 않습니다.")
    return rules


def load_expected_fields() -> list[str]:
    """YAML의 논리 원본 필드 목록을 반환한다."""

    fields = _load_rules().get("contract", {}).get("raw_fields")
    if not isinstance(fields, list) or not all(isinstance(field, str) for field in fields):
        raise ValueError("RULE_RAW_FIELDS_MISSING: 원본 필드 설정이 없습니다.")
    return fields


def load_expected_meta_fields() -> list[str]:
    """YAML의 API Meta 컬럼 목록을 반환한다."""

    fields = _load_rules().get("contract", {}).get("meta_fields")
    if not isinstance(fields, list) or not all(isinstance(field, str) for field in fields):
        raise ValueError("RULE_META_FIELDS_MISSING: Meta 컬럼 설정이 없습니다.")
    return fields


def validate_meta(meta: dict[str, Any]) -> dict[str, Any]:
    """Meta JSON의 필수값과 15개 컬럼을 확인한다."""

    if not isinstance(meta, dict):
        raise ValueError("META_RESPONSE_INVALID: Meta 응답은 JSON 객체여야 합니다.")

    required = ("released_rows", "next_refresh_at", "columns")
    missing = [name for name in required if name not in meta]
    if missing:
        raise ValueError(f"META_REQUIRED_FIELD_MISSING: {missing}")

    released_rows = meta["released_rows"]
    if (
        isinstance(released_rows, bool)
        or not isinstance(released_rows, int)
        or released_rows < 0
    ):
        raise ValueError("META_RELEASED_ROWS_INVALID: 정수가 아닙니다.")

    next_refresh_at = meta["next_refresh_at"]
    if not isinstance(next_refresh_at, str) or not next_refresh_at.strip():
        raise ValueError("META_REFRESH_TIME_INVALID: 시간이 없습니다.")

    columns = meta["columns"]
    if not isinstance(columns, list) or not all(isinstance(column, str) for column in columns):
        raise ValueError("META_COLUMNS_INVALID: 컬럼 목록이 올바르지 않습니다.")

    expected = load_expected_meta_fields()
    if columns != expected:
        raise ValueError("API_SCHEMA_MISMATCH: Meta 15개 컬럼과 다릅니다.")

    return {
        "released_rows": released_rows,
        "next_refresh_at": next_refresh_at,
        "columns": columns,
    }


class ApiClient:
    """API 요청만 담당한다. 저장·정규화는 다른 흐름에서 수행한다."""

    def __init__(
        self,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self.base_url = os.getenv(
            "API_BASE_URL", "http://192.168.0.51:8000"
        ).rstrip("/")
        self.timeout = int(os.getenv("API_TIMEOUT_SECONDS", "10"))
        # API가 허용하는 페이지 크기인 1,000건으로 고정한다.
        self.page_limit = PAGE_LIMIT
        # 일시적인 API·네트워크 오류만 재시도한다. 기본값은 3회다.
        try:
            self.max_retries = max(0, int(os.getenv("API_MAX_RETRIES", "3")))
        except ValueError:
            self.max_retries = 3
        try:
            self.retry_backoff_seconds = max(
                0.0,
                float(os.getenv("API_RETRY_BACKOFF_SECONDS", "1")),
            )
        except ValueError:
            self.retry_backoff_seconds = 1.0
        self.sleep = sleep or time.sleep
        # API 키는 저장하지 않는다. 응답 원문은 파일 보관 전까지만 메모리에 둔다.
        self.last_response_info: dict[str, Any] = {}
        # records 응답만 PipelineService가 파일로 보존한다.
        self.last_response_body: bytes = b""

    @staticmethod
    def _is_retryable_status(status: Any) -> bool:
        """재시도할 HTTP 상태인지 확인한다."""

        return (
            isinstance(status, int)
            and not isinstance(status, bool)
            and (status in {408, 429} or 500 <= status < 600)
        )

    def _get(
        self,
        path: str,
        api_key: str | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        """GET 요청을 보내고 JSON 응답을 반환한다."""

        headers = {"Accept": "application/json"}
        if api_key:
            headers["X-API-Key"] = api_key
        params = {
            key: value
            for key, value in (params or {}).items()
            if value is not None
        }
        source_url = f"{self.base_url}{path}"
        for retry_count in range(self.max_retries + 1):
            requested_at = datetime.now(timezone.utc)
            started = time.perf_counter()
            self.last_response_body = b""
            try:
                response = requests.get(
                    source_url,
                    headers=headers,
                    params=params,
                    timeout=self.timeout,
                )
            except Exception as error:
                # requests의 통신 예외는 같은 요청을 다시 시도한다.
                self.last_response_info = {
                    "http_status": None,
                    "requested_at": requested_at.isoformat(),
                    "received_at": datetime.now(timezone.utc).isoformat(),
                    "latency_ms": round((time.perf_counter() - started) * 1000, 2),
                    "error_code": type(error).__name__,
                    "source_url": source_url,
                    "retry_count": retry_count,
                }
                if retry_count < self.max_retries:
                    self.sleep(self.retry_backoff_seconds * (2 ** retry_count))
                    continue
                raise

            status = getattr(response, "status_code", 200)
            received_at = datetime.now(timezone.utc)
            response_headers = getattr(response, "headers", {}) or {}
            response_info = {
                "http_status": status,
                "requested_at": requested_at.isoformat(),
                "received_at": received_at.isoformat(),
                "latency_ms": round((time.perf_counter() - started) * 1000, 2),
                "source_url": source_url,
                "retry_count": retry_count,
            }

            if self._is_retryable_status(status) and retry_count < self.max_retries:
                # 중간 응답 본문은 저장하지 않고 최종 응답만 Bronze 원문으로 남긴다.
                self.last_response_info = response_info
                close = getattr(response, "close", None)
                if callable(close):
                    close()
                self.sleep(self.retry_backoff_seconds * (2 ** retry_count))
                continue

            content = getattr(response, "content", None)
            if isinstance(content, (bytes, bytearray)):
                self.last_response_body = bytes(content)
            else:
                try:
                    self.last_response_body = json.dumps(
                        response.json(), ensure_ascii=False
                    ).encode("utf-8")
                except Exception:
                    self.last_response_body = b""

            response_hash = hashlib.sha256(self.last_response_body).hexdigest()
            response_info.update({
                "response_hash": response_hash,
                "response_size_bytes": len(self.last_response_body),
                "content_type": response_headers.get("Content-Type"),
            })
            self.last_response_info = response_info

            try:
                response.raise_for_status()
                # 테스트용 응답 객체가 raise_for_status를 생략해도
                # 실제 HTTP 오류를 성공으로 처리하지 않는다.
                if isinstance(status, int) and status >= 400:
                    raise requests.HTTPError(
                        f"HTTP {status}",
                        response=response,
                    )
            except Exception as error:
                self.last_response_info["error_code"] = type(error).__name__
                raise
            try:
                return response.json()
            except Exception as error:
                self.last_response_info["error_code"] = type(error).__name__
                raise

        # range가 모두 소진되지만, 정적 분석기가 반환값을 요구할 수 있다.
        raise RuntimeError("API_RETRY_EXHAUSTED")

    def get_daily_api_key(self) -> str:
        """공개 엔드포인트에서 오늘 사용할 API 키를 가져온다."""

        data = self._get("/public/v1/key")
        if isinstance(data, str):
            return data
        api_key = data.get("api_key") or data.get("key")
        if not api_key:
            raise ValueError("API 키가 응답에 없습니다.")
        return api_key

    def get_meta(self, api_key: str) -> dict[str, Any]:
        """공개 행 수·다음 공개 시각·컬럼을 조회한다."""

        return self._get("/api/v1/meta", api_key=api_key)

    def get_records(
        self,
        api_key: str,
        cursor: str | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        """첫 페이지 또는 cursor 페이지를 1,000건 조회한다."""

        # limit 인자는 호출부 호환용이며 실제 요청값은 항상 1,000이다.
        return self._get(
            "/api/v1/records",
            api_key=api_key,
            params={"cursor": cursor, "limit": self.page_limit},
        )

    def get_record(self, api_key: str, record_id: str | int) -> dict[str, Any]:
        """공개된 단일 레코드를 조회한다."""

        return self._get(f"/api/v1/records/{record_id}", api_key=api_key)

    def check_ready(self) -> dict[str, Any]:
        """API와 데이터셋 준비 상태를 확인한다."""

        return self._get("/health/ready")


class PipelineService:
    """API 페이지를 읽고 원문 items를 Bronze·CSV에 저장한다."""

    def __init__(
        self,
        api_client: ApiClient | None = None,
        bronze_repository: BronzeRepository | None = None,
        control_repository: ControlRepository | None = None,
        run_repository: PipelineRunRepository | None = None,
        page_repository: PipelinePageRepository | None = None,
        raw_csv_repository: RawCsvRepository | None = None,
        raw_archive_repository: RawArchiveRepository | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.api = api_client or ApiClient()
        self.bronze = bronze_repository or BronzeRepository()
        self.control = control_repository or ControlRepository()
        self.run_repository = run_repository
        self.page_repository = page_repository
        self.raw_csv = raw_csv_repository
        self.raw_archive = raw_archive_repository
        self.sleep = sleep

    def _save_page(
        self,
        run_id: str | None,
        page_no: int,
        cursor: str | None,
        next_cursor: str | None,
        item_count: int,
        next_refresh_at: str | None,
        error_code: str | None = None,
    ) -> None:
        """페이지의 안전한 HTTP 메타데이터를 저장한다."""

        if not run_id or self.page_repository is None:
            return
        info = dict(getattr(self.api, "last_response_info", {}) or {})
        self.page_repository.save_page(
            run_id=run_id,
            page_no=page_no,
            cursor=cursor,
            next_cursor=next_cursor,
            response_hash=info.get("response_hash", ""),
            item_count=item_count,
            next_refresh_at=next_refresh_at,
            http_status=info.get("http_status"),
            requested_at=info.get("requested_at"),
            received_at=info.get("received_at"),
            latency_ms=info.get("latency_ms"),
            error_code=error_code or info.get("error_code"),
        )

    def _finish_run(
        self,
        run_id: str | None,
        status: str,
        page_count: int,
        saved_rows: int,
        http_counts: dict[str, int] | None = None,
        report: dict[str, Any] | None = None,
        archived_rows: int | None = None,
    ) -> None:
        """실행 상태와 처리 건수를 저장한다."""

        if not run_id or self.run_repository is None:
            return
        counts = {"pages": page_count, "saved_rows": saved_rows}
        if http_counts:
            counts.update(http_counts)
        if archived_rows is not None:
            counts["archived_rows"] = archived_rows
        self.run_repository.finish(run_id, status, counts=counts, report=report)

    def _save_raw_csv(
        self,
        items: list[dict[str, Any]],
        run_id: str | None,
        page_no: int,
    ) -> Any:
        """Bronze에 저장한 같은 원문을 CSV로 복사한다."""

        if self.raw_csv is None:
            return None
        try:
            return self.raw_csv.save_page(
                items,
                run_id=run_id or "manual",
                page_no=page_no,
            )
        except Exception as error:
            # MongoDB Bronze는 이미 저장되어 있으므로 원문은 유실되지 않는다.
            raise RuntimeError("RAW_CSV_WRITE_FAILED: 원문 CSV 저장에 실패했습니다.") from error

    def _save_raw_archive(
        self,
        run_id: str | None,
        page_no: int,
        item_count: int,
        parse_error: str | None = None,
        csv_path: Any = None,
    ) -> int:
        """API 응답 바이트를 그대로 저장하고 manifest 건수를 반환한다."""

        if self.raw_archive is None or run_id is None:
            return 0
        raw_body = getattr(self.api, "last_response_body", b"")
        if not isinstance(raw_body, bytes):
            raise RuntimeError("RAW_ARCHIVE_BODY_MISSING: API 응답 원문이 없습니다.")
        try:
            save_args = {
                "raw_body": raw_body,
                "run_id": run_id,
                "page_no": page_no,
                "item_count": item_count,
                "response_info": getattr(self.api, "last_response_info", {}) or {},
                "parse_error": parse_error,
            }
            if csv_path is not None:
                save_args["csv_path"] = csv_path
            try:
                manifest = self.raw_archive.save_page(**save_args)
            except TypeError:
                # 구형 사용자 저장소가 csv_path 인자를 모르는 경우에도
                # JSON 원문 보관은 계속한다.
                if csv_path is None:
                    raise
                save_args.pop("csv_path", None)
                manifest = self.raw_archive.save_page(**save_args)
        except Exception as error:
            raise RuntimeError("RAW_ARCHIVE_WRITE_FAILED: API 원문 저장에 실패했습니다.") from error
        return int(manifest.get("item_count", 0))

    @staticmethod
    def _cursor_key(cursor: Any) -> str | None:
        """cursor 비교용 키를 만든다. API 원문 cursor는 저장하지 않는다."""

        return None if cursor is None else str(cursor)

    @staticmethod
    def _check_page_accounting(
        item_count: int,
        saved_before: int,
        saved_after: int,
        deduplicated_before: int,
        deduplicated_after: int,
    ) -> None:
        """API items 수와 Bronze 저장·중복 건수의 페이지별 합계를 맞춘다."""

        accounted = (
            saved_after - saved_before
            + deduplicated_after - deduplicated_before
        )
        if accounted != item_count:
            raise RuntimeError(
                "BRONZE_PAGE_COUNT_MISMATCH: API 응답 건수와 Bronze 처리 건수가 다릅니다."
            )

    def _has_seen_response(self, response_hash: str | None) -> bool:
        """페이지 이력에 이미 저장된 응답 해시인지 확인한다."""

        if not response_hash or self.page_repository is None:
            return False
        checker = getattr(self.page_repository, "has_response_hash", None)
        if not callable(checker):
            return False
        try:
            return bool(checker(response_hash))
        except Exception:
            # 이력 조회 실패가 정상적인 Bronze 수집을 막지는 않도록 한다.
            return False

    @staticmethod
    def _count_http_status(info: dict[str, Any], counts: dict[str, int]) -> None:
        """페이지 HTTP 상태를 실행 집계에 반영한다."""

        status = info.get("http_status")
        if not isinstance(status, int):
            return
        if 200 <= status < 300:
            counts["http_2xx_count"] += 1
        elif 400 <= status < 500:
            counts["http_4xx_count"] += 1
        elif 500 <= status < 600:
            counts["http_5xx_count"] += 1

    def _api_context(self) -> tuple[str, dict[str, Any]]:
        """실행마다 새 API 키와 검증된 Meta를 준비한다."""

        api_key = self.api.get_daily_api_key()
        meta = validate_meta(self.api.get_meta(api_key))
        return api_key, meta

    @staticmethod
    def _wait_seconds(next_refresh_at: str) -> float:
        """next_refresh_at까지 남은 초를 계산한다."""

        value = next_refresh_at.replace("Z", "+00:00")
        refresh_at = datetime.fromisoformat(value)
        now = datetime.now(timezone.utc) if refresh_at.tzinfo else datetime.now()
        return max(0.0, (refresh_at - now).total_seconds())

    def _wait_for_refresh(self, next_refresh_at: str) -> None:
        """새 데이터 공개 시각까지 대기한다."""

        seconds = self._wait_seconds(next_refresh_at)
        if seconds:
            self.sleep(seconds)

    @classmethod
    def _is_refresh_due(cls, next_refresh_at: str | None) -> bool:
        """저장된 공개 시각이 되었는지 확인한다."""

        return not next_refresh_at or cls._wait_seconds(next_refresh_at) <= 0

    def collect(
        self,
        wait_for_refresh: bool = True,
        max_pages: int | None = None,
        scheduler_mode: bool = False,
    ) -> dict[str, Any]:
        """cursor가 끝날 때까지 API 페이지를 읽어 Bronze에 저장한다."""

        state = self.control.get_state()
        cursor = state["cursor"]
        run_id: str | None = None
        if self.run_repository is not None:
            # KST 날짜를 앞에 붙여 CSV 보관 폴더를 쉽게 찾는다.
            run_id = f"{datetime.now(KST):%Y%m%d}_{uuid4().hex[:12]}"
            self.run_repository.start(run_id, _load_rule_version())

        saved_rows = 0
        deduplicated_rows = 0
        api_item_count = 0
        archived_rows = 0
        page_count = 0
        # 한 실행 안에서만 cursor를 확인한다. 새 실행에서 저장된 cursor를
        # 다시 요청하는 것은 정상적인 이어받기이므로 이전 실행 이력과는
        # 비교하지 않는다.
        seen_cursors: set[str] = set()
        http_counts = {
            "http_2xx_count": 0,
            "http_4xx_count": 0,
            "http_5xx_count": 0,
        }
        try:
            if self.raw_csv is not None:
                self.raw_csv.compress_previous_runs()

            if scheduler_mode and not self._is_refresh_due(state["next_refresh_at"]):
                self._finish_run(run_id, "WAITING_FOR_REFRESH", 0, 0, http_counts)
                return {
                    "status": "WAITING_FOR_REFRESH",
                    "pages": 0,
                    "saved_rows": 0,
                    "cursor": cursor,
                    "next_refresh_at": state["next_refresh_at"],
                    "run_id": run_id,
                }

            api_key, meta = self._api_context()

            while max_pages is None or page_count < max_pages:
                page_no = page_count + 1
                current_key = self._cursor_key(cursor)
                if current_key is not None:
                    if current_key in seen_cursors:
                        page_count = page_no
                        self._save_page(
                            run_id, page_no, cursor, None, 0, None,
                            error_code="CURSOR_REPEAT",
                        )
                        raise RuntimeError(
                            "CURSOR_REPEAT: 같은 cursor를 한 실행에서 다시 요청했습니다."
                        )
                    seen_cursors.add(current_key)
                try:
                    response = self.api.get_records(api_key, cursor=cursor)
                except Exception as error:
                    page_count = page_no
                    self._save_raw_archive(
                        run_id, page_no, 0, parse_error=type(error).__name__
                    )
                    self._count_http_status(
                        dict(getattr(self.api, "last_response_info", {}) or {}),
                        http_counts,
                    )
                    self._save_page(
                        run_id, page_no, cursor, None, 0, None,
                        error_code=type(error).__name__,
                    )
                    raise

                page_count = page_no
                self._count_http_status(
                    dict(getattr(self.api, "last_response_info", {}) or {}),
                    http_counts,
                )
                items = response.get("items")
                if not isinstance(items, list):
                    self._save_raw_archive(
                        run_id, page_no, 0, parse_error="RECORDS_ITEMS_INVALID"
                    )
                    self._save_page(
                        run_id, page_no, cursor, None, 0, None,
                        error_code="RECORDS_ITEMS_INVALID",
                    )
                    raise ValueError("RECORDS_ITEMS_INVALID: items가 목록이 아닙니다.")

                if items:
                    saved_before = saved_rows
                    deduplicated_before = deduplicated_rows
                    api_item_count += len(items)
                    response_hash = (
                        dict(getattr(self.api, "last_response_info", {}) or {})
                    ).get("response_hash")
                    duplicate_response = self._has_seen_response(response_hash)
                    # JSON과 CSV를 같은 raw 폴더에 먼저 보관한다. 이후 MongoDB
                    # 저장이 실패해도 원문 파일과 manifest는 남는다.
                    csv_path = self._save_raw_csv(items, run_id, page_no)
                    archived_rows = self._save_raw_archive(
                        run_id, page_no, len(items), csv_path=csv_path
                    ) or archived_rows
                    if duplicate_response:
                        # 같은 응답을 다시 받은 경우 원문 파일·페이지 이력은
                        # 남기고, Bronze 업무 문서만 중복 저장하지 않는다.
                        deduplicated_rows += len(items)
                    else:
                        # payload를 펼치지 않고 API item 전체를 Bronze에 저장한다.
                        if run_id is None:
                            # 실행 이력을 주입하지 않는 단위 테스트·호출부와도 호환한다.
                            saved_rows += self.bronze.insert_many(items)
                        else:
                            saved_rows += self.bronze.insert_many(items, run_id=run_id)
                    self._check_page_accounting(
                        len(items),
                        saved_before,
                        saved_rows,
                        deduplicated_before,
                        deduplicated_rows,
                    )
                    # 재수신 중복은 의도적으로 Bronze 삽입을 건너뛸 수 있으므로
                    # 원문 보관 건수가 실제 저장 건수보다 적은 경우만 실패로 본다.
                    if self.raw_archive is not None and archived_rows < saved_rows:
                        raise RuntimeError(
                            "BRONZE_ARCHIVE_COUNT_MISMATCH: 원문과 MongoDB 건수가 다릅니다."
                        )
                    if "next_cursor" not in response:
                        self._save_page(
                            run_id, page_no, cursor, None, len(items),
                            response.get("next_refresh_at"),
                            error_code="NEXT_CURSOR_MISSING",
                        )
                        raise ValueError(
                            "NEXT_CURSOR_MISSING: API 응답에 next_cursor가 없습니다."
                        )
                    next_cursor = response.get("next_cursor")
                    if next_cursor is not None and (
                        not isinstance(next_cursor, str) or not next_cursor.strip()
                    ):
                        self._save_page(
                            run_id, page_no, cursor, None, len(items),
                            response.get("next_refresh_at"),
                            error_code="NEXT_CURSOR_INVALID",
                        )
                        raise ValueError(
                            "NEXT_CURSOR_INVALID: next_cursor 형식이 올바르지 않습니다."
                        )
                    next_key = self._cursor_key(next_cursor)
                    if next_key is not None and next_key in seen_cursors:
                        self._save_page(
                            run_id, page_no, cursor, next_cursor, len(items),
                            response.get("next_refresh_at"),
                            error_code="CURSOR_REPEAT",
                        )
                        raise RuntimeError(
                            "CURSOR_REPEAT: API가 이전 cursor를 다시 반환했습니다."
                        )
                    self._save_page(
                        run_id, page_no, cursor, next_cursor, len(items),
                        response.get("next_refresh_at"),
                    )
                    if next_cursor is None:
                        expected_bronze_count = saved_rows + deduplicated_rows
                        if api_item_count != expected_bronze_count:
                            raise RuntimeError(
                                "BRONZE_COUNT_MISMATCH: API items 누계와 Bronze 처리 누계가 다릅니다."
                            )
                        if (
                            self.raw_archive is not None
                            and archived_rows != api_item_count
                        ):
                            raise RuntimeError(
                                "BRONZE_ARCHIVE_COUNT_MISMATCH: API items 누계와 원문 누계가 다릅니다."
                            )
                        self.control.save_state(cursor, None)
                        self._finish_run(
                            run_id, "COMPLETED", page_count, saved_rows, http_counts,
                            report={
                                "deduplicated_rows": deduplicated_rows,
                                "api_item_count": api_item_count,
                            },
                            archived_rows=archived_rows,
                        )
                        return {
                            "status": "COMPLETED",
                            "pages": page_count,
                            "saved_rows": saved_rows,
                            "deduplicated_rows": deduplicated_rows,
                            "api_item_count": api_item_count,
                            "cursor": cursor,
                            "run_id": run_id,
                        }

                    self.control.save_state(next_cursor, None)
                    cursor = next_cursor
                    continue

                # 빈 items에서는 cursor를 바꾸지 않는다.
                archived_rows = self._save_raw_archive(
                    run_id, page_no, 0
                ) or archived_rows
                refresh_at = response.get("next_refresh_at") or meta["next_refresh_at"]
                self._save_page(run_id, page_no, cursor, cursor, 0, refresh_at)
                self.control.save_state(cursor, refresh_at)
                # 빈 응답 뒤에는 같은 cursor를 다음 공개 시각에 다시
                # 요청하는 것이 정상이다.
                seen_cursors.clear()
                if scheduler_mode or not wait_for_refresh:
                    self._finish_run(
                        run_id, "WAITING_FOR_REFRESH", page_count, saved_rows, http_counts,
                        report={"api_item_count": api_item_count},
                        archived_rows=archived_rows,
                    )
                    return {
                        "status": "WAITING_FOR_REFRESH",
                        "pages": page_count,
                        "saved_rows": saved_rows,
                        "deduplicated_rows": deduplicated_rows,
                        "api_item_count": api_item_count,
                        "cursor": cursor,
                        "next_refresh_at": refresh_at,
                        "run_id": run_id,
                    }

                self._wait_for_refresh(refresh_at)
                api_key, meta = self._api_context()

            self._finish_run(
                run_id, "PAGE_LIMIT_REACHED", page_count, saved_rows, http_counts,
                report={
                    "deduplicated_rows": deduplicated_rows,
                    "api_item_count": api_item_count,
                },
                archived_rows=archived_rows,
            )
            return {
                "status": "PAGE_LIMIT_REACHED",
                "pages": page_count,
                "saved_rows": saved_rows,
                "deduplicated_rows": deduplicated_rows,
                "api_item_count": api_item_count,
                "cursor": cursor,
                "run_id": run_id,
            }
        except Exception as error:
            self._finish_run(
                run_id,
                "FAILED",
                page_count,
                saved_rows,
                http_counts,
                report={
                    "error_type": type(error).__name__,
                    "http_status": (
                        getattr(self.api, "last_response_info", {}) or {}
                        ).get("http_status"),
                    "deduplicated_rows": deduplicated_rows,
                    "api_item_count": api_item_count,
                },
                archived_rows=archived_rows,
            )
            raise
