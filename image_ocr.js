تقرير إصلاح مشاكل قائمة "كورسات نادر"

تم فحص آخر نسخة من المشروع وتطبيق الإصلاحات المؤكدة مع الحفاظ على كل الميزات والوظائف.

ما تم إصلاحه/تأكيده:
1) bot.js — caption قبل الاستخدام:
   تم التحقق أن const caption يُعرّف قبل looksLikeImageEnhanceRequest(caption)، ولا يوجد استخدام قبله في مسار الصور.

2) bot.js — path module:
   تم نقل import path إلى أعلى الملف:
   const path = require('path');
   وتم حذف التعريف المتأخر، وتوحيد استخدام path.join بدل require('path').join المتكرر.

3) bot.js — findTool:
   تم توحيد استخدام fs المستورد أعلى الملف بدل require('fs') داخل findTool.

4) aiSemaphore:
   تم التحقق أن release محمي بمتغير released ولا يمكن تحريره مرتين.

5) web dashboard rate limiting:
   تم تحسين rate limit بحيث تكون طلبات /api و /data بحد أكثر صرامة من صفحات الويب العادية.

6) notifyAdmin:
   تم تحصينه ضد الرسائل الطويلة جداً، وإضافة log واضح إذا لم يكن sock جاهزاً بدل فشل صامت.

7) install_local_ai.sh:
   تم إضافة محاولة تثبيت espeak/espeak-ng اختيارياً لتحسين توافق pyttsx3 fallback.

8) parseCookie / readLimitedBody / image OCR timeout:
   كانت محمية مسبقاً في النسخة الحالية وتم التحقق منها.

9) splitMessage:
   كان محسناً مسبقاً لمعالجة النصوص الطويلة جداً بدون فقدان بيانات.

10) PDF/TXT/Image local workers:
   تم اختبار إنشاء TXT و PDF وصورة محلية بنجاح.

الفحوصات التي نجحت:
- node --check bot.js
- node --check pdf_processor.js
- node --check image_ocr.js
- node --check file_tools.js
- node --check image_tools.js
- python3 -m py_compile *.py
- bash -n install_local_ai.sh
- اختبار إنشاء PDF/TXT/صورة محلياً

ملاحظات مهمة:
- EasyOCR و Whisper ما زالا ثقيلين على Termux بطبيعتهم؛ هذا ليس خطأ كود، بل محدودية موارد الجهاز.
- edge-tts يحتاج إنترنت؛ pyttsx3 موجود كـ fallback لكن دعمه في Termux يعتمد على توفر محرك صوت مثل espeak.
- لم يتم حذف أي ميزة من المشروع.
