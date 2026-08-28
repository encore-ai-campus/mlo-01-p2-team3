# 데이터 변환 검증 계획

## 1. 검증 범위

```text
API → Bronze → 정규화·검증 → Silver → Gold → Django
```

| 구간 | 검사 내용 |
|---|---|
| API | 인증·HTTP·페이지·재시도 |
| API → Bronze | 원본 문서·본문 데이터 전체·논리 목록 17개·원본값·`run_id` 추적·중복. 선택 응답 메타데이터 누락은 허용 |
| Bronze → 정규화·검증 | 15개 매핑·형식 정규화·규칙 버전 |
| 정규화·검증 → Silver | 도메인·중복·조직 관계 검증 |
| Silver → Gold | PK·FK·조직 관계·건수 |
| Gold → Django | 화면 집계 일치 |

## 2. 사전 조건

- API 주소·인증·페이지·증분 기준값이 설정되어 있다.
- API 레코드가 전체 상태 스냅샷인지 확인되어 있다.
- 정규화 규칙 버전과 제어 저장소가 준비되어 있다.

### 2.1 API 수집 기준

- `records`의 `limit`은 항상 `1000`으로 고정한다.
- 첫 요청에는 cursor를 넣지 않고, 다음 요청부터 `next_cursor`를 그대로 사용한다.
- API 본문 데이터 목록이 비어도 cursor를 유지하고 `next_refresh_at` 이후 같은 cursor로 재요청한다.
- API 키는 매 실행 시 새로 조회한다.

페이지 이력에는 HTTP 상태, 요청 시각, 응답 시각, 지연 시간, 응답 해시와 오류 코드를 저장한다.
응답 본문과 API 키는 저장하지 않는다.

## 3. 핵심 검증 규칙

| 코드 | 검사 | 성공 기준 | 실패 처리 |
|---|---|---|---|
| `API-001` | 인증·HTTP | 허용된 `2xx` | 실행 실패 |
| `PAGE-001` | 페이지·커서 | 누락·반복 0건 | 실행 실패 |
| `SCHEMA-001` | 본문 데이터·응답 메타데이터 계약 | 원본 문서와 본문 데이터 구조 확인. `record_id`, `scheduled_release_at`은 선택 | 원문 보존 또는 페이지 격리 |
| `RECON-001` | API↔Bronze | 차이 0 또는 사유 설명 | 실행 실패 |
| `BRONZE-001` | Bronze 원본 보존 | 원본 항목 수·필드·값·해시·`run_id` 일치 | 실행·페이지 실패 |
| `BRONZE-002` | Bronze 페이지 무결성 | cursor 누락·반복 0건, 실패 사유 기록 | 같은 cursor 재처리 |
| `DUP-001` | 재수신 중복 | 중복 반영 0건 | 중복 기록 |
| `MAP-001` | Bronze↔정규화 결과 | 업무 15개 1:1 | Silver 저장 차단 |
| `CAND-001` | 정규화 결과 형식 | ID·날짜·상태·NULL 정규화 완료 | Silver 저장 차단 |
| `ID-001` | 조직·담당자 ID | 승인 패턴 | 검토 |
| `DATE-001` | 날짜 | 해석·범위 정상 | 검토 |
| `DOMAIN-001` | 정규화 결과 도메인 | 허용값만 존재 | Silver 차단·검토 |
| `CAND-002` | 중복 ID | 동일 실행에서 중복 0건 | 검토 |
| `REVIEW-001` | 검토 단계 구분 | 허용된 `failure_stage` 기록 | 검토 큐 저장 |
| `PK-001` | Gold PK | 중복 0건 | 트랜잭션 취소 |
| `FK-001` | 조직 부모·최상위 관계·순환 | 오류 0건 또는 승인 | Gold 차단 |
| `RECON-002` | Silver↔Gold | 차이 0 또는 사유 설명 | 트랜잭션 취소 |
| `KPI-001` | Gold↔Django | 주요 집계 일치 | 배포 차단 |

### 3.1 조직 관계 검증

