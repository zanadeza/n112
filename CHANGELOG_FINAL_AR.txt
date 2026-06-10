'use strict';

const {
    default: makeWASocket,
    useMultiFileAuthState,
    fetchLatestBaileysVersion,
    DisconnectReason,
    Browsers,
    downloadMediaMessage
} = require('@whiskeysockets/baileys');

const QRCode = require('qrcode-terminal');
const QRCodeImg = require('qrcode');
const { processPDF: _processPDF, checkDependencies: _checkPDFDeps } = require('./pdf_processor');
const { extractTextFromImage: _extractTextFromImage } = require('./image_ocr');
const { createPDF: _createPDF, createTXT: _createTXT } = require('./file_tools');
const { enhanceImage: _enhanceImage, generateLocalImage: _generateLocalImage } = require('./image_tools');
const http = require('http');
const fs   = require('fs');
const path = require('path');


// ============================================================
// CONFIG — القيم تُقرأ من متغيرات البيئة إن وُجدت، وإلا الافتراضي
// لتغييرها بدون تعديل الكود: export MISTRAL_API_KEY=xxx  في Termux
// ============================================================
const MISTRAL_API_KEY = process.env.MISTRAL_API_KEY;

if (!MISTRAL_API_KEY) {
    console.error('❌ خطأ فادح: MISTRAL_API_KEY غير مُعيَّن!');
    console.error('   عيّنه عبر: export MISTRAL_API_KEY=your_key_here');
    process.exit(1);
}
const ADMIN_NUMBER    = process.env.ADMIN_NUMBER;   // بدون + أو @
if (!ADMIN_NUMBER) {
    console.error('❌ خطأ فادح: ADMIN_NUMBER غير مُعيَّن!');
    console.error('   عيّنه عبر: export ADMIN_NUMBER=972xxxxxxxx');
    process.exit(1);
}
const BOT_NAME        = 'MedTerm';
const VOXTRAL_MODEL   = process.env.VOXTRAL_MODEL || 'voxtral-small-2507'; // قابل للتغيير: export VOXTRAL_MODEL=xxx
const DATA_FILE       = './bot_data.json';
const WEB_PORT        = parseInt(process.env.WEB_PORT || '8080', 10);
const PDF_CACHE_DIR   = './pdf_cache';  // مجلد حفظ كاش ملفات PDF

// ============================================================
// DASHBOARD AUTH — عيّنهما عبر: export DASHBOARD_USER=xxx && export DASHBOARD_PASS=xxx
// ============================================================
const DASHBOARD_USER  = process.env.DASHBOARD_USER;
const DASHBOARD_PASS  = process.env.DASHBOARD_PASS;

if (!DASHBOARD_USER || !DASHBOARD_PASS) {
    console.warn('⚠️  تحذير: DASHBOARD_USER أو DASHBOARD_PASS غير مُعيَّنَين — لوحة التحكم معطلة.');
    console.warn('   عيّنهما عبر: export DASHBOARD_USER=xxx && export DASHBOARD_PASS=xxx');
}
const crypto          = require('crypto');
// sessions: { token: { ip, createdAt } }
const _sessions       = {};
const SESSION_TTL_MS  = 12 * 60 * 60_000; // 12 ساعة

function generateToken() {
    return crypto.randomBytes(32).toString('hex');
}
function isValidSession(req) {
    const token = parseCookie(req.headers['cookie'] || '')['adm_tok'];
    if (!token || !_sessions[token]) return false;
    const s = _sessions[token];
    if (Date.now() - s.createdAt > SESSION_TTL_MS) {
        delete _sessions[token];
        return false;
    }
    // التحقق من User-Agent كـ fingerprint إضافي (لا نفحص IP لأن Ngrok يغيّره)
    const ua = req.headers['user-agent'] || '';
    if (s.ua && s.ua !== ua) {
        // User-Agent تغيّر — جلسة مشبوهة، نحذفها
        console.warn('[session] UA mismatch — possible session hijack attempt');
        delete _sessions[token];
        return false;
    }
    s.createdAt = Date.now(); // تجديد تلقائي عند كل طلب
    return true;
}
function parseCookie(str) {
    const out = {};
    for (const part of String(str || '').split(';')) {
        const idx = part.indexOf('=');
        if (idx === -1) continue;
        try {
            const key = decodeURIComponent(part.slice(0, idx).trim());
            const val = decodeURIComponent(part.slice(idx + 1).trim());
            if (key) out[key] = val;
        } catch (_) {
            // تجاهل cookie غير صالحة بدل إسقاط الطلب بالكامل
        }
    }
    return out;
}
// تنظيف sessions منتهية كل ساعة
setInterval(() => {
    const now = Date.now();
    for (const tok of Object.keys(_sessions))
        if (now - _sessions[tok].createdAt > SESSION_TTL_MS) delete _sessions[tok];
}, 60 * 60_000);

// حماية Brute-Force على صفحة الدخول: أقصى 10 محاولات/10 دقائق لكل IP
const _loginAttempts = {};
function checkLoginBrute(ip) {
    const now = Date.now();
    if (!_loginAttempts[ip]) _loginAttempts[ip] = { count: 0, first: now };
    const a = _loginAttempts[ip];
    if (now - a.first > 10 * 60_000) { a.count = 0; a.first = now; }
    a.count++;
    return a.count <= 10;
}
function resetLoginBrute(ip) {
    delete _loginAttempts[ip];
}
setInterval(() => {
    const now = Date.now();
    for (const ip of Object.keys(_loginAttempts))
        if (now - _loginAttempts[ip].first > 10 * 60_000) delete _loginAttempts[ip];
}, 5 * 60_000);

let currentQR = null;
let isConnected = false;
const MAX_HISTORY     = 30;              // أقصى رسائل في السياق
const API_TIMEOUT_MS  = 120_000;        // 120 ثانية timeout للـ API (pixtral-large يحتاج وقت أكثر)

// ============================================================
// PERSISTENCE
// ============================================================
function loadData() {
    try {
        if (fs.existsSync(DATA_FILE))
            return JSON.parse(fs.readFileSync(DATA_FILE, 'utf-8'));
    } catch (e) {
        try {
            const backup = `${DATA_FILE}.corrupt.${Date.now()}`;
            fs.copyFileSync(DATA_FILE, backup);
            console.error(`[loadData] bot_data.json تالف، تم حفظ نسخة: ${backup}`);
        } catch (_) {}
    }
    return {
        userNames:      {},
        welcomedUsers:  {},
        vipNumbers:     [],
        vipExpiry:      {},   // { sender: timestamp_ms } — تاريخ انتهاء VIP
        reports:        [],
        userLimits:     {},   // حدود مخصصة لكل مستخدم { sender: limit }
        userLimitsUsage: {},  // استهلاك كل مستخدم { sender: { messages, images, docs, tts, activatedAt } }
        trialNotified:  {},  // { sender: true } — أُرسلت له رسالة الفترة المجانية
        blacklist:      [],   // أرقام محظورة
        userLanguages:  {},   // لغة كل مستخدم { sender: 'ar'|'en'|... }
        stats:          { totalMessages: 0, totalImages: 0, totalMedical: 0, totalDocs: 0 }
    };
}

let _saveTimer = null;
function saveData() {
    // debounce: تأخير 200ms
    if (_saveTimer) clearTimeout(_saveTimer);
    _saveTimer = setTimeout(() => {
        try {
            const tmp = DATA_FILE + '.tmp';
            fs.writeFileSync(tmp, JSON.stringify(
                { userNames, welcomedUsers, vipNumbers, vipExpiry, reports, userLimits, userLimitsUsage, trialNotified, blacklist, userLanguages, stats },
                null, 2
            ));
            fs.renameSync(tmp, DATA_FILE);
        } catch (e) {
            console.error('[saveData] خطأ:', e.message);
        }
    }, 500);
}

let { userNames, welcomedUsers, vipNumbers, vipExpiry, reports, userLimits, userLimitsUsage, trialNotified, blacklist, userLanguages, stats } = loadData();

// ضمان وجود الحقول
if (!Array.isArray(reports))   reports = [];
if (!userLimits)               userLimits = {};
if (!userLimitsUsage)          userLimitsUsage = {};
if (!trialNotified)            trialNotified = {};
if (!Array.isArray(blacklist)) blacklist = [];
if (!userLanguages)            userLanguages = {};
if (!vipExpiry)                vipExpiry = {};
if (!stats)                    stats   = { totalMessages: 0, totalImages: 0, totalMedical: 0, totalDocs: 0 };
if (!stats.totalMessages)      stats.totalMessages = 0;
if (!stats.totalImages)        stats.totalImages   = 0;
if (!stats.totalMedical)       stats.totalMedical  = 0;
if (!stats.totalDocs)          stats.totalDocs     = 0;

// إضافة الأدمن للمستخدمين المرحّب بهم تلقائياً حتى لا يستقبل رسالة ترحيب
welcomedUsers[ADMIN_NUMBER] = true;
saveData();

let userChats       = {};   // سياق المحادثة (RAM فقط)
let userChatLastSeen = {}; // آخر نشاط لكل مستخدم
let userModes       = {};  // وضع كل مستخدم: 'terms' | 'pharma' | 'medai' | 'openai' | null
let userTTSPending  = {};  // { sender: { term, lang, expiresAt } } — انتظار "نعم" لإرسال النطق
let userPdfContext  = {};  // { sender: { fileName, docText } } — محتوى PDF النشط
let userPdfPending  = {};  // { sender: { fileName, docText, expiresAt } } — انتظار إذن المستخدم
let userExamMode    = {};  // { sender: { fileName, pages, docText, loadedAt } } — نظام الاختبارات (VIP فقط)
let userExamPending = {};  // { sender: true } — ينتظر PDF لنظام الاختبارات
let sock            = null;
let isReconnecting  = false; // منع الاتصال المتعدد

// تنظيف الذاكرة: احتفظ بآخر 800 جلسة الأكثر نشاطاً (LRU)
function cleanMemory() {
    const keys = Object.keys(userChats);
    if (keys.length > 800) {
        const sorted = keys.sort((a, b) => (userChatLastSeen[a] || 0) - (userChatLastSeen[b] || 0));
        const toDelete = sorted.slice(0, keys.length - 800);
        toDelete.forEach(k => { delete userChats[k]; delete userChatLastSeen[k]; });
        console.log(`[cleanMemory] حُذف ${toDelete.length} جلسة قديمة (LRU)`);
    }
    // تنظيف userPdfContext و userExamMode القديمة (أكثر من 6 ساعات)
    const SIX_HOURS = 6 * 60 * 60_000;
    const now = Date.now();
    for (const k of Object.keys(userPdfContext)) {
        if ((userPdfContext[k]?.loadedAt || 0) && now - userPdfContext[k].loadedAt > SIX_HOURS)
            delete userPdfContext[k];
    }
    for (const k of Object.keys(userExamMode)) {
        if ((userExamMode[k]?.loadedAt || 0) && now - userExamMode[k].loadedAt > SIX_HOURS)
            delete userExamMode[k];
    }
    // ── تنظيف Objects لم تكن تُنظَّف (تحذير #5) ──
    for (const k of Object.keys(userTTSPending)) {
        if (now > (userTTSPending[k]?.expiresAt || 0)) delete userTTSPending[k];
    }
    for (const k of Object.keys(userPdfPending)) {
        if (now > (userPdfPending[k]?.expiresAt || 0)) delete userPdfPending[k];
    }
    // userModes لا تحتوي على expiresAt — نحذف مستخدمين غير نشطين (لا جلسة لهم)
    for (const k of Object.keys(userModes)) {
        if (!userChats[k] && !userChatLastSeen[k]) delete userModes[k];
    }
    // userExamPending — حالة انتظار مؤقتة، نحذف القديمة بنفس معيار userModes
    for (const k of Object.keys(userExamPending)) {
        if (!userChats[k] && !userChatLastSeen[k]) delete userExamPending[k];
    }
}


// ============================================================
// PDF CACHE — حفظ واسترجاع نص PDF لتجنب إعادة الاستخراج
// ============================================================
function pdfCacheKey(fileName, buffer) {
    // مفتاح مزدوج: اسم الملف + هاش MD5 للمحتوى (أدق)
    const hash = crypto.createHash('md5').update(buffer).digest('hex').slice(0, 16);
    const safeName = fileName.replace(/[^a-zA-Z0-9\u0600-\u06FF._-]/g, '_').slice(0, 60);
    return `${safeName}__${hash}`;
}

function pdfCachePath(key) {
    return path.join(PDF_CACHE_DIR, `${key}.json`);
}

function pdfCacheGet(key) {
    try {
        const p = pdfCachePath(key);
        if (!fs.existsSync(p)) return null;
        const data = JSON.parse(fs.readFileSync(p, 'utf-8'));
        // تحقق من صحة البيانات
        if (!data || !data.docText) return null;
        return data; // { fileName, docText, pageCount, savedAt }
    } catch (_) { return null; }
}

function pdfCacheSet(key, fileName, docText, pageCount) {
    try {
        if (!fs.existsSync(PDF_CACHE_DIR))
            fs.mkdirSync(PDF_CACHE_DIR, { recursive: true });
        const p = pdfCachePath(key);
        fs.writeFileSync(p, JSON.stringify({
            fileName,
            docText,
            pageCount,
            savedAt: Date.now()
        }, null, 2));
        console.log(`[pdfCache] حُفظ: ${key} (${Math.round(docText.length/1024)}KB نص)`);
    } catch (e) {
        console.error('[pdfCache] خطأ في الحفظ:', e.message);
    }
}

// تنظيف كاش PDF القديمة (أكثر من 30 يوم) — يُستدعى عند بدء التشغيل وكل 24 ساعة
function cleanPdfCache() {
    try {
        if (!fs.existsSync(PDF_CACHE_DIR)) return;
        const THIRTY_DAYS = 30 * 24 * 60 * 60_000;
        const now = Date.now();
        let deleted = 0;
        for (const f of fs.readdirSync(PDF_CACHE_DIR)) {
            if (!f.endsWith('.json')) continue;
            try {
                const p = path.join(PDF_CACHE_DIR, f);
                const data = JSON.parse(fs.readFileSync(p, 'utf-8'));
                if (now - (data.savedAt || 0) > THIRTY_DAYS) {
                    fs.unlinkSync(p);
                    deleted++;
                }
            } catch (_) {}
        }
        if (deleted > 0) console.log(`[pdfCache] تنظيف: حُذف ${deleted} ملف قديم`);
    } catch (e) {
        console.error('[pdfCache] خطأ في التنظيف:', e.message);
    }
}
setInterval(cleanPdfCache, 24 * 60 * 60_000);

// فحص دوري لانتهاء اشتراكات VIP
function checkVIPExpiry() {
    // isActiveVIP() تتولى إزالة المنتهين وإشعارهم عند أي تفاعل.
    // هذه الدالة تُعالج فقط المنتهين الصامتين (لم يتفاعلوا مؤخراً)
    // دون إرسال رسالة إضافية لتجنب تكرار الإشعار.
    const now = Date.now();
    let changed = false;
    for (const num of [...vipNumbers]) {
        const expiry = vipExpiry[num];
        if (expiry && now > expiry) {
            vipNumbers = vipNumbers.filter(n => n !== num);
            delete vipExpiry[num];
            changed = true;
            console.log(`[VIP] انتهى اشتراك المستخدم ${num} (تنظيف دوري)`);
            // لا نرسل رسالة هنا — isActiveVIP() ترسلها عند تفاعل المستخدم التالي
        }
    }
    if (changed) saveData();
}

// ============================================================
// PURE HELPERS (no dependencies — must come before SYSTEM PROMPTS)
// ============================================================

// توقيت القدس — آمن على Termux، لا يعتمد على بناء Date من string مع timezone
function nowJerusalem() {
    const now = new Date();
    const formatter = new Intl.DateTimeFormat('en-US', {
        timeZone: 'Asia/Jerusalem',
        year: 'numeric', month: '2-digit', day: '2-digit',
        hour: '2-digit', minute: '2-digit', second: '2-digit',
        weekday: 'short',
        hour12: false
    });
    const parts = formatter.formatToParts(now);
    const g = type => parts.find(p => p.type === type)?.value || '00';

    const year  = parseInt(g('year'),  10);
    const month = parseInt(g('month'), 10) - 1; // 0-based
    const day   = parseInt(g('day'),   10);
    const hour  = parseInt(g('hour'),  10);
    const min   = parseInt(g('minute'),10);
    const sec   = parseInt(g('second'),10);

    const weekdayShort = g('weekday'); // "Sun","Mon",...
    const weekdayMap   = { Sun:0, Mon:1, Tue:2, Wed:3, Thu:4, Fri:5, Sat:6 };
    const dayOfWeek    = weekdayMap[weekdayShort] ?? new Date(year, month, day).getDay();

    return {
        getFullYear: () => year,
        getMonth:    () => month,
        getDate:     () => day,
        getDay:      () => dayOfWeek,
        getHours:    () => hour,
        getMinutes:  () => min,
        getSeconds:  () => sec,
        toLocaleTimeString: (_loc, opts) => {
            const h = String(hour).padStart(2, '0');
            const m = String(min).padStart(2, '0');
            if (opts && opts.hour === '2-digit' && opts.minute === '2-digit') return h + ':' + m;
            return h + ':' + m + ':' + String(sec).padStart(2, '0');
        }
    };
}

// ============================================================
// RATE LIMITING & DAILY MESSAGE QUOTA
// ============================================================
const DAILY_MSG_LIMIT   = 20; // رسائل نصية/شهر لكل مستخدم عادي
const DAILY_IMG_LIMIT   = 5;  // صور شهرياً للمستخدم العادي
const DAILY_TTS_LIMIT   = 10; // تسجيلات/صوت شهرياً للمستخدم العادي
const BLACKLIST_MSG   = '⛔ عذراً، تم حظرك من استخدام هذا البوت.\nللاستفسار تواصل مع المهندس نادر:\n👤 wa.me/972593850520'; // رسالة للمحظورين
// ============================================================
// نظام الحدود الدائمة — لا تتجدد تلقائياً، تُحفظ في bot_data.json
// التجديد فقط: الأدمن يعطي VIP أو يرفع الحد يدوياً
// ============================================================

// الحد الشهري الفعلي: مخصص إن وُجد، وإلا الافتراضي
function getUserDailyLimit(sender) {
    return (userLimits && userLimits[sender] != null) ? userLimits[sender] : DAILY_MSG_LIMIT;
}

function currentMonthKey() {
    const d = nowJerusalem();
    return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}`;
}

// جلب سجل الاستهلاك الشهري من الذاكرة الدائمة
function getDailyRecord(sender) {
    const monthKey = currentMonthKey();
    if (!userLimitsUsage[sender] || userLimitsUsage[sender].monthKey !== monthKey) {
        userLimitsUsage[sender] = { messages: 0, images: 0, docs: 0, tts: 0, activatedAt: Date.now(), monthKey };
        saveData();
    }
    return userLimitsUsage[sender];
}

// إعادة تصفير استهلاك مستخدم (يستدعيها الأدمن فقط عند رفع الحد أو إضافة VIP)
function resetUserUsage(sender) {
    userLimitsUsage[sender] = { messages: 0, images: 0, docs: 0, tts: 0, activatedAt: Date.now(), monthKey: currentMonthKey() };
    saveData();
}

// فحص الحد للرسائل النصية — يُعيد { allowed, remaining, commit }
function checkDailyMessages(sender) {
    const limit = getUserDailyLimit(sender);
    const rec = getDailyRecord(sender);
    if (rec.messages >= limit) return { allowed: false, remaining: 0, limit, commit: () => {} };
    const remaining = limit - rec.messages - 1;
    const commit = () => { rec.messages++; saveData(); };
    return { allowed: true, remaining, limit, commit };
}

// فحص حد الـ TTS — يُعيد { allowed, remaining, commit }
function checkDailyTTS(sender) {
    const d = getDailyRecord(sender);
    if (d.tts >= DAILY_TTS_LIMIT) return { allowed: false, remaining: 0 };
    const remaining = DAILY_TTS_LIMIT - d.tts - 1;
    return { allowed: true, remaining, commit: () => { d.tts++; saveData(); } };
}

// فحص حد الصور والملفات
function checkDailyLimit(sender, type) {
    const d = getDailyRecord(sender);
    if (type === 'image') { if (d.images >= DAILY_IMG_LIMIT) return false; d.images++; saveData(); return true; }
    if (type === 'pdf' || type === 'txt')   { if (d.docs   >= 10) return false; d.docs++; saveData(); return true; }
    return true;
}

// التحقق من VIP مع الانتهاء التلقائي
function isActiveVIP(sender) {
    if (!vipNumbers.includes(sender)) return false;
    const expiry = vipExpiry[sender];
    if (!expiry) return true; // بدون تاريخ انتهاء = دائم
    if (Date.now() > expiry) {
        // انتهى الاشتراك — إزالة تلقائية
        vipNumbers = vipNumbers.filter(n => n !== sender);
        delete vipExpiry[sender];
        saveData();
        // إشعار المستخدم
        if (sock && isConnected) {
            sock.sendMessage(`${sender}@s.whatsapp.net`, {
                text: '⚠️ انتهت صلاحية اشتراكك المميز (VIP).\n\nللتجديد تواصل مع المهندس نادر:\n👤 wa.me/972593850520'
            }).catch(() => {});
        }
        return false;
    }
    return true;
}
// ============================================================
// RATE LIMITING — متعدد المستويات
// المستوى 1: Anti-spam قصير (3 رسائل / 5 ثوانٍ)
// المستوى 2: حد دقيقي    (20 رسالة / دقيقة)
// المستوى 3: حد ساعي     (100 رسالة / ساعة)
// ============================================================
const _spamCheck   = {}; // timestamps في آخر 5 ثوانٍ
const _minLimit    = {}; // timestamps في آخر 60 ثانية
const _hourLimit   = {}; // timestamps في آخر ساعة

function checkSpam(sender) {
    const now = Date.now();

    // المستوى 1: 3 رسائل / 5 ثوانٍ
    if (!_spamCheck[sender]) _spamCheck[sender] = [];
    _spamCheck[sender] = _spamCheck[sender].filter(t => now - t < 5_000);
    if (_spamCheck[sender].length >= 3) return false;
    _spamCheck[sender].push(now);

    // المستوى 2: 20 رسالة / دقيقة
    if (!_minLimit[sender]) _minLimit[sender] = [];
    _minLimit[sender] = _minLimit[sender].filter(t => now - t < 60_000);
    if (_minLimit[sender].length >= 20) return false;
    _minLimit[sender].push(now);

    // المستوى 3: 100 رسالة / ساعة
    if (!_hourLimit[sender]) _hourLimit[sender] = [];
    _hourLimit[sender] = _hourLimit[sender].filter(t => now - t < 3_600_000);
    if (_hourLimit[sender].length >= 100) return false;
    _hourLimit[sender].push(now);

    return true;
}

// تنظيف سجلات anti-spam كل ساعة
setInterval(() => {
    const now = Date.now();
    for (const k of Object.keys(_spamCheck)) {
        _spamCheck[k] = (_spamCheck[k] || []).filter(t => now - t < 10_000);
        if (!_spamCheck[k].length) delete _spamCheck[k];
    }
    for (const k of Object.keys(_minLimit)) {
        _minLimit[k] = (_minLimit[k] || []).filter(t => now - t < 60_000);
        if (!_minLimit[k].length) delete _minLimit[k];
    }
    for (const k of Object.keys(_hourLimit)) {
        _hourLimit[k] = (_hourLimit[k] || []).filter(t => now - t < 3_600_000);
        if (!_hourLimit[k].length) delete _hourLimit[k];
    }
    // userLimitsUsage محفوظ دائماً — لا تنظيف تلقائي
}, 60 * 60_000);

// ============================================================
// SYSTEM PROMPTS
// ============================================================
let _cachedSystemPrompt = null;
let _cachedSystemPromptTime = 0;
function getSystemPrompt() {
    const nowMs = Date.now();
    if (_cachedSystemPrompt && nowMs - _cachedSystemPromptTime < 10 * 60_000) // 10 دقائق
        return _cachedSystemPrompt;
    _cachedSystemPromptTime = nowMs;
    const now = nowJerusalem();
    const days = ['الأحد','الاثنين','الثلاثاء','الأربعاء','الخميس','الجمعة','السبت'];
    const months = ['يناير','فبراير','مارس','أبريل','مايو','يونيو','يوليو','أغسطس','سبتمبر','أكتوبر','نوفمبر','ديسمبر'];
    const dateStr = `${days[now.getDay()]} ${now.getDate()} ${months[now.getMonth()]} ${now.getFullYear()}`;
    const timeStr = now.toLocaleTimeString('ar-SA', { hour: '2-digit', minute: '2-digit' });
    _cachedSystemPrompt = `اسمك "MedTerm"، مساعد ذكاء اصطناعي شامل على واتساب.\n\nالتاريخ والوقت الحالي: ${dateStr} - الساعة ${timeStr} (بتوقيت القدس)\nاستخدم هذا التاريخ دائماً عند أي سؤال عن اليوم أو التاريخ أو السنة، ولا تعتمد على معلوماتك القديمة أبداً.\n\nشخصيتك:\n- مساعد شامل ومتعدد المعرفة: تجيب على أي سؤال في أي مجال بدون استثناء\n- جدي ومهني، ردودك دقيقة ومباشرة بدون حشو أو مقدمات زائدة\n- اللغة الافتراضية عربية واضحة وسهلة، وإذا طلب المستخدم لغة أخرى تحدّث بها فوراً\n- أعطِ المعلومة الكاملة والصحيحة من أول رد\n- نظّم ردودك بشكل واضح: استخدم النقاط والعناوين والترقيم عند الحاجة لتسهيل القراءة\n- لا تستخدم مصطلحات أجنبية غير ضرورية\n- اقرأ سياق المحادثة كاملاً وربط الرسائل ببعضها قبل الرد\n- إذا سُئلت عن اسمك قل: أنا MedTerm، مساعد ذكاء اصطناعي\n- تتعامل مع جميع المستخدمين باحترام بغض النظر عن جنسيتهم أو لغتهم\n\nمجالات خبرتك (غير محدودة):\n- الطب والصحة: معلومات دقيقة، أدوية، جرعات، أعراض، تشخيص أولي — مع التنبيه بمراجعة الطبيب للحالات الجدية\n- العلوم والتقنية: برمجة، ذكاء اصطناعي، رياضيات، فيزياء، كيمياء\n- القانون والأعمال: معلومات عامة، عقود، ريادة أعمال، تسويق\n- التاريخ والجغرافيا والثقافة العامة\n- الدين والفقه: إجابات موضوعية ومتوازنة\n- الأدب والكتابة والترجمة\n- الطبخ والسفر ونمط الحياة\n- أي موضوع آخر يسألك عنه المستخدم\n\nقاعدة ذهبية: لا تقل أبداً "هذا خارج نطاق تخصصي" — أجب على كل سؤال بأفضل ما لديك.\n\nاسم المستخدم موجود في السياق، استخدمه أحياناً بشكل طبيعي.`
    return _cachedSystemPrompt;
}

// ============================================================
// SYSTEM PROMPTS للأنظمة الأربعة
// ============================================================
const MODE_PROMPTS = {
    terms: `أنت متخصص حصري في المصطلحات الطبية لجميع التخصصات الطبية. مهمتك الوحيدة هي شرح المصطلحات الطبية بشكل علمي ومتكامل.

