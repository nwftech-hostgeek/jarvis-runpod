[CmdletBinding()]
param(
    [string]$DataRoot
)

$ErrorActionPreference = "Stop"

function Read-DotEnvValue {
    param([string]$Name)
    if (-not (Test-Path ".env")) { return $null }
    foreach ($line in Get-Content ".env") {
        if ($line -match "^\s*$([regex]::Escape($Name))=(.*)$") {
            return $Matches[1].Trim()
        }
    }
    return $null
}

if (-not $DataRoot) {
    $DataRoot = Read-DotEnvValue "JARVIS_DATA_ROOT"
}
if (-not $DataRoot) {
    $DataRoot = ".\data"
}

$fullRoot = [IO.Path]::GetFullPath($DataRoot)
if ($fullRoot -eq [IO.Path]::GetPathRoot($fullRoot)) {
    throw "Refusing to use a filesystem root as JARVIS_DATA_ROOT: $fullRoot"
}

@(
    "$fullRoot\models",
    "$fullRoot\open-webui",
    "$fullRoot\voice-cache",
    "$fullRoot\tailscale",
    (Join-Path (Get-Location) "secrets")
) | ForEach-Object {
    New-Item -ItemType Directory -Path $_ -Force | Out-Null
}

Write-Host "Persistent directories are ready under $fullRoot"
