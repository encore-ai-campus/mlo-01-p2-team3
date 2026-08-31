# 실행 매뉴얼

## 1. 목적

이 문서는 현재 프로젝트를 로컬에서 실행하고 결과를 확인하는 방법만 정리한다.
기준 흐름은 다음과 같다.

~~~text
API 호출 → Bronze 원문 저장 → 메모리 정규화·검증
                              ├─ 통과 → Silver → Gold → Django
                              └─ 실패 → Review Queue
~~~

Bronze 원문은 수정·삭제하지 않는다. 정규화 후보를 저장하는 중간 컬렉션도 사용하지 않는다.

## 2. 실행 전 확인

압축을 풀어 만든 `hr_project/` 폴더를 프로젝트 루트로 사용하고, 모든 명령은 그 폴더에서 실행한다.

~~~powershell
Set-Location "<다운로드한 경로>\hr_project"
~~~

- Python 패키지 설치가 완료되어 있어야 한다.
- MongoDB와 MySQL 접속 정보는 환경변수 또는 설정 파일에 준비한다.
- API 키는 파일에 고정하지 않는다. 실행할 때 공개 키 API에서 새로 조회한다.
- 규칙 파일은 프로젝트의 src/data_pipeline/rules 아래에 둔다.

### 2.1 다른 PC에서 처음 설정

소스와 `requirements.txt`, `.env.example`이 함께 있는 `hr_project/` 폴더만 내려받으면 된다.
고정된 사용자 경로나 별도 문서 폴더를 요구하지 않는다.

~~~powershell
Set-Location "<다운로드한 경로>\hr_project"
py -m pip install -r requirements.txt
Copy-Item .env.example .env
~~~

`.env`에 MongoDB·MySQL·API 접속값을 입력한다. `.env`는 Git 제외 대상이며 커밋하지 않는다.
`py` 명령이 없으면 같은 명령에서 `py`를 `python`으로 바꾼다. 스크립트는 자신의 위치를
기준으로 `src`, `scripts`, `logs`, `data` 경로를 계산하므로 압축 해제 위치가 달라도 동작한다.

## 3. 실행 순서

### 3.1 테스트

~~~powershell
py -m pytest -q tests\data_pipeline
~~~

모든 테스트가 N passed로 끝나면 다음 단계로 진행한다.

### 3.2 Bronze 수집

~~~powershell
py scripts\run_pipeline.py
~~~

API 페이지를 호출해 응답 전체를 hr_bronze_raw_records에 저장한다.
실행 결과에는 status, run_id, pages, saved_rows, deduplicated_rows, api_item_count,
next_refresh_at을 출력한다. `deduplicated_rows`는 응답 해시 중복으로 원문은 보관했지만
MongoDB Bronze 업무 문서에 다시 넣지 않은 항목 수다.

원문 파일과 manifest도 run_id별 raw 디렉터리에 보관한다.

~~~text
data/bronze/source=hr_api/ingest_date=YYYY-MM-DD/run_id=<배치 ID>/raw/
~~~

### 3.3 Bronze 보관 검사

~~~powershell
py scripts\verify_bronze_archive.py --run-id <배치 ID>
~~~

원문 JSON·CSV와 manifest의 항목 수, 파일 크기, SHA-256을 확인한다.
누락이나 불일치가 있으면 Bronze 성공으로 보지 않는다.

### 3.4 Silver 정규화·검증

미리보기만 할 때:

~~~powershell
py scripts\run_silver_once.py --pending
~~~

미처리 Bronze를 1,000건씩 모두 Silver에 저장할 때:

~~~powershell
py scripts\run_silver_once.py --pending --drain --write
~~~

정상 데이터와 Silver 저장 가능한 경고 데이터는 hr_silver_standard_records에 저장하고,
경고 이력은 별도로 남긴다. 필수값 누락·실제 충돌 등 차단 데이터는 hr_review_queue에 저장한다.
Silver 실행은 API를 다시 호출하지 않고 Bronze만 읽는다.

### 3.5 Silver 품질 보고서

~~~powershell
py scripts\report_silver_quality.py
~~~

보고서 파일은 reports/silver_quality_latest.json에 생성된다.

### 3.6 Gold 품질 검사와 적재

검사만 할 때:

~~~powershell
py scripts\run_gold_once.py
~~~

전체 품질 게이트를 통과한 경우:

~~~powershell
py scripts\run_gold_once.py --write
~~~

게이트가 실패했지만 정상으로 판정된 테이블 행을 먼저 적재할 경우:

~~~powershell
py scripts\run_gold_once.py --partial --write
~~~

