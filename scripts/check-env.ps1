$ErrorActionPreference = "Stop"

if (-not (Test-Path ".env")) {
    throw ".env is missing; copy .env.example to .env first."
}

$values = @{}
foreach ($line in Get-Content ".env") {
    if ($line -match "^\s*([A-Za-z_][A-Za-z0-9_]*)=(.*)$") {
        $values[$Matches[1]] = $Matches[2].Trim()
    }
}

$failed = $false
foreach ($name in @(
    "WEBUI_SECRET_KEY",
    "LLAMA_API_KEY",
    "VOICE_API_KEY",
    "TOOL_API_KEY",
    "TS_AUTHKEY"
)) {
    $value = $values[$name]
    if (-not $value -or $value.StartsWith("change-me")) {
        Write-Error "$name must be replaced in .env" -ErrorAction Continue
        $failed = $true
    }
}

$webuiKey = [string]$values["WEBUI_SECRET_KEY"]
$apiKeys = @(
    [string]$values["LLAMA_API_KEY"],
    [string]$values["VOICE_API_KEY"],
    [string]$values["TOOL_API_KEY"]
)
if (
    $webuiKey.Length -lt 32 -or
    ($apiKeys | Where-Object { $_.Length -lt 20 }).Count -gt 0
) {
    Write-Error "WEBUI_SECRET_KEY must be 32+ characters; API keys must be 20+" `
        -ErrorAction Continue
    $failed = $true
}
if (($apiKeys | Select-Object -Unique).Count -ne $apiKeys.Count) {
    Write-Error "LLAMA_API_KEY, VOICE_API_KEY, and TOOL_API_KEY must be unique" `
        -ErrorAction Continue
    $failed = $true
}

if ($failed) {
    exit 1
}

docker compose config --quiet
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

Write-Host ".env and Docker Compose configuration passed validation."
