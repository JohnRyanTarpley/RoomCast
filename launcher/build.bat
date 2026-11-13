@echo off
rem Builds RoomCast.exe in the repo root using the C# compiler that ships with Windows.
rem The version shown in the exe's properties comes from the VERSION file in the repo root.
setlocal
cd /d "%~dp0"
set CSC=%WINDIR%\Microsoft.NET\Framework64\v4.0.30319\csc.exe
if not exist "%CSC%" set CSC=%WINDIR%\Microsoft.NET\Framework\v4.0.30319\csc.exe
if not exist "%CSC%" (
  echo Could not find csc.exe ^(.NET Framework 4^). It is included with Windows 10 and 11.
  exit /b 1
)
set VERSION=
if exist ..\VERSION set /p VERSION=<..\VERSION
if not defined VERSION (
  echo Could not read the version from the VERSION file in the RoomCast folder.
  exit /b 1
)
rem Version details for the exe, written to a temporary source file and compiled in.
set INFO=%TEMP%\RoomCastVersion.cs
> "%INFO%" echo using System.Reflection;
>>"%INFO%" echo [assembly: AssemblyTitle("RoomCast")]
>>"%INFO%" echo [assembly: AssemblyProduct("RoomCast")]
>>"%INFO%" echo [assembly: AssemblyDescription("RoomCast launcher")]
>>"%INFO%" echo [assembly: AssemblyCopyright("Copyright (c) 2026 John Ryan Tarpley")]
>>"%INFO%" echo [assembly: AssemblyVersion("%VERSION%.0")]
>>"%INFO%" echo [assembly: AssemblyFileVersion("%VERSION%.0")]
>>"%INFO%" echo [assembly: AssemblyInformationalVersion("%VERSION%")]
set ICON=
if exist ..\assets\roomcast.ico set ICON=/win32icon:..\assets\roomcast.ico
"%CSC%" /nologo /target:winexe /optimize+ /out:..\RoomCast.exe %ICON% /reference:System.Windows.Forms.dll RoomCast.cs "%INFO%"
set ERR=%ERRORLEVEL%
del "%INFO%" >nul 2>nul
if not %ERR%==0 exit /b %ERR%
echo Built %~dp0..\RoomCast.exe (version %VERSION%)
