param()

$ErrorActionPreference = "Continue"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$RuntimeDir = Join-Path $ProjectRoot ".runtime\bot-only"
$EnvFile = Join-Path $ProjectRoot ".env"
$PythonExe = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
. (Join-Path $PSScriptRoot "bot-only-common.ps1")

function Test-PortListening($Port) {
    try {
        return [bool](Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
    } catch {
        return $false
    }
}

function Test-PidFileProcess($Name) {
    $path = Join-Path $RuntimeDir "$Name.pid"
    if (-not (Test-Path $path)) { return "no pid file" }
    $raw = Get-Content -Path $path -ErrorAction SilentlyContinue | Select-Object -First 1
    $processIdValue = 0
    if (-not [int]::TryParse($raw, [ref]$processIdValue)) { return "invalid pid file" }
    $process = Get-Process -Id $processIdValue -ErrorAction SilentlyContinue
    if (-not $process) {
        Remove-Item -LiteralPath $path -Force -ErrorAction SilentlyContinue
        return "stale pid file removed (pid $processIdValue)"
    }
    if ($Name -eq "bot") {
        $logical = @(Get-PprBotRunnerProcesses | Where-Object { @($_.ProcessIds) -contains $processIdValue })
        if ($logical.Count -ne 1) {
            Remove-Item -LiteralPath $path -Force -ErrorAction SilentlyContinue
            return "stale pid file removed (pid $processIdValue is not bot runner)"
        }
        return "running (logical runner, effective python pid $($logical[0].EffectiveProcessId), pid file $processIdValue)"
    }
    return "running (pid $processIdValue, $($process.ProcessName))"
}

function Read-DotEnv($Path) {
    $result = @{}
    if (-not (Test-Path $Path)) { return $result }
    foreach ($line in Get-Content -Path $Path) {
        $trimmed = $line.Trim()
        if (-not $trimmed -or $trimmed.StartsWith("#")) { continue }
        $index = $trimmed.IndexOf("=")
        if ($index -le 0) { continue }
        $result[$trimmed.Substring(0, $index).Trim()] = $trimmed.Substring($index + 1).Trim().Trim('"').Trim("'")
    }
    return $result
}

Set-Location -LiteralPath $ProjectRoot
Write-Host "PPRBot bot_only status" -ForegroundColor Green

try {
    $postgresState = Get-PprBotDockerInspect "{{.State.Status}}" "pprbot-postgres"
    $postgresHealth = Get-PprBotDockerInspect "{{.State.Health.Status}}" "pprbot-postgres"
    Write-Host "PostgreSQL: $postgresState / health=$postgresHealth"
} catch {
    Write-Host "PostgreSQL: not available"
}

$backendHealth = "not checked"
if (Test-PortListening 8000) {
    try { $backendHealth = $(if ((Invoke-RestMethod http://127.0.0.1:8000/health -TimeoutSec 2).ok) { "ok" } else { "unexpected response" }) } catch { $backendHealth = "not responding" }
}
Write-Host "backend: port 8000 listening=$(Test-PortListening 8000), health=$backendHealth, pid=$(Test-PidFileProcess 'backend')"
$runnerProcesses = @(Get-PprBotRunnerProcesses)
$schedulerPid = Get-PprBotSchedulerWorkerPid $PythonExe $ProjectRoot
if ($runnerProcesses.Count -eq 1 -and $schedulerPid -gt 0 -and (Get-Process -Id $schedulerPid -ErrorAction SilentlyContinue)) {
    $runnerProcesses[0].EffectiveProcessId = $schedulerPid
    $pidPath = Join-Path $RuntimeDir "bot.pid"
    if (Test-Path -LiteralPath $pidPath) {
        Set-Content -LiteralPath $pidPath -Value $schedulerPid -Encoding ASCII
    }
}
if ($runnerProcesses.Count -eq 0) {
    $runnerStatus = "stopped"
} elseif ($runnerProcesses.Count -eq 1) {
    $runnerStatus = "running (logical runner, effective python pid $($runnerProcesses[0].EffectiveProcessId))"
} else {
    $runnerStatus = "ERROR: multiple logical runners (pids $($runnerProcesses.ProcessId -join ', '))"
}
Write-Host "bot: $runnerStatus, pid file=$(Test-PidFileProcess 'bot')"
Write-Host "frontend: port 5173 listening=$(Test-PortListening 5173) (must be false in bot_only)"
Write-Host "cloudflared: process count=$(@(Get-Process -Name cloudflared -ErrorAction SilentlyContinue).Count) (must be 0 in bot_only)"

try {
    $autostartTask = Get-ScheduledTask -TaskName "PPRBot Bot Only" -ErrorAction Stop
    Write-Host "autostart task: registered, state=$($autostartTask.State)"
} catch {
    Write-Host "autostart task: not registered"
}
try {
    $backupTask = Get-ScheduledTask -TaskName "PPRBot Database Backup" -ErrorAction Stop
    Write-Host "backup task: registered, state=$($backupTask.State)"
} catch {
    Write-Host "backup task: not registered"
}
Write-Host "logs: $(Join-Path $ProjectRoot 'logs')"

Write-Host ""
Write-Host "Safe .env flags" -ForegroundColor Green
$values = Read-DotEnv $EnvFile
foreach ($key in @("DEPLOYMENT_MODE", "TELEGRAM_ENABLED", "NOTIFICATIONS_AUTO_SEND_ENABLED", "PILOT_AUTO_SEND_ALLOWED", "AUTO_SEND_MASS_LIMIT", "AUTO_SEND_ALLOW_MASS", "DEV_COMMANDS_ENABLED", "WEBAPP_URL", "TELEGRAM_BOT_USERNAME", "TELEGRAM_MINIAPP_SHORT_NAME")) {
    $value = "<not set>"
    if ($values.ContainsKey($key)) {
        $value = $values[$key]
    } elseif ($key -eq "PILOT_AUTO_SEND_ALLOWED") {
        $value = "false"
    }
    Write-Host "$key=$value"
}
