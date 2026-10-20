"""
Modulos do Legendai: o instalador traz so o programa base; o resto (reconhecimento de fala,
tradutores, aceleracao pela placa de video) e baixado sob demanda por aqui.

Uso pelo painel (uma linha JSON por evento):
    python modulos.py instalar <id> [<id> ...]
    python modulos.py remover <id>
"""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
import zipfile

BASE = os.path.dirname(os.path.abspath(__file__))
MODELS = os.path.join(BASE, "models")
BIN = os.path.join(BASE, "bin")

# Arquivos que nao tem versao oficial para Windows com Vulkan ficam no Release do proprio projeto.
RELEASE = "https://github.com/vitorzf/legendai/releases/download/modulos-v1"
LLAMA_TAG = "b11748"  # versao do llama.cpp testada com o Legendai
LLAMA_URL = f"https://github.com/ggml-org/llama.cpp/releases/download/{LLAMA_TAG}/llama-{LLAMA_TAG}-bin-win-{{}}-x64.zip"
HF = "https://huggingface.co"
FW_FILES = ["config.json", "model.bin", "tokenizer.json", "vocabulary.txt"]


def _hf_dir(repo, folder):
    return [(f"{HF}/{repo}/resolve/main/{f}", os.path.join(MODELS, folder, f)) for f in FW_FILES]


def has_vulkan_gpu():
    sys.path.insert(0, BASE)
    import desempenho
    return desempenho.gpu_info()[1] >= 2


# Cada modulo: o que baixar/instalar e como saber se ja esta instalado.
#   pip:   pacotes Python
#   files: [(url, destino)]
#   zips:  [(url, pasta, filtro)] -> extrai os arquivos cujo nome contem o filtro ("" = todos)
MODULES = [
    {"id": "nucleo", "nome": "Núcleo de reconhecimento", "mb": 180, "requer": [], "removivel": False,
     "desc": "Detecção de voz e bibliotecas de IA. Necessário para tudo.",
     "pip": ["faster-whisper==1.2.1"], "check_import": "faster_whisper"},
    {"id": "fala_gpu", "nome": "Fala na placa de vídeo", "mb": 920, "requer": ["nucleo"], "gpu": True,
     "desc": "Whisper Large v3 Turbo acelerado pela placa de vídeo (AMD, NVIDIA ou Intel). O mais preciso e rápido.",
     "zips": [(f"{RELEASE}/whisper-server-vulkan-x64.zip", BIN, "")],
     "files": [(f"{HF}/ggerganov/whisper.cpp/resolve/main/ggml-large-v3-turbo-q8_0.bin",
                os.path.join(MODELS, "ggml-large-v3-turbo-q8_0.bin"))]},
    {"id": "fala_cpu", "nome": "Fala no processador (Base)", "mb": 145, "requer": ["nucleo"],
     "desc": "Reconhecimento de fala sem placa de vídeo. Leve, um pouco menos preciso.",
     "files": _hf_dir("Systran/faster-whisper-base.en", "faster-whisper-base.en")},
    {"id": "fala_cpu_small", "nome": "Fala no processador (Small)", "mb": 485, "requer": ["nucleo"],
     "desc": "Reconhecimento no processador mais preciso, para processadores mais fortes.",
     "files": _hf_dir("Systran/faster-whisper-small.en", "faster-whisper-small.en")},
    {"id": "motor_traducao", "nome": "Motor de tradução", "mb": 35, "requer": [],
     "desc": "llama.cpp, que roda os tradutores Gemma (na placa de vídeo ou no processador).",
     "zips": "llama"},  # versao Vulkan ou so-processador, escolhida na hora conforme o PC
    {"id": "gemma1", "nome": "Tradutor Gemma 3 1B", "mb": 806, "requer": ["motor_traducao"],
     "desc": "Português do Brasil. Ultrarrápido; roda até sem placa de vídeo.",
     "files": [(f"{HF}/ggml-org/gemma-3-1b-it-GGUF/resolve/main/gemma-3-1b-it-Q4_K_M.gguf",
                os.path.join(MODELS, "gemma-3-1b-it-Q4_K_M.gguf"))]},
    {"id": "gemma4", "nome": "Tradutor Gemma 3 4B", "mb": 2490, "requer": ["motor_traducao"], "gpu": True,
     "desc": "Português do Brasil muito bom. Para placas de vídeo a partir de ~6 GB.",
     "files": [(f"{HF}/ggml-org/gemma-3-4b-it-GGUF/resolve/main/gemma-3-4b-it-Q4_K_M.gguf",
                os.path.join(MODELS, "gemma-3-4b-it-Q4_K_M.gguf"))]},
    {"id": "gemma12", "nome": "Tradutor Gemma 3 12B", "mb": 7300, "requer": ["motor_traducao"], "gpu": True,
     "desc": "A melhor tradução. Para placas de vídeo com 12 GB ou mais.",
     "files": [(f"{HF}/ggml-org/gemma-3-12b-it-GGUF/resolve/main/gemma-3-12b-it-Q4_K_M.gguf",
                os.path.join(MODELS, "gemma-3-12b-it-Q4_K_M.gguf"))]},
    {"id": "argos", "nome": "Tradutor Argos", "mb": 85, "requer": ["nucleo"],
     "desc": "Levíssimo, mas traduz para o português de Portugal. Bom como reserva.",
     "pip": ["sentencepiece"], "check_import": "sentencepiece",
     "zips": [("https://argos-net.com/v1/translate-en_pt-1_9.argosmodel", MODELS, "")],
     "check_files": [os.path.join(MODELS, "translate-en_pt-1_9", "model", "model.bin")]},
]
BY_ID = {m["id"]: m for m in MODULES}
_LLAMA_EXE = os.path.join(BIN, "llama", "llama-server.exe")


