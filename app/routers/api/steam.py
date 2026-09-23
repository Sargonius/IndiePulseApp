"""Steam announcement export and translation."""

import httpx
import io
import os
import time
from app.core.config import STEAM_APP_ID
from app.services.settings import load_settings
from fastapi import APIRouter
from fastapi import File
from fastapi import Form
from fastapi import UploadFile
from fastapi.responses import HTMLResponse
from loguru import logger
from app.routers.api.common import _e

router = APIRouter()

@router.post("/steam/export")
def steam_export(
    title_ru: str = Form(""),
    title_en: str = Form(""),
    body_ru: str = Form(""),
    body_en: str = Form(""),
    subtitle: str = Form(""),
    subtitle_en: str = Form(""),
    short_desc: str = Form(""),
    short_desc_en: str = Form(""),
    main_image: UploadFile = File(None),
    overlay_enabled: str = Form("1"),
    overlay_version: str = Form("VERSION 0.7.0"),
    overlay_sub: str = Form("FEATURE-COMPLETE UPDATE!"),
    ov_v_start: str = Form("#d9c8ff"),
    ov_v_end: str = Form("#7a5cf8"),
    ov_sub_start: str = Form("#ff9ac4"),
    ov_sub_end: str = Form("#ff5a9a"),
    ov_v_stroke_color: str = Form("#000000"),
    ov_v_stroke_px: int = Form(1),
    ov_sub_stroke_color: str = Form("#000000"),
    ov_sub_stroke_px: int = Form(1),
    ov_v_size: int = Form(72),
    ov_sub_size: int = Form(36),
    ov_v_weight: int = Form(700),
    ov_sub_weight: int = Form(600),
    sub_offset_y: int = Form(10),
    show_bg: str = Form("1"),
    crop_x_pct: float = Form(0.5),
    crop_y_pct: float = Form(0.5),
    crop_scale: float = Form(1.0),
):
    # Create assets folder under media/steam_export/<timestamp>
    os.makedirs('media', exist_ok=True)
    base = str(int(time.time()))
    export_dir = os.path.join('media', 'steam_export', base)
    os.makedirs(export_dir, exist_ok=True)

    # Write text files with metadata
    ru_txt = f"[h1]{title_ru}[/h1]\n[b]{subtitle[:120]}[/b]\n[quote]{short_desc[:180]}[/quote]\n{body_ru}"
    en_txt = f"[h1]{title_en}[/h1]\n[b]{subtitle_en[:120]}[/b]\n[quote]{short_desc_en[:180]}[/quote]\n{body_en}"
    with open(os.path.join(export_dir, 'steam_ru.txt'), 'w', encoding='utf-8') as f:
        f.write(ru_txt)
    with open(os.path.join(export_dir, 'steam_en.txt'), 'w', encoding='utf-8') as f:
        f.write(en_txt)

    # Save main image and validate size 800x450; auto cover-crop to 800x450
    img_path = None
    if main_image is not None:
        try:
            from PIL import Image, ImageDraw, ImageFont
            data = main_image.file.read()
            img = Image.open(io.BytesIO(data)).convert('RGB')
            target_w, target_h = 800, 450
            # compute 16:9 crop region from original, with scale and offsets
            src_w, src_h = img.size
            aspect = target_w / target_h  # 16/9
            base_crop_w = min(src_w, int(src_h * aspect))
            # clamp crop scale 0.2..1.0
            try:
                cs = max(0.2, min(1.0, float(crop_scale)))
            except Exception:
                cs = 1.0
            crop_w = max(2, int(base_crop_w * cs))
            crop_h = int(crop_w / aspect)
            max_x = max(0, src_w - crop_w)
            max_y = max(0, src_h - crop_h)
            xx = int(max(0.0, min(1.0, float(crop_x_pct))) * max_x)
            yy = int(max(0.0, min(1.0, float(crop_y_pct))) * max_y)
            img = img.crop((xx, yy, xx + crop_w, yy + crop_h))
            img = img.resize((target_w, target_h), Image.LANCZOS)

            if (overlay_enabled or '').lower() in ('1','true','on','yes') and (overlay_version.strip() or overlay_sub.strip()):
                # Draw overlay: big version text and smaller subheader centered near bottom third
                draw = ImageDraw.Draw(img)
                # Fonts
                def _try_font(name: str, size: int):
                    try:
                        return ImageFont.truetype(name, size)
                    except Exception:
                        return None
                # Prefer Staatliches if installed on system or provided in working dir
                font_large = _try_font("Staatliches.ttf", ov_v_size) or _try_font("arialbd.ttf", ov_v_size) or _try_font("arial.ttf", ov_v_size) or ImageFont.load_default()
                font_small = _try_font("Staatliches.ttf", ov_sub_size) or _try_font("arialbd.ttf", ov_sub_size) or _try_font("arial.ttf", ov_sub_size) or ImageFont.load_default()

                # Gradient text function
                def draw_gradient_text(center_x: int, y: int, text: str, font, colors=("#c7b7ff","#6e48f6"), stroke=4, stroke_color="#000000"):
                    if not text:
                        return 0
                    bbox = draw.textbbox((0,0), text, font=font, stroke_width=stroke)
                    w = bbox[2]-bbox[0]
                    h = bbox[3]-bbox[1]
                    # build gradient image
                    from PIL import Image
                    grad = Image.new('RGB', (w, h), color=0)
                    c0, c1 = colors
                    # simple vertical gradient
                    r0,g0,b0 = ImageColor.getrgb(c0)
                    r1,g1,b1 = ImageColor.getrgb(c1)
                    pixels = grad.load()
                    for yy in range(h):
                        t = yy / max(1,h-1)
                        rr = int(r0 + (r1 - r0) * t)
                        gg = int(g0 + (g1 - g0) * t)
                        bb = int(b0 + (b1 - b0) * t)
                        for xx in range(w):
                            pixels[xx,yy] = (rr,gg,bb)
                    # stroke layer
                    if stroke and stroke > 0:
                        stroke_layer = Image.new('RGBA', (w, h), (0,0,0,0))
                        sd = ImageDraw.Draw(stroke_layer)
                        try:
                            sc = ImageColor.getrgb(stroke_color or '#000000')
                        except Exception:
                            sc = (0,0,0)
                        sd.text((0,0), text, font=font, fill=sc+(255,), stroke_width=stroke, stroke_fill=sc)
                        img.paste(stroke_layer, (center_x - w//2, y), stroke_layer)
                    # text mask
                    mask = Image.new('L', (w, h), 0)
                    md = ImageDraw.Draw(mask)
                    md.text((0,0), text, font=font, fill=255, stroke_width=stroke, stroke_fill=(0))
                    # position
                    x = center_x - w//2
                    img.paste(grad, (x, y), mask)
                    return h

                from PIL import ImageColor
                cx = target_w//2
                # Drop shadow backdrop
                shadow = Image.new('RGBA', (target_w, target_h), (0,0,0,0))
                sd = ImageDraw.Draw(shadow)
                # Draw shadow rectangles for readability
                sd.rectangle([0, int(target_h*0.6), target_w, target_h], fill=(0,0,0,90))
                img = Image.alpha_composite(img.convert('RGBA'), shadow).convert('RGB')
                draw = ImageDraw.Draw(img)

                title_y = 290
                # Optional dark band
                if (show_bg or '1').lower() in ('1','true','on','yes'):
                    draw.rectangle([0, max(0, title_y-20), target_w, target_h], fill=(0,0,0,90))
                # Heavier weight -> slightly thicker stroke
                v_weight_extra = 1 if (ov_v_weight or 700) >= 600 else 0
                if (ov_v_weight or 700) >= 800:
                    v_weight_extra += 1
                v_stroke = max(0, int(ov_v_stroke_px or 1) + v_weight_extra)
                h_big = draw_gradient_text(cx, title_y, overlay_version.strip(), font_large, colors=(ov_v_start or "#d9c8ff", ov_v_end or "#7a5cf8"), stroke=v_stroke, stroke_color=ov_v_stroke_color or "#000000")
                y_small = title_y + h_big + max(0, int(sub_offset_y or 10))
                # Smaller line with gradient as well
                sub_text = overlay_sub.strip()
                if sub_text:
                    s_weight_extra = 1 if (ov_sub_weight or 600) >= 600 else 0
                    if (ov_sub_weight or 600) >= 800:
                        s_weight_extra += 1
                    s_stroke = max(0, int(ov_sub_stroke_px or 1) + s_weight_extra)
                    draw_gradient_text(cx, y_small, sub_text, font_small, colors=(ov_sub_start or '#ff9ac4', ov_sub_end or '#ff5a9a'), stroke=s_stroke, stroke_color=ov_sub_stroke_color or "#000000")

            img_path = os.path.join(export_dir, 'main_800x450.jpg')
            img.save(img_path, format='JPEG', quality=92)
        except Exception as exc:
            return HTMLResponse(f"<span style='color:red'>Image processing failed: {_e(str(exc))}</span>", status_code=400)

    # Respond with folder details and a helper link
    return {
        "ok": True,
        "export_dir": export_dir.replace('\\','/'),
        "steam_create_url": f"https://steamcommunity.com/games/{STEAM_APP_ID}/partnerevents/create"
    }


@router.get("/steam/open_dir")
def steam_open_dir(path: str):
    # Open directory in explorer (Windows only)
    try:
        p = (path or '').strip()
        if not p:
            return HTMLResponse("<span style='color:red'>Empty path</span>", status_code=400)
        full = os.path.abspath(p)
        if not os.path.isdir(full):
            return HTMLResponse("<span style='color:red'>Not a directory</span>", status_code=400)
        if os.name == 'nt':
            import subprocess
            subprocess.Popen(["explorer", full])
            return {"ok": True}
        else:
            return HTMLResponse("<span style='color:orange'>Open folder supported on Windows only</span>")
    except Exception as exc:
        return HTMLResponse(f"<span style='color:red'>Open failed: {_e(str(exc))}</span>", status_code=500)


@router.post("/steam/translate_text")
def steam_translate_text(
    title_ru: str = Form(""),
    subtitle: str = Form(""),
    short_desc: str = Form(""),
    body_ru: str = Form(""),
):
    """Translate Steam RU fields to EN in one go. Uses OpenAI first, then falls back to public translators."""
    src = {
        "title": (title_ru or "").strip(),
        "subtitle": (subtitle or "").strip(),
        "short_desc": (short_desc or "").strip(),
        "body": (body_ru or "").strip(),
    }
    provider = ""
    out = {"title_en": "", "subtitle_en": "", "short_desc_en": "", "body_en": ""}
    try:
        cfg = load_settings()
        api_key = (cfg.get("openai_api_key") or "").strip()
        model = (cfg.get("openai_model") or "gpt-4o-mini").strip()
        if api_key and model:
            headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
            system = (
                "You are a professional localization translator for Steam Partner Events. "
                "Translate Russian to natural, idiomatic English. Preserve Steam markup tags (e.g., [h1], [b], [url], [list]) exactly as-is in the 'body'. "
                "Return ONLY a compact JSON object with keys: title_en, subtitle_en, short_desc_en, body_en."
            )
            user = (
                f"title: {src['title']}\n"
                f"subtitle: {src['subtitle']}\n"
                f"short_desc: {src['short_desc']}\n"
                f"body: {src['body']}\n"
                "Translate all fields to English."
            )
            payload = {"model": model, "temperature": 0.2, "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ]}
            with httpx.Client(timeout=25) as client:
                r = client.post("https://api.openai.com/v1/chat/completions", headers=headers, json=payload)
                if r.status_code < 400:
                    data = r.json()
                    content = (((data.get("choices") or [{}])[0].get("message") or {}).get("content") or "").strip()
                    import json as _json
                    try:
                        parsed = _json.loads(content)
                        out.update({
                            "title_en": (parsed.get("title_en") or "").strip(),
                            "subtitle_en": (parsed.get("subtitle_en") or "").strip(),
                            "short_desc_en": (parsed.get("short_desc_en") or "").strip(),
                            "body_en": (parsed.get("body_en") or "").strip(),
                        })
                        provider = f"openai:{model}"
                    except Exception as pe:
                        try:
                            logger.warning("steam_translate parse_json_failed raw=%s", content[:400])
                        except Exception:
                            pass
                else:
                    try:
                        logger.warning("openai_http_error steam status=%s model=%s body=%s", r.status_code, model, r.text[:400])
                    except Exception:
                        pass
    except Exception as exc:
        try:
            logger.warning("steam_translate openai_error: %s", str(exc))
        except Exception:
            pass
    # Fallbacks if any field missing
    def contains_cyrillic(s: str) -> bool:
        return any('а' <= ch <= 'я' or 'А' <= ch <= 'Я' for ch in s)
    if not all(out.values()) or any(contains_cyrillic(v) for v in out.values()):
        try:
            from deep_translator import GoogleTranslator, MyMemoryTranslator, LibreTranslator
            def tr(s: str) -> str:
                if not s: return ""
                for host in ('https://libretranslate.de','https://translate.astian.org'):
                    try:
                        v = LibreTranslator(source='ru', target='en', host=host).translate(s) or ""
                        if v and not contains_cyrillic(v):
                            return v
                    except Exception: pass
                try:
                    v = GoogleTranslator(source='ru', target='en').translate(s) or ""
                    if v and not contains_cyrillic(v):
                        return v
                except Exception: pass
                try:
                    v = MyMemoryTranslator(source='ru', target='en').translate(s) or ""
                    if v and not contains_cyrillic(v):
                        return v
                except Exception: pass
                return s
            if not out["title_en"] or contains_cyrillic(out["title_en"]): out["title_en"] = tr(src['title'])
            if not out["subtitle_en"] or contains_cyrillic(out["subtitle_en"]): out["subtitle_en"] = tr(src['subtitle'])
            if not out["short_desc_en"] or contains_cyrillic(out["short_desc_en"]): out["short_desc_en"] = tr(src['short_desc'])
            if not out["body_en"] or contains_cyrillic(out["body_en"]): out["body_en"] = tr(src['body'])
            if not provider:
                provider = "fallback"
        except Exception as exc:
            provider = provider or "fallback"
    try:
        logger.info(f"steam_translate provider={provider} tlen={len(src['title'])} blen={len(src['body'])}")
    except Exception:
        pass
    out["provider"] = provider or ""
    return out
