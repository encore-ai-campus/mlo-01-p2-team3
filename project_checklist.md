# 프로젝트 구현 체크리스트

> 기준: 내려받은 `hr_project/` 폴더의 현재 소스와 이 문서 폴더의 설계 문서.
> 상태 기준: `✓`는 소스와 자동 테스트에서 구현을 확인한 항목, `△`는 소스는 있으나
> 실제 API·DB 또는 운영 실행 검증이 남은 항목, `미구현`은 현재 소스에 없는 항목이다.

## 1. 고정 기준

- ✓ API 논리 원본 17개 필드 계약
- ✓ Silver 업무 필드 15개 계약
- ✓ API 응답 한 건 전체와 그 안의 본문 데이터를 원문 그대로 Bronze에 보존
- ✓ `record_id`, `scheduled_release_at`은 선택 원문 메타데이터로 보존하고 Silver 업무 필드에는 넣지 않음
- ✓ `release_at`을 별도 필드로 만들지 않음
- ✓ `run_id`는 실행 추적용으로 사용하며 `record_id`, `scheduled_release_at`과 함께 최신성 판단에 사용하지 않음
- ✓ 오류·판단 불가 데이터는 검토 큐에 보관하고 승인 후 재처리

## 2. 문서 확인

| 완료 | 확인 항목 | 문서 |
|---|---|---|
| ✓ | 프로젝트 목표와 범위 | [BRD](./brd.md), [PRD](./prd.md) |
| ✓ | 원본 필드·표준 필드 | [데이터 필드 사전](./data/data_field_dictionary.md) |
| ✓ | 정규화·도메인·오류 규칙 | [도메인 규칙](./data/data_normalization_and_domain.md) |
| ✓ | API 수집 기준 | [API 수집 기준](./data/api_data_collection_guide.md) |
| ✓ | 저장 구조 | [ERD](./erd.md) |
| ✓ | 파이프라인 | [파이프라인 설계](./architecture/data_pipeline_design.md) |
| ✓ | 품질·검증 | [품질 진단](./quality/data_quality_assessment.md), [검증 계획](./quality/data_validation_plan.md) |
| ✓ | 운영·스케줄 실행 방법 | [운영 런북](./operations_runbook.md), [스케줄러 매뉴얼](./scheduler_run_manual.md) |

## 3. 현재 구현 흐름

```text
API 호출
  → 응답 메타데이터·본문 데이터 계약 검사
  → Bronze MongoDB 원문 + raw JSON/manifest + CSV 보관
  → 메모리 정규화·검증
       ├─ 통과·경고 → Silver
       └─ 실패       → hr_review_queue
  → 메모리 Gold 품질 게이트
  → MySQL Gold(조직·담당자·배정·피처·적재 이력)
  → Django 대시보드
```

## 4. API·Bronze

- ✓ API 주소·인증키·페이지·증분 cursor 설정
- ✓ `limit=1000` 고정
- ✓ 첫 요청 cursor 생략
- ✓ 빈 API 본문 데이터에서도 같은 cursor 유지 및 `next_refresh_at` 이후 재호출
- ✓ 실행마다 당일 API 키 재조회
- ✓ 실행별 `run_id` 발급 및 실행 이력 연결
- ✓ `429`, `408`, `5xx`, 네트워크 오류 자동 재시도
- ✓ Meta의 15개 컬럼과 본문 데이터의 15개 키·문자열/NULL 타입 검사
- ✓ 본문 데이터 누락·초과 필드와 파싱 실패 응답을 원문·실패 사유와 함께 보존
- ✓ API 응답 한 건 전체(응답 메타데이터와 본문 데이터)를 `hr_bronze_raw_records`에 저장
- ✓ 응답·원문 SHA-256과 파일 크기는 manifest에, HTTP 상태·수집 시각·응답 해시는 페이지 이력에 저장
- ✓ 응답 해시만으로 재수신 Bronze 중복 방지
- ✓ `data/bronze/source=.../ingest_date=.../run_id=.../raw/page_*.json`과 `manifest.json` 저장
- ✓ `data/bronze/source=.../ingest_date=.../run_id=.../raw/page_*.csv` 저장
- ✓ 이전 날짜 CSV run 폴더 ZIP 압축
- ✓ 개별 run의 JSON·CSV·manifest 누락, 해시·크기·CSV 행 수 불일치, 고아·임시 파일 검사
- △ 과거 모든 run을 정기적으로 일괄 검사하는 별도 운영 작업(현재는 필요할 때 수동 실행)
- ✓ 한 실행의 API 응답 데이터 누계와 Bronze 저장·중복 제외 누계를 비교
- ✓ 페이지 이력에 HTTP 상태·요청/응답 시각·지연 시간·응답 해시·처리 건수 저장
- ✓ 페이지 이력에 원본 cursor가 아닌 `cursor_hash`, `next_cursor_hash` 저장
- ✓ 실행 이력과 운영 로그에 API 키·HTTP 응답 본문을 저장하지 않음

