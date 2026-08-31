"""Silver 품질 보고서를 화면과 JSON 파일로 출력한다."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import django

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
sys.path.insert(0, str(SRC_DIR))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from data_pipeline.quality_report import build_silver_quality_report  # noqa: E402


def main() -> None:
    report = build_silver_quality_report()
    output = PROJECT_ROOT / "reports" / "silver_quality_latest.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"보고서 파일: {output}")


if __name__ == "__main__":
    main()
