# Durable poll for build-runpod.yml success, then deploy-runpod.ps1.
# Does not print secrets. Safe to re-run (deploy script is idempotent for existing jarvis pod).
$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Set-Location -LiteralPath $root

$logPath = Join-Path $PSScriptRoot '.poll-deploy-status.txt'
function Write-Status([string]$msg) {
    $line = "$(Get-Date -Format o) $msg"
    Add-Content -LiteralPath $logPath -Value $line
    Write-Output $line
}

"==== start $(Get-Date -Format o) pid=$PID ====" | Add-Content -LiteralPath $logPath
Write-Status 'Durable poll-and-deploy watcher started'

$map = @{}
foreach ($line in Get-Content (Join-Path $root '.env')) {
    if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)=(.*)$') {
        $map[$Matches[1]] = $Matches[2].Trim()
    }
}
$headers = @{
    Authorization          = "Bearer $($map['GITHUB_TOKEN'])"
    Accept                 = 'application/vnd.github+json'
    'X-GitHub-Api-Version' = '2022-11-28'
    'User-Agent'           = 'jarvis-runpod-deployer'
}
$owner = $map['GITHUB_USERNAME']
$repo = $map['GITHUB_REPOSITORY']
$uri = "https://api.github.com/repos/$owner/$repo/actions/workflows/build-runpod.yml/runs?per_page=1"
$deadline = (Get-Date).AddMinutes(180)

try {
    do {
        $runs = Invoke-RestMethod -Uri $uri -Headers $headers
        $run = @($runs.workflow_runs) | Select-Object -First 1
        if (-not $run) {
            Write-Status 'No workflow runs yet'
            Start-Sleep -Seconds 30
            continue
        }
        Write-Status "id=$($run.id) status=$($run.status) conclusion=$($run.conclusion) url=$($run.html_url)"

        if ($run.status -eq 'completed') {
            if ($run.conclusion -ne 'success') {
                throw "Build failed with conclusion=$($run.conclusion). See $($run.html_url)"
            }

            $pkg = 'jarvis-runpod'
            $tagOk = $false
            foreach ($base in @(
                    "https://api.github.com/orgs/$owner/packages/container/$pkg/versions?per_page=20",
                    "https://api.github.com/users/$owner/packages/container/$pkg/versions?per_page=20"
                )) {
                try {
                    $versions = Invoke-RestMethod -Uri $base -Headers $headers
                    foreach ($v in @($versions)) {
                        if (@($v.metadata.container.tags) -contains '0.1.1') {
                            $tagOk = $true
                            break
                        }
                    }
                    if ($tagOk) { break }
                }
                catch {}
            }
            if (-not $tagOk) {
                try {
                    $mh = @{
                        Authorization = "Bearer $($map['GITHUB_TOKEN'])"
                        Accept        = 'application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.list.v2+json, application/vnd.docker.distribution.manifest.v2+json'
                        'User-Agent'  = 'jarvis-runpod-deployer'
                    }
                    $resp = Invoke-WebRequest -Uri "https://ghcr.io/v2/$owner/$pkg/manifests/0.1.1" -Headers $mh -Method Head
                    if ($resp.StatusCode -ge 200 -and $resp.StatusCode -lt 300) { $tagOk = $true }
                }
                catch {}
            }
            if (-not $tagOk) {
                throw 'Build succeeded but ghcr.io/nwftech-hostgeek/jarvis-runpod:0.1.1 was not found.'
            }

            Write-Status 'Image tag 0.1.1 confirmed. Starting deploy-runpod.ps1'
            & (Join-Path $PSScriptRoot 'deploy-runpod.ps1') -ForceRestart -NetworkVolumeId '3s1rieoxf6'
            Write-Status 'Deploy script finished successfully'
            exit 0
        }

        Start-Sleep -Seconds 60
    } while ((Get-Date) -lt $deadline)

    throw 'Timed out waiting for the GitHub Actions image build (durable watcher 180m).'
}
catch {
    Write-Status "FAILED: $($_.Exception.Message)"
    throw
}
