'use strict';
/**
 * pdf_processor.js — جسر Node.js ↔ pdf_worker.py
 * يستدعي pdf_worker.py عبر child_process.spawn ويُعيد النتيجة لـ bot.js
 */

const { spawn } = require('child_process');
const fs         = require('fs');
const os         = require('os');
const path       = require('path');
const crypto     = require('crypto');

const WORKER_PATH  = path.join(__dirname, 'pdf_worker.py');
const PYTHON_CMD   = process.env.PYTHON_CMD || 'python3';
const ENABLE_OCR   = process.env.PDF_OCR !== '0'; // يمكن تعطيل OCR عبر PDF_OCR=0

// ============================================================
// processPDF — يُستدعى من bot.js
// ============================================================
/**
 * @param {Buffer}  buffer    — بيانات ملف PDF الخام
 * @param {string}  fileName  — اسم الملف (للعرض فقط)
 * @param {string}  sender    — رقم المُرسل (للتتبع فقط)
 * @param {object}  sock      — كائن واتساب (لإرسال تقدم اختياري)
 * @param {string}  jid       — معرف المحادثة
 * @param {object}  _msg      — كائن الرسالة (غير مستخدم حالياً)
 * @returns {Promise<{docText, pageCount, stats, outputPath}>}
 */
async function processPDF(buffer, fileName, sender, sock, jid, _msg) {
    return new Promise((resolve, reject) => {
        // ── ملف PDF مؤقت للإدخال ──
        const tmpId      = crypto.randomBytes(8).toString('hex');
        const tmpPdfPath = path.join(os.tmpdir(), `pdf_in_${tmpId}.pdf`);
        const tmpOutPath = path.join(os.tmpdir(), `pdf_out_${tmpId}.txt`);

        try {
            fs.writeFileSync(tmpPdfPath, buffer);
        } catch (e) {
            return reject(new Error(`فشل كتابة الملف المؤقت: ${e.message}`));
        }

        const args = [WORKER_PATH, tmpPdfPath, tmpOutPath, ENABLE_OCR ? '1' : '0'];
        const child = spawn(PYTHON_CMD, args, {
            stdio: ['ignore', 'pipe', 'pipe'],
            env: { ...process.env }
        });

        let stderrBuf = '';
        let stats     = {};
        let pageCount = 0;
        let outputPath = tmpOutPath;
        let settled = false;

        child.stderr.on('data', d => { stderrBuf += d.toString(); });

        let stdoutBuf = '';
        child.stdout.on('data', data => {
            // pdf_worker يُرسل سطراً JSON لكل حدث — قد يصل مقسوماً على chunks
            stdoutBuf += data.toString();
            const lines = stdoutBuf.split('\n');
            stdoutBuf = lines.pop();
            for (const line of lines.filter(l => l.trim())) {
                try {
                    const evt = JSON.parse(line);
                    if (evt.type === 'progress') {
                        // إرسال تحديث اختياري عبر واتساب (يمكن تعطيله)
                        if (evt.message && sock && jid && process.env.PDF_PROGRESS_MSGS === '1') {
                            sock.sendMessage(jid, { text: `⏳ ${evt.message}` }).catch(() => {});
                        }
                    } else if (evt.type === 'done') {
                        pageCount  = evt.total_pages || 0;
                        stats      = evt.stats       || {};
                        outputPath = evt.output_path || tmpOutPath;
                    } else if (evt.type === 'error' && evt.fatal) {
                        if (settled) return;
                        settled = true;
                        child.kill();
                        cleanup(tmpPdfPath, tmpOutPath);
                        return reject(new Error(evt.message || 'خطأ في معالجة PDF'));
                    }
                } catch (_) { /* سطور غير JSON — تُتجاهل */ }
            }
        });

        child.on('close', code => {
            if (settled) return;
            settled = true;
            if (stdoutBuf.trim()) {
                try {
                    const evt = JSON.parse(stdoutBuf.trim());
                    if (evt.type === 'done') {
                        pageCount  = evt.total_pages || 0;
                        stats      = evt.stats       || {};
                        outputPath = evt.output_path || tmpOutPath;
                    }
                } catch (_) {}
            }
            if (code !== 0 && code !== null) {
                cleanup(tmpPdfPath, tmpOutPath);
                const errMsg = stderrBuf.slice(-400) || `pdf_worker خرج بكود: ${code}`;
                return reject(new Error(errMsg));
            }

            // قراءة ملف النص الناتج
            let docText = '';
            try {
                if (fs.existsSync(outputPath)) {
                    docText = fs.readFileSync(outputPath, 'utf-8');
                }
            } catch (e) {
                cleanup(tmpPdfPath, tmpOutPath);
                return reject(new Error(`فشل قراءة ملف الإخراج: ${e.message}`));
            }

            cleanup(tmpPdfPath, tmpOutPath);

            if (!docText.trim()) {
                return reject(new Error('لم يُستخرج أي نص من ملف PDF'));
            }

            resolve({ docText, pageCount, stats, outputPath: null });
        });

        child.on('error', err => {
            if (settled) return;
            settled = true;
            cleanup(tmpPdfPath, tmpOutPath);
            reject(new Error(`فشل تشغيل pdf_worker: ${err.message} — تأكد من تثبيت ${PYTHON_CMD}`));
        });
    });
}

// ============================================================
// checkDependencies — يفحص مكتبات Python ويُعيد تقرير
// ============================================================
async function checkDependencies() {
    return new Promise(resolve => {
        const script = `
import sys, json
result = {"python": True}
libs = {
    "fitz":        "PyMuPDF",
    "pdfplumber":  "pdfplumber",
    "PIL":         "Pillow",
    "pytesseract": "pytesseract",
    "cv2":         "opencv-python",
    "easyocr":     "easyocr",
    "numpy":       "numpy"
}
for mod, _ in libs.items():
    try:
        __import__(mod)
        result[mod] = True
    except ImportError:
        result[mod] = False
print(json.dumps(result))
`;
        const child = spawn(PYTHON_CMD, ['-c', script], {
            stdio: ['ignore', 'pipe', 'pipe']
        });

        const timer = setTimeout(() => {
            child.kill('SIGTERM');
            resolve({ python: false, timeout: true });
        }, 15_000);

        let settled = false;
        let out = '';
        child.stdout.on('data', d => { out += d.toString(); });
        child.on('close', () => {
            if (settled) return;
            settled = true;
            clearTimeout(timer);
            try   { resolve(JSON.parse(out.trim())); }
            catch { resolve({ python: false }); }
        });
        child.on('error', () => {
            if (settled) return;
            settled = true;
            clearTimeout(timer);
            resolve({ python: false });
        });
    });
}

// ============================================================
// HELPERS
// ============================================================
function cleanup(...paths) {
    for (const p of paths) {
        try { if (fs.existsSync(p)) fs.unlinkSync(p); } catch (_) {}
    }
}

module.exports = { processPDF, checkDependencies };
