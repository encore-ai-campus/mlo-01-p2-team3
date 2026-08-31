# 조직·담당자 데이터 명세

## 1. 문서 목적

이 문서는 프로젝트 전체에서 공통으로 사용하는 데이터 계약과 문서 위치를 한곳에 정리한다. 세부 규칙은 각 기준 문서를 따른다.

## 2. 데이터 범위

| 항목 | 기준 |
|---|---|
| 원천 | 레거시 API |
| 원본 필드 | 논리 목록 17개. API 본문 데이터 15개는 필수, API 응답 메타데이터 2개는 선택 |
| Silver 업무 필드 | 15개 |
| 원본 저장 | MongoDB Bronze |
| 표준 저장 | MongoDB Silver |
| 업무 저장 | MySQL Gold |
| 화면 | Django 대시보드 |

## 3. API 원문 필드

```text
record_id, scheduled_release_at,
area_no, area_nm, p_area_no, p_area_nm, top_area_no,
top_area_nm, top_area_lvl, mgr_no, mgr_nm, mgr_dept_nm,
mgr_pos_nm, mgr_hire_dtm, mgr_act_yn, area_reg_dtm,
top_area_reg_dtm
```

위 목록은 현재 API 원문의 논리 필드 목록이다. `record_id`와 `scheduled_release_at`은 API 호출 시 함께 올 수 있는
선택 응답 메타데이터 원문이고, 나머지 15개 업무 필드는 API 본문 데이터이다. 문서 호환상
`release_at` = `scheduled_release_at`으로 표기할 수 있지만, `release_at`은 API 응답이나
API 본문 데이터의 별도 필드로 저장·생성하지 않는다.
응답 메타데이터가 없다고 오류로 보지 않으며, 값이 있을 때만 원문 그대로 저장한다. 두 값 모두 도메인 판정이나 최신성 판단에 사용하지 않는다.
API가 함께 보내는 `source_row_no`, `source_record_sha256`, `release_slot` 등의 응답 메타데이터도
삭제하지 않고 그대로 보존한다. 이 메타데이터는 업무 컬럼을 추가한 것이 아니며, Silver 표준 필드에는 포함하지 않는다.
파이프라인이 발급한 `run_id`도 원본 문서 최상위에 수집 메타데이터로 함께 저장한다. `run_id`는 API 본문 데이터의 업무 필드에
넣지 않으며, 같은 실행의 상태·건수는 `hr_pipeline_runs`에서 관리한다.

### 3.1 Bronze 원문 저장 형태

아래 예시의 `payload`는 API 응답에서 본문 데이터를 담는 실제 키 이름이다. 문서 설명에서는
이 부분을 쉽게 “본문 데이터”라고 부르지만, 원문 JSON의 키 이름은 바꾸지 않는다.

```json
{
  "run_id": "<배치 ID>",
  "record_id": "...",
  "source_row_no": 1,
  "source_record_sha256": "...",
  "release_slot": "...",
  "scheduled_release_at": "...",
  "payload": {
    "area_no": "...",
    "area_nm": "...",
    "...": "..."
  }
}
```

위 예시는 선택 응답 메타데이터가 함께 온 경우다. 메타데이터가 없는 원본 문서는 해당 키 없이 저장하며, 원문에 없는 키를 NULL이나 기본값으로 만들지 않는다.

Bronze는 `record_id` 유무와 관계없이 저장 가능한 원본 문서 전체를 변경 없이 저장한다. `record_id`가 없으면 MongoDB 내부 식별자
(`_id`, 문서에서는 `bronze_id`로 참조)로 추적하며 원문에 `record_id`를 만들어 넣지 않는다. 이후 API 본문 데이터를 읽어 15개 표준 필드로 정규화하고,
도메인·중복·조직 관계 검증을 적용한다. Silver 저장 검증을 통과한 데이터(`PASS` 또는
`PASS_WITH_WARNING`)를 Silver에 저장하고, 차단 데이터는 검토 큐에 보관한다.