══════════════════════════════
قواعد النظام — يجب اتباعها بدقة تامة:
══════════════════════════════

1) اقبل أي إدخال له صلة بالمجال الطبي والصحي بأي شكل كان:
— مصطلح طبي رسمي: Meningitis · التهاب السحايا · Tachycardia
— كلمة طبية بسيطة: قلب · كبد · دم · عظم · heart · liver
— اسم مرض أو حالة: سكري · ضغط · ربو · سرطان · diabetes
— اسم عضو أو جهاز: بنكرياس · الغدة الدرقية · pancreas
— اسم دواء أو علاج: بنسيلين · أسبرين · penicillin
— إجراء أو فحص: تحليل دم · رنين · MRI · خزعة
— أعراض: صداع · ألم صدر · headache
— أي كلمة أو مفهوم أو تعريف في المجال الطبي والصحي

إذا أرسل المستخدم شيئاً لا علاقة له بالطب أو الصحة نهائياً (مثل: أسعار، طبخ، رياضة، سياسة، برمجة) رد بهذه الرسالة الثابتة فقط:

⚠️ هذا النظام مخصص للمجال الطبي والصحي فقط.
الرجاء إرسال أي كلمة أو مصطلح أو مفهوم طبي.

📋 أمثلة:
بالعربية: قلب · سكري · التهاب · ضغط · كبد · سرطان · أسبرين · رنين مغناطيسي
In English: heart · diabetes · inflammation · MRI · cancer · aspirin · fever

💡 لتغيير النظام اكتب: !قائمة

══════════════════════════════

2) إذا أرسل المستخدم أي كلمة أو مصطلح أو مفهوم له علاقة بالطب والصحة (بالعربية أو الإنجليزية أو كليهما)، اردّ بهذا النموذج الكامل والثابت بالضبط دون أي تغيير في الترتيب أو الشكل أو الأيقونات:

📌 المصطلح
[المصطلح بالإنجليزية]
[المصطلح بالعربية]

🌐 أصل المصطلح
[اذكر هنا: يوناني / لاتيني / إنجليزي] من "[الكلمة الأصلية]" = [المعنى الحرفي] · "[الجزء الثاني إن وجد]" = [المعنى]

🗣️ النطق — Pronunciation
بالعربية: [النطق الصوتي الكامل بمقاطع واضحة مثال: مِـنِـنْـجَـايْـتِـس]
In English: [English phonetic pronunciation مثال: men·in·JY·tis]
🔤 IPA: [/الرموز الصوتية الدولية/]

🔸 المعنى المختصر
[تعريف دقيق ومختصر في سطر أو سطرين]

📖 الشرح الطبي
🔹 بالعربية: [فقرة علمية واضحة تشمل: ما هو المصطلح، أسبابه الرئيسية، أعراضه، وأهميته السريرية — بشكل مدمج ومتدفق وليس نقاطاً]
🔹 In English: [Clear scientific paragraph covering: what it is, main causes, symptoms, and clinical significance — flowing text not bullet points]

━━ 🧬 تحليل المصطلح ━━
🌱 الجذر: [الجذر]←[معنى الجذر بالعربية / meaning in English]
⬅️ البادئة: [البادئة إن وجدت، وإلا اكتب: لا يوجد]
➡️ اللاحقة: [اللاحقة]←[معناها بالعربية / meaning]
📐 [البادئة +] [الجذر] + [اللاحقة] = [المعنى الحرفي الكامل]

🫀 الجهاز المصاب
[الجهاز أو الأعضاء المصابة]

🦠 الأمراض المرتبطة
[اذكر 4-5 أمراض مرتبطة بصيغة:
Disease Name — الاسم بالعربية: وصف مختصر جداً]

⚕️ التخصص الطبي
[التخصصات الطبية المعنية]

🔗 مصطلحات مرتبطة
[5 مصطلحات بصيغة: Term — الترجمة العربية]

💡 نصائح سريعة
• [نصيحة 1]
• [نصيحة 2]
• [نصيحة 3]

📝 أمثلة تطبيقية
بالعربية: [جملة كاملة في سياق طبي حقيقي]
In English: [Full sentence in a real medical context]

🎓 Learning Note
[فقرة تعليمية بالإنجليزية للطلاب والمهنيين الصحيين: الأهمية السريرية وكيفية التمييز والممارسة العملية]`,

    pharma: `أنت صيدلاني متخصص وخبير في علم الأدوية لجميع التخصصات الطبية. أجب على أي سؤال يتعلق بالأدوية.
عندما يسأل المستخدم عن دواء أو مادة فعّالة، اردّ بهذا النموذج الثابت:

⭐ [الاسم العلمي بالإنجليزية] — [الاسم بالعربية]

---

1) الاسم العلمي — Scientific Name
العربية: [الاسم]
English: [The name]

---

2) الأسماء التجارية — Trade Names
[قائمة الأسماء التجارية المشهورة]

---

3) التصنيف الدوائي — Drug Class
العربية: [التصنيف]
English: [Classification]

---

4) آلية العمل — Mechanism of Action
العربية: [الشرح]
English: [Explanation]

---

5) الاستخدامات — Indications
العربية: [القائمة]
English: [List]

---

6) الجرعة — Dosage
العربية: [الجرعة المعتادة]
English: [Usual dose]

---

7) الآثار الجانبية — Side Effects
العربية: [الشائعة والخطيرة]
English: [Common & serious]

---

8) موانع الاستعمال — Contraindications
العربية: [القائمة]
English: [List]

---

9) التفاعلات الدوائية — Drug Interactions
العربية: [أهم التفاعلات]
English: [Key interactions]

---

⚠️ تنبيه: هذه المعلومات للتثقيف الصحي فقط. استشر طبيبك أو صيدلانيك قبل أخذ أي دواء.

────────────
💡 لتغيير النظام اكتب: !قائمة`,

    medai: `أنت مساعد طبي ذكي ومتخصص في جميع التخصصات الطبية والعلوم الصحية. أجب على أي سؤال طبي بدقة ومهنية.
تشمل تخصصاتك: الطب العام، الجراحة، الأمراض الباطنية، طب الأطفال، أمراض النساء والتوليد، الأمراض العصبية، أمراض القلب، الأمراض الجلدية، طب العيون، طب الأسنان، الطب النفسي، التغذية والصحة العامة، وجميع التخصصات الأخرى.
إذا سأل المستخدم عن موضوع خارج المجال الطبي تماماً، رد بـ:
"⚕️ هذا النظام مخصص للمجال الطبي فقط. يرجى طرح أسئلة طبية أو صحية.
💡 للانتقال لنظام مفتوح اكتب: !قائمة"
اختم ردودك الطبية دائماً بـ: "⚠️ للتشخيص والعلاج يجب مراجعة طبيب متخصص."
────────────
💡 لتغيير النظام اكتب: !قائمة`,

    openai: null  // يستخدم getSystemPrompt() الأصلي
};

// رسالة الترحيب للمستخدمين الجدد
function buildModeMenu(name) {
    const first = name ? name.split(' ')[0] : null;
    const greeting = first ? `أهلاً ${first}` : 'أهلاً';
    return `${greeting} 👋\n\n*مرحباً بك في بوت MedTerm الطبي!*\n\n` +
        `يمكنني مساعدتك في:\n` +
        `🏥 شرح المصطلحات الطبية بالعربي والإنجليزي\n` +
        `💊 معلومات شاملة عن الأدوية والمستحضرات\n` +
        `⚕️ الإجابة على الأسئلة الطبية والصحية\n` +
        `🤖 المساعدة في أي موضوع عام\n` +
        `🖼️ تحليل الصور والتقارير الطبية\n` +
        `📄 قراءة وتحليل ملفات PDF\n` +
        `🔊 نطق المصطلحات الطبية صوتياً\n` +
        `🌐 الترجمة مع الصوت والنطق\n\n` +
        `─────────────────\n` +
        `✍️ *فقط أرسل سؤالك وسأرد عليك مباشرة!*`;
}

// اسم وأيقونة النظام الحالي
function getModeName(mode) {
    const names = {
        terms:  '📌 المصطلحات الطبية',
        pharma: '💊 علم الأدوية',
        medai:  '⚕️ ذكاء صناعي طبي',
        openai: '🤖 ذكاء صناعي عام'
    };
    return names[mode] || 'غير محدد';
}

// System prompt للنظام الحالي
function getModeSystemPrompt(mode) {
    if (!mode || mode === 'openai') return getSystemPrompt();
    const base = MODE_PROMPTS[mode];
    if (!base) return getSystemPrompt();
    // نضيف معلومات التاريخ والوقت من getSystemPrompt
    const now = nowJerusalem();
    const days = ['الأحد','الاثنين','الثلاثاء','الأربعاء','الخميس','الجمعة','السبت'];
    const months = ['يناير','فبراير','مارس','أبريل','مايو','يونيو','يوليو','أغسطس','سبتمبر','أكتوبر','نوفمبر','ديسمبر'];
    const dateStr = `${days[now.getDay()]} ${now.getDate()} ${months[now.getMonth()]} ${now.getFullYear()}`;
    const timeStr = now.toLocaleTimeString('ar-SA', { hour: '2-digit', minute: '2-digit' });
    return `${base}\n\nالتاريخ والوقت الحالي: ${dateStr} - الساعة ${timeStr} (بتوقيت القدس)`;
}

// كشف إذا كانت الرسالة تتعلق بالمجال الطبي
function isMedicalQuery(text) {
    const t = text || '';
    if (_RX_MEDICAL_QUERY.test(t)) return true;
    if (_RX_MEDICAL_BP.test(t))    return true;
    if (_RX_MEDICAL_BLOOD.test(t)) return true;
    return false;
}

// اختيار System Prompt الذكي بناءً على محتوى الرسالة
// مع كاش بسيط: المحتوى الطبي له prompt ثابت، نخزّنه لتجنب إعادة البناء
const _smartPromptCache = { medical_ar: null, general_ar: null };
function getSmartSystemPrompt(text, userLang) {
    const langSuffix = (userLang && userLang !== 'ar')
        ? `\n\nمهم: يجب أن تجيب على هذا المستخدم بلغة "${userLang}" فقط، حتى لو كتب بالعربية.`
        : '';
    const isMedical = isMedicalQuery(text);

    if (isMedical) {
        // موجّه طبي — ثابت، يُبنى مرة واحدة لكل لغة
        if (!userLang || userLang === 'ar') {
            if (!_smartPromptCache.medical_ar)
                _smartPromptCache.medical_ar = getModeSystemPrompt('medai');
            return _smartPromptCache.medical_ar;
        }
        return getModeSystemPrompt('medai') + langSuffix;
    }

    // موجّه عام — يحتاج التاريخ والوقت، لا نخزّنه بشكل مستقل (getSystemPrompt له كاشه)
    return getSystemPrompt() + langSuffix;
}

const MEDICAL_IMAGE_PROMPT = `أنت طبيب متخصص ومحلل صور طبية خبير. حلّل الصورة الطبية بدقة عالية.

اتبع هذا الترتيب في ردك:
1. نوع الصورة: أشعة X أو CT أو MRI أو تحليل دم أو تقرير مخبري
2. الملاحظات الرئيسية: ما تلاحظه بوضوح
3. التفسير الطبي: ماذا تعني هذه الملاحظات
4. الحالة: هل القيم ضمن المعدل الطبيعي أم لا، وضّح بوضوح
5. التوصية: الخطوة التالية المقترحة

قواعد:
- كن دقيقاً ومهنياً
- اذكر أي نتيجة غير طبيعية بوضوح
- لا تستخدم جداول، اكتب كل شيء كنص عادي
- اختم دائماً بـ: "تنبيه: هذا التحليل للمعلومة فقط، يجب مراجعة طبيب متخصص للتشخيص النهائي"`;

// ============================================================
// PRE-COMPILED REGEXES — مرة واحدة عند بدء التشغيل (أسرع من إعادة البناء كل مرة)
// ============================================================
const _RX_MEDICAL_QUERY = /دواء|دوا|حبة|علاج|مرض|أعراض|جرعة|وصفة|صيدلي|طبيب|مستشفى|عملية|سكري|قلب|كبد|كلى|تحليل|أشعة|رنين|سرطان|التهاب|ألم|حرارة|انفلونزا|covid|corona|كورونا|فيروس|بكتيريا|مضاد حيوي|بنسيلين|ابتوفين|باراسيتامول|اسبرين|ميزوبروستول|فيتامين|هرمون|انسولين|قرحة|ربو|صداع|دوخة|غثيان|اقياء|اسهال|امساك|عظم|مفصل|عصب|نفسي|اكتئاب|قلق|كوليسترول|triglyceride|glucose|hemoglobin|wbc|rbc|platelet|creatinine|uric acid|bilirubin|الغدة|بنكرياس|زائدة|حوصلة|الكوليرا|ملاريا|هيباتيتس|hepatitis|diabetes|hypertension|infection|antibiotic|surgery|physician|hospital|diagnosis|prescription|symptom|medication|dosage|overdose|allergy|immune|vaccine|cholesterol|مصطلح طبي|anatomy|physiology|pathology|pharmacology|medical|medicine|health|صحة|طب|صيدلة/i;
const _RX_MEDICAL_BP   = /ضغط\s*دم|ارتفاع\s*ضغط|انخفاض\s*ضغط|ضغط\s*الدم/i;
const _RX_MEDICAL_BLOOD = /تحليل دم|فصيلة دم|ضغط دم|نقل دم|كريات دم|دم في|نزيف/i;
const _RX_MEDICAL_IMAGE = /أشعة|xray|x-ray|mri|رنين|ct scan|تحليل دم|فحص دم|صورة طبية|تقرير طبي|مختبر|مخبر|lab|blood test|صورة صدر|قلب|كلية|كبد|دماغ|brain|lung|kidney|liver|heart|ultrasound|سونار|إيكو|echo|ecg|ekg|نتائج|results|تقرير|فحص/i;

// ============================================================
// HELPERS
// ============================================================

// استخراج الرقم النظيف من JID
function cleanNumber(jidOrNumber) {
    return (jidOrNumber || '')
        .replace('@s.whatsapp.net', '')
        .replace('@lid', '')
        .replace('@g.us', '')
        .replace('+', '')
        .trim();
}

// API call مع timeout
async function fetchWithTimeout(url, options, timeoutMs = API_TIMEOUT_MS) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    try {
        const res = await fetch(url, { ...options, signal: controller.signal });
        return res;
    } finally {
        clearTimeout(timer);
    }
}

// ============================================================
// TEXT-TO-SPEECH HELPERS
// ============================================================

// استخراج المصطلح الإنجليزي من نص الرد
function extractTermForTTS(botReply) {
    const lines = botReply.split('\n').map(l => l.trim()).filter(Boolean);
    for (const line of lines.slice(0, 8)) {
        const clean = line.replace(/[📌⭐━─\-\*_#►•]/g, '').trim();
        // أولاً: ابحث عن كلمة إنجليزية
        const englishWords = clean.match(/[A-Za-z][A-Za-z\s\-]+/g);
        if (englishWords) {
            const term = englishWords[0].trim();
            if (term.length >= 3 && term.length <= 80) return term;
        }
        // ثانياً: ابحث عن كلمة عربية إذا لم توجد إنجليزية
        const arabicWords = clean.match(/[\u0600-\u06FF][\u0600-\u06FF\s]+/g);
        if (arabicWords) {
            const term = arabicWords[0].trim();
            if (term.length >= 2 && term.length <= 80) return term;
        }
    }
    return null;
}

// ============================================================
// TEXT-TO-SPEECH — عبر tts_worker.py (edge-tts يحتاج إنترنت، وpyttsx3 fallback محلي)
// لا يعتمد على Google أو أي API مدفوع
// ============================================================
const { execFile, spawn } = require('child_process');
const os           = require('os');

const TTS_WORKER_PATH = path.join(__dirname, 'tts_worker.py');
const STT_WORKER_PATH = path.join(__dirname, 'stt_worker.py');
const PYTHON_BIN      = process.env.PYTHON_CMD || 'python3';

/**
 * توليد صوت OGG Opus محلياً عبر tts_worker.py
 * @param {string} text  — النص المراد تحويله
 * @param {string} lang  — 'ar' | 'en' | 'fr' ...
 * @returns {Promise<Buffer|null>}
 */
async function generateTTS(text, lang = 'en') {
    const ttsLang   = lang === 'ar' ? 'ar' : (lang || 'en');
    // تنظيف النص وتقطيعه على حدود الكلمات
    const maxChars  = parseInt(process.env.TTS_MAX_CHARS || '500', 10);
    const rawText   = text.replace(/[^\w\s\u0600-\u06FF.,!?؟،]/g, ' ').replace(/\s+/g, ' ').trim();
    const cleanText = rawText.length > maxChars
        ? rawText.slice(0, maxChars).replace(/\s\S*$/, '')
        : rawText;

    if (!cleanText) return null;

    const tmpId  = `${Date.now()}_${Math.random().toString(36).slice(2)}`;
    const mp3Out = path.join(os.tmpdir(), `tts_${tmpId}.mp3`);
    const oggOut = path.join(os.tmpdir(), `tts_${tmpId}.ogg`);

    return new Promise(resolve => {
        const child = spawn(PYTHON_BIN, [TTS_WORKER_PATH, cleanText, mp3Out, ttsLang], {
            stdio: ['ignore', 'pipe', 'pipe'],
            env: { ...process.env }
        });

        let mp3Path = null;
        let stdoutBuf = '';

        child.stdout.on('data', data => {
            stdoutBuf += data.toString();
            const lines = stdoutBuf.split('\n');
            stdoutBuf = lines.pop();
            for (const line of lines.filter(l => l.trim())) {
                try {
                    const evt = JSON.parse(line);
                    if (evt.type === 'done') {
                        mp3Path = evt.output;
                        console.log(`[TTS] ✅ ${evt.engine} "${cleanText.slice(0,30)}" → ${mp3Path}`);
                    } else if (evt.type === 'error') {
                        console.warn(`[TTS] worker: ${evt.message}`);
                    }
                } catch (_) {}
            }
        });

        child.stderr.on('data', d => {
            const msg = d.toString().trim();
            if (msg) console.warn(`[TTS stderr] ${msg.slice(0,120)}`);
        });

        child.on('close', async () => {
            if (!mp3Path || !fs.existsSync(mp3Path)) {
                console.warn('[TTS] mp3 غير موجود بعد worker');
                return resolve(null);
            }

            // تحويل MP3 → OGG Opus (WhatsApp يحتاج Opus)
            try {
                await new Promise((res, rej) => {
                    execFile('ffmpeg', [
                        '-y', '-i', mp3Path,
                        '-c:a', 'libopus',
                        '-b:a', '32k',
                        '-vn', oggOut
                    ], { timeout: 20_000 }, err => {
                        fs.unlink(mp3Path, () => {});
                        if (err) return rej(new Error(`ffmpeg: ${err.message}`));
                        res();
                    });
                });
            } catch (ffErr) {
                console.warn(`[TTS] ffmpeg فشل: ${ffErr.message}`);
                fs.unlink(mp3Path, () => {});
                return resolve(null);
            }

            const oggBuffer = await fs.promises.readFile(oggOut).catch(() => null);
            fs.unlink(oggOut, () => {});

            if (!oggBuffer || oggBuffer.length < 100) {
                console.warn('[TTS] OGG فارغ');
                return resolve(null);
            }
            console.log(`[TTS] ✅ OGG جاهز: ${oggBuffer.length} bytes`);
            resolve(oggBuffer);
        });

        child.on('error', err => {
            console.warn(`[TTS] spawn فشل: ${err.message}`);
            resolve(null);
        });
    });
}

// ============================================================
// SPEECH-TO-TEXT — محلي بالكامل عبر stt_worker.py (faster-whisper / vosk)
// بديل لـ Voxtral API عند الحاجة أو لتوفير رصيد API
// ============================================================

/**
 * استخراج نص من ملف صوتي محلياً
 * @param {Buffer} audioBuffer — بيانات الصوت الخام
 * @param {string} mimeType    — نوع الملف (audio/ogg ...)
 * @param {string} lang        — 'ar' | 'en' | 'auto'
 * @returns {Promise<{text:string, language:string, engine:string}|null>}
 */
async function transcribeLocal(audioBuffer, mimeType, lang = 'ar') {
    const tmpId      = `${Date.now()}_${Math.random().toString(36).slice(2)}`;
    const ext        = mimeType && mimeType.includes('mp4') ? 'm4a'
                     : mimeType && mimeType.includes('mpeg') ? 'mp3'
                     : 'ogg';
    const audioFile  = path.join(os.tmpdir(), `stt_${tmpId}.${ext}`);

    try {
        await fs.promises.writeFile(audioFile, audioBuffer);
    } catch (e) {
        console.warn(`[STT] فشل كتابة ملف مؤقت: ${e.message}`);
        return null;
    }

    return new Promise(resolve => {
        const child = spawn(PYTHON_BIN, [STT_WORKER_PATH, audioFile, lang], {
            stdio: ['ignore', 'pipe', 'pipe'],
            env: { ...process.env }
        });

        let result = null;
        let stdoutBuf = '';

        child.stdout.on('data', data => {
            stdoutBuf += data.toString();
            const lines = stdoutBuf.split('\n');
            stdoutBuf = lines.pop();
            for (const line of lines.filter(l => l.trim())) {
                try {
                    const evt = JSON.parse(line);
                    if (evt.type === 'result') {
                        result = { text: evt.text, language: evt.language, engine: evt.engine };
                        console.log(`[STT] ✅ ${evt.engine} [${evt.language}] "${evt.text?.slice(0,40)}" (${evt.duration}s)`);
                    } else if (evt.type === 'error') {
                        console.warn(`[STT] worker: ${evt.message}`);
                    }
                } catch (_) {}
            }
        });

        child.stderr.on('data', d => {
            const msg = d.toString().trim();
            if (msg) console.warn(`[STT stderr] ${msg.slice(0,120)}`);
        });

        child.on('close', () => {
            fs.unlink(audioFile, () => {});
            resolve(result);
        });

        child.on('error', err => {
            console.warn(`[STT] spawn فشل: ${err.message}`);
            fs.unlink(audioFile, () => {});
            resolve(null);
        });
    });
}

// إرسال voice note لـ WhatsApp
async function sendVoiceNote(jid, audioBuffer, quotedMsg) {
    const opts = quotedMsg ? { quoted: quotedMsg } : {};
    await sock.sendMessage(jid, {
        audio:    audioBuffer,
        mimetype: 'audio/ogg; codecs=opus',
        ptt:      true
    }, opts);
}


function cleanupLocalFile(filePath) {
    try { if (filePath) fs.unlink(filePath, () => {}); } catch (_) {}
}

function looksLikePDFCreateRequest(text) {
    return /(?:اعمل|أنشئ|انشئ|سوي|اصنع|اكتب|جهز).{0,25}(?:ملف\s*)?pdf|(?:pdf).{0,20}(?:عن|حول|شرح|اشرح|موضوع)/i.test(text || '');
}
function looksLikeTXTCreateRequest(text) {
    return /(?:اعمل|أنشئ|انشئ|سوي|اصنع|اكتب|جهز).{0,25}(?:ملف\s*)?(?:txt|نص|نصي)|(?:txt).{0,20}(?:عن|حول|شرح|اشرح|موضوع)/i.test(text || '');
}
function looksLikeImageGenerateRequest(text) {
    return /(?:ارسم|صمم|انشئ|أنشئ|ولد|ولّد|اعمل|اصنع).{0,25}(?:صورة|رسمة|بوستر|تصميم)|(?:صورة).{0,20}(?:عن|لـ|حول)/i.test(text || '');
}
function looksLikeImageEnhanceRequest(text) {
    return /(?:حسن|حسّن|وضح|وضّح|نقّي|نقي|زبط|عدّل|عدل|enhance|sharpen|improve|clear)/i.test(text || '');
}
function makeShortTitle(text, fallback='MedTerm') {
    const t = String(text || '').replace(/\s+/g, ' ').trim();
    return (t.slice(0, 55) || fallback).replace(/[\/:*?"<>|]/g, '_');
}
async function sendLocalDocument(jid, filePath, fileName, mimetype, quotedMsg) {
    const opts = quotedMsg ? { quoted: quotedMsg } : {};
    await sock.sendMessage(jid, {
        document: fs.readFileSync(filePath),
        fileName,
        mimetype
    }, opts);
}
async function sendLocalImage(jid, filePath, caption, quotedMsg) {
    const opts = quotedMsg ? { quoted: quotedMsg } : {};
    await sock.sendMessage(jid, { image: fs.readFileSync(filePath), caption: caption || '' }, opts);
}

// تنظيف TTS pending و PDF pending المنتهية كل 10 دقائق
setInterval(() => {
    const now = Date.now();
    for (const k of Object.keys(userTTSPending)) {
        if (now > (userTTSPending[k].expiresAt || 0)) delete userTTSPending[k];
    }
    for (const k of Object.keys(userPdfPending)) {
        if (now > (userPdfPending[k].expiresAt || 0)) delete userPdfPending[k];
    }
}, 10 * 60_000);

// ============================================================
// AI FUNCTIONS
// ============================================================
// Semaphore: أقصى 15 طلباً متزامناً للـ AI
// الإصلاح: release() مضمون حتى عند الفشل، وحماية ضد تعليق العداد
let _aiActive = 0;
const _aiQueue = [];
function aiSemaphore() {
    return new Promise(resolve => {
        function tryAcquire() {
            if (_aiActive < 15) {
                _aiActive++;
                let released = false;
                // release آمن: لا ينفذ أكثر من مرة حتى لو استُدعي عدة مرات
                const release = () => {
                    if (released) return;
                    released = true;
                    _aiActive = Math.max(0, _aiActive - 1); // حماية ضد السالب
                    if (_aiQueue.length) _aiQueue.shift()();
                };
                resolve(release);
            } else {
                _aiQueue.push(tryAcquire);
            }
        }
        tryAcquire();
    });
}

// مراقب دوري: إذا علق العداد أكثر من 5 دقائق بدون طلبات فعلية → إعادة تعيين
let _aiLastActivity = Date.now();
setInterval(() => {
    if (_aiActive > 0 && Date.now() - _aiLastActivity > 5 * 60_000) {
        console.warn('[aiSemaphore] تحذير: العداد علق عند', _aiActive, '— إعادة تعيين');
        _aiActive = 0;
        while (_aiQueue.length) _aiQueue.shift()();
    }
}, 60_000);

// إشعار الأدمن بمشاكل حرجة
let _lastAdminNotify = {}; // throttle: لا نرسل نفس التنبيه أكثر من مرة كل 30 دقيقة
const _lastBlockNotify = {}; // throttle منفصل لرسائل الحظر — حتى لا يتعارض مع _lastAdminNotify

// تنظيف _lastAdminNotify كل ساعة — منع memory leak بعد أسابيع من التشغيل
setInterval(() => {
    const now = Date.now();
    for (const k of Object.keys(_lastAdminNotify)) {
        if (now - _lastAdminNotify[k] > 24 * 60 * 60_000)
            delete _lastAdminNotify[k];
    }
    // تنظيف _lastBlockNotify أيضاً (بعد 2 ساعة)
    for (const k of Object.keys(_lastBlockNotify)) {
        if (now - _lastBlockNotify[k] > 2 * 60 * 60_000)
            delete _lastBlockNotify[k];
    }
}, 60 * 60_000);

async function notifyAdmin(message) {
    const safeMessage = String(message || '').slice(0, 3500);
    const key = safeMessage.slice(0, 60);
    const now = Date.now();
    if (_lastAdminNotify[key] && now - _lastAdminNotify[key] < 30 * 60_000) return;
    _lastAdminNotify[key] = now;
    try {
        if (sock && isConnected) {
            await sock.sendMessage(`${ADMIN_NUMBER}@s.whatsapp.net`, { text: safeMessage });
        } else {
            console.warn('[notifyAdmin] مؤجل/غير مرسل لأن الاتصال غير جاهز:', safeMessage.slice(0, 180));
        }
    } catch (e) {
        console.error('[notifyAdmin] فشل:', e.message);
    }
}


process.on('uncaughtException', err => {
    console.error('[uncaughtException]', err);
    notifyAdmin(`🚨 خطأ حرج غير معالج في البوت:
