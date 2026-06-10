'use strict';

/**
 * image_ocr.js — وحدة OCR للصور المستقلة
 * تستدعي image_ocr_worker.py عبر child_process.spawn
 *
 * الاستخدام:
 *   const { extractTextFromImage } = require('./image_ocr');
 *   const result = await extractTextFromImage(imageBuffer, 'image/jpeg');
 */

const { spawn }  = require('child_process');
const path       = require('path');
const fs         = require('fs');
const os         = require('os');
const crypto     = require('crypto');

const WORKER_PATH   = path.join(__dirname, 'image_ocr_worker.py');
const OCR_TIMEOUT   = 60 * 1000; // دقيقة واحدة كحد أقصى للصورة

// كشف تلقائي لأمر Python
function detectPython() {
    const { spawnSync } = require('child_process');
    for (const cmd of ['python3', 'python']) {
        try {
            const r = spawnSync(cmd, ['--version'], { timeout: 3000, encoding: 'utf8' });
            if (!r.error && r.status === 0) return cmd;
        } catch (_) {}
    }
    return 'python3';
}
const PYTHON_CMD = detectPython();

/**
 * استخراج النص من صورة
 * @param {Buffer} imageBuffer - بيانات الصورة
 * @param {string} mimeType    - نوع الصورة (image/jpeg, image/png, ...)
 * @returns {Promise<{text: string, engine: string, confidence: number, hasText: boolean, formatted: string|null}>}
 */
async function extractTextFromImage(imageBuffer, mimeType = 'image/jpeg') {
    // تحديد امتداد الملف
    const extMap = {
        'image/jpeg':    '.jpg',
        'image/jpg':     '.jpg',
        'image/png':     '.png',
        'image/webp':    '.webp',
        'image/gif':     '.gif',
        'image/bmp':     '.bmp',
        'image/tiff':    '.tiff',
        'image/heic':    '.heic',
        'image/heif':    '.heif',
    };
    const ext     = extMap[mimeType?.toLowerCase()] || '.jpg';
    const tmpFile = path.join(os.tmpdir(), `ocr_${crypto.randomBytes(6).toString('hex')}${ext}`);

    // حفظ الصورة مؤقتاً
    fs.writeFileSync(tmpFile, imageBuffer);

    return new Promise((resolve, reject) => {
        let stdout   = '';
        let stderr   = '';
        let settled  = false;

        const worker = spawn(PYTHON_CMD, [WORKER_PATH, tmpFile, 'ara+eng'], {
            stdio: ['ignore', 'pipe', 'pipe']
        });

        worker.stdout.on('data', d => stdout += d.toString());
        worker.stderr.on('data', d => stderr += d.toString());

        const finishReject = (err) => {
            if (settled) return;
            settled = true;
            clearTimeout(timer);
            cleanup(tmpFile);
            reject(err);
        };
        const finishResolve = (value) => {
            if (settled) return;
            settled = true;
            clearTimeout(timer);
            cleanup(tmpFile);
            resolve(value);
        };

        const timer = setTimeout(() => {
            worker.kill('SIGTERM');
            finishReject(new Error('OCR انتهت المهلة (60 ثانية)'));
        }, OCR_TIMEOUT);

        worker.on('close', (code) => {
            if (settled) return;

            // تحليل stdout سطراً سطراً
            const lines = stdout.split('\n').filter(l => l.trim());
            let result  = null;

            for (const line of lines) {
                try {
                    const data = JSON.parse(line.trim());
                    if (data.type === 'result') {
                        result = data;
                    } else if (data.type === 'error' && data.fatal) {
                        finishReject(new Error(data.message));
                        return;
                    }
                } catch (_) {
                    // سطر debug — نتجاهله
                }
            }

            if (!result) {
                const errMsg = stderr.slice(-300) || `Worker خرج بكود ${code}`;
                finishReject(new Error(`OCR Worker فشل: ${errMsg}`));
                return;
            }

            // تنسيق النتيجة للمستخدم
            let formatted = null;
            if (result.has_text && result.text && result.text.trim()) {
                const engineEmoji = result.engine === 'easyocr' ? '🤖' : '🔍';
                const confPct     = Math.round((result.confidence || 0) * 100);
                formatted =
                    `📄 *النص المستخرج من الصورة:*\n` +
                    `─────────────────\n` +
                    result.text.trim() + '\n' +
                    `─────────────────\n` +
                    `_${engineEmoji} ${result.engine} | دقة: ${confPct}%_`;
            }

            finishResolve({
                text:       result.text       || '',
                engine:     result.engine     || 'none',
                confidence: result.confidence || 0,
                hasText:    result.has_text   || false,
                formatted:  formatted
            });
        });

        worker.on('error', (err) => {
            if (settled) return;
            if (err.code === 'ENOENT') {
                finishReject(new Error(`Python غير موجود — ثبّته: pkg install python`));
            } else {
                finishReject(new Error(`OCR Worker: ${err.message}`));
            }
        });
    });
}

function cleanup(filePath) {
    try { if (filePath && fs.existsSync(filePath)) fs.unlinkSync(filePath); } catch (_) {}
}

module.exports = { extractTextFromImage };
