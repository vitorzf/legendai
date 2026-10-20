@echo off
cd /d "%~dp0"
rem Exemplos: iniciar.bat --programa vivaldi --ingles
set PYTHONIOENCODING=utf-8
.venv\Scripts\python.exe legendas.py %*
pause
