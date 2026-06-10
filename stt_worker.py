#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
stt_worker.py — استخراج النص من الصوت محلياً بدون API خارجي
يستخدم: faster-whisper (أساسي) → vosk (fallback)
يُستدعى من Node.js عبر child_process.spawn

الاستخدام:
  python3 stt_worker.py <audio_path> [lang=ar|en|auto]

الإخراج (JSON على stdout):
  {"type": "result", "text": "...", "language": "ar", "engine": "faster-whisper", "duration": 3.2}
  {"type": "error",  "message": "...", "fatal": true/false}

نماذج faster-whisper (تُحمَّل تلقائياً أول مرة):
  tiny   ~75MB  — سريع جداً، دقة معقولة
  base   ~145MB — توازن جيد ✅ (الافتراضي)
  small  ~244MB — دقة أعلى
  medium ~769MB — ممتاز (يحتاج ذاكرة أكثر)

تحكم عبر env:
  STT_MODEL=base          (اسم النموذج)
  STT_DEVICE=cpu          (cpu أو cuda)
  STT_COMPUTE=int8        (int8 أسرع على CPU)
  STT_LANG=ar             (لغة ثابتة أو auto للكشف التلقائي)
  WHISPER_CACHE=/path     (مجلد النماذج — الافتراضي ~/.cache/huggingface)