Gold는 Silver를 메모리에서 품질 게이트로 검증한 뒤 MySQL의
hr_area, hr_manager, hr_area_manager_assignment, area_manager_features에
반영한다. 별도 MySQL staging 테이블은 현재 사용하지 않는다.
적재 결과와 제외 건수는 hr_gold_load_batch에 남긴다.

### 3.7 Django 확인

~~~powershell
py src\manage.py check
py src\manage.py runserver 127.0.0.1:8000
~~~

주요 주소:

| 기능 | 주소 |
|---|---|
| 대시보드 | /hrdata/ |
| 조직 목록 | /hrdata/areas/ |
| 조직 트리 | /hrdata/organization-tree/ |
| 담당자 목록 | /hrdata/managers/ |
| 조직 CSV | /hrdata/areas/export.csv |
| 담당자 CSV | /hrdata/managers/export.csv |

전체 CSV는 각 주소 뒤에 ?all=1을 붙인다.
CSV는 조직코드·조직명과 담당자코드·담당자명을 각각 별도 열로 제공한다.

## 4. API 호출 규칙

- records 요청 limit은 항상 1000이다.
- 첫 요청에는 cursor를 넣지 않는다.
- 다음 요청부터 응답의 next_cursor를 그대로 사용한다.
- API 본문 데이터가 비어도 cursor를 버리지 않는다.
- next_refresh_at 이후 같은 cursor로 다시 호출한다.
- 매 실행마다 API 키 조회 주소에서 새 키를 가져온다.
- 페이지 이력에는 HTTP 상태, 요청·응답 시각, 지연 시간, 응답 해시만 저장한다.
- API 키와 응답 본문은 pipeline.log와 hr_pipeline_pages에 저장하지 않는다.
- 응답 본문 원문은 Bronze와 raw 파일에서만 확인한다.

## 5. 5분 스케줄러

현재 PowerShell 스크립트는 한 사이클에 Bronze → Silver → Gold를 순서대로 실행한다.
Silver는 미처리 Bronze를 drain 모드로 이어서 처리한다.
시작·상태 확인·중지 명령만 따로 보려면 [스케줄러 실행 매뉴얼](./scheduler_run_manual.md)을 사용한다.

한 번만 실행:

~~~powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File ".\scripts\run_pipeline_5m.ps1" once
~~~

5분 반복 시작:

~~~powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File ".\scripts\run_pipeline_5m.ps1" start
~~~

상태 확인:

~~~powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File ".\scripts\run_pipeline_5m.ps1" status
~~~

중지:

~~~powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File ".\scripts\run_pipeline_5m.ps1" stop
~~~

실행 중인 사이클은 끝난 뒤 중지 신호를 반영한다. 같은 스크립트를 다시 start해도
실행 중인 스케줄러가 있으면 중복으로 시작하지 않는다.

## 6. 검토 큐 처리

대기 목록을 파일로 내보낸다.

~~~powershell
py scripts\review_queue.py export
~~~

담당자가 승인 또는 반려를 기록한다.

~~~powershell
py scripts\review_queue.py approve --id <검토ID> --reviewer <담당자>
py scripts\review_queue.py reject --id <검토ID> --reviewer <담당자> --note <사유>
~~~

승인된 건만 Bronze에서 다시 정규화·검증한다.

~~~powershell
py scripts\review_queue.py reprocess --id <검토ID> --write
~~~

재처리 결과가 Silver에 저장되면 이후 Gold 검사·적재를 다시 실행한다.
검토 큐의 원본 연결과 결정 이력은 유지한다.

## 7. 결과 확인 위치

| 확인 대상 | 위치 |
|---|---|
| Bronze 원문 | MongoDB hr_bronze_raw_records |
| 페이지 실행 이력 | MongoDB hr_pipeline_pages |
| 배치 결과 | MongoDB hr_pipeline_runs |
| cursor 상태 | MongoDB hr_pipeline_control |
| Silver 정상 데이터 | MongoDB hr_silver_standard_records |
| 실패·검토 데이터 | MongoDB hr_review_queue |
| Bronze–Silver 연결 | MongoDB hr_lineage_links |
| Gold 데이터 | MySQL Gold 테이블 |
| Silver 보고서 | reports/silver_quality_latest.json |
| 원문 보관 보고서 | reports/bronze_archive |
| 로그 | logs/bronze.log, logs/silver.log, logs/gold.log, logs/scheduler.log |

## 8. 중지·재실행 원칙

- 중간에 중지해도 Bronze 원문은 남는다.
- 다음 실행은 저장된 cursor와 미처리 Bronze를 기준으로 이어간다.
- Silver와 Gold는 이미 저장된 키를 다시 덮어써 중복 행을 만들지 않는다.
- 규칙을 바꾼 뒤에는 테스트 → Silver 재처리 → Gold 검사 순서로 확인한다.
