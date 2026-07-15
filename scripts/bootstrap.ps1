param(
    [string]$Python = "",
    [switch]$SkipFrontend
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$CodexPython = "C:\Users\tayly\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
$CodexPnpm = "C:\Users\tayly\.cache\codex-runtimes\codex-primary-runtime\dependencies\bin\fallback\pnpm.cmd"
$CodexNode = "C:\Users\tayly\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin"

if (-not $Python) {
    $SystemPython = Get-Command python -ErrorAction SilentlyContinue
    if ($SystemPython) {
        $Python = $SystemPython.Source
    } elseif (Test-Path -LiteralPath $CodexPython) {
        $Python = $CodexPython
        Write-Host "Using the bundled Codex Python runtime for this prototype."
    } else {
        throw "Python 3.12+ was not found. Install Python or pass -Python <path>."
    }
}

$VenvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $VenvPython)) {
    & $Python -m venv (Join-Path $ProjectRoot ".venv")
}

& $VenvPython -m pip install --upgrade pip
& $VenvPython -m pip install -r (Join-Path $ProjectRoot "requirements.txt")

if (-not $SkipFrontend) {
    if (Test-Path -LiteralPath (Join-Path $CodexNode "node.exe")) {
        $env:PATH = "$CodexNode;$env:PATH"
    }
    $Pnpm = Get-Command pnpm -ErrorAction SilentlyContinue
    if ($Pnpm) {
        $PnpmPath = $Pnpm.Source
    } elseif (Test-Path -LiteralPath $CodexPnpm) {
        $PnpmPath = $CodexPnpm
    } else {
        throw "pnpm was not found. Install Node.js/pnpm or use -SkipFrontend."
    }

    Push-Location (Join-Path $ProjectRoot "frontend")
    try {
        & $PnpmPath install
        & $PnpmPath build
    } finally {
        Pop-Location
    }
}

$ConfigPath = Join-Path $ProjectRoot "config.json"
if (-not (Test-Path -LiteralPath $ConfigPath)) {
    Copy-Item -LiteralPath (Join-Path $ProjectRoot "config.example.json") -Destination $ConfigPath
}

Write-Host "Academic Vault bootstrap completed."
Write-Host "Double-click 'Start Academic Vault.cmd' or run scripts\start.ps1."
