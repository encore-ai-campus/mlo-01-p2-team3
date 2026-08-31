param(
    [ValidateSet("start", "once", "status", "stop")]
    [string]$Action = "start"
)

# 사용법: .\run_pipeline_5m.ps1 start | once | status | stop

# 스크립트 위치에서 루트를 계산하므로 다른 PC·폴더에 내려받아도 동작한다.
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$logDirectory = Join-Path $projectRoot "logs"
$pipelineLogFile = Join-Path $logDirectory "pipeline.log"
$schedulerLogFile = Join-Path $logDirectory "scheduler.log"
$bronzeLogFile = Join-Path $logDirectory "bronze.log"
$bronzeArchiveLogFile = Join-Path $logDirectory "bronze_archive.log"
$silverLogFile = Join-Path $logDirectory "silver.log"
$goldLogFile = Join-Path $logDirectory "gold.log"
$pidFile = Join-Path $logDirectory "scheduler.pid.json"
$stopFile = Join-Path $logDirectory "scheduler.stop"
$bronzeScript = Join-Path $projectRoot "scripts\run_pipeline.py"
$bronzeVerifyScript = Join-Path $projectRoot "scripts\verify_bronze_archive.py"
$silverScript = Join-Path $projectRoot "scripts\run_silver_once.py"
$goldScript = Join-Path $projectRoot "scripts\run_gold_once.py"
$schedulerName = "hr_project_scheduler"
$utf8 = New-Object System.Text.UTF8Encoding($false)

# 숨은 실행에서도 Python 한글 출력과 PowerShell 수신 인코딩을 UTF-8로 맞춘다.
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
$OutputEncoding = $utf8
[Console]::OutputEncoding = $utf8

New-Item -ItemType Directory -Force -Path $logDirectory | Out-Null

function Write-Log {
    param(
        [string]$Message,
        [string]$File = $pipelineLogFile
    )

    [System.IO.File]::AppendAllText(
        $File,
        $Message + [Environment]::NewLine,
        $utf8
    )
}

function Write-SchedulerLog {
    param([string]$Message)
    Write-Log $Message $schedulerLogFile
}

function Write-PipelineLog {
    param([string]$Message)
    Write-Log $Message $pipelineLogFile
}

function Write-BronzeLog {
    param([string]$Message)
    Write-Log $Message $bronzeLogFile
}

function Write-BronzeArchiveLog {
    param([string]$Message)
    Write-Log $Message $bronzeArchiveLogFile
}

function Write-SilverLog {
    param([string]$Message)
    Write-Log $Message $silverLogFile
}

function Write-GoldLog {
    param([string]$Message)
    Write-Log $Message $goldLogFile
}

function Read-SchedulerInfo {
    if (-not (Test-Path $pidFile)) {
        return $null
    }
    try {
        return (Get-Content -LiteralPath $pidFile -Raw | ConvertFrom-Json)
    }
    catch {
        return $null
    }
}

function Show-Status {
    $info = Read-SchedulerInfo
    if (-not $info) {
        [PSCustomObject]@{
            Name = $schedulerName
            Status = "STOPPED"
            ProcessId = $null
            Script = (Join-Path $PSScriptRoot "run_pipeline_5m.ps1")
        } | Format-List
        return
    }

    $process = Get-Process -Id ([int]$info.process_id) -ErrorAction SilentlyContinue
    [PSCustomObject]@{
        Name = $schedulerName
        Status = if ($process) { "RUNNING" } else { "STOPPED" }
        ProcessId = [int]$info.process_id
        StartedAtUtc = $info.process_start_utc
        Script = $info.script
    } | Format-List
}

function Stop-Scheduler {
    $info = Read-SchedulerInfo
    if (-not $info) {
        Write-Output "Name=$schedulerName Status=STOPPED (PID 파일 없음)"
        return
    }

    $processId = [int]$info.process_id
    $process = Get-Process -Id $processId -ErrorAction SilentlyContinue
    if (-not $process) {
        Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue
        Write-Output "Name=$schedulerName Status=STOPPED (프로세스 없음)"
        return
    }

    # 대기 중인 스케줄러에는 중지 신호를 보내고, 10초 뒤에도 남아 있으면 강제 종료한다.
    New-Item -ItemType File -Force -Path $stopFile | Out-Null
    $deadline = (Get-Date).AddSeconds(10)
    while ((Get-Date) -lt $deadline) {
        if (-not (Get-Process -Id $processId -ErrorAction SilentlyContinue)) {
            Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue
            Write-Output "Name=$schedulerName Status=STOPPED ProcessId=$processId"
            return
        }
        Start-Sleep -Seconds 1
    }

    $process = Get-Process -Id $processId -ErrorAction SilentlyContinue
    if ($process -and $process.ProcessName -eq "powershell") {
        taskkill.exe /PID $processId /T /F | Out-Null
    }
    Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $stopFile -Force -ErrorAction SilentlyContinue
    Write-Output "Name=$schedulerName Status=STOPPED_FORCE ProcessId=$processId"
}

