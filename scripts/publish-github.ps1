[CmdletBinding()]
param()

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

function Invoke-GitHub {
    param(
        [Parameter(Mandatory)][string]$Method,
        [Parameter(Mandatory)][string]$Path,
        [object]$Body
    )

    $parameters = @{
        Method      = $Method
        Uri         = "https://api.github.com$Path"
        Headers     = $script:GitHubHeaders
        ContentType = "application/json"
    }
    if ($null -ne $Body) {
        $parameters.Body = $Body | ConvertTo-Json -Depth 20 -Compress
    }
    return Invoke-RestMethod @parameters
}

function Set-DotEnvValue {
    param(
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)][string]$Name,
        [Parameter(Mandatory)][string]$Value
    )
    $text = [IO.File]::ReadAllText($Path)
    if ($text -match "(?m)^$([regex]::Escape($Name))=") {
        $text = [regex]::Replace(
            $text,
            "(?m)^$([regex]::Escape($Name))=.*$",
            "$Name=$Value"
        )
    }
    else {
        $text = $text.TrimEnd() + [Environment]::NewLine + "$Name=$Value" +
            [Environment]::NewLine
    }
    [IO.File]::WriteAllText(
        $Path,
        $text,
        [Text.UTF8Encoding]::new($false)
    )
}

$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$envPath = Join-Path $root ".env"
if (-not (Test-Path -LiteralPath $envPath)) {
    throw ".env is missing."
}

$config = Read-DotEnv -Path $envPath
$owner = [string]$config["GITHUB_USERNAME"]
$token = [string]$config["GITHUB_TOKEN"]
$repository = [string]$config["GITHUB_REPOSITORY"]
if (-not $repository) {
    $repository = "jarvis-runpod"
}

if (-not $token) {
    throw "Set GITHUB_TOKEN in .env."
}
if ($repository -notmatch "^[A-Za-z0-9._-]+$") {
    throw "GITHUB_REPOSITORY contains unsupported characters."
}

$script:GitHubHeaders = @{
    Authorization          = "Bearer $token"
    Accept                 = "application/vnd.github+json"
    "X-GitHub-Api-Version" = "2022-11-28"
    "User-Agent"           = "jarvis-runpod-deployer"
}

$identity = Invoke-GitHub -Method Get -Path "/user"
$authenticatedOwner = [string]$identity.login
if (
    -not $authenticatedOwner -or
    $authenticatedOwner -notmatch "^[A-Za-z0-9-]+$"
) {
    throw "The supplied token did not return a valid GitHub login."
}
if ($owner -ine $authenticatedOwner) {
    $owner = $authenticatedOwner
    Set-DotEnvValue -Path $envPath -Name "GITHUB_USERNAME" -Value $owner
    Write-Host "Corrected GITHUB_USERNAME from the authenticated token."
}

$repoPath = "/repos/$owner/$repository"
try {
    $repo = Invoke-GitHub -Method Get -Path $repoPath
}
catch {
    if ([int]$_.Exception.Response.StatusCode -ne 404) {
        throw
    }
    Write-Host "Creating private GitHub repository $owner/$repository..."
    $repo = Invoke-GitHub -Method Post -Path "/user/repos" -Body @{
        name        = $repository
        private     = $true
        auto_init   = $true
        description = "Private build source for the Jarvis RunPod image"
    }
}

$branch = [string]$repo.default_branch
if (-not $branch) {
    $branch = "main"
}

$ref = Invoke-GitHub -Method Get -Path "$repoPath/git/ref/heads/$branch"
$headCommit = Invoke-GitHub -Method Get -Path "$repoPath/git/commits/$($ref.object.sha)"
$baseTree = [string]$headCommit.tree.sha

$excludedDirectories = @(
    ".git",
    ".cursor",
    "data",
    "node_modules",
    "__pycache__"
)

$files = Get-ChildItem -LiteralPath $root -Recurse -File | Where-Object {
    $relative = $_.FullName.Substring($root.Length).TrimStart([char]92).Replace("\", "/")
    if ($relative -eq ".env") {
        return $false
    }
    if ($relative -like "secrets/*" -and $relative -ne "secrets/.gitkeep") {
        return $false
    }
    foreach ($directory in $excludedDirectories) {
        if (
            $relative -eq $directory -or
            $relative.StartsWith("$directory/")
        ) {
            return $false
        }
    }
    return $true
}

if (-not ($files | Where-Object {
    $_.FullName.Substring($root.Length).TrimStart([char]92).Replace("\", "/") -eq
        ".github/workflows/build-runpod.yml"
})) {
    throw "The RunPod image workflow is missing."
}

Write-Host "Uploading $($files.Count) non-secret project files..."
$treeEntries = foreach ($file in $files) {
    $relative = $file.FullName.Substring($root.Length).TrimStart([char]92).Replace("\", "/")
    $content = [Convert]::ToBase64String([IO.File]::ReadAllBytes($file.FullName))
    $blob = Invoke-GitHub -Method Post -Path "$repoPath/git/blobs" -Body @{
        content  = $content
        encoding = "base64"
    }
    @{
        path = $relative
        mode = if ($relative.EndsWith(".sh")) { "100755" } else { "100644" }
        type = "blob"
        sha  = $blob.sha
    }
}

$tree = Invoke-GitHub -Method Post -Path "$repoPath/git/trees" -Body @{
    base_tree = $baseTree
    tree      = @($treeEntries)
}
$commit = Invoke-GitHub -Method Post -Path "$repoPath/git/commits" -Body @{
    message = "Configure Jarvis RunPod deployment"
    tree    = $tree.sha
    parents = @($ref.object.sha)
}
Invoke-GitHub -Method Patch -Path "$repoPath/git/refs/heads/$branch" -Body @{
    sha   = $commit.sha
    force = $false
} | Out-Null

$image = "ghcr.io/$($owner.ToLowerInvariant())/jarvis-runpod:0.1.0"
Set-DotEnvValue -Path $envPath -Name "RUNPOD_IMAGE" -Value $image

Invoke-GitHub -Method Post `
    -Path "$repoPath/actions/workflows/build-runpod.yml/dispatches" `
    -Body @{ ref = $branch } | Out-Null

Write-Host "Build dispatched successfully."
Write-Host "Repository: $($repo.html_url)"
Write-Host "Image:      $image"
Write-Host "Actions:    $($repo.html_url)/actions"
