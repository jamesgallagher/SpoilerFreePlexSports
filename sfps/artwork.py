"""Artwork generation and badge compositing (Pillow).

Two jobs (design.md §3.4/§3.5):
- Placeholder cards when no artwork can be downloaded ("Unknown Event", or a
  neutral matchup card). Spoiler-free by construction.
- Variant badges ("HIGHLIGHTS" / "MINI") composited bottom-right onto thumbs.
  User-replaceable assets in <config>/badges/<variant>.png take precedence;
  a generated text pill is the fallback.
"""

from __future__ import annotations

import logging
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from sfps.config import Config

log = logging.getLogger(__name__)

CARD_SIZE = (1280, 720)
_BG = (24, 28, 38)  # dark slate
_FG = (235, 238, 245)
_ACCENT = (120, 130, 150)

BADGE_LABELS = {"highlights": "HIGHLIGHTS", "mini": "MINI"}


def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    return ImageFont.load_default(size=size)


def _center_text(draw: ImageDraw.ImageDraw, y: int, text: str, size: int, fill) -> None:
    font = _font(size)
    width = draw.textlength(text, font=font)
    draw.text(((CARD_SIZE[0] - width) / 2, y), text, font=font, fill=fill)


def generate_card(dest: Path, title: str, subtitle: str = "", footer: str = "") -> Path:
    """Render a neutral dark card (e.g. 'Unknown Event', or 'A vs B')."""
    image = Image.new("RGB", CARD_SIZE, _BG)
    draw = ImageDraw.Draw(image)
    _center_text(draw, 280, title[:60], 64, _FG)
    if subtitle:
        _center_text(draw, 380, subtitle[:80], 36, _ACCENT)
    if footer:
        _center_text(draw, 620, footer[:80], 28, _ACCENT)
    dest.parent.mkdir(parents=True, exist_ok=True)
    image.save(dest, "JPEG", quality=90)
    log.info("artwork: generated card '%s' -> %s", title, dest.name)
    return dest


def _generated_badge(label: str, width: int) -> Image.Image:
    """A text pill: rounded dark box with white label, sized relative to thumb."""
    height = max(28, int(width * 0.30))
    font = _font(int(height * 0.55))
    pill = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(pill)
    draw.rounded_rectangle(
        [(0, 0), (width - 1, height - 1)], radius=height // 3, fill=(10, 10, 12, 215)
    )
    text_w = draw.textlength(label, font=font)
    draw.text(((width - text_w) / 2, height * 0.16), label, font=font, fill=(255, 255, 255, 255))
    return pill


def apply_badge(thumb_path: Path, variant: str, config: Config) -> bool:
    """Composite the variant badge bottom-right onto an existing thumb, in place."""
    label = BADGE_LABELS.get(variant)
    if label is None:
        return False

    try:
        image = Image.open(thumb_path).convert("RGBA")
    except OSError as exc:
        log.warning("artwork: cannot open thumb for badging (%s)", exc)
        return False

    margin = int(image.width * 0.03)
    custom = config.config_dir / "badges" / f"{variant}.png"
    if custom.is_file():
        try:
            badge = Image.open(custom).convert("RGBA")
            target_w = int(image.width * 0.22)
            ratio = target_w / badge.width
            badge = badge.resize((target_w, max(1, int(badge.height * ratio))))
            log.info("artwork: using custom badge %s", custom)
        except OSError as exc:
            log.warning("artwork: bad custom badge %s (%s); using generated", custom, exc)
            badge = _generated_badge(label, int(image.width * 0.22))
    else:
        badge = _generated_badge(label, int(image.width * 0.22))

    position = (image.width - badge.width - margin, image.height - badge.height - margin)
    image.alpha_composite(badge, position)
    image.convert("RGB").save(thumb_path, "JPEG", quality=90)
    log.info("artwork: applied %s badge to %s", variant, thumb_path.name)
    return True