if ($Action -eq "status") {
    Show-Status
    exit 0
}

if ($Action -eq "stop") {
    Stop-Scheduler
    exit 0
}

# 이미 실행 중이면 중복 수집을 막는다.
$oldInfo = Read-SchedulerInfo
if ($oldInfo) {
    $oldProcess = Get-Process -Id ([int]$oldInfo.process_id) -ErrorAction SilentlyContinue
    if ($oldProcess) {
        Write-Output "Name=$schedulerName Status=ALREADY_RUNNING ProcessId=$($oldInfo.process_id)"
        exit 1
    }
    Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue
}
Remove-Item -LiteralPath $stopFile -Force -ErrorAction SilentlyContinue

# 숨은 PowerShell 프로세스에서도 같은 Python 실행기를 사용하도록 먼저 찾는다.
$pythonPath = $env:PYTHON_EXECUTABLE
if (-not $pythonPath -or -not (Test-Path -LiteralPath $pythonPath)) {
    $pythonCommand = Get-Command py.exe -ErrorAction SilentlyContinue
    if (-not $pythonCommand) {
        $pythonCommand = Get-Command python.exe -ErrorAction SilentlyContinue
    }
    $pythonPath = if ($pythonCommand) { $pythonCommand.Source } else { $null }
}
if (-not $pythonPath) {
    Write-SchedulerLog "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] scheduler failed (PYTHON_NOT_FOUND)"
    Write-Output "Name=$schedulerName Status=FAILED (py.exe 또는 python.exe를 찾을 수 없음)"
    exit 1
}

[PSCustomObject]@{
    name = $schedulerName
    process_id = $PID
    process_start_utc = (Get-Date).ToUniversalTime().ToString("o")
    script = $PSCommandPath
} | ConvertTo-Json | Set-Content -LiteralPath $pidFile -Encoding UTF8
Write-SchedulerLog "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] scheduler started (name=$schedulerName, pid=$PID)"
Set-Location $projectRoot

