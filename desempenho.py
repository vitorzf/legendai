"""
Niveis de desempenho do Legendai: uma escada do mais rapido ao mais preciso,
quais niveis cabem em cada PC e a escolha automatica a partir do teste do PC (autoajuste.py).
"""
import os

BASE = os.path.dirname(os.path.abspath(__file__))
MODELS = os.path.join(BASE, "models")
BIN = os.path.join(BASE, "bin")

MODEL_FILES = {
    "gemma12": "gemma-3-12b-it-Q4_K_M.gguf",
    "gemma4": "gemma-3-4b-it-Q4_K_M.gguf",
    "gemma1": "gemma-3-1b-it-Q4_K_M.gguf",
}

# Escada do mais rapido (0) ao mais preciso. Os niveis de processador e os de placa de video
# nao se misturam na barra: cada PC usa so os que cabem nele.
LEVELS = [
    {"key": "cpu_turbo", "nome": "Ultraleve", "qualidade": "tradução simples (português de Portugal)",
     "gpu": False, "vram": 0,
     "cfg": dict(motor="cpu", modelo_cpu="base.en", tradutor="argos", parcial_modelo="nenhum",
                 tradutor_gpu=False, parcial=False, cadencia="economica", beam_final=1)},
    {"key": "cpu_leve", "nome": "Leve", "qualidade": "tradução boa",
     "gpu": False, "vram": 0,
     "cfg": dict(motor="cpu", modelo_cpu="base.en", tradutor="gemma1", parcial_modelo="nenhum",
                 tradutor_gpu=False, parcial=False, cadencia="economica", beam_final=1)},
    {"key": "cpu_preciso", "nome": "Leve e preciso", "qualidade": "reconhece melhor a fala, tradução boa",
     "gpu": False, "vram": 0,
     "cfg": dict(motor="cpu", modelo_cpu="small.en", tradutor="gemma1", parcial_modelo="nenhum",
                 tradutor_gpu=False, parcial=False, cadencia="economica", beam_final=1)},
    {"key": "gpu_relampago", "nome": "Relâmpago", "qualidade": "tradução boa",
     "gpu": True, "vram": 3,
     "cfg": dict(motor="gpu", modelo_cpu="small.en", tradutor="gemma1", parcial_modelo="nenhum",
                 tradutor_gpu=True, parcial=True, cadencia="rapida", beam_final=1)},
    {"key": "gpu_agil", "nome": "Ágil", "qualidade": "tradução muito boa",
     "gpu": True, "vram": 5,
     "cfg": dict(motor="gpu", modelo_cpu="small.en", tradutor="gemma4", parcial_modelo="gemma1",
                 tradutor_gpu=True, parcial=True, cadencia="rapida", beam_final=1)},
    {"key": "gpu_equilibrado", "nome": "Equilibrado", "qualidade": "tradução muito boa",
     "gpu": True, "vram": 5,
     "cfg": dict(motor="gpu", modelo_cpu="small.en", tradutor="gemma4", parcial_modelo="nenhum",
                 tradutor_gpu=True, parcial=True, cadencia="normal", beam_final=1)},
    {"key": "gpu_preciso", "nome": "Preciso", "qualidade": "tradução excelente",
     "gpu": True, "vram": 11,
     "cfg": dict(motor="gpu", modelo_cpu="small.en", tradutor="gemma12", parcial_modelo="gemma4",
                 tradutor_gpu=True, parcial=True, cadencia="rapida", beam_final=1)},
    {"key": "gpu_maximo", "nome": "Máxima precisão", "qualidade": "tradução excelente, reconhecimento mais cuidadoso",
     "gpu": True, "vram": 11,
     "cfg": dict(motor="gpu", modelo_cpu="small.en", tradutor="gemma12", parcial_modelo="gemma4",
                 tradutor_gpu=True, parcial=True, cadencia="normal", beam_final=3)},
]
LEVEL_KEYS = [lv["key"] for lv in LEVELS]
PAUSE = 0.35  # silencio que fecha a frase (igual ao Recognizer do legendas.py)


def gpu_info():
    """(nome, GB de memoria) da placa de video com mais memoria dedicada."""
    import winreg
    best = ("", 0.0)
    path = r"SYSTEM\ControlSet001\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}"
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path) as k:
            for i in range(winreg.QueryInfoKey(k)[0]):
                try:
                    with winreg.OpenKey(k, winreg.EnumKey(k, i)) as s:
                        mem = winreg.QueryValueEx(s, "HardwareInformation.qwMemorySize")[0]
                        name = winreg.QueryValueEx(s, "DriverDesc")[0]
                except OSError:
                    continue
                if isinstance(mem, bytes):
                    mem = int.from_bytes(mem, "little")
                if mem / 2 ** 30 > best[1]:
                    best = (name, mem / 2 ** 30)
    except OSError:
        pass
    return best


def level(key):
    return LEVELS[LEVEL_KEYS.index(key)] if key in LEVEL_KEYS else LEVELS[0]


