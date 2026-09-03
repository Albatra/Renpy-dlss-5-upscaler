@echo off
rem RenPyHD setup: downloads the DLSS 5 Visual Enhancer v3.0 into DLSS5\ and builds RenPyHD.exe.
rem Installation de RenPyHD : telecharge le DLSS 5 Visual Enhancer v3.0 dans DLSS5\ et compile RenPyHD.exe.
rem Options are passed to setup.ps1, e.g.:  setup.bat -LocalZip "C:\Downloads\DLSS.5.Visual.Enhancer.v3.0.zip"
setlocal
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup.ps1" %*
set "RC=%ERRORLEVEL%"
echo.
if not "%RC%"=="0" (
    echo Setup failed / Installation echouee ^(code %RC%^). See the messages above / Voir les messages ci-dessus.
) else (
    echo Setup finished / Installation terminee. Double-click RenPyHD.exe ^(or run.bat^) to start.
)
pause
exit /b %RC%
