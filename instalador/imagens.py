"""Gera as imagens do assistente de instalacao (identidade do Legendai) em 100% e 200%."""
import os
import sys

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QFont, QGuiApplication, QIcon, QImage, QLinearGradient, QPainter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = sys.argv[1]
app = QGuiApplication(sys.argv)
icon = QIcon(os.path.join(ROOT, "assets", "icon.svg"))


def gradient(p, w, h):
    g = QLinearGradient(0, 0, w, h)
    g.setColorAt(0, QColor("#6D4AFF"))
    g.setColorAt(0.55, QColor("#7B61FF"))
    g.setColorAt(1, QColor("#06B6D4"))
    p.fillRect(0, 0, w, h, g)


def large(scale):
    w, h = 164 * scale, 314 * scale
    img = QImage(w, h, QImage.Format_RGB32)
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing)
    gradient(p, w, h)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(255, 255, 255, 26))
    p.drawEllipse(QRectF(w * 0.35, -h * 0.12, w * 1.1, w * 1.1))
    p.setBrush(QColor(255, 255, 255, 16))
    p.drawEllipse(QRectF(-w * 0.4, h * 0.62, w * 1.0, w * 1.0))
    s = 72 * scale
    icon.paint(p, int((w - s) / 2), int(h * 0.26), int(s), int(s))
    f = QFont("Segoe UI Variable Display", 17)
    f.setPixelSize(int(24 * scale))
    f.setWeight(QFont.Bold)
    p.setFont(f)
    p.setPen(QColor("white"))
    p.drawText(QRectF(0, h * 0.26 + s + 12 * scale, w, 34 * scale), Qt.AlignHCenter, "Legendaí")
    f = QFont("Segoe UI Variable Text")
    f.setPixelSize(int(11 * scale))
    p.setFont(f)
    p.setPen(QColor(255, 255, 255, 220))
    p.drawText(QRectF(14 * scale, h * 0.26 + s + 48 * scale, w - 28 * scale, 60 * scale),
               Qt.AlignHCenter | Qt.TextWordWrap, "Legendas ao vivo\ndo inglês para o português")
    p.end()
    img.save(os.path.join(OUT, f"wizard_{scale * 100}.bmp"))


def small(scale):
    s = 55 * scale
    img = QImage(s, s, QImage.Format_RGB32)
    img.fill(QColor("white"))
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing)
    icon.paint(p, 0, 0, s, s)
    p.end()
    img.save(os.path.join(OUT, f"wizard_small_{scale * 100}.bmp"))


for sc in (1, 2):
    large(sc)
    small(sc)
print("imagens ok")
