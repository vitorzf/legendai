"""
Legendai - painel para controlar as legendas ao vivo (legendas.py).
Escolha o programa, ajuste a legenda e inicie/pare com um clique.
"""
import ctypes
import json
import os
import re
import shutil
import sys
import time

import psutil
from PySide6.QtCore import QProcess, QProcessEnvironment, QRectF, QSize, Qt, QTimer
from PySide6.QtGui import (QColor, QFont, QIcon, QLinearGradient, QPainter, QPainterPath, QPen,
                           QPixmap)
from PySide6.QtWidgets import (QApplication, QFileIconProvider, QFrame, QHBoxLayout, QLabel,
                               QSystemTrayIcon, QVBoxLayout, QWidget)
from PySide6.QtCore import QFileInfo
from qfluentwidgets import (Action, BodyLabel, CaptionLabel, ComboBox, ExpandLayout, FluentIcon as FIF,
                            FluentWindow, IndeterminateProgressRing, InfoBar, InfoBarPosition,
                            NavigationItemPosition, PlainTextEdit, PrimaryPushButton, ProgressBar, PushButton,
                            ScrollArea, SegmentedWidget, SettingCard, SettingCardGroup, SimpleCardWidget, Slider,
                            StrongBodyLabel, SubtitleLabel, SwitchButton, SystemTrayMenu, Theme,
                            TitleLabel, ToolButton, isDarkTheme, setTheme, setThemeColor)

import desempenho as D
import modulos

BASE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE, "config.json")
ICON_SVG = os.path.join(BASE, "assets", "icon.svg")
ICON_ICO = os.path.join(BASE, "assets", "icon.ico")
MODELS = os.path.join(BASE, "models")
ACCENT = "#7B61FF"
ACCENT_2 = "#06B6D4"