${err.stack || err.message}`).catch(()=>{});
});
process.on('unhandledRejection', err => {
    console.error('[unhandledRejection]', err);
    const msg = err && (err.stack || err.message) ? (err.stack || err.message) : String(err);
    notifyAdmin(`🚨 Promise مرفوض بدون معالجة:
${msg}`).catch(()=>{});
});

async function callMistral(payload, retries = 3) {
    _aiLastActivity = Date.now();
    const release = await aiSemaphore();
    let lastError  = new Error('لم تكتمل أي محاولة');
    try {
        for (let attempt = 0; attempt < retries + 1; attempt++) {
            let response;
            try {
                response = await fetchWithTimeout(
                    'https://api.mistral.ai/v1/chat/completions',
                    {
                        method: 'POST',
                        headers: {
                            'Authorization': `Bearer ${MISTRAL_API_KEY}`,
                            'Content-Type': 'application/json'
                        },
                        body: JSON.stringify(payload)
                    }
                );
            } catch (fetchErr) {
                // خطأ شبكة أو timeout
                lastError = fetchErr;
                if (attempt >= retries) break;
                const wait = fetchErr.name === 'AbortError' ? 1000 : 1500 * (attempt + 1);
                console.warn(`[Mistral] محاولة ${attempt + 1}/${retries + 1} فشلت (${fetchErr.message})، انتظار ${wait/1000}ث...`);
                await new Promise(r => setTimeout(r, wait));
                continue;
            }

            // أخطاء لا تستحق retry
            if (response.status === 401) {
                console.error('[Mistral] ❌ خطأ 401: API Key غير صالح أو منتهي الصلاحية!');
                notifyAdmin('⚠️ تنبيه: Mistral API Key غير صالح (401). البوت لن يرد على الرسائل حتى يتم تحديث الـ Key.').catch(()=>{});
                throw new Error('AUTH_ERROR');
            }
            if (response.status === 402) {
                console.error('[Mistral] ❌ خطأ 402: نفاد رصيد Mistral API!');
                notifyAdmin('⚠️ تنبيه: نفاد رصيد Mistral API (402). يرجى شحن الحساب على console.mistral.ai').catch(()=>{});
                throw new Error('QUOTA_ERROR');
            }

            // أخطاء قابلة للإعادة (429 rate limit + 5xx server errors)
            if (response.status === 429 || response.status >= 500) {
                const isRateLimit = response.status === 429;
                const wait = isRateLimit
                    ? Math.min(3000 * Math.pow(2, attempt), 30_000)
                    : 2000 * (attempt + 1);
                lastError = new Error(isRateLimit ? 'RATE_LIMIT' : `SERVER_ERROR_${response.status}`);
                console.warn(`[Mistral] ${response.status} (محاولة ${attempt + 1}/${retries + 1})، انتظار ${wait/1000}ث...`);
                if (attempt >= retries) break;
                await new Promise(r => setTimeout(r, wait));
                continue;
            }

            if (!response.ok) {
                lastError = new Error(`HTTP_${response.status}`);
                break; // أخطاء 4xx الأخرى لا تستفيد من retry
            }

            // استجابة ناجحة
            let data;
            try { data = await response.json(); } catch (_) { lastError = new Error('JSON parse error'); break; }
            if (!data?.choices?.[0]?.message?.content) {
                lastError = new Error('استجابة فارغة من API');
                break;
            }
            return data.choices[0].message.content;
        }
        // استُنفدت كل المحاولات
        notifyAdmin(`⚠️ فشل طلب Mistral بعد كل المحاولات:
${lastError.message || lastError}`).catch(()=>{});
        throw lastError;
    } finally {
        release();
    }
}

// كشف الأسئلة المعقدة التي تحتاج نموذج أقوى
function isComplexQuery(text) {
    return /تشخيص|خطة علاج|تحليل مفصل|اشرح بالتفصيل|قارن بين|ما الفرق بين|برمج|اكتب كود|كود|code|برنامج|خوارزمية|قانون|عقد|فتوى|حكم شرعي|ترجم هذا النص|essay|مقال|تقرير|بحث|summarize|خلاصة شاملة|اشرح لي|فسّر|فسر|وضّح|وضح|ما هو الفرق|analyze|analysis|compare/i
        .test(text || '');
}

// askAI: نموذج سريع للمحادثات العادية، قوي للأسئلة المعقدة
async function askAI(messages) {
    // آخر رسالة من المستخدم لتحديد النموذج المناسب
    const lastUserMsg = [...messages].reverse().find(m => m.role === 'user')?.content || '';
    const useLarge = isComplexQuery(lastUserMsg) || lastUserMsg.length > 400;
    const model = useLarge ? 'mistral-large-latest' : 'mistral-small-latest';

    console.log(`[askAI] نموذج: ${model} | طول الرسالة: ${lastUserMsg.length}`);
    try {
        return await callMistral({
            model,
            messages,
            max_tokens: 1500,
            temperature: 0.5
        });
    } catch (e) {
        // إذا فشل الصغير، جرّب الكبير تلقائياً
        if (model === 'mistral-small-latest') {
            console.warn('[askAI] small فشل، محاولة large...');
            try {
                return await callMistral({
                    model: 'mistral-large-latest',
                    messages,
                    max_tokens: 1500,
                    temperature: 0.5
                });
            } catch (e2) {
                console.error('[askAI] large فشل أيضاً:', e2.message);
                if (e2.name === 'AbortError') return 'الرد يأخذ وقتاً أطول من المعتاد، يرجى إعادة المحاولة.';
                if (e2.message === 'AUTH_ERROR') return 'عذراً، حدثت مشكلة في إعدادات الخدمة. تم إشعار الأدمن.';
                if (e2.message === 'QUOTA_ERROR') return 'عذراً، نفاد رصيد الخدمة مؤقتاً. تم إشعار الأدمن.';
                return 'عذراً، تعذّر الرد الآن. يرجى المحاولة مرة أخرى.';
            }
        }
        console.error('[askAI] فشل نهائي:', e.message);
        if (e.name === 'AbortError') return 'الرد يأخذ وقتاً أطول من المعتاد، يرجى إعادة المحاولة.';
        if (e.message === 'AUTH_ERROR') return 'عذراً، حدثت مشكلة في إعدادات الخدمة. تم إشعار الأدمن.';
        if (e.message === 'QUOTA_ERROR') return 'عذراً، نفاد رصيد الخدمة مؤقتاً. تم إشعار الأدمن.';
        return 'عذراً، تعذّر الرد الآن. يرجى المحاولة مرة أخرى.';
    }
}

async function askAIWithDoc(docText, userQuestion, userName) {
    try {
        const question = userQuestion || 'لخّص هذا الملف بشكل شامل واذكر أهم نقاطه ومحتواه';
        const prompt = userName ? `اسم المستخدم: ${userName}\n${question}` : question;
        return await callMistral({
            model: 'mistral-large-latest',   // نص فقط — لا حاجة لـ pixtral
            messages: [
                { role: 'system', content: getSystemPrompt() },
                {
                    role: 'user',
                    content: `${prompt}\n\n--- محتوى ملف PDF ---\n${docText.slice(0, 14000)}`
                }
            ],
            max_tokens: 2500,
            temperature: 0.3
        });
    } catch (e) {
        console.error('[askAIWithDoc]', e.message);
        if (e.name === 'AbortError') return 'انتهت مهلة تحليل الملف، يرجى المحاولة مرة أخرى.';
        if (e.message === 'AUTH_ERROR' || e.message === 'QUOTA_ERROR') return 'عذراً، حدثت مشكلة في إعدادات الخدمة. تم إشعار الأدمن.';
        return 'عذراً، لم أتمكن من تحليل الملف. يرجى المحاولة مرة أخرى.';
    }
}

function isMedicalImage(text) {
    return _RX_MEDICAL_IMAGE.test(text || '');
}

async function askAIWithImage(base64Image, userQuestion, userName, mimeType) {
    try {
        const mime = mimeType || 'image/jpeg';
        const hasQuestion = userQuestion && userQuestion.trim().length > 0;

        // السيستم برومت العام — يقبل أي صورة بدون قيود
        const systemToUse = getSystemPrompt();

        // إذا ما في سؤال: وصف عام شامل
        const questionText = hasQuestion
            ? userQuestion
            : `حلل هذه الصورة بالتفصيل الكامل:
1. اشرح ما تراه في الصورة بدقة
2. إذا فيها نصوص أو أرقام: اقرأها كاملاً كما هي
3. إذا فيها جدول أو بيانات: اعرضها منظمة
4. إذا كانت صورة طبية أو أشعة: حللها طبياً بالتفصيل
5. أجب على أي سؤال يتعلق بمحتوى الصورة`;

        const prompt = userName ? `اسم المستخدم: ${userName}\n${questionText}` : questionText;

        return await callMistral({
            model: 'pixtral-large-latest',
            messages: [
                { role: 'system', content: systemToUse },
                {
                    role: 'user',
                    content: [
                        { type: 'image_url', image_url: { url: `data:${mime};base64,${base64Image}` } },
                        { type: 'text', text: prompt }
                    ]
                }
            ],
            max_tokens: 2500,
            temperature: 0.3
        });

    } catch (e) {
        console.error('[askAIWithImage]', e.message);
        if (e.name === 'AbortError') return 'انتهت مهلة التحليل، يرجى المحاولة مرة أخرى.';
        if (e.message === 'AUTH_ERROR' || e.message === 'QUOTA_ERROR') return 'عذراً، حدثت مشكلة في إعدادات الخدمة. تم إشعار الأدمن.';
        return 'عذراً، لم أتمكن من تحليل الصورة. يرجى المحاولة مرة أخرى.';
    }
}

// ============================================================
// VOXTRAL (Mistral) — تحويل الصوت إلى نص والرد عليه
// ============================================================
async function transcribeAndReplyAudio(buffer, mimeType, userQuestion, userName, chatHistory) {
    // نحوّل الصوت لـ base64
    const audioBase64 = buffer.toString('base64');

    // نبني محتوى الرسالة: صوت + سؤال (إن وُجد)
    const userContent = [
        {
            type: 'input_audio',
            input_audio: audioBase64   // base64 مباشرة بدون data URI
        }
    ];

    // إذا أرسل المستخدم نصاً مع الصوت نضيفه
    if (userQuestion && userQuestion.trim()) {
        userContent.push({ type: 'text', text: userQuestion.trim() });
    } else {
        userContent.push({
            type: 'text',
            text: userName
                ? `اسم المستخدم: ${userName}\nاستمع لهذه الرسالة الصوتية، افهمها وأجب عليها بشكل مفيد ومختصر.`
                : 'استمع لهذه الرسالة الصوتية، افهمها وأجب عليها بشكل مفيد ومختصر.'
        });
    }

    // نبني messages: system + سياق محدود + رسالة الصوت
    const messages = [
        { role: 'system', content: getSystemPrompt() },
        // آخر 6 رسائل من السياق للتواصل الطبيعي
        ...(chatHistory || []).slice(-6),
        { role: 'user', content: userContent }
    ];

    const release = await aiSemaphore();
    try {
        const response = await fetchWithTimeout(
            'https://api.mistral.ai/v1/chat/completions',
            {
                method: 'POST',
                headers: {
                    'Authorization': `Bearer ${MISTRAL_API_KEY}`,
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify({
                    model: VOXTRAL_MODEL,   // النموذج الأقوى للصوت (قابل للتغيير: export VOXTRAL_MODEL=xxx)
                    messages,
                    max_tokens: 1000,
                    temperature: 0.5
                })
            },
            60_000
        );

        if (response.status === 429) throw new Error('RATE_LIMIT');
        if (response.status === 401) throw new Error('AUTH_ERROR');
        if (response.status === 402) throw new Error('QUOTA_ERROR');
        if (!response.ok) {
            const errBody = await response.text();
            throw new Error(`Voxtral HTTP ${response.status}: ${errBody.slice(0, 120)}`);
        }

        const data = await response.json();
        const reply = data?.choices?.[0]?.message?.content;
        if (!reply) throw new Error('استجابة فارغة من Voxtral');
        return reply;

    } finally {
        release();
    }
}

// ============================================================
// USER COMMANDS — أوامر المستخدم
// ============================================================
// يُعيد true إذا تمت معالجة الأمر (ويجب التوقف عن المعالجة الاعتيادية)
async function handleUserCommand(body, sender, reply, react, isAdmin, isVIP) {
    const cmd = (body || '').trim();
    if (!cmd.startsWith('!')) return false;

    const parts = cmd.split(/\s+/);
    const command = parts[0].toLowerCase();

    // !مساعدة — قائمة الأوامر
    if (command === '!مساعدة' || command === '!help') {
        await reply(
            `📋 *قائمة الأوامر المتاحة:*\n\n` +
            `• *!مساعدة* — عرض هذه القائمة\n` +
            `• *!مسح* — مسح سياق المحادثة والبدء من جديد\n` +
            `• *!رصيد* — عرض عدد الرسائل المتبقية هذا الشهر\n` +
            `• *!لغة en* — تغيير لغة الردود (ar/en/fr/...)\n` +
            `• *!ملخص* — تلخيص المحادثة الحالية\n\n` +
            `_يمكنك أيضاً إرسال صور أو ملفات PDF للتحليل_\n` +
            `_لطلب نطق كلمة أو جملة: اكتب "نطق [الكلمة]"_\n` +
            `_لطلب ترجمة: اكتب "ترجم [النص]" وسأرسل لك الصوت تلقائياً_`
        );
        return true;
    }

    // !مسح — مسح سياق المحادثة
    if (command === '!مسح' || command === '!reset') {
        userChats[sender] = [];
        await react('✅');
        await reply(`🗑️ تم مسح سياق المحادثة. يمكنك البدء من جديد! 👋`);
        return true;
    }

    // !رصيد — عرض الرصيد المتبقي
    if (command === '!رصيد' || command === '!balance') {
        if (isAdmin || isVIP) {
            await reply('♾️ *رصيدك غير محدود* (VIP/أدمن)\n\n💬 رسائل: ♾️\n🖼️ صور: ♾️\n🔊 صوت/ترجمة: ♾️\n📄 PDF: ♾️ بلا حد للحجم');
        } else {
            const limit    = getUserDailyLimit(sender);
            const rec      = getDailyRecord(sender);
            const msgUsed  = rec.messages || 0;
            const imgUsed  = rec.images   || 0;
            const ttsUsed  = rec.tts      || 0;
            const msgLeft  = Math.max(0, limit - msgUsed);
            const imgLeft  = Math.max(0, DAILY_IMG_LIMIT - imgUsed);
            const ttsLeft  = Math.max(0, DAILY_TTS_LIMIT - ttsUsed);
            const resetDate = new Date(rec.activatedAt);
            const resetStr  = resetDate.toLocaleTimeString('ar-SA', { hour: '2-digit', minute: '2-digit' });
            await reply(
                `📊 *رصيدك الشهري — النسخة المجانية:*\n\n` +
                `💬 الرسائل النصية:    ${msgUsed}/${limit} — متبقي: *${msgLeft}*\n` +
                `🖼️ الصور المحللة:     ${imgUsed}/${DAILY_IMG_LIMIT} — متبقي: *${imgLeft}*\n` +
                `🔊 الرسائل الصوتية:  ${ttsUsed}/${DAILY_TTS_LIMIT} — متبقي: *${ttsLeft}*\n` +
                `📄 PDF:              حد الحجم 1MB فقط\n\n` +
                `🔄 يتجدد شهرياً حسب بداية الشهر: *${resetStr}*\n\n` +
                `─────────────\n` +
                `💎 *النسخة المميزة (VIP):*\n` +
                `✅ رسائل + صور + صوت: ♾️ غير محدود\n` +
                `✅ PDF: بدون حد للحجم\n` +
                `👤 wa.me/972593850520`
            );
        }
        return true;
    }

    // !لغة — تغيير اللغة
    if (command === '!لغة' || command === '!language' || command === '!lang') {
        const lang = parts[1] || '';
        if (!lang) {
            const currentLang = userLanguages[sender] || 'ar (افتراضي)';
            await reply(`🌐 لغتك الحالية: *${currentLang}*\n\nمثال لتغييرها:\n• !لغة en (إنجليزي)\n• !لغة ar (عربي)\n• !لغة fr (فرنسي)`);
        } else {
            userLanguages[sender] = lang.toLowerCase();
            saveData();
            const langNames = { ar: 'العربية', en: 'الإنجليزية', fr: 'الفرنسية', de: 'الألمانية', es: 'الإسبانية', tr: 'التركية' };
            const langName = langNames[lang.toLowerCase()] || lang;
            await react('✅');
            await reply(`🌐 تم تغيير اللغة إلى: *${langName}*\nسأرد عليك بهذه اللغة من الآن.`);
        }
        return true;
    }

    // !ملخص — تلخيص المحادثة الحالية
    if (command === '!ملخص' || command === '!summary') {
        const history = userChats[sender] || [];
        const convMsgs = history;
        if (convMsgs.length < 2) {
            await reply('📝 لا يوجد محادثة كافية للتلخيص بعد. تحدث أكثر ثم جرب الأمر مجدداً!');
            return true;
        }
        await react('⏳');
        const convText = convMsgs.slice(-20).map(m =>
            `${m.role === 'user' ? 'المستخدم' : 'البوت'}: ${m.content.slice(0, 300)}`
        ).join('\n');
        try {
            const summary = await callMistral({
                model: 'mistral-small-latest',
                messages: [
                    { role: 'system', content: 'لخّص هذه المحادثة بشكل مختصر ومنظم في 5-7 نقاط. ركّز على المحاور الرئيسية.' },
                    { role: 'user', content: convText }
                ],
                max_tokens: 600,
                temperature: 0.3
            });
            await reply(`📝 *ملخص المحادثة:*\n\n${summary}`);
            await react('✅');
        } catch (e) {
            await reply('عذراً، حدث خطأ أثناء التلخيص. حاول مرة أخرى.');
            await react('❌');
        }
        return true;
    }

    return false; // لم يُعرَّف الأمر
}

// ============================================================
// EXAM MODE — نظام الاختبارات (VIP فقط)
// ============================================================
// ============================================================
// نظام الاختبارات — معالجة الأسئلة (VIP فقط)
// ============================================================
async function handleExamMode(sender, body, pages, docText, fileName, reply, react) {
    const bodyTrimmed = (body || '').trim();

    // خروج من نظام الاختبارات
    if (/^(خروج|exit|إلغاء|الغاء|cancel)$/i.test(bodyTrimmed)) {
        delete userExamMode[sender];
        await react('✅');
        await reply(
            '👋 تم الخروج من *نظام الاختبارات*.\n\n' +
            'يمكنك استخدام البوت بشكل طبيعي الآن.\n' +
            '_أرسل *111* في أي وقت لتفعيل نظام الاختبارات مجدداً._'
        );
        return;
    }

    // تحقق من وجود محتوى الملف
    const hasPages   = Array.isArray(pages)  && pages.length  > 0;
    const hasDocText = typeof docText === 'string' && docText.trim().length > 0;

    if (!hasPages && !hasDocText) {
        await react('❌');
        await reply(
            '⚠️ لم يتم تحميل محتوى الملف بشكل صحيح.\n\n' +
            'أرسل *111* وأعد رفع ملف PDF من جديد.'
        );
        return;
    }

    await react('⏳');

    // System prompt ذكي يفهم السؤال ويجيب من الملف
    const examSystemPrompt =
        'أنت مساعد تعليمي ذكي ومتفهّم. مهمتك: الإجابة على أسئلة المستخدم اعتماداً على محتوى الملف المرفق.\n\n' +
        '══ تعليمات الإجابة ══\n\n' +
        '1. افهم السؤال جيداً — قد يكون السؤال مباشراً أو غير مباشر أو بصياغة مختلفة عما في الملف، ابحث عن المعنى لا عن النص الحرفي.\n' +
        '2. إذا وجدت الإجابة في الملف (كاملةً أو جزئياً):\n' +
        '   ✅ أجب بشكل واضح ومفصّل\n' +
        '   ✅ اذكر الأرقام والتواريخ والأسماء بدقة\n' +
        '   ✅ إذا كانت الإجابة موزعة على أجزاء عدة اجمعها معاً\n' +
        '3. إذا لم يكن السؤال في الملف إطلاقاً:\n' +
        '   ❌ أجب فقط: "⚠️ هذا السؤال غير موجود في محتوى الملف المرفق."\n' +
        '4. لا تخترع معلومات ولا تضف شيئاً من خارج الملف\n' +
        '5. أجب بنفس لغة السؤال (عربي أو إنجليزي)\n\n' +
        `الملف الحالي: "${fileName}"`;

    try {
        // نستخدم النص دائماً — أسرع بكثير من إرسال الصور
        const rawText = hasDocText ? docText : '';
        const fullText = rawText.length > 60000
            ? rawText.slice(0, 60000) + '\n\n[... باقي الملف محذوف بسبب الطول ...]'
            : rawText;

        const res = await callMistral({
            model: 'mistral-large-latest',
            messages: [
                { role: 'system', content: examSystemPrompt },
                {
                    role: 'user',
                    content:
                        '══ محتوى الملف ══\n' + fullText +
                        '\n\n══ سؤال المستخدم ══\n' + bodyTrimmed +
                        '\n\nأجب بدقة من محتوى الملف.'
                }
            ],
            max_tokens: 2048,  // حد آمن لجميع نماذج Mistral
            temperature: 0.3
        }, 4);   // 4 محاولات بدل 3

        // إرسال الجواب مع header واضح
        await reply(
            '📚 *نظام الاختبارات*\n' +
            '📄 ' + fileName + '\n' +
            '─────────────\n\n' +
            res +
            '\n\n─────────────\n' +
            '_💡 اكتب سؤالاً آخر أو اكتب *خروج* للخروج_'
        );
        await react('✅');

    } catch (e) {
        console.error('[examMode]', e.message);
        await react('❌');
        if (e.message === 'AUTH_ERROR') {
            await reply('عذراً، مشكلة في إعدادات الخدمة. تم إشعار الأدمن.');
        } else if (e.message === 'QUOTA_ERROR') {
            await reply('عذراً، نفاد رصيد الخدمة مؤقتاً. تم إشعار الأدمن.');
        } else if (e.name === 'AbortError') {
            await reply('⏱️ الملف كبير واستغرق وقتاً طويلاً. يرجى المحاولة مرة أخرى أو إرسال ملف أصغر.');
        } else {
            await reply('حدث خطأ أثناء معالجة سؤالك. يرجى المحاولة مرة أخرى.');
        }
    }
}


// كل مستخدم رسالة عشوائية مختلفة + rate limiting ذكي
async function broadcastToAll(getTextFn) {
    if (!isConnected) { console.log("[broadcast] البوت غير متصل"); return { sent: 0, failed: 0, total: 0 }; }
    const allUsers = Object.keys(welcomedUsers)
        .map(n => cleanNumber(n))
        .filter(n => n && n !== ADMIN_NUMBER && /^\d+$/.test(n));

    console.log(`📢 قائمة البث: ${allUsers.length} مستخدم`);
    let sent = 0, failed = 0;

    for (let i = 0; i < allUsers.length; i++) {
        const num = allUsers[i];

        if (i > 0 && i % 30 === 0) {
            console.log(`⏸️ استراحة 10 ثواني بعد ${i} رسائل...`);
            await new Promise(r => setTimeout(r, 10_000));
        }

        try {
            const text = typeof getTextFn === 'function' ? getTextFn() : getTextFn;
            const jid = `${num}@s.whatsapp.net`;
            console.log(`📤 إرسال لـ ${num}`);
            await sock.sendMessage(jid, { text });
            sent++;
            console.log(`✅ وصل لـ ${num}`);
        } catch(e) {
            console.error(`❌ فشل لـ ${num}:`, e.message);
            failed++;
        }

        await new Promise(r => setTimeout(r, 800));
    }

    console.log(`📢 اكتمل البث: ${sent} نجح، ${failed} فشل`);
    return { sent, failed, total: allUsers.length };
}


// ============================================================
// QR WEB SERVER
// ============================================================
function startQRServer() {
    const _webRateLimit = {}; // IP-based rate limit للـ web server
    function checkWebRate(ip, route = 'web') {
        const now = Date.now();
        const key = `${ip}:${route}`;
        if (!_webRateLimit[key]) _webRateLimit[key] = { count: 0, first: now };
        const s = _webRateLimit[key];
        if (now - s.first > 60_000) { s.count = 0; s.first = now; }
        s.count++;
        const limit = route === 'api' ? 30 : 60; // API أخطر، نحدّه أكثر
        return s.count <= limit;
    }
    setInterval(() => {
        const now = Date.now();
        for (const key of Object.keys(_webRateLimit))
            if (now - _webRateLimit[key].first > 120_000) delete _webRateLimit[key];
    }, 5 * 60_000);

    // صفحة تسجيل الدخول
    function loginPage(errMsg) {
        return `<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>${BOT_NAME} — تسجيل الدخول</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
