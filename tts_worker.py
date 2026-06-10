#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tts_worker.py — توليد صوت عبر edge-tts مع fallback محلي
يستخدم: edge-tts (يحتاج إنترنت) + pyttsx3 fallback محلي
يُستدعى من Node.js عبر child_process.spawn

الاستخدام:
  python3 tts_worker.py <text> <output_mp3> [lang=ar|en]

الإخراج (JSON على stdout):
  {"type": "done",  "output": "/path/to/file.mp3", "engine": "edge-tts"}
  {"type": "error", "message": "...", "fatal": true/false}

الأصوات المدعومة:
  العربية  : ar-SA-ZariyahNeural (أنثى) / ar-SA-HamedNeural (ذكر)
  الإنجليزية: en-US-AriaNeural   (أنثى) / en-US-GuyNeural    (ذكر)
"""

import sys
import os
import json
import asyncio

# ============================================================
# edge-tts
# ============================================================
try:
    import edge_tts
    EDGE_TTS_AVAILABLE = True
except ImportError:
    EDGE_TTS_AVAILABLE = False

# ============================================================
# pyttsx3 — fallback محلي كامل بدون إنترنت
# ============================================================
try:
    import pyttsx3
    PYTTSX3_AVAILABLE = True
except ImportError:
    PYTTSX3_AVAILABLE = False


def send_json(obj):
    print(json.dumps(obj, ensure_ascii=False), flush=True)


def send_error(msg, fatal=False):
    send_json({"type": "error", "message": msg, "fatal": fatal})


def send_done(output_path, engine):
    send_json({"type": "done", "output": output_path, "engine": engine})


def run_async(coro):
    # تشغيل coroutine بأمان حتى لو كانت البيئة تحتوي على event loop قائم.
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        return executor.submit(asyncio.run, coro).result()


# ============================================================
# خريطة الأصوات
# ============================================================
VOICES = {
    "ar": os.environ.get("TTS_VOICE_AR", "ar-SA-ZariyahNeural"),
    "en": os.environ.get("TTS_VOICE_EN", "en-US-AriaNeural"),
    "fr": "fr-FR-DeniseNeural",
    "de": "de-DE-KatjaNeural",
    "tr": "tr-TR-EmelNeural",
}

def get_voice(lang: str) -> str:
    return VOICES.get(lang.lower(), VOICES["en"])


# ============================================================
# توليد الصوت عبر edge-tts
# ============================================================
async def generate_edge_tts(text: str, output_path: str, lang: str) -> bool:
    voice = get_voice(lang)
    rate  = os.environ.get("TTS_RATE", "-5%")   # سرعة أبطأ قليلاً للنطق الطبي
    try:
        communicate = edge_tts.Communicate(text, voice, rate=rate)
        await communicate.save(output_path)
        # تحقق من أن الملف غير فارغ
        if os.path.exists(output_path) and os.path.getsize(output_path) > 500:
            return True
        return False
    except Exception as e:
        send_error(f"edge-tts فشل: {str(e)[:120]}", fatal=False)
        return False


# ============================================================
# fallback: pyttsx3 (لا يحتاج إنترنت على الإطلاق)
# ============================================================
def generate_pyttsx3(text: str, output_path: str) -> bool:
    if not PYTTSX3_AVAILABLE:
        return False
    tmp_output = output_path
    # pyttsx3 لا يضمن إخراج MP3 على كل الأنظمة؛ نولد WAV ثم نترك Node/ffmpeg يحوله لـ OGG.
    if output_path.lower().endswith(".mp3"):
        tmp_output = output_path + ".wav"
    try:
        engine = pyttsx3.init()
        engine.setProperty("rate", 160)
        engine.setProperty("volume", 1.0)
        engine.save_to_file(text, tmp_output)
        engine.runAndWait()
        engine.stop()
        if os.path.exists(tmp_output) and os.path.getsize(tmp_output) > 500:
            if tmp_output != output_path:
                os.replace(tmp_output, output_path)
            return True
        return False
    except Exception as e:
        send_error(f"pyttsx3 فشل: {str(e)[:120]}", fatal=False)
        try:
            if tmp_output != output_path and os.path.exists(tmp_output):
                os.remove(tmp_output)
        except Exception:
            pass
        return False


# ============================================================
# main
# ============================================================
def main():
    if len(sys.argv) < 3:
        send_error("الاستخدام: python3 tts_worker.py <text> <output_mp3> [lang]", fatal=True)
        sys.exit(1)

    text        = sys.argv[1]
    output_path = sys.argv[2]
    lang        = sys.argv[3].lower() if len(sys.argv) > 3 else "en"

    # تنظيف النص
    text = text.strip()
    if not text:
        send_error("النص فارغ", fatal=True)
        sys.exit(1)

    # حد النص (edge-tts يدعم نصوصاً طويلة لكن نحدّه بـ 500 حرف للسرعة)
    max_chars = int(os.environ.get("TTS_MAX_CHARS", "500"))
    if len(text) > max_chars:
        # قطع على حدود الكلمات
        text = text[:max_chars].rsplit(" ", 1)[0]

    # ── محاولة 1: edge-tts ──
    if EDGE_TTS_AVAILABLE:
        success = run_async(generate_edge_tts(text, output_path, lang))
        if success:
            send_done(output_path, "edge-tts")
            return

    # ── محاولة 2: pyttsx3 (fallback) ──
    if PYTTSX3_AVAILABLE:
        success = generate_pyttsx3(text, output_path)
        if success:
            send_done(output_path, "pyttsx3")
            return

    send_error(
        "لا توجد مكتبة TTS مثبّتة. ثبّت: pip install edge-tts",
        fatal=True
    )
    sys.exit(1)


if __name__ == "__main__":
    main()
