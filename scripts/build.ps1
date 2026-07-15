$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Pnpm = "C:\Users\tayly\.cache\codex-runtimes\codex-primary-runtime\dependencies\bin\fallback\pnpm.cmd"
$CodexNode = "C:\Users\tayly\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin"

if (Test-Path -LiteralPath (Join-Path $CodexNode "node.exe")) {
    $env:PATH = "$CodexNode;$env:PATH"
}

if (-not (Test-Path -LiteralPath $Pnpm)) {
    $Pnpm = (Get-Command pnpm -ErrorAction Stop).Source
}

Push-Location (Join-Path $ProjectRoot "frontend")
try {
    & $Pnpm build
} finally {
    Pop-Location
}
