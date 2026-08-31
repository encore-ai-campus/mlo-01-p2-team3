# 스케줄러 실행 매뉴얼

## 1. 대상

다운로드한 `hr_project/` 폴더를 기준으로 한 스크립트:

~~~text
hr_project/scripts/run_pipeline_5m.ps1
~~~

이 스크립트는 5분마다 다음 작업을 실행한다.

~~~text
Bronze API 수집
   ↓
미처리 Bronze 전체 Silver 처리
   ↓
Gold 부분 적재
~~~

## 2. 처음 실행하기

PowerShell에서 한 줄씩 실행한다.

~~~powershell
Set-Location "<다운로드한 경로>\hr_project"
~~~

먼저 한 사이클만 확인한다.

~~~powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File ".\scripts\run_pipeline_5m.ps1" once
~~~

정상이라면 Bronze, Silver, Gold 로그에 각각 실행 결과가 남는다.

## 3. 5분 반복 실행

### 현재 창에서 실행

~~~powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File ".\scripts\run_pipeline_5m.ps1" start
~~~

이 창을 닫지 않고 5분 주기로 계속 실행한다.

### 창을 숨겨 실행

~~~powershell
$projectRoot = (Get-Location).Path
$schedulerScript = Join-Path $projectRoot "scripts\run_pipeline_5m.ps1"
Start-Process -FilePath "powershell.exe" -WindowStyle Hidden -ArgumentList @("-NoProfile","-ExecutionPolicy","Bypass","-File",$schedulerScript,"start")
~~~

같은 스케줄러가 이미 실행 중이면 새로 시작하지 않는다.

## 4. 상태 확인과 중지

프로젝트 루트에서 실행한다.

~~~powershell
Set-Location "<다운로드한 경로>\hr_project"
~~~

상태 확인:

~~~powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File ".\scripts\run_pipeline_5m.ps1" status
~~~

정상 출력:

~~~text
Name       : hr_project_scheduler
Status     : RUNNING
ProcessId  : 숫자
~~~

중지:

~~~powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File ".\scripts\run_pipeline_5m.ps1" stop
~~~

중지 명령은 중지 파일을 만들고 현재 작업이 끝난 뒤 종료시킨다.
10초가 지나도 종료되지 않으면 해당 스케줄러 프로세스를 강제 종료한다.

## 5. 로그 위치

프로젝트의 logs 폴더에서 확인한다.

| 파일 | 내용 |
|---|---|
| scheduler.log | 시작·중지·프로세스 상태 |
| bronze.log | API 수집과 Bronze 저장 결과 |
| silver.log | Silver 정규화·검토 결과 |
| gold.log | Gold 품질 검사·적재 결과 |
| pipeline.log | 전체 사이클 시작·종료 |
| bronze_archive.log | 원문 JSON·CSV·manifest 검사 |

로그에는 API 키와 API 응답 본문을 남기지 않는다.

## 6. 실행 결과 해석

| 상황 | 의미 |
|---|---|
| Bronze COMPLETED | API 페이지를 Bronze에 저장 |
| Bronze WAITING_FOR_REFRESH | 다음 공개 시각 전이라 새 페이지가 없음 |
| Silver SUCCEEDED | 미처리 Bronze를 Silver 또는 Review Queue로 처리 |
| Gold SUCCEEDED_WITH_QUARANTINE | 정상 Gold 행은 적재하고 제외 건수는 보고서에 기록 |
| Gold skipped | Silver 실행이 실패해 Gold를 실행하지 않음 |
| ALREADY_RUNNING | 이미 실행 중인 스케줄러가 있음 |
| STOPPED | 현재 실행 중인 스케줄러가 없음 |

Bronze가 WAITING_FOR_REFRESH여도 이전에 쌓인 미처리 Bronze가 있으면 Silver가 처리할 수 있다.

## 7. 재시작 순서

~~~powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File ".\scripts\run_pipeline_5m.ps1" stop
powershell.exe -NoProfile -ExecutionPolicy Bypass -File ".\scripts\run_pipeline_5m.ps1" once
$schedulerScript = (Resolve-Path ".\scripts\run_pipeline_5m.ps1").Path
Start-Process -FilePath "powershell.exe" -WindowStyle Hidden -ArgumentList @("-NoProfile","-ExecutionPolicy","Bypass","-File",$schedulerScript,"start")
~~~

위 명령은 중지 → 한 사이클 확인 → 숨은 반복 실행 순서다.

## 8. Python을 찾지 못할 때

Python 경로가 자동 검색되지 않을 때만 스케줄러를 시작하기 전에 명시한다. PC마다 설치 경로가
다르므로 다른 사람의 경로를 그대로 복사하지 않는다.

~~~powershell
$python = Get-Command py.exe -ErrorAction SilentlyContinue
if (-not $python) { $python = Get-Command python.exe -ErrorAction SilentlyContinue }
if ($python) { $env:PYTHON_EXECUTABLE = $python.Source }
~~~

그 다음 3장의 반복 실행 명령을 다시 실행한다. 소스와 규칙·로그 경로는 스크립트 위치에서
자동 계산되므로 `C:\Users\...` 같은 고정 경로를 파일에 넣지 않는다.

## 9. 안전 원칙

- 스케줄러를 중지해도 Bronze 원문은 삭제되지 않는다.
- 다음 실행은 저장된 cursor와 미처리 Bronze를 기준으로 이어간다.
- Gold 적재 실패 시 기존 Gold 데이터는 유지된다.
- 문제가 있으면 status 확인 → stop → 로그 확인 순서로 점검한다.
