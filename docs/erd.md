# MongoDB·MySQL ERD

## 1. 설계 원칙

- Bronze는 API 응답 메타데이터와 API 본문 데이터를 포함한 원문 전체를 저장한다.
- 원본 계약의 논리 필드 목록은 17개로 관리한다. API 본문 데이터의 업무 필드 15개는 필수이고, API 응답 메타데이터 2개는 선택이며 업무 컬럼으로 세지 않는다.
- Bronze의 API 본문 데이터를 메모리에서 15개 표준 필드로 정규화한 뒤 도메인·중복·조직 관계를 검증한다.
- Silver는 위 검증을 통과한 업무 필드 15개만 저장한다.
- 오류·검토·실행 정보는 별도 컬렉션에 저장한다.
- Gold는 현재 조직·담당자·배정 상태만 저장한다.

## 2. 저장 구조

### MongoDB

| 컬렉션 | 역할 |
|---|---|
| `hr_bronze_raw_records` | API 원문 전체. API 본문 데이터 15개와, 있을 때만 보존하는 선택 응답 메타데이터 |
| `hr_silver_standard_records` | 정규화·도메인·중복 검증을 통과한 표준 15개 필드 |
| `hr_pipeline_runs` | 배치 실행·규칙 버전·처리 건수(실패 시 상세 보고) |
| `hr_pipeline_control` | 현재 cursor·`next_refresh_at` |
| `hr_pipeline_pages` | 페이지·HTTP 상태·요청/응답 시각·응답 해시 |
| `hr_review_queue` | 오류·판단 불가·승인 상태 |
| `hr_lineage_links` | 계층 간 연결과 검토·반영 근거(후속 연동 단계) |

### MySQL

| 테이블 | 역할 |
|---|---|
| `hr_area` | 조직과 조직 계층 |
| `hr_manager` | 담당자 현재 정보 |
| `hr_area_manager_assignment` | 조직별 현재 담당자 |
| `hr_gold_load_batch` | Gold 적재 실행 이력 |
| `stg_hr_standard_records` | Gold 반영 전 임시 검증 |

## 3. 간단한 관계

```text
Bronze 원본
   ↓ 15개 필드 매핑·형식 정규화
도메인·중복·조직 관계 검증
   ├─ 실패 ───────────────→ review_queue
   └─ 통과 → Silver 표준 ─→ staging → Gold

실행·상태·페이지·계보 정보는 별도 컬렉션에 저장
```

## 4. MongoDB 문서 기준

### Bronze 문서 필드(`hr_bronze_raw_records`)

Bronze는 API가 보낸 원본 데이터 전체를 이름과 값 그대로 저장한다. 원본 논리 목록은 17개이며,
API 응답 메타데이터의 `record_id`와 `scheduled_release_at`은 선택이고 나머지는 API 본문 데이터 안의 필드다. API가 함께 보낸 응답 메타데이터도
삭제하거나 변환하지 않는다. 따라서 Bronze에는 논리 원본 17개 외에 임의의 업무 컬럼을 추가하지
않으면서도 API 원문을 재현할 수 있다.

| 구분 | 저장 필드 | 저장 목적 |
|---|---|---|
| API 원문 식별·시각(선택) | `record_id`, `scheduled_release_at` | 값이 있을 때만 원문 보존. 최신성·도메인 판단에는 사용하지 않음 |
| 조직 | `area_no`, `area_nm`, `p_area_no`, `p_area_nm`, `top_area_no`, `top_area_nm`, `top_area_lvl` | API 본문 데이터 내부 조직·계층 원본 보존 |
| 담당자 | `mgr_no`, `mgr_nm`, `mgr_dept_nm`, `mgr_pos_nm`, `mgr_act_yn`, `mgr_hire_dtm` | API 본문 데이터 내부 담당자 원본 보존 |
| 등록 시각 | `area_reg_dtm`, `top_area_reg_dtm` | API 본문 데이터 내부 조직 등록 시각 원본 보존 |
| API 응답 메타데이터 | `source_row_no`, `source_record_sha256`, `release_slot` 등 | 수신 원문 재현 및 추적 |
| 수집 실행 메타데이터 | `run_id` | 어느 API 실행에서 저장했는지 추적. 업무 필드·API 본문 데이터에는 포함하지 않음 |

### Silver 문서 필드(`hr_silver_standard_records`)

Silver는 Bronze의 논리 원본에서 API 응답 메타데이터 값인 `record_id`, `scheduled_release_at`을 제외하고
API 본문 데이터 안의 업무 필드 15개를 정제해 저장한다. 문서 호환상 `release_at` = `scheduled_release_at`으로
표기할 수 있지만, `release_at`은 API 응답이나 API 본문 데이터의 별도 필드로 저장하지 않는다.

| 구분 | 저장 필드 | 저장 기준 |
|---|---|---|
| 조직 | `area_id`, `area_name`, `parent_area_id`, `parent_area_name`, `top_area_id`, `top_area_name`, `top_area_level` | ID·명칭·조직 레벨 표준화 |
| 담당자 | `manager_id`, `manager_name`, `department_name`, `position_name`, `manager_active_yn`, `manager_hire_at` | ID·이름·부서·직급·상태·입사일 표준화 |
| 등록 시각 | `area_registered_at`, `top_area_registered_at` | 승인된 날짜 형식으로 변환 |

