# Gera o instalador base do Legendai (Inno Setup) e os arquivos do Release de modulos.
#   powershell -ExecutionPolicy Bypass -File instalador\build.ps1 [-Versao 1.0.0]
# Saida: instalador\dist\Legendai-Setup-<versao>.exe e instalador\dist\release\*.zip
param([string]$Versao = "1.0.0")
$ErrorActionPreference = "Stop"
$root  = Split-Path $PSScriptRoot -Parent
$cache = Join-Path $PSScriptRoot "cache"
$build = Join-Path $PSScriptRoot "build"
$app   = Join-Path $build "app"
$rt    = Join-Path $app "runtime"
$dist  = Join-Path $PSScriptRoot "dist"
$iscc  = @("$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe", "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe") |
         Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $iscc) { throw "Inno Setup 6 nao encontrado (winget install JRSoftware.InnoSetup)" }

function Step($t) { Write-Host "`n== $t" -ForegroundColor Cyan }
Remove-Item -Recurse -Force $build -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force $rt, $dist, $cache, "$dist\release" | Out-Null

Step "Python embutido"
$py = "3.12.10"
$pyzip = Join-Path $cache "python-$py-embed-amd64.zip"
if (-not (Test-Path $pyzip)) { curl.exe -sSL --retry 5 -o $pyzip "https://www.python.org/ftp/python/$py/python-$py-embed-amd64.zip" }
Expand-Archive $pyzip $rt
# Habilita site-packages e a pasta do app (..) no caminho de importacao.
$pth = Get-ChildItem $rt -Filter "python*._pth" | Select-Object -First 1
Set-Content $pth.FullName -Encoding ascii -Value "python312.zip`r`n.`r`nLib\site-packages`r`n..`r`nimport site"
$getpip = Join-Path $cache "get-pip.py"
if (-not (Test-Path $getpip)) { curl.exe -sSL --retry 5 -o $getpip "https://bootstrap.pypa.io/get-pip.py" }
& "$rt\python.exe" $getpip --no-warn-script-location -q
if ($LASTEXITCODE) { throw "get-pip falhou" }

Step "Bibliotecas da base (interface, audio, sistema)"
$pip = @("-m", "pip", "install", "-q", "--no-warn-script-location", "--disable-pip-version-check")
& "$rt\python.exe" @pip PySide6-Essentials==6.12.0 PySideSix-Frameless-Window==0.8.2 darkdetect==0.8.0 `
    pycaw==20260927 psutil==7.2.2 numpy==2.5.4 pywin32==312
if ($LASTEXITCODE) { throw "pip falhou" }
# A biblioteca Fluent pede o PySide6 completo (~600 MB); a parte Essentials basta.
& "$rt\python.exe" @pip --no-deps PySide6-Fluent-Widgets==1.11.3
if ($LASTEXITCODE) { throw "pip falhou" }

Step "Enxugando o Qt (so o que o app usa)"
$ps = Join-Path $rt "Lib\site-packages\PySide6"
$keep = "Core|Gui|Widgets|Svg|SvgWidgets|Xml|Network|OpenGL|OpenGLWidgets|DBus"
Get-ChildItem $ps -Filter "Qt6*.dll" | Where-Object { $_.BaseName -notmatch "^Qt6($keep)$" } | Remove-Item
Get-ChildItem $ps -Filter "Qt*.pyd" | Where-Object { $_.BaseName -notmatch "^Qt($keep)$" } | Remove-Item
Get-ChildItem $ps -Filter "Qt*.pyi" | Remove-Item
Get-ChildItem $ps -Filter "*.exe" | Remove-Item
Remove-Item "$ps\opengl32sw.dll" -ErrorAction SilentlyContinue
foreach ($d in "qml", "translations", "include", "typesystems", "glue", "scripts", "examples", "resources", "metatypes", "lib") {
    Remove-Item -Recurse -Force (Join-Path $ps $d) -ErrorAction SilentlyContinue
}
Get-ChildItem "$ps\plugins" -Directory | Where-Object { $_.Name -notin "platforms", "imageformats", "iconengines", "styles" } |
    Remove-Item -Recurse -Force
Get-ChildItem $rt -Recurse -Directory -Filter "__pycache__" | Remove-Item -Recurse -Force

Step "Arquivos do programa"
foreach ($f in "launcher.py", "legendas.py", "desempenho.py", "autoajuste.py", "modulos.py") { Copy-Item "$root\$f" $app }
New-Item -ItemType Directory -Force "$app\assets", "$app\capture" | Out-Null
Copy-Item "$root\assets\icon.svg", "$root\assets\icon.ico", "$root\assets\amostra.wav" "$app\assets"
Copy-Item "$root\capture\ProcAudioCapture.exe" "$app\capture"

Step "Teste rapido da base (abre o painel sem mostrar e fecha)"
$env:QT_QPA_PLATFORM = "offscreen"
& "$rt\python.exe" -c "import sys; sys.argv=['x']; import launcher, legendas, modulos, desempenho; from PySide6.QtWidgets import QApplication; a=QApplication(sys.argv); launcher.CONFIG_PATH='nul'; w=launcher.MainWindow(); w.tray.hide(); print('painel ok; modulos:', sum(modulos.status().values()), 'instalados')"
$code = $LASTEXITCODE
Remove-Item Env:QT_QPA_PLATFORM
if ($code) { throw "a base nao abriu" }
"tamanho da base: {0:N0} MB" -f ((Get-ChildItem $app -Recurse -File | Measure-Object Length -Sum).Sum / 1MB)

Step "Imagens do assistente"
& "$root\.venv\Scripts\python.exe" "$PSScriptRoot\imagens.py" $build

Step "Instalador (Inno Setup)"
& $iscc /Q "/DVersao=$Versao" "/DBuild=$build" "/DDist=$dist" "$PSScriptRoot\Legendai.iss"
if ($LASTEXITCODE) { throw "ISCC falhou" }

Step "Arquivos do Release de modulos"
if (Test-Path "$root\bin\whisper-server.exe") {
    # whisper.cpp e MIT: a licenca vai junto com o executavel.
    $lic = Join-Path $cache "whisper.cpp-LICENSE.txt"
    if (-not (Test-Path $lic)) { curl.exe -sSL -o $lic "https://raw.githubusercontent.com/ggml-org/whisper.cpp/master/LICENSE" }
    Compress-Archive -Force "$root\bin\whisper-server.exe", $lic "$dist\release\whisper-server-vulkan-x64.zip"
}
Get-ChildItem $dist -Recurse -File | Select-Object @{n = "Arquivo"; e = { $_.FullName.Substring($dist.Length + 1) } },
    @{n = "MB"; e = { [math]::Round($_.Length / 1MB, 1) } } | Format-Table -AutoSize
