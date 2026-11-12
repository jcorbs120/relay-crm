# Build the Relay CRM Windows installer.
#   powershell -ExecutionPolicy Bypass -File deploy\windows\build_windows.ps1
# Produces dist\RelayCRM-Setup-<version>.exe
# Needs: Python 3.11+, Inno Setup 6 (https://jrsoftware.org/isdl.php). Internet access on first run.
# Optional code signing: set $env:SIGN_PFX (path) and $env:SIGN_PFX_PASSWORD before running.
$ErrorActionPreference = "Stop"
$root = Resolve-Path "$PSScriptRoot\..\.."
Set-Location $root

$version = (Select-String -Path crm\__init__.py -Pattern '__version__ = "(.+)"').Matches[0].Groups[1].Value
Write-Host "Building Relay CRM $version" -ForegroundColor Cyan

if (-not (Test-Path .build-venv)) { py -3 -m venv .build-venv }
$py = ".build-venv\Scripts\python.exe"
& $py -m pip install --upgrade pip -q
& $py -m pip install -r requirements.txt pytest -q
# PyInstaller with a freshly compiled launcher: prebuilt launchers are often flagged by antivirus (McAfee, Defender).
$env:PYINSTALLER_COMPILE_BOOTLOADER = "1"
& $py -m pip install --no-binary pyinstaller "pyinstaller==6.11.1" -q
if ($LASTEXITCODE -ne 0) { Write-Warning "Could not compile the PyInstaller launcher; using the prebuilt one."; & $py -m pip install "pyinstaller==6.11.1" -q }

Write-Host "Running tests..." -ForegroundColor Cyan
& $py -m pytest -q tests
if ($LASTEXITCODE -ne 0) { throw "Tests failed - not building." }

Write-Host "PyInstaller..." -ForegroundColor Cyan
& $py -m PyInstaller --noconfirm --clean --distpath dist --workpath build deploy\windows\relay.spec
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }

# NSSM (service wrapper)
$nssm = "deploy\windows\nssm.exe"
if (-not (Test-Path $nssm)) {
  Write-Host "Downloading NSSM..." -ForegroundColor Cyan
  $zip = "$env:TEMP\nssm.zip"
  try { Invoke-WebRequest "https://nssm.cc/release/nssm-2.24.zip" -OutFile $zip -UseBasicParsing }
  catch { Invoke-WebRequest "https://web.archive.org/web/2023/https://nssm.cc/release/nssm-2.24.zip" -OutFile $zip -UseBasicParsing }
  Expand-Archive $zip -DestinationPath "$env:TEMP\nssm" -Force
  Copy-Item "$env:TEMP\nssm\nssm-2.24\win64\nssm.exe" $nssm
}

function Sign($file) {
  if ($env:SIGN_PFX) {
    $signtool = Get-ChildItem "${env:ProgramFiles(x86)}\Windows Kits\10\bin\*\x64\signtool.exe" | Select-Object -Last 1
    & $signtool.FullName sign /f $env:SIGN_PFX /p $env:SIGN_PFX_PASSWORD /fd SHA256 /tr http://timestamp.digicert.com /td SHA256 $file
  }
}
Sign "dist\RelayCRM\RelayCRM.exe"

$iscc = @("${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe", "$env:ProgramFiles\Inno Setup 6\ISCC.exe") | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $iscc) { throw "Inno Setup 6 not found. Install from https://jrsoftware.org/isdl.php" }
& $iscc "/DAppVersion=$version" deploy\windows\RelayCRM.iss
if ($LASTEXITCODE -ne 0) { throw "Inno Setup failed" }
Sign "dist\RelayCRM-Setup-$version.exe"
Write-Host "Done: dist\RelayCRM-Setup-$version.exe" -ForegroundColor Green
