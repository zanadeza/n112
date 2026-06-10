#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""image_tools_worker.py — تحسين الصور وتوليد صورة بسيطة محلياً عبر Pillow."""
import sys, os, json, re, textwrap, random
from pathlib import Path
try:
    from PIL import Image, ImageFilter, ImageEnhance, ImageDraw, ImageFont, ImageOps
    PIL_AVAILABLE = True
except Exception:
    PIL_AVAILABLE = False

try:
    import arabic_reshaper
    from bidi.algorithm import get_display
    RTL_AVAILABLE = True
except Exception:
    RTL_AVAILABLE = False


def send(obj):
    print(json.dumps(obj, ensure_ascii=False), flush=True)

def font(size=32):
    for p in [os.environ.get('PDF_FONT_PATH'), '/system/fonts/NotoNaskhArabic-Regular.ttf', '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf', '/data/data/com.termux/files/usr/share/fonts/TTF/DejaVuSans.ttf']:
        if p and os.path.exists(p):
            return ImageFont.truetype(p, size)
    return ImageFont.load_default()

def shape(s):
    s = str(s or '')
    if RTL_AVAILABLE and re.search(r'[\u0600-\u06FF]', s):
        try: return get_display(arabic_reshaper.reshape(s))
        except Exception: return s
    return s


def enhance(input_path, output_path):
    if not PIL_AVAILABLE: raise RuntimeError('Pillow غير مثبتة')
    img = Image.open(input_path)
    img = ImageOps.exif_transpose(img).convert('RGB')
    # تحسين محلي محافظ: تباين + حدة + إضاءة خفيفة + denoise بسيط
    img = ImageEnhance.Contrast(img).enhance(1.18)
    img = ImageEnhance.Sharpness(img).enhance(1.45)
    img = ImageEnhance.Brightness(img).enhance(1.04)
    img = img.filter(ImageFilter.MedianFilter(size=3))
    img = ImageEnhance.Sharpness(img).enhance(1.25)
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    img.save(output_path, quality=94, optimize=True)
    send({'type':'done','output':output_path,'mode':'enhance'})


def generate(prompt, output_path):
    if not PIL_AVAILABLE: raise RuntimeError('Pillow غير مثبتة')
    prompt = str(prompt or 'صورة').strip()[:400]
    W, H = 1024, 1024
    # توليد محلي بسيط: خلفية متدرجة + أشكال + نص الطلب. ليس نموذج AI، لكنه يعمل بلا إنترنت.
    img = Image.new('RGB', (W,H), 'white')
    px = img.load()
    seed = sum(ord(c) for c in prompt) or 1
    random.seed(seed)
    c1 = [random.randint(40,150), random.randint(80,180), random.randint(120,230)]
    c2 = [random.randint(160,240), random.randint(160,240), random.randint(160,240)]
    for y in range(H):
        t = y / max(1, H-1)
        col = tuple(int(c1[i]*(1-t)+c2[i]*t) for i in range(3))
        for x in range(W): px[x,y] = col
    draw = ImageDraw.Draw(img, 'RGBA')
    for _ in range(18):
        x = random.randint(-100, W-100); y = random.randint(-100, H-100)
        r = random.randint(80, 260)
        fill = (255,255,255, random.randint(25,80))
        draw.ellipse((x,y,x+r,y+r), fill=fill)
    draw.rectangle((70, 690, 954, 925), fill=(255,255,255,190), outline=(255,255,255,230), width=3)
    f_title = font(42); f_body = font(30); f_small = font(22)
    draw.text((90, 715), shape('صورة مولدة محلياً'), fill=(20,20,20,255), font=f_title)
    lines = textwrap.wrap(prompt, width=38, break_long_words=False)[:4]
    yy = 780
    for ln in lines:
        draw.text((90, yy), shape(ln), fill=(30,30,30,255), font=f_body)
        yy += 40
    draw.text((90, 900), shape('MedTerm Local Image'), fill=(80,80,80,255), font=f_small)
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    img.save(output_path, quality=94)
    send({'type':'done','output':output_path,'mode':'generate'})


def main():
    if len(sys.argv) < 4:
        send({'type':'error','message':'usage: image_tools_worker.py enhance <input> <output> OR generate <prompt> <output>','fatal':True}); sys.exit(1)
    try:
        mode = sys.argv[1]
        if mode == 'enhance': enhance(sys.argv[2], sys.argv[3])
        elif mode == 'generate': generate(sys.argv[2], sys.argv[3])
        else: raise RuntimeError('وضع غير مدعوم')
    except Exception as e:
        send({'type':'error','message':str(e),'fatal':True}); sys.exit(2)

if __name__ == '__main__': main()
