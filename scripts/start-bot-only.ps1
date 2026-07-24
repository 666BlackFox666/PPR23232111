param()

$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$RuntimeDir = Join-Path $ProjectRoot ".runtime\bot-only"
$PythonExe = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$EnvFile = Join-Path $ProjectRoot ".env"
. (Join-Path $PSScriptRoot "bot-only-common.ps1")

function Fail($Message) {
    Write-Host "[ERROR] $Message" -ForegroundColor Red
    Write-PprBotLog $ProjectRoot "startup" $Message -Error
    exit 1
}

function Info($Message) {
    Write-Host "[INFO] $Message" -ForegroundColor Cyan
}

function Warn($Message) {
    Write-Host "[WARN] $Message" -ForegroundColor Yellow
}

function Get-RequiredCommand($Name, $Hint) {
    $command = Get-Command $Name -ErrorAction SilentlyContinue
    if (-not $command) {
        Fail "$Hint Command not found: $Name"
    }
    return $command.Source
}

function Test-PortListening($Port) {
    try {
        return [bool](Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
    } catch {
        return $false
    }
}

function Quote-PsString($Value) {
    return "'" + ($Value -replace "'", "''") + "'"
}

function Save-ProcessId($Name, $ProcessIdValue) {
    if (-not (Test-Path $RuntimeDir)) {
        New-Item -ItemType Directory -Path $RuntimeDir -Force | Out-Null
    }
    Set-Content -Path (Join-Path $RuntimeDir "$Name.pid") -Value $ProcessIdValue -Encoding ASCII
}

function Start-Window($Name, $CommandText) {
    $process = Start-Process `
        -FilePath "powershell.exe" `
        -ArgumentList @("-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", $CommandText) `
        -WorkingDirectory $ProjectRoot `
        -WindowStyle Hidden `
        -PassThru
    Save-ProcessId $Name $process.Id
    return $process.Id
}

function Start-DirectLoggedProcess($Name, $FilePath, $Arguments, $StdoutPath, $StderrPath) {
    $process = Start-Process -FilePath $FilePath -ArgumentList $Arguments -WorkingDirectory $ProjectRoot `
        -RedirectStandardOutput $StdoutPath -RedirectStandardError $StderrPath -WindowStyle Hidden -PassThru
    Save-ProcessId $Name $process.Id
    return $process.Id
}

function Resolve-BotEffectiveProcessId($LauncherProcessId) {
    for ($attempt = 1; $attempt -le 10; $attempt++) {
        $runner = @(Get-PprBotRunnerProcesses | Where-Object { @($_.ProcessIds) -contains $LauncherProcessId })
        if ($runner.Count -eq 1) {
            Save-ProcessId "bot" $runner[0].EffectiveProcessId
            return $runner[0].EffectiveProcessId
        }
        Start-Sleep -Milliseconds 500
    }
    $schedulerPid = Get-PprBotSchedulerWorkerPid $PythonExe $ProjectRoot
    if ($schedulerPid -gt 0 -and (Get-Process -Id $schedulerPid -ErrorAction SilentlyContinue)) {
        Save-ProcessId "bot" $schedulerPid
        return $schedulerPid
    }
    Save-ProcessId "bot" $LauncherProcessId
    return $LauncherProcessId
}

function Build-LoggedCommand($ProcessCommand, $LogPath) {
    $writer = Join-Path $PSScriptRoot "write-rotating-log.ps1"
    return "& $ProcessCommand 2>&1 | & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $(Quote-PsString $writer) -LogPath $(Quote-PsString $LogPath)"
}

function Wait-PostgresReady($Attempts, $DelaySeconds) {
    for ($index = 1; $index -le $Attempts; $index++) {
        $status = Get-PprBotDockerInspect "{{.State.Health.Status}}" "pprbot-postgres"
        if ($status -eq "healthy") {
            return $true
        }
        Info "Waiting for PostgreSQL ($index/$Attempts). Current health: $status"
        Start-Sleep -Seconds $DelaySeconds
    }
    return $false
}

function Wait-BackendHealth($Attempts, $DelaySeconds) {
    for ($index = 1; $index -le $Attempts; $index++) {
        try {
            $response = Invoke-RestMethod -Uri "http://127.0.0.1:8000/health" -TimeoutSec 2
            if ($response.ok -eq $true) {
                return $true
            }
        } catch {
            # Keep waiting until the backend starts.
        }
        Info "Waiting for backend /health ($index/$Attempts)."
        Start-Sleep -Seconds $DelaySeconds
    }
    return $false
}

Set-Location -LiteralPath $ProjectRoot
Info "Project root: $ProjectRoot"
Initialize-PprBotLogs $ProjectRoot
Write-PprBotLog $ProjectRoot "startup" "bot_only startup requested."

if (-not (Test-Path $EnvFile)) {
    Fail ".env file not found: $EnvFile"
}
try {
    Assert-PprBotOnlyProductionEnv $EnvFile | Out-Null
} catch {
    Fail $_.Exception.Message
}
Get-RequiredCommand "docker" "Install Docker Desktop and make sure docker.exe is in PATH." | Out-Null
if (-not (Test-Path $PythonExe)) {
    Fail "Python virtualenv not found: $PythonExe"
}

try {
    & docker compose version | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Fail "Docker Compose plugin is not available."
    }
} catch {
    Fail "Docker Compose plugin is not available."
}

