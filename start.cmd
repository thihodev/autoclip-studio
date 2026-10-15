@echo off
rem Start AutoClip Studio on Windows. Sets up .venv and installs dependencies
rem on the first run and whenever requirements.txt changes.
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo Creating .venv ...
  py -3 -m venv .venv 2>nul || python -m venv .venv || (echo Python 3.11 or newer is required: https://www.python.org/downloads/ & exit /b 1)
)

for /f "skip=1 delims=" %%h in ('certutil -hashfile requirements.txt SHA1') do if not defined STAMP set "STAMP=%%h"
set "OLD="
if exist ".venv\.requirements" set /p OLD=<".venv\.requirements"
if not "%STAMP%"=="%OLD%" (
  echo Installing dependencies ...
  ".venv\Scripts\python.exe" -m pip install -q --disable-pip-version-check -r requirements.txt || exit /b 1
  > ".venv\.requirements" echo %STAMP%
)

".venv\Scripts\python.exe" -m app %*
