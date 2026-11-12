[CmdletBinding()]
param(
    [string]$DataCenter = "EU-SE-1",
    [int]$VolumeSizeGb = 100,
    [switch]$TemplateOnly
)

$ErrorActionPreference = "Stop"

function Read-DotEnv {
    param([string]$Path)
    $values = @{}
    foreach ($line in Get-Content -LiteralPath $Path) {
        if ($line -match "^\s*([A-Za-z_][A-Za-z0-9_]*)=(.*)$") {
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

function Invoke-JsonApi {
    param(
        [Parameter(Mandatory)][string]$Method,
        [Parameter(Mandatory)][string]$Uri,
        [Parameter(Mandatory)][hashtable]$Headers,
        [object]$Body
    )
    $parameters = @{
        Method      = $Method
        Uri         = $Uri
        Headers     = $Headers
        ContentType = "application/json"
    }
    if ($null -ne $Body) {
        $parameters.Body = $Body | ConvertTo-Json -Depth 20 -Compress
    }
    try {
        return Invoke-RestMethod @parameters
    }
    catch {
        $detail = $_.Exception.Message
        if ($_.ErrorDetails.Message) {
            $detail = $_.ErrorDetails.Message
        }
        throw "$Method $Uri failed: $detail"
    }
}

function Require-Value {
    param(
        [hashtable]$Config,
        [string]$Name,
        [int]$MinimumLength = 1
    )
    $value = [string]$Config[$Name]
    if (
        $value.Length -lt $MinimumLength -or
        $value.StartsWith("change-me")
    ) {
        throw "$Name is missing or still contains a placeholder."
    }
    return $value
}

$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$envPath = Join-Path $root ".env"
if (-not (Test-Path -LiteralPath $envPath)) {
    throw ".env is missing."
}
$config = Read-DotEnv -Path $envPath

$runpodKey = Require-Value -Config $config -Name "RUNPOD_API_KEY" -MinimumLength 20
$githubUser = Require-Value -Config $config -Name "GITHUB_USERNAME"
$githubToken = Require-Value -Config $config -Name "GITHUB_TOKEN" -MinimumLength 20
$githubRepo = [string]$config["GITHUB_REPOSITORY"]
if (-not $githubRepo) {
    $githubRepo = "jarvis-runpod"
}
$image = Require-Value -Config $config -Name "RUNPOD_IMAGE"

foreach ($secretName in @(
    "WEBUI_SECRET_KEY",
    "LLAMA_API_KEY",
    "VOICE_API_KEY",
    "TOOL_API_KEY"
)) {
    [void](Require-Value -Config $config -Name $secretName -MinimumLength 20)
}

$githubHeaders = @{
    Authorization          = "Bearer $githubToken"
    Accept                 = "application/vnd.github+json"
    "X-GitHub-Api-Version" = "2022-11-28"
    "User-Agent"           = "jarvis-runpod-deployer"
}
$encodedWorkflow = [Uri]::EscapeDataString("build-runpod.yml")
$runsUri = "https://api.github.com/repos/$githubUser/$githubRepo/actions/" +
    "workflows/$encodedWorkflow/runs?event=workflow_dispatch&per_page=1"
$runs = Invoke-JsonApi -Method Get -Uri $runsUri -Headers $githubHeaders
$latestRun = @($runs.workflow_runs) | Select-Object -First 1
if (-not $latestRun) {
    throw "No GitHub image-build run was found."
}
if ($latestRun.status -ne "completed" -or $latestRun.conclusion -ne "success") {
    throw "Latest image build is status=$($latestRun.status), " +
        "conclusion=$($latestRun.conclusion). Check $($latestRun.html_url)"
}

$runpodHeaders = @{ Authorization = "Bearer $runpodKey" }
$runpodBase = "https://api.runpod.io/v2"

$registryList = Invoke-JsonApi `
    -Method Get `
    -Uri "$runpodBase/registries" `
    -Headers $runpodHeaders
$registry = @($registryList.registries) |
    Where-Object { $_.name -eq "jarvis-ghcr" } |
    Select-Object -First 1
if (-not $registry) {
    Write-Host "Creating private GHCR pull credential in RunPod..."
    $registry = Invoke-JsonApi `
        -Method Post `
        -Uri "$runpodBase/registries" `
        -Headers $runpodHeaders `
        -Body @{
            name     = "jarvis-ghcr"
            username = $githubUser
            password = $githubToken
        }
}

$envNames = @(
    "RUNPOD_DATA_ROOT",
    "RUNPOD_UI_PORT",
    "WEBUI_SECRET_KEY",
    "WEBUI_AUTH",
    "ENABLE_SIGNUP",
    "DEFAULT_USER_ROLE",
    "RAG_EMBEDDING_MODEL",
    "LLAMA_API_KEY",
    "HF_TOKEN",
    "HF_REPO_ID",
    "HF_FILENAME",
    "HF_MODEL_PATTERN",
    "MODEL_PATH",
    "MODEL_ALIAS",
    "LLAMA_N_GPU_LAYERS",
    "LLAMA_BATCH_SIZE",
    "LLAMA_UBATCH_SIZE",
    "LLAMA_CACHE_TYPE_K",
    "LLAMA_CACHE_TYPE_V",
    "LLAMA_FLASH_ATTN",
    "LLAMA_THREADS",
    "LLAMA_EXTRA_ARGS",
    "VOICE_API_KEY",
    "VOICE_PRELOAD_MODELS",
    "STT_MODEL",
    "TTS_MODEL",
    "TTS_VOICE",
    "WHISPER_DEVICE",
    "WHISPER_COMPUTE_TYPE",
    "VOICE_MODEL_TTL",
    "TOOL_API_KEY",
    "SSH_ENABLE_MUTATING_COMMANDS",
    "SSH_DEFAULT_TIMEOUT",
    "SSH_MAX_OUTPUT_BYTES",
    "SSH_PRIVATE_KEY_B64",
    "SSH_TARGETS_YAML_B64",
    "SSH_KNOWN_HOSTS_B64"
)
$podEnv = @{}
foreach ($name in $envNames) {
    $value = [string]$config[$name]
    if ($value) {
        $podEnv[$name] = $value
    }
}

# The selected A40/A6000 class has 48 GB VRAM. Two slots share this total
# context, yielding approximately 32K tokens per concurrent request.
$podEnv["LLAMA_CTX_SIZE"] = "65536"
$podEnv["LLAMA_PARALLEL"] = "2"
$podEnv["WHISPER_DEVICE"] = "cpu"
$podEnv["WHISPER_COMPUTE_TYPE"] = "int8"
$podEnv["TAILSCALE_REQUIRED"] = "false"
$podEnv.Remove("TS_AUTHKEY")

$templates = Invoke-JsonApi `
    -Method Get `
    -Uri "$runpodBase/templates" `
    -Headers $runpodHeaders
$template = @($templates.templates) |
    Where-Object { $_.name -eq "Jarvis AI Assistant 0.1.0" } |
    Select-Object -First 1
if (-not $template) {
    Write-Host "Creating private RunPod template..."
    $template = Invoke-JsonApi `
        -Method Post `
        -Uri "$runpodBase/templates" `
        -Headers $runpodHeaders `
        -Body @{
            name                = "Jarvis AI Assistant 0.1.0"
            image               = $image
            args                = ""
            category            = "NVIDIA"
            disk                = 50
            ports               = @("3000/http")
            env                 = $podEnv
            registry            = $registry.id
            public              = $false
            serverless          = $false
            startSsh            = $false
            startJupyter        = $false
            allowedCudaVersions = @("12.8")
        }
}

if ($TemplateOnly) {
    Write-Host "Template ready: $($template.id)"
    exit 0
}

$existingPods = Invoke-JsonApi `
    -Method Get `
    -Uri "$runpodBase/pods" `
    -Headers $runpodHeaders
$existingPod = @($existingPods.pods) |
    Where-Object {
        $_.name -eq "jarvis" -and
        $_.status -ne "TERMINATED"
    } |
    Select-Object -First 1
if ($existingPod) {
    Write-Host "Jarvis Pod already exists: $($existingPod.id) ($($existingPod.status))"
    Write-Host "URL: https://$($existingPod.id)-3000.proxy.runpod.net"
    exit 0
}

$volumes = Invoke-JsonApi `
    -Method Get `
    -Uri "$runpodBase/network-volumes" `
    -Headers $runpodHeaders
$volume = @($volumes.networkVolumes) |
    Where-Object {
        $_.name -eq "jarvis-data" -and
        $_.dataCenter -eq $DataCenter
    } |
    Select-Object -First 1
if (-not $volume) {
    Write-Host "Creating billable $VolumeSizeGb GB STANDARD Network Volume in $DataCenter..."
    $volume = Invoke-JsonApi `
        -Method Post `
        -Uri "$runpodBase/network-volumes" `
        -Headers $runpodHeaders `
        -Body @{
            name       = "jarvis-data"
            size       = $VolumeSizeGb
            dataCenter = $DataCenter
            type       = "STANDARD"
        }
}

$gpuCandidates = @(
    "NVIDIA A40",
    "NVIDIA RTX A6000"
)
$pod = $null
$errors = @()
foreach ($gpuId in $gpuCandidates) {
    try {
        Write-Host "Requesting $gpuId in $DataCenter..."
        $pod = Invoke-JsonApi `
            -Method Post `
            -Uri "$runpodBase/pods" `
            -Headers $runpodHeaders `
            -Body @{
                name           = "jarvis"
                templateId     = $template.id
                cloud          = "SECURE"
                dataCenterIds  = @($DataCenter)
                globalNetworking = $false
                gpu            = @{
                    id    = $gpuId
                    count = 1
                }
                mounts         = @{
                    network = @(
                        @{
                            volumeId = $volume.id
                            path     = "/workspace"
                        }
                    )
                }
                startSsh       = $false
                startJupyter   = $false
            }
        break
    }
    catch {
        $errors += "$gpuId`: $($_.Exception.Message)"
    }
}

if (-not $pod) {
    throw "No requested GPU had capacity. " + ($errors -join " | ")
}

$deploymentDirectory = Join-Path $root ".deployment"
New-Item -ItemType Directory -Force -Path $deploymentDirectory | Out-Null
$record = @{
    podId          = $pod.id
    templateId     = $template.id
    registryId     = $registry.id
    networkVolumeId = $volume.id
    dataCenter     = $pod.dataCenterId
    gpu            = $pod.gpu.id
    image          = $image
    createdAt      = [DateTime]::UtcNow.ToString("o")
}
[IO.File]::WriteAllText(
    (Join-Path $deploymentDirectory "runpod.json"),
    ($record | ConvertTo-Json -Depth 5),
    [Text.UTF8Encoding]::new($false)
)

Write-Host "Jarvis Pod created: $($pod.id)"
Write-Host "Status: $($pod.status)"
Write-Host "GPU: $($pod.gpu.id)"
Write-Host "URL: https://$($pod.id)-3000.proxy.runpod.net"
