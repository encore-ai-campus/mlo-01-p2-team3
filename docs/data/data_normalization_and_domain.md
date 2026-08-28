# 데이터 정규화 및 도메인 규칙

- 문서 ID: `NORM-HR-001`
- 기준 버전: `normalization-v1.2`
- 적용 대상: 레거시 API 응답 메타데이터와 API 본문 데이터의 원문 → 정규화·검증 → Silver 표준 업무 필드 15개

## 1. 핵심 원칙

1. API 응답 메타데이터와 API 본문 데이터를 먼저 MongoDB Bronze에 그대로 저장한다.
2. 응답 메타데이터의 선택 필드(`record_id`, `scheduled_release_at`)와 관계없이 API 본문 데이터의 15개 업무 필드를 메모리에서 정규화한다. 문서 호환상 `release_at` = `scheduled_release_at`으로 표기할 수 있지만, `release_at`은 API 응답이나 API 본문 데이터의 별도 필드로 저장·생성하지 않는다.
3. API 레코드는 한 시점의 전체 상태를 담은 스냅샷으로 본다.
4. ID·날짜·문자열·상태·NULL을 정규화한 결과에 도메인·중복·조직 관계 검증을 수행한다.
5. 검증을 통과한 결과만 Silver에 저장한다.
6. 오류나 판단 불가 값이 있으면 레코드 전체를 검토 대상으로 보낸다.
7. 서로 다른 배치의 필드를 섞어 새로운 값을 만들지 않는다.
8. Bronze는 수정하지 않으며, 도메인 검증을 통과한 결과만 Silver와 Gold에 반영한다.
9. Bronze 저장 후 항목 수·페이지·cursor·원본 값·해시·`run_id`를 확인한 뒤 정규화를 시작한다.
10. API 수집 소스는 Bronze까지만 처리하고, Silver 정규화·검증 소스는 Bronze를 읽어 별도로 실행한다.

## 2. 원본 필드 계약

```text
record_id, scheduled_release_at,
area_no, area_nm, p_area_no, p_area_nm, top_area_no,
top_area_nm, top_area_lvl, mgr_no, mgr_nm, mgr_dept_nm,
mgr_pos_nm, mgr_hire_dtm, mgr_act_yn, area_reg_dtm,
top_area_reg_dtm
```

- 논리 원본 필드 목록은 위 17개로 관리한다. API 본문 데이터의 업무 필드 15개는 필수이고, `record_id`, `scheduled_release_at`은 선택 응답 메타데이터다.
- 선택 응답 메타데이터가 없으면 해당 필드를 만들지 않는다. `record_id`가 없을 때는 MongoDB 내부 식별자(`_id`, 문서상 `bronze_id`)로 원본을 추적한다.
- 문서 호환상 `release_at` = `scheduled_release_at`으로 표기할 수 있지만, `release_at`을 API 응답이나 API 본문 데이터에 추가·추정하지 않는다.
- `source_row_no`, `source_record_sha256`, `release_slot` 등 API 응답 메타데이터는 원문 보존을 위해 함께 저장한다.
- API 본문 데이터의 업무 필드 15개는 `string` 또는 `null`만 허용한다.
- `record_id`, `scheduled_release_at`의 타입·의미는 API 계약으로 확인하되 도메인·최신성 판단에는 사용하지 않는다.
- API 본문 데이터가 객체가 아니거나 업무 필드 15개가 누락·추가되면 `MAPPING_PAYLOAD_SCHEMA_MISMATCH`, 값이 문자열/NULL이 아니면 `MAPPING_PAYLOAD_TYPE_INVALID`로 검토 큐에 보낸다. Bronze 원문은 유지한다.
- 파이프라인이 생성하는 `run_id`(배치 ID)는 Bronze 원본 문서 최상위 메타데이터로 저장한다. `run_id`, 오류 코드, 규칙 버전은 API 본문 데이터의 업무 필드에 추가하지 않는다.

