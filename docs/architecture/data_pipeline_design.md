# API 기반 데이터 파이프라인 설계

## 1. 한눈에 보는 구조

```text
스케줄러
  ↓
레거시 API → 계약 검사 → MongoDB Bronze → 메모리 매핑·정규화 → 도메인·중복 검증 → MongoDB Silver → MySQL Gold → Django
                  └─ 수집·계약 실패 → 페이지/실행 이력
                                                 └─ 정규화·검증 실패 → hr_review_queue
```

API 응답 메타데이터와 API 본문 데이터는 Bronze에 그대로 보관한다. 응답 메타데이터의 `record_id`,
`scheduled_release_at`은 선택이며 있을 때만 원문으로 보존한다. API 본문 데이터의 업무 필드 15개는 필수다.
문서 호환상 `release_at` = `scheduled_release_at`으로 표기할 수 있지만, `release_at`은 API 응답이나
API 본문 데이터의 별도 필드로 생성하지 않는다.

## 2. 계층별 역할

| 계층 | 역할 | 저장 내용 |
|---|---|---|
| API | 주기 호출·페이지·재시도 | 레거시 전체 응답 |
| Bronze | 원본 보존 | API 원본 문서 전체. 본문 데이터 15개, 선택 응답 메타데이터, 수집 `run_id` |
| 정규화 결과 | 메모리에서 API 본문 데이터를 표준 필드로 변환 | 검증에 사용할 업무 15개 필드 |
| Silver | 정규화 결과 중 Silver 저장 검증을 통과한 결과 | `PASS` 또는 `PASS_WITH_WARNING` 상태의 업무 15개 필드 |
| Gold | 현재 상태 제공 | 조직·담당자·배정·파생 피처 테이블 |
| Django | 조회 화면 | Gold와 품질 현황 |

## 3. API 실행

- 실행마다 `run_id`를 발급한다. 이 값은 배치 추적용 메타데이터이며 API 본문 데이터의 업무 필드는 아니다. Bronze 원본 문서 최상위와 실행 이력에 기록한다.
- 페이지·커서가 끝날 때까지 호출하고, 반복·누락을 확인한다.
- `records`의 `limit`은 항상 `1000`으로 고정한다. 첫 요청은 cursor 없이 호출한다.
- API 본문 데이터 목록이 비어도 같은 cursor를 유지하고 `next_refresh_at` 이후 다시 호출한다.
- API 키는 실행마다 `/public/v1/key`에서 새로 조회한다.
- 일시 오류(`429`, `408`, `5xx`, 네트워크)는 최대 3회 재시도한다.
- 인증 오류와 스키마 오류는 재시도하지 않고 실패 처리한다.
- 페이지 저장이 성공할 때마다 다음 cursor를 `hr_pipeline_control`에 저장한다.
- 오류가 나면 실패한 페이지의 cursor를 유지하고, 다음 실행에서 같은 cursor부터 다시 요청한다.

## 4. Bronze

컬렉션: `hr_bronze_raw_records`

- API 응답 메타데이터와 API 본문 데이터를 필드명·값 그대로 저장한다.
- `record_id`, `scheduled_release_at`은 있으면 응답 메타데이터로 보관하고 없으면 필드를 만들지 않는다. 업무 필드 15개는 API 본문 데이터에 보관한다.
- `record_id`가 없어도 원문을 저장하며 MongoDB 내부 식별자(`_id`, 문서상 `bronze_id`)로 추적한다.
- API가 함께 보낸 응답 메타데이터도 삭제하지 않는다. 이는 추가 업무 컬럼이 아니다.
- 같은 업무 ID가 새 배치로 들어와도 기존 문서를 수정하지 않고 새 문서로 보존한다.
- 같은 요청·응답 해시의 재수신은 중복 반영하지 않고 제어 이력만 남긴다.
- 파이프라인이 생성하는 배치 ID(`run_id`)는 Bronze 원본 문서 최상위와 `hr_pipeline_runs`에 저장한다. 오류 코드·규칙 버전은 제어 컬렉션에 저장한다.

### 4.1 Bronze 원본 무결성

Bronze 저장 직후 수집 결과와 저장 결과가 같은지 확인한다.

- API 페이지의 원본 항목 수와 Bronze 저장 건수를 비교한다.
- 페이지 순서와 cursor가 누락되거나 반복되지 않았는지 확인한다.
- 원본 필드명과 값이 저장 중 바뀌지 않았는지 확인한다.
- 각 원본 문서가 `run_id`와 원본 해시(`source_record_sha256`가 있으면 해당 값)로 추적되는지 확인한다.
- 동일 응답 재수신, 저장 실패·누락 건수를 실행 이력에 남긴다.

차이가 설명되지 않으면 해당 실행 또는 페이지를 실패로 표시한다. 원본은 가능한 한 그대로 보존하며,
무결성 확인이 끝나기 전에는 Silver 정규화를 시작하지 않는다.