:root{--bg:#0b0f1a;--surface:#111827;--border:#1e2d45;--text:#e2e8f0;--muted:#64748b;--accent:#38bdf8;--red:#f87171;--green:#22c55e}
body{font-family:'Segoe UI',system-ui,Arial,sans-serif;background:var(--bg);color:var(--text);min-height:100vh;display:flex;align-items:center;justify-content:center}
.card{background:var(--surface);border:1px solid var(--border);border-radius:20px;padding:40px 36px;width:100%;max-width:380px;box-shadow:0 20px 60px rgba(0,0,0,.5)}
.logo{display:flex;align-items:center;gap:12px;margin-bottom:32px;justify-content:center}
.logo-icon{width:48px;height:48px;border-radius:14px;background:linear-gradient(135deg,#0ea5e9,#6366f1);display:flex;align-items:center;justify-content:center;font-size:24px}
.logo-name{font-size:22px;font-weight:800;color:var(--accent)}
h2{font-size:16px;color:var(--muted);text-align:center;margin-bottom:28px;font-weight:400}
label{display:block;font-size:12px;color:var(--muted);margin-bottom:6px;margin-top:16px}
input{width:100%;background:#0b0f1a;border:1px solid var(--border);border-radius:10px;padding:11px 14px;color:var(--text);font-size:14px;outline:none;transition:.2s}
input:focus{border-color:var(--accent)}
.err{background:rgba(248,113,113,.12);border:1px solid rgba(248,113,113,.3);border-radius:8px;padding:10px 14px;color:var(--red);font-size:13px;margin-top:14px;display:${errMsg ? 'block' : 'none'}}
button{width:100%;margin-top:24px;background:linear-gradient(135deg,#0ea5e9,#6366f1);border:none;border-radius:10px;padding:13px;color:#fff;font-size:15px;font-weight:700;cursor:pointer;transition:.2s}
button:hover{opacity:.9;transform:translateY(-1px)}
.footer{text-align:center;margin-top:20px;font-size:11px;color:var(--muted)}
</style>
</head>
<body>
<div class="card">
  <div class="logo">
    <div class="logo-icon">🏥</div>
    <span class="logo-name">${BOT_NAME}</span>
  </div>
  <h2>لوحة تحكم الأدمن</h2>
  <form method="POST" action="/login">
    <label>اسم المستخدم</label>
    <input type="text" name="username" placeholder="أدخل اسم المستخدم" autocomplete="off" required>
    <label>كلمة السر</label>
    <input type="password" name="password" placeholder="أدخل كلمة السر" required>
    <div class="err">${errMsg || ''}</div>
    <button type="submit">🔐 دخول</button>
  </form>
  <div class="footer">${BOT_NAME} Admin Panel &copy; 2025</div>
</div>
</body>
</html>`;
    }

    const server = http.createServer(async (req, res) => {
        res.setHeader('Connection', 'close');
        const ip  = req.socket.remoteAddress || 'unknown';
        const url = req.url.split('?')[0];

        if (!checkWebRate(ip, (url === '/api' || url === '/data') ? 'api' : 'web')) {
            res.writeHead(429, { 'Content-Type': 'application/json' });
            res.end(JSON.stringify({ ok: false, msg: 'Too many requests' }));
            return;
        }

        // ===== LOGIN PAGE (GET) =====
        if (url === '/login' && req.method === 'GET') {
            res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' });
            res.end(loginPage(''));
            return;
        }

        // ===== LOGIN SUBMIT (POST) =====
        if (url === '/login' && req.method === 'POST') {
            // حماية Brute-Force
            if (!checkLoginBrute(ip)) {
                res.writeHead(429, { 'Content-Type': 'text/html; charset=utf-8' });
                res.end(loginPage('⛔ محاولات كثيرة، انتظر 10 دقائق ثم حاول مجدداً'));
                return;
            }
            try {
                const body = await readLimitedBody(req);
                const params = Object.fromEntries(
                    body.split('&').map(p => p.split('=').map(v => { try { return decodeURIComponent(v.replace(/\+/g, ' ')); } catch (_) { return ''; } }))
                );
                if (params.username === DASHBOARD_USER && params.password === DASHBOARD_PASS) {
                    const token = generateToken();
                    const ua = req.headers['user-agent'] || '';
                    _sessions[token] = { ip, ua, createdAt: Date.now() };
                    resetLoginBrute(ip); // إعادة تعيين عداد المحاولات عند النجاح
                    res.writeHead(302, {
                        'Set-Cookie': sessionCookie(token),
                        'Location': '/'
                    });
                    res.end();
                } else {
                    res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' });
                    res.end(loginPage('❌ اسم المستخدم أو كلمة السر غير صحيحة'));
                }
            } catch (e) {
                if (e.message === 'REQUEST_BODY_TOO_LARGE') {
                    res.writeHead(413, { 'Content-Type': 'text/html; charset=utf-8' });
                    res.end(loginPage('حجم الطلب كبير جداً'));
                } else {
                    res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' });
                    res.end(loginPage('حدث خطأ، حاول مرة أخرى'));
                }
            }
            return;
        }

        // ===== LOGOUT =====
        if (url === '/logout') {
            const token = parseCookie(req.headers['cookie'] || '')['adm_tok'];
            if (token) delete _sessions[token];
            res.writeHead(302, {
                'Set-Cookie': 'adm_tok=; Path=/; Max-Age=0',
                'Location': '/login'
            });
            res.end();
            return;
        }

        // ===== حماية كل الـ endpoints — إعادة توجيه لصفحة الدخول =====
        if (!isValidSession(req)) {
            if (url === '/api' || url === '/data' || url === '/qr-image') {
                res.writeHead(401, { 'Content-Type': 'application/json' });
                res.end(JSON.stringify({ ok: false, msg: 'Unauthorized' }));
            } else {
                res.writeHead(302, { 'Location': '/login' });
                res.end();
            }
            return;
        }

        // ===== QR IMAGE =====
        if (url === '/qr-image' && currentQR) {
            try {
                const buf = await QRCodeImg.toBuffer(currentQR, { errorCorrectionLevel: 'H', width: 400, margin: 2 });
                res.writeHead(200, { 'Content-Type': 'image/png' });
                res.end(buf);
            } catch { res.writeHead(500); res.end(); }
            return;
        }

        // ===== API =====
        if (url === '/api' && req.method === 'POST') {
            try {
                const body = await readLimitedBody(req);
                const { action, data } = JSON.parse(body);
                let result = { ok: true };

                    if (action === 'addVip') {
                        const num = String(data.num || '').replace(/\D/g, '').trim();
                        if (!num) { result = { ok: false, msg: 'رقم الهاتف غير صحيح' }; }
                        else if (!vipNumbers.includes(num)) {
                            vipNumbers.push(num);
                            const expiry = Date.now() + 30 * 24 * 60 * 60_000;
                            vipExpiry[num] = expiry;
                            resetUserUsage(num);
                            saveData();
                            if (sock && isConnected) {
                                try {
                                    const expDate = new Date(expiry).toLocaleDateString('ar-SA');
                                    await sock.sendMessage(`${num}@s.whatsapp.net`, {
                                        text:
                                            `🌟 *تهانينا! تم تفعيل اشتراكك المميز (VIP)*\n\n` +
                                            `✅ *صلاحياتك الآن غير محدودة:*\n` +
                                            `💬 رسائل: ♾️ غير محدودة\n` +
                                            `🖼️ صور: ♾️ غير محدودة\n` +
                                            `🔊 صوت وترجمة: ♾️ غير محدودة\n` +
                                            `📄 ملفات PDF: ♾️ بدون حد للحجم\n\n` +
                                            `📅 تاريخ الانتهاء: ${expDate}\n\n` +
                                            `شكراً لثقتك بنا! 🎉`
                                    });
                                } catch (_) {}
                            }
                            result.msg = `✅ تم تفعيل VIP للمستخدم ${num} لمدة شهر`;
                        } else {
                            // تجديد — إضافة شهر من الآن أو من تاريخ الانتهاء الحالي أيهما أبعد
                            const current = vipExpiry[num] || Date.now();
                            vipExpiry[num] = Math.max(current, Date.now()) + 30 * 24 * 60 * 60_000;
                            saveData();
                            if (sock && isConnected) {
                                try {
                                    const expDate = new Date(vipExpiry[num]).toLocaleDateString('ar-SA');
                                    await sock.sendMessage(`${num}@s.whatsapp.net`, {
                                        text: `🔄 *تم تجديد اشتراكك المميز (VIP)*\n📅 الانتهاء الجديد: ${expDate}\nشكراً لثقتك بنا! 🌟`
                                    });
                                } catch (_) {}
                            }
                            result.msg = `✅ تم تجديد VIP للمستخدم ${num} لشهر إضافي`;
                        }
                    }
                    else if (action === 'removeVip') {
                        const num = String(data.num || '').replace(/\D/g, '').trim();
                        if (!num) { result = { ok: false, msg: 'رقم الهاتف غير صحيح' }; }
                        else {
                            const wasVip = vipNumbers.includes(num);
                            vipNumbers = vipNumbers.filter(n => n !== num);
                            delete vipExpiry[num];
                            saveData();
                            if (wasVip && sock && isConnected) {
                                try {
                                    await sock.sendMessage(`${num}@s.whatsapp.net`, {
                                        text: `ℹ️ تم إلغاء اشتراكك المميز (VIP).\nيمكنك التجديد عبر التواصل معنا:\n👤 wa.me/972593850520`
                                    });
                                } catch (_) {}
                            }
                            result.msg = wasVip ? `✅ تم إزالة VIP عن ${num}` : `المستخدم ${num} لم يكن VIP`;
                        }
                    }
                    else if (action === 'deleteUser') {
                        const num = data.num;
                        delete userChats[num];
                        delete welcomedUsers[num];
                        delete userNames[num];
                        vipNumbers = vipNumbers.filter(n => n !== num);
                        reports = reports.filter(r => r.sender !== num);
                        saveData();
                    }
                    else if (action === 'clearReports') {
                        reports = [];
                        saveData();
                    }
                    else if (action === 'addBlacklist') {
                        const num = String(data.num || '').replace(/\D/g, '').trim();
                        if (!num) { result = { ok: false, msg: 'رقم الهاتف غير صحيح' }; }
                        else if (blacklist.includes(num)) {
                            result.msg = `المستخدم ${num} محظور مسبقاً`;
                        } else {
                            blacklist.push(num);
                            saveData();
                            if (sock && isConnected) {
                                try {
                                    await sock.sendMessage(`${num}@s.whatsapp.net`, { text: BLACKLIST_MSG });
                                } catch (_) {}
                            }
                            result.msg = `✅ تم حظر ${num} وإرسال إشعار`;
                        }
                    }
                    else if (action === 'removeBlacklist') {
                        const num = String(data.num || '').replace(/\D/g, '').trim();
                        if (!num) { result = { ok: false, msg: 'رقم الهاتف غير صحيح' }; }
                        else {
                            const wasBlocked = blacklist.includes(num);
                            blacklist = blacklist.filter(n => n !== num);
                            saveData();
                            result.msg = wasBlocked ? `✅ تم رفع الحظر عن ${num}` : `المستخدم ${num} لم يكن محظوراً`;
                        }
                    }
                    else if (action === 'clearSessions') {
                        userChats = {};
                        result.msg = 'تم مسح الجلسات';
                    }
                    else if (action === 'setUserLimit') {
                        // تعيين حد مخصص لمستخدم: { num, limit }
                        const num   = (data.num || '').replace(/\D/g, '');
                        const limit = parseInt(data.limit, 10);
                        if (!num || isNaN(limit) || limit < 0) {
                            result = { ok: false, msg: 'بيانات غير صحيحة' };
                        } else {
                            const oldLimit = getUserDailyLimit(num);
                            userLimits[num] = limit;
                            resetUserUsage(num); // تصفير الاستهلاك عند رفع الحد
                            // إذا كان المستخدم متجاوزاً وتم رفع الحد عن استهلاكه الحالي
                            // → نصفّر العداد لمستواه الحالي حتى يستطيع الإرسال فوراً
                            const rec = getDailyRecord(num);
                            const alreadyUsed = rec ? rec.messages : 0;
                            if (limit > oldLimit && alreadyUsed >= oldLimit && alreadyUsed < limit) {
                                // لا نمسح الاستخدام — فقط نتركه كما هو لأن الحد الجديد أعلى منه
                            } else if (limit > alreadyUsed && alreadyUsed >= oldLimit) {
                                // المستخدم كان محظور الإرسال — الحد الجديد يسمح له: لا حاجة لتعديل
                            }
                            // إذا رُفع الحد فوق الاستهلاك الحالي يُسمح للمستخدم تلقائياً (لأن checkDailyMessages تقارن rec.messages < limit)

                            saveData();
                            // إشعار المستخدم فقط إذا رُفع الحد (ليس عند التخفيض)
                            const nowAllowed = alreadyUsed < limit;
                            if (sock && isConnected && limit > oldLimit) {
                                try {
                                    const jid = `${num}@s.whatsapp.net`;
                                    const name = userNames[num] ? `${userNames[num]}` : '';
                                    const msg = nowAllowed
                                        ? `🎉 ${name ? `أهلاً ${name}، ` : ''}تم رفع حد رسائلك الشهري إلى *${limit}* رسالة!\n\nيمكنك الآن الاستمرار في المحادثة. 🚀`
                                        : `ℹ️ ${name ? `${name}، ` : ''}تم تعديل حدك الشهري إلى *${limit}* رسالة.\n\nللحصول على المزيد تواصل مع الأدمن أو اشترك بـ VIP`;
                                    await sock.sendMessage(jid, { text: msg });
                                } catch (e) {
                                    console.error('[setUserLimit notify]', e.message);
                                }
                            }
                            result.msg = `تم تعيين حد ${limit} رسالة للمستخدم ${num}`;
                        }
                    }
                    else if (action === 'resetUserLimit') {
                        // إعادة المستخدم للحد الافتراضي — العداد الحالي يبقى كما هو
                        const num = (data.num || '').replace(/\D/g, '');
                        if (num) {
                            delete userLimits[num];
                            // لا نصفّر العداد — المستخدم المتجاوز يبقى متجاوزاً حتى منتصف الليل
                            saveData();
                        }
                        result.msg = 'تم إعادة الحد للافتراضي';
                    }
                    else if (action === 'broadcast') {
                        if (!isConnected) { result = { ok: false, msg: 'البوت غير متصل' }; }
                        else {
                            const txt = data.text || '';
                            if (!txt) { result = { ok: false, msg: 'الرسالة فارغة' }; }
                            else {
                                broadcastToAll(`📢 *رسالة من الإدارة*\n\n${txt}`).then(r => {
                                    console.log(`📢 بث: ${r.sent} نجح، ${r.failed} فشل`);
                                }).catch(console.error);
                                result.msg = 'بدأ الإرسال في الخلفية';
                            }
                        }
                    }
                    else if (action === 'stats') {
                        result.data = {
                            connected: isConnected,
                            users: Object.keys(welcomedUsers).length,
                            active: Object.keys(userChats).length,
                            vip: vipNumbers.length,
                            reports: reports.length,
                            stats
                        };
                    }

                    res.writeHead(200, { 'Content-Type': 'application/json' });
                    res.end(JSON.stringify(result));
                } catch (e) {
                    res.writeHead(400, { 'Content-Type': 'application/json' });
                    res.end(JSON.stringify({ ok: false, msg: e.message }));
                }
            return;
        }

        // ===== DATA API =====
        if (url === '/data') {
            // بناء بيانات الاستهلاك لكل مستخدم
            const userUsage = {};
            for (const num of Object.keys(welcomedUsers)) {
                const cleanNum = cleanNumber(num);
                const rec = userLimitsUsage[cleanNum];
                const limit = getUserDailyLimit(cleanNum);
                userUsage[cleanNum] = {
                    used: rec ? rec.messages : 0,
                    images: rec ? rec.images : 0,
                    docs: rec ? rec.docs : 0,
                    limit,
                    remaining: Math.max(0, limit - (rec ? rec.messages : 0)),
                    resetAt: rec ? rec.activatedAt : null
                };
            }
            const d = {
                connected: isConnected,
                hasQR: !!currentQR,
                botName: BOT_NAME,
                defaultLimit: DAILY_MSG_LIMIT,
                stats: {
                    users: Object.keys(welcomedUsers).length,
                    active: Object.keys(userChats).length,
                    vip: vipNumbers.length,
                    messages: stats.totalMessages || 0,
                    images: stats.totalImages || 0,
                    docs: stats.totalDocs || 0,
                    medical: stats.totalMedical || 0,
                    reports: reports.length
                },
                vipNumbers,
                vipExpiry,
                userLimits,
                userNames,
                userUsage,
                blacklist: blacklist || [],
                welcomedUsers: Object.keys(welcomedUsers).map(n => cleanNumber(n)).filter(Boolean),
                reports: reports.slice(-50).reverse()
            };
            res.writeHead(200, { 'Content-Type': 'application/json; charset=utf-8' });
            res.end(JSON.stringify(d));
            return;
        }

        // ===== MAIN DASHBOARD =====
        res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' });
        res.end(`<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>${BOT_NAME} — لوحة التحكم</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
:root{
  --bg:#0b0f1a;--surface:#111827;--surface2:#1a2235;--border:#1e2d45;
  --text:#e2e8f0;--muted:#64748b;--accent:#38bdf8;--accent2:#818cf8;
  --green:#22c55e;--red:#f87171;--yellow:#fbbf24;--purple:#a78bfa;--orange:#fb923c;
}
body{font-family:'Segoe UI',system-ui,Arial,sans-serif;background:var(--bg);color:var(--text);min-height:100vh;overflow-x:hidden}

/* ── SIDEBAR ── */
.sidebar{position:fixed;top:0;right:0;height:100vh;width:220px;background:var(--surface);border-left:1px solid var(--border);display:flex;flex-direction:column;z-index:200;transition:.3s}
.sidebar-logo{padding:20px 16px 14px;border-bottom:1px solid var(--border);display:flex;align-items:center;gap:10px}
.sidebar-logo .bot-icon{width:36px;height:36px;border-radius:10px;background:linear-gradient(135deg,#0ea5e9,#6366f1);display:flex;align-items:center;justify-content:center;font-size:18px}
.sidebar-logo .bot-name{font-size:15px;font-weight:700;color:var(--accent)}
.sidebar-logo .bot-status{font-size:10px;color:var(--muted);margin-top:2px}
.dot{width:8px;height:8px;border-radius:50%;background:var(--muted);display:inline-block;vertical-align:middle;transition:.3s}
.dot.on{background:var(--green);box-shadow:0 0 6px var(--green);animation:pulse 2s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.5}}
.nav{flex:1;padding:12px 8px;overflow-y:auto;display:flex;flex-direction:column;gap:2px}
.nav-item{display:flex;align-items:center;gap:10px;padding:9px 12px;border-radius:9px;cursor:pointer;font-size:13px;color:var(--muted);transition:.2s;user-select:none}
.nav-item:hover{background:var(--surface2);color:var(--text)}
.nav-item.active{background:rgba(56,189,248,.12);color:var(--accent);font-weight:600}
.nav-item .icon{font-size:16px;width:20px;text-align:center}
.nav-badge{margin-right:auto;background:var(--red);color:#fff;border-radius:99px;font-size:10px;font-weight:700;padding:1px 6px;min-width:18px;text-align:center}
.sidebar-footer{padding:12px 16px;border-top:1px solid var(--border);font-size:11px;color:var(--muted)}
.sidebar-footer strong{color:var(--text)}

/* ── MAIN ── */
.main{margin-right:220px;min-height:100vh;display:flex;flex-direction:column}
.topbar{padding:14px 24px;background:var(--surface);border-bottom:1px solid var(--border);display:flex;align-items:center;justify-content:space-between;position:sticky;top:0;z-index:100}
.topbar-left{display:flex;align-items:center;gap:12px}
.topbar-title{font-size:16px;font-weight:700;color:var(--text)}
.topbar-sub{font-size:12px;color:var(--muted)}
.topbar-right{display:flex;align-items:center;gap:10px}
.refresh-btn{background:var(--surface2);border:1px solid var(--border);color:var(--muted);padding:6px 12px;border-radius:8px;cursor:pointer;font-size:12px;transition:.2s}
.refresh-btn:hover{color:var(--accent);border-color:var(--accent)}
.logout-btn{background:rgba(248,113,113,.12);border:1px solid rgba(248,113,113,.25);color:var(--red);padding:6px 14px;border-radius:8px;cursor:pointer;font-size:12px;transition:.2s;text-decoration:none}
.logout-btn:hover{background:rgba(248,113,113,.22)}
#last-updated{font-size:11px;color:var(--muted)}

/* ── CONTENT ── */
.content{padding:20px 24px;flex:1}
.panel{display:none}.panel.active{display:block;animation:fadeIn .2s}
@keyframes fadeIn{from{opacity:0;transform:translateY(4px)}to{opacity:1;transform:none}}

/* ── STATS GRID ── */
.stats-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:12px;margin-bottom:20px}
.stat-card{background:var(--surface);border:1px solid var(--border);border-radius:14px;padding:16px;position:relative;overflow:hidden;transition:.2s}
.stat-card::before{content:'';position:absolute;top:-20px;right:-20px;width:70px;height:70px;border-radius:50%;opacity:.08}
.stat-card.blue::before{background:var(--accent)}
.stat-card.green::before{background:var(--green)}
.stat-card.yellow::before{background:var(--yellow)}
.stat-card.red::before{background:var(--red)}
.stat-card.purple::before{background:var(--purple)}
.stat-card.orange::before{background:var(--orange)}
.stat-val{font-size:30px;font-weight:800;line-height:1}
.stat-card.blue .stat-val{color:var(--accent)}
.stat-card.green .stat-val{color:var(--green)}
.stat-card.yellow .stat-val{color:var(--yellow)}
.stat-card.red .stat-val{color:var(--red)}
.stat-card.purple .stat-val{color:var(--purple)}
.stat-card.orange .stat-val{color:var(--orange)}
.stat-label{font-size:11px;color:var(--muted);margin-top:6px}

/* ── CARDS ── */
.card{background:var(--surface);border:1px solid var(--border);border-radius:14px;overflow:hidden;margin-bottom:16px}
.card-header{padding:14px 18px;background:rgba(0,0,0,.25);border-bottom:1px solid var(--border);display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:8px}
.card-title{font-size:13px;font-weight:700;color:var(--text);display:flex;align-items:center;gap:6px}
.card-actions{display:flex;gap:8px;align-items:center;flex-wrap:wrap}

/* ── TABLE ── */
.tbl-wrap{overflow-x:auto}
table{width:100%;border-collapse:collapse}
th{padding:9px 14px;text-align:right;font-size:11px;color:var(--muted);background:rgba(0,0,0,.2);border-bottom:1px solid var(--border);white-space:nowrap}
td{padding:10px 14px;font-size:12px;border-bottom:1px solid rgba(255,255,255,.03);vertical-align:middle}
tr:last-child td{border-bottom:none}
tr:hover td{background:rgba(56,189,248,.03)}
.empty{text-align:center;color:var(--muted);padding:30px;font-size:13px}

/* ── BUTTONS ── */
.btn{border:none;padding:5px 11px;border-radius:7px;cursor:pointer;font-size:11px;font-weight:700;transition:.15s;white-space:nowrap}
.btn:hover{filter:brightness(1.15)}
.btn-primary{background:var(--accent);color:#0f172a}
.btn-success{background:var(--green);color:#0f172a}
.btn-danger{background:#dc2626;color:#fff}
.btn-warn{background:var(--yellow);color:#0f172a}
.btn-purple{background:#7c3aed;color:#fff}
.btn-ghost{background:var(--surface2);border:1px solid var(--border);color:var(--muted)}
.btn-ghost:hover{color:var(--accent);border-color:var(--accent)}

/* ── INPUTS ── */
.inp{background:var(--surface2);border:1px solid var(--border);color:var(--text);padding:8px 12px;border-radius:9px;font-size:12px;transition:.2s;font-family:inherit}
.inp:focus{outline:none;border-color:var(--accent);background:rgba(56,189,248,.05)}
.inp::placeholder{color:var(--muted)}
textarea.inp{resize:vertical;min-height:90px;width:100%}

/* ── BADGES ── */
.badge{padding:2px 9px;border-radius:99px;font-size:10px;font-weight:700;white-space:nowrap}
.badge-green{background:rgba(34,197,94,.15);color:var(--green)}
.badge-red{background:rgba(248,113,113,.15);color:var(--red)}
.badge-blue{background:rgba(56,189,248,.15);color:var(--accent)}
.badge-yellow{background:rgba(251,191,36,.15);color:var(--yellow)}
.badge-purple{background:rgba(167,139,250,.15);color:var(--purple)}
.badge-muted{background:rgba(100,116,139,.15);color:var(--muted)}

/* ── PROGRESS BAR ── */
.prog-wrap{display:flex;align-items:center;gap:8px}
.prog-bar{flex:1;height:5px;background:var(--border);border-radius:99px;overflow:hidden;min-width:60px}
.prog-fill{height:100%;border-radius:99px;transition:.3s}
.prog-fill.low{background:var(--green)}
.prog-fill.mid{background:var(--yellow)}
.prog-fill.high{background:var(--red)}
.prog-text{font-size:11px;color:var(--muted);white-space:nowrap;min-width:50px;text-align:left}

/* ── TOAST ── */
.toast{position:fixed;bottom:24px;left:50%;transform:translateX(-50%);padding:10px 22px;border-radius:12px;font-size:13px;opacity:0;transition:.3s;pointer-events:none;z-index:9999;font-weight:600;box-shadow:0 8px 32px rgba(0,0,0,.4)}
.toast.show{opacity:1;transform:translateX(-50%) translateY(-4px)}

/* ── QR ── */
.qr-wrap{display:flex;flex-direction:column;align-items:center;gap:20px;padding:32px}
.qr-wrap img{border-radius:16px;width:260px;border:4px solid var(--border)}
.qr-steps{background:var(--surface2);border-radius:12px;padding:16px 20px;font-size:13px;color:var(--muted);line-height:2.2;text-align:right;width:100%;max-width:340px}
.qr-steps span{color:var(--accent);font-weight:700}
.connected-msg{background:rgba(34,197,94,.1);border:1px solid rgba(34,197,94,.3);color:var(--green);padding:16px 28px;border-radius:14px;font-size:16px;font-weight:700;text-align:center}

/* ── SEARCH BAR ── */
.search-inp{background:var(--surface2);border:1px solid var(--border);color:var(--text);padding:7px 12px;border-radius:8px;font-size:12px;width:200px}
.search-inp:focus{outline:none;border-color:var(--accent)}

/* ── FORM ROW ── */
.form-row{display:flex;gap:8px;padding:14px 18px;background:rgba(0,0,0,.2);border-top:1px solid var(--border);flex-wrap:wrap;align-items:center}
.form-row label{font-size:12px;color:var(--muted)}

/* ── ACTION ROW ── */
.action-row{display:flex;gap:6px;flex-wrap:wrap}

/* ── QUICK ACTIONS ── */
.quick-actions{display:flex;gap:10px;flex-wrap:wrap;padding:16px 18px}

/* ── RESPONSIVE ── */
@media(max-width:760px){
  .sidebar{width:100%;height:auto;position:relative;border-left:none;border-bottom:1px solid var(--border)}
  .nav{flex-direction:row;flex-wrap:wrap;padding:8px}
  .nav-item{padding:6px 10px;font-size:12px}
  .main{margin-right:0}
  .stats-grid{grid-template-columns:repeat(auto-fill,minmax(120px,1fr))}
}

/* ── INFO BOX ── */
.info-box{background:rgba(56,189,248,.07);border:1px solid rgba(56,189,248,.2);border-radius:10px;padding:10px 14px;font-size:12px;color:var(--muted)}
.info-box strong{color:var(--accent)}

/* ── SECTION LABEL ── */
.section-label{font-size:11px;color:var(--muted);font-weight:700;text-transform:uppercase;letter-spacing:.05em;margin-bottom:8px;margin-top:4px;padding-right:4px}
</style>
</head>
<body>

<!-- SIDEBAR -->
<div class="sidebar">
  <div class="sidebar-logo">
    <div class="bot-icon">🤖</div>
    <div>
      <div class="bot-name">${BOT_NAME}</div>
      <div class="bot-status"><span class="dot" id="dot"></span> <span id="conn-label">جاري التحقق...</span></div>
    </div>
  </div>
  <nav class="nav">
    <div class="nav-item active" data-tab="overview" onclick="showTab('overview')"><span class="icon">📊</span> نظرة عامة</div>
    <div class="nav-item" data-tab="qr" onclick="showTab('qr')"><span class="icon">📱</span> ربط واتساب</div>
    <div class="nav-item" data-tab="users" onclick="showTab('users')"><span class="icon">👥</span> المستخدمون</div>
    <div class="nav-item" data-tab="usage" onclick="showTab('usage')"><span class="icon">📈</span> الاستهلاك</div>
    <div class="nav-item" data-tab="limits" onclick="showTab('limits')"><span class="icon">🔢</span> حدود الرسائل</div>
    <div class="nav-item" data-tab="vip" onclick="showTab('vip')"><span class="icon">⭐</span> VIP</div>
    <div class="nav-item" data-tab="blacklist" onclick="showTab('blacklist')"><span class="icon">⛔</span> المحظورون</div>
    <div class="nav-item" data-tab="broadcast" onclick="showTab('broadcast')"><span class="icon">📢</span> البث</div>
    <div class="nav-item" data-tab="reports" onclick="showTab('reports')"><span class="icon">🚨</span> البلاغات <span class="nav-badge" id="reports-badge" style="display:none">0</span></div>
  </nav>
  <div class="sidebar-footer">
    آخر تحديث: <strong id="last-updated">—</strong>
  </div>
</div>

<!-- MAIN -->
<div class="main">
  <div class="topbar">
    <div class="topbar-left">
      <div>
        <div class="topbar-title" id="page-title">نظرة عامة</div>
        <div class="topbar-sub" id="page-sub">إحصائيات البوت الكاملة</div>
      </div>
    </div>
    <div class="topbar-right">
      <button class="refresh-btn" onclick="loadData()">🔄 تحديث</button>
      <a href="/logout" class="logout-btn">🚪 خروج</a>
    </div>
  </div>

  <div class="content">

    <!-- ══ نظرة عامة ══ -->
    <div class="panel active" id="panel-overview">
      <div class="stats-grid" id="stats-grid">
        <div class="stat-card blue"><div class="stat-val" id="s-users">—</div><div class="stat-label">👥 إجمالي المستخدمين</div></div>
        <div class="stat-card green"><div class="stat-val" id="s-active">—</div><div class="stat-label">🟢 جلسات نشطة الآن</div></div>
        <div class="stat-card yellow"><div class="stat-val" id="s-vip">—</div><div class="stat-label">⭐ أعضاء VIP</div></div>
        <div class="stat-card purple"><div class="stat-val" id="s-msgs">—</div><div class="stat-label">💬 إجمالي الرسائل</div></div>
        <div class="stat-card blue"><div class="stat-val" id="s-imgs">—</div><div class="stat-label">🖼️ الصور المحللة</div></div>
        <div class="stat-card orange"><div class="stat-val" id="s-docs">—</div><div class="stat-label">📄 ملفات PDF</div></div>
        <div class="stat-card green"><div class="stat-val" id="s-med">—</div><div class="stat-label">🏥 صور طبية</div></div>
        <div class="stat-card red"><div class="stat-val" id="s-rep">—</div><div class="stat-label">🚨 البلاغات</div></div>
      </div>
      <div class="card">
        <div class="card-header"><div class="card-title">⚡ إجراءات سريعة</div></div>
        <div class="quick-actions">
          <button class="btn btn-danger" onclick="doAction('clearSessions')">🗑️ مسح كل الجلسات</button>
          <button class="btn btn-warn" onclick="showTab('broadcast')">📢 إرسال بث جماعي</button>
          <button class="btn btn-primary" onclick="loadData()">🔄 تحديث البيانات</button>
        </div>
      </div>
      <div class="card">
        <div class="card-header"><div class="card-title">📶 حالة النظام</div></div>
        <div style="padding:16px 18px;display:flex;flex-direction:column;gap:10px">
          <div style="display:flex;justify-content:space-between;align-items:center;font-size:13px">
            <span style="color:var(--muted)">حالة البوت</span>
            <span id="sys-status">—</span>
          </div>
          <div style="display:flex;justify-content:space-between;align-items:center;font-size:13px">
            <span style="color:var(--muted)">نسبة المحظورين</span>
            <span id="sys-blocked">—</span>
          </div>
          <div style="display:flex;justify-content:space-between;align-items:center;font-size:13px">
            <span style="color:var(--muted)">الحد الافتراضي الشهري</span>
            <span id="sys-deflimit">—</span>
          </div>
          <div style="display:flex;justify-content:space-between;align-items:center;font-size:13px">
            <span style="color:var(--muted)">نسبة VIP</span>
            <span id="sys-vippct">—</span>
          </div>
        </div>
      </div>
    </div>

    <!-- ══ ربط واتساب ══ -->
    <div class="panel" id="panel-qr">
      <div class="card">
        <div class="card-header"><div class="card-title">📱 حالة الاتصال بواتساب</div></div>
        <div class="qr-wrap" id="qr-section">
          <div style="color:var(--muted)">جاري التحميل...</div>
        </div>
      </div>
    </div>

    <!-- ══ المستخدمون ══ -->
    <div class="panel" id="panel-users">
      <div class="card">
        <div class="card-header">
          <div class="card-title">👥 إدارة المستخدمين</div>
          <div class="card-actions">
            <input class="search-inp" id="user-search" placeholder="🔍 بحث بالرقم أو الاسم..." oninput="filterUsers()">
          </div>
        </div>
        <div class="tbl-wrap">
          <table>
            <thead><tr>
              <th>الرقم</th><th>الاسم</th><th>الحالة</th>
              <th>الاستهلاك الشهري</th><th>الحد المخصص</th><th>الإجراءات</th>
            </tr></thead>
            <tbody id="users-table"><tr><td colspan="6" class="empty">جاري التحميل...</td></tr></tbody>
          </table>
        </div>
      </div>
    </div>

    <!-- ══ الاستهلاك ══ -->
    <div class="panel" id="panel-usage">
      <div class="card">
        <div class="card-header">
          <div class="card-title">📈 تقرير الاستهلاك الشهري</div>
          <div class="card-actions">
            <input class="search-inp" id="usage-search" placeholder="🔍 بحث..." oninput="filterUsage()">
            <select class="search-inp" id="usage-filter" onchange="filterUsage()" style="width:130px">
              <option value="all">الكل</option>
              <option value="active">نشطون هذا الشهر</option>
              <option value="full">وصلوا الحد</option>
              <option value="vip">VIP فقط</option>
            </select>
          </div>
        </div>
        <div class="tbl-wrap">
          <table>
            <thead><tr>
              <th>الرقم</th><th>الاسم</th><th>الحالة</th>
              <th>رسائل مستخدمة</th><th>صور</th><th>ملفات</th>
              <th>الحد الشهري</th><th>المتبقي</th><th>نسبة الاستهلاك</th>
            </tr></thead>
            <tbody id="usage-table"><tr><td colspan="9" class="empty">جاري التحميل...</td></tr></tbody>
          </table>
        </div>
      </div>
    </div>

    <!-- ══ حدود الرسائل ══ -->
    <div class="panel" id="panel-limits">
      <div class="card">
        <div class="card-header">
          <div class="card-title">🔢 حدود الرسائل الشهرية</div>
          <span id="default-limit-label" style="font-size:12px;color:var(--muted)"></span>
        </div>
        <div class="form-row">
          <label>تعيين حد لمستخدم:</label>
          <input class="inp" id="limit-num" placeholder="رقم الهاتف" dir="ltr" style="width:170px">
          <input class="inp" id="limit-val" placeholder="عدد الرسائل" type="number" min="0" style="width:120px">
          <button class="btn btn-primary" onclick="setUserLimit()">✅ تعيين وإشعار</button>
          <button class="btn btn-danger" onclick="resetUserLimit()">↩️ إعادة للافتراضي</button>
        </div>
        <div class="tbl-wrap">
          <table>
            <thead><tr><th>الرقم</th><th>الاسم</th><th>الحد المخصص</th><th>الاستخدام هذا الشهر</th><th>إجراء</th></tr></thead>
            <tbody id="limits-table"><tr><td colspan="5" class="empty">لا يوجد حدود مخصصة</td></tr></tbody>
          </table>
        </div>
      </div>
    </div>

    <!-- ══ VIP ══ -->
    <div class="panel" id="panel-vip">
      <div class="card">
        <div class="card-header"><div class="card-title">⭐ أعضاء VIP (اشتراك شهري — غير محدود)</div></div>
        <div class="tbl-wrap">
          <table>
            <thead><tr><th>الرقم</th><th>الاسم</th><th>ينتهي بعد</th><th>الإجراء</th></tr></thead>
            <tbody id="vip-table"><tr><td colspan="4" class="empty">جاري التحميل...</td></tr></tbody>
          </table>
        </div>
        <div class="form-row" style="margin-top:12px">
          <input class="inp" id="new-vip" placeholder="رقم الهاتف مع كود الدولة" dir="ltr" style="flex:1;min-width:200px">
          <button class="btn btn-success" onclick="addVip()">⭐ تفعيل VIP (شهر)</button>
        </div>
      </div>
    </div>

    <!-- ══ المحظورون ══ -->
    <div class="panel" id="panel-blacklist">
      <div class="card">
        <div class="card-header"><div class="card-title">⛔ قائمة المحظورين</div></div>
        <div class="form-row">
          <label>حظر مستخدم:</label>
          <input class="inp" id="bl-num" placeholder="رقم الهاتف مع كود الدولة" dir="ltr" style="width:200px">
          <button class="btn btn-danger" onclick="addBlacklist()">⛔ حظر وإشعار</button>
        </div>
        <div class="tbl-wrap">
          <table>
            <thead><tr><th>الرقم</th><th>الاسم</th><th>الإجراء</th></tr></thead>
            <tbody id="blacklist-table"><tr><td colspan="3" class="empty">لا يوجد مستخدمون محظورون</td></tr></tbody>
          </table>
        </div>
      </div>
    </div>

    <!-- ══ البث ══ -->
    <div class="panel" id="panel-broadcast">
      <div class="card">
        <div class="card-header"><div class="card-title">📢 إرسال رسالة جماعية لجميع المستخدمين</div></div>
        <div style="padding:18px;display:flex;flex-direction:column;gap:12px">
          <textarea class="inp" id="broadcast-text" placeholder="اكتب الرسالة هنا..."></textarea>
          <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
            <button class="btn btn-primary" onclick="sendBroadcast()">📢 إرسال للجميع</button>
            <span id="broadcast-count" style="font-size:12px;color:var(--muted)"></span>
          </div>
          <div class="info-box">⚠️ الرسالة ستُرسل لجميع المستخدمين مع تأخير 800ms بين كل رسالة لتجنب الحظر.</div>
        </div>
      </div>
    </div>

    <!-- ══ البلاغات ══ -->
    <div class="panel" id="panel-reports">
      <div class="card">
        <div class="card-header">
          <div class="card-title">🚨 البلاغات المستلمة</div>
          <button class="btn btn-danger" onclick="clearReports()">🗑️ مسح الكل</button>
        </div>
        <div class="tbl-wrap">
          <table>
            <thead><tr><th>#</th><th>الاسم</th><th>الرقم</th><th>المشكلة</th><th>الوقت</th><th>إجراء</th></tr></thead>
            <tbody id="reports-table"><tr><td colspan="6" class="empty">جاري التحميل...</td></tr></tbody>
          </table>
        </div>
      </div>
    </div>

  </div><!-- /content -->
</div><!-- /main -->

<div class="toast" id="toast"></div>

<script>
function esc(s){
  if(s==null)return'';
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#x27;');
}

const PAGE_META={
  overview:{title:'نظرة عامة',sub:'إحصائيات البوت الكاملة'},
  qr:{title:'ربط واتساب',sub:'امسح رمز QR لربط الجهاز'},
  users:{title:'المستخدمون',sub:'إدارة وصلاحيات كل مستخدم'},
  usage:{title:'الاستهلاك الشهري',sub:'تقرير استهلاك الرسائل والصور والملفات'},
  limits:{title:'حدود الرسائل',sub:'تخصيص الحد الشهري لكل مستخدم'},
  vip:{title:'أعضاء VIP',sub:'مستخدمون بصلاحيات غير محدودة'},
  blacklist:{title:'المحظورون',sub:'قائمة المحظورين من استخدام البوت'},
  broadcast:{title:'البث الجماعي',sub:'إرسال رسالة لجميع المستخدمين'},
  reports:{title:'البلاغات',sub:'البلاغات المرسلة من المستخدمين'}
};

let _data=null;

function showTab(name){
  document.querySelectorAll('.nav-item').forEach(el=>el.classList.toggle('active',el.dataset.tab===name));
  document.querySelectorAll('.panel').forEach(p=>p.classList.remove('active'));
  document.getElementById('panel-'+name).classList.add('active');
  const m=PAGE_META[name]||{title:name,sub:''};
  document.getElementById('page-title').textContent=m.title;
  document.getElementById('page-sub').textContent=m.sub;
  if(_data)updateUI();
}

function toast(msg,color){
  const t=document.getElementById('toast');
  t.textContent=msg;
  t.style.background=color||'#16a34a';
  t.classList.add('show');
  setTimeout(()=>t.classList.remove('show'),3000);
}

async function api(action,data={}){
  try{
    const r=await fetch('/api',{method:'POST',credentials:'include',headers:{'Content-Type':'application/json'},body:JSON.stringify({action,data})});
    if(r.status===401){window.location.href='/login';return{ok:false};}
    return await r.json();
  }catch(e){toast('خطأ في الاتصال','#dc2626');return{ok:false};}
}

async function loadData(){
  try{
    const r=await fetch('/data',{credentials:'include'});
    if(r.status===401){window.location.href='/login';return;}
    _data=await r.json();
    updateUI();
    document.getElementById('last-updated').textContent=new Date().toLocaleTimeString('ar');
  }catch(e){console.error(e);}
}

function updateUI(){
  if(!_data)return;
  const d=_data;
  const s=d.stats;

  // sidebar status
  const dot=document.getElementById('dot');
  const lbl=document.getElementById('conn-label');
  dot.className='dot'+(d.connected?' on':'');
  lbl.textContent=d.connected?'متصل':'غير متصل';

  // stats
  setText('s-users',s.users);setText('s-active',s.active);setText('s-vip',s.vip);
  setText('s-msgs',s.messages);setText('s-imgs',s.images);setText('s-docs',s.docs);
  setText('s-med',s.medical);setText('s-rep',s.reports);

  // reports badge
  const rb=document.getElementById('reports-badge');
  if(s.reports>0){rb.style.display='inline';rb.textContent=s.reports;}else{rb.style.display='none';}

  // system info
  const sysSt=document.getElementById('sys-status');
  if(sysSt)sysSt.innerHTML=d.connected?'<span class="badge badge-green">✅ متصل وشغال</span>':'<span class="badge badge-red">❌ غير متصل</span>';
  const sysBlocked=document.getElementById('sys-blocked');
  if(sysBlocked)sysBlocked.textContent=s.users>0?(((d.blacklist||[]).length/s.users*100).toFixed(1)+'%'):'0%';
  const sysDeflimit=document.getElementById('sys-deflimit');
  if(sysDeflimit)sysDeflimit.innerHTML='<span class="badge badge-blue">'+(d.defaultLimit||20)+' رسالة/شهر</span>';
  const sysVipPct=document.getElementById('sys-vippct');
  if(sysVipPct)sysVipPct.textContent=s.users>0?((s.vip/s.users*100).toFixed(1)+'%'):'0%';

  // broadcast count
  const bc=document.getElementById('broadcast-count');
  if(bc)bc.textContent='سيصل لـ '+(Math.max(0,s.users-1))+' مستخدم';

  // default limit label
  const dll=document.getElementById('default-limit-label');
  if(dll)dll.textContent='الحد الافتراضي: '+(d.defaultLimit||20)+' رسالة/شهر';

  // QR
  const qrSec=document.getElementById('qr-section');
  if(d.connected){
    qrSec.innerHTML='<div class="connected-msg">✅ البوت متصل بواتساب وجاهز للرد!</div>';
  }else if(d.hasQR){
    qrSec.innerHTML=\`<img src="/qr-image?t=\${Date.now()}" alt="QR Code">
    <div class="qr-steps">
      <div><span>1.</span> افتح واتساب على هاتفك</div>
      <div><span>2.</span> اذهب إلى الإعدادات ← الأجهزة المرتبطة</div>
      <div><span>3.</span> اضغط "ربط جهاز"</div>
      <div><span>4.</span> امسح هذا الرمز</div>
    </div>\`;
  }else{
    qrSec.innerHTML='<div style="color:var(--muted);font-size:14px">⏳ في انتظار رمز QR... يتجدد تلقائياً.</div>';
  }

  renderUsers(d);
  renderUsage(d);
  renderLimits(d);
  renderVip(d);
  renderBlacklist(d);
  renderReports(d);
}

function setText(id,val){const el=document.getElementById(id);if(el)el.textContent=val??'—';}

// ── USERS ──
let _allUsers=[];
function renderUsers(d){
  _allUsers=d.welcomedUsers.map(num=>({
    num,
    name:d.userNames[num]||'—',
    isVip:(d.vipNumbers||[]).includes(num),
    isBlocked:(d.blacklist||[]).includes(num),
    usage:d.userUsage?d.userUsage[num]:null,
    customLimit:d.userLimits?d.userLimits[num]:null
  }));
  filterUsers();
}
function filterUsers(){
  const q=(document.getElementById('user-search')?.value||'').toLowerCase();
  const filtered=q?_allUsers.filter(u=>u.num.includes(q)||u.name.toLowerCase().includes(q)):_allUsers;
  const tb=document.getElementById('users-table');
  if(!filtered.length){tb.innerHTML='<tr><td colspan="6" class="empty">لا يوجد مستخدمون</td></tr>';return;}
  tb.innerHTML=filtered.slice(0,200).map(u=>{
    const usage=u.usage||{used:0,limit:20,remaining:20,images:0,docs:0};
    const pct=usage.limit>0?Math.min(100,Math.round(usage.used/usage.limit*100)):0;
    const fillClass=pct>=100?'high':pct>=70?'mid':'low';
    const progHtml=\`<div class="prog-wrap"><div class="prog-bar"><div class="prog-fill \${fillClass}" style="width:\${pct}%"></div></div><div class="prog-text">\${usage.used}/\${usage.limit}</div></div>\`;
    const customLimitBadge=u.customLimit!=null?\`<span class="badge badge-yellow">\${u.customLimit}</span>\`:'<span class="badge badge-muted">افتراضي</span>';
    const statusBadge=u.isBlocked?'<span class="badge badge-red">⛔ محظور</span>':u.isVip?'<span class="badge badge-blue">⭐ VIP</span>':'<span class="badge badge-green">عادي</span>';
    const vipBtn=u.isVip?\`<button class="btn btn-warn" onclick="removeVipNum(\${JSON.stringify(u.num)})">إزالة VIP</button>\`:\`<button class="btn btn-primary" onclick="addVipNum(\${JSON.stringify(u.num)})">+ VIP</button>\`;
    const blockBtn=u.isBlocked?\`<button class="btn btn-success" onclick="removeBlacklistNum(\${JSON.stringify(u.num)})">✅ رفع حظر</button>\`:\`<button class="btn btn-purple" onclick="blockUser(\${JSON.stringify(u.num)})">⛔ حظر</button>\`;
    return \`<tr>
      <td dir="ltr" style="font-family:monospace;color:var(--accent)">\${esc(u.num)}</td>
      <td>\${esc(u.name)}</td>
      <td>\${statusBadge}</td>
      <td>\${progHtml}</td>
      <td>\${customLimitBadge}</td>
      <td><div class="action-row">
        \${!u.isBlocked?vipBtn:''}
        <button class="btn btn-warn" onclick="quickSetLimit(\${JSON.stringify(u.num)})">🔢 حد</button>
        \${!u.isBlocked?blockBtn:blockBtn}
        <button class="btn btn-danger" onclick="deleteUser(\${JSON.stringify(u.num)})">🗑️</button>
      </div></td>
    </tr>\`;
  }).join('');
}

// ── USAGE ──
let _allUsage=[];
function renderUsage(d){
  _allUsage=d.welcomedUsers.map(num=>({
    num,name:d.userNames[num]||'—',
    isVip:(d.vipNumbers||[]).includes(num),
    isBlocked:(d.blacklist||[]).includes(num),
    ...(d.userUsage?d.userUsage[num]:{used:0,images:0,docs:0,limit:20,remaining:20,resetAt:null})
  }));
  filterUsage();
}
function filterUsage(){
  const q=(document.getElementById('usage-search')?.value||'').toLowerCase();
  const filt=document.getElementById('usage-filter')?.value||'all';
  let arr=_allUsage;
  if(q)arr=arr.filter(u=>u.num.includes(q)||u.name.toLowerCase().includes(q));
  if(filt==='active')arr=arr.filter(u=>u.used>0||u.images>0||u.docs>0);
  if(filt==='full')arr=arr.filter(u=>u.remaining<=0&&!u.isVip);
  if(filt==='vip')arr=arr.filter(u=>u.isVip);
  arr=arr.slice().sort((a,b)=>b.used-a.used);
  const tb=document.getElementById('usage-table');
  if(!arr.length){tb.innerHTML='<tr><td colspan="9" class="empty">لا توجد بيانات</td></tr>';return;}
  tb.innerHTML=arr.slice(0,200).map(u=>{
    const pct=u.limit>0?Math.min(100,Math.round(u.used/u.limit*100)):0;
    const fillClass=pct>=100?'high':pct>=70?'mid':'low';
    const statusBadge=u.isBlocked?'<span class="badge badge-red">⛔ محظور</span>':u.isVip?'<span class="badge badge-blue">♾️ VIP</span>':u.remaining<=0?'<span class="badge badge-red">🔴 محدود</span>':'<span class="badge badge-green">🟢 نشط</span>';
    const progHtml=\`<div class="prog-wrap" style="min-width:100px"><div class="prog-bar"><div class="prog-fill \${fillClass}" style="width:\${pct}%"></div></div><div class="prog-text">\${pct}%</div></div>\`;
    const resetStr=u.resetAt?new Date(u.resetAt).toLocaleTimeString('ar',{hour:'2-digit',minute:'2-digit'}):'—';
    return \`<tr>
      <td dir="ltr" style="font-family:monospace;font-size:11px;color:var(--accent)">\${esc(u.num)}</td>
      <td>\${esc(u.name)}</td>
      <td>\${statusBadge}</td>
      <td style="text-align:center">\${u.used}</td>
      <td style="text-align:center">\${u.images}</td>
      <td style="text-align:center">\${u.docs}</td>
      <td style="text-align:center">\${u.isVip?'♾️':u.limit}</td>
      <td style="text-align:center">\${u.isVip?'♾️':u.remaining}</td>
      <td>\${u.isVip?'<span class="badge badge-blue">غير محدود</span>':progHtml}</td>
    </tr>\`;
  }).join('');
}

// ── LIMITS ──
function renderLimits(d){
  const tb=document.getElementById('limits-table');
  const limits=d.userLimits||{};
  const keys=Object.keys(limits);
  if(!keys.length){tb.innerHTML='<tr><td colspan="5" class="empty">لا يوجد حدود مخصصة — الكل على الافتراضي ('+(d.defaultLimit||20)+')</td></tr>';return;}
  tb.innerHTML=keys.map(num=>{
    const usage=d.userUsage?d.userUsage[num]:null;
    const used=usage?usage.used:0;
    const lim=limits[num];
    const pct=lim>0?Math.min(100,Math.round(used/lim*100)):0;
    const fillClass=pct>=100?'high':pct>=70?'mid':'low';
    return \`<tr>
      <td dir="ltr" style="font-family:monospace;color:var(--accent)">\${esc(num)}</td>
      <td>\${esc(d.userNames[num]||'—')}</td>
      <td><span class="badge badge-blue">\${lim} رسالة/شهر</span></td>
      <td><div class="prog-wrap"><div class="prog-bar"><div class="prog-fill \${fillClass}" style="width:\${pct}%"></div></div><div class="prog-text">\${used}/\${lim}</div></div></td>
      <td><button class="btn btn-danger" onclick="resetUserLimitNum(\${JSON.stringify(num)})">↩️ إعادة</button></td>
    </tr>\`;
  }).join('');
}

// ── VIP ──
function renderVip(d){
  const tb=document.getElementById('vip-table');
  if(!d.vipNumbers.length){tb.innerHTML='<tr><td colspan="4" class="empty">لا يوجد أعضاء VIP</td></tr>';return;}
  tb.innerHTML=d.vipNumbers.map(num=>{
    const expiry=d.vipExpiry?d.vipExpiry[num]:null;
    let expiryStr='<span class="badge badge-green">دائم ♾️</span>';
    if(expiry){
      const now=Date.now();
      const diff=expiry-now;
      if(diff<0){expiryStr='<span class="badge badge-red">منتهي ⛔</span>';}
      else{
        const days=Math.ceil(diff/(24*60*60*1000));
        expiryStr='<span class="badge badge-yellow">'+days+' يوم</span>';
      }
    }
    return \`<tr>
      <td dir="ltr" style="font-family:monospace;color:var(--accent)">\${esc(num)}</td>
      <td>\${esc(d.userNames[num]||'—')}</td>
      <td>\${expiryStr}</td>
      <td>
        <div class="action-row">
          <button class="btn btn-primary" onclick="renewVip(\${JSON.stringify(num)})">🔄 تجديد</button>
          <button class="btn btn-danger" onclick="removeVipNum(\${JSON.stringify(num)})">❌ إزالة</button>
        </div>
      </td>
    </tr>\`;
  }).join('');
}

// ── BLACKLIST ──
function renderBlacklist(d){
  const tb=document.getElementById('blacklist-table');
  const bl=d.blacklist||[];
  if(!bl.length){tb.innerHTML='<tr><td colspan="3" class="empty">لا يوجد مستخدمون محظورون</td></tr>';return;}
  tb.innerHTML=bl.map(num=>{
    return \`<tr>
      <td dir="ltr" style="font-family:monospace;color:var(--red)">\${esc(num)}</td>
      <td>\${esc(d.userNames[num]||'—')}</td>
      <td><button class="btn btn-success" onclick="removeBlacklistNum(\${JSON.stringify(num)})">✅ رفع الحظر</button></td>
    </tr>\`;
  }).join('');
}

// ── REPORTS ──
function renderReports(d){
  const tb=document.getElementById('reports-table');
  if(!d.reports.length){tb.innerHTML='<tr><td colspan="6" class="empty">لا يوجد بلاغات</td></tr>';return;}
  tb.innerHTML=d.reports.map((r,i)=>{
    return \`<tr>
      <td style="color:var(--muted)">\${i+1}</td>
      <td>\${esc(r.name||'—')}</td>
      <td dir="ltr" style="font-size:11px;font-family:monospace;color:var(--accent)">\${esc(r.sender)}</td>
      <td style="max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="\${esc(r.text)}">\${esc(r.text)}</td>
      <td style="font-size:11px;color:var(--muted)">\${esc(r.time)}</td>
      <td><button class="btn btn-purple" onclick="blockUser(\${JSON.stringify(r.sender)})">⛔ حظر</button></td>
    </tr>\`;
  }).join('');
}

// ── ACTIONS ──
async function doAction(action){
  if(action==='clearSessions'&&!confirm('مسح كل الجلسات النشطة؟'))return;
  const r=await api(action);
  toast(r.msg||'✅ تم');
  loadData();
}
async function sendBroadcast(){
  const text=document.getElementById('broadcast-text').value.trim();
  if(!text){toast('اكتب الرسالة أولاً','#f59e0b');return;}
  if(!confirm('إرسال لجميع المستخدمين؟'))return;
  const r=await api('broadcast',{text});
  if(r.ok){toast('✅ '+(r.msg||'بدأ الإرسال'));document.getElementById('broadcast-text').value='';}
  else toast(r.msg||'فشل الإرسال','#dc2626');
}
async function blockUser(num){
  if(!confirm('حظر المستخدم '+num+'؟ سيصله إشعار تلقائي.'))return;
  const r=await api('addBlacklist',{num:String(num)});
  if(r.ok){toast('⛔ '+r.msg);loadData();}
  else toast('❌ '+(r.msg||'فشل الحظر'),'#dc2626');
}
async function addBlacklist(){
  const raw=document.getElementById('bl-num').value.trim();
  const num=raw.replace(/\D/g,'');
  if(!num){toast('❌ أدخل رقم هاتف صحيح','#dc2626');return;}
  if(!confirm('حظر المستخدم '+num+'؟ سيصله إشعار تلقائي.'))return;
  const r=await api('addBlacklist',{num});
  if(r.ok){toast('⛔ '+r.msg);document.getElementById('bl-num').value='';loadData();}
  else toast('❌ '+(r.msg||'فشل'),'#dc2626');
}
async function removeBlacklistNum(num){
  if(!confirm('رفع الحظر عن '+num+'؟'))return;
  const r=await api('removeBlacklist',{num:String(num)});
  if(r.ok){toast('✅ '+r.msg);loadData();}
  else toast('❌ '+(r.msg||'فشل'),'#dc2626');
}
async function addVip(){
  const raw=document.getElementById('new-vip').value.trim();
  const num=raw.replace(/\D/g,'');
  if(!num){toast('❌ أدخل رقم هاتف صحيح','#dc2626');return;}
  const r=await api('addVip',{num});
  if(r.ok){toast('⭐ '+r.msg);document.getElementById('new-vip').value='';loadData();}
  else toast('❌ '+(r.msg||'فشل'),'#dc2626');
}
async function addVipNum(num){
  if(!confirm('تفعيل VIP للمستخدم '+num+'؟'))return;
  const r=await api('addVip',{num:String(num)});
  if(r.ok){toast('⭐ '+r.msg);loadData();}
  else toast('❌ '+(r.msg||'فشل'),'#dc2626');
}
async function removeVipNum(num){
  if(!confirm('إزالة '+num+' من VIP؟ سيصله إشعار.'))return;
  const r=await api('removeVip',{num:String(num)});
  if(r.ok){toast('✅ '+r.msg);loadData();}
  else toast('❌ '+(r.msg||'فشل'),'#dc2626');
}
async function deleteUser(num){
  if(!confirm('حذف هذا المستخدم نهائياً؟ لا يمكن التراجع.'))return;
  const r=await api('deleteUser',{num:String(num)});
  if(r.ok){toast('✅ تم الحذف');loadData();}
  else toast('❌ '+(r.msg||'فشل'),'#dc2626');
}
async function clearReports(){
  if(!confirm('مسح كل البلاغات؟'))return;
  const r=await api('clearReports');
  if(r.ok){toast('تم مسح البلاغات');loadData();}
}
async function setUserLimit(){
  const num=document.getElementById('limit-num').value.replace(/\D/g,'');
  const limit=parseInt(document.getElementById('limit-val').value,10);
  if(!num){toast('أدخل رقم الهاتف','#f59e0b');return;}
  if(isNaN(limit)||limit<0){toast('أدخل عدداً صحيحاً','#f59e0b');return;}
  const r=await api('setUserLimit',{num,limit});
  if(r.ok){toast('✅ تم تعيين الحد وإشعار المستخدم');document.getElementById('limit-num').value='';document.getElementById('limit-val').value='';loadData();}
  else toast(r.msg||'فشل','#dc2626');
}
async function resetUserLimit(){
  const num=document.getElementById('limit-num').value.replace(/\D/g,'');
  if(!num){toast('أدخل رقم الهاتف','#f59e0b');return;}
  const r=await api('resetUserLimit',{num});
  if(r.ok){toast('تم الإعادة للافتراضي');loadData();}
}
async function renewVip(num){
  if(!confirm('تجديد VIP للمستخدم '+num+' لشهر إضافي؟'))return;
  const r=await api('addVip',{num:String(num)});
  if(r.ok){toast('✅ '+r.msg);loadData();}
  else toast('❌ '+(r.msg||'فشل'),'#dc2626');
}

async function quickSetLimit(num){
  const val=prompt('عدد الرسائل الشهرية للمستخدم '+num+':');
  if(val===null)return;
  const limit=parseInt(val,10);
  if(isNaN(limit)||limit<0){toast('رقم غير صحيح','#f59e0b');return;}
  const r=await api('setUserLimit',{num,limit});
  if(r.ok){toast('✅ تم وتم إشعار المستخدم');loadData();}
  else toast(r.msg||'فشل','#dc2626');
}
async function resetUserLimitNum(num){
  if(!confirm('إعادة حد '+num+' للافتراضي؟'))return;
  const r=await api('resetUserLimit',{num});
  if(r.ok){toast('تم الإعادة للافتراضي');loadData();}
  else toast(r.msg||'فشل','#dc2626');
}

loadData();
setInterval(loadData,30000);
</script>
</body>
</html>`);
    });

    function tryListen(port) {
        server.listen(port, '0.0.0.0')
            .once('listening', () => {
                console.log(`\n🌐 لوحة التحكم: http://localhost:${port}`);
                console.log(`🌐 من جهاز ثاني: http://10.158.171.59:${port}\n`);
            })
            .once('error', (e) => {
                if (e.code === 'EADDRINUSE') {
                    console.log(`⚠️ البورت ${port} مشغول، جاري المحاولة على ${port + 1}...`);
                    server.removeAllListeners('error');
                    server.removeAllListeners('listening');
                    tryListen(port + 1);
                } else {
                    console.error('[server]', e.message);
                }
            });
    }

    tryListen(WEB_PORT);
}

// ============================================================
// WELCOME MESSAGE
// ============================================================
function buildWelcome(name) {
    return buildModeMenu(name);
}

// ============================================================
// MAIN BOT
// ============================================================
async function startBot() {
    const { state, saveCreds } = await useMultiFileAuthState('./session');
    const { version }          = await fetchLatestBaileysVersion();

    sock = makeWASocket({
        version,
        auth:    state,
        browser: Browsers.macOS('Desktop'),
        syncFullHistory: true   // جلب الرسائل الفائتة عند إعادة الاتصال
    });

    sock.ev.on('creds.update', saveCreds);

    sock.ev.on('connection.update', async (update) => {
        const { connection, qr, lastDisconnect } = update;

        if (qr) {
            currentQR = qr;
            isConnected = false;
            console.log('\n📱 امسح QR من المتصفح: http://localhost:' + WEB_PORT + '\n');
            QRCode.generate(qr, { small: true });
        }

        if (connection === 'open') {
            currentQR = null;
            isConnected = true;
            console.log('✅ البوت متصل وجاهز!');
        }

        if (connection === 'close') {
            isConnected = false;
            const errCode = lastDisconnect?.error?.output?.statusCode;
            const shouldReconnect = errCode !== DisconnectReason.loggedOut;
            console.log('❌ انقطع الاتصال، الكود:', errCode);
            if (shouldReconnect) {
                if (!isReconnecting) {
                    isReconnecting = true;
                    let attempt = 0;
                    // المحاولة الأولى بعد ثانية واحدة فقط، ثم backoff تدريجي أقصاه 30 ثانية
                    const DELAYS = [1_000, 2_000, 5_000, 10_000, 15_000, 30_000];
                    const tryReconnect = () => {
                        const delay = DELAYS[Math.min(attempt, DELAYS.length - 1)];
                        attempt++;
                        console.log(`🔄 محاولة إعادة الاتصال #${attempt} خلال ${delay / 1000}ث...`);
                        setTimeout(async () => {
                            try {
                                isReconnecting = false;
                                await startBot();
                            } catch (e) {
                                console.error('[reconnect] فشلت المحاولة:', e.message);
                                isReconnecting = true;
                                tryReconnect();
                            }
                        }, delay);
                    };
                    tryReconnect();
                }
            } else {
                console.log('🚪 تم تسجيل الخروج. احذف مجلد session وأعد التشغيل.');
                isReconnecting = false;
            }
        }
        if (connection === 'open') {
            isReconnecting = false;
        }
    });

    sock.ev.on('messages.upsert', async ({ messages, type }) => {
        // 'notify' = رسالة جديدة، 'append' = رسائل فائتة — كلاهما مصفوفة
        if (type !== 'notify' && type !== 'append') return;

        const msgList = Array.isArray(messages) ? messages : (messages ? [messages] : []);

        for (const msg of msgList) {
        if (!msg?.message || msg?.key?.fromMe) continue;
        // تجاهل الرسائل القديمة أكثر من 10 دقائق (فقط في حالة append)
        const msgTs = (msg.messageTimestamp || 0) * 1000;
        if (type === 'append' && Date.now() - msgTs > 10 * 60 * 1000) continue;

        try {
            const message = msg.message || {};

            // استخراج نوع الرسالة
            const IGNORED_TYPES = new Set([
                'protocolMessage', 'senderKeyDistributionMessage',
                'messageContextInfo', 'reactionMessage', 'pollUpdateMessage'
            ]);
            const msgType = Object.keys(message).find(
                k => message[k] != null && !IGNORED_TYPES.has(k)
            );
            if (!msgType) continue;

            const jid     = msg.key?.remoteJid;
            if (!jid) continue;

            // تحديد نوع المحادثة
            const isGroup = jid.endsWith('@g.us');

            // استخراج رقم المرسل بشكل موثوق
            const sender = cleanNumber(
                msg.key?.participant || (isGroup ? msg.key?.participant : jid)
            );
            if (!sender) return;

            const isAdmin = sender === ADMIN_NUMBER;
            userChatLastSeen[sender] = Date.now(); // تحديث آخر نشاط

            // تعريف reply و react أولاً — قبل أي كود يستخدمهما
            const WA_CHUNK_LIMIT = 3800;
            function splitMessage(text) {
                text = String(text || '');
                if (text.length <= WA_CHUNK_LIMIT) return [text];
                const chunks = [];
                const pushChunk = (chunk) => {
                    if (!chunk) return;
                    // حماية من كلمة/رابط طويل جداً بلا مسافات: لا نفقد البيانات، نجزئها حرفياً.
                    for (let i = 0; i < chunk.length; i += WA_CHUNK_LIMIT) {
                        const part = chunk.slice(i, i + WA_CHUNK_LIMIT).trim();
                        if (part) chunks.push(part);
                    }
                };
                const lines = text.split('\n');
                let current = '';
                for (const line of lines) {
                    const candidate = current ? current + '\n' + line : line;
                    if (candidate.length > WA_CHUNK_LIMIT) {
                        if (current.trim()) pushChunk(current.trim());
                        if (line.length > WA_CHUNK_LIMIT) {
                            const words = line.split(' ');
                            let subCurrent = '';
                            for (const word of words) {
                                const sub = subCurrent ? subCurrent + ' ' + word : word;
                                if (sub.length > WA_CHUNK_LIMIT) {
                                    if (subCurrent.trim()) pushChunk(subCurrent.trim());
                                    subCurrent = '';
                                    if (word.length > WA_CHUNK_LIMIT) pushChunk(word);
                                    else subCurrent = word;
                                } else {
                                    subCurrent = sub;
                                }
                            }
                            current = subCurrent;
                        } else {
                            current = line;
                        }
                    } else {
                        current = candidate;
                    }
                }
                if (current.trim()) pushChunk(current.trim());
                return chunks.filter(c => c.length > 0);
            }
            const reply = async (text) => {
                try {
                    const parts = splitMessage(text);
                    if (parts.length === 1) {
                        await sock.sendMessage(jid, { text: parts[0] }, { quoted: msg });
                    } else {
                        await sock.sendMessage(jid, { text: parts[0] }, { quoted: msg });
                        for (let i = 1; i < parts.length; i++) {
                            await new Promise(r => setTimeout(r, 500));
                            await sock.sendMessage(jid, { text: parts[i] });
                        }
                    }
                } catch (e) {
                    console.error('[reply] خطأ:', e.message);
                }
            };

            const react = async (emoji) => {
                try {
                    await sock.sendMessage(jid, { react: { text: emoji, key: msg.key } });
                } catch {}
            };

            // ============================================================
            // فحص الحظر (Blacklist) — قبل أي معالجة
            // ============================================================
            if (blacklist.includes(sender)) {
                // نرسل رسالة الحظر مرة واحدة كل ساعة فقط (anti-spam)
                // نستخدم _lastBlockNotify منفصل حتى لا يتعارض مع إشعارات الأدمن
                const now = Date.now();
                const lastNotify = _lastBlockNotify[sender] || 0;
                if (now - lastNotify > 60 * 60_000) {
                    _lastBlockNotify[sender] = now;
                    await reply(BLACKLIST_MSG);
                }
                // حذف جلسة المحظور فوراً لتحرير الذاكرة
                delete userChats[sender];
                return;
            }

            // استخراج نص الرسالة
            const body = (
                message?.conversation ||
                message?.extendedTextMessage?.text ||
                message?.imageMessage?.caption ||
                message?.documentMessage?.caption ||
                message?.videoMessage?.caption ||
                ''
            ).trim();

            console.log(`📨 [${isAdmin ? 'ADMIN' : 'USER'}] ${sender} | ${msgType} | "${body.slice(0, 50)}"`);

            // ============================================================
            // ============================================================
    // أوامر الأدمن عبر واتساب (تعمل من رقم الأدمن فقط)
    // ============================================================
    if (isAdmin) {
        // !removevip [رقم]
        const removeVipMatch = body.match(/^!removevip\s+(\d+)/i);
        if (removeVipMatch) {
            const num = removeVipMatch[1].trim();
            const wasVip = vipNumbers.includes(num);
            vipNumbers = vipNumbers.filter(n => n !== num);
            delete vipExpiry[num];
            saveData();
            if (wasVip && sock && isConnected) {
                try { await sock.sendMessage(`${num}@s.whatsapp.net`, { text: `ℹ️ تم إلغاء اشتراكك المميز (VIP).\nللتجديد: wa.me/972593850520` }); } catch (_) {}
            }
            await reply(wasVip ? `✅ تم إزالة VIP عن ${num} وتم إشعاره.` : `⚠️ الرقم ${num} لم يكن VIP أصلاً.`);
            return true;
        }
        // !addvip [رقم]
        const addVipMatch = body.match(/^!addvip\s+(\d+)/i);
        if (addVipMatch) {
            const num = addVipMatch[1].trim();
            if (!vipNumbers.includes(num)) {
                vipNumbers.push(num);
                vipExpiry[num] = Date.now() + 30 * 24 * 60 * 60_000;
                resetUserUsage(num);
                saveData();
                try { await sock.sendMessage(`${num}@s.whatsapp.net`, { text: `🌟 *تهانينا! تم تفعيل اشتراكك المميز (VIP)*\nرسائل وصور وصوت غير محدودة ✨` }); } catch (_) {}
                await reply(`✅ تم تفعيل VIP للرقم ${num} لمدة شهر.`);
            } else {
                await reply(`⚠️ الرقم ${num} VIP أصلاً.`);
            }
            return true;
        }
        // !resetlimit [رقم]
        const resetLimitMatch = body.match(/^!resetlimit\s+(\d+)/i);
        if (resetLimitMatch) {
            const num = resetLimitMatch[1].trim();
            resetUserUsage(num);
            await reply(`✅ تم تصفير استهلاك ${num}.`);
            return true;
        }
    }

    // أوامر الأدمن — تُدار من لوحة التحكم فقط
            // الأدمن يستخدم البوت كمستخدم عادي
            // ============================================================

            // ============================================================
            // استخراج / تحديث اسم المستخدم
            // ============================================================
            let userName = userNames[sender];
            const pushName = msg.pushName?.trim(); // pushName هو الخاصية الصحيحة الوحيدة في messages.upsert
            if (pushName && pushName !== userName) {
                userNames[sender] = pushName;
                userName = pushName;
                saveData();
            }

            // ============================================================
            // رسالة الترحيب (للمستخدمين الجدد فقط - في المحادثات الخاصة)
            // ============================================================
            if (!welcomedUsers[sender]) {
                welcomedUsers[sender] = true;
                userChats[sender] = [];
                if (userName) {
                    userChats[sender].push({ role: 'user',      content: `[اسم المستخدم: ${userName}]` });
                    userChats[sender].push({ role: 'assistant', content: `أهلاً ${userName}، كيف أستطيع مساعدتك؟` });
                }
                saveData();
                // رسالة الترحيب فقط في المحادثات الخاصة
                if (!isGroup) {
                    await reply(buildWelcome(userName));
                    const isProcessable = body || ['imageMessage','documentMessage'].includes(msgType);
                    if (!isProcessable) return;
                }
            }

            // ── رسالة الفترة المجانية — تُرسل مرة واحدة فقط لكل مستخدم جديد غير VIP ──
            if (!isAdmin && !isActiveVIP(sender) && !trialNotified[sender]) {
                trialNotified[sender] = true;
                saveData();
                const rec = getDailyRecord(sender);
                const msgLimit  = getUserDailyLimit(sender);
                const imgLimit  = DAILY_IMG_LIMIT;
                const ttsLimit  = DAILY_TTS_LIMIT;
                await sock.sendMessage(jid, {
                    text:
                        `🎉 *مرحباً بك في MedTerm!*\n\n` +
                        `أنت الآن في *الباقة المجانية الشهرية المجانية* 🆓\n\n` +
                        `📊 *رصيدك الشهري المجاني:*\n` +
                        `💬 الرسائل النصية: ${msgLimit - rec.messages} / ${msgLimit}\n` +
                        `🖼️ الصور: ${imgLimit - rec.images} / ${imgLimit}\n` +
                        `🔊 الرسائل الصوتية: ${ttsLimit - rec.tts} / ${ttsLimit}\n\n` +
                        `⚠️ *ملاحظة:* هذا الرصيد يتجدد شهرياً للمستخدم المجاني.\n` +
                        `للاستمرار بعد انتهاء الرصيد، تواصل معنا للاشتراك الشهري 👇\n` +
                        `📲 wa.me/972593850520`
                });
            }

            // ============================================================
            // معالجة أنواع الرسائل
            // ============================================================

            // ── تذكير: إذا كان ينتظر PDF لنظام الاختبارات وأرسل نصاً ──
            if (userExamPending[sender] && msgType !== 'documentMessage') {
                await reply(
                    '📎 *أنت في وضع تفعيل نظام الاختبارات.*\n\n' +
                    'يرجى إرسال ملف *PDF* الآن للبدء.\n' +
                    '_أرسل "خروج" للإلغاء._'
                );
                return;
            }

            // --- صور ---
            if (msgType === 'imageMessage') {
                if (!checkSpam(sender)) {
                    await reply('⚠️ أرسلت صوراً بشكل متسارع، انتظر ثوانٍ ثم أعد المحاولة.');
                    return;
                }
                const isVIPimg = isAdmin || isActiveVIP(sender);
                if (!isVIPimg && !checkDailyLimit(sender, 'image')) {
                    await reply(
                        `⚠️ *وصلت للحد الشهري للصور* (${DAILY_IMG_LIMIT} صور/شهر)\n\n` +
                        `للاشتراك المميز (غير محدود):\n👤 wa.me/972593850520`
                    );
                    return;
                }
                await react('🔍');
                try {
                    const imgMsg  = message.imageMessage;
                    const imgSize = imgMsg?.fileLength || 0;
                    if (imgSize > 10 * 1024 * 1024) {
                        await react('❌');
                        await reply(`حجم الصورة كبير جداً (${(imgSize/1024/1024).toFixed(1)}MB).\nالحد الأقصى 10MB.`);
                        return;
                    }
                    const mime   = imgMsg?.mimetype || 'image/jpeg';
                    const buffer = await downloadMediaMessage(msg, 'buffer', {}, {
                        logger: { level: 'silent', child: () => ({ level: 'silent' }) }
                    });
                    if (!buffer || buffer.length === 0) {
                        await react('❌');
                        await reply('لم أتمكن من تنزيل الصورة، يرجى المحاولة مرة أخرى.');
                        return;
                    }

                    const caption = body ? body.trim() : '';

                    // تحسين الصورة محلياً إذا طلب المستخدم ذلك في الكابشن
                    if (caption && looksLikeImageEnhanceRequest(caption)) {
                        let out = null;
                        try {
                            out = await _enhanceImage(buffer, mime);
                            await sendLocalImage(jid, out.path, '✅ تم تحسين الصورة محلياً بدون استخدام الذكاء الصناعي.', msg);
                            await react('✅');
                        } catch (e) {
                            console.error('[image-enhance]', e.message);
                            notifyAdmin(`⚠️ فشل تحسين صورة محلياً: ${e.message}`).catch(()=>{});
                            await react('❌');
                            await reply(`تعذر تحسين الصورة محلياً حالياً. السبب: ${e.message.slice(0, 180)}`);
                        } finally {
                            if (out && out.path) cleanupLocalFile(out.path);
                        }
                        return;
                    }

                    stats.totalImages++;
                    saveData();

                    // ── OCR محلي (بدون AI) ──
                    const ocrResult = await _extractTextFromImage(buffer, mime);

                    if (ocrResult.hasText && ocrResult.formatted) {
                        // ── يوجد نص في الصورة ──
                        let reply_text = ocrResult.formatted;

                        // إذا كان المستخدم أرسل سؤالاً مع الصورة، أجب عليه باستخدام AI مع النص المستخرج
                        if (caption && caption.length > 2) {
                            reply_text += `\n\n💬 *سؤالك:* ${caption}\n`;
                            try {
                                const aiAnswer = await callMistral({
                                    model: 'mistral-small-latest',
                                    messages: [
                                        {
                                            role: 'system',
                                            content: 'أنت مساعد ذكي. تم استخراج النص التالي من صورة بواسطة OCR. أجب على سؤال المستخدم بناءً على هذا النص.'
                                        },
                                        {
                                            role: 'user',
                                            content: `النص المستخرج من الصورة:\n${ocrResult.text}\n\nالسؤال: ${caption}`
                                        }
                                    ],
                                    max_tokens: 800,
                                    temperature: 0.3
                                });
                                reply_text += `\n🤖 *الجواب:*\n${aiAnswer}`;
                            } catch (_) {
                                // إذا فشل AI، نكتفي بالنص المستخرج
                            }
                        }

                        // حفظ في السياق
                        if (!userChats[sender]) userChats[sender] = [];
                        userChats[sender].push({ role: 'user',      content: caption ? `[صورة + سؤال: ${caption}]` : '[صورة]' });
                        userChats[sender].push({ role: 'assistant', content: ocrResult.text });

                        // رصيد متبقي
                        if (!isVIPimg) {
                            const rec2 = getDailyRecord(sender);
                            const imgLeft2 = Math.max(0, DAILY_IMG_LIMIT - (rec2.images || 0));
                            const msgLeft2 = Math.max(0, getUserDailyLimit(sender) - (rec2.messages || 0));
                            const ttsLeft2 = Math.max(0, DAILY_TTS_LIMIT - (rec2.tts || 0));
                            reply_text += `\n\n─────────────\n_🖼️ صور: ${imgLeft2} | 💬 رسائل: ${msgLeft2} | 🔊 صوت: ${ttsLeft2}_`;
                        }

                        await reply(reply_text);
                        await react('✅');

                    } else {
                        // ── لا يوجد نص في الصورة ──
                        let noTextReply = `🖼️ *لم أجد نصاً قابلاً للقراءة في هذه الصورة.*\n\n`;
                        noTextReply   += `📌 *ملاحظات:*\n`;
                        noTextReply   += `• الصورة قد تكون رسماً أو صورة شخصية\n`;
                        noTextReply   += `• جرّب إرسال صورة أوضح أو بدقة أعلى\n`;
                        noTextReply   += `• تأكد أن النص في الصورة واضح وغير مائل كثيراً`;

                        if (!isVIPimg) {
                            const rec2 = getDailyRecord(sender);
                            const imgLeft2 = Math.max(0, DAILY_IMG_LIMIT - (rec2.images || 0));
                            noTextReply += `\n\n─────────────\n_🖼️ صور: ${imgLeft2} متبقية_`;
                        }
                        await reply(noTextReply);
                        await react('ℹ️');
                    }

                } catch (e) {
                    console.error('[image-ocr]', e.message);
                    await react('❌');
                    await reply('عذراً، حدث خطأ أثناء معالجة الصورة. يرجى المحاولة مرة أخرى.');
                }
                return;
            }

            // --- ملفات PDF/TXT ---
            if (msgType === 'documentMessage') {
                if (!checkSpam(sender)) {
                    await reply('⚠️ أرسلت ملفات بشكل متسارع، انتظر ثوانٍ ثم أعد المحاولة.');
                    return;
                }
                if (!checkDailyLimit(sender, 'pdf')) {
                    await reply('⚠️ وصلت للحد الشهري للملفات (10 ملفات/شهر).');
                    return;
                }
                await react('⏳');
                try {
                    const docMsg = message.documentMessage;
                    const mime = docMsg?.mimetype || '';
                    const fileName = docMsg?.fileName || 'ملف';
                    const caption = body || '';
                    const isPDF = mime === 'application/pdf' || fileName.toLowerCase().endsWith('.pdf');
                    const isTXT = mime.startsWith('text/') || fileName.toLowerCase().endsWith('.txt');

                    if (!isPDF && !isTXT) {
                        await react('ℹ️');
                        await reply(
                            `📎 "${fileName}"\n` +
                            `هذا النوع غير مدعوم حالياً.\n` +
                            `المدعوم الآن: PDF و TXT.`
                        );
                        return;
                    }

                    const fileSize = docMsg?.fileLength || 0;
                    const isVIPfile = isAdmin || isActiveVIP(sender);
                    const maxSize = isVIPfile ? 20 * 1024 * 1024 : 1 * 1024 * 1024;
                    if (fileSize > maxSize) {
                        await react('❌');
                        await reply(
                            `⚠️ حجم الملف كبير جداً (${(fileSize/1024/1024).toFixed(1)}MB).\n` +
                            (isVIPfile ? `الحد الأقصى المسموح 20MB.` : `الحد الأقصى للنسخة المجانية 1MB فقط.`)
                        );
                        return;
                    }

                    const buffer = await downloadMediaMessage(msg, 'buffer', {}, {
                        logger: { level: 'silent', child: () => ({ level: 'silent' }) }
                    });
                    if (!buffer || buffer.length === 0) {
                        await react('❌');
                        await reply('لم أتمكن من تنزيل الملف، يرجى المحاولة مرة أخرى.');
                        return;
                    }

                    let docText = '';
                    let pageCount = 0;
                    if (isTXT) {
                        docText = buffer.toString('utf-8').replace(/^\uFEFF/, '').trim();
                        pageCount = 1;
                        if (!docText) {
                            await react('❌');
                            await reply('ملف TXT فارغ أو غير قابل للقراءة.');
                            return;
                        }
                    } else {
                        if (buffer.length < 4 || buffer.slice(0,4).toString('ascii') !== '%PDF') {
                            await react('❌');
                            await reply('الملف ليس PDF حقيقياً، يرجى إرسال ملف PDF صحيح.');
                            return;
                        }
                        const cacheKey = pdfCacheKey(fileName, buffer);
                        const cacheHit = pdfCacheGet(cacheKey);
                        if (cacheHit) {
                            docText = cacheHit.docText;
                            pageCount = cacheHit.pageCount || 0;
                        } else {
                            const result = await _processPDF(buffer, fileName, sender, sock, jid, msg);
                            docText = result.docText;
                            pageCount = result.pageCount || 0;
                            if (docText && docText.trim()) pdfCacheSet(cacheKey, fileName, docText, pageCount);
                            try { if (result.outputPath) fs.unlink(result.outputPath, () => {}); } catch (_) {}
                        }
                    }

                    stats.totalDocs = (stats.totalDocs || 0) + 1;
                    saveData();
                    userPdfContext[sender] = { fileName, docText, pages: null, loadedAt: Date.now(), type: isTXT ? 'txt' : 'pdf' };
                    delete userPdfPending[sender];
                    delete userExamPending[sender];
                    delete userExamMode[sender];

                    let answer = '';
                    if (caption && caption.trim().length > 2) {
                        try {
                            answer = await askAI([
                                { role: 'system', content: `أنت مساعد ذكي. استخدم محتوى الملف "${fileName}" عند الحاجة، لكن لا تحصر إجابتك فيه فقط. إذا طلب المستخدم شرحاً أو حلاً من الملف فاستخرج المطلوب من الملف واشرحه بوضوح.` },
                                { role: 'user', content: `محتوى الملف:\n${docText.slice(0, 18000)}\n\nطلب المستخدم:\n${caption}` }
                            ]);
                        } catch (e) {
                            console.error('[file-caption-answer]', e.message);
                            notifyAdmin(`⚠️ فشل جواب من ملف ${fileName}: ${e.message}`).catch(()=>{});
                        }
                    }

                    await react('📄');
                    await reply(
                        `✅ تم قراءة الملف: *${fileName}*\n` +
                        (pageCount ? `📊 الصفحات/الوحدات: ${pageCount}\n` : '') +
                        `📌 يمكنك الآن أن تسألني أي سؤال عنه، أو تطلب شرح/تلخيص/حل من الملف.\n` +
                        `لن أقيّدك بوضع "من الملف فقط"؛ سأستخدم الملف كسياق عندما يكون سؤالك متعلقاً به.` +
                        (answer ? `\n\n─────────────\n${answer}` : '')
                    );
                    await react('✅');
                } catch (e) {
                    console.error('[document]', e.message);
                    notifyAdmin(`⚠️ خطأ في معالجة ملف للمستخدم ${sender}: ${e.message}`).catch(()=>{});
                    await reply('حدث خطأ أثناء قراءة الملف، يرجى المحاولة مرة أخرى.');
                    await react('❌');
                }
                return;
            }

            // --- فيديو ---
            if (msgType === 'videoMessage') {
                await react('ℹ️');
                await reply(
                    `عذراً، الفيديوهات غير مدعومة حالياً.\n` +
                    `يمكنك أخذ لقطة شاشة وإرسالها كصورة إذا أردت تحليل محتوى معين. 📸`
                );
                return;
            }

            // --- صوت (Voxtral by Mistral) ---
            if (msgType === 'audioMessage' || msgType === 'pttMessage') {
                if (!checkSpam(sender)) {
                    await reply('⚠️ أرسلت رسائل بشكل متسارع، انتظر ثوانٍ ثم أعد المحاولة.');
                    return;
                }
                await react('🎙️');
                try {
                    const audioMsg = message.audioMessage || message.pttMessage || {};
                    const mime = audioMsg?.mimetype || 'audio/ogg; codecs=opus';
                    const buffer = await downloadMediaMessage(msg, 'buffer', {}, {
                        logger: { level: 'silent', child: () => ({ level: 'silent' }) }
                    });
                    if (!buffer || buffer.length === 0) {
                        await react('❌');
                        await reply('لم أتمكن من تنزيل الرسالة الصوتية، حاول مرة أخرى.');
                        return;
                    }

                    // فحص الحد الشهري للصوت (حد TTS منفصل)
                    const isVIPaudio = isAdmin || isActiveVIP(sender);
                    // ttsQuota مُعرَّف هنا (خارج if) حتى يكون متاحاً عند commit() لاحقاً
                    let ttsQuota = null;
                    if (!isAdmin && !isVIPaudio) {
                        ttsQuota = checkDailyTTS(sender);
                        if (!ttsQuota.allowed) {
                            await react('⛔');
                            await reply(`⚠️ *وصلت للحد الشهري للرسائل الصوتية* (${DAILY_TTS_LIMIT} مرات)\n\nللاشتراك المميز (غير محدود):\n👤 wa.me/972593850520`);
                            return;
                        }
                    }

                    // Voxtral يفهم الصوت ويرد مباشرة — بدون OpenAI
                    if (!userChats[sender]) userChats[sender] = [];

                    let res;
                    let usedLocalSTT = false;

                    try {
                        res = await transcribeAndReplyAudio(
                            buffer, mime, body, userName, userChats[sender]
                        );
                    } catch (voxtralErr) {
                        // ── Fallback: STT محلي (faster-whisper) ──
                        console.warn(`[audio] Voxtral فشل (${voxtralErr.message}) — جاري المحاولة بـ STT المحلي...`);
                        const sttLang   = 'auto';
                        const sttResult = await transcribeLocal(buffer, mime, sttLang);
                        if (sttResult && sttResult.text) {
                            usedLocalSTT = true;
                            // نُرسل النص المستخرج إلى نموذج الدردشة العادي
                            const sttMessages = [
                                { role: 'system', content: getSystemPrompt() },
                                ...(userChats[sender] || []).slice(-6),
                                { role: 'user', content: sttResult.text }
                            ];
                            res = await askAI(sttMessages).catch(
                                () => `📝 *نص رسالتك الصوتية:*\n${sttResult.text}\n\n_(تعذّر الرد التلقائي — يمكنك إرسال السؤال نصياً)_`
                            );
                        } else {
                            throw voxtralErr; // أعد الخطأ الأصلي
                        }
                    }

                    // حفظ في السياق
                    userChats[sender].push({ role: 'user',      content: body ? `[رسالة صوتية + نص: ${body}]` : '[رسالة صوتية]' });
                    userChats[sender].push({ role: 'assistant', content: res });

                    stats.totalMessages++;
                    saveData();

                    // إضافة رصيد متبقي للمستخدم العادي
                    let audioFinalRes = `🎙️ ${res}`;
                    if (!isVIPaudio) {
                        const rec3 = getDailyRecord(sender);
                        const ttsLeft3 = Math.max(0, DAILY_TTS_LIMIT - (rec3.tts || 0));
                        const imgLeft3 = Math.max(0, DAILY_IMG_LIMIT - (rec3.images || 0));
                        const msgLeft3 = Math.max(0, getUserDailyLimit(sender) - (rec3.messages || 0));
                        audioFinalRes += `\n\n─────────────\n_🔊 صوت: ${ttsLeft3} | 💬 رسائل: ${msgLeft3} | 🖼️ صور: ${imgLeft3}_`;
                    }
                    await reply(audioFinalRes);
                    if (ttsQuota) ttsQuota.commit?.(); // خصم بعد نجاح الرد فعلياً
                    await react('✅');

                } catch (audioErr) {
                    console.error('[audio/voxtral]', audioErr.message);
                    if (audioErr.message === 'AUTH_ERROR') {
                        notifyAdmin('⚠️ خطأ 401 في Voxtral — تحقق من MISTRAL_API_KEY').catch(()=>{});
                        await react('❌');
                        await reply('عذراً، مشكلة في إعدادات الخدمة. تم إشعار الأدمن.');
                    } else if (audioErr.message === 'QUOTA_ERROR') {
                        await react('❌');
                        await reply('عذراً، نفاد رصيد Mistral API مؤقتاً. تم إشعار الأدمن.');
                    } else {
                        await react('❌');
                        await reply('عذراً، حدث خطأ في معالجة الرسالة الصوتية. حاول مجدداً أو اكتب رسالتك نصياً. ✍️');
                    }
                }
                return;
            }

            // --- نص ---
            if (!body) return;

            // ============================================================
            // كشف "نعم" لإرسال النطق الصوتي (TTS)
            // إذا كان المستخدم في انتظار نطق مصطلح أو دواء وأرسل "نعم"
            // ============================================================
            const bodyTrimmed = body.trim();

            // ============================================================
            // وضع PDF — إذا كان المستخدم في انتظار إذن PDF
            // ============================================================
            // تنظيف userPdfPending المنتهي الصلاحية أولاً
            if (userPdfPending[sender] && Date.now() >= (userPdfPending[sender].expiresAt || 0)) {
                delete userPdfPending[sender];
            }
            if (userPdfPending[sender]) {
                const pdfEntry = userPdfPending[sender];
                const isYesPdf = /^(نعم|yes|أيوه|اه|ايوه|yep|yeah)$/i.test(bodyTrimmed);
                const isNoPdf  = /^(لا|no|لأ)$/i.test(bodyTrimmed);
                if (isYesPdf) {
                    // تحقق أن البيانات المطلوبة موجودة فعلاً
                    if (!pdfEntry.docText && !(pdfEntry.pages && pdfEntry.pages.length > 0)) {
                        delete userPdfPending[sender];
                        await react('❌');
                        await reply('انتهت صلاحية الملف، يرجى إعادة إرسال ملف PDF.');
                        return;
                    }
                    userPdfContext[sender] = {
                        fileName: pdfEntry.fileName,
                        docText:  pdfEntry.docText,
                        pages:    pdfEntry.pages || null,
                        loadedAt: Date.now()
                    };
                    delete userPdfPending[sender];
                    await react('📑');
                    await reply(
                        `📑 *تم تفعيل وضع الملف*\n"${userPdfContext[sender].fileName}"\n\n` +
                        `سأستخدم الملف كسياق عندما تسأل عنه، ويمكنك المتابعة بشكل عادي.\n` +
                        `اكتب *خروج* للخروج من وضع الملف\n` +
                        `اكتب *قائمة* للرجوع للقائمة الرئيسية`
                    );
                    return;
                }
                if (isNoPdf) {
                    delete userPdfPending[sender];
                    await react('✅');
                    await reply('حسناً، سأتابع معك بشكل عادي. 👍');
                    return;
                }
            }

            // ============================================================
            // وضع PDF النشط — الإجابة من الملف فقط
            // ============================================================
            if (userPdfContext[sender]) {
                const { fileName, docText, pages } = userPdfContext[sender];

                // خروج من وضع الملف
                if (/^خروج$/i.test(bodyTrimmed)) {
                    delete userPdfContext[sender];
                    await react('✅');
                    await reply('تم الخروج من وضع الملف. يمكنك الآن إرسال أي سؤال بشكل عادي. 👋');
                    return;
                }

                // رجوع للقائمة الرئيسية
                if (/^قائمة$/i.test(bodyTrimmed)) {
                    delete userPdfContext[sender];
                    delete userModes[sender];
                    userChats[sender] = [];
                    await react('🔄');
                    await reply(buildModeMenu(userName || ''));
                    return;
                }

                // anti-spam
                if (!checkSpam(sender)) {
                    await reply('⚠️ أرسلت رسائل بشكل متسارع، انتظر ثوانٍ ثم أعد المحاولة.');
                    return;
                }

                await react('⏳');
                try {
                    const pdfSystemPrompt =
                        `أنت مساعد ذكي يساعد المستخدم على فهم محتوى الملف: "${fileName}".\n` +
                        `أسلوبك طبيعي ومرن — اشرح وحلّل وأجب كما يفهم الإنسان، لا تقتبس حرفياً.\n` +
                        `اللغة: أجب بنفس لغة سؤال المستخدم (عربي أو إنجليزي).\n` +
                        `السياق: استخدم محتوى الملف عندما يكون سؤال المستخدم متعلقاً به، ويمكنك أيضاً الشرح والتوضيح بمعلومات عامة عند الحاجة.\n` +
                        `لا تقل إنك مقيّد بالملف فقط إلا إذا طلب المستخدم ذلك صراحة.`;

                    let res;
                    if (pages && pages.length > 0) {
                        // استخدام الصور للإجابة (يقرأ النص والصور معاً)
                        const imageContents = pages.map(b64 => ({
                            type: 'image_url',
                            image_url: { url: `data:image/jpeg;base64,${b64}` }
                        }));
                        res = await callMistral({
                            model: 'pixtral-large-latest',
                            messages: [
                                { role: 'system', content: pdfSystemPrompt },
                                { role: 'user', content: [...imageContents, { type: 'text', text: body }] }
                            ],
                            max_tokens: 2500,
                            temperature: 0.3
                        });
                    } else {
                        // fallback: نص فقط
                        res = await askAI([
                            { role: 'system', content: pdfSystemPrompt },
                            { role: 'user',   content: `محتوى الملف:\n${docText.slice(0, 14000)}\n\nسؤال المستخدم: ${body}` }
                        ]);
                    }
                    await reply(res);
                    await react('✅');
                } catch (e) {
                    console.error('[PDF mode]', e.message);
                    await react('❌');
                    await reply('حدث خطأ، يرجى المحاولة مرة أخرى.');
                }
                return;
            }


            // تم إلغاء نظام الاختبارات ووضع الانتظار حسب الطلب.
            // عند إرسال PDF/TXT يتم حفظ محتواه كسياق عادي، ويمكن للمستخدم السؤال عنه مباشرة.

            const isYes = /^(نعم|yes|نعم\s*✅|أيوه|اه|ايوه|yep|yeah)$/i.test(bodyTrimmed);
            if (isYes && userTTSPending[sender] && Date.now() < userTTSPending[sender].expiresAt) {
                const { term, termAr } = userTTSPending[sender];
                delete userTTSPending[sender];
                await react('🔊');
                try {
                    let sentAny = false;
                    // إرسال النطق الإنجليزي
                    if (term) {
                        const audioEn = await generateTTS(term, 'en');
                        if (audioEn) { await sendVoiceNote(jid, audioEn, msg); sentAny = true; }
                    }
                    // إرسال النطق العربي إذا وُجد
                    if (termAr) {
                        await new Promise(r => setTimeout(r, 600));
                        const audioAr = await generateTTS(termAr, 'ar');
                        if (audioAr) { await sendVoiceNote(jid, audioAr); sentAny = true; }
                    }
                    if (sentAny) {
                        await react('✅');
                    } else {
                        // ffmpeg غير متاح — fallback نصي
                        await reply(`🔤 النطق: *${term || ''}*${termAr ? `\nبالعربية: *${termAr}*` : ''}`);
                        await react('✅');
                    }
                } catch (ttsErr) {
                    console.error('[TTS] ❌ خطأ كامل:', ttsErr);
                    await react('❌');
                    await reply('عذراً، لم أتمكن من توليد الصوت حالياً. حاول مرة أخرى لاحقاً. 🔇');
                }
                return;
            }
            // إذا أرسل "لا" أو أي شيء آخر بعد عرض النطق → نمسح الانتظار ونكمل طبيعي
            if (userTTSPending[sender]) delete userTTSPending[sender];

            // أوامر المستخدم (!مساعدة، !مسح، !رصيد، !لغة، !ملخص) — تعمل دائماً
            const isVIPcmd = isActiveVIP(sender);
            const handledCmd = await handleUserCommand(body, sender, reply, react, isAdmin, isVIPcmd);
            if (handledCmd) return;

            // ============================================================
            // إنشاء ملفات PDF/TXT محلياً حسب طلب المستخدم
            // ============================================================
            if (looksLikePDFCreateRequest(bodyTrimmed) || looksLikeTXTCreateRequest(bodyTrimmed)) {
                const wantPdf = looksLikePDFCreateRequest(bodyTrimmed);
                await react('📝');
                let generatedText = '';
                try {
                    generatedText = await callMistral({
                        model: 'mistral-small-latest',
                        messages: [
                            { role: 'system', content: 'اكتب محتوى عربي/إنجليزي مرتب ومنسق للملف المطلوب. استخدم عناوين ونقاط واضحة. لا تذكر أنك ذكاء اصطناعي.' },
                            { role: 'user', content: bodyTrimmed }
                        ],
                        max_tokens: 1800,
                        temperature: 0.35
                    }, 2);
                } catch (e) {
                    console.error('[create-file AI]', e.message);
                    notifyAdmin(`⚠️ فشل توليد محتوى ملف للمستخدم ${sender}: ${e.message}`).catch(()=>{});
                    generatedText = `طلب المستخدم:
${bodyTrimmed}

تعذر توليد شرح تلقائي بسبب خطأ مؤقت في خدمة الذكاء الاصطناعي. يمكنك إعادة المحاولة لاحقاً.`;
                }
                let out = null;
                try {
                    const title = makeShortTitle(bodyTrimmed, wantPdf ? 'MedTerm_PDF' : 'MedTerm_TXT');
                    out = wantPdf ? await _createPDF(title, generatedText) : await _createTXT(title, generatedText);
                    await sendLocalDocument(
                        jid,
                        out.path,
                        out.fileName,
                        wantPdf ? 'application/pdf' : 'text/plain; charset=utf-8',
                        msg
                    );
                    await react('✅');
                } catch (e) {
                    console.error('[create-file]', e.message);
                    notifyAdmin(`⚠️ فشل إنشاء ملف ${wantPdf ? 'PDF' : 'TXT'} محلياً: ${e.message}`).catch(()=>{});
                    await react('❌');
                    await reply(`تعذر إنشاء الملف محلياً حالياً. السبب: ${e.message.slice(0, 180)}`);
                } finally {
                    if (out && out.path) cleanupLocalFile(out.path);
                }
                return;
            }

            // ============================================================
            // توليد صورة محلية بسيطة بدون API خارجي
            // ============================================================
            if (looksLikeImageGenerateRequest(bodyTrimmed)) {
                await react('🎨');
                let out = null;
                try {
                    out = await _generateLocalImage(bodyTrimmed);
                    await sendLocalImage(jid, out.path, '🎨 تم إنشاء الصورة محلياً بدون استخدام API خارجي.', msg);
                    await react('✅');
                } catch (e) {
                    console.error('[local-image-generate]', e.message);
                    notifyAdmin(`⚠️ فشل توليد صورة محلية: ${e.message}`).catch(()=>{});
                    await react('❌');
                    await reply(`تعذر إنشاء الصورة محلياً حالياً. السبب: ${e.message.slice(0, 180)}`);
                } finally {
                    if (out && out.path) cleanupLocalFile(out.path);
                }
                return;
            }

            // ============================================================
            // كشف طلب النطق الصوتي: "نطق [كلمة/جملة]"
            // ============================================================
            const ttsMatch = bodyTrimmed.match(/^(?:نطق|صوت|اسمعني|اقرأ|نطقها?|ارسل صوت|حولها? صوت|بصوت|اقرأها?|قرأ|قراءة|حول(?:ي|وا|)? (?:لـ?|إلى )?صوت)\s+(.+)$/i)
                          || bodyTrimmed.match(/^(?:.*?)\s*(?:بالصوت|صوتياً|بصوت|voice)\s+(.+)$/i)
                          || (bodyTrimmed.startsWith('صوت ') ? { 1: bodyTrimmed.slice(4) } : null);
            if (ttsMatch) {
                const isVIPnow = isActiveVIP(sender);
                let ttsCheck = null;
                if (!isAdmin && !isVIPnow) {
                    ttsCheck = checkDailyTTS(sender);
                    if (!ttsCheck.allowed) {
                        await reply(
                            `⚠️ *وصلت للحد الشهري للصوت* (${DAILY_TTS_LIMIT} مرات)\n\n` +
                            `للاشتراك المميز (غير محدود):\n👤 wa.me/972593850520`
                        );
                        return;
                    }
                }
                const ttsText = ttsMatch[1].trim();
                await react('🔊');
                try {
                    const lang  = /[\u0600-\u06FF]/.test(ttsText) ? 'ar' : 'en';
                    const audio = await generateTTS(ttsText, lang);
                    if (audio) {
                        await sendVoiceNote(jid, audio, msg);
                        if (!isAdmin && !isVIPnow) ttsCheck.commit?.();
                        await react('✅');
                    } else {
                        // ffmpeg غير متاح — fallback نصي
                        await reply(`🔤 النطق: *${ttsText}*\n_(تعذّر إرسال الصوت — ffmpeg غير متاح)_`);
                        await react('✅');
                    }
                } catch (e) {
                    await react('❌');
                    await reply('عذراً، لم أتمكن من توليد الصوت حالياً. حاول مرة أخرى لاحقاً. 🔇');
                }
                return;
            }

            // ============================================================
            // كشف طلب الترجمة: "ترجم [نص]" — يرسل الترجمة + صوت تلقائياً
            // ============================================================
            const translateMatch = bodyTrimmed.match(/^(?:ترجم|translate|ترجمة)\s+(.+)$/i);
            if (translateMatch) {
                const isVIPnow = isActiveVIP(sender);
                const textToTranslate = translateMatch[1].trim();
                await react('⏳');
                try {
                    // تحديد لغة المصدر والهدف
                    const isArabic = /[\u0600-\u06FF]/.test(textToTranslate);
                    const targetLang = isArabic ? 'English' : 'العربية';
                    const targetLangCode = isArabic ? 'en' : 'ar';

                    const translationResult = await callMistral({
                        model: 'mistral-small-latest',
                        messages: [
                            { role: 'system', content: `أنت مترجم محترف. ترجم النص التالي إلى ${targetLang}. أرسل الترجمة فقط بدون أي شرح أو تعليق.` },
                            { role: 'user', content: textToTranslate }
                        ],
                        max_tokens: 500,
                        temperature: 0.3
                    });

                    const originalLabel = isArabic ? '🇸🇦 الأصلي:' : '🔤 Original:';
                    const translatedLabel = isArabic ? '🇬🇧 الترجمة:' : '🇸🇦 الترجمة:';
                    await reply(`${originalLabel} ${textToTranslate}\n\n${translatedLabel} ${translationResult}`);

                    // إرسال الصوت تلقائياً بدون طلب "نعم"
                    let ttsCheck = null;
                    if (!isAdmin && !isVIPnow) {
                        ttsCheck = checkDailyTTS(sender);
                        if (!ttsCheck.allowed) {
                            await reply(`⚠️ وصلت للحد الشهري للصوت (${DAILY_TTS_LIMIT} مرات). الترجمة فوق بدون صوت.`);
                            await react('✅');
                            return;
                        }
                    }
                    await new Promise(r => setTimeout(r, 300));
                    const audio = await generateTTS(translationResult, targetLangCode);
                    if (audio) {
                        await sendVoiceNote(jid, audio);
                        if (!isAdmin && !isVIPnow) ttsCheck.commit?.();
                    }
                    // إذا كان null (ffmpeg غير متاح) — الترجمة النصية أُرسلت فعلاً فوق
                    await react('✅');
                } catch (e) {
                    console.error('[translate]', e.message);
                    await react('❌');
                    await reply('عذراً، حدث خطأ أثناء الترجمة. حاول مرة أخرى.');
                }
                return;
            }

            // anti-spam: منع الإرسال المتسارع جداً
            if (!checkSpam(sender)) {
                await reply('⚠️ أرسلت رسائل بشكل متسارع، انتظر ثوانٍ ثم أعد المحاولة.');
                return;
            }

            // الحد الشهري للرسائل (الأدمن وVIP بلا حدود)
            const isVIP = isActiveVIP(sender);
            let _quotaCommit = null;
            if (!isAdmin && !isVIP) {
                const quota = checkDailyMessages(sender);
                if (!quota.allowed) {
                    await reply(
                        `📢 *إشعار مهم لمستخدمي بوت MedTerm*\n\n` +
                        `نشكركم على استخدام النسخة التجريبية من البوت. نود إعلامكم بأن الباقة المجانية الشهرية قد انتهت، وللاستمرار بالاستفادة من جميع خدمات البوت أصبح الاشتراك الشهري متاحاً الآن.\n\n` +
                        `💰 *سعر الاشتراك: 5 شيكل فقط شهرياً*\n\n` +
                        `عند الاشتراك ستحصل على وصول غير محدود إلى جميع ميزات البوت، ومنها:\n\n` +
                        `🤖 الذكاء الاصطناعي على واتساب على مدار 24 ساعة\n` +
                        `💬 دعم غير محدود للرسائل النصية\n` +
                        `🔬 تحليل وشرح الصور الطبية وغير الطبية\n` +
                        `🎙️ دعم الرسائل الصوتية والرد عليها\n` +
                        `📄 إمكانية إرسال الملفات وتحليل محتواها\n` +
                        `🏥 شرح المصطلحات الطبية بشكل مفصل ومبسط\n` +
                        `💊 معلومات علم الأدوية واستخداماتها التعليمية\n` +
                        `👨‍⚕️ المساعدة في مختلف التخصصات الطبية\n` +
                        `❓ الإجابة عن الأسئلة العامة وغير الطبية\n` +
                        `🔊 النطق الصحيح للمصطلحات الطبية وأسماء الأدوية\n` +
                        `♾️ استخدام غير محدود لجميع الميزات دون قيود\n\n` +
                        `*كل هذه الخدمات مقابل 5 شيكل فقط شهرياً.*\n\n` +
                        `للاشتراك أو الاستفسار، يرجى التواصل معنا وسيتم تفعيل اشتراكك مباشرة:\n` +
                        `📲 https://wa.me/972593850520\n\n` +
                        `شكراً لثقتكم بنا، ونتمنى لكم تجربة مميزة مع *MedTerm* 💙`
                    );
                    const uName = userNames[sender] ? `${userNames[sender]} (${sender})` : sender;
                    notifyAdmin(`⚠️ المستخدم ${uName} انتهت فترته التجريبية (${DAILY_MSG_LIMIT} رسالة).`).catch(()=>{});
                    return;
                }
                _quotaCommit = quota.commit;
            }

            await react('👍');

            const maxHist = isVIP ? 60 : MAX_HISTORY;

            if (!userChats[sender]) userChats[sender] = [];

            // سياق أولي إذا كانت الجلسة فارغة
            if (userChats[sender].length === 0 && userName) {
                userChats[sender].push({ role: 'user',      content: `[اسم المستخدم: ${userName}]` });
                userChats[sender].push({ role: 'assistant', content: `أهلاً ${userName}، كيف أستطيع مساعدتك؟` });
            }

            stats.totalMessages++;
            saveData();

            // تقليم السياق قبل الإضافة
            if (userChats[sender].length >= maxHist)
                userChats[sender] = userChats[sender].slice(-(maxHist - 1));

            userChats[sender].push({ role: 'user', content: body });

            // اختيار system prompt ذكي: طبي للأسئلة الطبية، عام للباقي
            const smartPrompt = getSmartSystemPrompt(body, userLanguages[sender]);

            const res = await askAI([
                { role: 'system', content: smartPrompt },
                ...userChats[sender]
            ]);

            userChats[sender].push({ role: 'assistant', content: res });

            // إضافة رصيد المتبقي في نهاية الرد (للمستخدم العادي فقط)
            let finalRes = res;
            // خصم الرسالة مرة واحدة فقط بعد نجاح الرد
            if (_quotaCommit) {
                _quotaCommit();
                _quotaCommit = null; // منع أي استدعاء مزدوج
            }
            // إضافة رصيد المتبقي في نهاية الرد (للمستخدم العادي فقط)
            if (!isAdmin && !isVIP) {
                const rec = getDailyRecord(sender);
                const msgLimit = getUserDailyLimit(sender);
                const msgLeft  = Math.max(0, msgLimit - rec.messages);
                const imgLeft  = Math.max(0, DAILY_IMG_LIMIT - (rec.images || 0));
                const ttsLeft  = Math.max(0, DAILY_TTS_LIMIT - (rec.tts || 0));
                if (msgLeft <= 5) {
                    finalRes += `\n\n─────────────\n` +
                        `⚠️ *تنبيه — رصيدك المتبقي هذا الشهر:*\n` +
                        `💬 رسائل: *${msgLeft}* متبقية\n` +
                        `🖼️ صور: *${imgLeft}* متبقية\n` +
                        `🔊 صوت/ترجمة: *${ttsLeft}* متبقية\n` +
                        `_للاشتراك المميز (غير محدود): wa.me/972593850520_`;
                } else {
                    finalRes += `\n\n─────────────\n` +
                        `_💬 رسائل: ${msgLeft} | 🖼️ صور: ${imgLeft} | 🔊 صوت: ${ttsLeft}_`;
                }
            }

            await reply(finalRes);
            await react('✅');

            // ── عرض النطق الصوتي للمصطلحات الطبية والأدوية تلقائياً ──
            if (isMedicalQuery(body)) {
                const termEn = extractTermForTTS(res);
                let termAr = null;
                const arMatch = res.match(/(?:العربية|بالعربية)\s*[:\-–]\s*([^\n]{2,40})/i);
                if (arMatch) termAr = arMatch[1].replace(/[*_📌⭐]/g, '').trim().split(/[،,\n]/)[0].trim();

                if (termEn) {
                    userTTSPending[sender] = {
                        term:   termEn,
                        termAr: termAr || null,
                        expiresAt: Date.now() + 3 * 60_000
                    };
                    await new Promise(r => setTimeout(r, 400));
                    await reply(`🔊 *هل تريد سماع النطق الصحيح؟*\nأرسل *نعم* وسأرسل لك الصوت فوراً 🎙️`);
                }
            }


        } catch (error) {
            console.error('[messages.upsert]', error.message);
            try {
                await sock.sendMessage(
                    msg.key.remoteJid,
                    { text: 'حدث خطأ تقني، يرجى المحاولة مرة أخرى.' },
                    { quoted: msg }
                );
            } catch {}
        }
        } // نهاية for loop (معالجة الرسائل الفائتة)
    });
}

// ============================================================
// START
// ============================================================
// تنظيف الذاكرة كل ساعة
setInterval(cleanMemory, 60 * 60_000);

// فحص انتهاء VIP كل ساعة
setInterval(checkVIPExpiry, 60 * 60_000);






// ============================================================
// فحص التبعيات عند البدء
// ============================================================
(function checkDependencies() {
    const { spawnSync } = require('child_process');

    // دالة مرنة: تبحث عن الأداة في كل المسارات المحتملة
    function findTool(name) {
        try {
            const r = spawnSync('which', [name], { encoding: 'utf8', timeout: 3000 });
            if (!r.error && r.status === 0 && r.stdout.trim()) return r.stdout.trim();
        } catch (_) {}

        const termuxPaths = [
            `/data/data/com.termux/files/usr/bin/${name}`,
            `/data/data/com.termux/files/usr/local/bin/${name}`,
            `/usr/bin/${name}`,
            `/usr/local/bin/${name}`,
            `/bin/${name}`,
        ];
        for (const p of termuxPaths) {
            try {
                if (fs.existsSync(p)) return p;
            } catch (_) {}
        }

        try {
            const r = spawnSync(name, ['--version'], { encoding: 'utf8', timeout: 2000 });
            if (!r.error && (r.status === 0 || (r.stderr && r.stderr.length > 0))) return name;
        } catch (_) {}

        return null;
    }

    // فحص ffmpeg (إلزامي — للصوت)
    const ffmpegPath = findTool('ffmpeg');
    if (!ffmpegPath) {
        console.error('❌ ffmpeg غير مثبت أو غير موجود في PATH.');
        console.error('   ثبّته بـ:  pkg install ffmpeg');
        process.exit(1);
    }
    console.log(`✅ ffmpeg: ${ffmpegPath}`);

    // فحص python3 (إلزامي — لمعالجة PDF)
    const pythonPath = findTool('python3') || findTool('python');
    if (!pythonPath) {
        console.error('❌ python3 غير مثبت!');
        console.error('   ثبّته بـ:  pkg install python');
        process.exit(1);
    }
    console.log(`✅ python3: ${pythonPath}`);

    // فحص mutool (اختياري الآن — PDF يعتمد على Python Worker)
    const mutoolPath = findTool('mutool');
    if (mutoolPath) {
        console.log(`✅ mutool: ${mutoolPath} (متاح)`);
    } else {
        console.log(`ℹ️  mutool: غير موجود — لا بأس، PDF يعمل عبر Python Worker`);
    }

    // فحص مكتبات Python لـ PDF
    const pyCheck = spawnSync(pythonPath, ['-c',
        'import sys; missing=[]; ' +
        '[(lambda m: None if __import__(m, fromlist=["x"]) else missing.append(m))(m) for m in ["fitz","pdfplumber"]] if False else [m for m in ["fitz","pdfplumber"] if not __import__("importlib.util","importlib","util").util.find_spec(m)] ; ' +
        'import importlib.util; missing=[m for m in ["fitz","pdfplumber"] if not importlib.util.find_spec(m)]; ' +
        'print("OK" if not missing else "MISSING:"+",".join(missing))'
    ], { encoding: 'utf8', timeout: 5000 });

    if (!pyCheck.error) {
        const out = (pyCheck.stdout || '').trim();
        if (out.startsWith('MISSING:')) {
            const missing = out.replace('MISSING:', '');
            console.warn(`⚠️  مكتبات Python ناقصة: ${missing}`);
            console.warn('   ثبّتها بـ: pip install pymupdf pdfplumber');
        } else {
            console.log('✅ مكتبات Python (pymupdf + pdfplumber): موجودة');
        }
    }

    console.log('✅ جميع التبعيات الأساسية موجودة وجاهزة.');
})();

cleanPdfCache(); // تنظيف كاش PDF القديمة عند البدء

// فحص مكتبات Python Worker وعرض التقرير عند البدء
_checkPDFDeps().then(deps => {
    if (deps.python === false) {
        console.error('❌ Python غير موجود! ثبّته بـ: pkg install python');
        return;
    }
    const ok  = Object.entries(deps).filter(([k, v]) => k !== 'python' && v).map(([k]) => k);
    const bad = Object.entries(deps).filter(([k, v]) => k !== 'python' && !v).map(([k]) => k);
    if (ok.length)  console.log(`✅ Python PDF libs: ${ok.join(', ')}`);
    if (bad.length) console.warn(`⚠️  Python PDF libs ناقصة: ${bad.join(', ')} — شغّل install_pdf.sh`);
    // فحص وجود pdf_worker.py
    const workerPath = path.join(__dirname, 'pdf_worker.py');
    if (!require('fs').existsSync(workerPath)) {
        console.error(`❌ pdf_worker.py غير موجود في: ${workerPath}`);
        console.error('   ضع الملف في نفس مجلد bot.js');
    } else {
        console.log('✅ pdf_worker.py: موجود');
    }
}).catch(e => console.warn('[PDF deps check]', e.message));

// ── فحص مكتبات TTS و STT المحلية ──
(async () => {
    const script = `
import sys, json
r = {}
for mod in ['edge_tts', 'pyttsx3', 'faster_whisper', 'whisper', 'vosk']:
    try: __import__(mod); r[mod] = True
    except ImportError: r[mod] = False
print(json.dumps(r))
`;
    try {
        const { spawnSync } = require('child_process');
        const out = spawnSync(PYTHON_BIN, ['-c', script], { encoding: 'utf-8', timeout: 10_000 });
        const deps = JSON.parse(out.stdout.trim() || '{}');

        const ttsOk  = deps['edge_tts'] || deps['pyttsx3'];
        const sttOk  = deps['faster_whisper'] || deps['whisper'] || deps['vosk'];

        console.log(`${ttsOk ? '✅' : '⚠️ '} TTS محلي: ${deps['edge_tts'] ? 'edge-tts ✅' : ''}${deps['pyttsx3'] ? ' pyttsx3 ✅' : ''}${!ttsOk ? 'غير مثبَّت — شغّل install_local_ai.sh' : ''}`);
        console.log(`${sttOk ? '✅' : '⚠️ '} STT محلي: ${deps['faster_whisper'] ? 'faster-whisper ✅' : ''}${deps['whisper'] ? ' openai-whisper ✅' : ''}${deps['vosk'] ? ' vosk ✅' : ''}${!sttOk ? 'غير مثبَّت — شغّل install_local_ai.sh' : ''}`);

        if (!ttsOk) console.warn('   ⚠️  TTS سيعمل بـ Google Translate (غير موثوق). ثبّت: pip install edge-tts');
        if (!sttOk) console.warn('   ⚠️  STT سيعمل بـ Voxtral API فقط. ثبّت: pip install faster-whisper');

        // فحص وجود الـ workers
        const ttsW = path.join(__dirname, 'tts_worker.py');
        const sttW = path.join(__dirname, 'stt_worker.py');
        if (!require('fs').existsSync(ttsW)) console.error('❌ tts_worker.py مفقود!');
        else console.log('✅ tts_worker.py: موجود');
        if (!require('fs').existsSync(sttW)) console.error('❌ stt_worker.py مفقود!');
        else console.log('✅ stt_worker.py: موجود');

    } catch (e) {
        console.warn('[TTS/STT deps check]', e.message);
    }
})();


function ensurePythonRuntimeDeps() {
    if (process.env.AUTO_INSTALL_DEPS === '0') {
        console.log('ℹ️ AUTO_INSTALL_DEPS=0 — تخطّي التثبيت التلقائي للمكتبات.');
        return;
    }
    const { spawnSync } = require('child_process');
    const moduleToPkg = {
        fitz: 'pymupdf', pdfplumber: 'pdfplumber', PIL: 'pillow', pytesseract: 'pytesseract',
        cv2: 'opencv-python', easyocr: 'easyocr', numpy: 'numpy', edge_tts: 'edge-tts',
        faster_whisper: 'faster-whisper', reportlab: 'reportlab', arabic_reshaper: 'arabic-reshaper', bidi: 'python-bidi'
    };
    const mods = Object.keys(moduleToPkg);
    const checkCode = `import importlib.util, json
mods=${JSON.stringify(mods)}
missing=[m for m in mods if importlib.util.find_spec(m) is None]
print(json.dumps(missing))`;
    try {
        const chk = spawnSync(PYTHON_BIN || 'python3', ['-c', checkCode], { encoding: 'utf8', timeout: 15_000 });
        const missing = JSON.parse((chk.stdout || '[]').trim() || '[]');
        if (!missing.length) { console.log('✅ كل مكتبات Python الأساسية موجودة.'); return; }
        console.warn(`⚠️ مكتبات Python ناقصة وسيتم محاولة تثبيتها تلقائياً: ${missing.join(', ')}`);
        const pkgs = [...new Set(missing.map(m => moduleToPkg[m]).filter(Boolean))];
        const pipArgs = ['-m', 'pip', 'install', ...pkgs];
        let r = spawnSync(PYTHON_BIN || 'python3', pipArgs, { stdio: 'inherit', timeout: 10 * 60_000 });
        if (r.status !== 0) {
            r = spawnSync(PYTHON_BIN || 'python3', ['-m', 'pip', 'install', '--break-system-packages', ...pkgs], { stdio: 'inherit', timeout: 10 * 60_000 });
        }
        if (r.status !== 0) {
            const msg = `⚠️ فشل تثبيت بعض مكتبات Python تلقائياً: ${pkgs.join(', ')}. شغّل install_local_ai.sh يدوياً.`;
            console.warn(msg);
            notifyAdmin(msg).catch(()=>{});
        } else {
            console.log('✅ تم تثبيت مكتبات Python الناقصة تلقائياً.');
        }
    } catch (e) {
        console.warn('[ensurePythonRuntimeDeps]', e.message);
        notifyAdmin(`⚠️ فشل فحص/تثبيت مكتبات Python: ${e.message}`).catch(()=>{});
    }
}

ensurePythonRuntimeDeps();

console.log(`🚀 جاري تشغيل ${BOT_NAME}...`);
startQRServer();
startBot();
// حد أقصى لحجم body في لوحة التحكم لحماية الذاكرة
const DASHBOARD_MAX_BODY_BYTES = parseInt(process.env.DASHBOARD_MAX_BODY_BYTES || '65536', 10);

function readLimitedBody(req, limit = DASHBOARD_MAX_BODY_BYTES) {
    return new Promise((resolve, reject) => {
        let body = '';
        let size = 0;
        req.on('data', chunk => {
            size += chunk.length;
            if (size > limit) {
                reject(new Error('REQUEST_BODY_TOO_LARGE'));
                req.destroy();
                return;
            }
            body += chunk.toString();
        });
        req.on('end', () => resolve(body));
        req.on('error', reject);
    });
}

function sessionCookie(token) {
    const secure = process.env.DASHBOARD_SECURE_COOKIE === '1' ? '; Secure' : '';
    return `adm_tok=${encodeURIComponent(token)}; Path=/; HttpOnly; Max-Age=43200; SameSite=Lax${secure}`;
}


