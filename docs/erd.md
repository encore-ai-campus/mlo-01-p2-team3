# MongoDB·MySQL ERD

## 1. 현재 설계 범위

이 문서는 현재 구현된 데이터 관계만 설명한다.

- 입력은 레거시 API JSON이다.
- API 응답과 원문은 MongoDB Bronze에 먼저 저장한다.
- 정규화·도메인·중복·조직 관계 검사는 메모리에서 수행한다.
- 통과한 15개 표준 업무 필드는 Silver에 저장하고, 실패한 건은 검토 큐에 보관한다.
- Silver만 MySQL Gold의 입력으로 사용한다.
- Django는 Gold를 조회한다. Django가 Bronze나 Silver를 직접 수정하지 않는다.
- 별도의 표준화 후보 컬렉션은 두지 않는다. 정규화 후보는 메모리에만 존재한다.

## 2. 전체 관계

~~~text
API JSON
   │
   ▼
hr_bronze_raw_records  (원문 전체 보존)
   │
   ├─ 메모리 정규화·검증 통과 ─→ hr_silver_standard_records
   │                                  │
   │                                  ▼
   │                         메모리 품질 게이트 → MySQL Gold
   │                                  │
   │                                  ▼
   │                              Django
   │
   └─ 정규화·검증 실패 ───────────→ hr_review_queue
~~~

실행·페이지·cursor·계보 정보는 업무 데이터와 분리해 별도 컬렉션에 저장한다.

## 3. MongoDB 컬렉션

| 컬렉션 | 저장 내용 | 핵심 연결 |
|---|---|---|
| hr_bronze_raw_records | API 응답 메타데이터와 본문 데이터의 원문 전체 | run_id, bronze_id |
| hr_silver_standard_records | 검증을 통과한 표준 업무 필드 15개 | bronze_id는 계보에서 추적 |
| hr_review_queue | 정규화·도메인·중복·관계 검증 실패 건 | bronze_id, failure_stage |
| hr_pipeline_runs | 배치 상태·규칙 버전·처리 건수·결과 | run_id |
| hr_pipeline_pages | 페이지 순서·cursor 해시·next_cursor 해시·HTTP 상태·시각·지연·응답 해시 | run_id |
| hr_pipeline_control | 다음 수집에 사용할 cursor·next_refresh_at | 현재 증분 상태 |
| hr_lineage_links | Bronze·Silver·Gold의 연결 근거 | bronze_id, silver_key, load_batch_id, gold_key |

### 3.1 Bronze 문서

Bronze는 API가 보낸 구조와 값을 바꾸지 않는다. 응답 한 건의 본문 데이터는
실제 API 키인 `payload` 아래에 들어올 수 있고, 응답 메타데이터는 문서 최상위에
들어올 수 있지만 둘 다 원문 보존 대상이다. 설명에서는 `payload`를 “본문 데이터”라고 부른다.

~~~json
{
  "run_id": "배치 ID",
  "record_id": "있을 때만 저장",
  "scheduled_release_at": "있을 때만 저장",
  "source_record_sha256": "원문 해시",
  "payload": {
    "area_no": "BIZ-00001",
    "area_nm": "조직명",
    "p_area_no": "BIZ-00001",
    "p_area_nm": "조직명",
    "top_area_no": "BIZ-00001",
    "top_area_nm": "조직명",
    "top_area_lvl": "TOP",
    "mgr_no": "EMP000001",
    "mgr_nm": "담당자",
    "mgr_dept_nm": "부서",
    "mgr_pos_nm": "직급",
    "mgr_hire_dtm": "2020-01-01 09:00:00",
    "mgr_act_yn": "Y",
    "area_reg_dtm": "2020-01-01 09:00:00",
    "top_area_reg_dtm": "2020-01-01 09:00:00"
  }
}
~~~

- record_id, scheduled_release_at은 선택 응답 메타데이터다. 없으면 새 값을 만들지 않는다.
- 두 값과 run_id는 최신성이나 업무 도메인 판단에 사용하지 않는다.
- 원문이 깨졌거나 필드가 부족해도 Bronze 문서는 삭제하지 않고 검토 큐에 연결한다.
- 같은 API 원문이 다시 오면 기존 문서를 덮어쓰지 않고 수신 이력만 남긴다.

### 3.2 Silver 문서

Silver에는 API 본문 데이터 15개를 표준명으로 바꾼 결과만 저장한다.

| 그룹 | 표준 필드 |
|---|---|
| 조직 | area_id, area_name, parent_area_id, parent_area_name, top_area_id, top_area_name, top_area_level |
| 담당자 | manager_id, manager_name, department_name, position_name, manager_hire_at, manager_active_yn |
| 일시 | area_registered_at, top_area_registered_at |

record_id, scheduled_release_at, API 키와 응답 본문은 Silver 업무 필드에 넣지 않는다.
정규화 실패나 관계 판단 실패는 Silver에서 제외하고 hr_review_queue에 원본 연결 정보와 함께 기록한다.

## 4. MySQL Gold 테이블

