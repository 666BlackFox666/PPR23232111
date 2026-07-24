$script:PprBotLogMaxBytes = 5MB
$script:PprBotLogMaxFiles = 10

function Get-PprBotEnvValues($Path) {
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
        $result[$key] = $trimmed.Substring($index + 1).Trim().Trim('"').Trim("'")
    }
    return $result
}

function Get-PprBotEnvValue($Values, $Key) {
    if ($Values.ContainsKey($Key)) {
        return $Values[$Key]
    }
    return ""
}

function Assert-PprBotOnlyProductionEnv($EnvFile) {
    $values = Get-PprBotEnvValues $EnvFile
    $errors = @()
    $required = @(
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_CHAT_ID",
        "DATABASE_URL",
        "ADMIN_TELEGRAM_IDS"
    )

    if ((Get-PprBotEnvValue $values "DEPLOYMENT_MODE").ToLowerInvariant() -ne "bot_only") {
        $errors += "DEPLOYMENT_MODE must be bot_only"
    }
    if ((Get-PprBotEnvValue $values "TELEGRAM_ENABLED").ToLowerInvariant() -ne "true") {
        $errors += "TELEGRAM_ENABLED must be true"
    }
    foreach ($key in $required) {
        if ([string]::IsNullOrWhiteSpace((Get-PprBotEnvValue $values $key))) {
            $errors += "$key must be set"
        }
    }
    if ((Get-PprBotEnvValue $values "DEV_COMMANDS_ENABLED").ToLowerInvariant() -ne "false") {
        $errors += "DEV_COMMANDS_ENABLED must be false"
    }
    if ((Get-PprBotEnvValue $values "AUTO_SEND_ALLOW_MASS").ToLowerInvariant() -ne "false") {
        $errors += "AUTO_SEND_ALLOW_MASS must be false"
    }
    $massLimit = 0
    if (-not [int]::TryParse((Get-PprBotEnvValue $values "AUTO_SEND_MASS_LIMIT"), [ref]$massLimit) -or $massLimit -lt 1 -or $massLimit -gt 10) {
        $errors += "AUTO_SEND_MASS_LIMIT must be between 1 and 10"
    }
    $autoSendEnabled = (Get-PprBotEnvValue $values "NOTIFICATIONS_AUTO_SEND_ENABLED").ToLowerInvariant() -eq "true"
    $pilotAllowed = (Get-PprBotEnvValue $values "PILOT_AUTO_SEND_ALLOWED").ToLowerInvariant() -eq "true"
    if ($autoSendEnabled -and -not $pilotAllowed) {
        $errors += "NOTIFICATIONS_AUTO_SEND_ENABLED=true requires PILOT_AUTO_SEND_ALLOWED=true"
    }
    if ($autoSendEnabled -and $pilotAllowed) {
        Write-Warning "Pilot auto-send is enabled. Messages will be sent to the configured Telegram chat."
    }

    if ($errors.Count) {
        throw ("bot_only production configuration is invalid:`n - " + ($errors -join "`n - "))
    }
    return $values
}

function Initialize-PprBotLogs($ProjectRoot) {
    $logDir = Join-Path $ProjectRoot "logs"
    if (-not (Test-Path $logDir)) {
        New-Item -ItemType Directory -Path $logDir -Force | Out-Null
    }
    foreach ($name in @("backend", "bot", "startup", "errors")) {
        Rotate-PprBotLog (Join-Path $logDir "$name.log")
        if (-not (Test-Path (Join-Path $logDir "$name.log"))) {
            New-Item -ItemType File -Path (Join-Path $logDir "$name.log") -Force | Out-Null
        }
    }
}

function Rotate-PprBotLog($LogPath) {
    if (-not (Test-Path $LogPath)) {
        return
    }
    $file = Get-Item -LiteralPath $LogPath
    if ($file.Length -lt $script:PprBotLogMaxBytes) {
        return
    }

    $oldest = "$LogPath.$($script:PprBotLogMaxFiles - 1)"
    Remove-Item -LiteralPath $oldest -Force -ErrorAction SilentlyContinue
    for ($index = $script:PprBotLogMaxFiles - 2; $index -ge 1; $index--) {
        $source = "$LogPath.$index"
        if (Test-Path $source) {
            Move-Item -LiteralPath $source -Destination "$LogPath.$($index + 1)" -Force
        }
    }
    Move-Item -LiteralPath $LogPath -Destination "$LogPath.1" -Force
}

function Write-PprBotLog($ProjectRoot, $Name, $Message, [switch]$Error) {
    $logDir = Join-Path $ProjectRoot "logs"
    if (-not (Test-Path $logDir)) {
        New-Item -ItemType Directory -Path $logDir -Force | Out-Null
    }
    $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    $line = "[$timestamp] $Message"
    Add-Content -LiteralPath (Join-Path $logDir "$Name.log") -Value $line -Encoding UTF8
    if ($Error) {
        Add-Content -LiteralPath (Join-Path $logDir "errors.log") -Value $line -Encoding UTF8
    }
}

function Get-PprBotRunnerRecords() {
    try {
        return @(Get-CimInstance Win32_Process -ErrorAction Stop | Where-Object {
            $_.CommandLine -like "*app.bot.runner*" -and
            ($_.Name -match '^python(w)?\.exe$' -or $_.ExecutablePath -like '*\.venv\Scripts\python.exe')
        } | ForEach-Object {
            [pscustomobject]@{
                ProcessId = [int]$_.ProcessId
                ParentProcessId = [int]$_.ParentProcessId
                Name = $_.Name
                CommandLine = $_.CommandLine
                ExecutablePath = $_.ExecutablePath
            }
        })
    } catch {
        # Some locked-down Windows installations deny Win32_Process access.
        return $null
    }
}

