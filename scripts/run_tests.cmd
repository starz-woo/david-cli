@echo off
cd /d "%~dp0.."
set PYTHONDONTWRITEBYTECODE=1
set TZ=UTC
python -m unittest discover -s tests -v %*
exit /b %ERRORLEVEL%
