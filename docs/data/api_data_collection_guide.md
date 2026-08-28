# API 데이터 수집 기준

## 1. 목적

레거시 인사·조직 API를 호출해 API 응답 메타데이터와 API 본문 데이터를 포함한 원문 전체를 Bronze에 저장한다.
원본 계약의 논리 필드 목록은 17개로 관리하며, 정규화 규칙은 [데이터 정규화 및 도메인 규칙](./data_normalization_and_domain.md)을 따른다.

## 2. 수집 기준

| 항목 | 기준 |
|---|---|
| 제공처 | `legacy_hr_api` |
| 방식 | 내부 HTTP(S) API 호출 |
| 주기 | 운영 환경 확정 후 결정 |
| 응답 | JSON 객체 또는 배열 |
| 필드 | 논리 목록 17개(필수 API 본문 데이터 15개 + 선택 응답 메타데이터 2개) |
| 페이지 | 페이지 번호 또는 커서 |
| 증분 | API가 제공하는 cursor. API 버전 필드는 사용하지 않음 |
| 저장 | API → Bronze (이후 정규화·검증 → Silver → Gold) |

인증정보·API 주소·호출 한도는 환경 설정과 비밀 저장소에서 관리한다.

## 3. 원본 필드

`record_id`와 `scheduled_release_at`은 API 호출 시 함께 올 수 있는 선택 응답 메타데이터이고, 나머지 15개 업무 필드는
API 본문 데이터에 있다. 응답 메타데이터가 없더라도 Bronze 수집을 실패로 처리하지 않는다. 값이 있을 때만 그대로 저장하고,
없으면 필드를 만들지 않는다. 문서 호환상 `release_at` = `scheduled_release_at`으로 표기할 수 있지만,
`release_at`은 API 응답이나 API 본문 데이터의 별도 필드로 저장·생성하지 않는다.
제공된 응답 메타데이터와 API가 제공하는 추가 메타데이터는 원문 재현을 위해 함께 보관한다.
파이프라인이 발급한 `run_id`도 Bronze 원본 문서 최상위에 저장한다. 이는 API 본문 데이터의 업무 필드가 아닌 수집 추적 메타데이터다.

```text
record_id, scheduled_release_at,
area_no, area_nm, p_area_no, p_area_nm, top_area_no,
top_area_nm, top_area_lvl, mgr_no, mgr_nm, mgr_dept_nm,
mgr_pos_nm, mgr_hire_dtm, mgr_act_yn, area_reg_dtm,
top_area_reg_dtm
```

- 원본 항목 자체가 객체가 아니면 페이지 오류로 처리한다.
- `record_id` 또는 `scheduled_release_at` 누락은 오류가 아니다. `record_id`가 없을 때는 MongoDB 내부 식별자(`_id`, 문서에서는 `bronze_id`로 참조)로 추적한다.
- API 본문 데이터가 객체가 아니거나 업무 필드 15개가 누락·추가되면 Bronze 원문은 보존하고 매핑 단계에서 `MAPPING_PAYLOAD_SCHEMA_MISMATCH`로 검토 큐에 보낸다.
- 본문 데이터 값이 문자열/NULL이 아니면 `MAPPING_PAYLOAD_TYPE_INVALID`로 검토 큐에 보낸다.
- 원본 필드와 API 응답 메타데이터는 변환하거나 삭제하지 않는다.
- 응답 메타데이터는 업무 컬럼을 추가한 것으로 보지 않으며 Silver 표준 필드에도 넣지 않는다.
- API 레코드는 전체 상태 스냅샷으로 처리한다.

## 4. 간단한 수집 흐름

```text
스케줄러 → API 호출 → 페이지·계약 검사 → Bronze 저장
                                  └─ 실패: 페이지 격리·알림
```

이 문서의 수집 범위는 Bronze까지다. Bronze 저장 후 별도 단계에서 API 본문 데이터의 15개
업무 필드를 정규화하고, 도메인·중복·관계 검증을 통과한 데이터만 Silver로 보낸다.

### Bronze 무결성 확인

Bronze 저장이 끝나면 다음 항목을 확인한다.

