@echo off
REM Builds the Relay CRM installer on this Windows PC (alternative to GitHub Actions).
REM Needs: Python 3.12 (python.org, tick "Add to PATH") and Inno Setup 6 (jrsoftware.org).
cd /d "%~dp0"
where python >nul 2>nul || (echo Python not found. Install Python 3.12 from python.org and tick "Add Python to PATH". & pause & exit /b 1)
if not exist .buildenv python -m venv .buildenv
call .buildenv\Scripts\activate.bat
python -m pip install --upgrade pip >nul
python -m pip install -r requirements-build.txt || (pause & exit /b 1)
REM Build PyInstaller's launcher from source (fewer antivirus false alarms). Needs Visual Studio Build Tools; falls back to the prebuilt one.
set PYINSTALLER_COMPILE_BOOTLOADER=1
python -m pip install --no-binary pyinstaller pyinstaller==6.11.1 || python -m pip install pyinstaller==6.11.1
python -m pytest -q || (echo Tests failed. & pause & exit /b 1)
pyinstaller --noconfirm --clean packaging\relay.spec || (pause & exit /b 1)
dist\RelayCRM\RelayCRM.exe --selftest
set ISCC="%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if not exist %ISCC% (echo.& echo App built in dist\RelayCRM\RelayCRM.exe. Install Inno Setup 6 to also make Setup.exe. & pause & exit /b 0)
%ISCC% packaging\installer.iss || (pause & exit /b 1)
echo.
echo Done! Installer is in the Output folder.
explorer Output
pause
