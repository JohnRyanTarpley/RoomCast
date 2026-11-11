@echo off
rem Builds RoomCast.exe in the repo root using the C# compiler that ships with Windows.
setlocal
cd /d "%~dp0"
set CSC=%WINDIR%\Microsoft.NET\Framework64\v4.0.30319\csc.exe
if not exist "%CSC%" set CSC=%WINDIR%\Microsoft.NET\Framework\v4.0.30319\csc.exe
if not exist "%CSC%" (
  echo Could not find csc.exe ^(.NET Framework 4^). It is included with Windows 10 and 11.
  exit /b 1
)
set ICON=
if exist ..\assets\roomcast.ico set ICON=/win32icon:..\assets\roomcast.ico
"%CSC%" /nologo /target:winexe /optimize+ /out:..\RoomCast.exe %ICON% /reference:System.Windows.Forms.dll RoomCast.cs || exit /b 1
echo Built %~dp0..\RoomCast.exe
