# حفظ قاعدة إشارات TradingView في Google Drive

عند ضبط الربط، يستعيد `main:app` ملف `signals.sqlite3` من مجلد **Qaisar Radar Backups** في «ملفاتي» قبل استقبال الطلبات. وبعد كل إشارة جديدة، يرفع نسخة SQLite متسقة إلى الملف نفسه قبل إرجاع نجاح Webhook. يعيد إنشاء `trade_signals.csv` من القاعدة المستعادة. نطاق OAuth هو `https://www.googleapis.com/auth/drive.file`؛ التطبيق ينشئ مجلده وملفه ولا يطلب قراءة بقية Drive.

## إعداد Google مرة واحدة

1. أنشئ مشروعًا في Google Cloud، وفعّل **Google Drive API**.
2. اضبط شاشة موافقة OAuth لحسابك، ثم أنشئ OAuth Client من نوع **Web application**. أضف `https://developers.google.com/oauthplayground` ضمن Authorized redirect URIs.
3. في [OAuth 2.0 Playground](https://developers.google.com/oauthplayground/)، افتح الإعدادات واختر **Use your own OAuth credentials**. أدخل Client ID وClient Secret الخاصين بمشروعك. اختر **Offline**، ثم نطاق `https://www.googleapis.com/auth/drive.file`، واسمح للحساب الذي تريد حفظ النسخ فيه. في الخطوة الثانية استبدل authorization code بالرموز وخذ **refresh token**.
4. انقل القيم إلى **Render → الخدمة → Environment** في متغيرات منفصلة:

   | المتغير | القيمة |
   | --- | --- |
   | `GOOGLE_CLIENT_ID` | OAuth Client ID |
   | `GOOGLE_CLIENT_SECRET` | OAuth Client Secret |
   | `GOOGLE_REFRESH_TOKEN` | الرمز طويل الأجل الناتج من Playground |
   | `REQUIRE_DRIVE_BACKUP` | `1` |

5. احفظ المتغيرات وأعد النشر. تحقق من ظهور المجلد والملف في Google Drive بعد وصول إشارة تجريبية مصرح بها. أعد تشغيل الخدمة وتأكد أن `/signals` يعرض الإشارة السابقة؛ يتطلب `X-Admin-Token`.

**لا تضع الرموز في GitHub أو TradingView أو المحادثة.** إذا كانت شاشة موافقة OAuth خارجية وفي حالة **Testing**، فإن refresh token ينتهي عادة بعد سبعة أيام؛ انشر إعداد OAuth إلى **Production** قبل الاعتماد على النسخ المستمر. لا تنشر صلاحيات أوسع من `drive.file` لهذا الغرض.

## حدود التشغيل

- يستعيد التطبيق أحدث ملف قبل فتح SQLite، ويرفض بدء الخدمة إذا فشلت الاستعادة أو كان الملف غير صالح. لا يستبدل القاعدة المحلية بملف تالف.
- عند فشل رفع نسخة إشارة، يعيد `/webhook` رمز `503` ويحاول حفظ القاعدة المحلية مرة أخرى إذا وصل نفس التنبيه خلال نافذة منع التكرار. يبقى احتمال فقدان التنبيه إذا انقطعت خدمة Drive ثم توقف Render قبل نجاح الرفع؛ راقب أخطاء `503` وأعد إرسال الإشارة إن لزم. لا تعد هذه الآلية قاعدة بيانات معاملات مباشرة.
- ملف النسخة الواحدة له سقف 20 MiB. يلزم التخطيط لترحيل إلى قاعدة بيانات مستضافة إذا كبر الأرشيف أو زاد معدل الإشارات.
- حذف مجلد النسخ أو سحب صلاحية OAuth يوقف الاستعادة. على الخدمة المجانية تختفي الملفات المحلية عند السكون أو إعادة النشر.
- الكود الحالي يحفظ قاعدة `main.py` (`signals.db`). تطبيق `qaisar_radar/app.py` يستخدم قاعدة مستقلة ولم يُربط بهذا التغيير. تأكد من أمر التشغيل في Render لتعرف أي التطبيقين تستعمل.
- `render.yaml` الحالي يطلب `starter` وقرصًا مدفوعًا. هذه الخطوات تخص خدمة Render المجانية المنشأة يدويًا، بأمر التشغيل `uvicorn main:app --host 0.0.0.0 --port $PORT`.
