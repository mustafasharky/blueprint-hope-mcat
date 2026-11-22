@echo off
cd /d "%~dp0"
set "BLUEPRINT_PYTHON=python"
if exist ".venv\Scripts\python.exe" set "BLUEPRINT_PYTHON=.venv\Scripts\python.exe"
"%BLUEPRINT_PYTHON%" blueprint_run.py %*
set "BLUEPRINT_EXIT=%ERRORLEVEL%"
pause
exit /b %BLUEPRINT_EXIT%
