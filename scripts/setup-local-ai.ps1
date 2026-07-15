[CmdletBinding()]
param(
    [string]$DataRoot = "C:\research data",
    [ValidateSet("HuggingFace", "ModelScope")]
    [string]$ModelSource = "HuggingFace",
    [switch]$Download,
    [switch]$VerifyOnly
)

$ErrorActionPreference = "Stop"
$repositoryRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$lockPath = Join-Path $repositoryRoot "profiles\windows-model-lock.json"
$lock = Get-Content -LiteralPath $lockPath -Raw -Encoding utf8 | ConvertFrom-Json
$resolvedDataRoot = [System.IO.Path]::GetFullPath($DataRoot)

function Test-IsWithin([string]$Child, [string]$Parent) {
    $childPath = [System.IO.Path]::GetFullPath($Child).TrimEnd('\')
    $parentPath = [System.IO.Path]::GetFullPath($Parent).TrimEnd('\')
    return $childPath.StartsWith($parentPath + '\', [System.StringComparison]::OrdinalIgnoreCase) -or
        $childPath.Equals($parentPath, [System.StringComparison]::OrdinalIgnoreCase)
}

function Assert-OutsideRepository([string]$Path) {
    if (Test-IsWithin $Path $repositoryRoot) {
        throw "Local AI artifacts must be stored outside the Git repository: $Path"
    }
}

function Test-Artifact([string]$Path, [int64]$ExpectedBytes, [string]$ExpectedSha256) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return $false
    }
    $item = Get-Item -LiteralPath $Path
    if ($item.Length -ne $ExpectedBytes) {
        throw "Artifact size mismatch: $Path (expected $ExpectedBytes, got $($item.Length))"
    }
    $actual = (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actual -ne $ExpectedSha256.ToLowerInvariant()) {
        throw "Artifact SHA-256 mismatch: $Path"
    }
    return $true
}

function Receive-Artifact([string]$Url, [string]$Destination, [int64]$ExpectedBytes, [string]$ExpectedSha256) {
    if (Test-Artifact $Destination $ExpectedBytes $ExpectedSha256) {
        Write-Host "Verified existing artifact: $Destination"
        return
    }
    if (-not $Download) {
        Write-Host "Missing artifact (plan only): $Destination"
        return
    }
    $curl = Get-Command curl.exe -ErrorAction Stop
    $parent = Split-Path -Parent $Destination
    New-Item -ItemType Directory -Force -Path $parent | Out-Null
    $partial = "$Destination.partial"
    $downloadLockPath = "$Destination.download.lock"
    try {
        $downloadLock = [System.IO.File]::Open(
            $downloadLockPath,
            [System.IO.FileMode]::OpenOrCreate,
            [System.IO.FileAccess]::ReadWrite,
            [System.IO.FileShare]::None
        )
    } catch [System.IO.IOException] {
        throw "Another process is already downloading this artifact: $Destination"
    }
    try {
        # A second process may have completed the artifact while this process
        # waited to acquire the exclusive file handle.
        if (Test-Artifact $Destination $ExpectedBytes $ExpectedSha256) {
            Write-Host "Verified existing artifact: $Destination"
            return
        }
        Write-Host "Downloading $Url"
        & $curl.Source --location --fail --silent --show-error --retry 5 --retry-all-errors --retry-delay 5 --continue-at - --output $partial $Url
        if ($LASTEXITCODE -ne 0) {
            throw "Download failed: $Url"
        }
        if (-not (Test-Artifact $partial $ExpectedBytes $ExpectedSha256)) {
            throw "Downloaded artifact did not pass verification: $partial"
        }
        Move-Item -LiteralPath $partial -Destination $Destination
        Write-Host "Downloaded and verified: $Destination"
    } finally {
        $downloadLock.Dispose()
        Remove-Item -LiteralPath $downloadLockPath -Force -ErrorAction SilentlyContinue
    }
}

$modelRoot = Join-Path $resolvedDataRoot ("models\qwen3-vl-8b-instruct\" + $lock.model.revision)
$runtimeRoot = Join-Path $resolvedDataRoot ("runtimes\llama.cpp\" + $lock.runtime.release)
$archiveRoot = Join-Path $resolvedDataRoot ("runtimes\downloads\llama.cpp\" + $lock.runtime.release)
Assert-OutsideRepository $modelRoot
Assert-OutsideRepository $runtimeRoot
Assert-OutsideRepository $archiveRoot

$plannedBytes = [int64]$lock.model.bytes + [int64]$lock.vision_projector.bytes
foreach ($artifact in $lock.runtime.artifacts) {
    $plannedBytes += [int64]$artifact.bytes
}
$drive = [System.IO.DriveInfo]::new([System.IO.Path]::GetPathRoot($resolvedDataRoot))
if ($Download -and $drive.AvailableFreeSpace -lt ($plannedBytes + 2GB)) {
    throw "Insufficient free disk space for verified downloads and extraction."
}

if ($ModelSource -eq "ModelScope") {
    # ModelScope mirrors the official Qwen repository under its rolling master
    # revision. Exact byte counts and SHA-256 hashes from the lock remain the
    # authority, so a mirror can never silently change the pinned artifacts.
    $modelUrl = "https://modelscope.cn/models/$($lock.model.repository)/resolve/master/$($lock.model.filename)"
    $projectorUrl = "https://modelscope.cn/models/$($lock.vision_projector.repository)/resolve/master/$($lock.vision_projector.filename)"
} else {
    $modelUrl = "https://huggingface.co/$($lock.model.repository)/resolve/$($lock.model.revision)/$($lock.model.filename)?download=true"
    $projectorUrl = "https://huggingface.co/$($lock.vision_projector.repository)/resolve/$($lock.vision_projector.revision)/$($lock.vision_projector.filename)?download=true"
}
$modelPath = Join-Path $modelRoot $lock.model.filename
$projectorPath = Join-Path $modelRoot $lock.vision_projector.filename
Receive-Artifact $modelUrl $modelPath ([int64]$lock.model.bytes) $lock.model.sha256
Receive-Artifact $projectorUrl $projectorPath ([int64]$lock.vision_projector.bytes) $lock.vision_projector.sha256

$runtimeArchives = @()
foreach ($artifact in $lock.runtime.artifacts) {
    $destination = Join-Path $archiveRoot $artifact.filename
    Receive-Artifact $artifact.url $destination ([int64]$artifact.bytes) $artifact.sha256
    $runtimeArchives += $destination
}

if ($VerifyOnly) {
    if (-not (Test-Artifact $modelPath ([int64]$lock.model.bytes) $lock.model.sha256)) {
        throw "Model is missing."
    }
    if (-not (Test-Artifact $projectorPath ([int64]$lock.vision_projector.bytes) $lock.vision_projector.sha256)) {
        throw "Vision projector is missing."
    }
    foreach ($index in 0..($lock.runtime.artifacts.Count - 1)) {
        $artifact = $lock.runtime.artifacts[$index]
        if (-not (Test-Artifact $runtimeArchives[$index] ([int64]$artifact.bytes) $artifact.sha256)) {
            throw "Runtime archive is missing: $($runtimeArchives[$index])"
        }
    }
}

$runtimeBin = Join-Path $runtimeRoot "bin"
$serverPath = Join-Path $runtimeBin "llama-server.exe"
if ($Download -and -not (Test-Path -LiteralPath $serverPath -PathType Leaf)) {
    $extractRoot = Join-Path $runtimeRoot ".extracting"
    if (Test-Path -LiteralPath $extractRoot) {
        $resolvedExtract = [System.IO.Path]::GetFullPath($extractRoot)
        if (-not (Test-IsWithin $resolvedExtract $runtimeRoot)) {
            throw "Refusing to clear extraction path outside runtime root."
        }
        Remove-Item -LiteralPath $resolvedExtract -Recurse -Force
    }
    New-Item -ItemType Directory -Force -Path $extractRoot | Out-Null
    $mainExtract = Join-Path $extractRoot "main"
    $cudaExtract = Join-Path $extractRoot "cuda"
    Expand-Archive -LiteralPath $runtimeArchives[0] -DestinationPath $mainExtract
    Expand-Archive -LiteralPath $runtimeArchives[1] -DestinationPath $cudaExtract
    $server = Get-ChildItem -LiteralPath $mainExtract -Recurse -Filter "llama-server.exe" | Select-Object -First 1
    if (-not $server) {
        throw "Official runtime archive did not contain llama-server.exe"
    }
    $preparedBin = Join-Path $extractRoot "bin"
    New-Item -ItemType Directory -Force -Path $preparedBin | Out-Null
    Copy-Item -Path (Join-Path $server.Directory.FullName "*") -Destination $preparedBin -Recurse -Force
    Get-ChildItem -LiteralPath $cudaExtract -Recurse -File | ForEach-Object {
        Copy-Item -LiteralPath $_.FullName -Destination (Join-Path $preparedBin $_.Name) -Force
    }
    if (Test-Path -LiteralPath $runtimeBin) {
        throw "Refusing to overwrite existing runtime bin directory: $runtimeBin"
    }
    Move-Item -LiteralPath $preparedBin -Destination $runtimeBin
    Remove-Item -LiteralPath $extractRoot -Recurse -Force
}

$summary = [ordered]@{
    profile_id = $lock.profile_id
    model_source = $ModelSource
    planned_bytes = $plannedBytes
    data_root = $resolvedDataRoot
    model_path = $modelPath
    projector_path = $projectorPath
    runtime_server = $serverPath
    download_requested = [bool]$Download
    model_verified = Test-Artifact $modelPath ([int64]$lock.model.bytes) $lock.model.sha256
    projector_verified = Test-Artifact $projectorPath ([int64]$lock.vision_projector.bytes) $lock.vision_projector.sha256
    runtime_installed = Test-Path -LiteralPath $serverPath -PathType Leaf
}
$summary | ConvertTo-Json
