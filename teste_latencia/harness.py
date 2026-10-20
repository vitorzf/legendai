"""Mede a latencia do pipeline do Legendai com audio de teste tocado em tempo real.
Uso: python harness.py <legendas.py a testar> <saida.json>
"""
import argparse, difflib, importlib.util, json, os, queue, re, sys, threading, time, wave
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(HERE)
SR = 16000
GAPS = [0.8, 0.7, 1.0, 0.8, 1.0, 0.7, 1.0, 3.0]


def build_audio():
    from faster_whisper.vad import VadOptions, get_speech_timestamps
    sents = json.load(open(os.path.join(HERE, "frases.json"), encoding="utf-8-sig"))
    parts, marks, pos = [np.zeros(SR, np.float32)], [], SR
    for i, gap in enumerate(GAPS):
        w = wave.open(os.path.join(HERE, f"f{i}.wav"))
        a = np.frombuffer(w.readframes(w.getnframes()), np.int16).astype(np.float32) / 32768
        sp = get_speech_timestamps(a, VadOptions(min_silence_duration_ms=500))
        a = a[sp[0]["start"]:sp[-1]["end"]]
        marks.append({"text": sents[i], "start": pos / SR, "end": (pos + len(a)) / SR,
                      "trim": sp[0]["start"] / SR})
        parts += [a, np.zeros(int(gap * SR), np.float32)]
        pos += len(a) + int(gap * SR)
    return np.concatenate(parts), marks


