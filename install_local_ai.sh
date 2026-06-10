#!/data/data/com.termux/files/usr/bin/bash
# ============================================================
# install_local_ai.sh — تثبيت مكتبات TTS و STT المحلية
# للتشغيل على Termux (Android) أو Linux
# ============================================================
# الاستخدام:
#   chmod +x install_local_ai.sh
#   ./install_local_ai.sh
# ============================================================

set -e
echo ""
echo "╔═══════════════════════════════════════════════════╗"
echo "║   تثبيت TTS + STT + PDF/OCR   ║"
echo "╚═══════════════════════════════════════════════════╝"
echo ""

# كشف البيئة
IS_TERMUX=false
if [ -d "/data/data/com.termux" ]; then
    IS_TERMUX=true
    echo "✅ تم اكتشاف بيئة Termux (Android)"
else
    echo "✅ بيئة Linux عادية"
fi

# ── تحديث pip ──
echo ""
echo "📦 تحديث pip..."
python3 -m pip install --upgrade pip --break-system-packages 2>/dev/null || pip install --upgrade pip

# ── حزم نظام اختيارية لكنها مهمة لـ ffmpeg / tesseract / OCR ──
echo ""
echo "🧩 تثبيت حزم النظام المطلوبة إن توفرت..."
if [ "$IS_TERMUX" = true ]; then
    pkg update -y 2>/dev/null || true
    pkg install -y ffmpeg tesseract espeak 2>/dev/null || pkg install -y ffmpeg tesseract espeak-ng 2>/dev/null || echo "⚠️  تعذر تثبيت ffmpeg/tesseract/espeak عبر pkg — ثبّتها يدوياً إن لزم"
else
    if command -v apt >/dev/null 2>&1; then
        sudo apt update 2>/dev/null || true
        sudo apt install -y ffmpeg espeak-ng tesseract-ocr tesseract-ocr-ara tesseract-ocr-eng 2>/dev/null || echo "⚠️  تعذر تثبيت حزم النظام عبر apt — ثبّتها يدوياً إن لزم"
    fi
fi

# ────────────────────────────────────────────────────────────
# 1. edge-tts — توليد صوت عالي الجودة (العربية + الإنجليزية)
# ────────────────────────────────────────────────────────────
echo ""
echo "🔊 [1/5] تثبيت edge-tts (توليد الصوت)..."
pip install edge-tts --break-system-packages 2>/dev/null || pip install edge-tts

# ────────────────────────────────────────────────────────────
# 2. pyttsx3 — fallback صوتي بدون إنترنت
# ────────────────────────────────────────────────────────────
echo ""
echo "🔈 [2/5] تثبيت pyttsx3 (fallback صوتي)..."
if [ "$IS_TERMUX" = true ]; then
    pip install pyttsx3 --break-system-packages 2>/dev/null || echo "⚠️  pyttsx3 اختياري على Termux، نتخطاه"
else
    pip install pyttsx3 --break-system-packages 2>/dev/null || pip install pyttsx3 || echo "⚠️  pyttsx3 اختياري، نتخطاه"
fi

# ────────────────────────────────────────────────────────────
# 3. faster-whisper — STT عالي الدقة للعربية والإنجليزية
# ────────────────────────────────────────────────────────────
echo ""
echo "🎤 [3/5] تثبيت faster-whisper (تحويل الصوت لنص)..."
if [ "$IS_TERMUX" = true ]; then
    # Termux يحتاج ctranslate2 مبنياً يدوياً أو من wheels خاص
    echo "   ⚠️  على Termux، faster-whisper قد يحتاج وقتاً للبناء..."
    pip install faster-whisper --break-system-packages 2>/dev/null || {
        echo "   ⚠️  faster-whisper فشل، جرّب:"
        echo "       pip install openai-whisper --break-system-packages"
        pip install openai-whisper --break-system-packages 2>/dev/null || echo "   ⚠️  openai-whisper أيضاً فشل — سيعمل STT بـ Voxtral فقط"
    }
else
    pip install faster-whisper --break-system-packages 2>/dev/null || pip install faster-whisper
fi

# ────────────────────────────────────────────────────────────
# 4. تحميل نموذج Whisper (base ~145MB) مرة واحدة
# ────────────────────────────────────────────────────────────
echo ""
echo "📥 [4/5] تحميل نموذج Whisper base (~145MB)..."
echo "   (يُحمَّل مرة واحدة ويُخزَّن محلياً)"
python3 -c "
try:
    from faster_whisper import WhisperModel
    print('   ⏳ جاري التحميل...')
    m = WhisperModel('base', device='cpu', compute_type='int8')
    print('   ✅ نموذج base جاهز!')
