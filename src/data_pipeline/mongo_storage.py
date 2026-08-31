"""MongoDB 저장소를 한 파일에서 관리한다.

컬렉션별 클래스는 분리된 책임을 유지하되, 연결·Bronze·Silver·검토·실행
이력 저장 구현을 한 모듈에 모아 구조를 단순하게 유지한다. 기존 경로의
repository 모듈은 이 클래스들을 다시 내보내는 호환 파일이다.
"""

from __future__ import annotations

from datetime import datetime, timezone
from functools import lru_cache
import hashlib
import json
from typing import Any, Iterable, Mapping

from bson import ObjectId
from django.conf import settings
from pymongo import MongoClient, UpdateMany
from pymongo.collection import Collection
from pymongo.operations import ReplaceOne


@lru_cache(maxsize=1)
def get_mongo_client() -> MongoClient:
    """애플리케이션 실행 중 재사용할 MongoDB client를 반환한다."""

    return MongoClient(
        settings.MONGO_URI,
        serverSelectionTimeoutMS=settings.MONGO_SERVER_SELECTION_TIMEOUT_MS,
    )


def get_mongo_database():
    """Bronze·Silver·제어 컬렉션이 위치한 데이터베이스를 반환한다."""

    return get_mongo_client()[settings.MONGO_DATABASE]


def ping_mongo() -> None:
    """MongoDB 연결 상태를 확인한다."""

    get_mongo_client().admin.command("ping")


def close_mongo_connection() -> None:
    """애플리케이션 종료 시 MongoDB client를 닫는다."""

    get_mongo_client().close()
    get_mongo_client.cache_clear()


class BronzeRepository:
    """API item 원문을 MongoDB Bronze에 그대로 저장한다."""

    def __init__(self, collection_name: str = "hr_bronze_raw_records") -> None:
        self.collection: Collection = get_mongo_database()[collection_name]
        self._create_index()

    def _create_index(self) -> None:
        """가능한 경우 같은 record_id·원본 hash의 중복 저장을 막는다."""

        old_index_names = {
            "uq_bronze_record_release",
            "uq_bronze_record_payload_release",
            "uq_bronze_record_source_hash",
        }
        try:
            index_specs = {
                index["name"]: index
                for index in self.collection.list_indexes()
            }
        except Exception:
            index_specs = {}
        for old_index_name in old_index_names & set(index_specs):
            if (
                old_index_name == "uq_bronze_record_source_hash"
                and index_specs[old_index_name].get("sparse") is True
            ):
                continue
            self.collection.drop_index(old_index_name)

        if index_specs.get("uq_bronze_record_source_hash", {}).get("sparse") is True:
            return
        self.collection.create_index(
            [("record_id", 1), ("source_record_sha256", 1)],
            unique=True,
            sparse=True,
            name="uq_bronze_record_source_hash",
        )

    def _make_document(
        self,
        record: Mapping[str, Any],
        run_id: str | None = None,
    ) -> dict[str, Any]:
        """API item을 변경하지 않고 저장 문서로 만든다."""

        if not isinstance(record, Mapping):
            raise ValueError("Bronze 원본은 JSON 객체여야 합니다.")
        # payload와 API envelope는 그대로 복사하고, run_id만 별도 메타데이터로 추가한다.
        document = dict(record)
        if run_id is not None:
            document["run_id"] = run_id
        return document

    def insert_one(
        self,
        record: Mapping[str, Any],
        run_id: str | None = None,
    ) -> str:
        """원본 한 건을 저장하고 MongoDB 문서 ID를 반환한다."""

        result = self.collection.insert_one(
            self._make_document(record, run_id=run_id)
        )
        return str(result.inserted_id)

    def insert_many(
        self,
        records: Iterable[Mapping[str, Any]],
        run_id: str | None = None,
    ) -> int:
        """API 원문 여러 건을 저장하고 저장된 건수를 반환한다."""

        documents = [
            self._make_document(record, run_id=run_id)
            for record in records
        ]
        if not documents:
            return 0
        result = self.collection.insert_many(documents, ordered=False)
        return len(result.inserted_ids)

    def find_by_record_id(self, record_id: str | int) -> list[dict[str, Any]]:
        """같은 업무 ID의 원본 목록을 조회한다."""

        return list(self.collection.find({"record_id": record_id}))

    def find_by_bronze_id(self, bronze_id: str | ObjectId) -> dict[str, Any] | None:
        """Mongo 문서 ID로 Bronze 원문을 조회한다."""

        if isinstance(bronze_id, str) and ObjectId.is_valid(bronze_id):
            bronze_id = ObjectId(bronze_id)
        return self.collection.find_one({"_id": bronze_id})

    def count(self) -> int:
        """Bronze에 저장된 전체 문서 수를 반환한다."""

        return self.collection.count_documents({})