$postgresStatus = "error"
$migrationStatus = "not run"
$backendStatus = "not started"
$botStatus = "not started"
$startupFailed = $false

Info "Starting PostgreSQL container..."
Write-PprBotLog $ProjectRoot "startup" "Starting PostgreSQL container."
& docker compose up -d db
if ($LASTEXITCODE -ne 0) {
    Fail "docker compose up -d db failed."
}
if (-not (Wait-PostgresReady -Attempts 30 -DelaySeconds 2)) {
    Fail "PostgreSQL did not become healthy in time."
}
$postgresStatus = "running"
Write-PprBotLog $ProjectRoot "startup" "PostgreSQL is healthy."

Info "Applying Alembic migrations..."
Write-PprBotLog $ProjectRoot "startup" "Applying Alembic migrations."
$env:PYTHONPATH = "backend"
& $PythonExe -m alembic upgrade head
if ($LASTEXITCODE -ne 0) {
    $migrationStatus = "error"
    Fail "Alembic migration failed. Backend and bot were not started."
}
$migrationStatus = "applied"
Write-PprBotLog $ProjectRoot "startup" "Alembic migrations applied."

if (Test-PortListening 8000) {
    $backendStatus = "already running"
    Warn "Backend port 8000 is already in use. Backend window will not be started."
} else {
    $backendLog = Join-Path $ProjectRoot "logs\backend.log"
    $processCommand = "$(Quote-PsString $PythonExe) -m uvicorn app.main:app --host 127.0.0.1 --port 8000"
    $command = "Set-Location -LiteralPath $(Quote-PsString $ProjectRoot); `$env:PYTHONPATH = 'backend'; $(Build-LoggedCommand $processCommand $backendLog)"
    $processIdValue = Start-Window "backend" $command
    $backendStatus = "started (pid $processIdValue)"
    Write-PprBotLog $ProjectRoot "startup" "Backend launcher started with pid $processIdValue."
}

if (Wait-BackendHealth -Attempts 40 -DelaySeconds 2) {
    if ((Get-PprBotRunnerProcesses).Count -gt 0) {
        $botStatus = "already running"
        Warn "Bot runner is already running. Bot window will not be started."
    } else {
        $botLog = Join-Path $ProjectRoot "logs\bot.log"
        $botErrorLog = Join-Path $ProjectRoot "logs\bot.stderr.log"
        $launcherProcessId = Start-DirectLoggedProcess "bot" $PythonExe @("-m", "app.bot.runner") $botLog $botErrorLog
        Save-ProcessId "bot-root" $launcherProcessId
        Start-Sleep -Seconds 3
        $processIdValue = Resolve-BotEffectiveProcessId $launcherProcessId
        $runnerProcesses = @(Get-PprBotRunnerProcesses)
        if ($runnerProcesses.Count -gt 0) {
            $botStatus = "started (pid $processIdValue)"
            Write-PprBotLog $ProjectRoot "startup" "Bot runner started with pid $processIdValue."
        } else {
            $botStatus = "error: bot runner exited during startup"
            Write-PprBotLog $ProjectRoot "startup" "Bot runner exited during startup; inspect bot.log." -Error
            $startupFailed = $true
        }
    }
} else {
    $botStatus = "skipped: backend /health is not ready"
    Warn "Backend /health did not become ready. Bot was not started."
    Write-PprBotLog $ProjectRoot "startup" "Backend health did not become ready; bot was not started." -Error
    $startupFailed = $true
}

Write-Host ""
Write-Host "PPRBot bot_only startup summary" -ForegroundColor Green
Write-Host "PostgreSQL: $postgresStatus"
Write-Host "migrations: $migrationStatus"
Write-Host "backend: $backendStatus"
Write-Host "backend URL: http://127.0.0.1:8000"
Write-Host "bot: $botStatus"
Write-Host "frontend: not started (bot_only)"
Write-Host "cloudflared: not started (bot_only)"
Write-Host ""
Warn "This script does not change NOTIFICATIONS_AUTO_SEND_ENABLED, PILOT_AUTO_SEND_ALLOWED or AUTO_SEND_ALLOW_MASS."
Write-PprBotLog $ProjectRoot "startup" "Startup summary: PostgreSQL=$postgresStatus; migrations=$migrationStatus; backend=$backendStatus; bot=$botStatus."
if ($startupFailed) {
    exit 1
}
