@echo off
rem run.bat - starts RenPyHD without the compiled launcher (fallback). Exit code 75 = restart (language change).
rem run.bat - demarre RenPyHD sans le lanceur compile (secours). Code de sortie 75 = redemarrage (changement de langue).
setlocal
cd /d "%~dp0"
set "PY=%~dp0DLSS5\bin\python-3.13.15-embed-amd64\python.exe"
if not exist "%PY%" (
    echo DLSS5\ is missing: run setup.bat first. / DLSS5\ est absent : lancez d'abord setup.bat.
    pause
    exit /b 2
)
set "PYTHONNOUSERSITE=1"
set "PYTHONDONTWRITEBYTECODE=1"
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"
set "GRADIO_ANALYTICS_ENABLED=False"
set "HF_HUB_OFFLINE=1"
title RenPyHD
echo RenPyHD - close this window to stop. / Fermez cette fenetre pour arreter.
:run
"%PY%" "%~dp0app\renpy_hd_app.py" --tool "%~dp0DLSS5" %*
set "RC=%ERRORLEVEL%"
if "%RC%"=="75" (
    echo.
    echo Restarting RenPyHD... / Redemarrage de RenPyHD...
    goto run
)
if not "%RC%"=="0" pause
exit /b %RC%