"""

import sys
import os
import json
import time

# ============================================================
# faster-whisper
# ============================================================
try:
    from faster_whisper import WhisperModel
    WHISPER_AVAILABLE = True
except ImportError:
    WHISPER_AVAILABLE = False

# ============================================================
# vosk — fallback خفيف (يحتاج نموذج منفصل)
# ============================================================
try:
    from vosk import Model as VoskModel, KaldiRecognizer
    import wave
    VOSK_AVAILABLE = True
except ImportError:
    VOSK_AVAILABLE = False

# ============================================================
# ffmpeg-python أو subprocess للتحويل
# ============================================================
import subprocess


def send_json(obj):
    print(json.dumps(obj, ensure_ascii=False), flush=True)


def send_error(msg, fatal=False):
    send_json({"type": "error", "message": msg, "fatal": fatal})


def send_result(text, language, engine, duration=None):
    send_json({
        "type":     "result",
        "text":     text.strip(),
        "language": language,
        "engine":   engine,
        "duration": duration
    })


# ============================================================
# تحويل الصوت إلى WAV 16kHz Mono (مطلوب لـ Whisper و Vosk)
# ============================================================
def convert_to_wav(input_path: str, output_path: str) -> bool:
    """يحوّل أي صيغة صوتية إلى WAV 16kHz mono عبر ffmpeg"""
    try:
        result = subprocess.run(
            [
                "ffmpeg", "-y",
                "-i", input_path,
                "-ar", "16000",
                "-ac", "1",
                "-f", "wav",
                output_path
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=60
        )
        return result.returncode == 0 and os.path.exists(output_path) and os.path.getsize(output_path) > 100
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        send_error(f"ffmpeg فشل في التحويل: {e}", fatal=False)
        return False


# ============================================================
# Whisper STT
# ============================================================

_whisper_model = None  # singleton — يُحمَّل مرة واحدة

def get_whisper_model():
    global _whisper_model
    if _whisper_model is not None:
        return _whisper_model

    model_name   = os.environ.get("STT_MODEL",   "base")
    device       = os.environ.get("STT_DEVICE",  "cpu")
    compute_type = os.environ.get("STT_COMPUTE", "int8")
    cache_dir    = os.environ.get("WHISPER_CACHE", None)

    try:
        send_json({"type": "info", "message": f"جاري تحميل نموذج Whisper [{model_name}]..."})
        kwargs = {"device": device, "compute_type": compute_type}
        if cache_dir:
            kwargs["download_root"] = cache_dir
        _whisper_model = WhisperModel(model_name, **kwargs)
        send_json({"type": "info", "message": f"✅ Whisper [{model_name}] جاهز"})
        return _whisper_model
    except Exception as e:
        send_error(f"فشل تحميل Whisper: {str(e)[:150]}", fatal=False)
        return None


def transcribe_whisper(wav_path: str, lang: str) -> tuple[str | None, str]:
    """يُعيد (نص, لغة_مكتشفة)"""
    model = get_whisper_model()
    if model is None:
        return None, lang

    # إذا كانت اللغة auto نترك Whisper يكتشفها
    language_param = None if lang == "auto" else lang

    try:
        segments, info = model.transcribe(
            wav_path,
            language=language_param,
            beam_size=5,
            vad_filter=True,          # يُزيل الصمت تلقائياً
            vad_parameters={
                "min_silence_duration_ms": 500
            }
        )
        text = " ".join(seg.text for seg in segments).strip()
        detected_lang = info.language if info.language else (lang if lang != "auto" else "und")
        return text, detected_lang
    except Exception as e:
        send_error(f"Whisper transcribe فشل: {str(e)[:150]}", fatal=False)
        return None, lang


# ============================================================
# Vosk STT (fallback)
# ============================================================

def transcribe_vosk(wav_path: str, lang: str) -> str | None:
    """fallback بسيط باستخدام vosk — يحتاج نموذج مُحمَّل مسبقاً"""
    # مجلد نماذج Vosk
    vosk_models_dir = os.environ.get("VOSK_MODELS_DIR", os.path.expanduser("~/.vosk"))
    lang_dir = os.path.join(vosk_models_dir, f"model-{lang}")
    if not os.path.isdir(lang_dir):
        # جرّب الاسم المباشر
        lang_dir = os.path.join(vosk_models_dir, lang)
    if not os.path.isdir(lang_dir):
        send_error(f"نموذج Vosk غير موجود في: {lang_dir}", fatal=False)
        return None

    try:
        vosk_model = VoskModel(lang_dir)
        wf = wave.open(wav_path, "rb")
        rec = KaldiRecognizer(vosk_model, wf.getframerate())
        rec.SetWords(True)

        results = []
        while True:
            data = wf.readframes(4000)
            if len(data) == 0:
                break
            if rec.AcceptWaveform(data):
                r = json.loads(rec.Result())
                if r.get("text"):
                    results.append(r["text"])

        final = json.loads(rec.FinalResult())
        if final.get("text"):
            results.append(final["text"])

        wf.close()
        return " ".join(results).strip() or None
    except Exception as e:
        send_error(f"Vosk فشل: {str(e)[:120]}", fatal=False)
        return None


# ============================================================
# main
# ============================================================
def main():
    if len(sys.argv) < 2:
        send_error("الاستخدام: python3 stt_worker.py <audio_path> [lang=ar|en|auto]", fatal=True)
        sys.exit(1)

    audio_path = sys.argv[1]
    lang       = sys.argv[2].lower() if len(sys.argv) > 2 else os.environ.get("STT_LANG", "ar")

    if not os.path.exists(audio_path):
        send_error(f"الملف غير موجود: {audio_path}", fatal=True)
        sys.exit(1)

    if not WHISPER_AVAILABLE and not VOSK_AVAILABLE:
        send_error(
            "لا توجد مكتبة STT مثبّتة. ثبّت: pip install faster-whisper",
            fatal=True
        )
        sys.exit(1)

    start = time.time()

    # ── تحويل الصوت إلى WAV ──
    import tempfile
    tmp_file = tempfile.NamedTemporaryFile(prefix="stt_", suffix=".wav", delete=False)
    tmp_wav = tmp_file.name
    tmp_file.close()
    converted = convert_to_wav(audio_path, tmp_wav)

    wav_to_use = tmp_wav if converted else audio_path  # fallback: جرّب الملف الأصلي

    text     = None
    det_lang = lang
    engine   = "none"

    # ── محاولة 1: faster-whisper ──
    if WHISPER_AVAILABLE:
        text, det_lang = transcribe_whisper(wav_to_use, lang)
        if text:
            engine = "faster-whisper"

    # ── محاولة 2: vosk ──
    if not text and VOSK_AVAILABLE:
        vosk_lang = "ar" if "ar" in lang else "en"
        text = transcribe_vosk(wav_to_use, vosk_lang)
        if text:
            engine   = "vosk"
            det_lang = vosk_lang

    # تنظيف
    try:
        if converted and os.path.exists(tmp_wav):
            os.unlink(tmp_wav)
    except Exception:
        pass

    duration = round(time.time() - start, 2)

    if not text:
        send_error("لم يُستخرج أي نص من الملف الصوتي", fatal=True)
        sys.exit(1)

    send_result(text, det_lang, engine, duration)


if __name__ == "__main__":
    main()