## 5. Silver 정규화·검증

- ✓ ID·문자열·날짜·상태·NULL 정규화
- ✓ 공백·대소문자·구분 기호와 승인된 별칭 정규화
- ✓ `미사용`을 `N`으로 변환
- ✓ `UNKNOWN`·오류 이름·비정상 날짜를 YAML 정책에 따라 경고 또는 검토 처리
- ✓ 동일 `area_id`·`manager_id`의 유일한 값으로 누락값 보완
- ✓ 실제 값 충돌은 임의 선택하지 않고 검토 큐에 보존
- ✓ Silver 저장 가능한 경고는 Silver 저장 및 `QUALITY_WARNING` 이력으로 기록하고, Silver 차단 오류는 검토 큐로 분리
- ✓ Gold 적재를 막는 경고는 `GOLD_PREFLIGHT` 검토 대상으로 별도 기록
- ✓ Silver 기존 값 충돌·조직 관계·중복 검증
- 미구현: 일관된 부서 이동을 자동 갱신하는 기능(현재는 `SILVER_EXISTING_CONFLICT`로 검토 큐 보관)
- ✓ `--pending --drain`으로 1,000건 단위 전체 미처리 데이터 반복 처리
- ✓ YAML 규칙 버전과 Bronze–Silver 계보 기록
- ✓ 검토 큐 재실행 중복 방지와 승인·반려·재처리 이력 저장

## 6. Gold·Django

- ✓ Silver 승인 데이터에 대한 메모리 Gold 품질 게이트
- ✓ PK·필수값·FK·조직 계층·조직당 담당자 1명 품질 검사
- ✓ `hr_area`, `hr_manager`, `hr_area_manager_assignment`, `area_manager_features` 생성·일괄 upsert
- ✓ `organization_type`을 `area_id == top_area_id` 관계로 `TOP/SUB` 계산
- ✓ Gold 적재 실행 이력 `hr_gold_load_batch` 저장
- ✓ 입력 건수·적재 건수·제외 건수·시작/종료 시각·상세 JSON 보고서 저장
- ✓ `--partial` 실행 시 정상 데이터는 적재하고 차단 데이터는 제외 건수·검토 근거로 남김
- ✓ Gold 성공 후 `hr_lineage_links`에 `load_batch_id`·테이블별 Gold 키를 연결하는 소스 구현
- △ 실제 Gold 적재 후 `hr_lineage_links` 연결 건수 확인
- ✓ 실패 시 트랜잭션을 취소하고 기존 Gold를 유지
- ✓ 증분 미수신만으로 기존 Gold를 삭제하지 않음
- ✓ Django 조직·담당자·조직도·대시보드 화면 구현
- ✓ 검색 결과 CSV와 전체 CSV를 별도 다운로드

## 7. 검증·재처리

- ✓ Gold 키 기준 upsert로 동일 실행 재적재 중복 방지
- ✓ 검토 승인 후 기존 `bronze_id` 원문을 다시 읽고 현재 YAML로 재정제
- ✓ 수정값 적용·Silver 재저장·재처리 이력 기록
- ✓ Silver·Gold 품질 미리보기 및 실행 JSON 보고서 생성
- ✓ `hr_review_queue`, `hr_lineage_links` 조회 기능 확인

## 8. 실제 실행 검증 필요

- △ 실제 API 실행에서 API 항목 수·Bronze MongoDB 건수·manifest 건수 대조
- △ 실패 응답을 이용한 원문 JSON·CSV·manifest 보존 확인
- △ Silver 전체 재처리 후 정제·경고·검토 건수 대조
- △ Gold `--write` 실행 후 네 테이블·계보·배치 이력 확인
- △ Django 화면 집계와 Gold 테이블 건수 대조

## 9. 종료 기준

- ✓ API 원본 17개와 Silver 15개 매핑 확인
- ✓ Gold 품질 게이트에서 승인되지 않은 오류 데이터를 제외
- ✓ `requirements.txt`, `.env.example`, `.gitignore` 확인
