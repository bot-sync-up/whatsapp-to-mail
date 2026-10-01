# -*- coding: utf-8 -*-
"""
מעביר הודעות מקבוצות ווצאפ (דרך WAHA) למייל מרוכז.
ללא ספריות חיצוניות – רק פייתון רגיל.
"""
import html
import json
import os
import smtplib
import sqlite3
import threading
import time
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ---------- הגדרות (נקראות מקובץ .env) ----------
GROUPS_RAW = os.getenv("GROUPS", "").strip()          # 1203...@g.us|שם הקבוצה,1203...@g.us|שם אחר
MAIL_FROM = os.getenv("MAIL_FROM", "").strip()
MAIL_APP_PASSWORD = os.getenv("MAIL_APP_PASSWORD", "").replace(" ", "")
MAIL_TO = [x.strip() for x in os.getenv("MAIL_TO", "").split(",") if x.strip()]
SEND_MODE = os.getenv("SEND_MODE", "daily").strip().lower()   # immediate / hourly / daily
DAILY_TIME = os.getenv("DAILY_TIME", "21:00").strip()
DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"     # true = רק להדפיס את המייל בלוג
DB_PATH = os.getenv("DB_PATH", "/data/messages.db")
PORT = int(os.getenv("PORT", "5000"))

GROUPS = {}
for item in GROUPS_RAW.split(","):
    item = item.strip()
    if not item:
        continue
    gid, _, name = item.partition("|")
    GROUPS[gid.strip()] = name.strip() or "הקבוצה"

MEDIA_TYPES = {
    "image": "[נשלחה תמונה]",
    "video": "[נשלח סרטון]",
    "ptt": "[נשלחה הקלטה קולית]",
    "audio": "[נשלח קובץ שמע]",
    "document": "[נשלח קובץ]",
    "sticker": "[נשלח סטיקר]",
    "location": "[נשלח מיקום]",
    "vcard": "[נשלח איש קשר]",
    "multi_vcard": "[נשלחו אנשי קשר]",
    "poll_creation": "[נשלח סקר]",
}

db_lock = threading.Lock()


