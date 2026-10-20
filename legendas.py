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
# llama-server: versao oficial baixada pelo modulo "Motor de traducao" ou a compilada localmente.
LLAMA_SERVER = next((p for p in (os.path.join(BASE, "bin", "llama", "llama-server.exe"),
                                 os.path.join(BASE, "bin", "llama-server.exe")) if os.path.exists(p)),
                    os.path.join(BASE, "bin", "llama", "llama-server.exe"))
LLM_MODELS = [os.path.join(MODELS_DIR, f) for f in
              ("gemma-3-12b-it-Q4_K_M.gguf", "gemma-3-4b-it-Q4_K_M.gguf", "gemma-3-1b-it-Q4_K_M.gguf")]
PARTIAL_LLM_MODEL = LLM_MODELS[1]  # Gemma 4B: rapido o bastante para as parciais
# Quantas vezes a legenda parcial e refeita: (fala minima, fala nova entre atualizacoes, verificacao)
CADENCES = {"economica": (0.8, 0.6, 0.1), "normal": (0.5, 0.25, 0.05), "rapida": (0.4, 0.15, 0.03)}

SR = 16000
CAPTURE_CHUNK = 0.05  # segundos de audio por bloco lido da captura (menor = menos espera)
PARTIAL_MIN = 0.5     # segundos de fala antes da primeira legenda parcial
PARTIAL_STEP = 0.25   # segundos de fala nova antes de atualizar a parcial
POLL_EVERY = 0.05     # intervalo entre verificacoes da deteccao de voz
FINAL_BEAM = 1        # beam search da transcricao final (1 = guloso/rapido, 3 = mais cuidadoso)

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
            data = proc.stdout.read(int(SR * CAPTURE_CHUNK) * 2)
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

    def __call__(self, text, final=True, on_update=None):
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

    def __init__(self, model_path, threads, port=None, context=None, gpu_layers=99):
        where = "GPU" if gpu_layers else "CPU"
        self.name = f"{os.path.basename(model_path)} ({where}, PT-BR)"
        self.url = f"http://127.0.0.1:{port or self.PORT}"
        self.proc = start_server(
            [LLAMA_SERVER, "-m", model_path, "--host", "127.0.0.1", "--port", str(port or self.PORT),
             "-ngl", str(gpu_layers), "-c", "4096", "-np", "1", "-t", str(threads)],
            self.url, "llama.cpp", ("error", "failed"),
        )
        # Falas anteriores (ingles), para manter o sentido entre frases. Pode ser compartilhada
        # entre o tradutor das parciais e o das finais.
        self.context = context if context is not None else []
        self.cache = {}
        self("Hello.", final=False)  # aquece a GPU

    def __call__(self, text, final=True, on_update=None):
        """Traduz. Com on_update, recebe o texto parcial a cada pedaco gerado (streaming);
        se on_update devolver False, a geracao e interrompida."""
        key = (text, tuple(self.context))
        if key not in self.cache:
            prompt = ""
            if self.context:
                prompt += "Falas anteriores (só contexto, não traduza):\n" + "\n".join(self.context) + "\n\n"
            prompt += "Traduza esta fala:\n" + text
            body = json.dumps({
                "messages": [{"role": "system", "content": self.INSTRUCTIONS},
                             {"role": "user", "content": prompt}],
                "temperature": 0, "max_tokens": 150, "cache_prompt": True, "stream": True,
            }).encode()
            req = urllib.request.Request(self.url + "/v1/chat/completions", data=body,
                                         headers={"Content-Type": "application/json"})
            out, aborted = "", False
            with urllib.request.urlopen(req, timeout=30) as r:
                for raw in r:
                    line = raw.decode("utf-8").strip()
                    if not line.startswith("data: ") or line == "data: [DONE]":
                        continue
                    delta = json.loads(line[6:])["choices"][0].get("delta", {}).get("content") or ""
                    if not delta:
                        continue
                    out += delta
                    if on_update and on_update(self._clean(out)) is False:
                        aborted = True
                        break
            if aborted:
                return self._clean(out)
            if len(self.cache) > 500:
                self.cache.clear()
            self.cache[key] = self._clean(out)
        result = self.cache[key]
        if final:
            self.context[:] = (self.context + [text])[-3:]
        return result

    @staticmethod
    def _clean(s):
        return s.strip().strip('"“”').strip()


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
        local = os.path.join(MODELS_DIR, f"faster-whisper-{model_name}")  # baixado pelos modulos
        self.model = WhisperModel(local if os.path.isdir(local) else model_name, device="cpu",
                                  compute_type="int8", cpu_threads=threads)

    def __call__(self, seg, prompt, timestamps=False, beam=3):
        # Sempre com marcas de tempo: sem elas os modelos pequenos (base/small) costumam
        # parar na primeira frase e descartar o resto do trecho.
        segments, _ = self.model.transcribe(
            seg, language="en", beam_size=beam, vad_filter=False,
            condition_on_previous_text=False, without_timestamps=False, initial_prompt=prompt,
        )
        return [{"text": s.text, "no_speech": s.no_speech_prob, "logprob": s.avg_logprob,
                 "compression": s.compression_ratio, "start": s.start, "end": s.end} for s in segments]


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

    def __call__(self, seg, prompt, timestamps=False, beam=3):
        wav = io.BytesIO()
        with wave.open(wav, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(SR)
            w.writeframes((np.clip(seg, -1, 1) * 32767).astype(np.int16).tobytes())
        fields = {"response_format": "verbose_json", "temperature": "0.0", "temperature_inc": "0.2",
                  "no_timestamps": "false" if timestamps else "true"}
        if beam > 1:
            fields["beam_size"] = str(beam)
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
        return [{"text": s.get("text", ""), "no_speech": s.get("no_speech_prob", 0.0),
                 "logprob": s.get("avg_logprob", 0.0), "compression": None,
                 "start": s.get("start", 0.0), "end": s.get("end", 0.0)}
                for s in data.get("segments", [])]


class Recognizer:
    """Junta o audio em frases (cortando nas pausas) e transcreve cada uma."""

    PAUSE = 0.35       # segundos de silencio que fecham uma frase
    MAX_LEN = 7.0      # forca o corte se a pessoa falar sem pausa
    PAD = int(0.2 * SR)

    def __init__(self, backend):
        from faster_whisper.vad import VadOptions, get_speech_timestamps
        self.backend = backend
        self.vad = lambda a: get_speech_timestamps(a, VadOptions(min_silence_duration_ms=250, speech_pad_ms=100))
        self.buf = np.zeros(0, dtype=np.float32)
        self.prev_text = ""
        self.ongoing_start = 0

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
            self.ongoing_start = start
            return None, self.buf[start:]
        seg = self.buf[start:cut]
        self.buf = self.buf[cut:]
        return seg, None

    def commit(self, n):
        """Descarta o audio que ja virou legenda final (n amostras a partir do inicio da fala atual)."""
        self.buf = self.buf[self.ongoing_start + n:]

    @staticmethod
    def _valid(text):
        if text.lower() in HALLUCINATIONS or not any(c.isalpha() for c in text):
            return False
        # Audio que nao e ingles (ou musica) faz o Whisper repetir palavras: "foreign foreign foreign..."
        words = re.findall(r"[a-z']+", text.lower())
        return not ("foreign" in words or (len(words) >= 6 and len(set(words)) / len(words) < 0.35))

    def _segments(self, seg, timestamps, beam):
        out = []
        for s in self.backend(seg, self.prev_text[-200:] or None, timestamps=timestamps, beam=beam):
            if s["no_speech"] > 0.6 and s["logprob"] < -0.8:
                continue
            if s["compression"] is not None and s["compression"] > 2.4:  # texto repetido = alucinacao
                continue
            if self._valid(s["text"].strip()):
                out.append(s)
        return out

    def transcribe(self, seg):
        """Transcricao de uma frase completa (gulosa: mesmo texto que o beam search nos testes,
        mais rapida e com mais chance de coincidir com a ultima parcial)."""
        text = " ".join(s["text"].strip() for s in self._segments(seg, False, FINAL_BEAM)).strip()
        if not self._valid(text):
            return ""
        self.prev_text = text
        return text

    def transcribe_partial(self, seg):
        """Transcricao rapida (gulosa) da fala em andamento, com marcas de tempo por trecho."""
        return self._segments(seg, True, 1)


def load_translators(args):
    """Tradutor das frases finais e o (mais rapido) das parciais."""
    llm_model = args.modelo_llm or next((p for p in LLM_MODELS if os.path.exists(p)), None)
    gpu_layers = getattr(args, "llm_camadas_gpu", 99)
    final_tr = None
    if args.tradutor == "llm" or (args.tradutor == "auto" and llm_model and os.path.exists(LLAMA_SERVER)):
        try:
            final_tr = LlmTranslator(llm_model, args.threads, gpu_layers=gpu_layers)
        except Exception as e:
            log(f"Nao deu para usar o tradutor Gemma ({e}); usando o Argos.")
    if final_tr is None:
        tr = ArgosTranslator(args.threads)
        return tr, tr

    partial_model = getattr(args, "modelo_llm_parcial", None) or PARTIAL_LLM_MODEL
    if partial_model != "nenhum" and os.path.exists(partial_model) \
            and os.path.abspath(partial_model) != os.path.abspath(llm_model):
        try:
            return final_tr, LlmTranslator(partial_model, args.threads, port=LlmTranslator.PORT + 1,
                                           context=final_tr.context, gpu_layers=gpu_layers)
        except Exception as e:
            log(f"Nao deu para abrir o tradutor rapido das parciais ({e}); usando o principal.")
    return final_tr, final_tr


def _norm(s):
    return " ".join(re.findall(r"[a-z0-9']+", s.lower()))


def worker(args, audio_q, subs_q, status):
    """Tres linhas de trabalho em paralelo, para nenhuma etapa esperar a outra:
    reconhecimento (esta thread) -> traducao das parciais (modelo rapido) e das finais (modelo melhor)."""
    status("Carregando modelos...")
    final_tr, partial_tr = load_translators(args)
    log(f"Tradutor: {final_tr.name}" + (f" + parciais com {partial_tr.name}" if partial_tr is not final_tr else ""))
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
    finalized = {"seq": -1}     # maior frase ja enviada para a traducao final
    shown_partial = {}          # seq -> ultima traducao parcial mostrada
    final_q = queue.Queue()
    pending = {"job": None}     # so a parcial mais recente importa; as velhas sao descartadas
    pending_ev = threading.Event()

    def final_loop():
        while True:
            seq, en, seg_len, t0 = final_q.get()
            pre_pt, pre_en, pre_done = shown_partial.pop(seq, ("", "", False))
            if pre_done and partial_tr is not final_tr and _norm(pre_en) == _norm(en):
                # A parcial ja traduziu exatamente esta frase: fecha a legenda na hora e
                # deixa o modelo melhor revisar em segundo plano.
                log(f"({seg_len / SR:.1f}s de audio, {time.time() - t0:.2f}s p/ processar)\n   EN: {en}\n   PT: {pre_pt}")
                subs_q.put(("final", seq, pre_pt, en))
                pt = final_tr(en, final=True)
                if _norm(pt) != _norm(pre_pt):
                    log(f"   PT revisado: {pt}")
                    subs_q.put(("revise", seq, pt, en))
                continue

            def update(pt):  # mostra a final sendo escrita, sem "encolher" a parcial ja exibida
                if len(pt) >= len(pre_pt):
                    subs_q.put(("partial", seq, pt, en))
            pt = final_tr(en, final=True, on_update=update)
            log(f"({seg_len / SR:.1f}s de audio, {time.time() - t0:.2f}s p/ processar)\n   EN: {en}\n   PT: {pt}")
            subs_q.put(("final", seq, pt, en))

    def partial_loop():
        while True:
            pending_ev.wait()
            pending_ev.clear()
            job, pending["job"] = pending["job"], None
            if job is None or job[0] <= finalized["seq"]:
                continue
            seq, en = job

            def update(pt):
                if seq <= finalized["seq"] or pending["job"] is not None:
                    return False  # ja ficou velha: para de gerar
                shown_partial[seq] = (pt, en, False)
                subs_q.put(("partial", seq, pt, en))
            pt = partial_tr(en, final=False, on_update=update)
            if pt and seq > finalized["seq"] and pending["job"] is None:
                shown_partial[seq] = (pt, en, True)
                subs_q.put(("partial", seq, pt, en))
                log(f"   ... {pt}")

    threading.Thread(target=final_loop, daemon=True).start()
    if partial_on:
        threading.Thread(target=partial_loop, daemon=True).start()

    def send_final(en, seg_len, t0):
        nonlocal seq
        finalized["seq"] = seq
        if en:
            final_q.put((seq, en, seg_len, t0))
        else:
            subs_q.put(("cancel", seq, "", ""))
        seq += 1

    seq = 0
    last_poll, last_partial_len, prev_candidate = 0.0, 0, None
    while True:
        try:
            rec.feed(audio_q.get(timeout=0.02))
            while True:
                rec.feed(audio_q.get_nowait())
        except queue.Empty:
            pass
        if time.time() - last_poll < POLL_EVERY:
            continue
        last_poll = time.time()
        seg, ongoing = rec.poll()
        if seg is not None:
            t0 = time.time()
            send_final(rec.transcribe(seg), len(seg), t0)
            last_partial_len, prev_candidate = 0, None
        elif partial_on and ongoing is not None and len(ongoing) >= PARTIAL_MIN * SR \
                and len(ongoing) - last_partial_len >= PARTIAL_STEP * SR:
            t0 = time.time()
            last_partial_len = len(ongoing)
            segs = rec.transcribe_partial(ongoing)
            if not segs:
                continue
            # Frases completas (terminam em . ? !) que sairam iguais em duas transcricoes seguidas
            # ja viram legenda final, sem esperar a pessoa parar de falar.
            k = max((i + 1 for i in range(len(segs) - 1) if re.search(r"[.!?]$", segs[i]["text"].strip())), default=0)
            candidate = " ".join(s["text"].strip() for s in segs[:k])
            cut = int(segs[k - 1]["end"] * SR) if k else 0
            if k and prev_candidate is not None and _norm(candidate) == _norm(prev_candidate) \
                    and 0.5 * SR <= cut <= len(ongoing) - 0.2 * SR:
                rec.commit(cut)
                rec.prev_text = candidate
                send_final(candidate, cut, t0)
                last_partial_len, prev_candidate = 0, None
                segs = segs[k:]
            else:
                prev_candidate = candidate if k else None
            en = " ".join(s["text"].strip() for s in segs).strip()
            if en:
                pending["job"] = (seq, en)
                pending_ev.set()


# ---------------------------------------------------------------- legenda na tela

class Overlay:
    """Legenda por cima de todas as janelas (PySide6): transparente, deixa o clique passar
    e texto com contorno, para ler em qualquer fundo."""

    def __init__(self, args, subs_q):
        from PySide6.QtCore import Qt, QTimer
        from PySide6.QtGui import QColor, QFont, QFontMetricsF, QPainter, QPainterPath, QPen
        from PySide6.QtWidgets import QApplication, QWidget

        self.args, self.q = args, subs_q
        self.lines = []          # frases completas: [(pt, en, hora, seq)]
        self.partials = {}       # frases sendo faladas/traduzidas agora: {seq: (pt, en, hora)}
        self.done_seq = -1       # ultima frase ja finalizada
        self.status_text = None
        self._last_status = 0
        self.app = QApplication.instance() or QApplication(sys.argv)

        overlay = self
        font_pt = QFont("Segoe UI Semibold", args.fonte)
        font_en = QFont("Segoe UI", max(10, int(args.fonte * 0.6)))

        class Widget(QWidget):
            def paintEvent(self, _):
                p = QPainter(self)
                p.setRenderHint(QPainter.Antialiasing)
                y = self.height() - 10.0
                if not overlay.lines and not overlay.partials and overlay.status_text:
                    self._block(p, overlay.status_text, font_en, QColor("#ffd75e"), y)
                    return
                # No maximo 2 blocos: as frases mais recentes (finais e as que ainda estao sendo faladas).
                shown = (overlay.lines + [overlay.partials[s] for s in sorted(overlay.partials)])[-2:]
                for pt, en, *_ in reversed(shown):
                    if overlay.args.ingles and en:
                        y = self._block(p, en, font_en, QColor("#c8c8c8"), y) - 4
                    y = self._block(p, pt, font_pt, QColor("white"), y) - 12

            def _block(self, p, text, font, color, bottom):
                """Desenha o texto (quebrando linhas) com a base em 'bottom'; devolve o topo."""
                fm = QFontMetricsF(font)
                maxw = self.width() - 60
                lines, cur = [], ""
                for word in text.split():
                    test = f"{cur} {word}".strip()
                    if cur and fm.horizontalAdvance(test) > maxw:
                        lines.append(cur)
                        cur = word
                    else:
                        cur = test
                if cur:
                    lines.append(cur)
                path = QPainterPath()
                y = bottom
                for line in reversed(lines):
                    x = (self.width() - fm.horizontalAdvance(line)) / 2
                    path.addText(x, y - fm.descent(), font, line)
                    y -= fm.height()
                outline = max(3.0, font.pointSizeF() / 5)
                p.strokePath(path, QPen(QColor(0, 0, 0, 230), outline, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
                p.fillPath(path, color)
                return y

        self.w = Widget()
        self.w.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
                              | Qt.WindowTransparentForInput | Qt.WindowDoesNotAcceptFocus)
        self.w.setAttribute(Qt.WA_TranslucentBackground)
        self.w.setAttribute(Qt.WA_ShowWithoutActivating)
        geo = self.app.primaryScreen().geometry()
        width, height = int(geo.width() * 0.8), int(geo.height() * 0.3)
        self.w.setGeometry(geo.x() + (geo.width() - width) // 2,
                           geo.y() + geo.height() - height - int(geo.height() * args.altura / 100), width, height)
        self.w.show()
        self._last_raise = 0.0
        self.timer = QTimer(timeout=self._tick, interval=40)
        self.timer.start()

    def set_status(self, text):
        self.status_text = text  # chamado de outras threads; o _tick redesenha

    def _tick(self):
        changed = False
        while not self.q.empty():
            kind, seq, pt, en = self.q.get_nowait()
            if kind == "partial":
                if seq > self.done_seq:  # ignora parcial atrasada de frase ja finalizada
                    self.partials[seq] = (pt, en, time.time())
            elif kind == "revise":  # o modelo melhor corrigiu uma frase ja na tela
                self.lines = [(pt, en, l[2], l[3]) if l[3] == seq else l for l in self.lines]
            else:
                self.partials.pop(seq, None)
                self.done_seq = max(self.done_seq, seq)
                if kind == "final":
                    self.lines = (self.lines + [(pt, en, time.time(), seq)])[-2:]
            changed = True
        now = time.time()
        keep = [l for l in self.lines if now - l[2] < self.args.duracao + len(l[0]) * 0.03]
        if keep != self.lines:
            self.lines, changed = keep, True
        for seq in [s for s, p in self.partials.items() if now - p[2] > 3]:
            del self.partials[seq]
            changed = True
        if changed or self._last_status != self.status_text:
            self._last_status = self.status_text
            self.w.update()
        if now - self._last_raise > 2:  # outras janelas "sempre no topo" podem passar na frente
            self.w.raise_()
            self._last_raise = now

    def run(self):
        self.app.exec()


def build_parser():
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
    ap.add_argument("--modelo-llm-parcial", default=None,
                    help="arquivo .gguf mais rapido para as frases parciais (padrao: Gemma 3 4B; 'nenhum' desliga)")
    ap.add_argument("--parcial", choices=["auto", "sim", "nao"], default="auto",
                    help="mostra a frase se montando enquanto a pessoa fala (auto = so com GPU)")
    ap.add_argument("--ingles", action="store_true", help="mostra tambem o texto em ingles")
    ap.add_argument("--fonte", type=int, default=26, help="tamanho da fonte da legenda")
    ap.add_argument("--altura", type=float, default=8, help="distancia da borda de baixo, em %% da tela")
    ap.add_argument("--duracao", type=float, default=4, help="segundos que a legenda fica na tela")
    ap.add_argument("--threads", type=int, default=max(1, (os.cpu_count() or 4) // 2), help="threads de CPU")
    ap.add_argument("--cadencia", choices=list(CADENCES), default="normal",
                    help="frequencia das legendas parciais: economica (PC fraco), normal, rapida (placa forte)")
    ap.add_argument("--beam-final", type=int, default=1,
                    help="cuidado da transcricao final: 1 = rapido (padrao), 3 = mais preciso")
    ap.add_argument("--llm-camadas-gpu", type=int, default=99,
                    help="camadas do tradutor Gemma na placa de video (0 = tudo no processador)")
    return ap


def apply_cadence(name):
    global PARTIAL_MIN, PARTIAL_STEP, POLL_EVERY
    PARTIAL_MIN, PARTIAL_STEP, POLL_EVERY = CADENCES[name]


def main():
    args = build_parser().parse_args()
    apply_cadence(args.cadencia)
    global FINAL_BEAM
    FINAL_BEAM = args.beam_final

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
