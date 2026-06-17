#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
متتبّع ترتيب الموقع في جوجل (Daily SERP Rank Tracker)
- يبحث عن كلمات مفتاحية محددة في جوجل من داخل السعودية
- يحدد ترتيب الدومين المستهدف (الصفحة + الترتيب داخل الصفحة + الترتيب الكلي)
- يكتب النتائج في Google Sheet داخل تبويب (sheet) جديد باسم تاريخ اليوم
- يضيف عمود تدقيق (أعلى 10 نتائج) + رابط بحث جوجل للتحقق اليدوي

محرّك البحث: DataForSEO (Google Organic Live Advanced) — دقيق للعربي والسعودية.
مجدول عبر GitHub Actions.
"""

import os
import re
import sys
import json
import time
import base64
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse, quote_plus
from zoneinfo import ZoneInfo

import requests
import yaml
import gspread
from google.oauth2.service_account import Credentials

RIYADH = ZoneInfo("Asia/Riyadh")
DFS_ENDPOINT = "https://api.dataforseo.com/v3/serp/google/organic/live/advanced"


# ----------------------------- إعدادات وبيئة -----------------------------
def env(name, default=None, required=False):
    v = os.environ.get(name, default)
    if required and not v:
        sys.exit(f"❌ متغيّر بيئة مفقود: {name}")
    return v


def load_config():
    cfg = {}
    p = Path("config.yaml")
    if p.exists():
        cfg = yaml.safe_load(p.read_text(encoding="utf-8")) or {}

    # السماح بتمرير الكلمات عبر Secret بدل ملف الإعداد (للخصوصية)
    kw_json = os.environ.get("KEYWORDS_JSON")
    if kw_json:
        try:
            data = json.loads(kw_json)
            if isinstance(data, dict):
                cfg.update(data)
            elif isinstance(data, list):
                cfg["keywords"] = data
        except json.JSONDecodeError:
            cfg["keywords"] = [k.strip() for k in re.split(r"[\n,]", kw_json) if k.strip()]

    if os.environ.get("TARGET_DOMAIN"):
        cfg["target_domain"] = os.environ["TARGET_DOMAIN"]

    cfg.setdefault("country_code", "sa")
    cfg.setdefault("language", "ar")
    cfg.setdefault("location_name", "Saudi Arabia")
    cfg.setdefault("max_pages", 5)

    if not cfg.get("target_domain") or not cfg.get("keywords"):
        sys.exit("❌ لازم config يحتوي على target_domain و keywords")

    # تنظيف الكلمات: إزالة المسافات الزائدة وحذف التكرار مع الحفاظ على الترتيب
    seen_kw, clean = set(), []
    for k in cfg["keywords"]:
        k = str(k).strip()
        if k and k not in seen_kw:
            seen_kw.add(k)
            clean.append(k)
    cfg["keywords"] = clean
    return cfg


def norm_domain(d):
    d = (d or "").strip().lower()
    d = re.sub(r"^https?://", "", d)
    d = d.split("/")[0]
    return d.replace("www.", "")


def host_of(url):
    try:
        return urlparse(url).netloc.lower().replace("www.", "")
    except Exception:
        return ""


# ----------------------------- نداء DataForSEO -----------------------------
def dfs_google(login, password, keyword, location_name, language, depth=50):
    """يرجّع قائمة النتائج العضوية مرتّبة (top N) من DataForSEO في طلب واحد.

    نستخدم Google Organic Live Advanced مع location_name + language_code،
    ونطلب depth نتيجة. نُرجّع فقط عناصر type == "organic" بترتيبها الفعلي،
    فنحصل على ترتيب مطلق موثوق (1، 2، 3 ...) مطابق لبحث محايد في السعودية.
    """
    cred = base64.b64encode(f"{login}:{password}".encode()).decode()
    headers = {
        "Authorization": f"Basic {cred}",
        "Content-Type": "application/json",
    }
    # الجسم عبارة عن قائمة مهام (مهمة واحدة هنا)
    payload = [{
        "keyword": keyword,
        "location_name": location_name,
        "language_code": language,
        "depth": depth,
        "device": "desktop",
        "os": "windows",
    }]

    for attempt in range(3):
        try:
            r = requests.post(DFS_ENDPOINT, headers=headers, json=payload, timeout=180)
            if r.status_code == 200:
                data = r.json()
                if data.get("status_code") != 20000:
                    print(f"  ⚠️ DataForSEO حالة عامة {data.get('status_code')}: {data.get('status_message')}")
                tasks = data.get("tasks") or []
                if not tasks:
                    return []
                task = tasks[0]
                if task.get("status_code") != 20000:
                    print(f"  ⚠️ مهمة DataForSEO كود {task.get('status_code')}: {task.get('status_message')}")
                    # رصيد غير كافٍ / مفاتيح غلط تظهر هنا
                result = task.get("result") or []
                if not result:
                    return []
                items = result[0].get("items") or []
                # نُبقي فقط النتائج العضوية بترتيبها
                organic = [it for it in items if it.get("type") == "organic"]
                return organic
            print(f"  ⚠️ بحث (محاولة {attempt+1}) كود HTTP {r.status_code}: {r.text[:200]}")
        except Exception as e:
            print(f"  ⚠️ خطأ بحث (محاولة {attempt+1}): {e}")
        time.sleep(3 * (attempt + 1))
    return []


def google_search_url(keyword, country, language, page):
    """رابط بحث جوجل المباشر — نظيف ومحايد (gl=الدولة, hl=اللغة, pws=0 يلغي التخصيص)."""
    start = (page - 1) * 10
    return (
        f"https://www.google.com/search?q={quote_plus(keyword)}"
        f"&gl={country}&hl={language}&start={start}&pws=0"
    )


# ----------------------------- كتابة Google Sheet -----------------------------
def write_sheet(sa_json, sheet_id, date_str, rows, now):
    creds = Credentials.from_service_account_info(
        json.loads(sa_json),
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(sheet_id)

    title = date_str
    if title in [w.title for w in sh.worksheets()]:
        title = f"{date_str} ({now.strftime('%H%M')})"

    ws = sh.add_worksheet(title=title, rows=len(rows) + 6, cols=12)

    headers = [
        "#", "الكلمة المفتاحية", "الحالة", "الصفحة", "الترتيب في الصفحة",
        "الترتيب الكلي", "الرابط الظاهر", "المركز الأول (منافس)",
        "رابط البحث (تحقّق يدوي)", "أعلى 10 نتائج (تدقيق)", "وقت الفحص",
    ]
    values = [[f"تقرير ترتيب الموقع في جوجل — {date_str}"]]
    values.append(headers)
    values.extend(rows)
    ws.update(range_name="A1", values=values, value_input_option="USER_ENTERED")

    sid = ws.id
    body = {"requests": [
        # اتجاه الصفحة من اليمين لليسار
        {"updateSheetProperties": {
            "properties": {"sheetId": sid, "rightToLeft": True},
            "fields": "rightToLeft"}},
        # تجميد أول صفّين
        {"updateSheetProperties": {
            "properties": {"sheetId": sid, "gridProperties": {"frozenRowCount": 2}},
            "fields": "gridProperties.frozenRowCount"}},
        # تنسيق صف العناوين
        {"repeatCell": {
            "range": {"sheetId": sid, "startRowIndex": 1, "endRowIndex": 2},
            "cell": {"userEnteredFormat": {
                "backgroundColor": {"red": 0.13, "green": 0.30, "blue": 0.45},
                "horizontalAlignment": "CENTER",
                "verticalAlignment": "MIDDLE",
                "textFormat": {"bold": True, "foregroundColor": {"red": 1, "green": 1, "blue": 1}}}},
            "fields": "userEnteredFormat(backgroundColor,horizontalAlignment,verticalAlignment,textFormat)"}},
        # محاذاة عمودية لصفوف البيانات + التفاف النص
        {"repeatCell": {
            "range": {"sheetId": sid, "startRowIndex": 2, "endRowIndex": 2 + len(rows)},
            "cell": {"userEnteredFormat": {"verticalAlignment": "MIDDLE", "wrapStrategy": "WRAP"}},
            "fields": "userEnteredFormat(verticalAlignment,wrapStrategy)"}},
        # عرض عمود الكلمة المفتاحية
        {"updateDimensionProperties": {
            "range": {"sheetId": sid, "dimension": "COLUMNS", "startIndex": 1, "endIndex": 2},
            "properties": {"pixelSize": 220}, "fields": "pixelSize"}},
        # عرض عمود الرابط
        {"updateDimensionProperties": {
            "range": {"sheetId": sid, "dimension": "COLUMNS", "startIndex": 6, "endIndex": 7},
            "properties": {"pixelSize": 320}, "fields": "pixelSize"}},
        # عرض عمود رابط البحث (تحقّق يدوي)
        {"updateDimensionProperties": {
            "range": {"sheetId": sid, "dimension": "COLUMNS", "startIndex": 8, "endIndex": 9},
            "properties": {"pixelSize": 170}, "fields": "pixelSize"}},
        # عرض عمود التدقيق (أعلى 10 نتائج)
        {"updateDimensionProperties": {
            "range": {"sheetId": sid, "dimension": "COLUMNS", "startIndex": 9, "endIndex": 10},
            "properties": {"pixelSize": 260}, "fields": "pixelSize"}},
        # ارتفاع صفوف البيانات (لإظهار قائمة التدقيق ذات 10 أسطر)
        {"updateDimensionProperties": {
            "range": {"sheetId": sid, "dimension": "ROWS", "startIndex": 2, "endIndex": 2 + len(rows)},
            "properties": {"pixelSize": 190}, "fields": "pixelSize"}},
    ]}
    sh.batch_update(body)
    print(f"✅ تم إنشاء التبويب: {title}")


# ----------------------------- المنطق الرئيسي -----------------------------
def main():
    login = env("DATAFORSEO_LOGIN", required=True)
    password = env("DATAFORSEO_PASSWORD", required=True)
    sa_json = env("GOOGLE_SERVICE_ACCOUNT_JSON", required=True)
    cfg = load_config()
    sheet_id = env("SHEET_ID") or cfg.get("sheet_id")
    if not sheet_id:
        sys.exit("❌ متغيّر SHEET_ID مفقود")

    target = norm_domain(cfg["target_domain"])
    keywords = cfg["keywords"]
    country = cfg["country_code"]
    language = cfg["language"]
    location_name = cfg["location_name"]
    max_pages = int(cfg["max_pages"])

    now = datetime.now(RIYADH)
    date_str = now.strftime("%Y-%m-%d")

    # ملف نبضة يُحدّث كل تشغيل — يبقي جدولة GitHub نشطة (تتعطّل بعد 60 يوم خمول)
    Path("last_run.txt").write_text(
        now.strftime("%Y-%m-%d %H:%M (Asia/Riyadh)"), encoding="utf-8")

    nb = max_pages * 10  # عدد النتائج المطلوبة في طلب واحد (depth)
    print(f"🎯 الدومين المستهدف: {target} | الموقع: {location_name} | اللغة: {language} | أعلى {nb} نتيجة")
    rows = []
    for i, kw in enumerate(keywords, start=1):
        organic = dfs_google(login, password, kw, location_name, language, depth=nb)

        # بناء قائمة مرتّبة نظيفة (روابط النتائج العضوية بالترتيب)
        ordered = []
        for res in organic:
            u = res.get("url") or ""
            if u:
                ordered.append(u)

        top_competitor = host_of(ordered[0]) if ordered else ""

        # إيجاد أول ظهور لقيود في القائمة المرتّبة
        found = None
        for idx, u in enumerate(ordered):
            if target and target in host_of(u):
                abs_pos = idx + 1
                found = {
                    "abs_pos": abs_pos,
                    "page": (abs_pos - 1) // 10 + 1,       # 10 نتائج عضوية لكل صفحة
                    "rank_in_page": (abs_pos - 1) % 10 + 1,
                    "url": u,
                }
                break

        if found:
            status = "✅ ظهر"
            page_v, rank_v, abs_v, url_v = found["page"], found["rank_in_page"], found["abs_pos"], found["url"]
            verify_page = found["page"]
        else:
            status = f"❌ ما ظهر ضمن أول {nb} نتيجة"
            page_v = rank_v = abs_v = url_v = "—"
            verify_page = 1

        # رابط بحث جوجل للتحقق اليدوي في نافذة خاصة (Incognito)
        verify_url = google_search_url(kw, country, language, verify_page)
        verify_cell = f'=HYPERLINK("{verify_url}","🔎 افتح بحث جوجل")'

        # عمود التدقيق: أعلى 10 نتائج كما رأتها الأداة (رقم. الدومين)
        audit = "\n".join(f"{n}. {host_of(u)}" for n, u in enumerate(ordered[:10], 1)) or "—"

        rows.append([i, kw, status, page_v, rank_v, abs_v, url_v,
                     top_competitor or "—", verify_cell, audit, now.strftime("%H:%M")])
        print(f"[{i}/{len(keywords)}] {kw} → {status}"
              + (f" (صفحة {page_v}، ترتيب كلي {abs_v})" if found else ""))
        time.sleep(1)

    write_sheet(sa_json, sheet_id, date_str, rows, now)
    print("🎉 تم بنجاح.")


if __name__ == "__main__":
    main()