API 본문 데이터는 매핑 단계에서 업무 필드 15개가 정확히 있는지 확인한다. 필드 누락·추가 또는 값이
문자열/NULL이 아니면 `MAPPING_PAYLOAD_SCHEMA_MISMATCH` 또는 `MAPPING_PAYLOAD_TYPE_INVALID`로
검토 큐에 보내고, Bronze 원문은 유지한다. `scheduled_release_at` 누락은 이 오류에 포함하지 않는다.

### 3.2 정규화·검증 결과

정규화 결과는 별도 MongoDB 컬렉션에 저장하지 않고 메모리에서 검증한다.
검증 통과 건은 `hr_silver_standard_records`에 저장하고, 실패 건은 다음 정보와 함께
`hr_review_queue`에 보관한다.

| 정보 | 내용 |
|---|---|
| 원본 연결 | `bronze_id`, 제공된 경우의 `source_record_id`, `source_record_sha256` |
| 표준화 결과 | 변환된 15개 필드(가능한 경우) |
| 오류 | 필드별 `error_code`, 원본 값 |
| 실패 단계 | `NORMALIZATION` 또는 `CANDIDATE_VALIDATION` |

### 3.3 조직 관계 기준

- `parent_area_id`는 바로 위 부모 부서 ID이고, `top_area_id`는 최상위 부서 ID다.
- `area_id == top_area_id`이면 최상위 부서, `area_id != top_area_id`이면 하위 부서로 계산한다.
- 두 관계는 모두 보존하며 조직 계층은 2단계로 고정하지 않는다. 부모 부서가 추가·변경되면 관계를 다시 검증한다.
- 최상위 부서는 부모 부서가 없거나 자기 자신을 부모로 지정해도 정상이다.
- 하위 부서는 존재하는 다른 부서를 부모로 지정해야 한다. 부모가 없거나 자기 자신을 지정하거나 부서 연결이 순환하면 검토 큐로 보낸다.
- 같은 `area_id`의 부서명·상위 부서 정보 변경 또는 `area_id` 변경은 변경 후보로 분류한다. 현재 구현은 자동 갱신하지 않고 `SILVER_EXISTING_CONFLICT`로 검토 큐에 보관한다.

### 3.4 검토 큐 실패 단계

`hr_review_queue`는 하나로 운영하며 `failure_stage` 필드로 실패 지점을 구분한다.

| `failure_stage` | 의미 | 처리 |
|---|---|---|
| `NORMALIZATION` | 매핑·ID·날짜 등 형식 표준화 실패 | Silver에 저장하지 않고 검토 큐 보관 |
| `CANDIDATE_VALIDATION` | 메모리상의 정규화 결과에 대한 도메인·중복·조직 관계 검증 실패 | Silver 저장 보류 |
| `QUALITY_WARNING` | Silver 저장은 가능하지만 품질 경고를 남긴 건 | Silver 저장, 경고 이력 기록 |
| `UNKNOWN` | 기존 문서 등 단계 정보가 없음 | 원본 연결 후 확인 |

모든 검토 문서는 `bronze_id`와 오류 코드로 원본을 추적한다. Bronze 원본은 삭제하지
않으며, 승인 후에도 Bronze에서 정규화·검증을 다시 수행한다.

### 3.5 누락값 보완과 검토 큐 중복 방지

- 같은 `area_id` 또는 `manager_id`에서 하나로 확인되는 값만 누락 필드에 보완한다. 후보가 없거나 여러 값이면 임의로 선택하지 않는다.
- 공백·대소문자·승인된 별칭 차이만 같은 값으로 보고, 실제 값 충돌은 검토 대상으로 남긴다.
- 검토 식별 키는 `review_stage:bronze_id`이며 `bronze_id`가 없으면 `source_record_sha256`를 사용한다.
- 같은 키의 `PENDING_REVIEW` 문서는 upsert해 중복을 막는다. 상태는 `PENDING_REVIEW → APPROVED/REJECTED`로 변경하고, 승인 건만 Bronze에서 재처리한다.
- 재처리 결과·규칙 버전·처리 시각은 `reprocess_history`에 추가하며 Bronze 원문은 바꾸지 않는다.

## 4. 문서별 책임

