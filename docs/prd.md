# PRD — 레거시 인사·조직 데이터 표준화 및 조회 솔루션

문서 상태: 초안

## 1. 제품 목표

API로 수집한 레거시 인사·조직 데이터를 표준화하고, 검증된 조직 정보를 조회할 수 있는 Django 대시보드를 제공한다.

```text
API → MongoDB Bronze → 정규화·검증 → MongoDB Silver → MySQL Gold → Django 대시보드
                                      └─ 실패·판단 불가 → 검토 큐
```

제품은 인사 업무 시스템을 대체하지 않고, 조직과 관리자 정보를 안정적으로 조회하는 데 집중한다.

## 2. 대상 조직

### 인사·조직 부서

- 표준화된 부서·관리자·조직 계층 정보를 한 곳에서 확인할 수 있게 된다.
- 조직 변경 사항을 빠르게 파악할 수 있게 된다.

### 각 업무 부서

- 소속 부서와 상위 부서 관계를 바로 파악할 수 있게 된다.
- 담당 관리자와 재직 상태를 쉽게 확인할 수 있게 된다.

### 데이터 품질 관리 부서

- 오류·판단 불가 데이터를 별도로 파악할 수 있게 된다.
- 검토·재처리 상태를 추적할 수 있게 된다.

### 데이터 파이프라인 운영 부서

- API 수집과 데이터 처리 상태를 한눈에 파악할 수 있게 된다.
- 실패 원인과 실행 이력을 추적할 수 있게 된다.

### 조직 정보 조회 부서

- 검증된 조직 정보를 빠르게 검색할 수 있게 된다.
- 대시보드에서 조직 현황을 쉽게 파악할 수 있게 된다.

## 3. 기능 요구사항

### 3.1 API 수집

- API에서 조직 데이터를 주기적으로 수집한다.
- API 키는 실행 시 조회한다.
- API 버전이나 수정 시각은 사용하지 않는다.
- API 호출을 시작할 때 `run_id`를 생성하고 해당 실행의 데이터와 이력을 연결한다.
- 한 번의 요청은 최대 1,000건으로 처리한다.
- 첫 요청은 cursor 없이 시작한다.
- 응답의 `next_cursor`를 다음 요청에 사용한다.
- API 본문 데이터가 비어 있어도 cursor를 유지한다.
- `next_refresh_at` 이후 같은 cursor로 다시 요청한다.
- 수집 실패와 실행 상태를 확인할 수 있어야 한다.

사용 API:

| API | 인증 | 용도 |
|---|---|---|
| `GET /public/v1/key` | 없음 | 당일 사용할 API 키 조회 |
| `GET /api/v1/meta` | `X-API-Key` 필요 | 공개 행 수, 다음 공개 시각, 원본 컬럼 확인 |
| `GET /api/v1/records` | `X-API-Key` 필요 | cursor 기반 원본 데이터 페이지 수집 |
| `GET /api/v1/records/{id}` | `X-API-Key` 필요 | 공개된 단일 원본 레코드 확인 |
| `GET /health/ready` | 없음 | API와 데이터셋 준비 상태 확인 |

`/api/v1/records` 응답의 API 본문 데이터를 Bronze에 저장하고, `next_cursor`는 다음 페이지 요청에 사용한다.

### 3.2 Bronze 원본 보존

MongoDB 컬렉션 `hr_bronze_raw_records`에 API 응답 메타데이터와 실제 원본 데이터를 저장한다.

- 원본 필드명과 값을 변경하지 않는다.
- 수집된 원문은 삭제하거나 정제 결과로 덮어쓰지 않는다.
- 실행 식별자와 수집 시각을 원문과 연결한다.
- 원문 추적을 위한 해시를 저장한다.
- `record_id`와 `scheduled_release_at`은 원문 보존용 값이며 업무 판단에 사용하지 않는다.
- API 키와 HTTP 응답 본문은 페이지 실행 이력에 저장하지 않는다.

실행 이력은 다음 컬렉션에서 관리한다.

```text
hr_pipeline_control
hr_pipeline_pages
hr_pipeline_runs
```

### 3.3 Silver 표준화·검증

MongoDB 컬렉션 `hr_silver_standard_records`에는 표준화된 15개 필드만 저장한다.

```text
area_id
area_name
parent_area_id
parent_area_name
top_area_id
top_area_name
top_area_level
manager_id
manager_name
department_name
position_name
manager_hire_at
manager_active_yn
area_registered_at
top_area_registered_at
```

처리 내용:

- 원본 필드명을 표준 필드명으로 변환한다.
- 문자열 공백과 ID 표현을 정리한다.
- 날짜 형식을 표준 시간 형식으로 변환한다.
- 상태와 조직 레벨 값을 정해진 값으로 변환한다.
- YAML 규칙을 읽어 정규화한다.
- 정상 데이터만 Silver에 저장한다.

### 3.4 오류·검토 데이터

MongoDB 컬렉션 `hr_review_queue`에 다음 데이터를 저장한다.

- 필수값 누락
- 날짜·ID 형식 오류
- 미등록 도메인 값
- 중복 ID 또는 값 충돌
- 부모 부서 미존재
- 조직 관계를 판단할 수 없는 데이터

