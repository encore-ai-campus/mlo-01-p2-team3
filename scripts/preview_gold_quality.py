"""Gold 적재 전 품질 게이트 결과만 출력한다. MySQL에는 저장하지 않는다."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import django

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from data_pipeline.gold_quality import build_gold_quality_preview  # noqa: E402


if __name__ == "__main__":
    print(json.dumps(build_gold_quality_preview(), ensure_ascii=False, indent=2))