def llama_ready():
    return any(os.path.exists(p) for p in (os.path.join(BIN, "llama", "llama-server.exe"),
                                            os.path.join(BIN, "llama-server.exe")))


def has_cpu_model(name):
    """Modelo de fala do processador instalado pelos modulos (ou ja no cache do Hugging Face)."""
    if os.path.isdir(os.path.join(MODELS, f"faster-whisper-{name}")):
        return True
    cache = os.path.join(os.path.expanduser("~"), ".cache", "huggingface", "hub",
                         f"models--Systran--faster-whisper-{name}")
    return os.path.isdir(cache)


def has_model(key):
    return key == "argos" or os.path.exists(os.path.join(MODELS, MODEL_FILES.get(key, "?")))


def gpu_ready():
    return (os.path.exists(os.path.join(BIN, "whisper-server.exe"))
            and os.path.exists(os.path.join(MODELS, "ggml-large-v3-turbo-q8_0.bin")))


def feasible(lv, vram_gb, bench=None):
    """O nivel cabe neste PC? (placa, memoria de video, programas e modelos instalados,
    e o que o teste do PC conseguiu de fato rodar)."""
    c = lv["cfg"]
    for key in (c["tradutor"], c["parcial_modelo"]):
        if key != "nenhum" and not has_model(key):
            return False
    if c["tradutor"] != "argos" and not llama_ready():
        return False
    if c["tradutor"] == "argos" and not os.path.isdir(os.path.join(MODELS, "translate-en_pt-1_9")):
        return False
    if not lv["gpu"] and not has_cpu_model(c["modelo_cpu"]):
        return False
    if lv["gpu"]:
        if not gpu_ready() or vram_gb < lv["vram"]:
            return False
        if bench is not None:
            if bench.get("whisper_gpu") is None:
                return False
            for key in (c["tradutor"], c["parcial_modelo"]):
                if key != "nenhum" and bench.get("llm_gpu", {}).get(key) is None:
                    return False
    elif bench is not None:
        if bench.get("whisper_cpu", {}).get(c["modelo_cpu"]) is None:
            return False
        if c["tradutor"] != "argos" and bench.get("llm_cpu", {}).get(c["tradutor"]) is None:
            return False
    return True


def available(vram_gb, bench=None):
    """Niveis que aparecem na barra deste PC: os de placa de video, se houver algum; senao os de processador."""
    ok = [lv for lv in LEVELS if feasible(lv, vram_gb, bench)]
    gpu = [lv for lv in ok if lv["gpu"]]
    return gpu or [lv for lv in ok if not lv["gpu"]] or [LEVELS[0]]


def estimate(lv, bench):
    """(segundos ate a legenda final depois que a pessoa para de falar, segundos por atualizacao parcial)
    estimados com os tempos medidos neste PC. None se o teste nao cobriu este nivel."""
    if not bench:
        return None
    c = lv["cfg"]
    if lv["gpu"]:
        stt = bench.get("whisper_gpu")
        if stt is not None and c["beam_final"] > 1:
            stt *= 1.3
        tr = lambda k: bench.get("llm_gpu", {}).get(k)
    else:
        stt = bench.get("whisper_cpu", {}).get(c["modelo_cpu"])
        tr = lambda k: bench.get("argos") if k == "argos" else bench.get("llm_cpu", {}).get(k)
    t_final = tr(c["tradutor"])
    if stt is None or t_final is None:
        return None
    t_part = tr(c["parcial_modelo"]) if c["parcial_modelo"] != "nenhum" else t_final
    if c["parcial"]:
        # Com parciais, a final quase sempre aproveita a ultima parcial ja traduzida.
        final = PAUSE + stt + 0.2 * t_final
        partial = stt + (t_part or t_final)
    else:
        final, partial = PAUSE + stt + t_final, None
    return final, partial


def auto_choice(vram_gb, bench, max_partial=0.7):
    """Nivel mais preciso que ainda responde rapido neste PC."""
    levels = available(vram_gb, bench)
    # Sem placa de video, aceita esperar um pouco mais para ter portugues do Brasil.
    max_final = 1.2 if levels[0]["gpu"] else 2.5
    good = []
    for lv in levels:
        est = estimate(lv, bench)
        if est and est[0] <= max_final and (est[1] is None or est[1] <= max_partial):
            good.append(lv)
    if good:
        return good[-1]
    timed = [(estimate(lv, bench), lv) for lv in levels]
    timed = [(e[0], lv) for e, lv in timed if e]
    return min(timed, key=lambda x: x[0])[1] if timed else levels[0]


def heuristic_choice(vram_gb):
    """Sem teste do PC: escolhe pelo tamanho da memoria de video."""
    levels = available(vram_gb)
    if levels[0]["gpu"]:
        pick = "gpu_preciso" if vram_gb >= 11 else "gpu_equilibrado" if vram_gb >= 5 else "gpu_relampago"
        return next((lv for lv in levels if lv["key"] == pick), levels[len(levels) // 2])
    return next((lv for lv in levels if lv["key"] == "cpu_leve"), levels[0])
