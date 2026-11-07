"""Image diffusée par la caméra de l'agent : un invité anonyme n'a pas de photo de profil,
Teams n'affiche que ses initiales. Avec --avatar, la fausse caméra montre cette image."""

from __future__ import annotations

import atexit
import hashlib
import shutil
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

WIDTH, HEIGHT = 640, 360  # 16:9, format des vignettes Teams
DEFAULT_LABEL = "IMV · Assistant IM"


SCALE = 3  # dessin en 3x puis réduction : bords lissés (Pillow ne lisse pas les formes)


def draw_default_avatar(label: str = DEFAULT_LABEL) -> Image.Image:
    """Robot souriant avec casque-micro d'assistant, halo et ombre, et le nom de l'agent."""
    w, h, s = WIDTH * SCALE, HEIGHT * SCALE, SCALE

    def box(x0, y0, x1, y1):
        return [x0 * s, y0 * s, x1 * s, y1 * s]

    # Fond : dégradé diagonal bleu nuit → turquoise
    ramp = Image.linear_gradient("L")
    img = Image.blend(ramp, ramp.rotate(90), 0.5).resize((w, h))
    img = Image.merge("RGB", [p.point(lambda v, a=a, b=b: a + (b - a) * v // 255) for p, a, b in
                              zip((img, img, img), (24, 46, 104), (32, 150, 170))])

    # Halo derrière la tête et bulles décoratives
    glow = Image.new("L", (w, h))
    gd = ImageDraw.Draw(glow)
    gd.ellipse(box(170, 10, 470, 290), fill=110)
    for x, y, r, a in ((80, 70, 34, 40), (560, 90, 22, 45), (520, 280, 40, 30), (110, 290, 16, 50)):
        gd.ellipse(box(x - r, y - r, x + r, y + r), fill=a)
    glow = glow.filter(ImageFilter.GaussianBlur(24 * s))
    img.paste((255, 255, 255), mask=glow)

    cx, top = WIDTH // 2, 54
    # Ombre portée de la tête
    shadow = Image.new("L", (w, h))
    ImageDraw.Draw(shadow).rounded_rectangle(box(cx - 100, top + 14, cx + 110, top + 186), radius=44 * s, fill=120)
    img.paste((10, 25, 60), mask=shadow.filter(ImageFilter.GaussianBlur(10 * s)))

    draw = ImageDraw.Draw(img)
    dark, accent, cyan = (40, 52, 82), (255, 176, 60), (110, 230, 255)
    # Arceau du casque
    draw.arc(box(cx - 122, top - 34, cx + 122, top + 170), start=195, end=345, fill=dark, width=14 * s)
    # Tête
    draw.rounded_rectangle(box(cx - 105, top, cx + 105, top + 170), radius=42 * s, fill=(246, 249, 253))
    draw.rounded_rectangle(box(cx - 105, top + 128, cx + 105, top + 170), radius=42 * s, fill=(226, 234, 245))
    draw.rectangle(box(cx - 105, top + 128, cx + 105, top + 140), fill=(246, 249, 253))
    # Écouteurs
    for ex in (cx - 116, cx + 116):
        draw.rounded_rectangle(box(ex - 18, top + 50, ex + 18, top + 118), radius=16 * s, fill=dark)
        draw.rounded_rectangle(box(ex - 8, top + 62, ex + 8, top + 106), radius=8 * s, fill=accent)
    # Micro : tige depuis l'écouteur droit jusqu'à la bouche
    (x0, y0), (x1, y1), (x2, y2) = (cx + 116, top + 112), (cx + 112, top + 160), (cx + 54, top + 152)
    boom = [((1 - t) ** 2 * x0 + 2 * t * (1 - t) * x1 + t * t * x2,
             (1 - t) ** 2 * y0 + 2 * t * (1 - t) * y1 + t * t * y2) for t in (i / 20 for i in range(21))]
    draw.line([(x * s, y * s) for x, y in boom], fill=dark, width=7 * s, joint="curve")
    draw.ellipse(box(cx + 34, top + 140, cx + 58, top + 164), fill=accent)
    # Visière
    draw.rounded_rectangle(box(cx - 80, top + 24, cx + 80, top + 114), radius=32 * s, fill=(28, 40, 70))
    draw.rounded_rectangle(box(cx - 66, top + 32, cx - 20, top + 40), radius=4 * s, fill=(60, 78, 120))
    # Yeux avec reflets
    for ex in (cx - 36, cx + 36):
        draw.ellipse(box(ex - 19, top + 44, ex + 19, top + 82), fill=cyan)
        draw.ellipse(box(ex - 4, top + 50, ex + 7, top + 61), fill=(255, 255, 255))
    # Sourire et joues
    draw.arc(box(cx - 28, top + 70, cx + 28, top + 104), start=20, end=160, fill=cyan, width=5 * s)
    draw.ellipse(box(cx - 90, top + 118, cx - 62, top + 136), fill=(255, 172, 184))
    draw.ellipse(box(cx + 62, top + 118, cx + 90, top + 136), fill=(255, 172, 184))

    # Nom de l'agent dans une pastille
    font = ImageFont.load_default(size=26 * s)
    label = label.replace(" - ", " · ")
    tw = draw.textlength(label, font=font) / s
    pill = Image.new("L", (w, h))
    ImageDraw.Draw(pill).rounded_rectangle(box(cx - tw / 2 - 22, 278, cx + tw / 2 + 22, 322), radius=22 * s, fill=70)
    img.paste((255, 255, 255), mask=pill)
    draw.text((cx * s, 300 * s), label, font=font, fill=(255, 255, 255), anchor="mm")
    return img.resize((WIDTH, HEIGHT), Image.LANCZOS)


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


def avatar_video(source: Path | None = None, label: str | None = None) -> Path:
    """Fichier Y4M de l'avatar : `source` (PNG, JPEG…) ou le robot par défaut, avec `label` (nom de l'agent)."""
    image = fit(Image.open(source)) if source else draw_default_avatar(label or DEFAULT_LABEL)
    digest = hashlib.sha256(image.tobytes()).hexdigest()[:12]
    return to_y4m(image, media_dir() / f"avatar-{digest}.y4m")
