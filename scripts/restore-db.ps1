param(
    [Parameter(Mandatory = $true)]
    [string]$BackupPath,
    [switch]$Confirm
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot "bot-only-common.ps1")

Set-Location -LiteralPath $ProjectRoot
Initialize-PprBotLogs $ProjectRoot
if (-not $Confirm) {
    Write-Host "Restore is destructive. Re-run with -Confirm after verifying the backup path." -ForegroundColor Yellow
    exit 1
}
if (-not (Test-Path -LiteralPath $BackupPath -PathType Leaf)) {
    throw "Backup file not found: $BackupPath"
}

$postgresHealth = ((& docker inspect -f "{{.State.Health.Status}}" pprbot-postgres 2>$null) | Out-String).Trim()
if ($postgresHealth -ne "healthy") {
    throw "PostgreSQL container is not healthy."
}

$resolvedPath = (Resolve-Path -LiteralPath $BackupPath).Path
$containerPath = "/tmp/pprbot-restore-$([guid]::NewGuid().ToString('N')).dump"
Write-Host "WARNING: the current ppr_db data will be replaced from $resolvedPath" -ForegroundColor Yellow
Write-PprBotLog $ProjectRoot "startup" "Database restore requested from $(Split-Path -Leaf $resolvedPath)."

try {
    & docker cp $resolvedPath "pprbot-postgres:$containerPath"
    if ($LASTEXITCODE -ne 0) { throw "docker cp failed." }
    & docker exec pprbot-postgres pg_restore --clean --if-exists -U ppr_user -d ppr_db $containerPath
    if ($LASTEXITCODE -ne 0) { throw "pg_restore failed." }
} finally {
    & docker exec pprbot-postgres rm -f $containerPath 2>$null
}

Write-PprBotLog $ProjectRoot "startup" "Database restore completed from $(Split-Path -Leaf $resolvedPath)."
Write-Host "Database restore completed." -ForegroundColor Green