## 3. 처리 흐름

```text
API 호출 → 계약 검사 → 응답 메타데이터·본문 데이터 전체 Bronze 저장
                              ↓
                     15개 필드 정규화
                              ↓
                도메인·중복·관계 검증
                       ├─ 통과 → Silver → Gold
                       └─ 실패 → hr_review_queue
```

정규화 또는 검증에 실패한 데이터는 별도 중간 컬렉션에 저장하지 않고 바로 `hr_review_queue`에 보관한다. 검토가 끝나면 Silver나 Gold를 직접 고치지 않고 규칙 또는 원천 데이터를 수정한 뒤 Bronze에서 다시 처리한다.

## 4. 공통 정규화

| 대상 | 자동 처리 |
|---|---|
| 문자열 | Unicode NFKC, 전각 공백 변환, 앞뒤·연속 공백 정리 |
| 조직·담당자 ID | 영문 대문자화, 공백·`-`·`_` 제거, 패턴 검사 |
| 날짜 | 승인된 형식만 해석, 시간대가 없으면 `Asia/Seoul` 적용 |
| 상태 | YAML 별칭을 `Y` 또는 `N`으로 변환 |
| NULL | 빈 문자열, `없음`, `-`, `null`, `n/a`를 필드별 정책에 따라 변환 |

위 형식 변환 후 도메인 허용 목록 확인과 중복·조직 관계 검증을 수행한다.

단어 내부 공백은 기준정보와 정확히 일치할 때만 제거한다. 근거가 없으면 `AMBIGUOUS_INTERNAL_SPACE`로 검토한다.

### 4.1 ID 규칙

| 표준값 | 규칙 | 예시 |
|---|---|---|
| `ORG_ID` | `^BIZ[0-9]{5}$` | `BIZ-31536` → `BIZ31536` |
| `EMPLOYEE_ID` | `^EMP[0-9]{6}$` | `emp 000416` → `EMP000416` |

패턴이 맞지 않으면 각각 `INVALID_ORG_ID`, `INVALID_EMPLOYEE_ID`다.

### 4.2 날짜 규칙

허용 형식은 YAML로 관리한다.

```text
%Y-%m-%d %H:%M:%S
%Y-%m-%dT%H:%M:%S
%Y-%m-%d %H:%M:%S.%f
%Y-%m-%dT%H:%M:%S.%f
%Y. %m. %d. %H:%M:%S
%Y.%m.%d %H:%M:%S
%Y/%m/%d %H:%M:%S
%Y%m%d%H%M%S
```

존재하지 않는 날짜, 범위 오류, 예정 입사일은 검토 대상으로 보낸다. `9999-99-99 99:99:99`는 NULL이 아니라 `INVALID_DATETIME_FORMAT`이다.

## 5. 허용 값과 오류 표현

### 5.1 재직 상태

| 원본 | Silver |
|---|---|
| `사용`, `재직`, `YES`, `Y`, `y`, `1` | `Y` |
| `퇴직`, `NO`, `N`, `n`, `0`, `미사용` | `N` |

`UNKNOWN`과 등록되지 않은 값은 `UNKNOWN_ACTIVE_STATUS`다.

### 5.2 조직 레벨

조직 레벨은 다음 별칭을 모두 표준값 `TOP_LEVEL`로 변환한다.

```text
TOP_LEVEL, TOP LEVEL, top_level, 최상위, 1, L1 → TOP_LEVEL
```

현재 허용 목록은 `TOP_LEVEL` 하나다. `UNKNOWN` 등 목록에 없는 값은
`UNREGISTERED_ORGANIZATION_LEVEL`로 검토 큐에 보낸다.

### 5.3 필드별 특수값

