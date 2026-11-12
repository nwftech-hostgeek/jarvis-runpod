# Durable poll-and-deploy for jarvis-runpod image build.
# Detach-friendly: polls Actions, confirms GHCR :0.1.0, then runs deploy-runpod.ps1 once.
# Does not create RunPod resources until the image exists.
$ErrorActionPreference = 'Stop'

$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Set-Location -LiteralPath $root

$logPath = Join-Path $PSScriptRoot '.poll-deploy-status.txt'
$donePath = Join-Path $PSScriptRoot '.poll-deploy-done.txt'
$lockPath = Join-Path $PSScriptRoot '.poll-deploy.lock'
$failPath = Join-Path $PSScriptRoot '.poll-deploy-failed.txt'

function Write-Status([string]$msg) {
    $line = "$(Get-Date -Format o) $msg"
    Add-Content -LiteralPath $logPath -Value $line
    Write-Output $line
}

if (Test-Path -LiteralPath $donePath) {
    Write-Status "Deploy already marked done ($donePath). Exiting."
    exit 0
}

# Single-instance lock (stale locks older than 20m are reclaimable)
$lockStaleMinutes = 20
if (Test-Path -LiteralPath $lockPath) {
    $age = (Get-Date) - (Get-Item -LiteralPath $lockPath).LastWriteTime
    if ($age.TotalMinutes -lt $lockStaleMinutes) {
        $existing = Get-Content -LiteralPath $lockPath -ErrorAction SilentlyContinue | Select-Object -First 1
        Write-Status "Another watcher holds the lock ($existing). Exiting."
        exit 0
    }
    Write-Status "Reclaiming stale lock (age=$([int]$age.TotalMinutes)m)"
}

$lockPayload = "pid=$PID started=$(Get-Date -Format o)"
Set-Content -LiteralPath $lockPath -Value $lockPayload -Encoding utf8

try {
    "==== watcher start $(Get-Date -Format o) pid=$PID ====" | Add-Content -LiteralPath $logPath
    Write-Status 'Starting detached durable poll-and-deploy watcher (12h outer budget)'

    $map = @{}
    foreach ($line in Get-Content (Join-Path $root '.env')) {
        if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)=(.*)$') {
            $map[$Matches[1]] = $Matches[2].Trim()
        }
    }
    $headers = @{
        Authorization           = "Bearer $($map['GITHUB_TOKEN'])"
        Accept                  = 'application/vnd.github+json'
        'X-GitHub-Api-Version'  = '2022-11-28'
        'User-Agent'            = 'jarvis-runpod-deployer'
    }
    $owner = $map['GITHUB_USERNAME']
    $repo = $map['GITHUB_REPOSITORY']
    $uri = "https://api.github.com/repos/$owner/$repo/actions/workflows/build-runpod.yml/runs?per_page=1"
    $pkg = 'jarvis-runpod'
    $tag = '0.1.0'

    function Test-GhcrTag {
        foreach ($base in @(
                "https://api.github.com/orgs/$owner/packages/container/$pkg/versions?per_page=20",
                "https://api.github.com/users/$owner/packages/container/$pkg/versions?per_page=20"
            )) {
            try {
                $versions = Invoke-RestMethod -Uri $base -Headers $headers
                foreach ($v in @($versions)) {
                    if (@($v.metadata.container.tags) -contains $tag) { return $true }
                }
            }
            catch {}
        }
        try {
            $mh = @{
                Authorization = "Bearer $($map['GITHUB_TOKEN'])"
                Accept        = 'application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.list.v2+json, application/vnd.docker.distribution.manifest.v2+json'
                'User-Agent'  = 'jarvis-runpod-deployer'
            }
            $resp = Invoke-WebRequest -Uri "https://ghcr.io/v2/$owner/$pkg/manifests/$tag" -Headers $mh -Method Head
            if ($resp.StatusCode -ge 200 -and $resp.StatusCode -lt 300) { return $true }
        }
        catch {}
        return $false
    }

    $globalDeadline = (Get-Date).AddHours(12)
    $cycle = 0
    while ((Get-Date) -lt $globalDeadline) {
        $cycle++
        # Touch lock so peers know we are alive
        Set-Content -LiteralPath $lockPath -Value "pid=$PID cycle=$cycle touched=$(Get-Date -Format o)" -Encoding utf8

        $runs = Invoke-RestMethod -Uri $uri -Headers $headers
        $run = @($runs.workflow_runs) | Select-Object -First 1
        if (-not $run) {
            Write-Status "cycle=$cycle No workflow runs yet"
            Start-Sleep -Seconds 45
            continue
        }

        Write-Status "cycle=$cycle id=$($run.id) status=$($run.status) conclusion=$($run.conclusion) url=$($run.html_url)"

        if ($run.status -eq 'completed') {
            if ($run.conclusion -ne 'success') {
                $msg = "Build failed with conclusion=$($run.conclusion). See $($run.html_url)"
                Set-Content -LiteralPath $failPath -Value $msg -Encoding utf8
                throw $msg
            }

            if (-not (Test-GhcrTag)) {
                Write-Status 'Build succeeded but GHCR tag 0.1.0 not visible yet; retrying tag check'
                $tagDeadline = (Get-Date).AddMinutes(30)
                while ((Get-Date) -lt $tagDeadline) {
                    Start-Sleep -Seconds 30
                    if (Test-GhcrTag) { break }
                    Write-Status 'Waiting for GHCR tag 0.1.0 to appear'
                }
                if (-not (Test-GhcrTag)) {
                    throw 'Build succeeded but ghcr.io/nwftech-hostgeek/jarvis-runpod:0.1.0 was not found after wait.'
                }
            }

            Write-Status 'Image tag 0.1.0 confirmed. Starting deploy-runpod.ps1'
            & (Join-Path $PSScriptRoot 'deploy-runpod.ps1')
            Set-Content -LiteralPath $donePath -Value "deployed=$(Get-Date -Format o) run=$($run.id)" -Encoding utf8
            Write-Status 'Deploy script finished successfully'
            exit 0
        }

        # Self-restart cadence marker every ~90 minutes of waiting
        if (($cycle % 90) -eq 0) {
            Write-Status "Self-restart checkpoint cycle=$cycle (still waiting on build)"
        }

        Start-Sleep -Seconds 60
    }

    throw 'Timed out waiting for the GitHub Actions image build (detached watcher 12h).'
}
finally {
    if (Test-Path -LiteralPath $lockPath) {
        $cur = Get-Content -LiteralPath $lockPath -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($cur -and $cur -match "pid=$PID") {
            Remove-Item -LiteralPath $lockPath -Force -ErrorAction SilentlyContinue
        }
    }
}
