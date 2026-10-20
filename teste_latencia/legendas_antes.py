"""
Legendas ao vivo: ouve o audio de um programa (Chrome por padrao), transcreve
o ingles com Whisper e mostra a traducao em portugues numa legenda por cima da tela.
Tudo roda localmente, sem internet (depois de baixar os modelos na 1a vez).

Uso:
    python legendas.py                      # Chrome
    python legendas.py --programa vivaldi   # outro programa (nome do .exe)
    python legendas.py --ingles             # mostra tambem o texto original
"""
import argparse
import ctypes
import io
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
import tkinter as tk
import urllib.request
import wave
import zipfile

import numpy as np
import psutil

BASE = os.path.dirname(os.path.abspath(__file__))
CAPTURE_EXE = os.path.join(BASE, "capture", "ProcAudioCapture.exe")
MODELS_DIR = os.path.join(BASE, "models")
TRANSLATION_DIR = os.path.join(MODELS_DIR, "translate-en_pt-1_9")
TRANSLATION_URL = "https://argos-net.com/v1/translate-en_pt-1_9.argosmodel"
WHISPER_SERVER = os.path.join(BASE, "bin", "whisper-server.exe")
LLAMA_SERVER = os.path.join(BASE, "bin", "llama-server.exe")
LLM_MODELS = [os.path.join(MODELS_DIR, f) for f in ("gemma-3-12b-it-Q4_K_M.gguf", "gemma-3-4b-it-Q4_K_M.gguf")]

SR = 16000

# Frases que o Whisper costuma "inventar" em silencio ou musica.
HALLUCINATIONS = {
    "you", "thank you", "thank you.", "thanks for watching!", "thanks for watching.",
    "thank you for watching.", "bye.", "music", "(music)", "[music]", "♪", "...",
}


def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


# ---------------------------------------------------------------- captura

def find_root_process(name):
    """PID do processo principal (o que nao tem pai com o mesmo nome)."""
    exe = name.lower() if name.lower().endswith(".exe") else name.lower() + ".exe"
    candidates = []
    for p in psutil.process_iter(["pid", "name", "ppid", "create_time"]):
        if (p.info["name"] or "").lower() != exe:
            continue
        try:
            parent = psutil.Process(p.info["ppid"])
            if (parent.name() or "").lower() == exe:
                continue
        except psutil.Error:
            pass
        candidates.append(p.info)
    if not candidates:
        return None
    return min(candidates, key=lambda i: i["create_time"])["pid"]