class SilverRepository:
    """정규화·검증을 통과한 Silver 표준 문서를 저장한다."""

    COLLECTION_NAME = "hr_silver_standard_records"
    WRITE_BATCH_SIZE = 1000

    def __init__(self, collection: Any | None = None) -> None:
        self.collection = collection or get_mongo_database()[self.COLLECTION_NAME]
        if collection is None:
            self.collection.create_index(
                [("area_id", 1)],
                unique=True,
                name="uq_silver_area_id",
            )
            self.collection.create_index(
                [("manager_id", 1)],
                name="ix_silver_manager_id",
            )

    @staticmethod
    def _check_record(record: Mapping[str, Any]) -> dict[str, Any]:
        """Silver 저장에 필요한 대표 ID만 확인한다."""

        if not isinstance(record, Mapping):
            raise ValueError("SILVER_RECORD_INVALID: Silver 문서는 객체여야 합니다.")
        if not record.get("area_id"):
            raise ValueError("SILVER_AREA_ID_MISSING: area_id가 없습니다.")
        return dict(record)

    def list_records(self) -> list[dict[str, Any]]:
        """현재 Silver 문서를 조회한다."""

        return list(self.collection.find({}))

    def save_records(
        self,
        records: Iterable[Mapping[str, Any]],
        allow_update: bool = False,
    ) -> int:
        """area_id 기준으로 Silver 문서를 저장한다.

        기존 값이 다르면 일반 실행에서는 자동 갱신하지 않는다. 승인된
        검토 재처리만 ``allow_update=True``로 명시적으로 갱신한다.
        """

        documents = [self._check_record(record) for record in records]
        if hasattr(self.collection, "find_one") and not allow_update:
            # 기존 Silver를 한 번에 읽어 충돌을 먼저 확인한다.
            # 테스트용 가짜 collection처럼 find를 지원하지 않으면 기존 방식으로 확인한다.
            area_ids = [document["area_id"] for document in documents]
            if hasattr(self.collection, "find"):
                previous_records = self.collection.find(
                    {"area_id": {"$in": area_ids}}
                )
                existing_by_area = {
                    str(record.get("area_id")): record
                    for record in previous_records
                    if record.get("area_id")
                }
            else:
                existing_by_area = {
                    str(document["area_id"]): self.collection.find_one(
                        {"area_id": document["area_id"]}
                    )
                    for document in documents
                }

            # 충돌을 모두 확인한 뒤 저장해 일부만 반영되는 일을 막는다.
            for document in documents:
                previous = existing_by_area.get(str(document["area_id"]))
                previous_values = {
                    key: value
                    for key, value in (previous or {}).items()
                    if key != "_id"
                }
                if previous and previous_values != document:
                    raise ValueError(
                        "SILVER_EXISTING_CONFLICT: 기존 area_id와 값이 다릅니다."
                    )

        saved = 0
        if hasattr(self.collection, "bulk_write"):
            # 실제 MongoDB에서는 1,000건 단위 일괄 upsert로 왕복 횟수를 줄인다.
            for start in range(0, len(documents), self.WRITE_BATCH_SIZE):
                batch = documents[start:start + self.WRITE_BATCH_SIZE]
                operations = [
                    ReplaceOne(
                        {"area_id": document["area_id"]},
                        document,
                        upsert=True,
                    )
                    for document in batch
                ]
                self.collection.bulk_write(operations, ordered=False)
                saved += len(batch)
            return saved

        # 단위 테스트용 가짜 collection과 호환되는 단건 저장 경로다.
        for document in documents:
            self.collection.replace_one(
                {"area_id": document["area_id"]},
                document,
                upsert=True,
            )
            saved += 1
        return saved

    def force_save_records(self, records: Iterable[Mapping[str, Any]]) -> int:
        """승인된 재처리 결과만 기존 Silver 값을 갱신한다."""

        return self.save_records(records, allow_update=True)


