"""Reconhecimento em streaming de verdade (sherpa-onnx Zipformer) no mesmo audio de teste."""
import glob, json, os, sys, time
import numpy as np, sherpa_onnx
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import build_audio
d = glob.glob(os.path.join(os.environ["TEMP"], "sherpa_models", "*zipformer*"))[0]
f = lambda p: glob.glob(os.path.join(d, p))[0]
rec = sherpa_onnx.OnlineRecognizer.from_transducer(
    tokens=f("tokens.txt"), encoder=f("encoder*int8.onnx"), decoder=f("decoder*int8.onnx")
    if glob.glob(os.path.join(d, "decoder*int8.onnx")) else f("decoder*.onnx"), joiner=f("joiner*int8.onnx"),
    num_threads=4, sample_rate=16000, feature_dim=80, enable_endpoint_detection=True,
    rule1_min_trailing_silence=2.4, rule2_min_trailing_silence=0.2, rule3_min_utterance_length=20,
    decoding_method="greedy_search")
audio, _ = build_audio()
chunk = int(float(sys.argv[2]) * 16000) if len(sys.argv) > 2 else 1600
s = rec.create_stream(); events, last, comp = [], "", []
for k in range(0, len(audio), chunk):
    t0 = time.perf_counter()
    s.accept_waveform(16000, audio[k:k + chunk])
    while rec.is_ready(s):
        rec.decode_stream(s)
    text = rec.get_result(s).strip()
    dt = time.perf_counter() - t0; comp.append(dt)
    t = (k + chunk) / 16000 + dt  # o bloco so existe quando termina de tocar, mais o processamento
    if text and text != last:
        events.append({"t": t, "kind": "partial", "pt": text, "en": text}); last = text
    if rec.is_endpoint(s):
        if text:
            events.append({"t": t, "kind": "final", "pt": text, "en": text})
        rec.reset(s); last = ""
json.dump({"eventos": events}, open(sys.argv[1], "w"), indent=0)
print(f"processamento por bloco de {chunk/16000*1000:.0f} ms: mediana {np.median(comp)*1000:.1f} ms, max {max(comp)*1000:.1f} ms")
print("finais:", [e["en"] for e in events if e["kind"] == "final"])
