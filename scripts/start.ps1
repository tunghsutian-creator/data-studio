param(
    [switch]$NoBrowser
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $Python)) {
    throw "Environment not installed. Run scripts\bootstrap.ps1 first."
}

$env:ACADEMIC_VAULT_CONFIG = Join-Path $ProjectRoot "config.json"

try {
    $Health = Invoke-RestMethod -Uri "http://127.0.0.1:8765/api/health" -TimeoutSec 1
    if ($Health.status -eq "ok" -and $Health.service -eq "academic-vault") {
        if (-not $NoBrowser) {
            Start-Process "http://127.0.0.1:8765"
        }
        Write-Host "Academic Vault is already running at http://127.0.0.1:8765"
        exit 0
    }
} catch {
    # No existing local service; continue with a normal startup.
}

if (-not $NoBrowser) {
    Start-Process "http://127.0.0.1:8765"
}

Push-Location $ProjectRoot
try {
    & $Python -m uvicorn backend.app:app --host 127.0.0.1 --port 8765
} finally {
    Pop-Location
}
