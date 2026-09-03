@echo off
rem Builds ..\RenPyHD.exe from launcher.cs with the C# compiler shipped with the .NET Framework 4 (present on every
rem Windows 10/11). No SDK needed. / Compile ..\RenPyHD.exe avec le compilateur C# du .NET Framework 4 (present sur tout
rem Windows 10/11). Aucun SDK necessaire.
setlocal
cd /d "%~dp0"
set "CSC=%windir%\Microsoft.NET\Framework64\v4.0.30319\csc.exe"
if not exist "%CSC%" set "CSC=%windir%\Microsoft.NET\Framework\v4.0.30319\csc.exe"
if not exist "%CSC%" (
    echo csc.exe not found / introuvable: %CSC%
    exit /b 1
)
set "ICON="
if exist "%~dp0renpyhd.ico" set "ICON=/win32icon:%~dp0renpyhd.ico"
"%CSC%" /nologo /target:exe /optimize+ /out:"%~dp0..\RenPyHD.exe" /reference:System.Windows.Forms.dll /reference:System.dll %ICON% "%~dp0launcher.cs"
if errorlevel 1 (
    echo Build failed / Compilation echouee.
    exit /b 1
)
echo Built / Compile : %~dp0..\RenPyHD.exe