class ControlRepository:
    """API cursor와 다음 공개 시각을 저장한다."""

    DOCUMENT_ID = "records_cursor"
    COLLECTION_NAME = "hr_pipeline_control"
    LEGACY_COLLECTION_NAME = "hr_pipeline_pages"

    def __init__(
        self,
        collection_name: str = COLLECTION_NAME,
        collection: Any | None = None,
        legacy_collection: Any | None = None,
    ) -> None:
        if collection is None:
            database = get_mongo_database()
            collection = database[collection_name]
            if collection_name != self.LEGACY_COLLECTION_NAME:
                legacy_collection = database[self.LEGACY_COLLECTION_NAME]
        self.collection = collection
        self.legacy_collection = legacy_collection

    def get_state(self) -> dict[str, Any]:
        """cursor와 다음 공개 시각을 조회한다."""

        document = self.collection.find_one({"_id": self.DOCUMENT_ID})
        if not document and self.legacy_collection is not None:
            document = self.legacy_collection.find_one({"_id": self.DOCUMENT_ID})
        if not document:
            return {"cursor": None, "next_refresh_at": None}
        return {
            "cursor": document.get("cursor"),
            "next_refresh_at": document.get("next_refresh_at"),
        }

    def save_state(self, cursor: str | None, next_refresh_at: str | None) -> None:
        """cursor와 next_refresh_at을 저장한다."""

        self.collection.update_one(
            {"_id": self.DOCUMENT_ID},
            {"$set": {"cursor": cursor, "next_refresh_at": next_refresh_at}},
            upsert=True,
        )

    def get_cursor(self) -> str | None:
        """마지막 cursor를 반환한다."""

        return self.get_state()["cursor"]

    def save_cursor(self, cursor: str | None) -> None:
        """기존 호환용으로 cursor만 저장한다."""

        state = self.get_state()
        self.save_state(cursor, state["next_refresh_at"])