검토 처리:

1. 담당자가 검토 결과와 수정값을 입력한다.
2. 승인된 데이터는 Bronze 원문을 다시 읽는다.
3. 현재 YAML 규칙으로 다시 정규화한다.
4. 검증을 통과하면 Silver에 저장한다.
5. 검토 및 재처리 이력을 기록한다.

### 3.5 조직 계층

- `parent_area_id`는 바로 위 부모 부서 ID로 사용한다.
- `top_area_id`는 최상위 부서 ID로 사용한다.
- `area_id == top_area_id`이면 최상위 부서로 계산한다.
- `area_id != top_area_id`이면 하위 부서로 계산한다.
- `parent_area_id`와 `top_area_id`는 서로 다른 관계로 보존하며, 조직 계층은 2단계 이상으로 확장할 수 있어야 한다.
- 최상위 부서는 부모 부서가 없거나 자기 자신을 부모로 지정해도 정상으로 처리한다.
- 하위 부서는 존재하는 다른 부서를 부모로 지정해야 한다.
- 부서 코드·명칭·상위 부서 정보가 함께 일관되게 변경되면 부서 이동으로 보고 갱신한다.
- 변경 근거가 부족하거나 하위 부서의 부모가 없거나, 자기 자신을 부모로 지정하거나, 부서 간 연결이 순환하면 검토 큐로 보낸다.

세부 판정 규칙과 예외 처리는
[data_normalization_and_domain.md](<../data_normalization_and_domain.md>)를 참조한다.

### 3.6 Gold와 대시보드

- Gold는 검증을 통과한 Silver만 입력으로 사용한다.
- Gold에서 조직 계층과 조회용 지표를 계산한다.
- Gold 데이터는 MySQL에 저장한다.
- Django 대시보드는 부서·관리자·조직 계층·데이터 품질 상태를 제공한다.
- Gold와 대시보드에는 검토되지 않은 데이터를 표시하지 않는다.

## 4. 데이터 매핑 기준

현재 API에서 받은 실제 원본 데이터의 필드를 다음 Silver 필드로 매핑한다.

```text
area_no          → area_id
area_nm          → area_name
p_area_no        → parent_area_id
p_area_nm        → parent_area_name
top_area_no      → top_area_id
top_area_nm      → top_area_name
top_area_lvl     → top_area_level
mgr_no           → manager_id
mgr_nm           → manager_name
mgr_dept_nm      → department_name
mgr_pos_nm       → position_name
mgr_hire_dtm     → manager_hire_at
mgr_act_yn       → manager_active_yn
area_reg_dtm     → area_registered_at
top_area_reg_dtm → top_area_registered_at
```

Bronze는 API 응답 메타데이터와 원본 17개 필드를 함께 보존하고, Silver는 업무용 15개 필드만 사용한다.

## 5. 스케줄러와 실행 방식

운영 환경과 실행 주기, 실행 방법은 구현·운영 환경을 확정한 뒤 결정한다.

## 6. 실행 이력과 로그

페이지 실행 이력에는 다음 정보를 저장한다.

- HTTP 상태
- 요청 시각
- 응답 시각
- 지연 시간
- 응답 해시
- 처리 행 수
- `next_cursor`
- `next_refresh_at`

API 키와 HTTP 응답 본문은 실행 이력에 저장하지 않는다. 원본 데이터 확인이 필요하면 Bronze 원문을 참조한다.

## 7. 품질 기준

- Bronze 원문과 수집 실행을 연결할 수 있어야 한다.
- Silver는 정의된 15개 표준 필드 구조를 사용해야 한다.
- 정제 실패 데이터가 삭제되지 않고 검토 큐에 남아야 한다.
- Gold에는 Silver 검증 통과 데이터만 포함되어야 한다.
- 조직 계층 관계와 관리자 상태를 조회할 수 있어야 한다.
- 동일한 입력과 규칙으로 재실행하면 같은 결과가 나와야 한다.

## 8. 비기능 요구사항

- 원본 데이터는 변경하지 않는다.
- 정제 규칙은 YAML에서 관리한다.
- 오류 원인과 처리 상태를 추적할 수 있어야 한다.
- 수집이 중단되어도 다음 실행에서 마지막 cursor부터 이어받을 수 있어야 한다.
- 운영 로그에 API 키와 민감한 원문을 기록하지 않는다.

## 9. Gold 테이블과 기준 시점

- Gold 업무 테이블은 `hr_area`, `hr_manager`, `hr_area_manager_assignment`를 사용한다.
- `stg_hr_standard_records`는 Gold 적재 전 검증용으로 사용한다.
- `hr_gold_load_batch`는 Gold 적재 이력을 저장한다.
- Gold 기준 시점은 부서 등록일시가 아니라 `hr_pipeline_runs`의 실행 시각으로 관리한다.
- `run_id`로 Gold 적재 이력과 해당 수집 실행을 연결한다.

## 10. 구현 순서

```text
1. API 수집과 Bronze 저장
2. Silver 매핑·정규화·검증
3. 검토 큐와 재처리
4. 조직 관계 검증
5. Gold 계산과 MySQL 저장
6. Django 대시보드
7. 전체 통합 테스트
```