def main():
    mod_path, out_path = os.path.abspath(sys.argv[1]), sys.argv[2]
    sys.path.insert(0, PROJECT)
    spec = importlib.util.spec_from_file_location("legendas_under_test", mod_path)
    L = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(L)
    L.BASE = PROJECT
    for name in ("CAPTURE_EXE", "MODELS_DIR", "TRANSLATION_DIR", "WHISPER_SERVER", "LLAMA_SERVER"):
        if hasattr(L, name):
            setattr(L, name, getattr(L, name).replace(os.path.dirname(mod_path), PROJECT))
    if hasattr(L, "LLM_MODELS"):
        L.LLM_MODELS = [p.replace(os.path.dirname(mod_path), PROJECT) for p in L.LLM_MODELS]

    # Variacoes para experimentos (variaveis de ambiente):
    #   LEG_SET="PARTIAL_STEP=0.1;Recognizer.PAUSE=0.2"  troca parametros
    #   LEG_INSTANT=1   "traducao" instantanea (mede so o reconhecimento)
    #   LEG_LLM / LEG_LLM_PARCIAL = arquivo .gguf (ou 'nenhum') dos tradutores
    for item in filter(None, os.environ.get("LEG_SET", "").split(";")):
        name, val = item.split("=")
        obj, attr = (getattr(L, name.split(".")[0]), name.split(".")[1]) if "." in name else (L, name)
        setattr(obj, attr, float(val))
        print("ajuste:", name, "=", val)
    if os.environ.get("LEG_INSTANT"):
        class Instant:
            name, context = "instantaneo", []

            def __call__(self, text, final=True, on_update=None):
                if on_update:
                    on_update(text)
                return text
        L.load_translators = lambda args: (Instant(), Instant())

    audio, marks = build_audio()
    chunk = getattr(L, "CAPTURE_CHUNK", 0.2)
    t0 = {}
    done = threading.Event()

    def fake_capture(program, audio_q, status):
        n = int(chunk * SR)
        t0["t"] = time.monotonic()
        for k in range(0, len(audio), n):
            target = t0["t"] + (k + n) / SR  # o bloco so fica disponivel quando termina de tocar
            time.sleep(max(0, target - time.monotonic()))
            audio_q.put(audio[k:k + n].copy())
        done.set()

    L.capture_loop = fake_capture
    models = os.path.join(PROJECT, "models")
    args = argparse.Namespace(
        programa="teste", motor="gpu", modelo_gpu=os.path.join(models, "ggml-large-v3-turbo-q8_0.bin"),
        modelo="small.en", tradutor="llm",
        modelo_llm=os.environ.get("LEG_LLM") or os.path.join(models, "gemma-3-12b-it-Q4_K_M.gguf"),
        modelo_llm_parcial=os.environ.get("LEG_LLM_PARCIAL") or os.path.join(models, "gemma-3-4b-it-Q4_K_M.gguf"),
        parcial="sim", ingles=False, fonte=26, altura=8, duracao=4, threads=4)
    if os.environ.get("LEG_PRESET"):  # usa exatamente os argumentos que o painel passaria nesse nivel
        sys.path.insert(0, PROJECT)
        import types
        import launcher
        cfg = dict(launcher.DEFAULTS, intensidade=os.environ["LEG_PRESET"])
        cfg.update(launcher.PRESETS[cfg["intensidade"]])
        argv = launcher.MainWindow.build_args(types.SimpleNamespace(cfg=cfg))[1:]
        args = L.build_parser().parse_args(argv)
        args.programa = "teste"
        L.apply_cadence(args.cadencia)
        print("nivel", cfg["intensidade"], ":", " ".join(a if "\\" not in a else os.path.basename(a) for a in argv))

    audio_q, subs_q = queue.Queue(), queue.Queue()
    threading.Thread(target=L.worker, args=(args, audio_q, subs_q, lambda s: None), daemon=True).start()

    cpu_samples = []

    def sample_cpu():  # uso de CPU deste processo + servidores filhos, em % de um nucleo
        import psutil
        me = psutil.Process()
        while True:
            procs = [me] + me.children(recursive=True)
            for p in procs:
                try:
                    p.cpu_percent(None)
                except psutil.Error:
                    pass
            time.sleep(1)
            if "t" in t0 and not done.is_set():
                total = 0.0
                for p in procs:
                    try:
                        total += p.cpu_percent(None)
                    except psutil.Error:
                        pass
                cpu_samples.append(total / psutil.cpu_count())
    threading.Thread(target=sample_cpu, daemon=True).start()

    events = []
    started = time.monotonic()
    while not (done.is_set() and time.monotonic() - t0.get("t", 0) > len(audio) / SR + 1):
        try:
            item = subs_q.get(timeout=0.05)
        except queue.Empty:
            if "t" not in t0 and time.monotonic() - started > 120:
                print("ERRO: o pipeline nao comecou em 120 s"); os._exit(1)
            continue
        if "t" not in t0:
            continue
        kind, pt, en = (item[0], item[-2], item[-1])
        events.append({"t": time.monotonic() - t0["t"], "kind": kind, "pt": pt, "en": en})

    # ---- analise
    def words(s):
        return re.findall(r"[a-z']+", s.lower())

    def owner(en):
        w = set(words(en))
        scores = [len(w & set(words(m["text"]))) / max(1, len(set(words(m["text"])))) for m in marks]
        best = int(np.argmax(scores))
        return best if scores[best] >= 0.15 else None

    res = []
    for i, m in enumerate(marks):
        evs = [e for e in events if e["en"] and owner(e["en"]) == i]
        finals = [e for e in evs if e["kind"] == "final"]
        first = min((e["t"] for e in evs), default=None)
        last_final = max((e["t"] for e in finals), default=None)
        res.append({
            "frase": m["text"][:48], "fala_s": round(m["end"] - m["start"], 2),
            "primeiro_texto_apos_inicio": None if first is None else round(first - m["start"], 2),
            "final_apos_fim_da_fala": None if last_final is None else round(last_final - m["end"], 2),
            "parciais": sum(e["kind"] == "partial" for e in evs), "finais": len(finals),
        })
    final_en = " ".join(e["en"] for e in events if e["kind"] == "final")
    ref = " ".join(m["text"] for m in marks)
    acc = difflib.SequenceMatcher(None, words(final_en), words(ref)).ratio()
    cpu = round(float(np.mean(cpu_samples)), 1) if cpu_samples else None
    print("uso medio de CPU (% do processador inteiro):", cpu)
    out = {"resultado": res, "acerto_palavras_en": round(acc, 3), "cpu_medio": cpu,
           "finais_pt": [e["pt"] for e in events if e["kind"] == "final"], "eventos": events}
    json.dump(out, open(out_path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    for r in res:
        print(r)
    print("acerto EN:", round(acc, 3))
    os._exit(0)


if __name__ == "__main__":
    main()