class PipelinePageRepository:
    """페이지 처리 메타데이터를 저장한다."""

    COLLECTION_NAME = "hr_pipeline_pages"

    def __init__(self, collection: Any | None = None) -> None:
        self.collection = (
            collection
            if collection is not None
            else get_mongo_database()[self.COLLECTION_NAME]
        )

    @staticmethod
    def _cursor_hash(cursor: str | None) -> str | None:
        """cursor 원문을 저장하지 않고 추적용 SHA-256만 남긴다."""

        if cursor is None:
            return None
        return hashlib.sha256(str(cursor).encode("utf-8")).hexdigest()

    def save_page(
        self,
        run_id: str,
        page_no: int,
        cursor: str | None,
        next_cursor: str | None,
        response_hash: str,
        item_count: int,
        next_refresh_at: str | None = None,
        http_status: int | None = None,
        requested_at: str | None = None,
        received_at: str | None = None,
        latency_ms: float | None = None,
        error_code: str | None = None,
    ) -> None:
        """한 페이지의 처리 메타데이터를 저장한다."""

        self.collection.insert_one({
            "run_id": run_id,
            "page_no": page_no,
            "cursor_hash": self._cursor_hash(cursor),
            "next_cursor_hash": self._cursor_hash(next_cursor),
            "response_hash": response_hash,
            "item_count": item_count,
            "next_refresh_at": next_refresh_at,
            "http_status": http_status,
            "requested_at": requested_at,
            "received_at": received_at,
            "latency_ms": latency_ms,
            "error_code": error_code,
        })

    def migrate_legacy_cursor_fields(self) -> dict[str, int]:
        """기존 페이지의 cursor 원문을 해시로 바꾸고 원문을 제거한다.

        해시 저장 정책을 적용하기 전에 생성된 페이지 이력에는 ``cursor``와
        ``next_cursor``가 남아 있을 수 있다. 원문을 다시 API 호출에 사용할
        필요는 없으므로, 추적에 필요한 SHA-256만 남긴다.
        """

        query = {
            "$or": [
                {"cursor": {"$exists": True}},
                {"next_cursor": {"$exists": True}},
            ]
        }
        documents = list(
            self.collection.find(
                query,
                {"_id": 1, "cursor": 1, "next_cursor": 1},
            )
        )
        migrated = 0
        skipped = 0
        for document in documents:
            document_id = document.get("_id")
            if document_id is None:
                skipped += 1
                continue

            set_fields: dict[str, Any] = {}
            unset_fields: dict[str, str] = {}
            if "cursor" in document:
                set_fields["cursor_hash"] = self._cursor_hash(
                    document.get("cursor")
                )
                unset_fields["cursor"] = ""
            if "next_cursor" in document:
                set_fields["next_cursor_hash"] = self._cursor_hash(
                    document.get("next_cursor")
                )
                unset_fields["next_cursor"] = ""

            if not set_fields:
                skipped += 1
                continue
            self.collection.update_one(
                {"_id": document_id},
                {"$set": set_fields, "$unset": unset_fields},
            )
            migrated += 1
        return {
            "scanned": len(documents),
            "migrated": migrated,
            "skipped": skipped,
        }

    def has_response_hash(self, response_hash: str | None) -> bool:
        """같은 API 응답을 이전 페이지 이력에서 찾는다."""

        if not response_hash:
            return False
        return self.collection.find_one({"response_hash": response_hash}) is not None


class PipelineRunRepository:
    """배치 실행 결과를 저장한다."""

    COLLECTION_NAME = "hr_pipeline_runs"

    def __init__(self, collection: Any | None = None) -> None:
        self.collection = (
            collection
            if collection is not None
            else get_mongo_database()[self.COLLECTION_NAME]
        )

    def start(self, run_id: str, rule_version: str) -> str:
        """배치 시작 정보를 저장한다."""

        self.collection.insert_one({
            "_id": run_id,
            "run_id": run_id,
            "status": "RUNNING",
            "rule_version": rule_version,
            "started_at": datetime.now(timezone.utc),
        })
        return run_id

    def finish(
        self,
        run_id: str,
        status: str,
        counts: Mapping[str, int] | None = None,
        report: Mapping[str, Any] | None = None,
    ) -> None:
        """배치 종료 상태와 처리 건수를 갱신한다."""

        update: dict[str, Any] = {
            "status": status,
            "finished_at": datetime.now(timezone.utc),
        }
        if counts is not None:
            update["counts"] = dict(counts)
        if report is not None:
            update["report"] = dict(report)
        self.collection.update_one({"_id": run_id}, {"$set": update})

    def record_archive_verification(
        self,
        run_id: str,
        result: Mapping[str, Any],
    ) -> None:
        """원문 파일 검사 결과를 수집 실행 이력에 추가한다."""

        self.collection.update_one(
            {"_id": run_id},
            {"$set": {
                "archive_status": result.get("status", "FAILED"),
                "archive_verification": dict(result),
                "archive_verified_at": datetime.now(timezone.utc),
            }},
        )