- `area_id == top_area_id`이면 최상위 부서로 처리하고 부모 없음·자기 참조를 허용한다.
- `area_id != top_area_id`이면 하위 부서로 처리하고, 존재하는 다른 부서를 부모로 요구한다.
- 하위 부서의 부모 누락·자기 참조·순환 연결은 검토 큐로 보낸다.
- 같은 `area_id`의 명칭·상위 부서 정보 변경이나 `area_id` 변경은 변경 후보로 분류한다.
- 관련 코드·명이 함께 일관되게 변경되면 이동 갱신하고, 그 밖의 변경은 자동 갱신하지 않는다.
- 부모 부서가 추가·변경되면 조직 관계를 다시 검증한다. 계층 깊이는 2단계로 제한하지 않는다.

Bronze 수집과 Silver 정규화·검증은 서로 다른 실행 책임이다. Bronze 검사가 끝난 원본만 Silver 검증이 읽으며,
Silver 검증 실패가 API 호출이나 Bronze 원본 저장을 수정하지 않는다.

## 4. 동일 ID·재처리 기대 결과

| 입력 | 기대 결과 |
|---|---|
| 같은 원본 재수신 | 중복 반영 없음 |
| 형식 정규화가 끝난 레코드 | 도메인·중복 검증 진행 |
| 도메인·중복 검증 통과 | Silver 저장 |
| 도메인·중복 검증 실패 | Review 보관, Silver 차단 |
| 같은 업무 ID의 API 본문 데이터가 다름 | Review 보관, 자동 갱신 금지 |
| 담당자 승인 후 수정값이 있는 레코드 | Bronze에서 재처리 후 통과 시 Silver 저장 |
| 여러 배치에 같은 ID가 존재 | Bronze 보존, 본문 데이터가 다르면 Review |

## 5. 기본 테스트 목록

| 테스트 | 기대 결과 |
|---|---|
| API 항목 수와 Bronze 저장 건수 비교 | 설명되지 않은 차이 0, 차이는 실행 실패로 기록 |
| Bronze 원본 필드·값·해시·`run_id` 확인 | 원본 변경·연결 누락 0건 |
| 페이지 cursor 누락·반복 | 페이지 실패 처리, cursor 유지 |
| API 본문 데이터 업무 필드 누락·추가 | Bronze 보존, 매핑 검토 큐(`MAPPING_PAYLOAD_SCHEMA_MISMATCH`) |
| `record_id`·`scheduled_release_at` 누락 | Bronze 저장, 오류 아님 |
| ID 공백·하이픈·소문자 | 승인 패턴으로 정규화 |
| 비정상 날짜 | Silver·Gold 차단 |
| `미사용` 상태 | `N`으로 변환 |
| `UNKNOWN` 상태·담당자 ID | 검토 격리 |
| 내부 공백이 여러 기준값과 일치 | 자동 수정 금지 |
| 같은 ID의 다른 API 본문 데이터 | `SOURCE_RECORD_CONFLICT`, 검토 격리 |
| 최상위 부서의 부모 없음·자기 참조 | 정상 통과 |
| 하위 부서의 부모 없음·자기 참조·순환 | 검토 큐 격리 |
| 부서 정보 변경 근거가 충분함 | 이동으로 갱신 |
| 부서 정보 변경 근거가 부족함 | 검토 큐 격리 |
| Gold FK·순환 오류 | 트랜잭션 취소 |
| 동일 실행 재실행 | 중복 Gold 없음 |

## 6. 처리 상태

| 상태 | 의미 | 처리 |
|---|---|---|
| `PASS` | 정규화·도메인·중복 검증 통과 | Silver 저장 |
| `WARNING` | 사용 가능, 기록 필요 | 진행 |
| `REVIEW_REQUIRED` | 업무 판단 필요 | Silver·Gold 차단 |
| `ERROR` | 자동 처리 불가 | Bronze 원문을 남기고 검토 큐 보관 |
| `REJECTED` | 이번 실행에서 제외 | 원본 보존 |
| `SCHEMA_MISMATCH` | 페이지 계약 오류 | 페이지 격리 |

## 7. 재실행 순서

