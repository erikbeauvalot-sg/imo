"""Image diffusée par la caméra de l'agent : un invité anonyme n'a pas de photo de profil,
Teams n'affiche que ses initiales. Avec --avatar, la fausse caméra montre cette image."""

from __future__ import annotations

import atexit
import hashlib
import shutil
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

WIDTH, HEIGHT = 640, 360  # 16:9, format des vignettes Teams


def draw_default_avatar() -> Image.Image:
    """Petit robot souriant sur fond bleu-vert, avec le nom de l'agent."""
    img = Image.new("RGB", (WIDTH, HEIGHT))
    draw = ImageDraw.Draw(img)
    for y in range(HEIGHT):  # dégradé vertical
        t = y / HEIGHT
        draw.line([(0, y), (WIDTH, y)], fill=(int(40 + 30 * t), int(110 + 60 * t), int(190 - 20 * t)))

    cx, top = WIDTH // 2, 70
    # Antenne
    draw.line([(cx, top - 30), (cx, top)], fill=(240, 240, 250), width=6)
    draw.ellipse([cx - 12, top - 46, cx + 12, top - 22], fill=(255, 200, 70))
    # Tête et oreilles
    draw.rounded_rectangle([cx - 120, top - 12, cx - 100, top + 98], radius=8, fill=(210, 220, 235))
    draw.rounded_rectangle([cx + 100, top - 12, cx + 120, top + 98], radius=8, fill=(210, 220, 235))
    draw.rounded_rectangle([cx - 105, top, cx + 105, top + 170], radius=40, fill=(245, 248, 252))
    # Visière
    draw.rounded_rectangle([cx - 80, top + 25, cx + 80, top + 115], radius=30, fill=(35, 50, 80))
    # Yeux avec reflets
    for ex in (cx - 38, cx + 38):
        draw.ellipse([ex - 18, top + 45, ex + 18, top + 81], fill=(110, 230, 255))
        draw.ellipse([ex - 4, top + 51, ex + 6, top + 61], fill=(255, 255, 255))
    # Sourire et joues
    draw.arc([cx - 30, top + 72, cx + 30, top + 108], start=20, end=160, fill=(110, 230, 255), width=5)
    draw.ellipse([cx - 92, top + 120, cx - 66, top + 140], fill=(255, 170, 180))
    draw.ellipse([cx + 66, top + 120, cx + 92, top + 140], fill=(255, 170, 180))

    font = ImageFont.load_default(size=30)
    label = "IMV · Assistant IM"
    width = draw.textlength(label, font=font)
    draw.text(((WIDTH - width) / 2, HEIGHT - 62), label, font=font, fill=(255, 255, 255))
    return img


def fit(image: Image.Image) -> Image.Image:
    """Centre l'image sur un fond 16:9 sans la déformer."""
    image = image.convert("RGB")
    image.thumbnail((WIDTH, HEIGHT))
    canvas = Image.new("RGB", (WIDTH, HEIGHT), (40, 110, 190))
    canvas.paste(image, ((WIDTH - image.width) // 2, (HEIGHT - image.height) // 2))
    return canvas


def to_y4m(image: Image.Image, path: Path) -> Path:
    """Image fixe → vidéo Y4M d'une image (YUV 4:2:0), bouclée par la fausse caméra de Chromium."""
    y, cb, cr = image.convert("YCbCr").split()
    half = (WIDTH // 2, HEIGHT // 2)
    frame = y.tobytes() + cb.resize(half, Image.BILINEAR).tobytes() + cr.resize(half, Image.BILINEAR).tobytes()
    path.write_bytes(f"YUV4MPEG2 W{WIDTH} H{HEIGHT} F15:1 Ip A1:1 C420jpeg\nFRAME\n".encode() + frame)
    return path


_media_dir: Path | None = None


def media_dir() -> Path:
    """Dossier privé (0700) propre au processus, supprimé à la sortie : Chromium y lit les
    fichiers des fausses caméra et micro, qu'un autre utilisateur ne doit pas pouvoir remplacer."""
    global _media_dir
    if _media_dir is None:
        _media_dir = Path(tempfile.mkdtemp(prefix="imv-media-"))
        atexit.register(shutil.rmtree, _media_dir, ignore_errors=True)
    return _media_dir


def avatar_video(source: Path | None = None) -> Path:
    """Fichier Y4M de l'avatar : `source` (PNG, JPEG…) ou le robot par défaut."""
    image = fit(Image.open(source)) if source else draw_default_avatar()
    digest = hashlib.sha256(image.tobytes()).hexdigest()[:12]
    return to_y4m(image, media_dir() / f"avatar-{digest}.y4m")