CPU_COUNT = os.cpu_count() or 4
DEFAULTS = {
    "programa": "chrome", "fonte": 26, "altura": 8, "duracao": 4, "ingles": False, "tema": "auto",
    "modo": "auto", "nivel": "gpu_equilibrado", "teste": None, "beam_final": 1, "parcial": True, "tradutor": "gemma4", "parcial_modelo": "nenhum",
    "tradutor_gpu": True, "motor": "auto", "modelo_cpu": "small.en", "cadencia": "normal",
    "threads": max(1, CPU_COUNT // 2),
}

TRANSLATORS = [  # (chave, nome, descricao, arquivo do modelo)
    ("gemma12", "Gemma 3 12B", "Melhor qualidade · exige placa forte", "gemma-3-12b-it-Q4_K_M.gguf"),
    ("gemma4", "Gemma 3 4B", "Rápido · placa de vídeo", "gemma-3-4b-it-Q4_K_M.gguf"),
    ("gemma1", "Gemma 3 1B", "Ultrarrápido · roda até sem placa de vídeo", "gemma-3-1b-it-Q4_K_M.gguf"),
    ("argos", "Argos", "Levíssimo · português de Portugal", None),
]
PARTIAL_TRANSLATORS = [("nenhum", "O mesmo do principal"), ("gemma4", "Gemma 3 4B"), ("gemma1", "Gemma 3 1B")]
ENGINES = [("auto", "Automático"), ("gpu", "Placa de vídeo (whisper.cpp)"), ("cpu", "Processador (faster-whisper)")]
CPU_MODELS = [("base.en", "Base — mais leve"), ("small.en", "Small — equilíbrio"),
              ("distil-medium.en", "Distil Medium — mais preciso, mais pesado")]
CADENCES = [("economica", "Econômica — atualiza menos, poupa o PC"), ("normal", "Normal"),
            ("rapida", "Rápida — mais responsiva, exige placa forte")]

# Processos com janela que nao faz sentido legendar.
IGNORE = {"explorer.exe", "textinputhost.exe", "applicationframehost.exe", "systemsettings.exe",
          "shellexperiencehost.exe", "searchhost.exe", "startmenuexperiencehost.exe", "lockapp.exe",
          "python.exe", "pythonw.exe", "claude.exe", "nvidia overlay.exe", "widgets.exe",
          "msedgewebview2.exe", "steamwebhelper.exe", "pritunl.exe", "rtkuwp.exe", "gamebar.exe"}


# ---------------------------------------------------------------- utilidades

def load_config():
    cfg = dict(DEFAULTS)
    vram = D.gpu_info()[1]
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            saved = json.load(f)
        old = saved.pop("intensidade", None)  # versao anterior: niveis por nome
        if "modo" not in saved:
            saved["modo"] = "avancado" if old in (None, "personalizado") else "barra"
            saved["nivel"] = {"minimo": "cpu_turbo", "leve": "cpu_leve", "equilibrado": "gpu_equilibrado",
                              "maximo": "gpu_preciso"}.get(old, D.heuristic_choice(vram)["key"])
        cfg.update(saved)
    except Exception:
        # Primeira vez: modo automatico, comecando por uma estimativa pela memoria de video
        # (o teste do PC refina a escolha logo em seguida).
        cfg["nivel"] = D.heuristic_choice(vram)["key"]
        cfg.update(D.level(cfg["nivel"])["cfg"])
    return cfg


def save_config(cfg):
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
    except Exception:
        pass


def kill_with_parent(pid):
    """Se o painel fechar (ou travar), as legendas fecham junto."""
    k32 = ctypes.windll.kernel32
    k32.CreateJobObjectW.restype = ctypes.c_void_p
    k32.OpenProcess.restype = ctypes.c_void_p
    job = k32.CreateJobObjectW(None, None)
    info = (ctypes.c_byte * 144)()
    ctypes.c_uint32.from_buffer(info, 16).value = 0x2000  # KILL_ON_JOB_CLOSE
    k32.SetInformationJobObject(ctypes.c_void_p(job), 9, info, ctypes.sizeof(info))
    handle = k32.OpenProcess(0x1F0FFF, False, pid)
    k32.AssignProcessToJobObject(ctypes.c_void_p(job), ctypes.c_void_p(handle))
    kill_with_parent.jobs.append(job)
kill_with_parent.jobs = []


def friendly_name(exe_path, fallback):
    try:
        import win32api
        lang, cp = win32api.GetFileVersionInfo(exe_path, r"\VarFileInfo\Translation")[0]
        desc = win32api.GetFileVersionInfo(exe_path, rf"\StringFileInfo\{lang:04x}{cp:04x}\FileDescription")
        if desc and desc.strip():
            return re.sub(r"\.Root$", "", desc.strip())
    except Exception:
        pass
    return fallback[:-4].capitalize() if fallback.lower().endswith(".exe") else fallback


def playing_audio():
    """Nomes dos .exe que estao tocando som agora (sessoes de audio ativas)."""
    names = set()
    try:
        from pycaw.pycaw import AudioUtilities
        for s in AudioUtilities.GetAllSessions():
            try:
                if s.Process and s.State == 1:  # AudioSessionStateActive
                    names.add(s.Process.name().lower())
            except Exception:
                pass
    except Exception:
        pass
    return names


def list_programs():
    """Programas com janela aberta (agrupados por .exe), os que tocam som primeiro."""
    user32 = ctypes.windll.user32
    titles = {}
    proc_t = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def cb(hwnd, _):
        if user32.IsWindowVisible(hwnd) and user32.GetWindowTextLengthW(hwnd) > 0 and not user32.GetWindow(hwnd, 4):
            pid = ctypes.c_ulong()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            buf = ctypes.create_unicode_buffer(256)
            user32.GetWindowTextW(hwnd, buf, 256)
            titles.setdefault(pid.value, buf.value)
        return True
    user32.EnumWindows(proc_t(cb), 0)

    playing = playing_audio()
    progs = {}
    for pid, title in titles.items():
        try:
            p = psutil.Process(pid)
            name, exe = p.name(), p.exe()
        except psutil.Error:
            continue
        key = name.lower()
        if key in IGNORE or pid == os.getpid():
            continue
        progs.setdefault(key, {"exe": key[:-4], "path": exe, "title": title,
                               "name": friendly_name(exe, name), "playing": key in playing})
    return sorted(progs.values(), key=lambda p: (not p["playing"], p["name"].lower()))


def app_icon():
    return QIcon(ICON_SVG)


def ensure_ico():
    if not os.path.exists(ICON_ICO):
        QIcon(ICON_SVG).pixmap(256, 256).save(ICON_ICO, "ICO")


# ---------------------------------------------------------------- widgets proprios

class HeroBanner(QWidget):
    """Faixa com gradiente da marca no topo da tela inicial."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(168)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect())
        path = QPainterPath()
        path.addRoundedRect(r, 12, 12)
        g = QLinearGradient(r.topLeft(), r.bottomRight())
        g.setColorAt(0, QColor("#6D4AFF"))
        g.setColorAt(0.55, QColor("#7B61FF"))
        g.setColorAt(1, QColor("#06B6D4"))
        p.fillPath(path, g)

        # Brilhos decorativos
        p.setClipPath(path)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(255, 255, 255, 28))
        p.drawEllipse(QRectF(r.width() - 230, -90, 300, 300))
        p.setBrush(QColor(255, 255, 255, 18))
        p.drawEllipse(QRectF(r.width() - 120, 60, 220, 220))

        # Mini legenda ilustrativa
        p.setBrush(QColor(0, 0, 0, 70))
        box = QRectF(r.width() - 300, r.height() - 64, 250, 34)
        if box.left() > 360:
            p.drawRoundedRect(box, 8, 8)
            f = QFont("Segoe UI Variable Display", 11)
            f.setWeight(QFont.DemiBold)
            p.setFont(f)
            p.setPen(QColor("white"))
            p.drawText(box, Qt.AlignCenter, "Pra onde a gente vai?")

        p.setClipping(False)
        QIcon(ICON_SVG).paint(p, 28, 34, 64, 64)
        p.setPen(QColor("white"))
        f = QFont("Segoe UI Variable Display", 26)
        f.setWeight(QFont.Bold)
        p.setFont(f)
        p.drawText(QRectF(108, 30, 600, 44), Qt.AlignLeft | Qt.AlignVCenter, "Legendaí")
        f = QFont("Segoe UI Variable Text", 11)
        p.setFont(f)
        p.setPen(QColor(255, 255, 255, 225))
        p.drawText(QRectF(110, 74, max(200, r.width() - 450), 70), Qt.AlignLeft | Qt.TextWordWrap,
                   "Legendas ao vivo do inglês para o português do Brasil.\n"
                   "Tudo no seu PC — sem internet, sem assinatura.")


class StatusDot(QWidget):
    COLORS = {"idle": "#8A8A8A", "loading": "#F7B731", "running": "#22C55E", "waiting": "#F7B731", "error": "#EF4444"}

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(14, 14)
        self.state = "idle"
        self.phase = 0.0
        self.timer = QTimer(self, timeout=self._tick, interval=50)

    def setState(self, state):
        self.state = state
        self.timer.start() if state in ("running", "loading", "waiting") else self.timer.stop()
        self.update()

    def _tick(self):
        self.phase = (self.phase + 0.05) % 1.0
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        c = QColor(self.COLORS[self.state])
        if self.timer.isActive():
            halo = QColor(c)
            halo.setAlphaF(0.45 * (1 - self.phase))
            p.setPen(Qt.NoPen)
            p.setBrush(halo)
            s = 6 + 8 * self.phase
            p.drawEllipse(QRectF(7 - s / 2, 7 - s / 2, s, s))
        p.setPen(Qt.NoPen)
        p.setBrush(c)
        p.drawEllipse(QRectF(3, 3, 8, 8))


class SubtitlePreview(QWidget):
    """Mostra como a legenda vai ficar, sobre uma 'cena' de video."""

    def __init__(self, cfg, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.setMinimumHeight(220)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        path = QPainterPath()
        path.addRoundedRect(r, 10, 10)
        g = QLinearGradient(r.topLeft(), r.bottomRight())
        g.setColorAt(0, QColor("#1E293B"))
        g.setColorAt(0.6, QColor("#3B2C6B"))
        g.setColorAt(1, QColor("#0E4E5C"))
        p.fillPath(path, g)
        p.setClipPath(path)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(255, 255, 255, 14))
        p.drawEllipse(QRectF(r.width() * 0.55, -r.height() * 0.4, r.height() * 1.3, r.height() * 1.3))

        scale = r.height() / 1080 * 1.9  # aproxima a proporcao de uma tela Full HD
        bottom = r.bottom() - r.height() * self.cfg["altura"] / 100 - 6
        lines = [("Pra onde a gente vai? Você vai ver.", int(self.cfg["fonte"] * scale) + 4, True)]
        if self.cfg["ingles"]:
            lines.append(("Where are we going? You'll see.", int(self.cfg["fonte"] * 0.6 * scale) + 3, False))
        for text, size, bold in reversed(lines):
            f = QFont("Segoe UI Semibold" if bold else "Segoe UI", max(7, size))
            p.setFont(f)
            h = p.fontMetrics().height()
            box = QRectF(r.left(), bottom - h, r.width(), h)
            p.setPen(QColor("black"))
            for dx in (-1.5, 0, 1.5):
                for dy in (-1.5, 0, 1.5):
                    if dx or dy:
                        p.drawText(box.translated(dx, dy), Qt.AlignHCenter | Qt.AlignVCenter, text)
            p.setPen(QColor("white") if bold else QColor("#C8C8C8"))
            p.drawText(box, Qt.AlignHCenter | Qt.AlignVCenter, text)
            bottom -= h + 2


def card_title(icon, text, parent):
    row = QHBoxLayout()
    row.setSpacing(10)
    ic = QLabel(parent)
    ic.setPixmap(icon.icon().pixmap(18, 18))
    row.addWidget(ic)
    row.addWidget(StrongBodyLabel(text, parent))
    row.addStretch(1)
    return row


class Page(ScrollArea):
    def __init__(self, name, parent=None):
        super().__init__(parent)
        self.setObjectName(name)
        self.view = QWidget(self)
        self.view.setObjectName("view")
        self.setWidget(self.view)
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setStyleSheet("Page, #view { background: transparent; border: none; }")


# ---------------------------------------------------------------- telas

class HomePage(Page):
    def __init__(self, win):
        super().__init__("home", win)
        self.win = win
        lay = QVBoxLayout(self.view)
        lay.setContentsMargins(36, 28, 36, 36)
        lay.setSpacing(16)
        lay.addWidget(HeroBanner(self))

        # --- Falta instalar modulos
        self.setup_card = SimpleCardWidget(self)
        sc = QHBoxLayout(self.setup_card)
        sc.setContentsMargins(20, 16, 20, 16)
        sc.setSpacing(12)
        ic = QLabel(self.setup_card)
        ic.setPixmap(FIF.CLOUD_DOWNLOAD.icon().pixmap(26, 26))
        sc.addWidget(ic)
        st = QVBoxLayout()
        st.setSpacing(2)
        st.addWidget(StrongBodyLabel("Falta pouco para começar", self.setup_card))
        self.setup_text = CaptionLabel("", self.setup_card)
        self.setup_text.setWordWrap(True)
        st.addWidget(self.setup_text)
        sc.addLayout(st, 1)
        choose = PushButton(self.setup_card)
        choose.setText("Escolher módulos")
        choose.clicked.connect(lambda: win.switchTo(win.modules))
        sc.addWidget(choose)
        self.setup_btn = PrimaryPushButton(self.setup_card)
        self.setup_btn.setIcon(FIF.DOWNLOAD)
        self.setup_btn.clicked.connect(lambda: (win.switchTo(win.modules),
                                                win.modules.install(win.modules.missing_recommended())))
        sc.addWidget(self.setup_btn)
        self.setup_card.hide()
        lay.addWidget(self.setup_card)

        # --- Programa
        prog = SimpleCardWidget(self)
        pl = QVBoxLayout(prog)
        pl.setContentsMargins(20, 16, 20, 18)
        pl.setSpacing(10)
        pl.addLayout(card_title(FIF.APPLICATION, "Programa a legendar", prog))
        row = QHBoxLayout()
        row.setSpacing(8)
        self.combo = ComboBox(prog)
        self.combo.setMinimumWidth(360)
        self.combo.currentIndexChanged.connect(self._on_program)
        self.refresh_btn = ToolButton(prog)
        self.refresh_btn.setIcon(FIF.SYNC)
        self.refresh_btn.setToolTip("Atualizar lista de programas")
        self.refresh_btn.clicked.connect(self.refresh_programs)
        row.addWidget(self.combo, 1)
        row.addWidget(self.refresh_btn)
        pl.addLayout(row)
        self.audio_hint = CaptionLabel("", prog)
        pl.addWidget(self.audio_hint)
        lay.addWidget(prog)

        # --- Controle
        ctl = SimpleCardWidget(self)
        cl = QHBoxLayout(ctl)
        cl.setContentsMargins(20, 18, 20, 18)
        cl.setSpacing(14)
        self.dot = StatusDot(ctl)
        cl.addWidget(self.dot, 0, Qt.AlignVCenter)
        txt = QVBoxLayout()
        txt.setSpacing(2)
        self.status_title = SubtitleLabel("Parado", ctl)
        self.status_detail = CaptionLabel("Escolha o programa e clique em Iniciar.", ctl)
        txt.addWidget(self.status_title)
        txt.addWidget(self.status_detail)
        cl.addLayout(txt, 1)
        self.ring = IndeterminateProgressRing(ctl)
        self.ring.setFixedSize(26, 26)
        self.ring.setStrokeWidth(3)
        self.ring.hide()
        cl.addWidget(self.ring)
        self.start_btn = PrimaryPushButton(ctl)
        self.start_btn.setText("Iniciar legendas")
        self.start_btn.setIcon(FIF.PLAY)
        self.start_btn.setFixedHeight(40)
        self.start_btn.setMinimumWidth(190)
        self.start_btn.clicked.connect(win.toggle)
        cl.addWidget(self.start_btn)
        lay.addWidget(ctl)

        # --- Ao vivo
        live = SimpleCardWidget(self)
        ll = QVBoxLayout(live)
        ll.setContentsMargins(20, 16, 20, 20)
        ll.setSpacing(8)
        head = card_title(FIF.CHAT, "Ao vivo", live)
        self.latency = CaptionLabel("", live)
        head.addWidget(self.latency)
        ll.addLayout(head)
        self.live_en = CaptionLabel("", live)
        self.live_en.setWordWrap(True)
        self.live_pt = SubtitleLabel("", live)
        self.live_pt.setWordWrap(True)
        self.live_empty = BodyLabel("As falas traduzidas aparecem aqui enquanto as legendas estiverem rodando.", live)
        self.live_empty.setWordWrap(True)
        self.live_empty.setStyleSheet("color: gray;")
        ll.addWidget(self.live_empty)
        ll.addWidget(self.live_en)
        ll.addWidget(self.live_pt)
        self.live_en.hide()
        self.live_pt.hide()
        lay.addWidget(live)

        # --- Resumo do motor
        self.engine_info = CaptionLabel("", self)
        self.engine_info.setStyleSheet("color: gray;")
        lay.addWidget(self.engine_info)
        lay.addStretch(1)

        self.programs = []
        self.refresh_programs()
        self.audio_timer = QTimer(self, timeout=self._update_audio_hint, interval=3000)
        self.audio_timer.start()

    # programas
    def refresh_programs(self):
        current = self.win.cfg["programa"].lower()
        self.programs = list_programs()
        if not any(p["exe"] == current for p in self.programs):
            self.programs.append({"exe": current, "path": None, "title": "", "playing": False,
                                  "name": current.capitalize() + " (fechado)"})
        self.combo.blockSignals(True)
        self.combo.clear()
        provider = QFileIconProvider()
        for p in self.programs:
            icon = provider.icon(QFileInfo(p["path"])) if p["path"] else FIF.APPLICATION.icon()
            label = p["name"] + ("   🔊" if p["playing"] else "")
            self.combo.addItem(label, icon, p["exe"])
        idx = next(i for i, p in enumerate(self.programs) if p["exe"] == current)
        self.combo.setCurrentIndex(idx)
        self.combo.blockSignals(False)
        self._update_audio_hint()

    def _on_program(self, idx):
        if 0 <= idx < len(self.programs):
            self.win.cfg["programa"] = self.programs[idx]["exe"]
            save_config(self.win.cfg)
            self._update_audio_hint()
            if self.win.running():
                InfoBar.info("Programa alterado", "Reinicie as legendas para ouvir o novo programa.",
                             duration=3500, position=InfoBarPosition.TOP_RIGHT, parent=self.win)

    def _update_audio_hint(self):
        exe = self.win.cfg["programa"].lower() + ".exe"
        running = any((p.info["name"] or "").lower() == exe for p in psutil.process_iter(["name"]))
        if not running:
            self.audio_hint.setText("○  Esse programa não está aberto — as legendas começam quando ele abrir.")
        elif exe in playing_audio():
            self.audio_hint.setText("●  Tocando som agora")
        else:
            self.audio_hint.setText("○  Aberto, sem som no momento")

    # estado
    def refresh_setup(self, st):
        """Mostra o aviso e bloqueia o Iniciar enquanto faltar algo essencial."""
        self.missing = essentials_missing(st)
        if self.missing:
            mb = sum(modulos.BY_ID[m]["mb"] for m in modulos.recommended(self.win.vram) if not st[m])
            self.setup_text.setText(f"Ainda falta: {', '.join(self.missing)}. A instalação completa recomendada "
                                    f"para este PC baixa {fmt_size(mb)}.")
            self.setup_btn.setText("Instalação completa")
        self.setup_card.setVisible(bool(self.missing))
        if not self.win.running():
            self.start_btn.setEnabled(not self.missing)

    def set_state(self, state, title, detail=""):
        self.dot.setState(state)
        self.status_title.setText(title)
        self.status_detail.setText(detail)
        self.ring.setVisible(state == "loading")
        if state == "idle" or state == "error":
            self.start_btn.setText("Iniciar legendas")
            self.start_btn.setIcon(FIF.PLAY)
        else:
            self.start_btn.setText("Parar legendas")
            self.start_btn.setIcon(FIF.PAUSE)

    def show_line(self, pt, en, latency=None):
        self.live_empty.hide()
        self.live_pt.show()
        self.live_pt.setText(pt)
        self.live_en.setVisible(bool(en))
        self.live_en.setText(en)
        if latency is not None:
            self.latency.setText(f"{latency:.2f} s".replace(".", ","))

    def clear_live(self):
        self.live_pt.hide()
        self.live_en.hide()
        self.live_empty.show()
        self.latency.setText("")


class OptionCard(SettingCard):
    def __init__(self, icon, title, content, widget, parent=None):
        super().__init__(icon, title, content, parent)
        self.hBoxLayout.addWidget(widget, 0, Qt.AlignRight)
        self.hBoxLayout.addSpacing(16)


class SliderBox(QWidget):
    def __init__(self, lo, hi, value, fmt, on_change, parent=None):
        super().__init__(parent)
        h = QHBoxLayout(self)
        h.setContentsMargins(0, 0, 0, 0)
        self.label = BodyLabel("", self)
        self.label.setFixedWidth(52)
        self.label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.slider = Slider(Qt.Horizontal, self)
        self.slider.setRange(lo, hi)
        self.slider.setValue(value)
        self.slider.setFixedWidth(220)
        self.fmt = fmt
        self.label.setText(fmt(value))
        self.slider.valueChanged.connect(lambda v: (self.label.setText(fmt(v)), on_change(v)))
        h.addWidget(self.label)
        h.addSpacing(8)
        h.addWidget(self.slider)


class PerformanceCard(SimpleCardWidget):
    """Como o Legendai deve trabalhar: automatico (testa o PC), manual (barra rapido x preciso)
    ou avancado (cada ajuste a mao)."""

    MODES = [("auto", "Automático"), ("barra", "Manual"), ("avancado", "Avançado")]
    DESC = {
        "auto": "O Legendaí testa este PC e usa o nível mais preciso que ainda responde rápido.",
        "barra": "Arraste para escolher entre velocidade e precisão. Só aparecem os níveis que cabem neste PC.",
        "avancado": "Você ajusta cada parte em “Inteligência”, logo abaixo.",
    }

    def __init__(self, page):
        super().__init__(page.view)
        self.page, self.win = page, page.win
        self.setFixedHeight(258)
        v = QVBoxLayout(self)
        v.setContentsMargins(20, 16, 20, 16)
        v.setSpacing(8)

        top = QHBoxLayout()
        icon = QLabel(self)
        icon.setPixmap(FIF.SPEED_HIGH.icon().pixmap(18, 18))
        top.addWidget(icon)
        top.addSpacing(6)
        top.addWidget(StrongBodyLabel("Como o Legendaí deve trabalhar", self))
        top.addStretch(1)
        self.seg = SegmentedWidget(self)
        for key, text in self.MODES:
            self.seg.addItem(key, text, onClick=lambda k=key: self.page.set_mode(k))
        top.addWidget(self.seg)
        v.addLayout(top)
        self.desc = CaptionLabel("", self)
        self.desc.setStyleSheet("color: gray;")
        v.addWidget(self.desc)
        v.addSpacing(6)

        row = QHBoxLayout()
        row.addWidget(CaptionLabel("⚡  Mais rápido", self))
        self.slider = Slider(Qt.Horizontal, self)
        self.slider.setPageStep(1)
        self.slider.valueChanged.connect(lambda i: self.page.set_level(self.levels[i]["key"]))
        row.addWidget(self.slider, 1)
        row.addWidget(CaptionLabel("Mais preciso  🎯", self))
        v.addLayout(row)

        self.level_name = SubtitleLabel("", self)
        self.level_info = CaptionLabel("", self)
        self.level_info.setWordWrap(True)
        v.addWidget(self.level_name)
        v.addWidget(self.level_info)
        v.addStretch(1)

        bottom = QHBoxLayout()
        self.test_info = CaptionLabel("", self)
        self.test_info.setStyleSheet(f"color: {ACCENT};")
        bottom.addWidget(self.test_info, 1)
        self.progress = ProgressBar(self)
        self.progress.setFixedWidth(160)
        self.progress.hide()
        bottom.addWidget(self.progress)
        self.test_btn = PushButton(self)
        self.test_btn.setText("Testar meu PC")
        self.test_btn.setIcon(FIF.SPEED_MEDIUM)
        self.test_btn.clicked.connect(self.win.run_selftest)
        bottom.addWidget(self.test_btn)
        v.addLayout(bottom)
        self.refresh()

    def refresh(self):
        cfg = self.win.cfg
        bench = cfg.get("teste")
        self.levels = D.available(self.win.vram, bench)
        keys = [lv["key"] for lv in self.levels]
        mode = cfg["modo"]
        self.seg.setCurrentItem(mode)
        self.desc.setText(self.DESC[mode])
        self.slider.blockSignals(True)
        self.slider.setRange(0, len(self.levels) - 1)
        self.slider.setValue(keys.index(cfg["nivel"]) if cfg["nivel"] in keys else 0)
        self.slider.blockSignals(False)
        self.slider.setEnabled(mode == "barra" and len(self.levels) > 1)

        if mode == "avancado":
            self.level_name.setText("Configuração personalizada")
            self.level_info.setText("Os níveis da barra não se aplicam enquanto o modo Avançado estiver ativo.")
        else:
            lv = D.level(cfg["nivel"])
            self.level_name.setText(lv["nome"] + ("  · escolhido automaticamente" if mode == "auto" else ""))
            parts = [lv["qualidade"].capitalize(), "placa de vídeo" if lv["gpu"] else "só processador"]
            est = D.estimate(lv, bench)
            if est:
                parts.insert(1, f"legenda final ~{est[0]:.1f} s após a fala".replace(".", ","))
                if est[1]:
                    parts.insert(2, f"frase se montando a cada ~{est[1]:.1f} s".replace(".", ","))
            self.level_info.setText("  ·  ".join(parts))

        if self.win.testing():
            return
        self.progress.hide()
        self.test_btn.setEnabled(True)
        if bench:
            gpu = bench.get("gpu") or "sem placa de vídeo dedicada"
            self.test_info.setText(f"Último teste: {bench.get('data', '')} · {gpu}")
        else:
            self.test_info.setText("Este PC ainda não foi testado — os tempos aparecem depois do teste (~1 min).")

    def set_progress(self, value, step):
        self.progress.show()
        self.progress.setValue(int(value * 100))
        self.test_btn.setEnabled(False)
        self.test_info.setText(f"Testando: {step}…")


class SettingsPage(Page):
    def __init__(self, win):
        super().__init__("settings", win)
        self.win = win
        cfg = win.cfg
        lay = ExpandLayout(self.view)
        lay.setContentsMargins(36, 24, 36, 36)
        lay.setSpacing(24)

        title = TitleLabel("Ajustes", self.view)
        lay.addWidget(title)

        # --- Desempenho
        g0 = SettingCardGroup("Desempenho", self.view)
        self.perf = PerformanceCard(self)
        g0.addSettingCard(self.perf)
        lay.addWidget(g0)

        # --- Legenda
        g1 = SettingCardGroup("Legenda na tela", self.view)
        self.preview = SubtitlePreview(cfg, g1)
        prev_card = SimpleCardWidget(g1)
        prev_card.setFixedHeight(250)  # o grupo de ajustes usa a altura fixa de cada card
        pv = QVBoxLayout(prev_card)
        pv.setContentsMargins(12, 12, 12, 12)
        pv.addWidget(self.preview)
        g1.addSettingCard(prev_card)
        g1.addSettingCard(OptionCard(FIF.FONT_SIZE, "Tamanho do texto", "Tamanho da legenda em português",
                                     SliderBox(16, 56, cfg["fonte"], lambda v: f"{v} pt",
                                               lambda v: self._set("fonte", v)), g1))
        g1.addSettingCard(OptionCard(FIF.ALIGNMENT, "Altura na tela", "Distância da borda de baixo",
                                     SliderBox(0, 40, int(cfg["altura"]), lambda v: f"{v} %",
                                               lambda v: self._set("altura", v)), g1))
        g1.addSettingCard(OptionCard(FIF.STOP_WATCH, "Tempo na tela", "Quanto tempo cada frase fica visível",
                                     SliderBox(2, 10, int(cfg["duracao"]), lambda v: f"{v} s",
                                               lambda v: self._set("duracao", v)), g1))
        g1.addSettingCard(OptionCard(FIF.LANGUAGE, "Mostrar o inglês", "Exibe a fala original embaixo da tradução",
                                     self._switch("ingles"), g1))
        lay.addWidget(g1)

        # --- Inteligencia (editavel so no nivel Personalizado)
        self.g2 = g2 = SettingCardGroup("Inteligência", self.view)
        self.controls = {}
        avail = [t for t in TRANSLATORS if t[3] is None or os.path.exists(os.path.join(MODELS, t[3]))]
        self._combo(g2, "tradutor", [(k, f"{n} — {d}") for k, n, d, _ in avail],
                    FIF.ROBOT, "Tradutor", "Modelo que traduz as frases para o português")
        partial_avail = [p for p in PARTIAL_TRANSLATORS
                         if p[0] == "nenhum" or any(t[0] == p[0] for t in avail)]
        self._combo(g2, "parcial_modelo", partial_avail, FIF.SPEED_MEDIUM, "Tradutor das frases parciais",
                    "Um modelo menor deixa a frase em andamento mais ágil")
        self._bool(g2, "tradutor_gpu", FIF.GAME, "Tradutor na placa de vídeo",
                   "Desligado, o tradutor usa só o processador (mais lento)")
        self._bool(g2, "parcial", FIF.EDIT, "Montar a frase enquanto falam",
                   "A legenda aparece palavra por palavra (pesa mais no PC)")
        self._combo(g2, "cadencia", CADENCES, FIF.SYNC, "Velocidade das parciais",
                    "Com que frequência a frase em andamento é atualizada")
        self._combo(g2, "motor", ENGINES, FIF.MICROPHONE, "Reconhecimento de fala", "Onde o Whisper roda")
        self._combo(g2, "modelo_cpu", CPU_MODELS, FIF.MICROPHONE, "Modelo de fala no processador",
                    "Usado quando o reconhecimento roda no processador")
        threads = SliderBox(1, CPU_COUNT, int(cfg["threads"]), lambda v: f"{v}", lambda v: self._set("threads", v))
        self.controls["threads"] = ("slider", threads.slider)
        self._combo(g2, "beam_final", [(1, "Rápido"), (3, "Cuidadoso — um pouco mais lento")], FIF.ACCEPT,
                    "Reconhecimento da frase final", "Cuidadoso erra menos palavras, mas demora um pouco mais")
        g2.addSettingCard(OptionCard(FIF.DEVELOPER_TOOLS, "Núcleos do processador",
                                     f"Quantos dos {CPU_COUNT} núcleos as legendas podem usar", threads, g2))
        lay.addWidget(g2)

        # --- App
        g3 = SettingCardGroup("Aplicativo", self.view)
        th = ComboBox(g3)
        th.setMinimumWidth(180)
        themes = [("auto", "Igual ao Windows"), ("light", "Claro"), ("dark", "Escuro")]
        for key, name in themes:
            th.addItem(name, None, key)
        th.setCurrentIndex(next((i for i, t in enumerate(themes) if t[0] == cfg["tema"]), 0))
        th.currentIndexChanged.connect(lambda i: (self._set("tema", themes[i][0], restart_hint=False),
                                                  apply_theme(themes[i][0])))
        g3.addSettingCard(OptionCard(FIF.PALETTE, "Tema", "Aparência deste painel", th, g3))
        lay.addWidget(g3)

        self._refresh_controls()

    # construtores de controles ligados ao config
    def _combo(self, group, key, options, icon, title, content):
        cb = ComboBox(group)
        cb.setMinimumWidth(280)
        for k, name in options:
            cb.addItem(name, None, k)
        cb.currentIndexChanged.connect(lambda i: self._set(key, options[i][0]))
        self.controls[key] = ("combo", cb, options)
        group.addSettingCard(OptionCard(icon, title, content, cb, group))

    def _bool(self, group, key, icon, title, content):
        sw = self._switch(key)
        self.controls[key] = ("switch", sw)
        group.addSettingCard(OptionCard(icon, title, content, sw, group))

    def _switch(self, key):
        sw = SwitchButton(self.view)
        sw.setOnText("Ligado")
        sw.setOffText("Desligado")
        sw.setChecked(bool(self.win.cfg[key]))
        sw.checkedChanged.connect(lambda v: self._set(key, v))
        return sw

    def _refresh_controls(self):
        """Mostra nos controles os valores atuais e so deixa editar no nivel Personalizado."""
        cfg = self.win.cfg
        for key, ctl in self.controls.items():
            w = ctl[1]
            w.blockSignals(True)
            if ctl[0] == "combo":
                idx = next((i for i, o in enumerate(ctl[2]) if o[0] == cfg[key]), None)
                if idx is None:  # modelo escolhido nao existe neste PC: usa o primeiro disponivel
                    idx = 0
                    cfg[key] = ctl[2][0][0]
                w.setCurrentIndex(idx)
            elif ctl[0] == "switch":
                w.setChecked(bool(cfg[key]))
            else:
                w.setValue(int(cfg[key]))
            w.blockSignals(False)
        self.g2.setEnabled(cfg["modo"] == "avancado")

    def refresh_performance(self):
        self._refresh_controls()
        self.perf.refresh()

    def set_mode(self, mode):
        cfg = self.win.cfg
        cfg["modo"] = mode
        if mode == "auto":
            if cfg.get("teste"):
                cfg["nivel"] = D.auto_choice(self.win.vram, cfg["teste"])["key"]
            else:
                QTimer.singleShot(0, self.win.run_selftest)
        if mode in ("auto", "barra"):
            cfg.update(D.level(cfg["nivel"])["cfg"])  # o Avancado parte dos valores do nivel atual
        self.refresh_performance()
        self._set("modo", mode)

    def set_level(self, key):
        cfg = self.win.cfg
        cfg["nivel"] = key
        cfg.update(D.level(key)["cfg"])
        self.refresh_performance()
        self._set("nivel", key)

    def _set(self, key, value, restart_hint=True):
        self.win.cfg[key] = value
        save_config(self.win.cfg)
        self.preview.update()
        if restart_hint and self.win.running() and not getattr(self, "_hinted", False):
            self._hinted = True
            InfoBar.info("Ajuste salvo", "Vale na próxima vez que você iniciar as legendas.",
                         duration=3000, position=InfoBarPosition.TOP_RIGHT, parent=self.win)
            QTimer.singleShot(4000, lambda: setattr(self, "_hinted", False))


def fmt_size(mb):
    return f"{mb / 1024:.1f} GB".replace(".", ",") if mb >= 1000 else f"{mb:.0f} MB"


def essentials_missing(st):
    """O que ainda falta para conseguir legendar (vazio = pronto)."""
    missing = []
    if not st["nucleo"]:
        missing.append("núcleo de reconhecimento")
    if not (st["fala_gpu"] or st["fala_cpu"] or st["fala_cpu_small"]):
        missing.append("um módulo de fala")
    if not (st["argos"] or (st["motor_traducao"] and (st["gemma1"] or st["gemma4"] or st["gemma12"]))):
        missing.append("um tradutor")
    return missing


class ModuleRow(SimpleCardWidget):
    def __init__(self, page, mod):
        super().__init__(page.view)
        self.page, self.mod = page, mod
        self.setFixedHeight(76)
        h = QHBoxLayout(self)
        h.setContentsMargins(18, 10, 18, 10)
        h.setSpacing(14)
        icon = QLabel(self)
        icon.setPixmap(page.ICONS.get(mod["id"], FIF.APPLICATION).icon().pixmap(22, 22))
        h.addWidget(icon)
        txt = QVBoxLayout()
        txt.setSpacing(2)
        top = QHBoxLayout()
        top.setSpacing(8)
        top.addWidget(StrongBodyLabel(mod["nome"], self))
        self.badge = CaptionLabel("", self)
        top.addWidget(self.badge)
        top.addStretch(1)
        txt.addLayout(top)
        desc = CaptionLabel(mod["desc"], self)
        desc.setStyleSheet("color: gray;")
        txt.addWidget(desc)
        h.addLayout(txt, 1)
        h.addWidget(CaptionLabel(fmt_size(mod["mb"]), self))
        self.btn = PushButton(self)
        self.btn.setFixedWidth(120)
        self.btn.clicked.connect(self._click)
        h.addWidget(self.btn)

    def refresh(self, installed, recommended, busy):
        mid = self.mod["id"]
        if installed:
            self.badge.setText("✓ Instalado")
            self.badge.setStyleSheet("color: #22C55E;")
            self.btn.setText("Remover")
            self.btn.setIcon(FIF.DELETE)
            self.btn.setEnabled(not busy and self.mod.get("removivel", True))
        else:
            self.badge.setText("★ Recomendado para este PC" if mid in recommended else "")
            self.badge.setStyleSheet(f"color: {ACCENT};")
            self.btn.setText("Instalar")
            self.btn.setIcon(FIF.DOWNLOAD)
            self.btn.setEnabled(not busy)

    def _click(self):
        if modulos.installed(self.mod["id"]):
            self.page.remove(self.mod["id"])
        else:
            self.page.install([self.mod["id"]])


class ModulesPage(Page):
    ICONS = {"nucleo": FIF.DEVELOPER_TOOLS, "fala_gpu": FIF.GAME, "fala_cpu": FIF.MICROPHONE,
             "fala_cpu_small": FIF.MICROPHONE, "motor_traducao": FIF.ROBOT, "gemma1": FIF.LANGUAGE,
             "gemma4": FIF.LANGUAGE, "gemma12": FIF.LANGUAGE, "argos": FIF.LANGUAGE}
    GROUPS = [("Reconhecimento de fala", ["nucleo", "fala_gpu", "fala_cpu", "fala_cpu_small"]),
              ("Tradução", ["motor_traducao", "gemma1", "gemma4", "gemma12", "argos"])]

    def __init__(self, win):
        super().__init__("modules", win)
        self.win = win
        self.proc = None
        lay = QVBoxLayout(self.view)
        lay.setContentsMargins(36, 24, 36, 36)
        lay.setSpacing(12)
        lay.addWidget(TitleLabel("Módulos", self.view))
        sub = BodyLabel("O Legendaí instala só o essencial. Baixe aqui as partes que quiser usar — "
                        "dá para remover depois e liberar espaço.", self.view)
        sub.setWordWrap(True)
        lay.addWidget(sub)
        lay.addSpacing(6)

        # --- instalacao completa
        hero = SimpleCardWidget(self.view)
        hv = QVBoxLayout(hero)
        hv.setContentsMargins(20, 16, 20, 16)
        hv.setSpacing(8)
        row = QHBoxLayout()
        icon = QLabel(hero)
        icon.setPixmap(FIF.CLOUD_DOWNLOAD.icon().pixmap(22, 22))
        row.addWidget(icon)
        row.addSpacing(6)
        col = QVBoxLayout()
        col.setSpacing(2)
        col.addWidget(StrongBodyLabel("Instalação completa recomendada", hero))
        self.full_info = CaptionLabel("", hero)
        self.full_info.setWordWrap(True)
        col.addWidget(self.full_info)
        row.addLayout(col, 1)
        self.full_btn = PrimaryPushButton(hero)
        self.full_btn.setIcon(FIF.DOWNLOAD)
        self.full_btn.setMinimumWidth(220)
        self.full_btn.setFixedHeight(36)
        self.full_btn.clicked.connect(lambda: self.install(self.missing_recommended()))
        row.addWidget(self.full_btn)
        hv.addLayout(row)
        # progresso
        self.prog_row = QWidget(hero)
        pr = QHBoxLayout(self.prog_row)
        pr.setContentsMargins(0, 6, 0, 0)
        self.prog_label = CaptionLabel("", self.prog_row)
        self.prog_bar = ProgressBar(self.prog_row)
        self.cancel_btn = PushButton(self.prog_row)
        self.cancel_btn.setText("Cancelar")
        self.cancel_btn.clicked.connect(self.cancel)
        pr.addWidget(self.prog_label, 1)
        pr.addWidget(self.prog_bar, 1)
        pr.addWidget(self.cancel_btn)
        self.prog_row.hide()
        hv.addWidget(self.prog_row)
        lay.addWidget(hero)

        self.rows = {}
        for title, ids in self.GROUPS:
            lay.addSpacing(10)
            lay.addWidget(SubtitleLabel(title, self.view))
            for mid in ids:
                self.rows[mid] = ModuleRow(self, modulos.BY_ID[mid])
                lay.addWidget(self.rows[mid])
        self.disk = CaptionLabel("", self.view)
        self.disk.setStyleSheet("color: gray;")
        lay.addSpacing(6)
        lay.addWidget(self.disk)
        lay.addStretch(1)
        self.refresh()

    def busy(self):
        return self.proc is not None and self.proc.state() != QProcess.NotRunning

    def recommended(self):
        return modulos.recommended(self.win.vram)

    def missing_recommended(self):
        return [m for m in self.recommended() if not modulos.installed(m)]

    def refresh(self):
        st = modulos.status()
        rec, busy = self.recommended(), self.busy()
        for mid, row in self.rows.items():
            row.refresh(st[mid], rec, busy)
        missing = self.missing_recommended()
        gpu = f"{self.win.gpu_name}, {self.win.vram:.0f} GB" if self.win.vram >= 2 else "sem placa de vídeo dedicada"
        if missing:
            mb = sum(modulos.BY_ID[m]["mb"] for m in missing)
            names = ", ".join(modulos.BY_ID[m]["nome"] for m in missing)
            self.full_info.setText(f"Para este PC ({gpu}): {names}.")
            self.full_btn.setText(f"Instalar tudo  ({fmt_size(mb)})")
            self.full_btn.setEnabled(not busy)
        else:
            self.full_info.setText(f"Tudo o que é recomendado para este PC ({gpu}) já está instalado.")
            self.full_btn.setText("Tudo instalado ✓")
            self.full_btn.setEnabled(False)
        free = shutil.disk_usage(BASE).free / 2 ** 30
        self.disk.setText(f"Espaço livre no disco: {free:.0f} GB".replace(".", ","))
        self.win.home.refresh_setup(st)

    def _run(self, args, label):
        self.proc = QProcess(self)
        self.proc.setProgram(self.win._python())
        self.proc.setArguments([os.path.join(BASE, "modulos.py"), *args])
        self.proc.setWorkingDirectory(BASE)
        env = QProcessEnvironment.systemEnvironment()
        env.insert("PYTHONIOENCODING", "utf-8")
        env.insert("PYTHONUNBUFFERED", "1")
        self.proc.setProcessEnvironment(env)
        self.buf, self.error = "", None
        self.proc.readyReadStandardOutput.connect(self._read)
        self.proc.finished.connect(self._done)
        self.proc.start()
        if self.proc.waitForStarted(5000):
            kill_with_parent(self.proc.processId())
        self.prog_row.show()
        self.prog_bar.setValue(0)
        self.prog_label.setText(label)
        self.win.log.append(f"──── {time.strftime('%H:%M:%S')}  módulos: {' '.join(args)} ────")
        self.refresh()

    def install(self, ids):
        if self.busy() or not ids:
            return
        if self.win.running():
            InfoBar.warning("Pare as legendas primeiro", "A instalação pode substituir arquivos em uso.",
                            duration=4000, position=InfoBarPosition.TOP_RIGHT, parent=self.win)
            return
        ids = modulos.with_deps(ids)
        self.total = len(ids)
        self.order = ids
        self._run(["instalar", *ids], "Preparando…")

    def remove(self, mid):
        if self.busy() or self.win.running():
            return
        self.order, self.total = [mid], 1
        self._run(["remover", mid], f"Removendo {modulos.BY_ID[mid]['nome']}…")

    def cancel(self):
        if self.busy():
            self.cancelled = True
            self.proc.kill()

    def _read(self):
        self.buf += bytes(self.proc.readAllStandardOutput()).decode("utf-8", errors="replace")
        *lines, self.buf = self.buf.split("\n")
        for line in lines:
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if "erro" in msg:
                self.error = msg["erro"]
                self.win.log.append("[módulos] erro: " + msg["erro"])
            elif "modulo" in msg:
                mid = msg["modulo"]
                idx = self.order.index(mid) if mid in self.order else 0
                frac = (idx + msg.get("progresso", 0)) / max(1, self.total)
                self.prog_bar.setValue(int(frac * 100))
                self.prog_label.setText(f"{modulos.BY_ID[mid]['nome']}: {msg.get('etapa', '')}")
                if msg.get("ok") or msg.get("removido"):
                    self.win.log.append(f"[módulos] {modulos.BY_ID[mid]['nome']}: "
                                        + ("removido" if msg.get("removido") else "instalado"))
                    self.refresh()

    def _done(self, *_):
        cancelled = getattr(self, "cancelled", False)
        self.cancelled = False
        self.prog_row.hide()
        if cancelled:
            InfoBar.info("Download pausado", "Ao instalar de novo, ele continua de onde parou.",
                         duration=4000, position=InfoBarPosition.TOP_RIGHT, parent=self.win)
        elif self.error:
            InfoBar.error("Não deu para concluir", self.error[:160], duration=8000,
                          position=InfoBarPosition.TOP_RIGHT, parent=self.win)
        else:
            InfoBar.success("Módulos atualizados", "Tudo pronto.", duration=3000,
                            position=InfoBarPosition.TOP_RIGHT, parent=self.win)
        self.refresh()
        self.win.modules_changed()


class LogPage(Page):
    def __init__(self, win):
        super().__init__("log", win)
        lay = QVBoxLayout(self.view)
        lay.setContentsMargins(36, 24, 36, 36)
        lay.setSpacing(16)
        head = QHBoxLayout()
        head.addWidget(TitleLabel("Registro", self.view))
        head.addStretch(1)
        clear = PushButton(self.view)
        clear.setText("Limpar")
        clear.setIcon(FIF.BROOM)
        head.addWidget(clear)
        lay.addLayout(head)
        lay.addWidget(CaptionLabel("Tudo o que as legendas fizeram nesta sessão — útil se algo der errado.", self.view))
        self.text = PlainTextEdit(self.view)
        self.text.setReadOnly(True)
        self.text.setFont(QFont("Cascadia Mono", 9))
        self.text.setMinimumHeight(420)
        clear.clicked.connect(self.text.clear)
        lay.addWidget(self.text, 1)

    def append(self, line):
        self.text.appendPlainText(line)


# ---------------------------------------------------------------- janela

def apply_theme(key):
    setTheme({"auto": Theme.AUTO, "light": Theme.LIGHT, "dark": Theme.DARK}[key])


class MainWindow(FluentWindow):
    def __init__(self):
        super().__init__()
        self.cfg = load_config()
        self.proc = None
        self.buf = ""
        self.pending_en = None
        self.pending_latency = None
        self.started_at = None
        self.stopping = False
        self.tail = []
        self.test_proc = None
        self.gpu_name, self.vram = D.gpu_info()

        self.setWindowTitle("Legendaí")
        self.setWindowIcon(app_icon())
        self.resize(980, 760)
        self.setMinimumWidth(760)

        self.home = HomePage(self)
        self.modules = ModulesPage(self)
        self.settings = SettingsPage(self)
        self.log = LogPage(self)
        self.addSubInterface(self.home, FIF.HOME, "Início")
        self.addSubInterface(self.modules, FIF.DOWNLOAD, "Módulos")
        self.addSubInterface(self.log, FIF.HISTORY, "Registro")
        self.addSubInterface(self.settings, FIF.SETTING, "Ajustes", NavigationItemPosition.BOTTOM)
        self.navigationInterface.setExpandWidth(220)

        self.clock = QTimer(self, timeout=self._tick_clock, interval=1000)
        self._setup_tray()
        self._center()

        # Modo automatico: testa o PC na primeira vez e sempre que a placa de video mudar.
        bench = self.cfg.get("teste")
        if self.cfg["modo"] == "auto" and (not bench or bench.get("gpu") != self.gpu_name)                 and not self.home.missing:
            QTimer.singleShot(1500, self.run_selftest)

    def modules_changed(self):
        """Depois de instalar/remover modulos: atualiza os niveis e refaz o teste no modo automatico."""
        self.settings.refresh_performance()
        if self.cfg["modo"] == "auto" and not self.home.missing:
            self.run_selftest()

    # --- teste do PC (ajuste automatico)
    def testing(self):
        return self.test_proc is not None and self.test_proc.state() != QProcess.NotRunning

    def _python(self):
        python = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
        return python if os.path.exists(python) else sys.executable

    def run_selftest(self):
        if self.testing():
            return
        if self.running():
            InfoBar.warning("Pare as legendas primeiro", "O teste usa a placa de vídeo e o processador por ~1 minuto.",
                            duration=4000, position=InfoBarPosition.TOP_RIGHT, parent=self)
            return
        self.test_proc = QProcess(self)
        self.test_proc.setProgram(self._python())
        self.test_proc.setArguments([os.path.join(BASE, "autoajuste.py")])
        self.test_proc.setWorkingDirectory(BASE)
        env = QProcessEnvironment.systemEnvironment()
        env.insert("PYTHONIOENCODING", "utf-8")
        env.insert("PYTHONUNBUFFERED", "1")
        self.test_proc.setProcessEnvironment(env)
        self.test_buf, self.test_result = "", None
        self.test_proc.readyReadStandardOutput.connect(self._read_test)
        self.test_proc.finished.connect(self._test_done)
        self.test_proc.start()
        if self.test_proc.waitForStarted(5000):
            kill_with_parent(self.test_proc.processId())
        self.settings.perf.set_progress(0, "preparando")
        self.log.append(f"──── {time.strftime('%H:%M:%S')}  testando este PC ────")
        InfoBar.info("Testando este PC", "Leva cerca de 1 minuto. O resultado aparece em Ajustes.",
                     duration=4000, position=InfoBarPosition.TOP_RIGHT, parent=self)

    def _read_test(self):
        self.test_buf += bytes(self.test_proc.readAllStandardOutput()).decode("utf-8", errors="replace")
        *lines, self.test_buf = self.test_buf.split("\n")
        for line in lines:
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if "progresso" in msg:
                self.settings.perf.set_progress(msg["progresso"], msg["etapa"])
            elif "aviso" in msg:
                self.log.append("[teste] " + msg["aviso"])
            elif "resultado" in msg:
                self.test_result = msg["resultado"]

    def _test_done(self, *_):
        if not self.test_result:
            InfoBar.error("O teste do PC falhou", "Veja a aba Registro. Mantive a configuração atual.",
                          duration=5000, position=InfoBarPosition.TOP_RIGHT, parent=self)
            self.settings.refresh_performance()
            return
        self.cfg["teste"] = self.test_result
        self.log.append("[teste] " + json.dumps(self.test_result, ensure_ascii=False))
        if self.cfg["modo"] == "auto":
            lv = D.auto_choice(self.vram, self.test_result)
            self.cfg["nivel"] = lv["key"]
            self.cfg.update(lv["cfg"])
            InfoBar.success("Ajuste automático concluído", f"Nível escolhido para este PC: {lv['nome']}.",
                            duration=5000, position=InfoBarPosition.TOP_RIGHT, parent=self)
        save_config(self.cfg)
        self.settings.refresh_performance()

    def _center(self):
        geo = QApplication.primaryScreen().availableGeometry()
        self.move(geo.center().x() - self.width() // 2, geo.center().y() - self.height() // 2)

    def _setup_tray(self):
        self.tray = QSystemTrayIcon(app_icon(), self)
        self.tray.setToolTip("Legendaí")
        menu = SystemTrayMenu(parent=self)
        self.tray_toggle = Action(FIF.PLAY, "Iniciar legendas", self, triggered=self.toggle)
        menu.addActions([
            Action(FIF.HOME, "Abrir o painel", self, triggered=self._show),
            self.tray_toggle,
        ])
        menu.addSeparator()
        menu.addAction(Action(FIF.CLOSE, "Sair", self, triggered=self.quit))
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(lambda r: self._show() if r == QSystemTrayIcon.Trigger else None)
        self.tray.show()
        self.tray_hint_shown = False

    def _show(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    # --- processo das legendas
    def running(self):
        return self.proc is not None and self.proc.state() != QProcess.NotRunning

    def toggle(self):
        self.stop() if self.running() else self.start()

    def build_args(self):
        c = self.cfg
        args = [os.path.join(BASE, "legendas.py"), "--programa", c["programa"],
                "--fonte", str(c["fonte"]), "--altura", str(c["altura"]), "--duracao", str(c["duracao"]),
                "--parcial", "sim" if c["parcial"] else "nao", "--motor", c["motor"],
                "--modelo", c["modelo_cpu"], "--cadencia", c["cadencia"], "--threads", str(c["threads"]),
                "--llm-camadas-gpu", "99" if c["tradutor_gpu"] else "0", "--beam-final", str(c["beam_final"])]
        if c["ingles"]:
            args.append("--ingles")

        def model_path(key):
            """Arquivo do modelo escolhido; se nao existir, o proximo mais leve que existir."""
            keys = [t[0] for t in TRANSLATORS]
            for t in TRANSLATORS[keys.index(key) if key in keys else 0:]:
                if t[3] and os.path.exists(os.path.join(MODELS, t[3])):
                    return os.path.join(MODELS, t[3])
            return None

        main = model_path(c["tradutor"]) if c["tradutor"] != "argos" else None
        if main:
            args += ["--tradutor", "llm", "--modelo-llm", main]
            partial = model_path(c["parcial_modelo"]) if c["parcial_modelo"] != "nenhum" else None
            args += ["--modelo-llm-parcial", partial or "nenhum"]
        else:
            args += ["--tradutor", "argos"]
        return args

    def start(self):
        if self.home.missing:
            self.switchTo(self.modules)
            return
        if self.testing():
            InfoBar.warning("Aguarde o teste do PC", "Ele termina em cerca de 1 minuto; depois é só iniciar.",
                            duration=4000, position=InfoBarPosition.TOP_RIGHT, parent=self)
            return
        exe_dir = os.path.dirname(sys.executable)
        python = os.path.join(exe_dir, "pythonw.exe")
        if not os.path.exists(python):
            python = sys.executable
        self.proc = QProcess(self)
        self.proc.setProgram(python)
        self.proc.setArguments(self.build_args())
        self.proc.setWorkingDirectory(BASE)
        env = QProcessEnvironment.systemEnvironment()
        env.insert("PYTHONIOENCODING", "utf-8")
        env.insert("PYTHONUNBUFFERED", "1")
        self.proc.setProcessEnvironment(env)
        self.proc.setProcessChannelMode(QProcess.MergedChannels)
        self.proc.readyReadStandardOutput.connect(self._read)
        self.proc.finished.connect(self._finished)
        self.buf, self.tail, self.stopping = "", [], False
        self.log.append(f"──── {time.strftime('%H:%M:%S')}  iniciando: {self.cfg['programa']} ────")
        self.proc.start()
        if not self.proc.waitForStarted(5000):
            self.home.set_state("error", "Não consegui iniciar", self.proc.errorString())
            return
        kill_with_parent(self.proc.processId())
        self.home.set_state("loading", "Carregando modelos…", "Leva alguns segundos para preparar os modelos de fala e tradução.")
        self.home.clear_live()
        self.tray_toggle.setText("Parar legendas")
        self.tray_toggle.setIcon(FIF.PAUSE.icon())

    def stop(self):
        if self.running():
            self.stopping = True
            self.proc.kill()

    def _finished(self, code, _status):
        self.clock.stop()
        self.tray_toggle.setText("Iniciar legendas")
        self.tray_toggle.setIcon(FIF.PLAY.icon())
        self.log.append(f"──── {time.strftime('%H:%M:%S')}  encerrado (código {code}) ────")
        if self.stopping:
            self.home.set_state("idle", "Parado", "Escolha o programa e clique em Iniciar.")
        else:
            last = next((l for l in reversed(self.tail) if l.strip() and "warn" not in l.lower()), "")
            self.home.set_state("error", "As legendas pararam", last[:160] or "Veja a aba Registro.")
            InfoBar.error("As legendas pararam", "Veja a aba Registro para detalhes.",
                          duration=6000, position=InfoBarPosition.TOP_RIGHT, parent=self)

    def _read(self):
        self.buf += bytes(self.proc.readAllStandardOutput()).decode("utf-8", errors="replace")
        *lines, self.buf = self.buf.split("\n")
        for line in lines:
            line = line.rstrip("\r")
            self.log.append(line)
            self.tail = (self.tail + [line])[-20:]
            self._parse(line)

    def _parse(self, line):
        body = re.sub(r"^\d\d:\d\d:\d\d ", "", line)
        if body.startswith("Tradutor:"):
            self.translator_name = body.split(":", 1)[1].strip()
        elif "Pronto!" in body and body.startswith("Modelo de fala:"):
            speech = body.split(":", 1)[1].replace("Pronto!", "").strip(" .")
            lv = ("Personalizado" if self.cfg["modo"] == "avancado" else D.level(self.cfg["nivel"])["nome"]
                  + (" (automático)" if self.cfg["modo"] == "auto" else ""))
            self.home.engine_info.setText(f"Nível: {lv}   ·   Fala: {speech}   ·   "
                                          f"Tradução: {getattr(self, 'translator_name', '?')}")
        elif body.startswith("Capturando audio do"):
            name = next((p["name"] for p in self.home.programs if p["exe"] == self.cfg["programa"].lower()),
                        self.cfg["programa"].capitalize()).replace(" (fechado)", "")
            self.started_at = time.time()
            self.clock.start()
            self.home.set_state("running", f"Legendando o {name}", "Ouvindo… as legendas aparecem por cima da tela.")
        elif body.startswith("Esperando o"):
            self.home.set_state("waiting", "Esperando o programa abrir",
                                f"Assim que o {self.cfg['programa']} abrir, as legendas começam.")
        elif body.startswith("Nao deu para usar"):
            InfoBar.warning("Usando modo alternativo", body, duration=6000,
                            position=InfoBarPosition.TOP_RIGHT, parent=self)
        m = re.search(r"\(([\d.]+)s de audio, ([\d.]+)s p/ processar\)", body)
        if m:
            self.pending_latency = float(m.group(2))
        if line.startswith("   EN: "):
            self.pending_en = line[7:]
        elif line.startswith("   PT: "):
            self.home.show_line(line[7:], self.pending_en, self.pending_latency)
        elif line.startswith("   PT revisado: "):
            self.home.show_line(line[16:], self.pending_en, None)
        elif body.startswith("   ... "):  # frase parcial (vem com a hora na frente)
            self.home.show_line(body[7:].rstrip(" .…") + "…", "", None)

    def _tick_clock(self):
        if self.started_at and self.home.dot.state == "running":
            s = int(time.time() - self.started_at)
            self.home.status_detail.setText(f"Ouvindo há {s // 60:02d}:{s % 60:02d} · as legendas aparecem por cima da tela.")

    # --- janela
    def closeEvent(self, e):
        if self.running():
            e.ignore()
            self.hide()
            if not self.tray_hint_shown:
                self.tray_hint_shown = True
                self.tray.showMessage("Legendaí continua rodando",
                                      "As legendas seguem ativas. Use o ícone na bandeja para parar ou sair.",
                                      app_icon(), 4000)
        else:
            self.quit()

    def quit(self):
        self.stop()
        if self.proc is not None:
            self.proc.waitForFinished(3000)
        self.tray.hide()
        QApplication.quit()


def main():
    # Icone proprio na barra de tarefas (senao aparece o do Python).
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Legendai.App")
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    app.setWindowIcon(app_icon())
    ensure_ico()
    cfg = load_config()
    apply_theme(cfg["tema"])
    setThemeColor(ACCENT)
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