| 필드 | 특수값 | 처리 |
|---|---|---|
| `mgr_nm` | `오류값`, `UNKNOWN`, 빈 값 | 필수 이름 오류, 검토 |
| `mgr_dept_nm`, `mgr_pos_nm` | `미상`, `N/A` | NULL 또는 `WARNING` |
| 날짜 필드 | 존재하지 않는 날짜 | 날짜 오류, 검토 |
| 조직·직급 | `기타`, `기타팀` | 실제 기준정보에 없을 때만 검토 |
| `mgr_no` | `UNKNOWN`, 빈 값 | 담당자 식별 불가, 매핑 금지 |

### 5.4 조직 계층

- `parent_area_id`는 바로 위 부모 부서 ID이고, `top_area_id`는 최상위 부서 ID다.
- `area_id == top_area_id`이면 최상위 부서, `area_id != top_area_id`이면 하위 부서로 계산한다.
- 두 관계는 모두 보존하며 조직 계층은 2단계로 고정하지 않는다. 부모 부서가 추가·변경되면 관계를 다시 검증한다.
- 최상위 부서는 부모 부서가 없거나 자기 자신을 부모로 지정해도 정상이다.
- 하위 부서는 존재하는 다른 부서를 부모로 지정해야 한다. 부모가 없거나 자기 자신을 지정하거나 부서 연결이 순환하면 검토 큐로 보낸다.
- 같은 `area_id`의 부서명·상위 부서 정보 변경 또는 `area_id` 변경은 변경 후보로 분류한다. 관련 코드와 명칭이 함께 일관되게 바뀌면 이동으로 갱신하고, 근거가 부족하면 검토 큐에 보관한다.

## 6. 같은 ID가 새 배치로 들어오는 경우

`run_id`는 실행 추적용일 뿐 버전이 아니다. API는 버전 필드나 정렬 기준을 제공하지 않으므로
`run_id`, `record_id`, `scheduled_release_at`으로 최신값을 추정하지 않는다.

| 상황 | 처리 |
|---|---|
| 같은 API 본문 데이터 재수신 | 중복 기록만 남기고 중복 반영하지 않음 |
| 같은 업무 ID인데 API 본문 데이터가 다름 | `SOURCE_RECORD_CONFLICT`, 검토 |
| 어느 값이 최신인지 판단 불가 | Bronze 보존, Silver·Gold 자동 갱신 금지 |
| 담당자 승인 후 수정값 존재 | Bronze 복사본에 수정값을 적용해 재처리 |

서로 다른 배치의 필드를 임의로 섞지 않는다. 검토 승인 후 재처리한 결과만 Silver에 저장한다.

## 7. 중복·충돌 처리

| 코드 | 의미 | 결과 |
|---|---|---|
| `DUPLICATE_RESPONSE` | 같은 요청·페이지·응답 해시 재수신 | 중복 반영 제외 |
| `DUPLICATE_EXACT` | API 본문 데이터 15개와 존재하는 응답 메타데이터 값이 모두 동일 | 한 건만 처리 |
| `NORMALIZATION_EQUIVALENT` | 표기만 다르고 표준 결과가 같음 | Silver 한 건, 이력 연결 |
| `NORMALIZATION_COLLISION` | 어느 값이 맞는지 판단할 수 없는 속성 충돌 | 검토 |
| `AREA_MANAGER_CONFLICT` | 한 조직에 현재 담당자가 둘 이상 | 검토 |

## 8. 상태와 검토

| 상태 | 의미 | Silver·Gold |
|---|---|---|
| `PASS` | 필수 규칙 통과 | 반영 가능 |
| `WARNING` | 사용 가능하지만 기록 필요 | 반영 가능 |
| `REVIEW_REQUIRED` | 업무 판단 필요 | 반영 금지 |
| `ERROR` | 자동 처리 불가 | 반영 금지 |
| `REJECTED` | 이번 실행에서 제외 | 원본만 보존 |

