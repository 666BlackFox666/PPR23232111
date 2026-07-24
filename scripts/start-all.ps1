param()

$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$RuntimeDir = Join-Path $ProjectRoot ".runtime"
$PythonExe = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$EnvFile = Join-Path $ProjectRoot ".env"

function Fail($Message) {
    Write-Host "[ERROR] $Message" -ForegroundColor Red
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
        $connections = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
        if ($connections) {
            return $true
        }
    } catch {
        # Fallback below.
    }

    $client = $null
    try {
        $client = New-Object System.Net.Sockets.TcpClient
        $async = $client.BeginConnect("127.0.0.1", $Port, $null, $null)
        $connected = $async.AsyncWaitHandle.WaitOne(300, $false)
        if ($connected -and $client.Connected) {
            $client.EndConnect($async)
            return $true
        }
    } catch {
        return $false
    } finally {
        if ($client) {
            $client.Close()
        }
    }
    return $false
}

function Get-CommandLineProcess($Pattern) {
    try {
        return @(Get-CimInstance Win32_Process -ErrorAction Stop | Where-Object { $_.CommandLine -like "*$Pattern*" })
    } catch {
        return @()
    }
}

function Test-ProcessNameRunning($Name) {
    return [bool](Get-Process -Name $Name -ErrorAction SilentlyContinue)
}

function Quote-PsString($Value) {
    return "'" + ($Value -replace "'", "''") + "'"
}

function Save-ProcessId($Name, $ProcessIdValue) {
    if (-not (Test-Path $RuntimeDir)) {
        New-Item -ItemType Directory -Path $RuntimeDir | Out-Null
    }
    $path = Join-Path $RuntimeDir "$Name.pid"
    Set-Content -Path $path -Value $ProcessIdValue -Encoding ASCII
}