class ReviewQueueRepository:
    """판단이 필요한 데이터를 검토 큐에 저장한다."""

    COLLECTION_NAME = "hr_review_queue"
    PENDING_STATUS = "PENDING_REVIEW"
    APPROVED_STATUS = "APPROVED"
    REJECTED_STATUS = "REJECTED"

    def __init__(self, collection: Any | None = None) -> None:
        self.collection = (
            collection
            if collection is not None
            else get_mongo_database()[self.COLLECTION_NAME]
        )
        if collection is None:
            self.collection.create_index(
                [("bronze_id", 1)],
                name="ix_review_bronze_id",
            )
            # 같은 원문·단계의 대기 큐가 스케줄러 재실행으로 중복되지
            # 않도록 한다. 승인·반려 후에는 새 대기 건을 다시 만들 수 있다.
            self.collection.create_index(
                [("review_dedup_key", 1), ("review_status", 1)],
                unique=True,
                sparse=True,
                name="uq_review_pending_dedup",
            )

    @staticmethod
    def _review_stage(document: Mapping[str, Any]) -> str:
        """검토 문서가 Silver용인지 Gold용인지 정한다."""

        explicit = document.get("review_stage")
        if explicit in {"SILVER", "GOLD"}:
            return str(explicit)
        if document.get("blocks_silver", True):
            return "SILVER"
        if document.get("blocks_gold"):
            return "GOLD"
        return "INFO"

    @classmethod
    def _dedup_key(cls, document: Mapping[str, Any]) -> str | None:
        """재실행 시 같은 원문·단계를 식별할 키를 만든다."""

        source_id = document.get("bronze_id")
        if source_id in (None, ""):
            source_id = document.get("source_record_sha256")
        if source_id in (None, ""):
            return None
        return f"{cls._review_stage(document)}:{source_id}"

    def enqueue(
        self,
        records: Iterable[Mapping[str, Any]],
        run_id: str | None = None,
    ) -> int:
        """격리된 레코드를 중복 없이 검토 대기 상태로 저장한다."""

        documents = []
        now = datetime.now(timezone.utc)
        for record in records:
            document = dict(record)
            document.setdefault("status", "REVIEW_REQUIRED")
            document.setdefault("failure_stage", "UNKNOWN")
            document.setdefault("review_status", self.PENDING_STATUS)
            document.setdefault("review_stage", self._review_stage(document))
            if run_id is not None:
                document["run_id"] = run_id
            document.setdefault("first_seen_at", now)
            document["last_seen_at"] = now
            if run_id is not None:
                document["last_seen_run_id"] = run_id
            dedup_key = self._dedup_key(document)
            if dedup_key is not None:
                document["review_dedup_key"] = dedup_key
            documents.append(document)
        if not documents:
            return 0

        # Bronze ID가 있는 실제 파이프라인 건은 대기 상태인 동일 건을
        # upsert한다. 테스트용 단순 collection이나 식별자가 없는 문서는
        # 기존 일괄 삽입 경로를 사용한다.
        keyed = [document for document in documents if document.get("review_dedup_key")]
        plain = [document for document in documents if not document.get("review_dedup_key")]
        inserted = 0
        if keyed and hasattr(self.collection, "update_one"):
            for document in keyed:
                query = {
                    "review_dedup_key": document["review_dedup_key"],
                    "review_status": self.PENDING_STATUS,
                }
                # MongoDB는 같은 필드를 $setOnInsert와 $set에서 동시에
                # 갱신하면 WriteError를 발생시킨다. 재실행마다 바뀌는 값은
                # 최초 삽입 문서에서 제외하고 $set으로만 갱신한다.
                insert_document = dict(document)
                insert_document.pop("last_seen_at", None)
                insert_document.pop("last_seen_run_id", None)
                update = {
                    "$setOnInsert": insert_document,
                    "$set": {
                        "last_seen_at": now,
                        **({"last_seen_run_id": run_id} if run_id else {}),
                    },
                }
                try:
                    result = self.collection.update_one(query, update, upsert=True)
                except (KeyError, TypeError):
                    # 아주 단순한 테스트용 collection은 $setOnInsert를
                    # 모를 수 있으므로 일괄 삽입으로 대체한다.
                    plain.append(document)
                    continue
                if getattr(result, "upserted_id", None) is not None:
                    inserted += 1
        else:
            plain.extend(keyed)

        if plain:
            result = self.collection.insert_many(plain, ordered=False)
            inserted += len(result.inserted_ids)
        return inserted

    @staticmethod
    def _to_id(review_id: str | ObjectId) -> str | ObjectId:
        """문자열 ID가 Mongo ObjectId 형식이면 변환한다."""

        if isinstance(review_id, ObjectId):
            return review_id
        if ObjectId.is_valid(review_id):
            return ObjectId(review_id)
        return review_id

    def list_pending(self, limit: int = 100) -> list[dict[str, Any]]:
        """대기 중인 검토 데이터를 반환한다."""

        if limit < 1:
            raise ValueError("limit은 1 이상이어야 합니다.")
        query = {
            "$or": [
                {"review_status": self.PENDING_STATUS},
                {"review_status": {"$exists": False}},
            ]
        }
        cursor = self.collection.find(query)
        if hasattr(cursor, "sort"):
            cursor = cursor.sort("_id", 1)
        if hasattr(cursor, "limit"):
            cursor = cursor.limit(limit)
        return list(cursor)

    def find_by_id(self, review_id: str | ObjectId) -> dict[str, Any] | None:
        """검토 문서 한 건을 조회한다."""

        return self.collection.find_one({"_id": self._to_id(review_id)})

    def save_decision(
        self,
        review_id: str | ObjectId,
        decision: str,
        reviewer: str,
        note: str | None = None,
        corrected_values: Mapping[str, Any] | None = None,
    ) -> bool:
        """검토 문서에 승인·반려 결과를 기록한다."""

        if decision not in {self.APPROVED_STATUS, self.REJECTED_STATUS}:
            raise ValueError("decision은 APPROVED 또는 REJECTED여야 합니다.")
        if not reviewer or not reviewer.strip():
            raise ValueError("reviewer는 필수입니다.")

        update: dict[str, Any] = {
            "review_status": decision,
            "reviewed_by": reviewer.strip(),
            "reviewed_at": datetime.now(timezone.utc).isoformat(),
        }
        if note is not None:
            update["review_note"] = note
        if corrected_values is not None:
            update["corrected_values"] = dict(corrected_values)

        query = {
            "_id": self._to_id(review_id),
            "$or": [
                {"review_status": self.PENDING_STATUS},
                {"review_status": {"$exists": False}},
            ],
        }
        result = self.collection.update_one(query, {"$set": update})
        return bool(getattr(result, "matched_count", 1))

    def mark_reprocessed(
        self,
        review_id: str | ObjectId,
        status: str,
        rule_version: str,
        details: Mapping[str, Any] | None = None,
    ) -> bool:
        """재처리 결과를 검토 이력에 추가한다."""

        review = self.find_by_id(review_id)
        if not review or review.get("review_status") != self.APPROVED_STATUS:
            return False

        now = datetime.now(timezone.utc).isoformat()
        history = list(review.get("reprocess_history") or [])
        history.append({
            "status": status,
            "rule_version": rule_version,
            "processed_at": now,
            "details": dict(details or {}),
        })
        update = {
            "reprocess_status": status,
            "reprocessed_at": now,
            "reprocess_rule_version": rule_version,
            "reprocess_history": history,
        }
        result = self.collection.update_one(
            {
                "_id": self._to_id(review_id),
                "review_status": self.APPROVED_STATUS,
            },
            {"$set": update},
        )
        return bool(getattr(result, "matched_count", 1))


