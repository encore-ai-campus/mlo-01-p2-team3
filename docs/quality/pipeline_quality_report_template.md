# 파이프라인 품질 보고서 양식

## 1. 실행 정보

| 항목 | 값 |
|---|---|
| run_id |  |
| Silver 처리 시각 |  |
| Gold load_batch_id |  |
| 정규화 규칙 버전 |  |
| Gold 규칙 버전 |  |
| 실행 상태 |  |
| Gold 시작 시각 |  |
| Gold 종료 시각 |  |
| Silver 입력 건수 |  |
| Gold 제외 건수 |  |
| Gold 계보 연결 건수 |  |

## 2. 계층별 건수

| 구간 | 건수 |
|---|---:|
| API 응답 원본 항목 |  |
| Bronze 저장 |  |
| Silver 저장 |  |
| Review Queue |  |
| Gold hr_area |  |
| Gold hr_manager |  |
| Gold hr_area_manager_assignment |  |
| Gold area_manager_features |  |

Bronze = Silver + Review Queue가 되지 않는 경우에는 중복·재처리·페이지 실패 등 사유를 적는다.
Bronze 원문과 검토 큐는 Gold 적재 여부와 관계없이 보존한다.

## 3. 오류·경고

| 구분 | 건수 | 대표 코드 | 처리 |
|---|---:|---|---|
| 매핑·정규화 실패 |  |  | Review Queue |
| 도메인·조직 관계 실패 |  |  | Review Queue |
| Silver 경고 |  |  | Silver 저장 후 기록 |
| Gold 품질 게이트 실패 |  |  | Gold 제외 또는 partial |

failure_stage는 NORMALIZATION, CANDIDATE_VALIDATION,
QUALITY_WARNING, UNKNOWN 중 하나로 기록한다.

## 4. 무결성 확인

- [ ] API 응답 데이터 건수와 Bronze 저장 건수를 비교했다.
- [ ] 페이지 cursor 누락·반복이 없다.
- [ ] Bronze 원문 파일과 manifest의 수량·크기·SHA-256이 일치한다.
- [ ] Bronze 문서와 run_id 연결이 모두 확인된다.
- [ ] Silver 문서가 15개 표준 필드를 사용한다.
- [ ] 검토 큐 문서가 Silver에 중복 저장되지 않았다.
- [ ] 같은 `area_id`·`manager_id`의 유일값 보완 건수와 보완 필드를 기록했다.
- [ ] 같은 원문·단계의 `PENDING_REVIEW` 중복이 없다.
- [ ] Gold PK·FK·조직 계층 검증을 통과했다.
- [ ] Gold 적재 건수와 hr_gold_load_batch의 loaded_count가 일치한다.
- [ ] `hr_gold_load_batch`의 시작 시각 기준으로 최신 성공 배치를 확인했다.
- [ ] `hr_gold_load_batch.report_json`에서 테이블별 처리·제외 건수를 확인했다.
- [ ] `hr_lineage_links`에 Gold `load_batch_id`와 Gold 키가 연결되어 있다.

## 5. Django 출력 확인

- [ ] 조직 화면에서 조직코드와 조직명이 별도 열로 보인다.
- [ ] 담당자 화면에서 담당자코드와 담당자명이 별도 열로 보인다.
- [ ] 검색 CSV가 검색 결과만 포함한다.
- [ ] 전체 CSV 주소에 all=1을 사용하면 전체 Gold 결과가 포함된다.
- [ ] 이름 공백·반복 단어 정리는 화면과 CSV에만 적용된다.
- [ ] 표시 정리로 ID나 행 수가 바뀌지 않는다.

## 6. 판정

| 판정 | 기준 |
|---|---|
| PASS | 원문 무결성, Silver 검증, Gold 품질 게이트를 모두 통과 |
| PASS_WITH_QUARANTINE | 정상 행은 반영하고 실패 행은 Review Queue에 보관 |
| REVIEW_REQUIRED | 수량 차이·충돌·관계 오류의 원인 확인 필요 |
| FAILED | 원문 누락, 저장 실패, PK/FK 실패 등 재실행 필요 |

## 7. 첨부 위치

- Silver 자동 보고서: reports/silver_quality_latest.json
- Bronze 원문 검사 보고서: reports/bronze_archive
- 검토 큐 내보내기: reports/review.json
- 실행 로그: logs/bronze.log, logs/silver.log, logs/gold.log, logs/scheduler.log
