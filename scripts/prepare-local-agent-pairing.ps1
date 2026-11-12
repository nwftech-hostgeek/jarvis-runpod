#Requires -Version 5.1
<#
.SYNOPSIS
  Ensure JARVIS_LOCAL_AGENT_TOKEN exists in .env and write PC pair.env for the live Pod.

.DESCRIPTION
  Does not print the full token. Updates RunPod template ports/env when
  -UpdateTemplate is set (requires RUNPOD_API_KEY). Cloud code still needs a
  rebuilt image (or manual Pod file copy) before the WS bridge is live.
#>
[CmdletBinding()]
param(
    [switch]$UpdateTemplate,
    [string]$PodId = ""
)

$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$envPath = Join-Path $root ".env"
if (-not (Test-Path -LiteralPath $envPath)) {
    throw ".env not found at $envPath"
}

function Read-DotEnv([string]$Path) {
    $values = @{}
    foreach ($line in Get-Content -LiteralPath $Path) {
        if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)=(.*)$') {
            $value = $Matches[2].Trim()
            if (
                $value.Length -ge 2 -and
                (
                    ($value.StartsWith('"') -and $value.EndsWith('"')) -or
                    ($value.StartsWith("'") -and $value.EndsWith("'"))
                )
            ) {
                $value = $value.Substring(1, $value.Length - 2)
            }
            $values[$Matches[1]] = $value
        }
    }
    return $values
}

function Set-DotEnvValue([string]$Path, [string]$Name, [string]$Value) {
    $lines = @(Get-Content -LiteralPath $Path)
    $found = $false
    for ($i = 0; $i -lt $lines.Count; $i++) {
        if ($lines[$i] -match ("^\s*" + [regex]::Escape($Name) + "=")) {
            $lines[$i] = "$Name=$Value"
            $found = $true
            break
        }
    }
    if (-not $found) {
        $lines += "$Name=$Value"
    }
    Set-Content -LiteralPath $Path -Value $lines -Encoding utf8
}

$config = Read-DotEnv $envPath
$token = [string]$config["JARVIS_LOCAL_AGENT_TOKEN"]
if (
    $token.Length -lt 20 -or
    $token.StartsWith("change-me")
) {
    $bytes = New-Object byte[] 32
    [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    $token = -join ($bytes | ForEach-Object { $_.ToString("x2") })
    Set-DotEnvValue -Path $envPath -Name "JARVIS_LOCAL_AGENT_TOKEN" -Value $token
    Write-Host "Generated JARVIS_LOCAL_AGENT_TOKEN in .env (not printed)."
}
else {
    Write-Host "JARVIS_LOCAL_AGENT_TOKEN already set in .env."
}

Set-DotEnvValue -Path $envPath -Name "LOCAL_AGENT_ENABLED" -Value "true"
Set-DotEnvValue -Path $envPath -Name "LOCAL_AGENT_LISTEN" -Value "0.0.0.0"

if (-not $PodId -and (Test-Path (Join-Path $root ".deployment\runpod.json"))) {
    $rec = Get-Content (Join-Path $root ".deployment\runpod.json") -Raw | ConvertFrom-Json
    $PodId = [string]$rec.podId
}

$toolsUrl = "https://$PodId-8090.proxy.runpod.net"
$pairDir = Join-Path $env:APPDATA "jarvis-local-agent"
New-Item -ItemType Directory -Force -Path $pairDir | Out-Null
$pairPath = Join-Path $pairDir "pair.env"
Set-Content -LiteralPath $pairPath -Value @(
    "JARVIS_TOOLS_URL=$toolsUrl"
    "JARVIS_LOCAL_AGENT_TOKEN=$token"
) -Encoding utf8
Write-Host "Wrote pairing file: $pairPath"
Write-Host "Tools URL: $toolsUrl"

if ($UpdateTemplate) {
    $runpodKey = [string]$config["RUNPOD_API_KEY"]
    if ($runpodKey.Length -lt 20) {
        throw "RUNPOD_API_KEY missing; cannot update template."
    }
    # Reuse deploy script's template update path by invoking TemplateOnly after env is ready.
    Write-Host "Updating RunPod template (ports 3000+8090, local-agent env) via deploy-runpod.ps1 -TemplateOnly..."
    & (Join-Path $PSScriptRoot "deploy-runpod.ps1") -TemplateOnly
}

Write-Host ""
Write-Host "Next:"
Write-Host "  1. Rebuild/push jarvis-runpod image with tools/local_agent_bridge.py (GH Actions), then recreate Pod from the updated template — OR manually copy tools/*.py onto the Pod and restart jarvis-tools."
Write-Host "  2. Install Python 3.11+ on this PC, then: .\scripts\start-local-agent.ps1"
Write-Host "  3. Paste prompts/jarvis-system.md into the Open WebUI model; add openwebui-tools/jarvis_local.py if desired."