def _paths(m):
    if m.get("check_files"):
        return m["check_files"]
    if m["id"] == "motor_traducao":
        return [_LLAMA_EXE]
    if m["id"] == "fala_gpu":
        return [os.path.join(BIN, "whisper-server.exe"), m["files"][0][1]]
    return [dest for _, dest in m.get("files", [])]


def installed(mid):
    m = BY_ID[mid]
    if m["id"] == "motor_traducao" and os.path.exists(os.path.join(BIN, "llama-server.exe")):
        return True  # versao compilada localmente (compilar_gpu.bat)
    if m.get("check_import") and importlib.util.find_spec(m["check_import"]) is None:
        return False
    return all(os.path.exists(p) for p in _paths(m))


def status():
    return {m["id"]: installed(m["id"]) for m in MODULES}


def recommended(vram_gb):
    """Instalacao completa recomendada para este PC."""
    ids = ["nucleo", "motor_traducao", "gemma1", "argos", "fala_cpu"]
    if vram_gb >= 2:
        ids.append("fala_gpu")
    if vram_gb >= 5:
        ids.append("gemma4")
    if vram_gb >= 11:
        ids.append("gemma12")
    return [m["id"] for m in MODULES if m["id"] in ids]


def with_deps(ids):
    out = []

    def add(i):
        for d in BY_ID[i]["requer"]:
            add(d)
        if i not in out:
            out.append(i)
    for i in ids:
        add(i)
    return out


# ---------------------------------------------------------------- instalacao

def emit(**kw):
    print(json.dumps(kw, ensure_ascii=False), flush=True)


def download(url, dest, mid, base_frac, span):
    """Baixa com retomada (.part) e informa o progresso."""
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    part = dest + ".part"
    for attempt in range(5):
        have = os.path.getsize(part) if os.path.exists(part) else 0
        req = urllib.request.Request(url, headers={"User-Agent": "Legendai", **({"Range": f"bytes={have}-"} if have else {})})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                if have and r.status != 206:  # servidor nao aceitou retomar: recomeca
                    have = 0
                total = have + int(r.headers.get("Content-Length") or 0)
                with open(part, "ab" if have else "wb") as f:
                    last = 0.0
                    while True:
                        chunk = r.read(1 << 20)
                        if not chunk:
                            break
                        f.write(chunk)
                        have += len(chunk)
                        if time.time() - last > 0.3 and total:
                            last = time.time()
                            emit(modulo=mid, progresso=round(base_frac + span * have / total, 3),
                                 etapa=f"Baixando {os.path.basename(dest)} — {have / 2**20:.0f} de {total / 2**20:.0f} MB")
            os.replace(part, dest)
            return
        except Exception as e:
            if attempt == 4:
                raise
            emit(modulo=mid, etapa=f"Conexão interrompida ({e}); tentando de novo…")
            time.sleep(3)


