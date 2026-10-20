import os, sys, time, statistics as st
sys.path.insert(0, ".."); import legendas as L
S = ["Hey, are you coming to the party tonight?", "I don't know, I still have a lot of work",
     "Where did you put the keys?", "You never listen to me, that's the problem.", "Okay, okay, I'm sorry, I will call"]
for f in ("gemma-3-1b-it-Q4_K_M.gguf", "gemma-3-4b-it-Q4_K_M.gguf", "gemma-3-12b-it-Q4_K_M.gguf"):
    tr = L.LlmTranslator(os.path.join(L.MODELS_DIR, f), 4)
    ttft, full, outs = [], [], []
    for s in S:
        for rep in range(2):
            tr.cache.clear(); t0 = time.perf_counter(); first = []
            o = tr(s + " " * rep, final=False, on_update=lambda pt: first.append(time.perf_counter()) or None)
            ttft.append(first[0] - t0); full.append(time.perf_counter() - t0)
        outs.append(o)
    print(f"{f[:13]}: 1a palavra {st.median(ttft)*1000:.0f} ms | frase inteira {st.median(full)*1000:.0f} ms")
    for o in outs: print("     ", o)
    tr.proc.kill(); tr.proc.wait(); time.sleep(1)
ar = L.ArgosTranslator(4); ar("x"); t = []
for s in S: t0 = time.perf_counter(); o = ar(s, final=False); t.append(time.perf_counter() - t0)
print(f"Argos (CPU): frase inteira {st.median(t)*1000:.0f} ms  ex.: {o}")
