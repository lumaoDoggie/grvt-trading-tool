param(
  [string]$Name = "GRVTVolumeBoost",
  [string]$Entry = "volume_boost_gui.py",
  [string]$OutDir = "dist",
  [string]$BrowserDir = "playwright-browsers"
)

# Must come after the param() block; otherwise PowerShell can treat param as a normal command,
# causing a parse error in GitHub Actions.
$ErrorActionPreference = "Stop"

Write-Host "== Building Windows package ($Name) =="

# Ensure Playwright browsers are installed into a local folder we can ship alongside the EXE.
if (-not (Test-Path $BrowserDir)) {
  New-Item -ItemType Directory -Force -Path $BrowserDir | Out-Null
}
$env:PLAYWRIGHT_BROWSERS_PATH = (Resolve-Path $BrowserDir).Path

python -m pip install --upgrade pip
python -m pip install -r requirements.txt pyinstaller

Write-Host "Installing Playwright Chromium into $($env:PLAYWRIGHT_BROWSERS_PATH)"
python -m playwright install chromium

Write-Host "Running PyInstaller..."
pyinstaller `
  --noconfirm `
  --clean `
  --windowed `
  --name $Name `
  --distpath $OutDir `
  --collect-all playwright `
  --collect-all playwright_stealth `
  --collect-submodules eth_account `
  --collect-submodules eth_keys `
  --collect-submodules eth_utils `
  $Entry

# Ship browsers folder next to the EXE. Our runtime sets PLAYWRIGHT_BROWSERS_PATH automatically when present.
$target = Join-Path $OutDir $Name
if (-not (Test-Path $target)) {
  throw "Expected PyInstaller output folder not found: $target"
}
Copy-Item -Recurse -Force $BrowserDir (Join-Path $target $BrowserDir)

Write-Host "Build complete: $target"