| 문서 | 담당 내용 |
|---|---|
| [BRD](./brd.md) | 프로젝트 목표와 범위 |
| [PRD](./prd.md) | 구현 기능과 사용자 결과 |
| [API 수집 기준](./data/api_data_collection_guide.md) | 호출·페이지·배치·증분 |
| [필드 사전](./data/data_field_dictionary.md) | 필드 의미와 표준명 |
| [정규화 규칙](./data/data_normalization_and_domain.md) | 값 변환·충돌·오류 |
| [파이프라인 설계](./architecture/data_pipeline_design.md) | Bronze·정규화·Silver·Gold 흐름 |
| [ERD](./erd.md) | 컬렉션·테이블·키 |
| [품질 진단](./quality/data_quality_assessment.md) | 초기 품질과 지표 |
| [검증 계획](./quality/data_validation_plan.md) | 테스트·검증·재처리 |
| [실행 매뉴얼](./operations_runbook.md) | 로컬 실행·스케줄러·재처리 명령 |
| [스케줄러 매뉴얼](./scheduler_run_manual.md) | 5분 실행·상태 확인·중지 |

## 5. 공통 처리 흐름

```text
API → 계약 검사 → Bronze → 메모리 매핑·정규화 → 도메인·중복 검증 → Silver → Gold → Django
             └─ 수집·계약 실패 → 페이지/실행 이력
                                      └─ 정규화·검증 실패 → Review Queue
```

### 5.1 Bronze 원본 무결성 및 책임 분리

Bronze 저장 직후 API 응답 데이터 건수와 저장 건수, 페이지·cursor 누락·반복, 원본 필드명·값, 원본 해시와 `run_id` 연결을 확인한다.
설명되지 않은 차이는 실행 또는 페이지 실패로 기록하고 원본은 보존한다. 무결성 확인이 끝나기 전에는 Silver 처리를 시작하지 않는다.

API 수집 소스는 API 호출·페이지 기록·Bronze 저장까지만 담당한다. 정규화·도메인·중복·조직 관계 검증은 별도 Silver 실행이
Bronze를 읽어 수행하며, 통과 결과는 Silver에 저장하고 실패 결과는 `hr_review_queue`에 보관한다.

## 6. 동일 ID 처리 요약

`run_id`는 배치 추적용일 뿐 버전이 아니다. API는 버전 필드나 정렬 기준을 제공하지 않으므로
`run_id`, `record_id`, `scheduled_release_at`으로 최신값을 추정하지 않는다. `run_id`는 Bronze 원본 문서 최상위 메타데이터와
`hr_pipeline_runs` 실행 이력에 함께 기록한다.

| 상황 | 처리 |
|---|---|
| 동일 API 본문 데이터 재수신 | 중복 반영 제외, 수신 이력만 기록 |
| 같은 업무 ID의 다른 API 본문 데이터 | `SOURCE_RECORD_CONFLICT`로 Review 보관 |
| 어느 값이 최신인지 판단 불가 | Bronze 원문 보존, Silver·Gold 자동 갱신 금지 |
| 담당자 승인 후 수정값 존재 | Bronze 복사본에 수정값을 적용해 재처리 |

서로 다른 배치의 필드를 임의로 섞지 않는다. 검토 승인 후 재처리한 결과만 Silver에 저장한다.

## 7. 데이터 품질 목표

- 필수 API 본문 데이터 15개 계약 준수율 100%와 제공된 선택 응답 메타데이터 원문 보존율 100%
- API→Bronze 원본값 유실 0건
- 승인 전 오류·판단 불가 Gold 반영 0건
- 설명되지 않는 계층별 건수 차이 0건
- 동일 ID 충돌 데이터가 기존 정상값을 자동 변경한 건수 0건
- Gold와 Django 주요 집계 차이 0건

## 8. 제어 정보 분리

API 원본 문서에 포함된 응답 메타데이터와 파이프라인이 발급한 `run_id`는 원문 추적을 위해 Bronze에 남긴다.
`run_id`는 API 본문 데이터의 업무 컬럼에 추가하지 않는다. 실행 상태 등 나머지 제어 정보는 다음 저장소에 둔다.

