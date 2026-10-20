"""
Teste rapido deste PC: mede quanto tempo cada modelo de fala e de traducao leva aqui,
para o Legendai escolher sozinho a melhor configuracao.
Escreve uma linha JSON por etapa ({"progresso", "etapa"}) e no fim {"resultado": {...}}.
"""
import json
import os
import statistics as st
import sys
import time
import wave

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import desempenho as D  # noqa: E402
import legendas as L  # noqa: E402

SAMPLE = os.path.join(D.BASE, "assets", "amostra.wav")
SENTENCES = ["Where did you put the keys? They were right here on the table a minute ago.",
             "I don't know, I still have a lot of work to finish before tomorrow morning."]


def emit(**kw):
    print(json.dumps(kw, ensure_ascii=False), flush=True)


def timeit(fn, n=3):
    fn()  # aquecimento
    ts = []
    for _ in range(n):
        t0 = time.perf_counter()
        fn()
        ts.append(time.perf_counter() - t0)
    return round(st.median(ts), 3)


def main():
    L.log = lambda msg: None  # silencia os logs do legendas.py
    w = wave.open(SAMPLE)
    audio = np.frombuffer(w.readframes(w.getnframes()), np.int16).astype(np.float32) / 32768
    name, vram = D.gpu_info()
    threads = max(1, (os.cpu_count() or 4) // 2)
    res = {"gpu": name, "vram": round(vram, 1), "whisper_gpu": None, "whisper_cpu": {},
           "llm_gpu": {}, "llm_cpu": {}, "argos": None, "data": time.strftime("%Y-%m-%d %H:%M")}
    llama_ok = D.llama_ready()

    def translate_time(tr):
        k = [0]

        def run():  # textos sempre diferentes, para nao usar o cache
            k[0] += 1
            for s in SENTENCES:
                tr(s + " " * k[0], final=False)
        return round(timeit(run) / len(SENTENCES), 3)

    steps = []
    if D.gpu_ready() and vram >= 2:
        steps.append(("Reconhecimento de fala na placa de vídeo", "whisper_gpu", None))
        for key in ("gemma1", "gemma4", "gemma12"):
            need = {"gemma1": 3, "gemma4": 5, "gemma12": 11}[key]
            if llama_ok and D.has_model(key) and vram >= need:
                steps.append((f"Tradutor {key.replace('gemma', 'Gemma ')}B na placa de vídeo", "llm_gpu", key))
    for name, label in (("base.en", "Base"), ("small.en", "Small")):
        if D.has_cpu_model(name):
            steps.append((f"Reconhecimento de fala no processador ({label})", "whisper_cpu", name))
    if llama_ok and D.has_model("gemma1"):
        steps.append(("Tradutor Gemma 1B no processador", "llm_cpu", "gemma1"))
    if os.path.isdir(os.path.join(D.MODELS, "translate-en_pt-1_9")):
        steps.append(("Tradutor Argos", "argos", None))

    for i, (label, kind, key) in enumerate(steps):
        emit(progresso=round(i / len(steps), 2), etapa=label)
        try:
            if kind == "whisper_gpu":
                b = L.WhisperCppBackend(os.path.join(D.MODELS, "ggml-large-v3-turbo-q8_0.bin"), threads)
                res["whisper_gpu"] = timeit(lambda: b(audio, None, beam=1))
                b.proc.kill()
            elif kind == "llm_gpu":
                tr = L.LlmTranslator(os.path.join(D.MODELS, D.MODEL_FILES[key]), threads, gpu_layers=99)
                res["llm_gpu"][key] = translate_time(tr)
                tr.proc.kill()
                tr.proc.wait()
            elif kind == "whisper_cpu":
                if key == "small.en" and (res["whisper_cpu"].get("base.en") or 9) > 1.5:
                    continue  # se o Base ja e lento, o Small nao vai servir
                b = L.FasterWhisperBackend(key, threads)
                res["whisper_cpu"][key] = timeit(lambda: b(audio, None, beam=1), n=2)
            elif kind == "llm_cpu":
                tr = L.LlmTranslator(os.path.join(D.MODELS, D.MODEL_FILES[key]), threads, gpu_layers=0)
                res["llm_cpu"][key] = translate_time(tr)
                tr.proc.kill()
                tr.proc.wait()
            elif kind == "argos":
                tr = L.ArgosTranslator(threads)
                res["argos"] = translate_time(tr)
        except Exception as e:  # o nivel que depende disso simplesmente nao sera oferecido
            emit(aviso=f"{label}: {e}")
    emit(progresso=1.0, etapa="Pronto")
    emit(resultado=res)
    os._exit(0)  # encerra ja, junto com os servidores abertos


if __name__ == "__main__":
    main()
