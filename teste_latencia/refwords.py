"""Gera refwords.json: instante de fim de cada palavra do audio de teste (referencia)."""
import json, os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import build_audio
from faster_whisper import WhisperModel
audio, marks = build_audio()
m = WhisperModel("small.en", device="cpu", compute_type="int8")
segs, _ = m.transcribe(audio, language="en", word_timestamps=True, vad_filter=False)
words = [{"w": w.word.strip(), "start": round(w.start, 3), "end": round(w.end, 3)} for s in segs for w in s.words]
json.dump(words, open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "refwords.json"), "w"), indent=0)
print(len(words), "palavras;", words[:4], words[-2:])