function Get-PprBotPidFallback() {
    $pidPath = Join-Path (Split-Path -Parent $PSScriptRoot) ".runtime\bot-only\bot.pid"
    if (-not (Test-Path -LiteralPath $pidPath)) {
        return $null
    }
    $rawPid = Get-Content -LiteralPath $pidPath -ErrorAction SilentlyContinue | Select-Object -First 1
    $pidValue = 0
    if (-not [int]::TryParse($rawPid, [ref]$pidValue)) {
        Remove-Item -LiteralPath $pidPath -Force -ErrorAction SilentlyContinue
        return $null
    }
    $process = Get-Process -Id $pidValue -ErrorAction SilentlyContinue
    if (-not $process -or $process.ProcessName -notmatch '^python(w)?$') {
        Remove-Item -LiteralPath $pidPath -Force -ErrorAction SilentlyContinue
        return $null
    }
    return [pscustomobject]@{
        ProcessId = $pidValue
        ParentProcessId = 0
        Name = "$($process.ProcessName).exe"
        CommandLine = ""
        ExecutablePath = $null
    }
}

function Get-PprBotLogicalRunnerChains($Records) {
    $byId = @{}
    foreach ($record in @($Records)) { $byId[$record.ProcessId] = $record }
    $roots = @($Records | Where-Object { -not $byId.ContainsKey($_.ParentProcessId) })
    $runners = @()
    foreach ($root in $roots) {
        $members = @()
        $queue = [System.Collections.Generic.Queue[object]]::new()
        $queue.Enqueue([pscustomobject]@{ Record = $root; Depth = 0 })
        while ($queue.Count -gt 0) {
            $item = $queue.Dequeue()
            $members += [pscustomobject]@{ Record = $item.Record; Depth = $item.Depth }
            foreach ($child in @($Records | Where-Object { $_.ParentProcessId -eq $item.Record.ProcessId })) {
                $queue.Enqueue([pscustomobject]@{ Record = $child; Depth = $item.Depth + 1 })
            }
        }
        $effective = @($members | Sort-Object Depth -Descending)[0].Record
        $runners += [pscustomobject]@{
            ProcessId = $effective.ProcessId
            EffectiveProcessId = $effective.ProcessId
            RootProcessId = $root.ProcessId
            ProcessIds = @($members | ForEach-Object { $_.Record.ProcessId })
            Processes = @($members | ForEach-Object { $_.Record })
        }
    }
    return @($runners)
}

function Get-PprBotRunnerProcesses() {
    $records = Get-PprBotRunnerRecords
    if ($null -eq $records -or $records.Count -eq 0) {
        $fallback = Get-PprBotPidFallback
        if ($fallback) {
            return @([pscustomobject]@{
                ProcessId = $fallback.ProcessId
                EffectiveProcessId = $fallback.ProcessId
                RootProcessId = $fallback.ProcessId
                ProcessIds = @($fallback.ProcessId)
                Processes = @($fallback)
            })
        }
        return @()
    }
    return Get-PprBotLogicalRunnerChains $records
}

function Get-PprBotSchedulerWorkerPid($PythonExe, $ProjectRoot) {
    if (-not (Test-Path -LiteralPath $PythonExe)) {
        return 0
    }
    $code = "from app.db.session import SessionLocal; from app.services.telegram_sender import scheduler_status; db=SessionLocal(); worker_id=scheduler_status(db).get('worker_id') or ''; print(worker_id.split(':')[-2] if len(worker_id.split(':')) >= 3 else ''); db.close()"
    $oldPythonPath = $env:PYTHONPATH
    $env:PYTHONPATH = "backend"
    try {
        Push-Location -LiteralPath $ProjectRoot
        $workerText = (& $PythonExe -c $code 2>$null | Out-String).Trim()
        if ($LASTEXITCODE -ne 0) { return 0 }
        $workerPid = 0
        if ([int]::TryParse($workerText, [ref]$workerPid)) { return $workerPid }
        return 0
    } finally {
        Pop-Location
        $env:PYTHONPATH = $oldPythonPath
    }
}

function Get-PprBotDockerInspect($Format, $Container) {
    $docker = Get-Command docker -ErrorAction SilentlyContinue
    if (-not $docker) {
        return ""
    }
    $tempRoot = Join-Path $env:TEMP ("pprbot-docker-" + [guid]::NewGuid().ToString("N"))
    $stdoutPath = Join-Path $tempRoot "stdout.log"
    $stderrPath = Join-Path $tempRoot "stderr.log"
    New-Item -ItemType Directory -Path $tempRoot -Force | Out-Null
    try {
        $process = Start-Process -FilePath $docker.Source -ArgumentList @("inspect", "-f", $Format, $Container) `
            -RedirectStandardOutput $stdoutPath -RedirectStandardError $stderrPath -Wait -PassThru -WindowStyle Hidden
        if ($process.ExitCode -ne 0 -or -not (Test-Path $stdoutPath)) {
            return ""
        }
        return (Get-Content -LiteralPath $stdoutPath -Raw).Trim()
    } finally {
        Remove-Item -LiteralPath $tempRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
}

function Test-PprBotPortListening($Port) {
    try {
        return [bool](Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
    } catch {
        return $false
    }
}
