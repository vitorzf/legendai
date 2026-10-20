import os, sys, time, wave, statistics as st
import numpy as np
sys.path.insert(0, ".."); import legendas as L
w = wave.open("f4.wav"); a = np.frombuffer(w.readframes(w.getnframes()), np.int16).astype(np.float32) / 32768
print(f"audio: {len(a)/16000:.1f} s")
for name in ("base.en", "small.en"):
    for thr in (6, 12):
        b = L.FasterWhisperBackend(name, thr); b(a[:16000], None)
        ts = []
        for _ in range(3):
            t0 = time.perf_counter(); out = b(a, None, beam=1); ts.append(time.perf_counter() - t0)
        print(f"Whisper {name} CPU {thr} threads: {st.median(ts)*1000:.0f} ms -> {' '.join(s['text'].strip() for s in out)!r}")
text = "Where did you put the keys? They were right here on the table a minute ago."
for f, thr in (("gemma-3-1b-it-Q4_K_M.gguf", 6), ("gemma-3-1b-it-Q4_K_M.gguf", 12), ("gemma-3-4b-it-Q4_K_M.gguf", 12)):
    tr = L.LlmTranslator(os.path.join(L.MODELS_DIR, f), thr, gpu_layers=0)
    ts = []
    for i in range(3):
        tr.cache.clear(); t0 = time.perf_counter(); o = tr(text + " " * i, final=False); ts.append(time.perf_counter() - t0)
    print(f"{f[:12]} CPU {thr} threads: {st.median(ts)*1000:.0f} ms -> {o!r}")
    tr.proc.kill(); tr.proc.wait(); time.sleep(1)
ar = L.ArgosTranslator(6); ar("x"); t0 = time.perf_counter(); o = ar(text); print(f"Argos CPU: {(time.perf_counter()-t0)*1000:.0f} ms -> {o!r}")