1. 실패한 페이지의 cursor를 건너뛰지 않고, 마지막 성공 페이지 상태를 유지한다.
2. 새 `run_id`로 같은 구간을 다시 호출한다.
3. 응답 해시로 재수신 여부를 확인한다.
4. 수정된 규칙과 이전 정상 결과를 비교한다.
5. 형식 정규화 결과에 도메인·중복·관계 검증을 적용한다.
6. 검증을 통과한 경우에만 Silver·Gold와 증분 기준값을 갱신한다.
7. 승인된 검토 문서를 직접 입력하지 않고 Bronze에서 다시 정규화·검증한다.

## 8. 결과 보고서

각 실행은 `hr_pipeline_runs`에 다음 형태로 저장한다.

```json
{
  "run_id": "<배치 ID>",
  "rule_version": "normalization-v1.2",
  "status": "COMPLETED",
  "counts": {
    "pages": 0,
    "saved_rows": 0,
    "http_2xx_count": 0,
    "http_4xx_count": 0,
    "http_5xx_count": 0
  }
}
```

상세 오류는 `hr_review_queue`에서 조회한다. Gold 지표는 해당 단계를 구현한 뒤 실행 보고서에 추가한다.

검토 큐는 하나로 운영하고 `failure_stage`로 실패 단계를 구분한다.

| `failure_stage` | 의미 | 처리 |
|---|---|---|
| `NORMALIZATION` | 매핑·ID·날짜 등 형식 표준화 실패 | Silver에 저장하지 않고 검토 큐 보관 |
| `CANDIDATE_VALIDATION` | 메모리상의 정규화 결과에 대한 도메인·중복·조직 관계 검증 실패 | Silver에 저장하지 않고 검토 큐 보관 |
| `UNKNOWN` | 기존 문서 등 단계 정보가 없음 | 원본 연결 후 확인 |

## 9. 완료 기준

- 논리 원본 목록 17개 중 필수 API 본문 데이터 15개와 선택 응답 메타데이터의 처리 방식이 검증된다.
- 형식 정규화 실패와 도메인 검증 실패가 구분된다.
- 검토 큐의 모든 문서에 `failure_stage`가 기록된다.
- 오류·판단 불가 데이터가 승인 전에 Gold에 들어가지 않는다.
- API→Bronze 원본 무결성 차이와 설명되지 않은 저장 누락이 0건이다.
- 동일 ID 충돌의 자동 반영·필드 혼합·기존 정상값 훼손이 0건이다.

## 10. 실행 명령

프로젝트 루트에서 실행한다.

```powershell
py -m pytest -q tests\data_pipeline
py scripts\run_pipeline.py
py scripts\run_silver_once.py --limit 1
py scripts\run_silver_once.py --all
py scripts\run_silver_once.py --limit <전체건수> --write
```

테스트는 `N passed`로 종료되어야 한다. Silver 확인 명령은 `정제된 건수`와
`정제되지 않은 건수`를 출력한다. `--all --write`는 사용하지 않는다.

운영 스케줄러는 `scripts\run_pipeline_30m.ps1`을 실행할 수 있다. 실행 주기와 방법은
운영 환경 확정 후 결정하며, 새 Bronze 건수만 `run_silver_once.py --latest N --write`로 처리한다.
최초 전체 Silver 저장은 수동으로 실행한다.

## 11. 검토 후 재처리

1. `hr_review_queue`의 오류 필드와 원본 연결 정보를 확인한다.
2. 담당자가 `review_status`, `reviewed_by`, `reviewed_at`, `review_note`를 기록한다.
3. 반복되는 정상 값은 `domains.yaml`에 수동으로 추가하고 규칙 버전을 올린다.
4. 한 건의 예외 수정 내용은 검토 메모로 기록하며 YAML 허용 목록에는 추가하지 않는다.
5. 승인된 수정값을 `corrected_values`로 저장한다.
6. Bronze에서 정규화·도메인·중복·관계 검증을 재실행한다.
7. 통과한 결과만 Silver에 재저장하고, 실패 데이터는 Review Queue에 유지한다.
8. 검토자·결정·수정·재처리 결과를 이력으로 남긴다.
