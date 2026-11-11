@echo off
setlocal
cd /d "%~dp0"
set PY=py -3
where py >nul 2>nul || set PY=python
set FRESH=
if not exist .venv\Scripts\python.exe (
  echo First run: setting up RoomCast, this takes a minute or two...
  %PY% -m venv .venv || goto :nopython
  set FRESH=1
)
rem (Re)install packages on first run and whenever requirements.txt has changed.
fc /b requirements.txt .venv\requirements.installed >nul 2>nul || (
  if not defined FRESH echo Updating RoomCast's packages, this takes a minute...
  .venv\Scripts\python.exe -m pip install --upgrade pip >nul
  .venv\Scripts\python.exe -m pip install -r requirements.txt || goto :fail
  copy /y requirements.txt .venv\requirements.installed >nul
)
start "" .venv\Scripts\pythonw.exe main.py
exit /b 0
:nopython
echo.
echo Python 3.10 or newer is required. Install it from https://www.python.org/downloads/ ^(tick "Add python.exe to PATH"^) and run this again.
pause
exit /b 1
:fail
echo.
if defined FRESH (
  echo Installing the required packages failed. Check your internet connection and run this again.
  rmdir /s /q .venv
) else (
  echo Updating the packages failed. Check your internet connection and run this again.
)
pause
exit /b 1