def log(msg):
    print(datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg, flush=True)


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""CREATE TABLE IF NOT EXISTS messages (
        id TEXT PRIMARY KEY, ts INTEGER, group_id TEXT, group_name TEXT,
        sender TEXT, text TEXT, sent INTEGER DEFAULT 0)""")
    conn.execute("CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT)")
    return conn


def sender_name(p):
    if p.get("fromMe"):
        return "אני"
    data = p.get("_data") or {}
    info = data.get("Info") or {}
    for name in (data.get("notifyName"), data.get("pushName"), info.get("PushName")):
        if name:
            return name
    who = p.get("participant") or p.get("author") or ""
    if who.endswith("@lid"):        # מספר מוסתר של ווצאפ
        return "משתתף"
    return who.split("@")[0] or "לא ידוע"


def message_text(p):
    body = (p.get("body") or "").strip()
    data = p.get("_data") or {}
    mtype = data.get("type", "")
    label = MEDIA_TYPES.get(mtype)
    if label is None and p.get("hasMedia"):
        label = "[נשלחה מדיה]"
    if label and mtype in ("image", "video", "document"):
        return f"{label} {body}".strip()        # מדיה + כיתוב
    if label:
        return label
    return body


def handle_event(event):
    if event.get("event") not in ("message", "message.any"):
        return
    p = event.get("payload") or {}
    chat_id = p.get("to") if p.get("fromMe") else p.get("from")
    if not chat_id or not chat_id.endswith("@g.us"):
        return
    text = message_text(p)
    if not text:
        return
    if not GROUPS:
        log(f"[מצב למידה] הודעה בקבוצה עם ID: {chat_id} | {text[:40]}")
        return
    if chat_id not in GROUPS:
        return
    sender = sender_name(p)
    with db_lock:
        conn = db()
        cur = conn.execute(
            "INSERT OR IGNORE INTO messages (id, ts, group_id, group_name, sender, text) VALUES (?,?,?,?,?,?)",
            (p.get("id"), int(p.get("timestamp") or time.time()), chat_id, GROUPS[chat_id], sender, text))
        conn.commit()
        conn.close()
    if cur.rowcount:
        log(f"התקבלה הודעה מ-{sender} ב-{GROUPS[chat_id]}: {text[:60]}")


def build_mail(rows):
    by_group = {}
    for _id, ts, _gid, gname, sender, text in rows:
        by_group.setdefault(gname, []).append((ts, sender, text))
    parts_html, parts_txt = [], []
    for gname, msgs in by_group.items():
        parts_html.append(f'<h2 style="color:#075e54">{html.escape(gname)}</h2>')
        parts_txt.append(f"=== {gname} ===")
        last_day = None
        for ts, sender, text in msgs:
            dt = datetime.fromtimestamp(ts)
            day = dt.strftime("%d/%m/%Y")
            if day != last_day:
                parts_html.append(f'<div style="color:#888;margin-top:10px">{day}</div>')
                last_day = day
            safe = html.escape(text).replace("\n", "<br>")
            parts_html.append(
                f'<div style="margin:6px 0"><b>{dt:%H:%M} – {html.escape(sender)}:</b> {safe}</div>')
            parts_txt.append(f"{dt:%d/%m %H:%M} – {sender}: {text}")
    body_html = ('<div dir="rtl" style="font-family:Arial,sans-serif;font-size:15px">'
                 + "".join(parts_html) + "</div>")
    names = " + ".join(by_group.keys())
    subject = f"סיכום ווצאפ – {names} – {datetime.now():%d/%m/%Y %H:%M}"
    return subject, body_html, "\n".join(parts_txt)


def send_pending():
    with db_lock:
        conn = db()
        rows = conn.execute(
            "SELECT id, ts, group_id, group_name, sender, text FROM messages WHERE sent=0 ORDER BY ts").fetchall()
        conn.close()
    if not rows:
        return
    subject, body_html, body_txt = build_mail(rows)
    if DRY_RUN:
        log(f"[DRY_RUN] מייל לא נשלח באמת. נושא: {subject}\n{body_txt}")
    else:
        msg = MIMEMultipart("alternative")
        msg["Subject"], msg["From"], msg["To"] = subject, MAIL_FROM, ", ".join(MAIL_TO)
        msg.attach(MIMEText(body_txt, "plain", "utf-8"))
        msg.attach(MIMEText(body_html, "html", "utf-8"))
        try:
            with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as s:
                s.login(MAIL_FROM, MAIL_APP_PASSWORD)
                s.sendmail(MAIL_FROM, MAIL_TO, msg.as_string())
        except Exception as e:
            log(f"שגיאה בשליחת המייל (ננסה שוב בסבב הבא): {e}")
            return
    with db_lock:
        conn = db()
        conn.executemany("UPDATE messages SET sent=1 WHERE id=?", [(r[0],) for r in rows])
        conn.execute("DELETE FROM messages WHERE sent=1 AND ts < ?", (int(time.time()) - 30 * 86400,))
        conn.commit()
        conn.close()
    log(f"נשלח מייל עם {len(rows)} הודעות")


def get_meta(k):
    with db_lock:
        conn = db()
        row = conn.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
        conn.close()
    return row[0] if row else None


def set_meta(k, v):
    with db_lock:
        conn = db()
        conn.execute("INSERT OR REPLACE INTO meta (k, v) VALUES (?,?)", (k, v))
        conn.commit()
        conn.close()


def scheduler():
    while True:
        try:
            now = datetime.now()
            if SEND_MODE == "immediate":
                send_pending()
            elif SEND_MODE == "hourly":
                key = now.strftime("%Y-%m-%d %H")
                if get_meta("last") != key:
                    send_pending()
                    set_meta("last", key)
            else:  # daily
                key = now.strftime("%Y-%m-%d")
                if now.strftime("%H:%M") >= DAILY_TIME and get_meta("last") != key:
                    send_pending()
                    set_meta("last", key)
        except Exception as e:
            log(f"שגיאה בתזמון: {e}")
        time.sleep(20)


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")
        try:
            handle_event(json.loads(raw or b"{}"))
        except Exception as e:
            log(f"שגיאה בטיפול בהודעה: {e}")

    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write("forwarder פועל".encode("utf-8"))

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    db().close()
    if not GROUPS:
        log("לא הוגדרו קבוצות (GROUPS ריק) – מצב למידה: שלחו הודעה בקבוצה וה-ID שלה יופיע כאן")
    else:
        log(f"מאזין לקבוצות: {', '.join(GROUPS.values())} | מצב שליחה: {SEND_MODE}")
    threading.Thread(target=scheduler, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
