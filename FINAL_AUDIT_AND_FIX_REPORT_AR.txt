'use strict';
const { spawn } = require('child_process');
const path = require('path');
const os = require('os');
const fs = require('fs');
const crypto = require('crypto');

const PYTHON_CMD = process.env.PYTHON_CMD || 'python3';
const WORKER_PATH = path.join(__dirname, 'file_maker_worker.py');
const TIMEOUT_MS = 90_000;

function safeName(name, ext) {
  const base = String(name || 'MedTerm').replace(/[\\/:*?"<>|\r\n]+/g, '_').slice(0, 80) || 'MedTerm';
  return base.endsWith(ext) ? base : base + ext;
}

function runMaker(format, title, text) {
  const ext = format === 'pdf' ? '.pdf' : '.txt';
  const output = path.join(os.tmpdir(), `medterm_${format}_${crypto.randomBytes(8).toString('hex')}${ext}`);
  return new Promise((resolve, reject) => {
    let stdoutBuf = '';
    let stderrBuf = '';
    let finished = false;
    const child = spawn(PYTHON_CMD, [WORKER_PATH, format, title || 'MedTerm', text || '', output], {
      stdio: ['ignore', 'pipe', 'pipe'],
      env: { ...process.env }
    });
    const timer = setTimeout(() => {
      if (finished) return;
      finished = true;
      try { child.kill('SIGTERM'); } catch (_) {}
      try { if (fs.existsSync(output)) fs.unlinkSync(output); } catch (_) {}
      reject(new Error(`انتهت مهلة إنشاء ${format.toUpperCase()}`));
    }, TIMEOUT_MS);
    child.stdout.on('data', d => { stdoutBuf += d.toString(); });
    child.stderr.on('data', d => { stderrBuf += d.toString(); });
    child.on('error', err => {
      if (finished) return;
      finished = true; clearTimeout(timer);
      try { if (fs.existsSync(output)) fs.unlinkSync(output); } catch (_) {}
      reject(new Error(`فشل تشغيل file_maker_worker: ${err.message}`));
    });
    child.on('close', code => {
      if (finished) return;
      finished = true; clearTimeout(timer);
      let last = null;
      for (const line of stdoutBuf.split('\n').filter(Boolean)) {
        try { const evt = JSON.parse(line); if (evt.type === 'done' || evt.type === 'error') last = evt; } catch (_) {}
      }
      if (code !== 0 || !last || last.type === 'error') {
        try { if (fs.existsSync(output)) fs.unlinkSync(output); } catch (_) {}
        return reject(new Error((last && last.message) || stderrBuf.slice(-300) || `worker خرج بكود ${code}`));
      }
      if (!fs.existsSync(output) || fs.statSync(output).size < 10) {
        try { if (fs.existsSync(output)) fs.unlinkSync(output); } catch (_) {}
        return reject(new Error('تم إنشاء ملف فارغ'));
      }
      resolve({ path: output, fileName: safeName(title, ext), meta: last });
    });
  });
}

module.exports = {
  createPDF: (title, text) => runMaker('pdf', title, text),
  createTXT: (title, text) => runMaker('txt', title, text),
};