| 테이블 | 기본 키 | 역할 |
|---|---|---|
| hr_area | area_id | 조직·부모·최상위 조직 정보 |
| hr_manager | manager_id | 담당자 현재 정보 |
| hr_area_manager_assignment | area_id | 조직별 현재 담당자 연결 |
| area_manager_features | area_id | 조직 유형과 담당자 상태를 함께 제공 |
| hr_gold_load_batch | load_batch_id | Gold 적재 실행·규칙·건수·상태 |

Gold 적재 전 품질 게이트는 메모리에서 수행하며, `stg_hr_standard_records` 테이블은
현재 사용하지 않는다.

### 4.1 조직 관계

- parent_area_id는 바로 위 부모 조직 ID다.
- top_area_id는 최상위 조직 ID다.
- area_id == top_area_id이면 Gold의 organization_type을 TOP으로 계산한다.
- area_id != top_area_id이면 organization_type을 SUB로 계산한다.
- 최상위 조직은 부모 ID와 부모명이 NULL이어도 정상이다.
- 하위 조직은 존재하는 다른 조직을 부모로 가져야 한다. 부모 누락·자기 참조·순환 연결은 검토 대상이다.
- 부모와 최상위 관계는 모두 보존하며, 계층 깊이를 2단계로 고정하지 않는다.

top_area_level은 API 값의 표준화 결과이고, organization_type은 Gold가 ID 관계로 계산한 조회용 값이다.

### 4.2 담당자 관계

- 한 담당자가 여러 조직을 맡는 것은 허용한다.
- 한 조직의 현재 담당자는 한 명으로 관리한다.
- manager_id, manager_name, manager_active_yn은 Gold 담당자 필수값이다.
- 부서명·직급·입사일의 선택값 충돌은 해당 컬럼만 NULL로 둘 수 있다.
- 실제 담당자 ID·이름·상태 충돌은 Gold 자동 적재를 막고 검토 정보로 남긴다.

## 5. 실행·검토·계보 문서 필드

### hr_pipeline_runs

run_id, 실행 시작·종료 시각, 상태, 규칙 버전, 페이지 수, Bronze 저장 건수,
Silver 처리 건수, 검토 건수, 오류 요약을 저장한다.

### hr_pipeline_pages

run_id, 페이지 순서, 요청 cursor 해시, 다음 cursor 해시, HTTP 상태,
요청 시각, 응답 시각, 지연 시간, 응답 해시, 오류 코드를 저장한다.
응답 본문과 API 키는 저장하지 않는다.

### hr_review_queue

review_id, bronze_id, 정규화 결과(가능한 경우), 원본 오류값, 오류 코드,
failure_stage, 검토 상태, 검토자, 검토 시각, 수정값, 재처리 결과를 저장한다.

failure_stage는 다음처럼 구분한다.

| 값 | 의미 |
|---|---|
| NORMALIZATION | 매핑·ID·날짜·문자열 형식 변환 실패 |
| CANDIDATE_VALIDATION | 도메인·중복·조직 관계 검증 실패 |
| QUALITY_WARNING | Silver에는 저장했지만 경고를 남긴 건 |
| UNKNOWN | 실패 단계가 확인되지 않는 기존 건 |

### hr_lineage_links

bronze_id, bronze_run_id, Silver 키, 처리 시각, 규칙 버전,
품질 상태와 경고 코드를 저장한다. Gold 적재 시에는 load_batch_id와 Gold 키를 연결한다.

### hr_gold_load_batch

load_batch_id, run_id, rule_version, input_hash, loaded_count, status, report_hash와
함께 Silver 입력 건수, 제외 건수, 실행 시작·종료 시각, 상세 report_json을 저장한다.
loaded_count는 해당 배치의 적재 행 수이며 Gold 전체 테이블 행 수를 의미하지 않는다.

## 6. Django 조회와 CSV

Django는 MySQL Gold만 읽는다.

- 조직 화면은 조직코드, 조직명, 부모·최상위 조직 정보를 별도 열로 보여준다.
- 담당자 화면은 담당자코드, 담당자명, 부서, 직급, 상태를 별도 열로 보여준다.
- 검색 결과 CSV와 전체 CSV를 각각 내려받을 수 있다.
- 조직 CSV: /hrdata/areas/export.csv, 전체는 ?all=1
- 담당자 CSV: /hrdata/managers/export.csv, 전체는 ?all=1
- 화면에서만 이름의 앞뒤 공백과 승인된 반복 단어를 정리한다. DB의 Silver·Gold 원문 값은 변경하지 않는다.

## 7. 적재 원칙

1. Silver 검증 통과 데이터를 staging에서 다시 확인한다.
2. Gold PK·FK·조직 관계를 확인한다.
3. 통과하면 트랜잭션으로 Gold 테이블을 일괄 반영한다.
4. 실패하면 기존 Gold를 유지하고 적재 이력과 오류를 남긴다.
5. Bronze와 Review Queue 원본은 Gold 결과와 관계없이 삭제하지 않는다.

## 8. 참고 문서

- [데이터 명세](./data_spec.md)
- [API 수집 기준](./data/api_data_collection_guide.md)
- [필드 사전](./data/data_field_dictionary.md)
- [정규화 및 도메인 규칙](./data/data_normalization_and_domain.md)
- [파이프라인 설계](./architecture/data_pipeline_design.md)
- [검증 계획](./quality/data_validation_plan.md)
- [실행 매뉴얼](./operations_runbook.md)
