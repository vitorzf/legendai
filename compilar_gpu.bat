@echo off
rem Compila whisper.cpp e llama.cpp com Vulkan (placa AMD) e baixa os modelos
rem (Whisper large-v3-turbo para ouvir, Gemma 3 12B para traduzir em PT-BR).
rem Requer: Git, Visual Studio 2022 com C++ e Vulkan SDK (vulkan.lunarg.com).
cd /d "%~dp0"
set SRC=%TEMP%\wcpp
set CMAKE="C:\Program Files\Microsoft Visual Studio\2022\Community\Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin\cmake.exe"
if "%VULKAN_SDK%"=="" for /d %%d in (C:\VulkanSDK\*) do set VULKAN_SDK=%%d
set PATH=%VULKAN_SDK%\Bin;%PATH%
if not exist "%SRC%" git clone --depth 1 https://github.com/ggml-org/whisper.cpp "%SRC%" || goto erro
%CMAKE% -S "%SRC%" -B "%SRC%\build" -G "Visual Studio 17 2022" -A x64 -DGGML_VULKAN=ON -DWHISPER_BUILD_TESTS=OFF -DBUILD_SHARED_LIBS=OFF || goto erro
%CMAKE% --build "%SRC%\build" --config Release -j 12 --target whisper-server whisper-bench || goto erro
if not exist bin mkdir bin
copy /y "%SRC%\build\bin\Release\whisper-server.exe" bin\ || goto erro
copy /y "%SRC%\build\bin\Release\whisper-bench.exe" bin\
set LSRC=%TEMP%\lcpp
if not exist "%LSRC%" git clone --depth 1 https://github.com/ggml-org/llama.cpp "%LSRC%" || goto erro
%CMAKE% -S "%LSRC%" -B "%LSRC%\build" -G "Visual Studio 17 2022" -A x64 -DGGML_VULKAN=ON -DBUILD_SHARED_LIBS=OFF -DLLAMA_CURL=OFF -DLLAMA_BUILD_TESTS=OFF || goto erro
%CMAKE% --build "%LSRC%\build" --config Release -j 12 --target llama-server || goto erro
copy /y "%LSRC%\build\bin\Release\llama-server.exe" bin\ || goto erro
if not exist models\ggml-large-v3-turbo-q8_0.bin curl.exe -L --retry 5 -C - -o models\ggml-large-v3-turbo-q8_0.bin https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3-turbo-q8_0.bin || goto erro
if not exist models\gemma-3-4b-it-Q4_K_M.gguf curl.exe -L --retry 5 -C - -o models\gemma-3-4b-it-Q4_K_M.gguf https://huggingface.co/ggml-org/gemma-3-4b-it-GGUF/resolve/main/gemma-3-4b-it-Q4_K_M.gguf || goto erro
if not exist models\gemma-3-1b-it-Q4_K_M.gguf curl.exe -L --retry 5 -C - -o models\gemma-3-1b-it-Q4_K_M.gguf https://huggingface.co/ggml-org/gemma-3-1b-it-GGUF/resolve/main/gemma-3-1b-it-Q4_K_M.gguf || goto erro
if not exist models\gemma-3-12b-it-Q4_K_M.gguf curl.exe -L --retry 5 -C - -o models\gemma-3-12b-it-Q4_K_M.gguf https://huggingface.co/ggml-org/gemma-3-12b-it-GGUF/resolve/main/gemma-3-12b-it-Q4_K_M.gguf || goto erro
echo Pronto! O iniciar.bat agora usa a placa de video automaticamente.
pause
exit /b 0
:erro
echo Algo deu errado.
pause
exit /b 1