- `run_id`, 실행 시각, 증분 구간 → Bronze 최상위 및 `hr_pipeline_runs`
- 현재 `cursor`, `next_refresh_at` → `hr_pipeline_control`
- 페이지 순서·요청/다음 cursor 해시·요청/응답 정보·응답 해시 → `hr_pipeline_pages`
- 규칙 버전과 실행 처리 건수 → `hr_pipeline_runs`
- 실행 결과 JSON 요약 → 실행 로그(stdout)
- 오류·검토·승인 상태 → `hr_review_queue`
- Bronze–Silver–Gold 계보와 검토·반영 근거 → `hr_lineage_links`

`hr_lineage_links`는 Silver 저장 시 Bronze–Silver 연결을 기록하고, Gold 적재가
끝나면 이번 Silver 원본의 `bronze_id`를 우선 찾아 `load_batch_id`와 테이블별
`gold_key`를 같은 링크에 추가한다. 원본 ID가 없는 구형 링크만 `silver_key`로
호환 갱신한다.

`hr_gold_load_batch`에는 `started_at`, `finished_at`, `source_silver_count`,
`loaded_count`, `skipped_count`, `report_json`을 저장한다. `loaded_count`는
현재 Gold 전체 행 수가 아니라 해당 실행에서 처리한 네 테이블의 합계다.

새 실행부터 위 이력·해시·계보 필드가 기록된다. 기존 `hr_pipeline_pages` 문서에
남아 있는 원본 cursor 필드와 기존 Silver 계보의 Gold 필드는 자동으로 소급 변경하지
않으며, 기존 데이터까지 기준을 맞출 때는 별도 점검·백필 작업을 수행한다.

Gold 업무 테이블은 `hr_area`, `hr_manager`, `hr_area_manager_assignment`,
`area_manager_features` 네 개이며, 마지막 테이블은 조직·담당자 관계에서
계산한 `organization_type`과 `has_parent`를 제공한다.

## 9. 변경 원칙

- 원본 필드명과 개수는 고정한다.
- 허용 값·날짜 형식·NULL 토큰 추가는 YAML과 테스트만 수정한다.
- 새로운 판정 알고리즘은 Python 모듈과 테스트를 추가한다.
- 규칙을 변경하면 이전 정상 결과와 회귀 테스트를 수행한다.

## 10. 완료 기준

- 모든 상세 규칙에 기준 문서 링크가 있다.
- Bronze·Silver·Gold의 책임과 데이터 흐름이 설명된다.
- 오류 데이터의 격리와 재처리 방법이 정의된다.
- 실행 결과를 JSON과 계보로 확인할 수 있다.

## 11. Django 조회·CSV 출력 계약

Django는 MySQL Gold만 조회한다. Bronze 원문이나 Silver 문서를 화면에 직접 연결하지 않는다.

| 화면 | 기본 조회 | CSV 주소 |
|---|---|---|
| 조직 목록 | 조직코드·조직명·부모·최상위 조직·담당자 | /hrdata/areas/export.csv |
| 담당자 목록 | 담당자코드·담당자명·부서·직급·상태·담당 조직 수 | /hrdata/managers/export.csv |

- 검색 조건이 있으면 검색 결과를 내려받고, ?all=1이면 검색 조건과 페이지 제한 없이 전체 Gold 데이터를 내려받는다.
- CSV에서는 조직코드와 조직명, 담당자코드와 담당자명을 서로 다른 열로 제공한다.
- CSV의 `조직구분`은 `area_id`와 `top_area_id` 관계로 계산한 Gold `organization_type`을 사용한다.
- 날짜는 화면과 CSV에서 초 단위까지만 표시한다.
- 이름의 앞뒤 공백과 승인된 반복 단어 제거는 화면 표시용이다. Silver·Gold에 저장된 원본 표준값은 변경하지 않는다.
- 화면 표시 정리로 원본 레코드 수나 키 값이 합쳐지지 않는다.

## 12. 추가 산출물

- 실행 순서·명령어·스케줄러 확인 방법은 [실행 매뉴얼](./operations_runbook.md)에 기록한다.
- 실행별 Bronze–Silver–Review–Gold 건수와 품질 게이트 결과는 [품질 보고서 양식](./quality/pipeline_quality_report_template.md)에 기록한다.
