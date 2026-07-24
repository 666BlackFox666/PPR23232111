param()

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$BackupDir = Join-Path $ProjectRoot "backups"
. (Join-Path $PSScriptRoot "bot-only-common.ps1")

Set-Location -LiteralPath $ProjectRoot
Initialize-PprBotLogs $ProjectRoot
if (-not (Test-Path $BackupDir)) {
    New-Item -ItemType Directory -Path $BackupDir -Force | Out-Null
}

$postgresHealth = ((& docker inspect -f "{{.State.Health.Status}}" pprbot-postgres 2>$null) | Out-String).Trim()
if ($postgresHealth -ne "healthy") {
    Write-PprBotLog $ProjectRoot "errors" "Database backup aborted: PostgreSQL is not healthy." -Error
    throw "PostgreSQL container is not healthy."
}

$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$fileName = "pprbot-$stamp.dump"
$hostPath = Join-Path $BackupDir $fileName
$containerPath = "/tmp/$fileName"

try {
    & docker exec pprbot-postgres pg_dump -U ppr_user -d ppr_db -Fc -f $containerPath
    if ($LASTEXITCODE -ne 0) { throw "pg_dump failed." }
    & docker cp "pprbot-postgres:$containerPath" $hostPath
    if ($LASTEXITCODE -ne 0) { throw "docker cp failed." }
    if (-not (Test-Path $hostPath) -or (Get-Item -LiteralPath $hostPath).Length -eq 0) { throw "Backup file was not created." }
} finally {
    & docker exec pprbot-postgres rm -f $containerPath 2>$null
}

$oldBackups = Get-ChildItem -LiteralPath $BackupDir -Filter "pprbot-*.dump" -File | Sort-Object LastWriteTime -Descending | Select-Object -Skip 14
foreach ($oldBackup in $oldBackups) {
    Remove-Item -LiteralPath $oldBackup.FullName -Force
}

$sizeMb = [math]::Round(((Get-Item -LiteralPath $hostPath).Length / 1MB), 2)
Write-PprBotLog $ProjectRoot "startup" "Database backup created: $fileName ($sizeMb MB)."
Write-Host "Backup created: $hostPath ($sizeMb MB)" -ForegroundColor Green
