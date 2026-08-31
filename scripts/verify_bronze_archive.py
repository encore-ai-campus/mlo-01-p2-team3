"""Bronze 원문 JSON·CSV와 manifest 상태를 한 번 검사한다."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django

django.setup()

from data_pipeline.mongo_storage import PipelineRunRepository
from data_pipeline.raw_archive import verify_archive


def _arguments():
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id")
    parser.add_argument("--compact", action="store_true")
    return parser.parse_args()


def _save_report(result: dict, run_id: str | None) -> Path:
    report_folder = PROJECT_ROOT / "reports" / "bronze_archive"
    report_folder.mkdir(parents=True, exist_ok=True)
    safe_run_id = re.sub(r"[^A-Za-z0-9_.=-]+", "_", run_id or "all")
    output = report_folder / f"run_id={safe_run_id}.json"
    content = json.dumps(result, ensure_ascii=False, indent=2)
    output.write_text(content, encoding="utf-8")
    (report_folder / "latest.json").write_text(content, encoding="utf-8")
    return output


def main() -> int:
    args = _arguments()
    result = verify_archive(run_id=args.run_id)
    report_path = _save_report(result, args.run_id)
    result["report_path"] = str(report_path)
    if args.run_id:
        PipelineRunRepository().record_archive_verification(args.run_id, result)
    print(json.dumps(
        result,
        ensure_ascii=False,
        indent=None if args.compact else 2,
    ))
    return 1 if result["status"] == "FAILED" else 0


if __name__ == "__main__":
    raise SystemExit(main())
