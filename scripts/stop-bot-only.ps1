param(
    [switch]$StopDatabase
)

$ErrorActionPreference = "Continue"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$RuntimeDir = Join-Path $ProjectRoot ".runtime\bot-only"
. (Join-Path $PSScriptRoot "bot-only-common.ps1")

function Info($Message) {
    Write-Host "[INFO] $Message" -ForegroundColor Cyan
}

function Warn($Message) {
    Write-Host "[WARN] $Message" -ForegroundColor Yellow
}

function Stop-ProcessTree($ProcessIdValue) {
    try {
        foreach ($child in @(Get-CimInstance Win32_Process -Filter "ParentProcessId=$ProcessIdValue" -ErrorAction Stop)) {
            Stop-ProcessTree ([int]$child.ProcessId)
        }
    } catch {
        # Stop the parent even when child discovery is unavailable.
    }
    $process = Get-Process -Id $ProcessIdValue -ErrorAction SilentlyContinue
    if ($process) {
        Info "Stopping process id $ProcessIdValue ($($process.ProcessName))"
        Stop-Process -Id $ProcessIdValue -Force -ErrorAction SilentlyContinue
    }
}

function Stop-FromPidFile($Name) {
    $path = Join-Path $RuntimeDir "$Name.pid"
    if (-not (Test-Path $path)) {
        Warn "$Name pid file not found."
        return
    }
    $raw = Get-Content -Path $path -ErrorAction SilentlyContinue | Select-Object -First 1
    $processIdValue = 0
    if ([int]::TryParse($raw, [ref]$processIdValue)) {
        if ($Name -eq "bot") {
            $logical = @(Get-PprBotRunnerProcesses | Where-Object { @($_.ProcessIds) -contains $processIdValue })
            if ($logical.Count -eq 1) {
                Stop-ProcessTree $logical[0].RootProcessId
            } else {
                Stop-ProcessTree $processIdValue
            }
        } else {
            Stop-ProcessTree $processIdValue
        }
    } else {
        Warn "$Name pid file is invalid: $raw"
    }
    Remove-Item -Path $path -Force -ErrorAction SilentlyContinue
}

Set-Location -LiteralPath $ProjectRoot
Initialize-PprBotLogs $ProjectRoot
Write-PprBotLog $ProjectRoot "startup" "bot_only stop requested."
Stop-FromPidFile "backend"
Stop-FromPidFile "bot"
Stop-FromPidFile "bot-root"

Start-Sleep -Seconds 2
if (Test-PprBotPortListening 8000) {
    Warn "Backend port 8000 is still listening."
    Write-PprBotLog $ProjectRoot "startup" "Backend port 8000 is still listening after stop." -Error
} else {
    Write-PprBotLog $ProjectRoot "startup" "Backend port 8000 is free after stop."
}
if ((Get-PprBotRunnerProcesses).Count -gt 0) {
    Warn "A bot runner process is still present."
    Write-PprBotLog $ProjectRoot "startup" "A bot runner process is still present after stop." -Error
} else {
    Write-PprBotLog $ProjectRoot "startup" "No bot runner process remains after stop."
}

if ($StopDatabase) {
    Info "Stopping PostgreSQL container..."
    & docker compose stop db
} else {
    Info "PostgreSQL was left running. Use -StopDatabase to stop it."
}

Write-Host ""
Write-Host "PPRBot bot_only stop completed." -ForegroundColor Green
Write-PprBotLog $ProjectRoot "startup" "bot_only stop completed."