class LineageRepository:
    """Bronze·Silver·Gold의 연결 정보를 저장한다."""

    COLLECTION_NAME = "hr_lineage_links"

    def __init__(self, collection: Any | None = None) -> None:
        self.collection = (
            collection
            if collection is not None
            else get_mongo_database()[self.COLLECTION_NAME]
        )
        if collection is None:
            self.collection.create_index(
                [("bronze_id", 1)],
                name="ix_lineage_bronze_id",
            )
            self.collection.create_index(
                [("silver_key.area_id", 1)],
                name="ix_lineage_silver_area_id",
            )

    def save_links(self, links: Iterable[Mapping[str, Any]]) -> int:
        """계보 연결 문서를 저장한다."""

        documents = [dict(link) for link in links]
        if not documents:
            return 0
        result = self.collection.insert_many(documents, ordered=False)
        return len(result.inserted_ids)

    def attach_gold(
        self,
        load_batch_id: str,
        gold_keys_by_area: Mapping[str, Mapping[str, Any]],
        rule_version: str | None = None,
        processed_at: datetime | None = None,
        bronze_id_by_area: Mapping[str, str] | None = None,
    ) -> int:
        """Silver 계보에 Gold 배치 ID와 테이블별 키를 연결한다.

        Gold 적재가 끝난 뒤 이번 Silver 원본의 ``bronze_id``가 있으면 그
        링크를, 없으면 같은 ``silver_key.area_id``를 가진 링크를 갱신한다.
        MongoDB와 MySQL은 별도 저장소이므로, 연결 실패가 Gold 트랜잭션을
        되돌리지는 않으며 호출부가 결과 건수를 보고한다.
        """

        if not isinstance(load_batch_id, str) or not load_batch_id.strip():
            raise ValueError("GOLD_LINEAGE_BATCH_ID_MISSING: load_batch_id가 필요합니다.")
        if not gold_keys_by_area:
            return 0

        linked_at = processed_at or datetime.now(timezone.utc)
        updates: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for area_id, gold_key in gold_keys_by_area.items():
            if area_id in (None, "") or not gold_key:
                continue
            update: dict[str, Any] = {
                "load_batch_id": load_batch_id,
                "gold_key": dict(gold_key),
                "gold_result": "GOLD_SAVED",
                "gold_processed_at": linked_at,
            }
            if rule_version:
                update["gold_rule_version"] = rule_version
            bronze_id = (
                bronze_id_by_area.get(area_id)
                if bronze_id_by_area is not None
                else None
            )
            # Bronze ID가 있으면 이번 Silver 원본 링크만 갱신한다. 예전
            # 링크까지 같은 area_id로 덮지 않도록 하며, ID가 없는 구형
            # 링크는 Silver 키로 호환 갱신한다.
            query = (
                {"bronze_id": bronze_id}
                if bronze_id not in (None, "")
                else {"silver_key.area_id": area_id}
            )
            updates.append((query, {"$set": update}))

        if not updates:
            return 0
        # area_id별 키가 다르므로 bulk_write로 묶어 MongoDB 왕복 횟수를
        # 줄인다. 아주 단순한 테스트용 collection에는 기존 방식으로
        # 대체한다.
        if hasattr(self.collection, "bulk_write"):
            updated = 0
            operations = [UpdateMany(query, update) for query, update in updates]
            for start in range(0, len(operations), 1000):
                result = self.collection.bulk_write(
                    operations[start:start + 1000], ordered=False
                )
                updated += int(getattr(result, "matched_count", 0) or 0)
            return updated

        updated = 0
        for query, update in updates:
            result = self.collection.update_many(
                query,
                update,
            )
            updated += int(getattr(result, "matched_count", 0) or 0)
        return updated
