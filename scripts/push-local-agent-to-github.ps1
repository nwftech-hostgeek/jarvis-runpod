#Requires -Version 5.1
<#
.SYNOPSIS
  Commit local-agent related files to the private GitHub repo via the Contents API
  (no local git required), then optionally dispatch the RunPod image workflow.
#>
[CmdletBinding()]
param(
    [switch]$DispatchWorkflow
)

$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$config = @{}
foreach ($line in Get-Content (Join-Path $root ".env")) {
    if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)=(.*)$') {
        $config[$Matches[1]] = $Matches[2].Trim().Trim('"').Trim("'")
    }
}
$owner = $config["GITHUB_USERNAME"]
$repo = $config["GITHUB_REPOSITORY"]
$token = $config["GITHUB_TOKEN"]
if (-not $owner -or -not $repo -or $token.Length -lt 20) {
    throw "GITHUB_USERNAME / GITHUB_REPOSITORY / GITHUB_TOKEN required in .env"
}

$headers = @{
    Authorization          = "Bearer $token"
    Accept                 = "application/vnd.github+json"
    "X-GitHub-Api-Version" = "2022-11-28"
    "User-Agent"           = "jarvis-local-agent-pusher"
}

$paths = @(
    "tools/app.py",
    "tools/local_agent_bridge.py",
    "tools/requirements.txt",
    "local-agent/requirements.txt",
    "local-agent/config.example.yaml",
    "local-agent/pair.py",
    "local-agent/jarvis_local_agent/__init__.py",
    "local-agent/jarvis_local_agent/__main__.py",
    "local-agent/jarvis_local_agent/agent.py",
    "local-agent/jarvis_local_agent/config.py",
    "local-agent/jarvis_local_agent/confirm.py",
    "local-agent/jarvis_local_agent/safe_cmds.py",
    "local-agent/jarvis_local_agent/ssh_exec.py",
    "scripts/start-local-agent.ps1",
    "scripts/prepare-local-agent-pairing.ps1",
    "scripts/push-local-agent-to-github.ps1",
    "scripts/deploy-runpod.ps1",
    "docs/local-agent.md",
    "prompts/jarvis-system.md",
    "openwebui-tools/jarvis_local.py",
    "config/ssh-targets.yaml",
    ".env.example",
    ".gitignore",
    "README.md",
    "docker-compose.yml",
    "docker/tools/Dockerfile",
    "docker/runpod/Dockerfile",
    "docker/runpod/supervisord.conf",
    "docker/runpod/entrypoint.sh",
    "web/jarvis-orb/app.js"
)

function Get-RemoteSha([string]$Path) {
    $uri = "https://api.github.com/repos/$owner/$repo/contents/$($Path.Replace('\','/'))"
    try {
        $resp = Invoke-RestMethod -Method Get -Uri $uri -Headers $headers
        return [string]$resp.sha
    }
    catch {
        $status = $_.Exception.Response.StatusCode.value__
        if ($status -eq 404) { return $null }
        throw
    }
}

foreach ($rel in $paths) {
    $full = Join-Path $root $rel
    if (-not (Test-Path -LiteralPath $full)) {
        Write-Warning "Missing locally: $rel"
        continue
    }
    $bytes = [IO.File]::ReadAllBytes($full)
    $b64 = [Convert]::ToBase64String($bytes)
    $remoteSha = Get-RemoteSha $rel
    $body = @{
        message = "feat: Windows local-agent LAN bridge ($rel)"
        content = $b64
        branch  = "main"
    }
    if ($remoteSha) { $body.sha = $remoteSha }
    $uri = "https://api.github.com/repos/$owner/$repo/contents/$($rel.Replace('\','/'))"
    Write-Host "Uploading $rel ..."
    [void](Invoke-RestMethod -Method Put -Uri $uri -Headers $headers -ContentType "application/json" -Body (
        $body | ConvertTo-Json -Compress -Depth 5
    ))
}

Write-Host "Upload complete."

if ($DispatchWorkflow) {
    $dispatchUri = "https://api.github.com/repos/$owner/$repo/actions/workflows/build-runpod.yml/dispatches"
    Invoke-RestMethod -Method Post -Uri $dispatchUri -Headers $headers -ContentType "application/json" -Body '{"ref":"main"}'
    Write-Host "Dispatched build-runpod.yml on main."
}