- API 페이지의 원본 항목 수와 Bronze 저장 건수가 일치하는지 확인한다.
- 페이지·cursor가 누락되거나 같은 페이지가 반복되지 않았는지 확인한다.
- 원본 필드명·값이 저장 중 바뀌지 않았는지 확인한다.
- `run_id`와 원본 해시(`source_record_sha256`가 있으면 해당 값)로 각 문서를 추적한다.
- 저장 실패·누락·동일 응답 재수신 건수를 `hr_pipeline_runs` 또는 `hr_pipeline_pages`에 기록한다.

설명되지 않은 차이가 있으면 해당 실행을 실패로 표시하고 원본은 보존한다. 이 수집 소스에서는
Silver 정규화나 도메인 검증을 수행하지 않으며, 별도 Silver 실행이 Bronze를 읽어 처리한다.

### 호출 제어

- `records` 요청의 `limit`은 항상 `1000`으로 고정한다. 환경변수 값이 더 커도 사용하지 않는다.
- 첫 요청에는 cursor를 넣지 않고, 다음 요청부터 응답의 `next_cursor`를 그대로 전달한다.
- API 본문 데이터 목록이 비어도 cursor를 버리지 않는다. `next_refresh_at` 이후 같은 cursor로 다시 요청한다.
- 실행할 때마다 `/public/v1/key`를 호출해 새 API 키를 사용한다.
- `429`, `408`, `5xx`, 네트워크 오류는 최대 3회 재시도한다.
- `400`, `401`, `403`, 스키마 오류는 재시도하지 않고 실패 처리한다.
- 페이지·커서 반복, 누락, 무한 순환을 검사한다.
- 페이지 저장이 성공할 때마다 다음 cursor를 `hr_pipeline_control`에 저장한다.
- 오류가 나면 실패한 페이지의 cursor를 유지하고, 다음 실행에서 같은 cursor부터 다시 요청한다.

## 5. 배치와 동일 ID 처리

`run_id`는 한 번의 API 실행을 식별하는 배치 ID일 뿐 버전이 아니다. API는 버전 필드나 정렬 기준을 제공하지 않으므로 최신값을 추정하지 않는다.

| 상황 | 처리 |
|---|---|
| 같은 요청·응답 해시 재수신 | 중복 반영하지 않고 이력 기록 |
| 같은 `record_id`에 다른 API 본문 데이터 | 원천 충돌 격리 |
| 같은 업무 ID의 다른 API 본문 데이터 | `SOURCE_RECORD_CONFLICT`, 검토 격리 |
| 어느 값이 최신인지 판단 불가 | Bronze 보존, Silver·Gold 자동 갱신 금지 |
| 담당자 승인 후 수정값 존재 | Bronze 복사본으로 재처리 |

`record_id`와 `scheduled_release_at`도 최신성 판단에 사용하지 않는다.

## 6. 제어 정보

Bronze에 보존하는 API 응답 메타데이터·`run_id`와, 파이프라인 실행 상태를 구분한다.

| 정보 | 저장 위치 |
|---|---|
| 배치 ID(`run_id`) | Bronze 원본 문서 최상위 및 `hr_pipeline_runs` |
| 실행 시각, 상태, 증분 구간 | `hr_pipeline_runs` |
| 현재 cursor, `next_refresh_at` | `hr_pipeline_control` |
| 페이지 순서, 커서 해시, 응답 해시 | `hr_pipeline_pages` |
| 오류·검토 상태 | `hr_review_queue` |
| Bronze–Silver–Gold 연결과 검토·반영 근거(후속 연동 단계) | `hr_lineage_links` |

페이지 실행 이력에는 HTTP 상태, 요청 시각, 응답 시각, 지연 시간, 응답 해시와 오류 코드를 저장한다.
응답 본문과 API 키는 파일 로그나 페이지 이력에 저장하지 않는다.

## 7. 수집 완료 기준

- 정상 레코드가 API 본문 데이터 안의 15개 업무 필드를 가지며, 선택 응답 메타데이터는 있을 때만 원문 그대로 보존된다.
- 페이지 계약 오류가 정상 Bronze에 섞이지 않는다.
- 동일 응답을 다시 받아도 중복 반영되지 않는다.
- 배치·페이지·응답 해시를 조회할 수 있다.
- 실패한 페이지의 cursor는 건너뛰지 않으며, 마지막 성공 페이지 상태를 유지한다.
