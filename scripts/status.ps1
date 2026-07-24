param()

$ErrorActionPreference = "Continue"

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$RuntimeDir = Join-Path $ProjectRoot ".runtime"
$EnvFile = Join-Path $ProjectRoot ".env"

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

function Test-PidFileProcess($Name) {
    $path = Join-Path $RuntimeDir "$Name.pid"
    if (-not (Test-Path $path)) {
        return "no pid file"
    }
    $raw = (Get-Content -Path $path -ErrorAction SilentlyContinue | Select-Object -First 1)
    $processIdValue = 0
    if (-not [int]::TryParse($raw, [ref]$processIdValue)) {
        return "invalid pid file"
    }
    $process = Get-Process -Id $processIdValue -ErrorAction SilentlyContinue
    if ($process) {
        return "running (pid $processIdValue, $($process.ProcessName))"
    }
    return "stale pid file (pid $processIdValue)"
}

function Read-DotEnv($Path) {
    $result = @{}
    if (-not (Test-Path $Path)) {
        return $result
    }
    foreach ($line in Get-Content -Path $Path) {
        $trimmed = $line.Trim()
        if (-not $trimmed -or $trimmed.StartsWith("#")) {
            continue
        }
        $index = $trimmed.IndexOf("=")
        if ($index -le 0) {
            continue
        }
        $key = $trimmed.Substring(0, $index).Trim()
        $value = $trimmed.Substring($index + 1).Trim().Trim('"').Trim("'")
        $result[$key] = $value
    }
    return $result
}

Set-Location -LiteralPath $ProjectRoot

Write-Host "PPRBot status" -ForegroundColor Green
Write-Host "Project root: $ProjectRoot"
Write-Host ""

$docker = Get-Command docker -ErrorAction SilentlyContinue
if ($docker) {
    $postgresHealth = ""
    $postgresState = ""
    try {
        $postgresHealth = (& docker inspect -f "{{.State.Health.Status}}" pprbot-postgres 2>$null).Trim()
        $postgresState = (& docker inspect -f "{{.State.Status}}" pprbot-postgres 2>$null).Trim()
    } catch {
        $postgresHealth = ""
        $postgresState = ""
    }
    if ($postgresState) {
        Write-Host "PostgreSQL: $postgresState / health=$postgresHealth"
    } else {
        Write-Host "PostgreSQL: container not found"
    }
} else {
    Write-Host "PostgreSQL: docker command not found"
}

$backendPort = Test-PortListening 8000
$backendHealth = "not checked"
if ($backendPort) {
    try {
        $response = Invoke-RestMethod -Uri "http://127.0.0.1:8000/health" -TimeoutSec 2
        if ($response.ok -eq $true) {
            $backendHealth = "ok"
        } else {
            $backendHealth = "unexpected response"
        }
    } catch {
        $backendHealth = "not responding"
    }
}
Write-Host "backend: port 8000 listening=$backendPort, health=$backendHealth, pid=$(Test-PidFileProcess 'backend')"
Write-Host "frontend: port 5173 listening=$(Test-PortListening 5173), pid=$(Test-PidFileProcess 'frontend')"

$botProcesses = Get-CommandLineProcess "app.bot.runner"
Write-Host "bot: command-line matches=$($botProcesses.Count), pid=$(Test-PidFileProcess 'bot')"

$cloudProcesses = @(Get-Process -Name cloudflared -ErrorAction SilentlyContinue)
Write-Host "cloudflared: process count=$($cloudProcesses.Count), pid=$(Test-PidFileProcess 'cloudflared')"

Write-Host ""
Write-Host "Safe .env flags" -ForegroundColor Green
$envValues = Read-DotEnv $EnvFile
$safeKeys = @(
    "TELEGRAM_ENABLED",
    "NOTIFICATIONS_AUTO_SEND_ENABLED",
    "PILOT_AUTO_SEND_ALLOWED",
    "AUTO_SEND_MASS_LIMIT",
    "AUTO_SEND_ALLOW_MASS",
    "DEV_COMMANDS_ENABLED",
    "ENV",
    "WEBAPP_URL",
    "TELEGRAM_BOT_USERNAME",
    "TELEGRAM_MINIAPP_SHORT_NAME"
)
foreach ($key in $safeKeys) {
    $value = "<not set>"
    if ($envValues.ContainsKey($key)) {
        $value = $envValues[$key]
    }
    Write-Host "$key=$value"
}
