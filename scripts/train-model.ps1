param(
    [Parameter(Mandatory = $true)]
    [string]$Manifest,
    [string]$Output = "C:\Research Data\models\modality-classifier.joblib",
    [string]$Root = "C:\Research Data\data ref"
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $Python)) {
    throw "Environment not installed. Run scripts\bootstrap.ps1 first."
}
if (-not (Test-Path -LiteralPath $Manifest)) {
    throw "Reviewed manifest not found: $Manifest"
}

Push-Location $ProjectRoot
try {
    & $Python -m backend.train_model $Manifest $Output --root $Root
} finally {
    Pop-Location
}
