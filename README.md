# Legendaí

Legendas ao vivo **do inglês para o português do Brasil**, por cima de qualquer programa do Windows
(navegador, player, jogo…). Tudo roda no seu PC: sem internet, sem assinatura.

- Ouve **só o programa escolhido** (o Discord e o resto do sistema ficam de fora).
- Reconhece a fala com **Whisper** e traduz com **Gemma** (português do Brasil natural) ou Argos.
- A frase vai **se montando enquanto a pessoa fala**; a versão final aparece ~0,4 s depois da pausa
  numa placa de vídeo boa.
- Funciona em PCs sem placa de vídeo dedicada (níveis mais leves) e se ajusta sozinho ao seu PC.

## Instalar

1. Baixe o `Legendai-Setup-x.y.z.exe` em [Releases](https://github.com/vitorzf/legendai/releases)
   (~40 MB, não precisa de administrador).
2. Abra o Legendaí → **Módulos** → **Instalação completa** (ou escolha só o que quiser).
3. Escolha o programa na tela inicial e clique em **Iniciar legendas**.

Requer Windows 10 (2004) ou 11, 64 bits.

## Módulos

O instalador traz só o programa base (~160 MB instalado). O resto é baixado sob demanda:

| Módulo | Tamanho | Para quê |
|---|---|---|
| Núcleo de reconhecimento | ~180 MB | detecção de voz e bibliotecas de IA (obrigatório) |
| Fala na placa de vídeo | ~920 MB | Whisper Large v3 Turbo via Vulkan (AMD, NVIDIA, Intel) |
| Fala no processador (Base / Small) | 145 / 485 MB | para PCs sem placa de vídeo |
| Motor de tradução | ~35 MB | llama.cpp, que roda os tradutores Gemma |
| Gemma 3 1B / 4B / 12B | 0,8 / 2,5 / 7,3 GB | tradução para português do Brasil |
| Argos | ~85 MB | tradutor levíssimo (português de Portugal) |

## Desempenho

Em **Ajustes → Desempenho**:

- **Automático**: o Legendaí testa o PC (~1 min) e escolhe o nível mais preciso que ainda responde rápido.
- **Manual**: uma barra de *mais rápido* ↔ *mais preciso*, só com os níveis que cabem no seu PC.
- **Avançado**: cada parte ajustável à mão.

## Desenvolvimento

```bat
instalar.bat          :: cria o .venv e compila o capturador de áudio
compilar_gpu.bat      :: compila whisper.cpp e llama.cpp com Vulkan e baixa os modelos
.venv\Scripts\python launcher.py
```

- `launcher.py` — painel (PySide6 + Fluent Widgets)
- `legendas.py` — captura → reconhecimento → tradução → legenda na tela
- `desempenho.py` / `autoajuste.py` — níveis e teste do PC
- `modulos.py` — instalação dos módulos
- `capture/ProcAudioCapture.cs` — captura do áudio de um processo (WASAPI process loopback)
- `teste_latencia/` — mede o atraso entre a fala e a legenda
- `instalador/build.ps1` — gera o instalador (Python embutido + Inno Setup)

## Licenças de terceiros

whisper.cpp e llama.cpp (MIT), modelos Whisper (MIT), Gemma ([Gemma Terms of Use](https://ai.google.dev/gemma/terms)),
Argos Translate / OPUS-MT, PySide6 (LGPL), PySide6-Fluent-Widgets (GPLv3).
