param(
    [switch]$StopDatabase
)

$ErrorActionPreference = "Continue"

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$RuntimeDir = Join-Path $ProjectRoot ".runtime"

function Info($Message) {
    Write-Host "[INFO] $Message" -ForegroundColor Cyan
}

function Warn($Message) {
    Write-Host "[WARN] $Message" -ForegroundColor Yellow
}

function Stop-ProcessTree($ProcessIdValue) {
    try {
        $children = @(Get-CimInstance Win32_Process -Filter "ParentProcessId=$ProcessIdValue" -ErrorAction Stop)
        foreach ($child in $children) {
            Stop-ProcessTree ([int]$child.ProcessId)
        }
    } catch {
        # Stop parent below even if child discovery is unavailable.
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

    $raw = (Get-Content -Path $path -ErrorAction SilentlyContinue | Select-Object -First 1)
    $processIdValue = 0
    if ([int]::TryParse($raw, [ref]$processIdValue)) {
        Stop-ProcessTree $processIdValue
    } else {
        Warn "$Name pid file is invalid: $raw"
    }
    Remove-Item -Path $path -Force -ErrorAction SilentlyContinue
}

Set-Location -LiteralPath $ProjectRoot

Stop-FromPidFile "backend"
Stop-FromPidFile "frontend"
Stop-FromPidFile "bot"
Stop-FromPidFile "cloudflared"

if ($StopDatabase) {
    Info "Stopping PostgreSQL container..."
    & docker compose stop db
} else {
    Info "PostgreSQL was left running. Use -StopDatabase to stop it."
}

Write-Host ""
Write-Host "PPRBot stop completed." -ForegroundColor Green