- `record_id`, `scheduled_release_at`은 Bronze와 후속 계보 정보에서만 확인한다.
- `run_id`는 표준 업무 필드에 섞지 않고 Bronze 원본 문서 최상위와 실행 이력에 둔다. 오류 코드·규칙 버전은 제어 저장소에 둔다.

### Bronze

- API 응답 메타데이터와 API 본문 데이터를 필드명·값 그대로 저장한다.
- `record_id`와 `scheduled_release_at`은 있으면 저장하고, 없으면 필드를 만들지 않는다. `record_id`가 없을 때는 MongoDB 내부 `bronze_id`로 추적한다.
- 같은 업무 ID의 여러 API 원문을 함께 보관한다. `scheduled_release_at`으로 최신성을 판단하지 않는다.
- 파이프라인이 생성하는 `run_id`는 Bronze 원본 문서 최상위와 `hr_pipeline_runs`에 저장한다. 오류 코드·규칙 버전은 제어 컬렉션에 따로 저장한다.

### Silver

- 선택 응답 메타데이터(`record_id`, `scheduled_release_at`)를 제외한 표준 15개 필드만 저장한다. 표준화 대상은 Bronze API 본문 데이터 내부 값이다.
- 현재 조직–담당자 상태는 `area_id`당 한 문서를 유지한다.
- API 버전 정보가 없으므로 동일 ID 충돌은 자동으로 현재값을 교체하지 않는다. 승인 후 재처리한 결과만 명시적으로 갱신한다.

### 제어·검토

| 컬렉션 | 필요한 값 |
|---|---|
| `hr_pipeline_runs` | `run_id`, 실행 시각, 상태, 규칙 버전, 처리 건수, 실패 시 상세 보고 |
| `hr_pipeline_control` | 현재 cursor, `next_refresh_at` |
| `hr_pipeline_pages` | `run_id`, 페이지 순서, cursor, HTTP 상태, 요청·응답 시각, 지연 시간, 응답 해시, 오류 코드 |
| `hr_review_queue` | 관련 Bronze ID, `failure_stage`, 오류 코드, 검토 상태, 검토자, 검토 시각, 메모 |
| `hr_lineage_links` | Bronze ID, Silver ID, Gold 키, API 원문 시각, 검토·반영 사유 |

페이지 이력에는 응답 본문과 API 키를 저장하지 않는다. 검토 결과는 `PENDING_REVIEW`,
`APPROVED`, `REJECTED` 중 하나로 기록하며, 승인 결과도 Silver에 직접 입력하지 않고 재처리 절차를 거친다.

`hr_review_queue.failure_stage`는 다음 값 중 하나다.

| 값 | 의미 |
|---|---|
| `NORMALIZATION` | 매핑·ID·날짜 등 형식 표준화 실패. Silver에 저장하지 않음 |
| `CANDIDATE_VALIDATION` | 메모리상의 정규화 결과에 대한 도메인·중복·조직 관계 검증 실패. Silver에 저장하지 않음 |
| `UNKNOWN` | 이전 문서 등 실패 단계 정보가 없는 검토 건 |

## 5. MySQL 핵심 컬럼

### `hr_area`

`area_id` PK, `area_name`, `parent_area_id`, `parent_area_name`, `top_area_id`, `top_area_name`, `top_area_level`, 등록일시

### `hr_manager`

`manager_id` PK, `manager_name`, `department_name`, `position_name`, `manager_hire_at`, `manager_active_yn`

### `hr_area_manager_assignment`

`area_id` PK/FK, `manager_id` FK

- 조직당 현재 담당자는 한 명이다.
- 한 담당자가 여러 조직을 맡는 것은 허용한다.

### `hr_gold_load_batch`

`load_batch_id` PK, `run_id`, `rule_version`, 입력 해시, 적재 건수, 상태, 보고서 해시

## 6. 동일 ID 처리

| 상황 | Silver·Gold |
|---|---|
| 동일 API 본문 데이터 재수신 | 중복 반영 없음, 수신 이력 기록 |
| 같은 업무 ID의 다른 API 본문 데이터 | Review Queue 보관 |
| 최신 여부를 판단할 수 없는 배치 | Bronze 보존, Silver·Gold 자동 갱신 금지 |
| 담당자 승인 후 재처리 성공 | Silver 저장 후 Gold 반영 대상 |

API는 버전 필드나 정렬 기준을 제공하지 않는다. 따라서 `run_id`, `record_id`,
`scheduled_release_at`으로 최신값을 추정하지 않는다.

## 7. 반영 절차

1. 도메인·중복·관계 검증을 통과한 Silver를 staging에 넣는다.
2. 건수·PK·FK·조직 관계를 확인한다.
3. 통과하면 하나의 트랜잭션으로 Gold를 갱신한다.
4. 실패하면 전체를 취소하고 기존 Gold를 유지한다.

## 8. 참고 문서

- [데이터 필드 사전](./data/data_field_dictionary.md)
- [정규화 및 도메인 규칙](./data/data_normalization_and_domain.md)
- [파이프라인 설계](./architecture/data_pipeline_design.md)
- [검증 계획](./quality/data_validation_plan.md)
