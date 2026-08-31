"""API 원문을 CSV로 보관하는 간단한 저장소."""

from __future__ import annotations

import csv
import json
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping
from zipfile import ZIP_DEFLATED, ZipFile


PROJECT_ROOT = Path(__file__).resolve().parents[2]
# JSON 원문과 같은 Bronze 영역에 CSV를 저장한다.
DEFAULT_ROOT = PROJECT_ROOT / "data" / "bronze"
KST = timezone(timedelta(hours=9))


def _safe_run_id(run_id: str) -> str:
    """run_id를 폴더명으로 안전하게 만든다."""

    value = re.sub(r"[^A-Za-z0-9_.=-]+", "_", str(run_id)).strip("._")
    return value or "manual"


def _ingest_date(run_id: str) -> str:
    """run_id 앞의 YYYYMMDD를 날짜 폴더명으로 바꾼다."""

    match = re.match(r"^(\d{8})(?:_|$)", str(run_id))
    if match:
        try:
            return datetime.strptime(match.group(1), "%Y%m%d").date().isoformat()
        except ValueError:
            pass
    return datetime.now(KST).date().isoformat()


def _csv_value(value: Any) -> Any:
    """중첩 값은 JSON 문자열로 바꾸고 나머지는 원래 표현을 유지한다."""

    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, ensure_ascii=False, default=str)
    if value is None:
        return ""
    return value


class RawCsvRepository:
    """API item을 run_id/page 단위 CSV로 저장하고 이전 파일을 압축한다."""

    def __init__(
        self,
        root: str | Path | None = None,
        source_name: str = "hr_api",
    ) -> None:
        # 기본 실행은 raw_archive와 같은 경로를 쓴다. 외부에서 임시 root를
        # 직접 주는 기존 호출부는 예전 레이아웃을 유지해 호환한다.
        self._archive_layout = root is None
        self.root = Path(root) if root is not None else DEFAULT_ROOT
        self.source_name = re.sub(
            r"[^A-Za-z0-9_.=-]+", "_", str(source_name)
        ).strip("._") or "hr_api"

    def _run_folder(self, run_id: str) -> Path:
        """JSON manifest와 같은 source/date/run 폴더를 계산한다."""

        safe_run_id = _safe_run_id(run_id)
        if not self._archive_layout:
            return self.root / f"run_id={safe_run_id}"
        return (
            self.root
            / f"source={self.source_name}"
            / f"ingest_date={_ingest_date(safe_run_id)}"
            / f"run_id={safe_run_id}"
        )

    def save_page(
        self,
        records: Iterable[Mapping[str, Any]],
        run_id: str,
        page_no: int,
    ) -> Path | None:
        """API 원문 페이지를 CSV로 저장한다."""

        items = list(records)
        if not items:
            return None
        if page_no < 1:
            raise ValueError("RAW_CSV_PAGE_INVALID: page_no는 1 이상이어야 합니다.")
        if not all(isinstance(item, Mapping) for item in items):
            raise ValueError("RAW_CSV_RECORD_INVALID: 원문은 JSON 객체여야 합니다.")

        # 원문 키를 처음 나타난 순서대로 유지한다.
        fieldnames: list[str] = []
        for item in items:
            for key in item:
                key = str(key)
                if key not in fieldnames:
                    fieldnames.append(key)
        # payload 안의 예기치 않은 중첩 필드까지 보존할 수 있도록 원문 JSON도 둔다.
        if "raw_json" not in fieldnames:
            fieldnames.append("raw_json")

        run_folder = self._run_folder(run_id)
        raw_folder = run_folder / "raw" if self._archive_layout else run_folder
        raw_folder.mkdir(parents=True, exist_ok=True)
        output_path = raw_folder / f"page_{page_no:04d}.csv"
        with output_path.open("w", encoding="utf-8", newline="") as file:
            writer = csv.DictWriter(file, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            for item in items:
                row = {str(key): _csv_value(value) for key, value in item.items()}
                row["raw_json"] = json.dumps(
                    dict(item), ensure_ascii=False, default=str
                )
                writer.writerow(row)
        return output_path

    def compress_previous_runs(self, today: date | None = None) -> list[Path]:
        """오늘보다 오래된 run 폴더를 ZIP으로 보관한다.

        원본 CSV 폴더는 삭제하지 않는다. ZIP 생성이 실패해도 원문을 잃지 않도록
        보존하며, 같은 ZIP이 있으면 다시 만들지 않는다.
        """

        if not self.root.exists():
            return []
        today = today or datetime.now(KST).date()
        archives: list[Path] = []
        if self._archive_layout:
            run_folders = sorted(
                self.root.glob("source=*/ingest_date=*/run_id=*")
            )
        else:
            run_folders = sorted(self.root.glob("run_id=*"))

        for run_folder in run_folders:
            if not run_folder.is_dir():
                continue
            run_id = run_folder.name.removeprefix("run_id=")
            match = re.match(r"^(\d{8})(?:_|$)", run_id)
            if not match:
                continue
            try:
                run_date = datetime.strptime(match.group(1), "%Y%m%d").date()
            except ValueError:
                continue
            if run_date >= today:
                continue

            # 새 레이아웃에서는 해당 ingest_date 폴더 옆에 압축한다.
            archive_path = run_folder.with_suffix(".zip")
            if not self._archive_layout:
                archive_path = self.root / f"{run_folder.name}.zip"
            if archive_path.exists():
                continue
            with ZipFile(archive_path, "w", compression=ZIP_DEFLATED) as archive:
                for file_path in run_folder.rglob("*"):
                    if file_path.is_file():
                        archive.write(file_path, file_path.relative_to(self.root))
            archives.append(archive_path)
        return archives
