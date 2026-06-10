#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
image_ocr_worker.py — Worker لاستخراج النص من الصور
يستخدم: EasyOCR (أساسي) + Tesseract (fallback) + OpenCV (تحضير)
يُستدعى من Node.js عبر child_process.spawn

الاستخدام:
  python3 image_ocr_worker.py <image_path> [lang=ara+eng]

الإخراج (JSON على stdout):
  {"type": "result", "text": "...", "engine": "easyocr|tesseract|none", "confidence": 0.9}
  {"type": "error",  "message": "...", "fatal": true/false}
"""

import sys
import os
import json
import gc
import io

# ============================================================
# استيراد المكتبات — كل واحدة مستقلة
# ============================================================
try:
    import numpy as np
    NUMPY_AVAILABLE = True
except ImportError:
    NUMPY_AVAILABLE = False

try:
    import cv2
    OPENCV_AVAILABLE = NUMPY_AVAILABLE
except ImportError:
    OPENCV_AVAILABLE = False

try:
    from PIL import Image, ImageFilter, ImageEnhance, ImageOps
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False

try:
    import pytesseract
    TESSERACT_AVAILABLE = PIL_AVAILABLE
except ImportError:
    TESSERACT_AVAILABLE = False

try:
    import easyocr
    EASYOCR_AVAILABLE = True
except ImportError:
    EASYOCR_AVAILABLE = False


# ============================================================
# EasyOCR Singleton — يُحمَّل مرة واحدة
# ============================================================
_reader = None
_reader_failed = False

def get_reader(lang_list=None):
    global _reader, _reader_failed
    if _reader_failed or not EASYOCR_AVAILABLE:
        return None
    if _reader is not None:
        return _reader
    try:
        langs = lang_list or ['ar', 'en']
        model_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.easyocr_models')
        os.makedirs(model_dir, exist_ok=True)
        # إسكات logs التحميل
        import sys as _sys, io as _io
        _old = _sys.stderr
        _sys.stderr = _io.StringIO()
        try:
            _reader = easyocr.Reader(langs, gpu=False, verbose=False,
                                     model_storage_directory=model_dir)
        finally:
            _sys.stderr = _old
        return _reader
    except Exception as e:
        _reader_failed = True
        return None


# ============================================================
# Pipeline تحضير الصورة
# ============================================================
def preprocess(img_bytes, for_easyocr=False):
    """
    Pipeline متقدم لتحضير الصورة قبل OCR.
    for_easyocr=True  → numpy RGB
    for_easyocr=False → PIL grayscale محسّن
    """
    try:
        if OPENCV_AVAILABLE:
            nparr = np.frombuffer(img_bytes, np.uint8)
            img   = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
            if img is None:
                raise ValueError("cv2 decode failed")

            # تقليل الحجم الزائد مع الحفاظ على حدة النص
            h, w = img.shape[:2]
            max_dim = 2000
            min_dim = 800  # تكبير الصور الصغيرة جداً لتحسين OCR
            if max(h, w) > max_dim:
                scale = max_dim / max(h, w)
                img = cv2.resize(img, (int(w*scale), int(h*scale)),
                                 interpolation=cv2.INTER_AREA)
            elif max(h, w) < min_dim:
                scale = min_dim / max(h, w)
                img = cv2.resize(img, (int(w*scale), int(h*scale)),
                                 interpolation=cv2.INTER_CUBIC)

            if for_easyocr:
                result = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
                del img
                return result

            # ── Pipeline Tesseract ──
            gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
            del img

            # إزالة الضوضاء
            blur = cv2.GaussianBlur(gray, (3, 3), 0)
            del gray

            # تحسين التباين
            clahe   = cv2.createCLAHE(clipLimit=2.5, tileGridSize=(8, 8))
            enhanced = clahe.apply(blur)
            del blur

            # كشف الخلفية الداكنة وعكسها
            if np.mean(enhanced) < 120:
                enhanced = cv2.bitwise_not(enhanced)

            # Thresholding
            thresh = cv2.adaptiveThreshold(
                enhanced, 255,
                cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                cv2.THRESH_BINARY, 15, 8
            )
            del enhanced

            # تنظيف morphological
            kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (2, 2))
            clean  = cv2.morphologyEx(thresh, cv2.MORPH_OPEN, kernel)
            del thresh

            pil_img = Image.fromarray(clean)
            del clean
            gc.collect()
            return pil_img

        elif PIL_AVAILABLE:
            # fallback بدون OpenCV
            pil_img = Image.open(io.BytesIO(img_bytes))
            # تحويل RGBA/P → RGB
            if pil_img.mode in ('RGBA', 'P', 'LA'):
                pil_img = pil_img.convert('RGB')
            # تقليل/تكبير الحجم
            max_dim, min_dim = 2000, 800
            long = max(pil_img.size)
            if long > max_dim:
                ratio = max_dim / long
                pil_img = pil_img.resize(
                    (int(pil_img.width*ratio), int(pil_img.height*ratio)),
                    Image.LANCZOS
                )
            elif long < min_dim:
                ratio = min_dim / long
                pil_img = pil_img.resize(
                    (int(pil_img.width*ratio), int(pil_img.height*ratio)),
                    Image.BICUBIC
                )
            if for_easyocr:
                if NUMPY_AVAILABLE:
                    return np.array(pil_img.convert('RGB'))
                return None
            # Grayscale + تحسين
            pil_img = ImageOps.autocontrast(pil_img.convert('L'))
            pil_img = ImageEnhance.Contrast(pil_img).enhance(2.5)
            pil_img = pil_img.filter(ImageFilter.SHARPEN)
            return pil_img

    except Exception:
        pass

    # آخر محاولة — إرجاع الصورة كما هي
    try:
        if PIL_AVAILABLE:
            img = Image.open(io.BytesIO(img_bytes))
            if for_easyocr:
                if NUMPY_AVAILABLE:
                    return np.array(img.convert('RGB'))
                return None
            return img.convert('L')
    except:
        pass
    return None


# ============================================================
# تنظيف النص الناتج
# ============================================================
def clean_text(text):
    """تنظيف النص: إزالة السطور الفارغة الزائدة والمسافات الغريبة"""
    if not text:
        return ""
    lines = text.splitlines()
    cleaned = []
    prev_empty = False
    for line in lines:
        stripped = line.strip()
        if not stripped:
            if not prev_empty:
                cleaned.append("")
            prev_empty = True
        else:
            cleaned.append(stripped)
            prev_empty = False
    return "\n".join(cleaned).strip()


# ============================================================
# OCR بـ EasyOCR
# ============================================================
def ocr_easyocr(img_bytes):
    reader = get_reader(['ar', 'en'])
    if reader is None:
        return None, 0.0
    try:
        img_input = preprocess(img_bytes, for_easyocr=True)
        if img_input is None:
            return None, 0.0

        results = reader.readtext(img_input, detail=1, paragraph=False)
        del img_input
        gc.collect()

        if not results:
            return "", 0.0

        # فرز النتائج بالترتيب (من أعلى لأسفل، يمين لشمال للعربية)
        results.sort(key=lambda r: (r[0][0][1], -r[0][0][0]))  # y أولاً ثم x معكوس

        lines, confidences = [], []
        for bbox, text, conf in results:
            if text and text.strip() and conf > 0.1:
                lines.append(text.strip())
                confidences.append(conf)

        full_text = "\n".join(lines)
        avg_conf  = sum(confidences) / len(confidences) if confidences else 0.0
        return clean_text(full_text), avg_conf

    except Exception as e:
        return None, 0.0


# ============================================================
# OCR بـ Tesseract
# ============================================================
def ocr_tesseract(img_bytes, lang='ara+eng'):
    if not TESSERACT_AVAILABLE:
        return None, 0.0
    try:
        pil_img = preprocess(img_bytes, for_easyocr=False)
        if pil_img is None:
            return None, 0.0

        # PSM 6: كتلة نصية موحدة — الأفضل لمعظم الصور
        config = f'--oem 3 --psm 6 -l {lang}'
        data   = pytesseract.image_to_data(pil_img, config=config,
                                           output_type=pytesseract.Output.DICT)
        del pil_img
        gc.collect()

        words, confs = [], []
        for i, word in enumerate(data['text']):
            c = int(data['conf'][i])
            if word.strip() and c > 30:  # تجاهل الكلمات ذات الثقة المنخفضة
                words.append(word.strip())
                confs.append(c / 100.0)

        if not words:
            return "", 0.0

        text     = " ".join(words)
        avg_conf = sum(confs) / len(confs) if confs else 0.0
        return clean_text(text), avg_conf

    except Exception as e:
        return None, 0.0


# ============================================================
# OCR هجين — الدالة الرئيسية
# ============================================================
def run_hybrid_ocr(img_bytes, lang='ara+eng'):
    """
    يجرّب EasyOCR ثم Tesseract ويعيد الأفضل.
    يُعيد: (text, engine_name, confidence)
    """
    easy_text, easy_conf = ocr_easyocr(img_bytes)
    tess_text, tess_conf = None, 0.0

    # EasyOCR أعطى نتيجة وافية
    if easy_text and len(easy_text.strip()) > 15 and easy_conf > 0.4:
        return easy_text, "easyocr", easy_conf

    # Tesseract كـ fallback
    tess_text, tess_conf = ocr_tesseract(img_bytes, lang)

    # اختر الأفضل بناءً على الطول والثقة
    if easy_text and tess_text:
        # رجّح بين الاثنين: الثقة × الطول
        easy_score = easy_conf * len(easy_text)
        tess_score = tess_conf * len(tess_text)
        if easy_score >= tess_score:
            return easy_text, "easyocr", easy_conf
        else:
            return tess_text, "tesseract", tess_conf

    if easy_text and easy_text.strip():
        return easy_text, "easyocr", easy_conf
    if tess_text and tess_text.strip():
        return tess_text, "tesseract", tess_conf

    return "", "none", 0.0


# ============================================================
# تنسيق النتيجة للمستخدم
# ============================================================
def format_result(text, engine, conf, image_path):
    """تنسيق النص المستخرج ليُرسَل للمستخدم عبر WhatsApp"""
    if not text or not text.strip():
        return None  # لا يوجد نص

    engine_emoji = {
        "easyocr":   "🤖",
        "tesseract": "🔍",
        "none":      "❓"
    }.get(engine, "🔍")

    conf_pct = int(conf * 100)
    result   = f"📄 *النص المستخرج من الصورة:*\n"
    result  += f"─────────────────\n"
    result  += text
    result  += f"\n─────────────────\n"
    result  += f"_{engine_emoji} المحرك: {engine} | الدقة: {conf_pct}%_"
    return result


# ============================================================
# نقطة الدخول الرئيسية
# ============================================================
def main():
    if len(sys.argv) < 2:
        print(json.dumps({
            "type": "error",
            "message": "الاستخدام: python3 image_ocr_worker.py <image_path> [lang]",
            "fatal": True
        }, ensure_ascii=False), flush=True)
        sys.exit(1)

    image_path = sys.argv[1]
    lang       = sys.argv[2] if len(sys.argv) > 2 else 'ara+eng'

    # فحص وجود الملف
    if not os.path.exists(image_path):
        print(json.dumps({
            "type": "error",
            "message": f"الملف غير موجود: {image_path}",
            "fatal": True
        }, ensure_ascii=False), flush=True)
        sys.exit(1)

    # فحص وجود أي محرك OCR
    if not EASYOCR_AVAILABLE and not TESSERACT_AVAILABLE:
        print(json.dumps({
            "type": "error",
            "message": "لا يوجد محرك OCR — ثبّت EasyOCR أو Tesseract",
            "fatal": True
        }, ensure_ascii=False), flush=True)
        sys.exit(1)

    # قراءة الصورة
    try:
        with open(image_path, 'rb') as f:
            img_bytes = f.read()
    except Exception as e:
        print(json.dumps({
            "type": "error",
            "message": f"فشل قراءة الصورة: {e}",
            "fatal": True
        }, ensure_ascii=False), flush=True)
        sys.exit(1)

    # تشغيل OCR
    text, engine, conf = run_hybrid_ocr(img_bytes, lang)
    del img_bytes
    gc.collect()

    # إرسال النتيجة
    print(json.dumps({
        "type":       "result",
        "text":       text,
        "engine":     engine,
        "confidence": round(conf, 3),
        "has_text":   bool(text and text.strip())
    }, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