`REVIEW_REQUIRED` 또는 `ERROR`가 하나라도 있으면 레코드 전체를 `hr_review_queue`에 보관한다. 오류 코드와 실패 단계를 함께 기록한다. 검토 결과와 Silver·Gold 연결은 후속 계보 연동 단계에서 `hr_lineage_links`에 기록한다.
검토 큐에는 실패 단계도 `failure_stage`로 기록한다.

### 8.1 정규화·검증 상태

| 상태 | 의미 | 다음 단계 |
|---|---|---|
| `PASS` | 정규화와 도메인·중복 검증 통과 | Silver 저장 |
| `REVIEW_REQUIRED` | 정규화 또는 도메인 판단 필요 | 검토 큐 |
| `ERROR` | 자동 처리 불가 | 검토 큐 |

### 8.2 검토 큐 실패 단계

`hr_review_queue`는 하나의 컬렉션으로 유지하고, `failure_stage`로 실패 지점을 구분한다.

| `failure_stage` | 의미 | 처리 결과 |
|---|---|---|
| `NORMALIZATION` | 매핑·ID·날짜 등 형식 표준화 실패 | Silver에 저장하지 않음 |
| `CANDIDATE_VALIDATION` | 메모리상의 정규화 결과에 대한 도메인·중복·조직 관계 검증 실패 | Silver에 저장하지 않음 |
| `UNKNOWN` | 이전 문서 등 단계 정보가 없는 검토 건 | 원본 연결 후 확인 |

두 유형 모두 `bronze_id`, 오류 코드와 함께 저장한다. Bronze 원본은 삭제하지 않으며,
재처리는 Bronze에서 다시 시작한다.

## 9. YAML과 Python의 역할

### YAML로 관리

- 허용 원본 필드와 타입
- NULL 토큰과 필드별 오류 표현
- ID 패턴과 날짜 형식
- 상태·조직 레벨 별칭

### Python으로 구현

- 중복·충돌 판정
- 조직 계층 순환·FK 검사
- 검토·계보 연결

도메인 검증은 메모리에서 정규화된 15개 필드에 적용한다. `review.json`은 검토 자료이며 YAML을 자동으로 수정하지 않는다. 담당자가 반복되는 정상 값을 확인한 경우에만 YAML을 수동 수정하고, 규칙 버전을 올린 뒤 Bronze에서 다시 처리한다.

### 9.1 검토 후 재처리

1. `review.json`에서 오류 필드와 원본 값을 확인한다.
2. 담당자가 승인·반려 결과와 검토 메모를 기록한다.
3. 반복되는 정상 값이면 `domains.yaml`을 수동 수정하고 규칙 버전을 `normalization-v1.2`에서 다음 버전으로 올린다.
4. 예외 한 건의 수정 내용은 검토 메모로 기록하며 YAML 허용 목록에는 추가하지 않는다.
5. Bronze 원본에서 정규화·도메인 검증을 재실행한다.
6. 검증을 통과한 결과만 Silver에 저장하고, 다시 실패한 데이터는 Review Queue에 유지한다.
7. 담당자·시각·결정·메모는 검토 이력으로 남긴다.

값 목록이나 날짜 형식 추가는 YAML만 수정하고, 새로운 알고리즘이 필요할 때만 Python 모듈과 테스트를 추가한다.

## 10. 완료 기준

- 논리 원본 목록 17개 중 필수 API 본문 데이터 15개와 선택 응답 메타데이터의 처리 방식이 일관된다.
- 정규화·도메인 검증 결과가 Silver 또는 Review Queue로 구분된다.
- 오류·판단 불가 데이터가 승인 전에 Silver·Gold에 들어가지 않는다.
- 동일 ID API 본문 데이터 충돌을 검토 대상으로 구분한다.
- 서로 다른 배치의 필드를 섞은 Silver 레코드가 없다.
- 사용한 정규화 규칙 버전과 재처리 결과를 실행 이력에서 확인할 수 있다.
