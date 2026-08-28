# HR 데이터 파이프라인 실행 매뉴얼

이 문서는 현재 프로젝트를 실행하고, API 수집·Silver 처리·검토·스케줄러를 운영하는 방법만 정리한다.

## 1. 프로젝트 위치

```powershell
cd C:\Users\USER\Documents\Codex\hr_project
```

모든 명령은 위 프로젝트 루트에서 실행한다.

## 2. 최초 준비

### Python 패키지 설치

```powershell
py -m pip install -r requirements.txt
py --version
```

### 환경변수 확인

파일:

```text
C:\Users\USER\Documents\Codex\hr_project\.env
```

```env
MONGO_URI=mongodb://127.0.0.1:27017
MONGO_DATABASE=hr_pipeline
MONGO_SERVER_SELECTION_TIMEOUT_MS=3000
API_BASE_URL=http://192.168.0.51:8000
API_TIMEOUT_SECONDS=10
API_PAGE_LIMIT=1000
```

`.env`와 비밀번호 등 비밀값은 Git에 올리지 않는다. API 키는 실행 시 API에서 조회한다.

### MongoDB 확인

```powershell
mongosh "mongodb://127.0.0.1:27017" --eval "db.runCommand({ ping: 1 })"
```

`{ ok: 1 }`이면 연결된 상태다.

## 3. 작업 순서

```text
1. 자동화 테스트
2. API → Bronze 원본 수집
3. Bronze → 정규화·검증 → Silver 또는 Review Queue
4. Review Queue 확인
5. 필요하면 YAML 수정 후 재검증
6. 30분 스케줄러 실행
```

현재 구현 기준의 최종 구조는 다음과 같다. 정규화 결과는 별도 컬렉션에 저장하지 않는다.

```text
Bronze → 매핑·정규화 → 도메인·중복 검증 → Silver → Gold
                    └─ 실패 → Review Queue
```

## 4. 테스트

```powershell
py -m pytest -q tests\data_pipeline
```

예상 결과:

```text
N passed
```

실제 결과의 `passed` 숫자가 모두 통과했는지 확인한다.

## 5. API 원본 수집

```powershell
py scripts\run_pipeline.py
```

수집 규칙:

- `records`의 `limit`은 항상 1,000이다.
- 첫 요청에는 cursor를 넣지 않는다.
- 다음 요청은 응답의 `next_cursor`를 그대로 사용한다.
- `items`가 비어도 cursor를 유지한다.
- `next_refresh_at` 전에는 API를 다시 호출하지 않는다.
- 공개 시각이 되면 같은 cursor로 다시 호출한다.
- API 키는 실행마다 새로 조회한다.
- API item 원문은 Bronze에 저장한다.
- 실행마다 발급한 `run_id`는 Bronze item 최상위와 `hr_pipeline_runs`에 저장한다. `payload`에는 넣지 않는다.
- `record_id`, `scheduled_release_at`은 있으면 저장하고 없어도 수집을 실패시키지 않는다. `record_id`가 없으면 MongoDB 내부 식별자(`_id`)로 추적한다.
- payload 업무 필드 15개 누락·추가·타입 오류는 Bronze 원문을 보존한 뒤 매핑 단계에서 검토 큐로 보낸다.

주요 API 경로:

```text
GET /public/v1/key
GET /api/v1/meta
GET /api/v1/records
GET /api/v1/records/{id}
GET /health/ready
```

수집 결과는 `hr_bronze_raw_records`에 저장된다. cursor와 `next_refresh_at`은
`hr_pipeline_control`에, 페이지 처리 이력은 `hr_pipeline_pages`에 저장된다.

## 6. Silver 처리

### 저장 없이 확인

```powershell
py scripts\run_silver_once.py --limit 1
```

Bronze 전체를 저장 없이 점검하려면:

```powershell
py scripts\run_silver_once.py --all
```

예상 출력:

```text
정제된 건수: N
정제되지 않은 건수: M
```

### 최초 Silver 전체 저장

전체 결과를 확인한 뒤에만 실행한다.

