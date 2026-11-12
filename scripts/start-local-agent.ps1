#Requires -Version 5.1
<#
.SYNOPSIS
  Start the Windows Jarvis local companion agent (LAN SSH / ping / resolve bridge).

.DESCRIPTION
  Loads pairing config from %APPDATA%\jarvis-local-agent\pair.env (or the
  process environment), ensures a venv + deps under local-agent\.venv, then
  runs the agent. Keeps a console open for approve/deny prompts.
#>
[CmdletBinding()]
param(
    [string]$ConfigDir = "",
    [switch]$InstallOnly,
    [switch]$Pair,
    [string]$ToolsUrl = "",
    [string]$Token = ""
)

$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$agentRoot = Join-Path $root "local-agent"

function Find-Python {
    foreach ($candidate in @(
            "python",
            "python3",
            "$env:LocalAppData\Programs\Python\Python312\python.exe",
            "$env:LocalAppData\Programs\Python\Python311\python.exe",
            "$env:LocalAppData\Programs\Python\Python313\python.exe"
        )) {
        try {
            if ($candidate -match '[\\/]' -and -not (Test-Path -LiteralPath $candidate)) {
                continue
            }
            $ver = & $candidate -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>$null
            if ($LASTEXITCODE -eq 0 -and $ver -match '^[3]\.(1[1-9]|[2-9][0-9])') {
                return $candidate
            }
        }
        catch {}
    }
    return $null
}

function Import-PairEnv {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) { return }
    foreach ($line in Get-Content -LiteralPath $Path) {
        if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)=(.*)$') {
            $name = $Matches[1]
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
            Set-Item -Path "Env:$name" -Value $value
        }
    }
}

if (-not $ConfigDir) {
    $ConfigDir = Join-Path $env:APPDATA "jarvis-local-agent"
}
New-Item -ItemType Directory -Force -Path $ConfigDir | Out-Null
$env:JARVIS_LOCAL_AGENT_CONFIG_DIR = $ConfigDir

Import-PairEnv -Path (Join-Path $ConfigDir "pair.env")

$python = Find-Python
if (-not $python) {
    Write-Error @"
Python 3.11+ was not found on PATH.
Install from https://www.python.org/downloads/windows/ (check 'Add python.exe to PATH'),
then re-run this script.
"@
}

$venvPython = Join-Path $agentRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $venvPython)) {
    Write-Host "Creating venv at $agentRoot\.venv"
    & $python -m venv (Join-Path $agentRoot ".venv")
}
& $venvPython -m pip install --upgrade pip --quiet
& $venvPython -m pip install -r (Join-Path $agentRoot "requirements.txt") --quiet

if ($Pair -or $ToolsUrl) {
    if (-not $ToolsUrl) {
        throw "Pass -ToolsUrl when using -Pair (e.g. https://PODID-8090.proxy.runpod.net)"
    }
    $pairArgs = @((Join-Path $agentRoot "pair.py"), "--tools-url", $ToolsUrl)
    if ($Token) { $pairArgs += @("--token", $Token) }
    & $venvPython @pairArgs
    Import-PairEnv -Path (Join-Path $ConfigDir "pair.env")
}

if ($InstallOnly) {
    Write-Host "Dependencies installed. Pairing/config dir: $ConfigDir"
    exit 0
}

if (-not $env:JARVIS_TOOLS_URL -and -not $env:JARVIS_LOCAL_AGENT_TOOLS_URL) {
    Write-Error @"
JARVIS_TOOLS_URL is not set.
Run once:
  .\scripts\start-local-agent.ps1 -Pair -ToolsUrl 'https://<POD_ID>-8090.proxy.runpod.net'
Or set JARVIS_LOCAL_AGENT_TOKEN + JARVIS_TOOLS_URL in $ConfigDir\pair.env
See docs\local-agent.md
"@
}

if (-not $env:JARVIS_LOCAL_AGENT_TOKEN -and -not $env:LOCAL_AGENT_TOKEN) {
    Write-Error "JARVIS_LOCAL_AGENT_TOKEN is not set. Pair first (see docs/local-agent.md)."
}

Write-Host "Starting Jarvis local agent (Ctrl+C to stop). Approve/deny prompts appear here."
Set-Location -LiteralPath $agentRoot
& $venvPython -m jarvis_local_agent
