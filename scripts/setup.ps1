# Creates .venv with Python 3.12 and installs the project with dev tools.
# Run from the project root:  powershell -ExecutionPolicy Bypass -File scripts\setup.ps1
$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)

if (-not (Test-Path ".venv\Scripts\python.exe")) {
    py -3.12 -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw "Python 3.12 not found. Install it or edit this script to use 3.11." }
}
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
if ($LASTEXITCODE -ne 0) { throw "Dependency installation failed." }
Write-Host "Setup complete. Next: scripts\check.ps1"
