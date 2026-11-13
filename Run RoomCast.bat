@echo off
rem Starts RoomCast, setting it up first if needed. RoomCast runs on a Python of its own:
rem   .venv     made from the Python already installed on this PC (3.10 or newer, 64-bit).
rem             Packages that Python already has are reused instead of downloaded again.
rem   runtime   otherwise: a private copy of Python downloaded from python.org.
rem Nothing is installed system-wide and no admin rights are needed. Packages are
rem (re)installed on first run and whenever requirements.txt changes; pip skips the
rem ones that are already there. RoomCast.exe shows this window only while it does that.
setlocal
cd /d "%~dp0"
set PYTHON_VERSION=3.12.10
set PYTHON_SHA256=4acbed6dd1c744b0376e3b1cf57ce906f9dc9e95e68824584c8099a63025a3c3
set FRESH=

if exist runtime\python.exe goto :runtime_ready
if exist .venv\Scripts\python.exe goto :venv_ready

echo First run: setting up RoomCast. This takes a few minutes and needs an internet connection.
echo.
set FRESH=1
if defined ROOMCAST_NO_SYSTEM_PYTHON goto :make_runtime
call :find_python
if not defined SYSPY goto :make_runtime
echo Using the Python already installed on this PC.
%SYSPY% -m venv --system-site-packages .venv || goto :venv_failed

:venv_ready
set ENVDIR=.venv
set PYEXE=.venv\Scripts\python.exe
set PYWEXE=.venv\Scripts\pythonw.exe
call :install_packages && goto :start
if not defined FRESH goto :update_failed
:venv_failed
rem This Python couldn't run RoomCast (e.g. too new for one of the packages): use a private one.
echo.
echo That didn't work with the installed Python, so RoomCast will use its own copy instead.
echo.
if exist .venv rmdir /s /q .venv

:make_runtime
echo Downloading Python %PYTHON_VERSION% for RoomCast (about 11 MB)...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$ErrorActionPreference = 'Stop'; $ProgressPreference = 'SilentlyContinue';" ^
  "[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12;" ^
  "$zip = Join-Path $env:TEMP 'roomcast-python.zip';" ^
  "Invoke-WebRequest -UseBasicParsing -OutFile $zip 'https://www.python.org/ftp/python/%PYTHON_VERSION%/python-%PYTHON_VERSION%-embed-amd64.zip';" ^
  "if ((Get-FileHash $zip -Algorithm SHA256).Hash -ne '%PYTHON_SHA256%') { throw 'the download was damaged (checksum mismatch)' };" ^
  "Expand-Archive $zip runtime -Force; Remove-Item $zip;" ^
  "$pth = Get-ChildItem runtime\*._pth | Select-Object -First 1;" ^
  "$lines = (Get-Content $pth) -replace '^#\s*import site', 'import site';" ^
  "Set-Content $pth ($lines + '..');" ^
  "Invoke-WebRequest -UseBasicParsing -OutFile runtime\get-pip.py 'https://bootstrap.pypa.io/get-pip.py'" ^
  || goto :runtime_failed
rem (The ._pth edit turns on site-packages and puts the RoomCast folder on the module path.)
runtime\python.exe runtime\get-pip.py --no-warn-script-location --disable-pip-version-check || goto :runtime_failed
del runtime\get-pip.py

:runtime_ready
set ENVDIR=runtime
set PYEXE=runtime\python.exe
set PYWEXE=runtime\pythonw.exe
call :install_packages && goto :start
if defined FRESH goto :runtime_failed
goto :update_failed

:start
start "" "%PYWEXE%" main.py
exit /b 0

rem ---- helpers ---------------------------------------------------------------------

:find_python
rem Sets SYSPY to an installed Python 3.10+ (64-bit), or leaves it empty.
set SYSPY=
for %%P in ("py -3" "python") do if not defined SYSPY (
  %%~P -c "import sys, struct; sys.exit(not (sys.version_info >= (3, 10) and struct.calcsize('P') == 8))" >nul 2>nul && set "SYSPY=%%~P"
)
exit /b 0

:install_packages
rem Installs what requirements.txt asks for, unless it already did for this version of the file.
fc /b requirements.txt %ENVDIR%\requirements.installed >nul 2>nul && exit /b 0
if not defined FRESH echo Updating RoomCast's packages, this takes a minute...
echo Installing the packages RoomCast needs (already installed ones are skipped)...
"%PYEXE%" -m pip install --disable-pip-version-check --no-warn-script-location -r requirements.txt || exit /b 1
copy /y requirements.txt %ENVDIR%\requirements.installed >nul
exit /b 0

rem ---- failures --------------------------------------------------------------------

:runtime_failed
echo.
echo Setting up RoomCast failed. Check your internet connection and run RoomCast again.
if exist runtime rmdir /s /q runtime
pause
exit /b 1

:update_failed
echo.
echo Updating RoomCast's packages failed. Check your internet connection and run RoomCast again.
pause
exit /b 1
