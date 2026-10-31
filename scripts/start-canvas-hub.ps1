$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo
$env:Path = "$env:USERPROFILE\.local\bin;$env:Path"

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Host "Canvas Hub needs 'uv' (a Python installer). Install it with:"
    Write-Host '  powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"'
    Write-Host "Then close this window, open a new PowerShell window, and run this script again."
    Read-Host "Press Enter to close"
    exit 1
}

& uv sync --quiet
if ($LASTEXITCODE -ne 0) {
    Write-Host "Canvas Hub could not install its dependencies."
    Read-Host "Press Enter to close"
    exit $LASTEXITCODE
}

if ([string]::IsNullOrWhiteSpace($env:CANVAS_HUB_BROWSER_CHANNEL)) {
    & uv run playwright install chromium
    if ($LASTEXITCODE -ne 0) {
        Write-Host "Canvas Hub could not install its Chromium browser."
        Read-Host "Press Enter to close"
        exit $LASTEXITCODE
    }
}

& uv run canvas-hub serve @args
exit $LASTEXITCODE
