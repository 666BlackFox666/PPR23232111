param(
    [switch]$SendTestMessage
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$EnvFile = Join-Path $ProjectRoot ".env"
$PythonExe = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
. (Join-Path $PSScriptRoot "bot-only-common.ps1")

function Test-TcpEndpoint($HostName, $Port) {
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $task = $client.BeginConnect($HostName, $Port, $null, $null)
        return $task.AsyncWaitHandle.WaitOne(5000, $false) -and $client.Connected
    } catch {
        return $false
    } finally {
        $client.Close()
    }
}

function Report($Name, $Passed, $Failure) {
    if ($Passed) {
        Write-Host "[OK] $Name" -ForegroundColor Green
        return $true
    }
    Write-Host "[FAIL] ${Name}: $Failure" -ForegroundColor Red
    Write-PprBotLog $ProjectRoot "startup" "${Name} failed: $Failure" -Error
    return $false
}

function Invoke-PythonCapture($Arguments, $Label) {
    $tempRoot = Join-Path $env:TEMP ("pprbot-" + [guid]::NewGuid().ToString("N"))
    $stdoutPath = Join-Path $tempRoot "stdout.log"
    $stderrPath = Join-Path $tempRoot "stderr.log"
    New-Item -ItemType Directory -Path $tempRoot -Force | Out-Null
    try {
        $process = Start-Process -FilePath $PythonExe -ArgumentList $Arguments -WorkingDirectory $ProjectRoot `
            -RedirectStandardOutput $stdoutPath -RedirectStandardError $stderrPath -Wait -PassThru -WindowStyle Hidden
        $stdout = if (Test-Path $stdoutPath) { Get-Content -LiteralPath $stdoutPath -Raw } else { "" }
        $stderr = if (Test-Path $stderrPath) { Get-Content -LiteralPath $stderrPath -Raw } else { "" }
        if ($process.ExitCode -ne 0) {
            $detail = ($stderr.Trim(), $stdout.Trim() | Where-Object { $_ }) -join "`n"
            Write-Host "$Label failed (exit code $($process.ExitCode)): $detail" -ForegroundColor Red
        }
        return [pscustomobject]@{ ExitCode = $process.ExitCode; Stdout = $stdout; Stderr = $stderr }
    } finally {
        Remove-Item -LiteralPath $tempRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
}

Set-Location -LiteralPath $ProjectRoot
Initialize-PprBotLogs $ProjectRoot
$failed = $false
try {
    $values = Assert-PprBotOnlyProductionEnv $EnvFile
    Report "production env" $true "" | Out-Null
} catch {
    Report "production env" $false $_.Exception.Message | Out-Null
    exit 1
}

$postgresHealth = Get-PprBotDockerInspect "{{.State.Health.Status}}" "pprbot-postgres"
if (-not (Report "PostgreSQL health" ($postgresHealth -eq "healthy") "container is not healthy")) { $failed = $true }

$env:PYTHONPATH = "backend"
$currentResult = Invoke-PythonCapture @("-m", "alembic", "current") "Alembic current"
$headsResult = Invoke-PythonCapture @("-m", "alembic", "heads") "Alembic heads"
$currentText = $currentResult.Stdout
$heads = $headsResult.Stdout -split "`r?`n"
$migrationsReady = $currentResult.ExitCode -eq 0 -and $headsResult.ExitCode -eq 0 -and -not [string]::IsNullOrWhiteSpace($currentText)
foreach ($head in @($heads)) {
    $revision = ($head -split '\s+')[0]
    if ($revision -and $currentText -notmatch [regex]::Escape($revision)) {
        $migrationsReady = $false
    }
}
if (-not (Report "Alembic current" $migrationsReady "cannot read current migration")) { $failed = $true }

$backendReady = $false
try { $backendReady = (Invoke-RestMethod http://127.0.0.1:8000/health -TimeoutSec 3).ok -eq $true } catch {}
if (-not (Report "backend health" $backendReady "http://127.0.0.1:8000/health is unavailable")) { $failed = $true }

if (-not (Report "api.telegram.org:443" (Test-TcpEndpoint "api.telegram.org" 443) "network connection failed")) { $failed = $true }

$apiBase = "https://api.telegram.org/bot$($values['TELEGRAM_BOT_TOKEN'])"
$getMe = $null
try { $getMe = Invoke-RestMethod "$apiBase/getMe" -TimeoutSec 10 } catch {}
if (-not (Report "Telegram getMe" ($getMe -and $getMe.ok -eq $true) "token was rejected or Telegram is unavailable")) { $failed = $true }

$chat = $null
try { $chatId = [uri]::EscapeDataString($values['TELEGRAM_CHAT_ID']); $chat = Invoke-RestMethod "$apiBase/getChat?chat_id=$chatId" -TimeoutSec 10 } catch {}
if (-not (Report "Telegram chat access" ($chat -and $chat.ok -eq $true) "bot cannot access TELEGRAM_CHAT_ID")) { $failed = $true }

$runnerProcesses = @(Get-PprBotRunnerProcesses)
$runnerCount = $runnerProcesses.Count
if (-not (Report "bot runner count" ($runnerCount -eq 1) "expected 1 logical runner, found $runnerCount")) { $failed = $true }
if (-not (Report "frontend port 5173" (-not (Test-PprBotPortListening 5173)) "port 5173 is occupied")) { $failed = $true }
if (-not (Report "cloudflared" (@(Get-Process -Name cloudflared -ErrorAction SilentlyContinue).Count -eq 0) "cloudflared is running")) { $failed = $true }

if ($SendTestMessage -and -not $failed) {
    $sent = $false
    try {
        $response = Invoke-RestMethod -Method Post -Uri "$apiBase/sendMessage" -Body @{ chat_id = $values['TELEGRAM_CHAT_ID']; text = "PPRBot: проверка подключения" } -TimeoutSec 10
        $sent = $response.ok -eq $true
    } catch {}
    if (-not (Report "test Telegram message" $sent "message was not sent")) { $failed = $true }
}

if ($failed) { exit 1 }
Write-PprBotLog $ProjectRoot "startup" "bot_only connection check passed."
Write-Host "bot_only connection check passed." -ForegroundColor Green
