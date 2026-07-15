[CmdletBinding()]
param(
    [switch]$Staged,
    [switch]$Tracked
)

$ErrorActionPreference = "Stop"
if (-not $Staged -and -not $Tracked) {
    $Staged = $true
}

$repo = (& git rev-parse --show-toplevel 2>$null).Trim()
if (-not $repo) {
    throw "Not inside a Git repository."
}

$forbiddenPrefixes = @(
    "data/", "data ref/", "inbox/", "vault/", "catalog/", "models/",
    "runtimes/", "exports/", "backups/", "quarantine/", "academic-notes/",
    "benchmark-results/", "evaluation/", "gold-set-results/"
)
$forbiddenExtensions = @(
    ".sqlite", ".sqlite3", ".db", ".gguf", ".safetensors", ".joblib",
    ".pt", ".pth", ".ckpt", ".npy", ".npz", ".parquet", ".feather",
    ".arrow", ".h5", ".hdf5", ".csv", ".tsv", ".xls", ".xlsx",
    ".tif", ".tiff", ".bmp", ".zip", ".7z", ".rar", ".pdf", ".docx"
)
$imageExtensions = @(".png", ".jpg", ".jpeg", ".webp")
$allowedImagePrefixes = @("design/", "frontend/public/")
$forbiddenNames = @("candidate-audit.json", "blind-review.csv", "soak-report.json")
$maximumBytes = 5MB

$paths = @()
if ($Staged) {
    $paths += @(& git diff --cached --name-only --diff-filter=ACMR)
}
if ($Tracked) {
    $paths += @(& git ls-files)
}
$paths = @($paths | Where-Object { $_ } | Sort-Object -Unique)

$violations = [System.Collections.Generic.List[string]]::new()
foreach ($rawPath in $paths) {
    $path = $rawPath.Replace("\", "/")
    $lower = $path.ToLowerInvariant()
    foreach ($prefix in $forbiddenPrefixes) {
        if ($lower.StartsWith($prefix)) {
            $violations.Add("forbidden data/runtime path: $path")
            break
        }
    }

    $extension = [System.IO.Path]::GetExtension($lower)
    if ($forbiddenNames -contains [System.IO.Path]::GetFileName($lower)) {
        $violations.Add("forbidden real-data evaluation artifact: $path")
    }
    if ($forbiddenExtensions -contains $extension -or
        $lower.EndsWith(".sqlite-wal") -or $lower.EndsWith(".sqlite-shm") -or
        $lower.EndsWith(".sqlite3-wal") -or $lower.EndsWith(".sqlite3-shm") -or
        $lower.EndsWith(".db-wal") -or $lower.EndsWith(".db-shm")) {
        $violations.Add("forbidden data/model extension: $path")
    }
    if ($imageExtensions -contains $extension) {
        $allowed = $false
        foreach ($prefix in $allowedImagePrefixes) {
            if ($lower.StartsWith($prefix)) {
                $allowed = $true
                break
            }
        }
        if (-not $allowed) {
            $violations.Add("image outside approved UI asset directories: $path")
        }
    }

    $indexLine = (& git ls-files -s -- $path | Select-Object -First 1)
    if ($indexLine -and $indexLine.StartsWith("120000 ")) {
        $violations.Add("symbolic links are not allowed in the repository: $path")
    }
    if ($indexLine) {
        $objectSize = & git cat-file -s ":$path" 2>$null
        if ($LASTEXITCODE -eq 0 -and [int64]$objectSize -gt $maximumBytes) {
            $violations.Add("file exceeds 5 MiB safety limit: $path ($objectSize bytes)")
        }
    }
}

if ($violations.Count -gt 0) {
    Write-Error ("Git data-safety check failed:`n - " + ($violations -join "`n - "))
    exit 1
}

Write-Host "Git data-safety check passed for $($paths.Count) file(s)."
