param(
    [Parameter(Mandatory = $true)]
    [string]$LogPath
)

. (Join-Path $PSScriptRoot "bot-only-common.ps1")

begin {
    $directory = Split-Path -Parent $LogPath
    if (-not (Test-Path $directory)) {
        New-Item -ItemType Directory -Path $directory -Force | Out-Null
    }
}

process {
    Rotate-PprBotLog $LogPath
    Add-Content -LiteralPath $LogPath -Value $_ -Encoding UTF8
}