def install(mid):
    m = BY_ID[mid]
    if installed(mid):
        emit(modulo=mid, progresso=1.0, etapa="Já instalado", ok=True)
        return
    zips = m.get("zips", [])
    if zips == "llama":
        variant = "vulkan" if has_vulkan_gpu() else "cpu"
        zips = [(LLAMA_URL.format(variant), os.path.join(BIN, "llama"), "server+dll")]
    steps = (1 if m.get("pip") else 0) + len(zips) + len(m.get("files", []))
    done = 0
    if m.get("pip"):
        emit(modulo=mid, progresso=0.02, etapa="Instalando bibliotecas (pode levar alguns minutos)…")
        r = subprocess.run([sys.executable, "-m", "pip", "install", "--disable-pip-version-check",
                            "--no-warn-script-location", *m["pip"]], capture_output=True, text=True,
                           creationflags=subprocess.CREATE_NO_WINDOW)
        if r.returncode != 0:
            raise RuntimeError("pip: " + (r.stderr or r.stdout).strip().splitlines()[-1])
        done += 1
    for url, folder, filt in zips:
        tmp = os.path.join(BASE, "downloads", os.path.basename(url.split("?")[0]))
        download(url, tmp, mid, done / steps, 0.9 / steps)
        emit(modulo=mid, progresso=round((done + 0.9) / steps, 3), etapa="Extraindo…")
        os.makedirs(folder, exist_ok=True)
        with zipfile.ZipFile(tmp) as z:
            for name in z.namelist():
                keep = name.endswith((".dll", "llama-server.exe")) if filt == "server+dll" else filt in name
                if keep:
                    z.extract(name, folder)
        os.remove(tmp)
        done += 1
    for url, dest in m.get("files", []):
        download(url, dest, mid, done / steps, 1 / steps)
        done += 1
    if mid == "motor_traducao":  # o zip oficial traz tudo dentro de uma subpasta? deixa o exe em bin/llama
        _flatten(os.path.join(BIN, "llama"), "llama-server.exe")
    emit(modulo=mid, progresso=1.0, etapa="Instalado", ok=True)


def _flatten(folder, exe):
    if os.path.exists(os.path.join(folder, exe)):
        return
    for root, _, files in os.walk(folder):
        if exe in files:
            for f in os.listdir(root):
                shutil.move(os.path.join(root, f), os.path.join(folder, f))
            return


def remove(mid):
    m = BY_ID[mid]
    if not m.get("removivel", True):
        raise RuntimeError("este módulo é necessário e não pode ser removido")
    if mid == "motor_traducao":
        shutil.rmtree(os.path.join(BIN, "llama"), ignore_errors=True)
    elif mid == "argos":
        shutil.rmtree(os.path.join(MODELS, "translate-en_pt-1_9"), ignore_errors=True)
    else:
        for p in _paths(m):
            if os.path.exists(p):
                os.remove(p)
        for _, dest in m.get("files", []):  # pastas de modelo que ficaram vazias
            d = os.path.dirname(dest)
            if d != MODELS and os.path.isdir(d) and not os.listdir(d):
                os.rmdir(d)
    emit(modulo=mid, removido=True)


def main():
    cmd, ids = sys.argv[1], sys.argv[2:]
    try:
        if cmd == "instalar":
            for mid in with_deps(ids):
                install(mid)
        elif cmd == "remover":
            for mid in ids:
                remove(mid)
        emit(fim=True)
    except Exception as e:
        emit(erro=str(e))
        sys.exit(1)


if __name__ == "__main__":
    main()