try {
    while ($true) {
        if (Test-Path $stopFile) {
            Write-SchedulerLog "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] stop requested"
            break
        }

        Write-PipelineLog "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] pipeline start"

        # Bronze API 수집은 공개 시각 전이면 즉시 끝나며, 결과 요약만 로그에 남긴다.
        $bronzeOutput = @()
        $pipelineExit = 1
        $bronzeRunId = $null
        $bronzePages = 0
        try {
            $bronzeOutput = @(& $pythonPath $bronzeScript 2>&1)
            $pipelineExit = $LASTEXITCODE
        }
        catch {
            $bronzeOutput = @($_.Exception.Message)
        }
        if ($null -eq $pipelineExit) {
            $pipelineExit = 1
        }

        $bronzeSummary = $bronzeOutput |
            Where-Object { $_.ToString().TrimStart().StartsWith('{') } |
            Select-Object -Last 1
        if ($bronzeSummary) {
            Write-BronzeLog $bronzeSummary.ToString().TrimEnd()
            try {
                $bronzeResult = $bronzeSummary.ToString() | ConvertFrom-Json
                $bronzeRunId = [string]$bronzeResult.run_id
                $bronzePages = [int]$bronzeResult.pages
                if (@('COMPLETED', 'PAGE_LIMIT_REACHED', 'WAITING_FOR_REFRESH') -contains [string]$bronzeResult.status) {
                    # 정상 JSON 요약이 있으면 Windows 종료 코드가 1이어도 Bronze는 성공으로 본다.
                    $pipelineExit = 0
                }
            }
            catch {
                # 요약을 읽지 못하면 기존 종료 코드를 유지한다.
            }
        }
        else {
            foreach ($line in $bronzeOutput) {
                if ($line.ToString().Trim()) {
                    Write-BronzeLog $line.ToString().TrimEnd()
                }
            }
        }

        # 실제 원문 페이지를 받은 실행만 JSON·manifest 무결성을 검사한다.
        # 고아 JSON과 임시 manifest는 삭제하지 않고 보고서와 실행 이력에 남긴다.
        if ($pipelineExit -eq 0 -and $bronzeRunId -and $bronzePages -gt 0) {
            Write-BronzeArchiveLog "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] archive verification start (run_id=$bronzeRunId)"
            $archiveOutput = @()
            $archiveExit = 1
            try {
                $archiveOutput = @(
                    & $pythonPath $bronzeVerifyScript --run-id $bronzeRunId --compact 2>&1
                )
                $archiveExit = $LASTEXITCODE
            }
            catch {
                $archiveOutput = @($_.Exception.Message)
            }
            foreach ($line in $archiveOutput) {
                if ($line.ToString().Trim()) {
                    Write-BronzeArchiveLog $line.ToString().TrimEnd()
                }
            }
            if ($null -eq $archiveExit -or $archiveExit -ne 0) {
                $pipelineExit = 1
                Write-BronzeArchiveLog "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] archive verification failed (run_id=$bronzeRunId)"
            }
            else {
                Write-BronzeArchiveLog "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] archive verification end (run_id=$bronzeRunId)"
            }
        }

        # Bronze가 실패해도 이미 저장된 미처리 원문은 Silver에서 계속 처리한다.
        # 내부적으로 1,000건씩 저장하므로 다음 실행에서 backlog를 이어간다.
        $silverExit = 1
        $silverLogPrefix = if ($pipelineExit -eq 0) {
            "silver start (pending drain)"
        }
        else {
            "silver start (bronze failed; pending drain)"
        }
        Write-SilverLog "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] $silverLogPrefix"
        $silverOutput = @()
        try {
            $silverOutput = @(
                & $pythonPath $silverScript --pending --drain --write 2>&1
            )
            $silverExit = $LASTEXITCODE
        }
        catch {
            $silverOutput = @($_.Exception.Message)
        }
        foreach ($line in $silverOutput) {
            $text = $line.ToString().Trim()
            if ($text) {
                Write-SilverLog "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] $text"
            }
        }
        # Python JSON status로 성공 여부를 판단한다. 한글 로그를 정규식으로
        # 검사하지 않으므로 Windows PowerShell 5.1 인코딩 영향을 받지 않는다.
        $silverText = ($silverOutput | ForEach-Object { $_.ToString() }) -join "`n"
        $silverReportedFailure = $silverText -match '"status"\s*:\s*"FAILED"'
        $silverReportedComplete = $silverText -match '"status"\s*:\s*"SUCCEEDED"'
        if ($silverReportedFailure) {
            $silverExit = 1
        }
        elseif ($silverReportedComplete) {
            $silverExit = 0
        }
        if ($null -eq $silverExit) {
            $silverExit = 1
        }
        Write-SilverLog "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] silver end (exit=$silverExit)"
        if ($silverExit -ne 0) {
            $pipelineExit = 1
        }

        $goldExit = 1
        if ($silverExit -eq 0) {
            # Bronze 상태와 관계없이 Silver가 성공하면 Gold를 실행한다.
            # 전체 품질 게이트에 실패한 행은 제외하고, 정상 조직·관리자·배정·
            # 피처는 부분 적재한다. 실패 데이터는 Silver/검토 이력에 남는다.
            Write-GoldLog "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] gold start (partial write)"
            $goldOutput = @()
            try {
                $goldOutput = @(
                    & $pythonPath $goldScript --partial --write 2>&1
                )
                $goldExit = $LASTEXITCODE
            }
            catch {
                $goldOutput = @($_.Exception.Message)
            }
            foreach ($line in $goldOutput) {
                $text = $line.ToString().Trim()
                if ($text) {
                    Write-GoldLog "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] $text"
                }
            }
            # Gold도 JSON 결과를 기준으로 실제 성공 여부를 보정한다.
            $goldText = ($goldOutput | ForEach-Object { $_.ToString() }) -join "`n"
            if ($goldText -match '"status"\s*:\s*"(SUCCEEDED|SUCCEEDED_WITH_QUARANTINE)"') {
                $goldExit = 0
            }
            elseif ($goldText -match '"status"\s*:\s*"FAILED"') {
                $goldExit = 1
            }
            if ($null -eq $goldExit) {
                $goldExit = 1
            }
            Write-GoldLog "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] gold end (exit=$goldExit)"
            if ($goldExit -ne 0) {
                $pipelineExit = 1
            }
        }
        else {
            Write-GoldLog "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] gold skipped (silver failed)"
        }

        Write-PipelineLog "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] pipeline end (exit=$pipelineExit)"

        # 테스트·수동 검증에서는 한 주기만 실행하고 종료한다.
        if ($Action -eq "once") {
            break
        }

        # 5분 대기 중에도 중지 요청을 확인한다.
        $stopRequested = $false
        for ($second = 0; $second -lt 300; $second++) {
            if (Test-Path $stopFile) {
                $stopRequested = $true
                break
            }
            Start-Sleep -Seconds 1
        }
        if ($stopRequested) {
            Write-SchedulerLog "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] stop requested"
            break
        }
    }
}
finally {
    Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $stopFile -Force -ErrorAction SilentlyContinue
    try {
        Write-SchedulerLog "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] scheduler stopped (name=$schedulerName, pid=$PID)"
    }
    catch {
        # 로그를 쓸 수 없어도 종료는 진행한다.
    }
}