def capture_loop(program, audio_q, status):
    """Mantem o helper de captura rodando e joga o audio na fila."""
    waiting_logged = False
    while True:
        pid = find_root_process(program)
        if pid is None:
            if not waiting_logged:
                log(f"Esperando o {program} abrir...")
                waiting_logged = True
            status(f"Esperando o {program} abrir...")
            time.sleep(2)
            continue
        waiting_logged = False
        log(f"Capturando audio do {program} (PID {pid})")
        proc = subprocess.Popen(
            [CAPTURE_EXE, str(pid)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        kill_with_parent(proc)
        threading.Thread(target=_pipe_stderr, args=(proc,), daemon=True).start()
        status(None)
        while True:
            data = proc.stdout.read(SR // 5 * 2)  # blocos de 200 ms
            if not data:
                break
            audio_q.put(np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0)
        proc.wait()
        log(f"Captura encerrada (codigo {proc.returncode}); tentando de novo...")
        time.sleep(2)


def _pipe_stderr(proc):
    for line in proc.stderr:
        log("[captura] " + line.decode(errors="replace").strip())


# ---------------------------------------------------------------- tradutor

def ensure_translation_model():
    if os.path.isdir(TRANSLATION_DIR):
        return
    os.makedirs(MODELS_DIR, exist_ok=True)
    zpath = os.path.join(MODELS_DIR, "en_pt.zip")
    log("Baixando modelo de traducao ingles->portugues (~80 MB)...")
    urllib.request.urlretrieve(TRANSLATION_URL, zpath)
    with zipfile.ZipFile(zpath) as z:
        z.extractall(MODELS_DIR)
    os.remove(zpath)


def split_sentences(text):
    # Os modelos traduzem uma frase por vez; com varias juntas eles pulam algumas.
    return [s for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]


class ArgosTranslator:
    """Opus-MT/Argos: muito rapido, mas escreve em portugues de Portugal."""

    name = "Argos en->pt (PT-PT)"

    def __init__(self, threads):
        import ctranslate2
        import sentencepiece as spm
        ensure_translation_model()
        self.sp = spm.SentencePieceProcessor(model_file=os.path.join(TRANSLATION_DIR, "sentencepiece.model"))
        self.tr = ctranslate2.Translator(os.path.join(TRANSLATION_DIR, "model"), device="cpu",
                                         compute_type="int8", intra_threads=threads)

    def __call__(self, text, final=True):
        beam_size = 4 if final else 1
        batch = [self.sp.encode(s, out_type=str) for s in split_sentences(text)]
        results = self.tr.translate_batch(batch, beam_size=beam_size, max_decoding_length=256)
        return " ".join("".join(r.hypotheses[0]).replace("▁", " ").strip() for r in results)


def start_server(cmd, url, what, log_filter):
    """Abre um servidor local (whisper.cpp / llama.cpp) e espera ele ficar pronto."""
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                            cwd=os.path.dirname(cmd[0]), creationflags=subprocess.CREATE_NO_WINDOW)
    kill_with_parent(proc)

    def pipe():
        for line in proc.stderr:
            line = line.decode(errors="replace").strip()
            if any(k in line.lower() for k in log_filter):
                log(f"[{what}] {line}")
    threading.Thread(target=pipe, daemon=True).start()

    deadline = time.time() + 180
    while True:
        if proc.poll() is not None:
            raise RuntimeError(f"{what} fechou ao iniciar (veja as mensagens acima)")
        try:
            with urllib.request.urlopen(url + "/health", timeout=2) as r:
                if r.status == 200:
                    return proc
        except Exception:
            pass
        if time.time() > deadline:
            raise RuntimeError(f"{what} nao respondeu")
        time.sleep(0.5)


class LlmTranslator:
    """Modelo de linguagem (Gemma 3) na placa de video via llama.cpp: portugues do Brasil natural."""

    PORT = 8179
    INSTRUCTIONS = (
        "Você traduz legendas de filmes e séries do inglês para o português do Brasil. "
        "Escreva como numa legenda brasileira profissional: natural, coloquial quando a fala for coloquial, "
        "curta e fácil de ler. Use sempre o português brasileiro (você, celular, cara, a gente, tá), "
        "nunca o de Portugal (tu, telemóvel, estou a fazer). Mantenha nomes próprios. "
        "Se a fala estiver incompleta, traduza só o que já foi dito. "
        "Responda APENAS com a tradução, sem aspas, sem comentários."
    )

    def __init__(self, model_path, threads):
        self.name = f"{os.path.basename(model_path)} (GPU, PT-BR)"
        self.url = f"http://127.0.0.1:{self.PORT}"
        self.proc = start_server(
            [LLAMA_SERVER, "-m", model_path, "--host", "127.0.0.1", "--port", str(self.PORT),
             "-ngl", "99", "-c", "4096", "-np", "1", "-t", str(threads)],
            self.url, "llama.cpp", ("error", "failed"),
        )
        self.context = []  # falas anteriores (ingles), para manter o sentido entre frases
        self.cache = {}
        self("Hello.")     # aquece a GPU

    def __call__(self, text, final=True, **_):
        key = (text, tuple(self.context))
        if key not in self.cache:
            prompt = ""
            if self.context:
                prompt += "Falas anteriores (só contexto, não traduza):\n" + "\n".join(self.context) + "\n\n"
            prompt += "Traduza esta fala:\n" + text
            body = json.dumps({
                "messages": [{"role": "system", "content": self.INSTRUCTIONS},
                             {"role": "user", "content": prompt}],
                "temperature": 0, "max_tokens": 150, "cache_prompt": True,
            }).encode()
            req = urllib.request.Request(self.url + "/v1/chat/completions", data=body,
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=30) as r:
                out = json.loads(r.read().decode("utf-8"))["choices"][0]["message"]["content"]
            if len(self.cache) > 500:
                self.cache.clear()
            self.cache[key] = out.strip().strip('"“”').strip()
        result = self.cache[key]
        if final:
            self.context = (self.context + [text])[-3:]
        return result


# ---------------------------------------------------------------- reconhecimento

def kill_with_parent(proc):
    """Garante que o subprocesso morre junto com este programa (Job Object do Windows)."""
    k32 = ctypes.windll.kernel32
    k32.CreateJobObjectW.restype = ctypes.c_void_p
    k32.OpenProcess.restype = ctypes.c_void_p
    job = k32.CreateJobObjectW(None, None)
    info = (ctypes.c_byte * 144)()  # JOBOBJECT_EXTENDED_LIMIT_INFORMATION (x64)
    ctypes.c_uint32.from_buffer(info, 16).value = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    k32.SetInformationJobObject(ctypes.c_void_p(job), 9, info, ctypes.sizeof(info))
    handle = k32.OpenProcess(0x1F0FFF, False, proc.pid)
    k32.AssignProcessToJobObject(ctypes.c_void_p(job), ctypes.c_void_p(handle))
    kill_with_parent.jobs.append(job)  # o job precisa ficar aberto enquanto o programa roda
kill_with_parent.jobs = []


class FasterWhisperBackend:
    """Whisper no processador (faster-whisper)."""

    def __init__(self, model_name, threads):
        from faster_whisper import WhisperModel
        self.name = f"{model_name} (CPU)"
        self.model = WhisperModel(model_name, device="cpu", compute_type="int8", cpu_threads=threads)

    def __call__(self, seg, prompt):
        segments, _ = self.model.transcribe(
            seg, language="en", beam_size=3, vad_filter=False,
            condition_on_previous_text=False, without_timestamps=True, initial_prompt=prompt,
        )
        return [(s.text, s.no_speech_prob, s.avg_logprob, s.compression_ratio) for s in segments]


class WhisperCppBackend:
    """Whisper na placa de video (whisper.cpp + Vulkan), via whisper-server local."""

    PORT = 8178

    def __init__(self, model_path, threads):
        self.name = f"{os.path.basename(model_path)} (GPU)"
        self.url = f"http://127.0.0.1:{self.PORT}"
        self.proc = start_server(
            [WHISPER_SERVER, "-m", model_path, "--host", "127.0.0.1", "--port", str(self.PORT),
             "-t", str(threads), "-l", "en", "-fa"],
            self.url, "whisper.cpp", ("error", "ggml_vulkan: 0", "failed"),
        )
        # Aquece a GPU (a primeira inferencia compila shaders e e bem mais lenta).
        for _ in range(2):
            self(np.random.randn(2 * SR).astype(np.float32) * 0.01, None)

    def __call__(self, seg, prompt):
        wav = io.BytesIO()
        with wave.open(wav, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(SR)
            w.writeframes((np.clip(seg, -1, 1) * 32767).astype(np.int16).tobytes())
        fields = {"response_format": "verbose_json", "temperature": "0.0", "temperature_inc": "0.2",
                  "beam_size": "3", "no_timestamps": "true"}
        if prompt:
            fields["prompt"] = prompt
        boundary = "----legendas" + str(time.time_ns())
        body = io.BytesIO()
        for k, v in fields.items():
            body.write(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
        body.write(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="a.wav"\r\n'
                   f"Content-Type: audio/wav\r\n\r\n".encode())
        body.write(wav.getvalue())
        body.write(f"\r\n--{boundary}--\r\n".encode())
        req = urllib.request.Request(self.url + "/inference", data=body.getvalue(),
                                     headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read().decode("utf-8"))
        return [(s.get("text", ""), s.get("no_speech_prob", 0.0), s.get("avg_logprob", 0.0), None)
                for s in data.get("segments", [])]


class Recognizer:
    """Junta o audio em frases (cortando nas pausas) e transcreve cada uma."""

    PAUSE = 0.4        # segundos de silencio que fecham uma frase
    MAX_LEN = 7.0      # forca o corte se a pessoa falar sem pausa
    PAD = int(0.2 * SR)

    def __init__(self, backend):
        from faster_whisper.vad import VadOptions, get_speech_timestamps
        self.backend = backend
        self.vad = lambda a: get_speech_timestamps(a, VadOptions(min_silence_duration_ms=250, speech_pad_ms=100))
        self.buf = np.zeros(0, dtype=np.float32)
        self.prev_text = ""

    def feed(self, chunk):
        self.buf = np.concatenate([self.buf, chunk])

    def poll(self):
        """Devolve (frase_completa, frase_em_andamento); qualquer um pode ser None."""
        if len(self.buf) < SR // 2:
            return None, None
        speech = self.vad(self.buf)
        if not speech:
            if len(self.buf) > 3 * SR:
                self.buf = self.buf[-SR // 2:]
            return None, None
        start = max(0, speech[0]["start"] - self.PAD)
        last_end = speech[-1]["end"]
        if len(self.buf) - last_end >= self.PAUSE * SR:
            cut = min(len(self.buf), last_end + self.PAD)
        elif len(self.buf) - start >= self.MAX_LEN * SR:
            # Sem pausa longa: corta na ultima pausa curta, se houver.
            gaps = [s["end"] for s in speech[:-1] if s["end"] - start > 2 * SR]
            cut = gaps[-1] if gaps else len(self.buf)
        else:
            return None, self.buf[start:]
        seg = self.buf[start:cut]
        self.buf = self.buf[cut:]
        return seg, None

    def transcribe(self, seg, final=True):
        parts = []
        for text, no_speech, logprob, compression in self.backend(seg, self.prev_text[-200:] or None):
            if no_speech > 0.6 and logprob < -0.8:
                continue
            if compression is not None and compression > 2.4:  # texto repetido = alucinacao
                continue
            parts.append(text.strip())
        text = " ".join(parts).strip()
        if text.lower() in HALLUCINATIONS or not any(c.isalpha() for c in text):
            return ""
        # Audio que nao e ingles (ou musica) faz o Whisper repetir palavras: "foreign foreign foreign..."
        words = re.findall(r"[a-z']+", text.lower())
        if "foreign" in words or (len(words) >= 6 and len(set(words)) / len(words) < 0.35):
            return ""
        if final:
            self.prev_text = text
        return text


def worker(args, audio_q, subs_q, status):
    status("Carregando modelos...")
    translate = None
    llm_model = args.modelo_llm or next((p for p in LLM_MODELS if os.path.exists(p)), None)
    if args.tradutor == "llm" or (args.tradutor == "auto" and llm_model and os.path.exists(LLAMA_SERVER)):
        try:
            translate = LlmTranslator(llm_model, args.threads)
        except Exception as e:
            log(f"Nao deu para usar o tradutor na placa de video ({e}); usando o Argos.")
    if translate is None:
        translate = ArgosTranslator(args.threads)
    log(f"Tradutor: {translate.name}")
    use_gpu = args.motor == "gpu" or (args.motor == "auto" and os.path.exists(WHISPER_SERVER)
                                      and os.path.exists(args.modelo_gpu))
    backend = None
    if use_gpu:
        try:
            backend = WhisperCppBackend(args.modelo_gpu, args.threads)
        except Exception as e:
            log(f"Nao deu para usar a placa de video ({e}); usando o processador.")
    if backend is None:
        backend = FasterWhisperBackend(args.modelo, args.threads)
    rec = Recognizer(backend)
    log(f"Modelo de fala: {backend.name}. Pronto!")
    status(None)
    threading.Thread(target=capture_loop, args=(args.programa, audio_q, status), daemon=True).start()

    # Legenda parcial (montando enquanto a pessoa fala) so compensa se a transcricao for rapida.
    partial_on = args.parcial == "sim" or (args.parcial == "auto" and isinstance(backend, WhisperCppBackend))
    last_vad, last_partial_len, last_partial_en = 0.0, 0, ""
    while True:
        try:
            rec.feed(audio_q.get(timeout=0.1))
            while True:
                rec.feed(audio_q.get_nowait())
        except queue.Empty:
            pass
        if time.time() - last_vad < 0.15:
            continue
        last_vad = time.time()
        seg, ongoing = rec.poll()
        if seg is not None:
            t0 = time.time()
            last_partial_len, last_partial_en = 0, ""
            en = rec.transcribe(seg)
            if not en:
                subs_q.put(("cancel", "", ""))
                continue
            pt = translate(en)
            log(f"({len(seg) / SR:.1f}s de audio, {time.time() - t0:.2f}s p/ processar)\n   EN: {en}\n   PT: {pt}")
            subs_q.put(("final", pt, en))
        elif partial_on and ongoing is not None and len(ongoing) >= 0.6 * SR \
                and len(ongoing) - last_partial_len >= 0.3 * SR:
            last_partial_len = len(ongoing)
            en = rec.transcribe(ongoing, final=False)
            if en and en != last_partial_en:
                last_partial_en = en
                pt = translate(en, final=False)
                log(f"   ... {pt}")
                subs_q.put(("partial", pt, en))


# ---------------------------------------------------------------- legenda na tela

class Overlay:
    KEY = "#010101"  # cor que vira transparente

    def __init__(self, args, subs_q):
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
        self.args, self.q = args, subs_q
        self.lines = []          # frases completas: [(pt, en, hora)]
        self.partial = None      # frase sendo falada agora: (pt, en, hora)
        self.status_text = None

        self.root = tk.Tk()
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.attributes("-transparentcolor", self.KEY)
        self.root.configure(bg=self.KEY)

        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        self.w, self.h = int(sw * 0.8), int(sh * 0.3)
        x = (sw - self.w) // 2
        y = sh - self.h - int(sh * args.altura / 100)
        self.root.geometry(f"{self.w}x{self.h}+{x}+{y}")
        self.canvas = tk.Canvas(self.root, bg=self.KEY, highlightthickness=0, width=self.w, height=self.h)
        self.canvas.pack(fill="both", expand=True)

        self.font_pt = ("Segoe UI Semibold", args.fonte)
        self.font_en = ("Segoe UI", max(10, int(args.fonte * 0.6)))
        self.root.after(100, self._click_through)
        self.root.after(100, self._tick)

    def _click_through(self):
        # Deixa os cliques passarem pela legenda e esconde do Alt+Tab.
        hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id())
        GWL_EXSTYLE, WS_EX_LAYERED, WS_EX_TRANSPARENT, WS_EX_TOOLWINDOW = -20, 0x80000, 0x20, 0x80
        style = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        ctypes.windll.user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style | WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW)

    def set_status(self, text):
        self.status_text = text  # chamado de outras threads; o _tick redesenha

    def _text(self, x, y, text, font, color):
        """Texto com contorno preto, para ler em qualquer fundo."""
        ids = []
        for dx in (-2, 0, 2):
            for dy in (-2, 0, 2):
                if dx or dy:
                    ids.append(self.canvas.create_text(x + dx, y + dy, text=text, font=font, fill="black",
                                                       width=self.w - 40, anchor="s", justify="center"))
        ids.append(self.canvas.create_text(x, y, text=text, font=font, fill=color,
                                           width=self.w - 40, anchor="s", justify="center"))
        bbox = self.canvas.bbox(ids[-1])
        return bbox[1] if bbox else y

    def _tick(self):
        changed = False
        while not self.q.empty():
            kind, pt, en = self.q.get_nowait()
            if kind == "final":
                self.lines = (self.lines + [(pt, en, time.time())])[-2:]
                self.partial = None
            elif kind == "partial":
                self.partial = (pt, en, time.time())
            else:
                self.partial = None
            changed = True
        now = time.time()
        keep = [l for l in self.lines if now - l[2] < self.args.duracao + len(l[0]) * 0.03]
        if keep != self.lines:
            self.lines, changed = keep, True
        if self.partial and now - self.partial[2] > 3:
            self.partial, changed = None, True
        if changed or getattr(self, "_last_status", 0) != self.status_text:
            self._last_status = self.status_text
            self._draw()
        self.root.attributes("-topmost", True)
        self.root.after(150, self._tick)

    def _draw(self):
        self.canvas.delete("all")
        y = self.h - 10
        cx = self.w // 2
        if not self.lines and not self.partial and self.status_text:
            self._text(cx, y, self.status_text, self.font_en, "#ffd75e")
            return
        # No maximo 2 blocos: a frase anterior e a que esta sendo falada agora.
        shown = self.lines[-1:] + [self.partial] if self.partial else self.lines[-2:]
        for pt, en, _ in reversed(shown):
            if self.args.ingles:
                y = self._text(cx, y, en, self.font_en, "#c8c8c8") - 4
            y = self._text(cx, y, pt, self.font_pt, "white") - 12

    def run(self):
        self.root.mainloop()


def main():
    ap = argparse.ArgumentParser(description="Legendas ao vivo (ingles -> portugues)")
    ap.add_argument("--programa", default="chrome", help="nome do .exe a ouvir (padrao: chrome)")
    ap.add_argument("--motor", choices=["auto", "gpu", "cpu"], default="auto",
                    help="gpu = whisper.cpp na placa de video; cpu = faster-whisper; auto = gpu se instalado")
    ap.add_argument("--modelo-gpu", default=os.path.join(MODELS_DIR, "ggml-large-v3-turbo-q8_0.bin"),
                    help="arquivo de modelo do whisper.cpp")
    ap.add_argument("--modelo", default="small.en",
                    help="modelo do modo CPU: base.en (rapido), small.en (padrao), distil-medium.en, medium.en")
    ap.add_argument("--tradutor", choices=["auto", "llm", "argos"], default="auto",
                    help="llm = Gemma na placa de video, portugues do Brasil (padrao se instalado); "
                         "argos = so processador, portugues de Portugal")
    ap.add_argument("--modelo-llm", default=None, help="arquivo .gguf do tradutor (padrao: Gemma 3)")
    ap.add_argument("--parcial", choices=["auto", "sim", "nao"], default="auto",
                    help="mostra a frase se montando enquanto a pessoa fala (auto = so com GPU)")
    ap.add_argument("--ingles", action="store_true", help="mostra tambem o texto em ingles")
    ap.add_argument("--fonte", type=int, default=26, help="tamanho da fonte da legenda")
    ap.add_argument("--altura", type=float, default=8, help="distancia da borda de baixo, em %% da tela")
    ap.add_argument("--duracao", type=float, default=4, help="segundos que a legenda fica na tela")
    ap.add_argument("--threads", type=int, default=max(1, (os.cpu_count() or 4) // 2), help="threads de CPU")
    args = ap.parse_args()

    if not os.path.exists(CAPTURE_EXE):
        sys.exit("Falta capture\\ProcAudioCapture.exe - rode instalar.bat primeiro.")

    audio_q, subs_q = queue.Queue(), queue.Queue()
    overlay = Overlay(args, subs_q)
    threading.Thread(target=worker, args=(args, audio_q, subs_q, overlay.set_status), daemon=True).start()
    log("Legendas rodando. Feche esta janela (ou Ctrl+C) para sair.")
    try:
        overlay.run()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
