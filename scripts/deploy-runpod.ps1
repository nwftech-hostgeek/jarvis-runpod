[CmdletBinding()]
param(
    [string]$DataCenter = "",
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

function Test-AnonymousGhcrPull {
    param([string]$Image)
    if ($Image -notmatch '^ghcr\.io/([^/]+)/([^:]+):(.+)$') {
        return $false
    }
    $owner = $Matches[1]
    $name = $Matches[2]
    $tag = $Matches[3]
    try {
        $token = Invoke-RestMethod `
            -Uri "https://ghcr.io/token?service=ghcr.io&scope=repository:${owner}/${name}:pull"
        $null = Invoke-WebRequest `
            -Uri "https://ghcr.io/v2/${owner}/${name}/manifests/${tag}" `
            -Headers @{
                Authorization = "Bearer $($token.token)"
                Accept        = "application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.v2+json"
            } `
            -UseBasicParsing
        return $true
    }
    catch {
        return $false
    }
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

$publicImage = Test-AnonymousGhcrPull -Image $image
$registryId = $null
if (-not $publicImage) {
    Write-Host "Image is private; configuring RunPod GHCR pull credentials..."
    $registryList = Invoke-JsonApi `
        -Method Get `
        -Uri "$runpodBase/registries" `
        -Headers $runpodHeaders
    $registry = @($registryList.registries) |
        Where-Object { $_.name -eq "jarvis-ghcr" } |
        Select-Object -First 1
    if (-not $registry) {
        $registry = Invoke-JsonApi `
            -Method Post `
            -Uri "$runpodBase/registries" `
            -Headers $runpodHeaders `
            -Body @{
                name     = "jarvis-ghcr"
                username = $githubUser.ToLowerInvariant()
                password = $githubToken
            }
    }
    $registryId = $registry.id
}
else {
    Write-Host "Image is anonymously pullable; no RunPod registry credential required."
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
$templateBody = @{
    name                = "Jarvis AI Assistant 0.1.0"
    image               = $image
    args                = ""
    category            = "NVIDIA"
    disk                = 50
    ports               = @("3000/http")
    env                 = $podEnv
    public              = $false
    serverless          = $false
    startSsh            = $false
    startJupyter        = $false
    allowedCudaVersions = @()
}
if ($registryId) {
    $templateBody.registry = $registryId
}
if (-not $template) {
    Write-Host "Creating RunPod template..."
    $template = Invoke-JsonApi `
        -Method Post `
        -Uri "$runpodBase/templates" `
        -Headers $runpodHeaders `
        -Body $templateBody
}
else {
    Write-Host "Updating RunPod template $($template.id)..."
    $template = Invoke-JsonApi `
        -Method Patch `
        -Uri "$runpodBase/templates/$($template.id)" `
        -Headers $runpodHeaders `
        -Body $templateBody
}

if ($TemplateOnly) {
    Write-Host "Template ready: $($template.id)"
    exit 0
}

$existingPods = Invoke-JsonApi `
    -Method Get `
    -Uri "$runpodBase/pods" `
    -Headers $runpodHeaders
foreach ($existingPod in @($existingPods.pods)) {
    if ($existingPod.name -ne "jarvis") { continue }
    if ($existingPod.status -eq "TERMINATED") { continue }
    if ($existingPod.status -eq "RUNNING") {
        Write-Host "Jarvis Pod already running: $($existingPod.id)"
        Write-Host "URL: https://$($existingPod.id)-3000.proxy.runpod.net"
        exit 0
    }
    Write-Host "Terminating non-running Pod $($existingPod.id) ($($existingPod.status))..."
    [void](Invoke-JsonApi `
        -Method Post `
        -Uri "$runpodBase/pods/$($existingPod.id)/action" `
        -Headers $runpodHeaders `
        -Body @{ action = "terminate" })
}

$datacenters = Invoke-JsonApi `
    -Method Get `
    -Uri "$runpodBase/catalog/datacenters" `
    -Headers $runpodHeaders
$volumeDcs = @($datacenters.dataCenters |
    Where-Object { @($_.networkVolumeTypes) -contains "STANDARD" } |
    ForEach-Object { $_.id })
$gpus = Invoke-JsonApi `
    -Method Get `
    -Uri "$runpodBase/catalog/gpus?include=AVAILABILITY&product=POD&gpuCount=1&minCudaVersion=12.4" `
    -Headers $runpodHeaders

$candidates = @()
foreach ($gpu in @($gpus.gpus)) {
    if ($gpu.memory -lt 24 -or $gpu.price.secure -le 0 -or $gpu.price.secure -gt 2.0) {
        continue
    }
    foreach ($loc in @($gpu.dataCenters)) {
        if ($loc.id -notin $volumeDcs) { continue }
        if ($loc.availability -eq "NONE") { continue }
        if ($DataCenter -and $loc.id -ne $DataCenter) { continue }
        $candidates += [pscustomobject]@{
            Gpu          = $gpu.id
            Memory       = [int]$gpu.memory
            Price        = [double]$gpu.price.secure
            Availability = $loc.availability
            Dc           = $loc.id
        }
    }
}
$candidates = @($candidates | Sort-Object Price, Dc)
if (-not $candidates) {
    throw "No Secure Cloud GPU with Network Volume support is currently listed as available."
}

$volumes = Invoke-JsonApi `
    -Method Get `
    -Uri "$runpodBase/network-volumes" `
    -Headers $runpodHeaders
$pod = $null
$volume = $null
$errors = @()
foreach ($opt in $candidates) {
    try {
        Write-Host "Trying $($opt.Gpu) / $($opt.Dc) ($($opt.Memory) GB, `$$($opt.Price)/hr)..."
        $volume = @($volumes.networkVolumes) |
            Where-Object {
                $_.name -eq "jarvis-data" -and
                $_.dataCenter -eq $opt.Dc
            } |
            Select-Object -First 1
        if (-not $volume) {
            $volume = Invoke-JsonApi `
                -Method Post `
                -Uri "$runpodBase/network-volumes" `
                -Headers $runpodHeaders `
                -Body @{
                    name       = "jarvis-data"
                    size       = $VolumeSizeGb
                    dataCenter = $opt.Dc
                    type       = "STANDARD"
                }
            $volumes = Invoke-JsonApi `
                -Method Get `
                -Uri "$runpodBase/network-volumes" `
                -Headers $runpodHeaders
        }

        $ctx = if ($opt.Memory -ge 40) { "65536" } else { "16384" }
        $parallel = if ($opt.Memory -ge 40) { "2" } else { "1" }
        $createEnv = @{}
        foreach ($key in $podEnv.Keys) { $createEnv[$key] = $podEnv[$key] }
        $createEnv["LLAMA_CTX_SIZE"] = $ctx
        $createEnv["LLAMA_PARALLEL"] = $parallel

        $createBody = @{
            name             = "jarvis"
            templateId       = $template.id
            cloud            = "SECURE"
            dataCenterIds    = @($opt.Dc)
            globalNetworking = $false
            gpu              = @{
                id    = $opt.Gpu
                count = 1
            }
            env              = $createEnv
            mounts           = @{
                network = @(
                    @{
                        volumeId = $volume.id
                        path     = "/workspace"
                    }
                )
            }
            startSsh         = $false
            startJupyter     = $false
        }
        if ($registryId) {
            $createBody.registry = $registryId
        }
        $pod = Invoke-JsonApi `
            -Method Post `
            -Uri "$runpodBase/pods" `
            -Headers $runpodHeaders `
            -Body $createBody
        break
    }
    catch {
        $errors += "$($opt.Gpu)@$($opt.Dc): $($_.Exception.Message)"
    }
}

if (-not $pod) {
    throw "No requested GPU had capacity. " + ($errors -join " | ")
}

# Remove unused jarvis-data volumes created during capacity search.
$allVolumes = Invoke-JsonApi `
    -Method Get `
    -Uri "$runpodBase/network-volumes" `
    -Headers $runpodHeaders
$mountedIds = @()
if ($pod.mounts.network) {
    $mountedIds = @($pod.mounts.network | ForEach-Object { $_.volumeId })
}
foreach ($v in @($allVolumes.networkVolumes)) {
    if ($v.name -ne "jarvis-data") { continue }
    if ($v.id -in $mountedIds) { continue }
    try {
        Invoke-WebRequest `
            -Method Delete `
            -Uri "$runpodBase/network-volumes/$($v.id)" `
            -Headers $runpodHeaders `
            -UseBasicParsing | Out-Null
        Write-Host "Deleted unused Network Volume $($v.id) in $($v.dataCenter)."
    }
    catch {
        Write-Host "Could not delete unused volume $($v.id): $($_.Exception.Message)"
    }
}

$deploymentDirectory = Join-Path $root ".deployment"
New-Item -ItemType Directory -Force -Path $deploymentDirectory | Out-Null
$record = @{
    podId           = $pod.id
    templateId      = $template.id
    registryId      = $registryId
    networkVolumeId = $volume.id
    dataCenter      = $pod.dataCenterId
    gpu             = $pod.gpu.id
    image           = $image
    createdAt       = [DateTime]::UtcNow.ToString("o")
}
[IO.File]::WriteAllText(
    (Join-Path $deploymentDirectory "runpod.json"),
    ($record | ConvertTo-Json -Depth 5),
    [Text.UTF8Encoding]::new($false)
)

Write-Host "Jarvis Pod created: $($pod.id)"
Write-Host "Status: $($pod.status)"
Write-Host "GPU: $($pod.gpu.id)"
Write-Host "DataCenter: $($pod.dataCenterId)"
Write-Host "URL: https://$($pod.id)-3000.proxy.runpod.net"