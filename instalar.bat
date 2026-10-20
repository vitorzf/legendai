@echo off
cd /d "%~dp0"
echo == Compilando o capturador de audio ==
C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe /nologo /platform:x64 /optimize /out:capture\ProcAudioCapture.exe capture\ProcAudioCapture.cs || goto erro
echo == Criando ambiente Python ==
if not exist .venv python -m venv .venv || goto erro
.venv\Scripts\python.exe -m pip install --upgrade pip
.venv\Scripts\python.exe -m pip install -r requirements.txt || goto erro
echo.
echo Pronto! Use iniciar.bat para abrir as legendas.
pause
exit /b 0
:erro
echo Algo deu errado na instalacao.
pause
exit /b 1
