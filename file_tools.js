#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""file_maker_worker.py — إنشاء PDF/TXT محلياً للبوت.
الاستخدام:
  python3 file_maker_worker.py pdf <title> <text> <output.pdf>
  python3 file_maker_worker.py txt <title> <text> <output.txt>
يعتمد على Pillow لإنشاء PDF بصورة صفحات، وهذا يقلل مشاكل تقطيع/تشويش العربية في Termux.
"""
import sys, os, json, textwrap, re
from pathlib import Path

try:
    from PIL import Image, ImageDraw, ImageFont
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


def clean_text(s):
    s = str(s or '').replace('\r\n', '\n').replace('\r', '\n')
    s = re.sub(r'\n{4,}', '\n\n\n', s)
    return s.strip()


def find_font():
    candidates = [
        os.environ.get('PDF_FONT_PATH'),
        '/system/fonts/NotoNaskhArabic-Regular.ttf',
        '/system/fonts/NotoSansArabic-Regular.ttf',
        '/system/fonts/DroidSansArabic.ttf',
        '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
        '/usr/share/fonts/dejavu/DejaVuSans.ttf',
        '/data/data/com.termux/files/usr/share/fonts/TTF/DejaVuSans.ttf',
    ]
    for p in candidates:
        if p and os.path.exists(p):
            return p
    return None


def shape_line(line):
    line = str(line or '')
    if RTL_AVAILABLE and re.search(r'[\u0600-\u06FF]', line):
        try:
            return get_display(arabic_reshaper.reshape(line))
        except Exception:
            return line
    return line


def wrap_text(text, max_chars=78):
    lines = []
    for para in clean_text(text).split('\n'):
        para = para.strip()
        if not para:
            lines.append('')
            continue
        # المحافظة على النقاط/العناوين، وتقسيم لطيف حسب الكلمات
        wrapped = textwrap.wrap(para, width=max_chars, break_long_words=False, replace_whitespace=False)
        if not wrapped:
            lines.append('')
        else:
            lines.extend(wrapped)
    return lines


def create_txt(title, text, output):
    title = clean_text(title) or 'ملف نصي'
    text = clean_text(text)
    content = f"{title}\n{'=' * min(60, max(10, len(title)))}\n\n{text}\n"
    Path(output).parent.mkdir(parents=True, exist_ok=True)
    with open(output, 'w', encoding='utf-8') as f:
        f.write(content)
    send({'type': 'done', 'output': output, 'format': 'txt'})


def create_pdf(title, text, output):
    if not PIL_AVAILABLE:
        raise RuntimeError('Pillow غير مثبتة: pip install pillow')
    title = clean_text(title) or 'ملف PDF'
    text = clean_text(text)
    font_path = find_font()
    if font_path:
        title_font = ImageFont.truetype(font_path, 34)
        body_font = ImageFont.truetype(font_path, 24)
        small_font = ImageFont.truetype(font_path, 18)
    else:
        title_font = body_font = small_font = ImageFont.load_default()

    W, H = 1240, 1754  # A4 تقريباً على 150dpi
    margin = 90
    line_h = 38
    pages = []
    lines = wrap_text(text, max_chars=76)
    idx = 0
    page_no = 1
    while idx < len(lines) or page_no == 1:
        img = Image.new('RGB', (W, H), 'white')
        draw = ImageDraw.Draw(img)
        y = margin
        if page_no == 1:
            draw.text((margin, y), shape_line(title), fill='black', font=title_font)
            y += 62
            draw.line((margin, y, W-margin, y), fill=(80,80,80), width=2)
            y += 38
        while idx < len(lines) and y < H - margin - 60:
            line = lines[idx]
            if line == '':
                y += line_h // 2
            else:
                draw.text((margin, y), shape_line(line), fill='black', font=body_font)
                y += line_h
            idx += 1
        footer = f"MedTerm — صفحة {page_no}"
        draw.text((margin, H - margin + 15), shape_line(footer), fill=(100,100,100), font=small_font)
        pages.append(img)
        page_no += 1
        if page_no > 80:  # حماية من ملفات ضخمة جداً
            break
    Path(output).parent.mkdir(parents=True, exist_ok=True)
    first, rest = pages[0], pages[1:]
    first.save(output, 'PDF', resolution=150.0, save_all=True, append_images=rest)
    send({'type': 'done', 'output': output, 'format': 'pdf', 'pages': len(pages)})


def main():
    if len(sys.argv) < 5:
        send({'type':'error','message':'usage: file_maker_worker.py <pdf|txt> <title> <text> <output>','fatal':True})
        sys.exit(1)
    fmt, title, text, output = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
    try:
        if fmt == 'pdf':
            create_pdf(title, text, output)
        elif fmt == 'txt':
            create_txt(title, text, output)
        else:
            raise RuntimeError('صيغة غير مدعومة')
    except Exception as e:
        send({'type':'error','message':str(e),'fatal':True})
        sys.exit(2)

if __name__ == '__main__':
    main()
