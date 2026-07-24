param(
    [switch]$IncludeTelegramMessage
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$EnvFile = Join-Path $ProjectRoot ".env"
$PythonExe = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
. (Join-Path $PSScriptRoot "bot-only-common.ps1")

Set-Location -LiteralPath $ProjectRoot
Initialize-PprBotLogs $ProjectRoot
try {
    $values = Assert-PprBotOnlyProductionEnv $EnvFile
} catch {
    Write-Host "[FAIL] $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}

$failed = $false
function Check($Name, $Passed) {
    if ($Passed) { Write-Host "[OK] $Name" -ForegroundColor Green } else { Write-Host "[FAIL] $Name" -ForegroundColor Red; $script:failed = $true }
}

Check "deployment mode bot_only" ($values['DEPLOYMENT_MODE'].ToLowerInvariant() -eq "bot_only")
$health = $false
try { $health = (Invoke-RestMethod http://127.0.0.1:8000/health -TimeoutSec 3).ok -eq $true } catch {}
Check "/health" $health
$runnerProcesses = @(Get-PprBotRunnerProcesses)
Check "bot runner" ($runnerProcesses.Count -eq 1)
Check "frontend is stopped" (-not (Test-PprBotPortListening 5173))
Check "cloudflared is stopped" (@(Get-Process -Name cloudflared -ErrorAction SilentlyContinue).Count -eq 0)

$getMe = $null
try { $getMe = Invoke-RestMethod "https://api.telegram.org/bot$($values['TELEGRAM_BOT_TOKEN'])/getMe" -TimeoutSec 10 } catch {}
Check "Telegram getMe" ($getMe -and $getMe.ok -eq $true)

$env:PYTHONPATH = "backend"
$dbCheck = & $PythonExe -c "from sqlalchemy import text; from app.db.session import engine; print(engine.connect().execute(text('select 1')).scalar())" 2>$null
Check "database connection" ($LASTEXITCODE -eq 0 -and $dbCheck -match "1")
$scheduler = & $PythonExe -c "import json; from app.db.session import SessionLocal; from app.services.telegram_sender import scheduler_status; db=SessionLocal(); print(json.dumps(scheduler_status(db), default=str)); db.close()" 2>$null | Out-String
$schedulerExitCode = $LASTEXITCODE
$schedulerObject = $null
try { $schedulerObject = $scheduler | ConvertFrom-Json } catch {}
$autoSendEnabled = $values['NOTIFICATIONS_AUTO_SEND_ENABLED'].ToLowerInvariant() -eq "true"
if ($autoSendEnabled) {
    $schedulerReady = $schedulerExitCode -eq 0 -and $schedulerObject -and
        $schedulerObject.auto_send_enabled -eq $true -and
        $schedulerObject.scheduler_running -eq $true -and
        -not [string]::IsNullOrWhiteSpace([string]$schedulerObject.worker_id) -and
        -not [string]::IsNullOrWhiteSpace([string]$schedulerObject.last_poll_at)
    Check "scheduler status" $schedulerReady
} else {
    Check "scheduler status" ($schedulerExitCode -eq 0 -and $schedulerObject -and $schedulerObject.auto_send_enabled -eq $false)
}
if ($schedulerObject) { Write-Host "scheduler: $($scheduler.Trim())" }

if ($IncludeTelegramMessage -and -not $failed) {
    $sent = $false
    try {
        $response = Invoke-RestMethod -Method Post -Uri "https://api.telegram.org/bot$($values['TELEGRAM_BOT_TOKEN'])/sendMessage" -Body @{ chat_id = $values['TELEGRAM_CHAT_ID']; text = "PPRBot: проверка подключения" } -TimeoutSec 10
        $sent = $response.ok -eq $true
    } catch {}
    Check "Telegram test message" $sent
}

if ($failed) {
    Write-PprBotLog $ProjectRoot "startup" "Pilot smoke test failed." -Error
    exit 1
}
Write-PprBotLog $ProjectRoot "startup" "Pilot smoke test passed."
Write-Host "Pilot smoke test passed." -ForegroundColor Green
