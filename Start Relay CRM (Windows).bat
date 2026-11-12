@echo off
title Relay CRM
cd /d "%~dp0"

where py >nul 2>nul
if %errorlevel%==0 (set PY=py -3) else (
  where python >nul 2>nul
  if %errorlevel%==0 (set PY=python) else (
    echo.
    echo  Python 3.10+ is required. Install it from https://www.python.org/downloads/
    echo  ^(tick "Add python.exe to PATH" during setup^), then run this file again.
    echo.
    pause
    exit /b 1
  )
)

if not exist ".venv\Scripts\python.exe" (
  echo  First run: setting up Relay CRM ^(about 1 minute^)...
  %PY% -m venv .venv || goto :err
  ".venv\Scripts\python.exe" -m pip install --upgrade pip -q
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt -q || goto :err
)

rem Upgrade dependencies if requirements changed since the last start
fc /b requirements.txt ".venv\req.stamp" >nul 2>nul || (
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt -q && copy /y requirements.txt ".venv\req.stamp" >nul
)

echo.
echo  Relay CRM team server. Teammates open the "For your team" address shown below.
echo  Add --local to keep it on this computer only.
".venv\Scripts\python.exe" run.py %*
pause
exit /b 0

:err
echo.
echo  Setup failed. If you are behind a corporate proxy, set HTTPS_PROXY and try again,
echo  or ask IT to allow pypi.org / files.pythonhosted.org.
pause
exit /b 1
