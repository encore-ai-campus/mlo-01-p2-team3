"""검토 큐를 파일로 내보내고 승인·반려 결과를 기록한다.

사용 예:
    py scripts\\review_queue.py export
    py scripts\\review_queue.py approve --id <MongoID> --reviewer 홍길동
    py scripts\\review_queue.py reject --id <MongoID> --reviewer 홍길동 --note 사유
    py scripts\\review_queue.py reprocess --id <MongoID> --write

이 도구는 YAML이나 원본 데이터를 자동으로 변경하지 않는다.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any


# 어느 위치에서 실행해도 src 패키지를 찾도록 프로젝트 경로를 추가한다.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
sys.path.insert(0, str(SRC_DIR))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django

django.setup()

from data_pipeline.review import ReviewService


def _json_default(value: Any) -> str:
    """Mongo ObjectId와 날짜를 JSON 문자열로 바꾼다."""

    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="hr_review_queue 검토 도구")
    subparsers = parser.add_subparsers(dest="command", required=True)

    export = subparsers.add_parser("export", help="대기 데이터를 review.json으로 출력")
    export.add_argument(
        "--output",
        default=str(PROJECT_ROOT / "reports" / "review.json"),
        help="출력 파일 경로(기본값: reports/review.json)",
    )
    export.add_argument("--limit", type=int, default=1000, help="최대 출력 건수")

    for name, help_text in (
        ("approve", "검토 대상을 승인"),
        ("reject", "검토 대상을 반려"),
    ):
        command = subparsers.add_parser(name, help=help_text)
        command.add_argument("--id", required=True, help="검토 문서의 MongoDB _id")
        command.add_argument("--reviewer", required=True, help="검토 담당자")
        command.add_argument("--note", help="검토 메모")
        command.add_argument(
            "--corrected-json",
            help="수정값 JSON. 원본 payload 또는 Silver 필드명을 사용",
        )

    reprocess = subparsers.add_parser("reprocess", help="승인된 검토 건을 재처리")
    reprocess.add_argument("--id", required=True, help="검토 문서의 MongoDB _id")
    reprocess.add_argument(
        "--write",
        action="store_true",
        help="검증 통과 결과를 Silver에 저장",
    )
    return parser


def main() -> int:
    args = _parser().parse_args()
    service = ReviewService()

    if args.command == "export":
        if args.limit < 1:
            raise SystemExit("--limit은 1 이상이어야 합니다.")
        records = service.list_pending(limit=args.limit)
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(records, ensure_ascii=False, indent=2, default=_json_default),
            encoding="utf-8",
        )
        print(f"검토 대상 {len(records)}건을 {output}에 저장했습니다.")
        return 0

    if args.command == "reprocess":
        result = service.reprocess(args.id, write=args.write)
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result["status"] in {"VALIDATED", "SILVER_SAVED"} else 1

    corrected_values = None
    if args.corrected_json:
        try:
            corrected_values = json.loads(args.corrected_json)
        except json.JSONDecodeError as error:
            raise SystemExit(f"--corrected-json 형식 오류: {error}") from error
        if not isinstance(corrected_values, dict):
            raise SystemExit("--corrected-json은 JSON 객체여야 합니다.")

    decision = "APPROVED" if args.command == "approve" else "REJECTED"
    saved = service.decide(
        review_id=args.id,
        decision=decision,
        reviewer=args.reviewer,
        note=args.note,
        corrected_values=corrected_values,
    )
    if not saved:
        print("대상 문서가 없거나 이미 처리된 문서입니다.")
        return 1
    print(f"검토 결과 저장 완료: {decision}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
