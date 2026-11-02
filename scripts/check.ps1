# Runs ruff (lint + format check) and pytest. Exits non-zero on the first failure.
# Run from the project root:  powershell -ExecutionPolicy Bypass -File scripts\check.ps1
$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)
$py = ".\.venv\Scripts\python.exe"

& $py -m ruff check .
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& $py -m ruff format --check .
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& $py -m pytest
exit $LASTEXITCODE