수집 소스는 API 호출, 페이지·실행 정보 기록, Bronze 저장까지만 담당한다. Silver 처리 소스는 Bronze를 읽어
정규화·도메인·중복·조직 관계를 검증한 뒤 Silver 또는 `hr_review_queue`에 저장한다. 따라서 Silver 검증 규칙을
추가해도 API 클라이언트와 Bronze 원본 저장 로직은 변경하지 않는다.

스케줄러는 `scripts/run_pipeline_5m.ps1`로 5분마다 실행한다. 한 사이클에서 API를 수집하고
미처리 Bronze를 Silver로 처리한 뒤, Silver 성공 시 Gold 부분 적재를 실행한다. 최초 전체 Silver
저장은 수동으로 한 번 실행하며, 남은 미처리 건수는 다음 5분 사이클에서 이어서 처리한다.

## 5. 정규화·검증

- API 본문 데이터가 객체인지와 업무 필드 15개가 정확히 있는지 매핑 단계에서 확인한 뒤 메모리에서 표준 컬럼명과 형식으로 변환한다.
- 업무 필드 누락·추가·타입 오류는 `MAPPING_PAYLOAD_SCHEMA_MISMATCH` 또는 `MAPPING_PAYLOAD_TYPE_INVALID`로 검토 큐에 보낸다. Bronze 원문은 유지한다.
- 같은 `area_id`·`manager_id`에서 하나로 확인되는 값만 누락 필드에 보완한다. 후보가 없거나 여러 값이면 임의로 채우지 않는다.
- 정규화 결과에 도메인·중복·조직 관계 검증을 적용한다.
- 오류가 있으면 결과를 별도 컬렉션에 저장하지 않고 `hr_review_queue`에 보낸다.

## 6. Silver

컬렉션: `hr_silver_standard_records`

- 정규화 결과 중 Silver 저장 검증을 통과한 업무 필드 15개를 저장한다. 선택 속성 경고가 있어도 `PASS_WITH_WARNING`이면 저장하고 경고 이력을 남긴다.
- API 레코드는 전체 스냅샷으로 처리한다. 서로 다른 배치의 필드를 섞지 않는다.
- API 버전 정보가 없으므로 같은 업무 ID의 충돌은 자동으로 현재값을 교체하지 않는다.
- 담당자 승인 후 재처리한 결과만 명시적으로 현재값을 갱신한다.
- 현재 조직–담당자 레코드는 `area_id`당 한 건을 유지한다.

조직 계층은 `parent_area_id`를 바로 위 부모 부서, `top_area_id`를 최상위 부서로 사용한다.
`area_id == top_area_id`이면 최상위 부서로, 다르면 하위 부서로 계산한다. 최상위 부서는 부모가 없거나
자기 자신을 가리켜도 정상으로 처리하고, 하위 부서는 존재하는 다른 부서를 부모로 지정해야 한다.
하위 부서의 부모가 없거나 자기 참조·순환 연결이면 검토 큐로 보낸다. 계층은 2단계로 고정하지 않으며,
부모가 추가·변경되면 관계를 다시 검증한다.

같은 `area_id`의 부서명·상위 부서 정보 변경이나 `area_id` 변경은 변경 후보로 분류한다. 현재 구현은
자동 갱신하지 않고 `SILVER_EXISTING_CONFLICT`로 검토 큐에 보낸다. 일관된 부서 이동 자동 갱신은
별도 기준 확정 후 구현할 대상이다.

## 7. Gold

테이블: `hr_area`, `hr_manager`, `hr_area_manager_assignment`, `area_manager_features`

- 조직은 `area_id`, 담당자는 `manager_id`, 배정은 `area_id`를 기준으로 현재값을 관리한다.
- 한 담당자가 여러 조직을 맡는 것은 허용한다.
- 검증을 통과한 Silver를 메모리에서 품질 게이트한 뒤 Gold에 반영한다.
- PK/FK·건수 검증에 실패하면 전체 변경을 취소하고 기존 Gold를 유지한다.
- 증분 응답에 없다는 이유만으로 Gold 행을 삭제하지 않는다.
- 별도 `stg_hr_standard_records` 테이블은 현재 미구현이며, 실행 규칙에서
  `staging_enabled: false`로 관리한다. 적재 이력은 `hr_gold_load_batch`에 남긴다.

## 8. 검토와 재처리

컬렉션: `hr_review_queue`, `hr_lineage_links`

검토 대상:

- 필수값·ID·날짜·허용 값 오류
- 같은 업무 ID의 API 본문 데이터 충돌
- 검증에 실패한 레코드
- 어느 값이 최신인지 판단할 수 없는 배치
- 조직 계층·담당자 충돌

정규화 실패 또는 도메인·중복 검증 실패 데이터는 `hr_review_queue`에 보관한다. 승인된 내용은 Silver·Gold에 직접 입력하지 않는다. 규칙 또는 원천 데이터를 수정한 뒤 Bronze에서 다시 정규화하고 검증한다.

검토 큐는 하나로 운영하며 `failure_stage`로 실패 단계를 구분한다.

