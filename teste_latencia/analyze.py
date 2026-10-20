"""Compara as medicoes de latencia salvas pelo harness.py. Uso: python analyze.py antes.json depois.json"""
import difflib, json, re, statistics as st, sys
sys.path.insert(0, __file__.rsplit("\\", 1)[0].rsplit("/", 1)[0])
from harness import build_audio

_, MARKS = build_audio()


def words(s):
    return re.findall(r"[a-z']+", s.lower())


def owner(en):
    w = set(words(en))
    scores = [len(w & set(words(m["text"]))) / max(1, len(set(words(m["text"])))) for m in MARKS]
    best = max(range(len(scores)), key=scores.__getitem__)
    return best if scores[best] >= 0.15 else None


def ref_words():
    """Inicio/fim exato de cada palavra (o sintetizador de voz informa o instante de cada uma)."""
    tts = json.load(open(__file__.replace("analyze.py", "ttswords.json"), encoding="utf-8-sig"))
    out = []
    for i, m in enumerate(MARKS):
        raw = tts[i]["value"] if isinstance(tts[i], dict) else tts[i]
        items = [(float(x.split("|", 1)[0]), x.split("|", 1)[1]) for x in raw]
        starts = [m["start"] + t - m["trim"] for t, _ in items]
        for k, (_, w) in enumerate(items):
            end = starts[k + 1] if k + 1 < len(items) else m["end"]
            for ww in words(w):
                out.append({"w": ww, "sent": i, "end": end})
    return out


REF = ref_words()


def word_lag(ev):
    """Para cada palavra: tempo entre o fim dela e a primeira legenda COMPLETA (traducao terminada)
    cujo texto em ingles ja a contem."""
    complete = [e for k, e in enumerate(ev) if e["kind"] == "final"
                or (e["kind"] == "partial" and (k + 1 == len(ev) or ev[k + 1]["en"] != e["en"]))]
    first_seen = {}
    for e in complete:
        i = owner(e["en"]) if e["en"] else None
        if i is None:
            continue
        ref_idx = [j for j, r in enumerate(REF) if r["sent"] == i]
        ref = [REF[j]["w"] for j in ref_idx]
        sm = difflib.SequenceMatcher(None, words(e["en"]), ref, autojunk=False)
        for blk in sm.get_matching_blocks():
            for d in range(blk.size):
                j = ref_idx[blk.b + d]
                if j not in first_seen and e["t"] >= REF[j]["end"] - 0.05:
                    first_seen[j] = e["t"]
    lags = sorted(first_seen[j] - REF[j]["end"] for j in first_seen)
    return lags, len(first_seen) / len(REF)


def analyze(path):
    ev = json.load(open(path, encoding="utf-8"))["eventos"]
    first, final, gaps = [], [], []
    for i, m in enumerate(MARKS):
        nxt = MARKS[i + 1]["start"] if i + 1 < len(MARKS) else m["end"] + 3
        win = [e for e in ev if m["start"] <= e["t"] < nxt and not (e["en"] and owner(e["en"]) == i - 1)]
        if win:
            first.append(win[0]["t"] - m["start"])
        # Primeira legenda final depois que a frase termina (nao depende de o texto estar certo).
        fin = [e["t"] for e in ev if e["kind"] == "final" and m["end"] - 0.1 < e["t"] < nxt + 1.5]
        if fin:
            final.append(min(fin) - m["end"])
        upd = [e["t"] for e in ev if m["start"] <= e["t"] <= m["end"]]
        gaps += [b - a for a, b in zip(upd, upd[1:])]
    fin_en = " ".join(e["en"] for e in ev if e["kind"] == "final")
    acc = difflib.SequenceMatcher(None, words(fin_en), words(" ".join(m["text"] for m in MARKS))).ratio()
    lags, cover = word_lag(ev)
    q = lambda p: lags[min(len(lags) - 1, int(p * len(lags)))]
    return {
        "ATRASO PALAVRA -> TELA (mediana)": st.median(lags),
        "atraso palavra -> tela (p90)": q(0.9),
        "palavras com atraso <= 200 ms": f"{sum(l <= 0.2 for l in lags) / len(lags):.0%}",
        "palavras reconhecidas": f"{cover:.0%}",
        "1o texto após início da fala (média)": st.mean(first),
        "1o texto (pior caso)": max(first),
        "legenda final após fim da fala (média)": st.mean(final),
        "legenda final (pior caso)": max(final),
        "intervalo entre atualizações durante a fala (mediana)": st.median(gaps) if gaps else float("nan"),
        "frases com final": f"{len(final)}/{len(MARKS)}",
        "acerto do texto EN (0-1)": acc,
    }


if __name__ == "__main__":
    res = [analyze(p) for p in sys.argv[1:]]
    names = ["antes", "depois"][:len(res)]
    print(f"{'métrica':58s}" + "".join(f"{n:>10s}" for n in names))
    for k in res[0]:
        vals = [r[k] for r in res]
        print(f"{k:58s}" + "".join(f"{v:>10.2f}" if isinstance(v, float) else f"{v:>10s}" for v in vals))
