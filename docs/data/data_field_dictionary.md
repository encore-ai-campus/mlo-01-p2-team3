# 데이터 필드 사전

## 1. 목적

API 응답 메타데이터 2개(선택)와 API 본문 데이터의 업무 필드 15개(필수)를 합친 논리 원본 목록 17개,
정규화 결과와 Silver의 표준 업무 필드 15개의 의미와 대응 관계를 정의한다. 상세 값 규칙은
[데이터 정규화 및 도메인 규칙](./data_normalization_and_domain.md)을 따른다.

## 2. 필드 대응표

| 원본 필드 | Silver 필드 | 의미 | NULL |
|---|---|---|---|
| `record_id` (선택 응답 메타데이터) | — | 원천 레코드 ID. 없으면 MongoDB 내부 식별자(`_id`, 문서상 `bronze_id`)로 추적 | Y |
| `scheduled_release_at` (선택 응답 메타데이터) | — | API 호출 때 함께 전달되는 공개 예정 시각. 없으면 필드를 만들지 않음 | Y |
| `area_no` | `area_id` | 조직 ID | N |
| `area_nm` | `area_name` | 조직명 | N |
| `p_area_no` | `parent_area_id` | 부모 조직 ID | 최상위만 |
| `p_area_nm` | `parent_area_name` | 부모 조직명 | 최상위만 |
| `top_area_no` | `top_area_id` | 최상위 조직 ID | N |
| `top_area_nm` | `top_area_name` | 최상위 조직명 | N |
| `top_area_lvl` | `top_area_level` | 조직 레벨 | N |
| `mgr_no` | `manager_id` | 담당자 ID | N |
| `mgr_nm` | `manager_name` | 담당자명 | N |
| `mgr_dept_nm` | `department_name` | 담당자 부서 | Y |
| `mgr_pos_nm` | `position_name` | 담당자 직급 | Y |
| `mgr_hire_dtm` | `manager_hire_at` | 담당자 입사일시 | Y |
| `mgr_act_yn` | `manager_active_yn` | 재직·활성 상태 | N |
| `area_reg_dtm` | `area_registered_at` | 조직 등록일시 | Y |
| `top_area_reg_dtm` | `top_area_registered_at` | 최상위 조직 등록일시 | Y |

## 3. 표준 타입과 규칙

| 표준 타입 | 규칙 |
|---|---|
| `ORG_ID` | 대문자화·구분자 제거 후 `^BIZ[0-9]{5}$` |
| `EMPLOYEE_ID` | 대문자화·구분자 제거 후 `^EMP[0-9]{6}$` |
| `ORG_LEVEL` | YAML에 등록된 조직 레벨만 허용 |
| `ACTIVE_YN` | `Y` 또는 `N`; `미사용`은 `N` |
| `DATETIME_KST` | 승인 형식 해석, Silver는 BSON Date(UTC) |
| 이름·부서·직급 | Unicode·공백 정리, 내부 공백은 근거가 있을 때만 수정 |

`record_id`, `scheduled_release_at`은 API 호출 시 함께 올 수 있는 선택 응답 메타데이터 원문으로 Bronze에만 보관한다. `release_at`은
문서 호환상 `release_at` = `scheduled_release_at`으로 표기할 수 있지만, `release_at`은 API 응답이나
API 본문 데이터의 별도 필드로 저장·생성하지 않는다.
위 업무 필드는 Bronze에 보존된 API 본문 데이터의 값을 정제한 뒤 도메인·중복·관계 검증을 적용한다. 검증을 통과한 결과만 Silver에 저장한다. 두 응답 메타데이터 값은 도메인 판정이나 최신성 판단에 사용하지 않는다. `run_id`는 Bronze 원본 문서 최상위와 실행 이력에 저장하고, 오류 코드·규칙 버전은 별도 제어 저장소에 둔다.

API 본문 데이터의 업무 필드 15개가 정확히 없거나 값 타입이 문자열/NULL이 아니면 매핑 단계에서 각각
`MAPPING_PAYLOAD_SCHEMA_MISMATCH`, `MAPPING_PAYLOAD_TYPE_INVALID`로 검토 큐에 보낸다. Bronze 원문은 삭제하지 않는다.

Bronze는 이 필드가 포함된 API 본문 데이터와 API 응답 메타데이터를 포함한 원본 문서 전체를 원문 그대로 보관한다.

## 4. 주요 NULL·오류 표현

- 공통 NULL 토큰: 빈 문자열, `없음`, `-`, `null`, `n/a`
- `mgr_no = UNKNOWN`: 담당자 식별 불가로 매핑하지 않음
- `mgr_nm = 오류값`: 필수 이름 오류
- `mgr_dept_nm`, `mgr_pos_nm = 미상`: NULL 또는 경고
- `9999-99-99 99:99:99`: 날짜 오류
- `기타`, `기타팀`: 기준정보 확인 전에는 임의 변환하지 않음

## 5. Gold 대응

정규화 결과와 Silver는 모두 표준 업무 필드 15개를 사용한다. 정규화 결과에 도메인·중복·조직 관계
검증을 적용하고, Silver에는 검증을 통과한 결과만 저장한다.

| Silver 그룹 | Gold 테이블 |
|---|---|
| 조직 필드 | `hr_area` |
| 담당자 필드 | `hr_manager` |
| 조직·담당자 연결 | `hr_area_manager_assignment` |

현재값은 조직 `area_id`, 담당자 `manager_id`, 배정 `area_id`를 기준으로 관리한다. 한 담당자가 여러 조직을 맡는 것은 허용한다.

## 6. 조직 계층 기준

- `parent_area_id`는 바로 위 부모 부서 ID이고, `top_area_id`는 최상위 부서 ID다.
- `area_id == top_area_id`이면 최상위 부서, `area_id != top_area_id`이면 하위 부서로 계산한다.
- 두 관계는 모두 보존하며 조직 계층은 2단계로 고정하지 않는다.
- 최상위 부서는 부모 부서가 없거나 자기 자신을 부모로 지정해도 정상이다.
- 하위 부서는 존재하는 다른 부서를 부모로 지정해야 한다. 부모가 없거나 자기 자신을 지정하거나 순환하면 검토 큐로 보낸다.
- 같은 `area_id`의 부서명·상위 부서 정보 변경 또는 `area_id` 변경은 변경 후보로 분류한다. 관련 코드와 명칭이 함께 일관되게 변경되면 이동으로 갱신하고, 근거가 부족하면 검토 큐에 보관한다.

## 7. 변경 원칙

- 원본 필드명과 개수는 자동으로 바꾸지 않는다.
- 허용 값·날짜 형식·NULL 토큰 추가는 YAML과 테스트 데이터만 수정한다.
- 새로운 중복·충돌 알고리즘은 Python 모듈과 테스트를 추가한다.
- 규칙 버전은 실행 제어 정보에 기록한다.
