#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pdf_worker.py — Python Worker لاستخراج محتوى PDF
يستخدم: PyMuPDF + pdfplumber + EasyOCR (أساسي) + Tesseract (fallback) + OpenCV
يُستدعى من Node.js عبر child_process.spawn

نظام OCR هجين:
  - EasyOCR  : دقة عالية للعربية والإنجليزية المختلطة، يُحمَّل مرة واحدة (singleton)
  - Tesseract: خفيف وسريع، يُستخدم fallback أو للصور البسيطة
  - تحضير الصورة: pipeline متقدم (OpenCV) يرفع دقة OCR بشكل كبير
"""

import sys
import os
import json
import gc
import traceback
import io
import threading

# ============================================================
# تحميل المكتبات مع التعامل مع حالة عدم التثبيت
# ============================================================
try:
    import fitz  # PyMuPDF
    FITZ_AVAILABLE = True
except ImportError:
    FITZ_AVAILABLE = False

try:
    import pdfplumber
    PLUMBER_AVAILABLE = True
except ImportError:
    PLUMBER_AVAILABLE = False

# PIL مستقلة — قد تكون موجودة بدون tesseract
try:
    from PIL import Image, ImageFilter, ImageEnhance
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False

try:
    import pytesseract
    TESSERACT_AVAILABLE = PIL_AVAILABLE  # tesseract يحتاج PIL ليعمل
except ImportError:
    TESSERACT_AVAILABLE = False

# numpy مستقلة — قد يكون cv2 موجوداً بدونها أو العكس
try:
    import numpy as np
    NUMPY_AVAILABLE = True
except ImportError:
    NUMPY_AVAILABLE = False

try:
    import cv2
    OPENCV_AVAILABLE = NUMPY_AVAILABLE  # OpenCV يحتاج numpy
except ImportError:
    OPENCV_AVAILABLE = False

# ============================================================
# EasyOCR Singleton — يُحمَّل مرة واحدة فقط، يبقى في الذاكرة
# ============================================================
try:
    import easyocr
    EASYOCR_AVAILABLE = True
except ImportError:
    EASYOCR_AVAILABLE = False

_easyocr_reader = None          # الـ singleton
_easyocr_lock   = threading.Lock()  # thread-safe initialization
_easyocr_failed = False         # إذا فشل التحميل مرة، لا نحاول ثانية

def get_easyocr_reader():
    """
    يُعيد EasyOCR Reader محمّل مرة واحدة فقط.
    Thread-safe: إذا استدعته خيوط متعددة معاً، ينتظر الأول وينتهي الثاني فوراً.
    يُعيد None إذا كان EasyOCR غير مثبت أو فشل التحميل.
    """
    global _easyocr_reader, _easyocr_failed

    if not EASYOCR_AVAILABLE or _easyocr_failed:
        return None
    if _easyocr_reader is not None:
        return _easyocr_reader

    with _easyocr_lock:
        # double-check بعد الحصول على القفل
        if _easyocr_reader is not None:
            return _easyocr_reader
        if _easyocr_failed:
            return None
        try:
            import sys as _sys, io as _io
            # إسكات output التحميل (EasyOCR يطبع logs كثيرة)
            _old_stderr = _sys.stderr
            _sys.stderr  = _io.StringIO()
            try:
                # gpu=False إلزامي على Termux (لا GPU)
                # verbose=False لمنع logs التحميل من تلويث stdout
                _easyocr_reader = easyocr.Reader(
                    ['ar', 'en'],
                    gpu=False,
                    verbose=False,
                    model_storage_directory=os.path.join(
                        os.path.dirname(os.path.abspath(__file__)), '.easyocr_models'
                    )
                )
            finally:
                _sys.stderr = _old_stderr
            print(json.dumps({"type": "info", "message": "EasyOCR محمّل بنجاح"}), flush=True)
            return _easyocr_reader
        except Exception as e:
            _easyocr_failed = True
            print(json.dumps({"type": "info", "message": f"EasyOCR لم يُحمَّل: {str(e)[:80]} — سيُستخدم Tesseract"}), flush=True)
            return None


# ============================================================
# إرسال التقدم إلى Node.js عبر stdout
# ============================================================
def send_progress(current_page, total_pages, message=""):
    """إرسال تقدم المعالجة إلى Node.js"""
    data = {
        "type": "progress",
        "current": current_page,
        "total": total_pages,
        "message": message
    }
    print(json.dumps(data, ensure_ascii=False), flush=True)


def send_error(message, fatal=False):
    """إرسال خطأ إلى Node.js"""
    data = {
        "type": "error",
        "message": message,
        "fatal": fatal
    }
    print(json.dumps(data, ensure_ascii=False), flush=True)


def send_done(output_path, total_pages, stats):
    """إرسال إشعار الانتهاء"""
    data = {
        "type": "done",
        "output_path": output_path,
        "total_pages": total_pages,
        "stats": stats
    }
    print(json.dumps(data, ensure_ascii=False), flush=True)


# ============================================================
# تحسين الصورة قبل OCR — pipeline متقدم
# ============================================================
def preprocess_image_for_ocr(img_bytes, for_easyocr=False):
    """
    تحضير الصورة قبل OCR بأفضل جودة ممكنة.

    for_easyocr=True  → يُعيد numpy array ملوّن (RGB) — EasyOCR يفضله
    for_easyocr=False → يُعيد PIL Image رمادي محسّن — Tesseract يفضله
    """
    try:
        if OPENCV_AVAILABLE and NUMPY_AVAILABLE:
            # ── تحويل bytes → numpy ──
            nparr = np.frombuffer(img_bytes, np.uint8)
            img   = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
            if img is None:
                raise ValueError("cv2 decode failed")

            # ── تقليل الحجم الزائد (حماية الذاكرة) ──
            h, w = img.shape[:2]
            max_dim = 1600  # أقل قليلاً من السابق لتوفير RAM
            if max(h, w) > max_dim:
                scale = max_dim / max(h, w)
                img = cv2.resize(img, (int(w * scale), int(h * scale)),
                                 interpolation=cv2.INTER_AREA)

            if for_easyocr:
                # EasyOCR يعمل على RGB — فقط تكبير الصور الصغيرة جداً
                h2, w2 = img.shape[:2]
                if min(h2, w2) < 50:  # صورة صغيرة جداً → تكبير
                    scale2 = 100 / min(h2, w2)
                    img = cv2.resize(img, (int(w2 * scale2), int(h2 * scale2)),
                                     interpolation=cv2.INTER_CUBIC)
                # تحويل BGR → RGB
                result = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
                del img
                gc.collect()
                return result  # numpy array RGB

            # ── pipeline Tesseract: رمادي + تحسين ──
            gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
            del img

            # إزالة الضوضاء الخفيفة
            denoised = cv2.GaussianBlur(gray, (3, 3), 0)
            del gray

            # تحسين التباين (CLAHE) — أفضل من Histogram Equalization للنصوص
            clahe    = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
            enhanced = clahe.apply(denoised)
            del denoised

            # تحديد نوع الصورة: نص على خلفية بيضاء أم العكس؟
            mean_val = np.mean(enhanced)
            if mean_val < 127:
                # صورة داكنة (نص أبيض على خلفية سوداء) → عكس
                enhanced = cv2.bitwise_not(enhanced)

            # Thresholding تكيّفي
            thresh = cv2.adaptiveThreshold(
                enhanced, 255,
                cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                cv2.THRESH_BINARY, 15, 8
            )
            del enhanced

            # تنظيف الضوضاء الصغيرة (morphological opening)
            kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (2, 2))
            clean  = cv2.morphologyEx(thresh, cv2.MORPH_OPEN, kernel)
            del thresh

            # تحويل إلى PIL
            pil_img = Image.fromarray(clean)
            del clean
            gc.collect()
            return pil_img

        elif PIL_AVAILABLE:
            # ── Fallback بدون OpenCV ──
            pil_img = Image.open(io.BytesIO(img_bytes))
            max_dim = 1600
            if max(pil_img.size) > max_dim:
                ratio    = max_dim / max(pil_img.size)
                pil_img  = pil_img.resize(
                    (int(pil_img.width * ratio), int(pil_img.height * ratio)),
                    Image.LANCZOS
                )
            if for_easyocr:
                return np.array(pil_img.convert('RGB')) if NUMPY_AVAILABLE else None
            pil_img = pil_img.convert('L')
            enhancer = ImageEnhance.Contrast(pil_img)
            pil_img  = enhancer.enhance(2.5)
            pil_img  = pil_img.filter(ImageFilter.SHARPEN)
            return pil_img

    except Exception as e:
        try:
            if PIL_AVAILABLE:
                pil_img = Image.open(io.BytesIO(img_bytes))
                if for_easyocr:
                    if NUMPY_AVAILABLE:
                        return np.array(pil_img.convert('RGB'))
                    return None
                return pil_img.convert('L')
        except:
            pass
        return None


# ============================================================
# OCR الهجين: EasyOCR (أساسي) + Tesseract (fallback)
# ============================================================
def _run_easyocr(img_bytes):
    """
    تشغيل EasyOCR — يُعيد النص أو None عند الفشل.
    يستخدم الـ singleton (يُحمَّل مرة واحدة).
    """
    reader = get_easyocr_reader()
    if reader is None:
        return None
    try:
        img_input = preprocess_image_for_ocr(img_bytes, for_easyocr=True)
        if img_input is None:
            return None
        results = reader.readtext(img_input, detail=0, paragraph=True)
        del img_input
        gc.collect()
        text = " ".join(str(r) for r in results if r and str(r).strip())
        return text.strip() if text.strip() else None
    except Exception as e:
        return None


def _run_tesseract(img_bytes):
    """
    تشغيل Tesseract OCR — يُعيد النص أو None عند الفشل.
    """
    if not TESSERACT_AVAILABLE:
        return None
    try:
        pil_img = preprocess_image_for_ocr(img_bytes, for_easyocr=False)
        if pil_img is None:
            return None
        config = '--oem 3 --psm 6 -l ara+eng'
        text   = pytesseract.image_to_string(pil_img, config=config)
        del pil_img
        gc.collect()
        text = text.strip()
        return text if text else None
    except Exception as e:
        return None


def run_ocr(img_bytes):
    """
    OCR هجين ذكي:
    1. يجرّب EasyOCR أولاً (دقة عالية للعربية والمختلطة)
    2. إذا أعطى نتيجة قصيرة/فارغة → يجرّب Tesseract
    3. يُعيد أطول/أفضل نتيجة من الاثنين
    4. إذا فشل الاثنان → رسالة توضيحية
    """
    easy_result = _run_easyocr(img_bytes)
    tess_result = None

    # إذا كان EasyOCR أعطى نتيجة وافية (أكثر من 10 أحرف) نكتفي به
    if easy_result and len(easy_result) > 10:
        return easy_result

    # Tesseract كـ fallback أو للمقارنة
    tess_result = _run_tesseract(img_bytes)

    # اختر الأفضل (الأطول = الأكثر استخراجاً)
    if easy_result and tess_result:
        return easy_result if len(easy_result) >= len(tess_result) else tess_result
    if easy_result:
        return easy_result
    if tess_result:
        return tess_result

    # لا توجد نتيجة من أي محرك
    if not EASYOCR_AVAILABLE and not TESSERACT_AVAILABLE:
        return "[OCR غير متاح — ثبّت EasyOCR أو Tesseract]"
    return "[الصورة لا تحتوي على نص مقروء]"


# ============================================================
# استخراج النص من صفحة واحدة
# ============================================================
def extract_text_fitz(pdf_path, page_num):
    """استخراج نص من صفحة واحدة باستخدام PyMuPDF"""
    doc = None
    try:
        doc = fitz.open(pdf_path)
        page = doc[page_num]
        text = page.get_text("text")
        return (text or "").strip()
    except Exception as e:
        return None
    finally:
        if doc:
            doc.close()
        gc.collect()


def extract_text_plumber(pdf_path, page_num):
    """استخراج نص من صفحة واحدة باستخدام pdfplumber (خطة بديلة)"""
    try:
        with pdfplumber.open(pdf_path) as pdf:
            if page_num >= len(pdf.pages):
                return None
            page = pdf.pages[page_num]
            text = page.extract_text()
            return (text or "").strip()
    except Exception as e:
        return None


# ============================================================
# استخراج الصور من صفحة واحدة
# ============================================================
MAX_IMAGES_PER_PAGE = int(os.environ.get('PDF_MAX_IMAGES_PER_PAGE', '8'))   # حد أقصى للصور لكل صفحة
MAX_PAGES           = int(os.environ.get('PDF_MAX_PAGES', '150'))            # حد أقصى للصفحات (ضبطه: export PDF_MAX_PAGES=200)

def extract_images_from_page(pdf_path, page_num):
    """استخراج الصور من صفحة وإرجاع قائمة bytes (بحد أقصى MAX_IMAGES_PER_PAGE)"""
    images = []
    doc = None
    try:
        doc = fitz.open(pdf_path)
        page = doc[page_num]
        img_list = page.get_images(full=True)

        for img_index, img_info in enumerate(img_list):
            if len(images) >= MAX_IMAGES_PER_PAGE:
                break  # تجاهل الصور الزائدة حماية للذاكرة
            try:
                xref = img_info[0]
                base_image = doc.extract_image(xref)
                if not base_image:
                    continue
                img_bytes = base_image.get("image")
                if img_bytes and len(img_bytes) > 500:  # تجاهل الصور الصغيرة جداً (أيقونات)
                    images.append(img_bytes)
                    del img_bytes
            except Exception as e:
                if os.environ.get('PDF_DEBUG') == '1':
                    send_error(f"تعذر استخراج صورة من الصفحة {page_num + 1}: {str(e)[:100]}", fatal=False)
                continue

    except Exception as e:
        if os.environ.get('PDF_DEBUG') == '1':
            send_error(f"تعذر فتح صور الصفحة {page_num + 1}: {str(e)[:100]}", fatal=False)
    finally:
        if doc:
            doc.close()
        gc.collect()

    return images


# ============================================================
# استخراج الجداول من صفحة واحدة
# ============================================================
def extract_tables_from_page(pdf_path, page_num):
    """استخراج الجداول من صفحة باستخدام pdfplumber"""
    tables = []
    if not PLUMBER_AVAILABLE:
        return tables

    try:
        with pdfplumber.open(pdf_path) as pdf:
            if page_num >= len(pdf.pages):
                return tables
            page = pdf.pages[page_num]
            raw_tables = page.extract_tables()

            for table in (raw_tables or []):
                if not table:
                    continue
                lines = []
                for row in table:
                    if not row:
                        continue
                    # تنظيف الخلايا
                    cleaned = [str(cell).strip() if cell is not None else "" for cell in row]
                    lines.append(" | ".join(cleaned))
                if lines:
                    tables.append("\n".join(lines))

    except Exception as e:
        pass

    return tables


# ============================================================
# معالجة صفحة واحدة كاملة
# ============================================================
def process_page(pdf_path, page_num, output_file, enable_ocr=True):
    """
    معالجة صفحة واحدة: نص + صور OCR + جداول
    كتابة النتيجة مباشرة إلى ملف الإخراج
    """
    page_label = f"===== PAGE {page_num + 1} =====\n"
    page_content = [page_label]

    # ── 1. استخراج النص ──
    text = None

    if FITZ_AVAILABLE:
        text = extract_text_fitz(pdf_path, page_num)

    # خطة بديلة: pdfplumber إذا فشل fitz أو كانت النتيجة ضعيفة
    if (not text or len(text) < 20) and PLUMBER_AVAILABLE:
        fallback_text = extract_text_plumber(pdf_path, page_num)
        if fallback_text and len(fallback_text) > (len(text) if text else 0):
            text = fallback_text

    if text:
        page_content.append("[TEXT]\n")
        page_content.append(text)
        page_content.append("\n")
    else:
        page_content.append("[TEXT]\n[لا يوجد نص قابل للاستخراج]\n")

    # ── 2. استخراج الصور + OCR (EasyOCR أو Tesseract) ──
    if enable_ocr and FITZ_AVAILABLE and (EASYOCR_AVAILABLE or TESSERACT_AVAILABLE):
        images = extract_images_from_page(pdf_path, page_num)
        for img_idx, img_bytes in enumerate(images, start=1):
            ocr_text = run_ocr(img_bytes)
            page_content.append(f"\n[IMAGE_{img_idx}_OCR]\n")
            page_content.append(ocr_text)
            page_content.append("\n")
            del img_bytes
        del images
        gc.collect()

    # ── 3. استخراج الجداول ──
    tables = extract_tables_from_page(pdf_path, page_num)
    for tbl_idx, table_text in enumerate(tables, start=1):
        page_content.append(f"\n[TABLE_{tbl_idx}]\n")
        page_content.append(table_text)
        page_content.append("\n")

    page_content.append("\n")  # سطر فاصل بين الصفحات

    # ── كتابة الصفحة إلى الملف ──
    with open(output_file, 'a', encoding='utf-8') as f:
        f.write("".join(page_content))

    # تحرير الذاكرة
    del page_content
    gc.collect()


# ============================================================
# الدالة الرئيسية
# ============================================================
def main():
    if len(sys.argv) < 3:
        send_error("الاستخدام: python pdf_worker.py <pdf_path> <output_path> [enable_ocr=1]", fatal=True)
        sys.exit(1)

    pdf_path   = sys.argv[1]
    output_path = sys.argv[2]
    enable_ocr = (sys.argv[3].lower() not in ('0', 'false', 'no')) if len(sys.argv) > 3 else True

    # فحص وجود الملف
    if not os.path.exists(pdf_path):
        send_error(f"الملف غير موجود: {pdf_path}", fatal=True)
        sys.exit(1)

    # فحص توفر PyMuPDF على الأقل
    if not FITZ_AVAILABLE and not PLUMBER_AVAILABLE:
        send_error("PyMuPDF وpdfplumber غير مثبتين!", fatal=True)
        sys.exit(1)

    # تهيئة ملف الإخراج (مسح المحتوى القديم)
    try:
        with open(output_path, 'w', encoding='utf-8') as f:
            f.write("")
    except Exception as e:
        send_error(f"لا يمكن إنشاء ملف الإخراج: {e}", fatal=True)
        sys.exit(1)

    # تحديد عدد الصفحات
    total_pages = 0
    doc = None
    try:
        if FITZ_AVAILABLE:
            doc = fitz.open(pdf_path)
            total_pages = doc.page_count
            doc.close()
        elif PLUMBER_AVAILABLE:
            with pdfplumber.open(pdf_path) as pdf:
                total_pages = len(pdf.pages)
    except Exception as e:
        send_error(f"لا يمكن قراءة الملف: {e}", fatal=True)
        sys.exit(1)

    if total_pages == 0:
        send_error("الملف لا يحتوي على صفحات!", fatal=True)
        sys.exit(1)

    # إحصائيات
    ocr_ready = enable_ocr and (EASYOCR_AVAILABLE or TESSERACT_AVAILABLE)
    if ocr_ready and EASYOCR_AVAILABLE:
        # تحميل EasyOCR مبكراً — أفضل من التحميل على الصفحة الأولى
        send_progress(0, 0, "جاري تحميل نموذج EasyOCR...")
        get_easyocr_reader()  # يُحمَّل مرة واحدة هنا ويبقى في الذاكرة

    stats = {
        "total_pages": total_pages,
        "processed": 0,
        "failed": 0,
        "ocr_enabled": ocr_ready,
        "ocr_engine": ("easyocr+tesseract" if (EASYOCR_AVAILABLE and TESSERACT_AVAILABLE)
                        else "easyocr" if EASYOCR_AVAILABLE
                        else "tesseract" if TESSERACT_AVAILABLE
                        else "none"),
        "libraries": {
            "fitz": FITZ_AVAILABLE,
            "pdfplumber": PLUMBER_AVAILABLE,
            "easyocr": EASYOCR_AVAILABLE,
            "tesseract": TESSERACT_AVAILABLE,
            "opencv": OPENCV_AVAILABLE,
            "numpy": NUMPY_AVAILABLE
        }
    }

    # تطبيق حد MAX_PAGES — منع استهلاك الذاكرة على الملفات الضخمة
    if total_pages > MAX_PAGES:
        send_error(
            f"الملف يحتوي على {total_pages} صفحة، الحد الأقصى المسموح {MAX_PAGES}. "
            f"سيتم معالجة أول {MAX_PAGES} صفحة فقط.",
            fatal=False
        )
        total_pages = MAX_PAGES

    send_progress(0, total_pages, f"بدأت المعالجة - {total_pages} صفحة")

    # معالجة صفحة بصفحة
    for page_num in range(total_pages):
        try:
            process_page(pdf_path, page_num, output_path, enable_ocr)
            stats["processed"] += 1
        except Exception as e:
            stats["failed"] += 1
            # تسجيل الخطأ في ملف log
            try:
                log_path = output_path + ".log"
                with open(log_path, 'a', encoding='utf-8') as lf:
                    lf.write(f"[خطأ صفحة {page_num + 1}]: {traceback.format_exc()}\n")
            except:
                pass
            # كتابة إشعار الخطأ في ملف الإخراج ثم الاستمرار
            try:
                with open(output_path, 'a', encoding='utf-8') as f:
                    f.write(f"===== PAGE {page_num + 1} =====\n[فشل معالجة هذه الصفحة: {str(e)[:200]}]\n\n")
            except:
                pass

        # إرسال التقدم كل 5 صفحات أو عند آخر صفحة
        if (page_num + 1) % 5 == 0 or page_num == total_pages - 1:
            send_progress(page_num + 1, total_pages, f"تمت معالجة {page_num + 1} من {total_pages}")

        # تحرير الذاكرة بعد كل صفحة، وتحرير أعمق كل 10 صفحات
        gc.collect()
        if (page_num + 1) % 10 == 0:
            gc.collect()  # تحرير أعمق — يضمن تحرير الـ cyclic references

    send_done(output_path, total_pages, stats)


if __name__ == "__main__":
    main()