```powershell
py scripts\run_silver_once.py --limit <전체건수> --write
```

`--all --write`는 사용할 수 없다. 검증 실패 데이터는 Silver에 저장하지 않고
`hr_review_queue`에 보관한다.

## 7. 검토 대상 확인과 재처리

### 검토 대상 출력

```powershell
py scripts\review_queue.py export
```

결과 파일:

```text
reports/review.json
```

### 승인·반려 기록

```powershell
py scripts\review_queue.py approve --id <MongoID> --reviewer 홍길동 --note "정상 값 확인"
py scripts\review_queue.py reject --id <MongoID> --reviewer 홍길동 --note "원본 확인 불가"
```

수정값을 함께 승인할 때는 원본 payload 필드명 또는 Silver 필드명을 JSON으로 전달한다.

```powershell
py scripts\review_queue.py approve --id <MongoID> --reviewer 홍길동 --corrected-json '{"top_area_lvl":"TOP_LEVEL"}'
```

### 규칙 수정 후 재처리

1. `reports/review.json`에서 오류 필드와 값을 확인한다.
2. 반복되는 정상 값이면 담당자가 `src/data_pipeline/rules/domains.yaml`을 수정한다.
3. 필요한 경우 정규화 규칙 YAML을 수정하고 테스트를 실행한다.
4. 승인된 검토 건을 저장 없이 재처리해 결과를 확인한다.
5. 확인 후 `--write`를 붙여 통과한 데이터만 Silver에 저장한다.
6. 재처리에 실패하면 Review Queue에 결과와 사유를 남긴다.

```powershell
py scripts\review_queue.py reprocess --id <MongoID>
py scripts\review_queue.py reprocess --id <MongoID> --write
```

YAML은 자동으로 수정하지 않는다. Bronze 원본도 직접 수정하지 않는다.

## 8. 30분 스케줄러

백그라운드 실행:

```powershell
Start-Process -FilePath "powershell.exe" -WindowStyle Hidden -ArgumentList @(
    "-NoProfile",
    "-ExecutionPolicy", "Bypass",
    "-File",
    "C:\Users\USER\Documents\Codex\hr_project\scripts\run_pipeline_30m.ps1"
)
```

동작 순서:

```text
30분마다 API 수집
      ↓
Bronze 새 저장 건수 N 확인
      ↓
run_silver_once.py --latest N --write
      ↓
로그 저장 후 30분 대기
```

기존 Bronze 전체를 매번 처리하지 않는다. 최초 전체 Silver 저장은 수동으로 한 번만 실행하고,
이후 스케줄러는 새로 들어온 Bronze만 처리한다.

로그 확인:

```powershell
Get-Content "C:\Users\USER\Documents\Codex\hr_project\logs\pipeline.log" -Tail 30 -Wait
```

프로세스 확인:

```powershell
Get-CimInstance Win32_Process |
Where-Object { $_.CommandLine -like "*run_pipeline_30m.ps1*" } |
Select-Object ProcessId, CommandLine
```

중지할 때는 확인한 PID만 종료한다.

```powershell
Stop-Process -Id <PID>
```

## 9. MongoDB 결과 확인

Bronze 건수:

```powershell
mongosh "mongodb://127.0.0.1:27017/hr_pipeline" --eval "db.hr_bronze_raw_records.countDocuments()"
```

Silver 건수:

```powershell
mongosh "mongodb://127.0.0.1:27017/hr_pipeline" --eval "db.hr_silver_standard_records.countDocuments()"
```

검토 큐 건수:

```powershell
mongosh "mongodb://127.0.0.1:27017/hr_pipeline" --eval "db.hr_review_queue.countDocuments()"
```

## 10. 로그와 보안

페이지 실행 이력에는 HTTP 상태, 요청 시각, 응답 시각, 지연 시간, 응답 해시,
오류 코드만 저장한다. 응답 본문·API 키·cursor는 파일 로그에 남기지 않는다.

로그 파일:

```text
C:\Users\USER\Documents\Codex\hr_project\logs\pipeline.log
```