function Start-Window($Name, $CommandText) {
    $process = Start-Process `
        -FilePath "powershell.exe" `
        -ArgumentList @("-NoExit", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", $CommandText) `
        -WorkingDirectory $ProjectRoot `
        -PassThru
    Save-ProcessId $Name $process.Id
    return $process.Id
}

function Wait-PostgresReady($Attempts, $DelaySeconds) {
    for ($index = 1; $index -le $Attempts; $index++) {
        $status = ""
        try {
            $status = (& docker inspect -f "{{.State.Health.Status}}" pprbot-postgres 2>$null).Trim()
        } catch {
            $status = ""
        }
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
            # Keep waiting.
        }
        Info "Waiting for backend /health ($index/$Attempts)."
        Start-Sleep -Seconds $DelaySeconds
    }
    return $false
}

function Get-DotEnvValue($Path, $Key) {
    if (-not (Test-Path $Path)) {
        return $null
    }
    foreach ($line in Get-Content -Path $Path) {
        $trimmed = $line.Trim()
        if ($trimmed -and -not $trimmed.StartsWith("#") -and $trimmed.StartsWith("$Key=")) {
            return $trimmed.Substring($Key.Length + 1).Trim().Trim('"').Trim("'")
        }
    }
    return $null
}

Set-Location -LiteralPath $ProjectRoot

Info "Project root: $ProjectRoot"

Get-RequiredCommand "docker" "Install Docker Desktop and make sure docker.exe is in PATH." | Out-Null
$NpmCmd = Get-RequiredCommand "npm.cmd" "Install Node.js and make sure npm.cmd is in PATH."
$CloudflaredExe = Get-RequiredCommand "cloudflared" "Install cloudflared and make sure it is in PATH."

if (-not (Test-Path $PythonExe)) {
    Fail "Python virtualenv not found: $PythonExe"
}
if (-not (Test-Path $EnvFile)) {
    Fail ".env file not found: $EnvFile"
}

$deploymentMode = Get-DotEnvValue $EnvFile "DEPLOYMENT_MODE"
if ($deploymentMode -ne "full") {
    $displayMode = $deploymentMode
    if ([string]::IsNullOrWhiteSpace($displayMode)) {
        $displayMode = "bot_only"
    }
    Warn "DEPLOYMENT_MODE=$displayMode. start-all.ps1 is intended for full mode and will start frontend plus cloudflared."
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
$frontendStatus = "not started"
$cloudflaredStatus = "not started"
$botStatus = "not started"

Info "Starting PostgreSQL container..."
& docker compose up -d db
if ($LASTEXITCODE -ne 0) {
    Fail "docker compose up -d db failed."
}

if (Wait-PostgresReady -Attempts 30 -DelaySeconds 2) {
    $postgresStatus = "running"
} else {
    Fail "PostgreSQL did not become healthy in time."
}

Info "Applying Alembic migrations..."
$env:PYTHONPATH = "backend"
& $PythonExe -m alembic upgrade head
if ($LASTEXITCODE -ne 0) {
    $migrationStatus = "error"
    Fail "Alembic migration failed. Other components were not started."
}
$migrationStatus = "applied"

if (Test-PortListening 8000) {
    $backendStatus = "already running"
    Warn "Backend port 8000 is already in use. Backend window will not be started."
} else {
    $command = "Set-Location -LiteralPath $(Quote-PsString $ProjectRoot); `$env:PYTHONPATH = 'backend'; & $(Quote-PsString $PythonExe) -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000"
    $processIdValue = Start-Window "backend" $command
    $backendStatus = "started (pid $processIdValue)"
}

if (Test-PortListening 5173) {
    $frontendStatus = "already running"
    Warn "Frontend port 5173 is already in use. Frontend window will not be started."
} else {
    $command = "Set-Location -LiteralPath $(Quote-PsString (Join-Path $ProjectRoot 'frontend')); & $(Quote-PsString $NpmCmd) run dev -- --host 127.0.0.1 --port 5173"
    $processIdValue = Start-Window "frontend" $command
    $frontendStatus = "started (pid $processIdValue)"
}

if (Test-ProcessNameRunning "cloudflared") {
    $cloudflaredStatus = "already running"
    Warn "cloudflared is already running. Cloudflare window will not be started."
} else {
    $command = "Set-Location -LiteralPath $(Quote-PsString $ProjectRoot); & $(Quote-PsString $CloudflaredExe) tunnel --url http://127.0.0.1:5173"
    $processIdValue = Start-Window "cloudflared" $command
    $cloudflaredStatus = "started (pid $processIdValue)"
}

if (Wait-BackendHealth -Attempts 40 -DelaySeconds 2) {
    if ((Get-CommandLineProcess "app.bot.runner").Count -gt 0) {
        $botStatus = "already running"
        Warn "Bot runner is already running. Bot window will not be started."
    } else {
        $command = "Set-Location -LiteralPath $(Quote-PsString $ProjectRoot); `$env:PYTHONPATH = 'backend'; & $(Quote-PsString $PythonExe) -m app.bot.runner"
        $processIdValue = Start-Window "bot" $command
        $botStatus = "started (pid $processIdValue)"
    }
} else {
    $botStatus = "skipped: backend /health is not ready"
    Warn "Backend /health did not become ready. Bot was not started."
}

Write-Host ""
Write-Host "PPRBot startup summary" -ForegroundColor Green
Write-Host "PostgreSQL: $postgresStatus"
Write-Host "migrations: $migrationStatus"
Write-Host "backend: $backendStatus"
Write-Host "backend URL: http://127.0.0.1:8000"
Write-Host "frontend: $frontendStatus"
Write-Host "frontend URL: http://127.0.0.1:5173"
Write-Host "cloudflared: $cloudflaredStatus"
Write-Host "bot: $botStatus"
Write-Host ""
Warn "Cloudflare Quick Tunnel URL is temporary. Copy the trycloudflare URL from the cloudflared window into .env WEBAPP_URL and BotFather if it changed."
Warn "This script does not change NOTIFICATIONS_AUTO_SEND_ENABLED or AUTO_SEND_ALLOW_MASS."
