"""API 응답 원문과 manifest를 run/page 단위로 보관한다."""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from time import sleep
from typing import Any, Mapping


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ROOT = PROJECT_ROOT / "data" / "bronze"
KST = timezone(timedelta(hours=9))
DEFAULT_CODE_VERSION = "bronze-v1.0"


def _replace_with_retry(source: Path, target: Path, attempts: int = 5) -> None:
    """Windows의 짧은 파일 잠금이 풀릴 때까지 manifest 교체를 재시도한다."""

    for attempt in range(attempts):
        try:
            source.replace(target)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            sleep(0.05 * (attempt + 1))


def _safe_name(value: str) -> str:
    """폴더명에 사용할 수 있는 문자만 남긴다."""

    safe = re.sub(r"[^A-Za-z0-9_.=-]+", "_", str(value)).strip("._")
    return safe or "unknown"


def _ingest_date(run_id: str) -> str:
    """run_id 앞의 YYYYMMDD를 날짜로 바꾸고, 없으면 오늘 날짜를 쓴다."""

    match = re.match(r"^(\d{8})(?:_|$)", run_id)
    if match:
        try:
            return datetime.strptime(match.group(1), "%Y%m%d").date().isoformat()
        except ValueError:
            pass
    return datetime.now(KST).date().isoformat()


class RawArchiveRepository:
    """HTTP 응답 바이트를 변경하지 않고 저장하고 manifest를 갱신한다."""

    def __init__(
        self,
        root: str | Path | None = None,
        source_name: str = "hr_api",
        code_version: str | None = None,
        rule_version: str | None = None,
    ) -> None:
        self.root = Path(root) if root is not None else DEFAULT_ROOT
        self.source_name = _safe_name(source_name)
        self.code_version = code_version or os.getenv(
            "APP_CODE_VERSION", DEFAULT_CODE_VERSION
        )
        self.rule_version = rule_version or "unknown"

    def _run_folder(self, run_id: str) -> Path:
        ingest_date = _ingest_date(run_id)
        return (
            self.root
            / f"source={self.source_name}"
            / f"ingest_date={ingest_date}"
            / f"run_id={_safe_name(run_id)}"
        )

    def save_page(
        self,
        raw_body: bytes,
        run_id: str,
        page_no: int,
        item_count: int,
        response_info: Mapping[str, Any] | None = None,
        parse_error: str | None = None,
    ) -> dict[str, Any]:
        """원문 한 페이지를 저장하고 갱신된 manifest를 반환한다."""

        if page_no < 1:
            raise ValueError("RAW_ARCHIVE_PAGE_INVALID: page_no는 1 이상이어야 합니다.")
        if not isinstance(raw_body, bytes):
            raise TypeError("RAW_ARCHIVE_BODY_INVALID: 응답 원문은 bytes여야 합니다.")

        run_folder = self._run_folder(run_id)
        raw_folder = run_folder / "raw"
        raw_folder.mkdir(parents=True, exist_ok=True)
        raw_path = raw_folder / f"page_{page_no:04d}.json"
        if raw_path.exists():
            raise FileExistsError("RAW_ARCHIVE_PAGE_EXISTS: 같은 원문 페이지가 이미 있습니다.")
        raw_path.write_bytes(raw_body)

        info = dict(response_info or {})
        page = {
            "page_no": page_no,
            "path": raw_path.relative_to(run_folder).as_posix(),
            "item_count": item_count,
            "file_size_bytes": len(raw_body),
            "sha256": hashlib.sha256(raw_body).hexdigest(),
            "content_type": info.get("content_type"),
            "source_url": info.get("source_url"),
            "http_status": info.get("http_status"),
            "requested_at": info.get("requested_at"),
            "received_at": info.get("received_at"),
            "retry_count": info.get("retry_count", 0),
            "parse_status": "FAILED" if parse_error else "SUCCESS",
            "parse_error": parse_error,
        }

        manifest_path = run_folder / "manifest.json"
        if manifest_path.exists():
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        else:
            manifest = {
                "run_id": run_id,
                "source_name": self.source_name,
                "ingest_date": _ingest_date(run_id),
                "code_version": self.code_version,
                "rule_version": self.rule_version,
                "pages": [],
            }

        manifest["pages"].append(page)
        manifest["page_count"] = len(manifest["pages"])
        manifest["item_count"] = sum(
            int(saved_page.get("item_count", 0)) for saved_page in manifest["pages"]
        )
        manifest["updated_at"] = datetime.now(timezone.utc).isoformat()

        temp_path = manifest_path.with_suffix(".tmp")
        temp_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        _replace_with_retry(temp_path, manifest_path)
        return manifest


def verify_archive(
    root: str | Path | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    """원문 JSON과 manifest의 누락·해시·임시 파일을 검사한다."""

    archive_root = Path(root) if root is not None else DEFAULT_ROOT
    result: dict[str, Any] = {
        "manifest_count": 0,
        "manifest_page_count": 0,
        "manifest_item_count": 0,
        "raw_file_count": 0,
        "missing_manifests": [],
        "missing_files": [],
        "hash_mismatches": [],
        "orphan_files": [],
        "temp_files": [],
        "invalid_manifests": [],
    }
    if not archive_root.exists():
        result["status"] = "FAILED"
        result["invalid_manifests"].append(str(archive_root))
        return result

    run_folders = [
        path
        for path in archive_root.rglob("run_id=*")
        if path.is_dir()
        and (run_id is None or path.name == f"run_id={_safe_name(run_id)}")
    ]
    if run_id is not None and not run_folders:
        result["status"] = "FAILED"
        result["missing_manifests"].append(f"run_id={_safe_name(run_id)}")
        return result

    for run_folder in run_folders:
        manifest_path = run_folder / "manifest.json"
        raw_folder = run_folder / "raw"
        actual_files = set(raw_folder.glob("*.json")) if raw_folder.exists() else set()
        result["raw_file_count"] += len(actual_files)
        if not manifest_path.exists():
            result["missing_manifests"].append(str(manifest_path))
            result["orphan_files"].extend(str(path) for path in sorted(actual_files))
            continue

        result["manifest_count"] += 1
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            pages = manifest.get("pages", [])
            if not isinstance(pages, list):
                raise ValueError("pages가 목록이 아닙니다.")
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            result["invalid_manifests"].append(str(manifest_path))
            continue

        expected_files: set[Path] = set()
        result["manifest_page_count"] += len(pages)
        result["manifest_item_count"] += int(manifest.get("item_count", 0))
        for page in pages:
            if not isinstance(page, Mapping) or not page.get("path"):
                result["invalid_manifests"].append(str(manifest_path))
                continue
            raw_path = run_folder / str(page["path"])
            expected_files.add(raw_path)
            if not raw_path.exists():
                result["missing_files"].append(str(raw_path))
                continue
            expected_hash = page.get("sha256")
            actual_hash = hashlib.sha256(raw_path.read_bytes()).hexdigest()
            if expected_hash and actual_hash != expected_hash:
                result["hash_mismatches"].append(str(raw_path))

        result["orphan_files"].extend(
            str(path) for path in sorted(actual_files - expected_files)
        )

    result["temp_files"] = [
        str(path)
        for folder in run_folders
        for path in sorted(folder.rglob("*.tmp"))
    ]
    hard_failures = (
        result["missing_manifests"]
        or result["missing_files"]
        or result["hash_mismatches"]
        or result["invalid_manifests"]
    )
    warnings = result["orphan_files"] or result["temp_files"]
    result["status"] = (
        "FAILED" if hard_failures else "WARNING" if warnings else "PASS"
    )
    return result