except ImportError:
    try:
        import whisper
        print('   ⏳ جاري التحميل (openai-whisper)...')
        whisper.load_model('base')
        print('   ✅ نموذج base جاهز!')
    except ImportError:
        print('   ⚠️  لم يُحمَّل أي نموذج — سيعمل STT بـ Voxtral API')
except Exception as e:
    print(f'   ⚠️  {e}')
" 2>/dev/null || echo "   ⚠️  تحميل النموذج اختياري، سيحدث تلقائياً عند أول استخدام"

# ────────────────────────────────────────────────────────────
# 5. مكتبات PDF/OCR المطلوبة لقراءة PDF والصور
# ────────────────────────────────────────────────────────────
echo ""
echo "📄 [5/5] تثبيت مكتبات PDF/OCR..."
pip install pymupdf pdfplumber pillow pytesseract numpy --break-system-packages 2>/dev/null || \
pip install pymupdf pdfplumber pillow pytesseract numpy

if [ "$IS_TERMUX" = true ]; then
    echo "   ⚠️  opencv-python/easyocr على Termux قد تكون ثقيلة أو تفشل حسب الجهاز..."
    pip install opencv-python-headless easyocr --break-system-packages 2>/dev/null || \
    pip install opencv-python-headless easyocr 2>/dev/null || \
    echo "   ⚠️  تعذر تثبيت opencv/easyocr — سيعمل fallback إن توفرت Pillow/Tesseract"
else
    pip install opencv-python-headless easyocr --break-system-packages 2>/dev/null || \
    pip install opencv-python-headless easyocr || \
    echo "   ⚠️  تعذر تثبيت opencv/easyocr — سيعمل fallback إن توفرت Pillow/Tesseract"
fi

# ────────────────────────────────────────────────────────────
# تقرير نهائي
# ────────────────────────────────────────────────────────────
echo ""
echo "════════════════════════════════"
echo "📋 تقرير التثبيت:"
echo ""
python3 -c "
libs = {
    'edge_tts':        'edge-tts (TTS أساسي)',
    'pyttsx3':         'pyttsx3 (TTS fallback)',
    'faster_whisper':  'faster-whisper (STT أساسي)',
    'whisper':         'openai-whisper (STT بديل)',
    'fitz':            'PyMuPDF (PDF)',
    'pdfplumber':      'pdfplumber (PDF)',
    'PIL':             'Pillow (صور)',
    'pytesseract':     'pytesseract (OCR fallback)',
    'cv2':             'OpenCV (تحضير صور)',
    'easyocr':         'EasyOCR (OCR أساسي)',
    'numpy':           'NumPy',
}
for mod, name in libs.items():
    try:
        __import__(mod)
        print(f'  ✅ {name}')
    except ImportError:
        print(f'  ❌ {name} — غير مثبَّت')
"

echo ""
echo "════════════════════════════════"
echo ""
echo "🎉 انتهى التثبيت!"
echo ""
echo "📝 ملاحظات:"
echo "  • TTS: يستخدم edge-tts ويحتاج إنترنت؛ pyttsx3 يعمل كـ fallback محلي إن توفر"
echo "  • STT: يستخدم faster-whisper محلياً بالكامل (بدون إنترنت)"
echo "  • Voxtral API لا يزال يعمل كخيار أساسي + STT المحلي كـ fallback"
echo ""
echo "🔧 متغيرات البيئة الاختيارية:"
echo "  export STT_MODEL=small     # نموذج أدق (244MB بدل 145MB)"
echo "  export TTS_VOICE_AR=ar-SA-HamedNeural  # صوت ذكر عربي"
echo "  export TTS_VOICE_EN=en-US-GuyNeural    # صوت ذكر إنجليزي"
echo "  export TTS_RATE=-10%       # أبطأ قليلاً (مفيد للنطق الطبي)"
echo ""

# ────────────────────────────────────────────────────────────
# 5. مكتبات PDF/OCR/TXT/Image Tools المطلوبة للبوت
# ────────────────────────────────────────────────────────────
echo ""
echo "📄 [5/5] تثبيت مكتبات PDF/OCR وإنشاء الملفات وتحسين الصور..."
COMMON_PKGS="pymupdf pdfplumber pillow pytesseract numpy arabic-reshaper python-bidi reportlab"
pip install $COMMON_PKGS --break-system-packages 2>/dev/null || pip install $COMMON_PKGS || echo "⚠️ فشل تثبيت بعض مكتبات PDF الأساسية"

# OpenCV/EasyOCR ثقيلة على Termux، نحاول تثبيتها بدون إيقاف السكربت عند الفشل
pip install opencv-python easyocr --break-system-packages 2>/dev/null || pip install opencv-python easyocr || echo "⚠️ opencv/easyocr اختياريان وقد يحتاجان تثبيتاً يدوياً على Termux"

echo ""
echo "✅ انتهى تثبيت مكتبات MedTerm المحلية."