| `failure_stage` | 의미 | 처리 |
|---|---|---|
| `NORMALIZATION` | 매핑·ID·날짜 등 형식 표준화 실패 | Silver에 저장하지 않고 검토 큐 보관 |
| `CANDIDATE_VALIDATION` | 메모리상의 정규화 결과에 대한 도메인·중복·조직 관계 검증 실패 | Silver에 저장하지 않고 검토 큐 보관 |
| `QUALITY_WARNING` | Silver 저장은 가능하지만 품질 경고를 남긴 건 | Silver 저장 및 경고 이력 |
| `UNKNOWN` | 기존 문서 등 단계 정보가 없음 | 원본 연결 후 확인 |

검토 식별 키는 `review_stage:bronze_id`를 사용하고 `bronze_id`가 없으면
`source_record_sha256`를 사용한다. 같은 키의 `PENDING_REVIEW`는 upsert하여 중복을 막고,
상태는 `PENDING_REVIEW → APPROVED` 또는 `PENDING_REVIEW → REJECTED`로 변경한다.
승인 건만 Bronze에서 재처리하며, 결과와 규칙 버전은 `reprocess_history`에 추가한다.

## 9. 제어 정보

| 컬렉션 | 역할 |
|---|---|
| `hr_pipeline_runs` | 배치 실행·규칙 버전·처리 건수(실패 시 상세 보고) |
| `hr_pipeline_control` | 현재 cursor·`next_refresh_at` |
| `hr_pipeline_pages` | 페이지·cursor 해시·HTTP 상태·요청/응답 시각·지연 시간·응답 해시·오류 코드 |
| `hr_review_queue` | 오류·판단 불가·실패 단계·승인 상태 |
| `hr_lineage_links` | Bronze–Silver–Gold 연결, `load_batch_id`·Gold 키와 검토·반영 근거 |

응답 본문과 API 키는 페이지 이력이나 파일 로그에 저장하지 않는다.

`hr_gold_load_batch`에는 Silver 입력 건수, Gold 처리 건수, 제외 건수,
시작·종료 시각과 상세 결과 JSON을 저장한다. 대시보드는 시작 시각이 가장
최근인 성공 배치를 표시한다.

이 필드들은 다음 Gold 실행부터 기록된다. 기존 페이지 이력의 원본 cursor나
기존 계보 문서에 Gold 정보가 없는 경우는 자동 소급하지 않고 점검·백필 대상으로
남긴다.

## 10. 장애 원칙

- 실패한 페이지의 cursor는 건너뛰지 않으며, 마지막 성공 페이지 상태를 유지한다.
- 동일 ID 충돌 데이터가 기존 정상값을 자동으로 덮어쓰지 않는다.
- Gold 반영은 하나의 트랜잭션으로 처리한다.
- 재실행해도 같은 입력이 중복 Gold로 들어가지 않는다.

## 11. 구현 완료 기준

- API → Bronze → 정규화·검증 → Silver → Gold → Django 흐름이 재현된다.
- 논리 원본 목록 17개 중 필수 API 본문 데이터 15개와 선택 응답 메타데이터의 대응이 검증된다.
- 표준화 실패와 도메인 검증 실패가 구분된다.
- 배치·규칙·검토·재처리 이력을 조회할 수 있다.
- 실행 결과 JSON 요약이 생성되고, 배치 상태·건수는 실행 이력에서 확인된다.

## 12. 현재 Django 출력 연결

Django는 `presentation → service → repository` 3계층으로 구성한다.

- `presentation`: URL·View·Template과 CSV 응답을 담당하며 요청·표시 형식만 다룬다.
- `service`: Gold 조회 결과를 조합하고 검색·집계·표시용 정리를 담당한다. Bronze·Silver를 직접 수정하지 않는다.
- `repository`: MySQL Gold 테이블을 읽는 SQL만 담당하며 수집·정규화 규칙을 구현하지 않는다.

repository는 MySQL Gold 테이블만 읽고 service가 화면용 결과를 만든다.

- 조직 목록은 area_id와 area_name을 분리해 표시하고, 부모·최상위 조직과 담당자 정보를 함께 제공한다.
- 담당자 목록은 manager_id와 manager_name을 분리해 표시하고, 부서·직급·재직 상태·담당 조직 수를 제공한다.
- 조직 CSV는 /hrdata/areas/export.csv, 담당자 CSV는 /hrdata/managers/export.csv에서 제공한다.
- 각 주소에 all=1을 주면 현재 검색 조건과 페이지 제한을 무시하고 전체 Gold 결과를 내려받는다.
- 화면·CSV의 이름 반복 단어와 공백 정리는 표시 단계에서만 수행한다. 데이터베이스 값, 키, 건수는 바꾸지 않는다.
- CSV 생성은 브라우저에 보이는 현재 페이지의 DOM을 복사하지 않고 서버가 Gold 조회 결과로 직접 만든다.

모든 경로는 스크립트 자신의 위치에서 프로젝트 루트를 계산하므로 압축을 푼 위치가 달라도 동작한다.
현재 구현의 실행·검증 명령은 [실행 매뉴얼](../operations_runbook.md)에 정리한다.
