import base64
import hashlib
import hmac
import json
import os
import time

import httpx
import psycopg2
import psycopg2.extras
from psycopg2.pool import SimpleConnectionPool
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request, UploadFile, File, Form
from fastapi.responses import JSONResponse, Response, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pathlib import Path

load_dotenv()

CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")
CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET", "")
DATABASE_URL = os.getenv("DATABASE_URL", "")
LIFF_ID = os.getenv("LIFF_ID", "")

BASE_DIR = Path(__file__).parent

app = FastAPI(title="LINE OA External Chat POC")

_pool: SimpleConnectionPool | None = None


def get_pool() -> SimpleConnectionPool:
    global _pool
    if _pool is None:
        if not DATABASE_URL:
            raise RuntimeError(
                "DATABASE_URL is not set. Create a free Postgres database "
                "(e.g. Neon) and set DATABASE_URL in your environment."
            )
        _pool = SimpleConnectionPool(1, 5, dsn=DATABASE_URL)
    return _pool


def init_db():
    conn = get_pool().getconn()
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS media (
                    id SERIAL PRIMARY KEY,
                    mime_type TEXT NOT NULL,
                    filename TEXT,
                    data BYTEA NOT NULL,
                    created_at DOUBLE PRECISION NOT NULL
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS messages (
                    id SERIAL PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    display_name TEXT,
                    direction TEXT NOT NULL,
                    msg_type TEXT NOT NULL DEFAULT 'text',
                    text TEXT,
                    media_id INTEGER REFERENCES media(id),
                    created_at DOUBLE PRECISION NOT NULL,
                    line_message_id TEXT
                )
                """
            )
            # Migration for tables created before line_message_id existed.
            cur.execute("ALTER TABLE messages ADD COLUMN IF NOT EXISTS line_message_id TEXT")
            # LINE sometimes redelivers the same webhook event (e.g. if our
            # server was slow to respond, such as waking up from sleep on
            # Render's free tier). This unique index + ON CONFLICT DO NOTHING
            # in save_message() makes that safe to ignore instead of creating
            # duplicate messages.
            cur.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_line_message_id
                ON messages (line_message_id) WHERE line_message_id IS NOT NULL
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS conversation_meta (
                    user_id TEXT PRIMARY KEY,
                    assigned_to TEXT,
                    updated_at DOUBLE PRECISION NOT NULL
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS notes (
                    id SERIAL PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    author TEXT,
                    text TEXT,
                    media_id INTEGER REFERENCES media(id),
                    created_at DOUBLE PRECISION NOT NULL
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS customer_profile (
                    user_id TEXT PRIMARY KEY,
                    email TEXT,
                    phone TEXT,
                    first_name TEXT,
                    last_name TEXT,
                    birthday TEXT,
                    consent_at DOUBLE PRECISION,
                    updated_at DOUBLE PRECISION NOT NULL
                )
                """
            )
    finally:
        get_pool().putconn(conn)


@app.on_event("startup")
def on_startup():
    init_db()


# Mock sales team for the "assign conversation" demo feature. In a real
# system this would come from a users/accounts table with real login.
SALES_TEAM = [
    {"id": "sales1", "name": "คุณเอ - โครงการ A", "color": "#F44336"},
    {"id": "sales2", "name": "คุณบี - โครงการ B", "color": "#2196F3"},
    {"id": "sales3", "name": "คุณซี - โครงการ C", "color": "#FF9800"},
    {"id": "sales4", "name": "คุณดี - โครงการ D", "color": "#4CAF50"},
    {"id": "sales5", "name": "คุณอี - โครงการ E", "color": "#9C27B0"},
]


def verify_signature(body: bytes, signature: str) -> bool:
    """Verify LINE webhook signature. If CHANNEL_SECRET is not set (local dev
    before LINE credentials exist), skip verification so the app still runs."""
    if not CHANNEL_SECRET:
        return True
    digest = hmac.new(CHANNEL_SECRET.encode("utf-8"), body, hashlib.sha256).digest()
    expected = base64.b64encode(digest).decode("utf-8")
    return hmac.compare_digest(expected, signature or "")


async def fetch_display_name(user_id: str) -> str:
    if not CHANNEL_ACCESS_TOKEN:
        return user_id
    url = f"https://api.line.me/v2/bot/profile/{user_id}"
    headers = {"Authorization": f"Bearer {CHANNEL_ACCESS_TOKEN}"}
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(url, headers=headers)
            if resp.status_code == 200:
                return resp.json().get("displayName", user_id)
    except Exception:
        pass
    return user_id


async def download_line_content(message_id: str) -> tuple[bytes, str]:
    """Download image/video/audio/file content sent by a user via the
    LINE Content API. Returns (bytes, content_type)."""
    url = f"https://api-data.line.me/v2/bot/message/{message_id}/content"
    headers = {"Authorization": f"Bearer {CHANNEL_ACCESS_TOKEN}"}
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(url, headers=headers)
        resp.raise_for_status()
        content_type = resp.headers.get("content-type", "application/octet-stream")
        return resp.content, content_type


def save_media(mime_type: str, filename: str | None, data: bytes) -> int:
    conn = get_pool().getconn()
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO media (mime_type, filename, data, created_at) VALUES (%s, %s, %s, %s) RETURNING id",
                (mime_type, filename, psycopg2.Binary(data), time.time()),
            )
            return cur.fetchone()[0]
    finally:
        get_pool().putconn(conn)


def get_media(media_id: int):
    conn = get_pool().getconn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT mime_type, filename, data FROM media WHERE id = %s", (media_id,))
            return cur.fetchone()
    finally:
        get_pool().putconn(conn)


def save_message(
    user_id: str,
    display_name: str,
    direction: str,
    msg_type: str = "text",
    text: str | None = None,
    media_id: int | None = None,
    line_message_id: str | None = None,
):
    conn = get_pool().getconn()
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO messages (user_id, display_name, direction, msg_type, text, media_id, created_at, line_message_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (line_message_id) WHERE line_message_id IS NOT NULL DO NOTHING
                """,
                (user_id, display_name, direction, msg_type, text, media_id, time.time(), line_message_id),
            )
    finally:
        get_pool().putconn(conn)


def is_duplicate_line_message(line_message_id: str) -> bool:
    conn = get_pool().getconn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM messages WHERE line_message_id = %s LIMIT 1", (line_message_id,))
            return cur.fetchone() is not None
    finally:
        get_pool().putconn(conn)


def get_known_display_name(user_id: str) -> str:
    conn = get_pool().getconn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT display_name FROM messages
                WHERE user_id = %s AND display_name IS NOT NULL AND display_name != ''
                ORDER BY created_at DESC LIMIT 1
                """,
                (user_id,),
            )
            row = cur.fetchone()
            return row["display_name"] if row else user_id
    finally:
        get_pool().putconn(conn)


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.get("/api/sales-team")
def get_sales_team():
    return SALES_TEAM


@app.post("/webhook")
async def line_webhook(request: Request):
    body = await request.body()
    signature = request.headers.get("X-Line-Signature", "")

    if not verify_signature(body, signature):
        raise HTTPException(status_code=403, detail="Invalid signature")

    payload = json.loads(body) if body else {}
    events = payload.get("events", [])

    for event in events:
        if event.get("type") != "message":
            continue

        message = event.get("message", {})
        msg_type = message.get("type")
        user_id = event["source"]["userId"]
        line_message_id = message.get("id")

        # LINE may redeliver the same webhook event (e.g. our server was slow
        # to wake up from Render's free-tier sleep and LINE timed out waiting
        # for a response, then retried). Skip anything we've already stored.
        if line_message_id and is_duplicate_line_message(line_message_id):
            continue

        display_name = await fetch_display_name(user_id)

        if msg_type == "text":
            save_message(
                user_id, display_name, "in", "text",
                text=message.get("text", ""), line_message_id=line_message_id,
            )

        elif msg_type in ("image", "file", "video", "audio"):
            try:
                data, content_type = await download_line_content(message["id"])
            except Exception:
                save_message(
                    user_id, display_name, "in", "text",
                    text=f"[ไม่สามารถดาวน์โหลด {msg_type} จากลูกค้าได้]",
                    line_message_id=line_message_id,
                )
                continue

            filename = message.get("fileName")
            stored_type = msg_type if msg_type in ("image", "file") else "file"
            if stored_type == "file" and not filename:
                ext = content_type.split("/")[-1] if "/" in content_type else "bin"
                filename = f"{msg_type}.{ext}"
            media_id = save_media(content_type, filename, data)
            save_message(
                user_id, display_name, "in", stored_type,
                text=filename, media_id=media_id, line_message_id=line_message_id,
            )

        else:
            save_message(
                user_id, display_name, "in", "text",
                text=f"[ลูกค้าส่ง {msg_type} มา — ยังไม่รองรับการแสดงผลประเภทนี้]",
                line_message_id=line_message_id,
            )

    return JSONResponse({"status": "ok"})


@app.get("/api/conversations")
def list_conversations():
    conn = get_pool().getconn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT m.user_id, MAX(m.created_at) as last_at, cm.assigned_to
                FROM messages m
                LEFT JOIN conversation_meta cm ON cm.user_id = m.user_id
                GROUP BY m.user_id, cm.assigned_to
                ORDER BY last_at DESC
                """
            )
            rows = cur.fetchall()
    finally:
        get_pool().putconn(conn)

    result = []
    for r in rows:
        result.append(
            {
                "user_id": r["user_id"],
                "display_name": get_known_display_name(r["user_id"]),
                "last_at": r["last_at"],
                "assigned_to": r["assigned_to"],
            }
        )
    return result


@app.post("/api/conversations/{user_id}/assign")
async def assign_conversation(user_id: str, request: Request):
    body = await request.json()
    assigned_to = body.get("assigned_to")  # a SALES_TEAM id, or null to unassign
    conn = get_pool().getconn()
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO conversation_meta (user_id, assigned_to, updated_at)
                VALUES (%s, %s, %s)
                ON CONFLICT (user_id) DO UPDATE SET assigned_to = EXCLUDED.assigned_to, updated_at = EXCLUDED.updated_at
                """,
                (user_id, assigned_to, time.time()),
            )
    finally:
        get_pool().putconn(conn)
    return {"status": "ok", "assigned_to": assigned_to}


@app.get("/api/conversations/{user_id}/messages")
def get_messages(user_id: str):
    conn = get_pool().getconn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT direction, msg_type, text, media_id, created_at
                FROM messages WHERE user_id = %s ORDER BY created_at ASC
                """,
                (user_id,),
            )
            return [dict(r) for r in cur.fetchall()]
    finally:
        get_pool().putconn(conn)


@app.get("/api/conversations/{user_id}/notes")
def get_notes(user_id: str):
    conn = get_pool().getconn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT id, author, text, media_id, created_at
                FROM notes WHERE user_id = %s ORDER BY created_at ASC
                """,
                (user_id,),
            )
            return [dict(r) for r in cur.fetchall()]
    finally:
        get_pool().putconn(conn)


@app.post("/api/conversations/{user_id}/notes")
async def add_note(user_id: str, request: Request):
    body = await request.json()
    author = (body.get("author") or "").strip() or "ไม่ระบุ"
    text = (body.get("text") or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="text is required")
    conn = get_pool().getconn()
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO notes (user_id, author, text, created_at) VALUES (%s, %s, %s, %s)",
                (user_id, author, text, time.time()),
            )
    finally:
        get_pool().putconn(conn)
    return {"status": "ok"}


@app.post("/api/conversations/{user_id}/notes-media")
async def add_note_with_media(
    user_id: str,
    author: str = Form("ไม่ระบุ"),
    text: str = Form(""),
    file: UploadFile = File(...),
):
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="empty file")
    content_type = file.content_type or "application/octet-stream"
    media_id = save_media(content_type, file.filename, data)
    conn = get_pool().getconn()
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO notes (user_id, author, text, media_id, created_at) VALUES (%s, %s, %s, %s, %s)",
                (user_id, author.strip() or "ไม่ระบุ", text.strip(), media_id, time.time()),
            )
    finally:
        get_pool().putconn(conn)
    return {"status": "ok", "media_id": media_id}


@app.get("/api/conversations/{user_id}/profile")
def get_customer_profile(user_id: str):
    conn = get_pool().getconn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT email, phone, first_name, last_name, birthday, consent_at, updated_at
                FROM customer_profile WHERE user_id = %s
                """,
                (user_id,),
            )
            row = cur.fetchone()
            return dict(row) if row else {}
    finally:
        get_pool().putconn(conn)


@app.post("/api/customer-profile")
async def save_customer_profile(request: Request):
    """Called from the /register LIFF form the customer fills in themselves."""
    body = await request.json()
    user_id = (body.get("user_id") or "").strip()
    email = (body.get("email") or "").strip()
    phone = (body.get("phone") or "").strip()
    first_name = (body.get("first_name") or "").strip()
    last_name = (body.get("last_name") or "").strip()
    birthday = (body.get("birthday") or "").strip()

    if not user_id or not email or not first_name or not last_name:
        raise HTTPException(status_code=400, detail="user_id, email, first_name, last_name are required")

    now = time.time()
    conn = get_pool().getconn()
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO customer_profile (user_id, email, phone, first_name, last_name, birthday, consent_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (user_id) DO UPDATE SET
                    email = EXCLUDED.email,
                    phone = EXCLUDED.phone,
                    first_name = EXCLUDED.first_name,
                    last_name = EXCLUDED.last_name,
                    birthday = EXCLUDED.birthday,
                    consent_at = EXCLUDED.consent_at,
                    updated_at = EXCLUDED.updated_at
                """,
                (user_id, email, phone or None, first_name, last_name, birthday or None, now, now),
            )
    finally:
        get_pool().putconn(conn)
    return {"status": "ok"}


@app.post("/api/conversations/{user_id}/send-registration-link")
async def send_registration_link(user_id: str):
    if not CHANNEL_ACCESS_TOKEN:
        raise HTTPException(status_code=500, detail="LINE_CHANNEL_ACCESS_TOKEN is not set on the server")
    if not LIFF_ID:
        raise HTTPException(status_code=500, detail="LIFF_ID is not set on the server (see README)")

    link = f"https://liff.line.me/{LIFF_ID}"
    text = f"รบกวนกรอกข้อมูลติดต่อเพิ่มเติมที่ลิงก์นี้ด้วยนะคะ/ครับ 🙏\n{link}"
    await push_message(user_id, [{"type": "text", "text": text}])
    save_message(user_id, "", "out", "text", text=text)
    return {"status": "sent", "url": link}


REGISTER_PAGE_HTML = """<!DOCTYPE html>
<html lang="th">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1" />
<title>ลงทะเบียนรับข้อมูลเพิ่มเติม</title>
<script src="https://static.line-scdn.net/liff/edge/2/sdk.js"></script>
<style>
  * { box-sizing: border-box; }
  body { font-family: -apple-system, "Segoe UI", Tahoma, sans-serif; margin: 0; padding: 24px 16px; background: #f4f4f4; }
  .card { background: #fff; border-radius: 14px; padding: 22px; max-width: 420px; margin: 0 auto; box-shadow: 0 1px 4px rgba(0,0,0,0.08); }
  h1 { font-size: 18px; margin: 0 0 6px; }
  .sub { font-size: 13px; color: #666; margin-bottom: 4px; }
  label { display: block; font-size: 13px; margin: 14px 0 4px; color: #333; }
  input[type=email], input[type=tel], input[type=text], input[type=date] {
    width: 100%; padding: 10px 12px; border: 1px solid #ccc; border-radius: 8px; font-size: 15px;
  }
  .row2 { display: flex; gap: 10px; }
  .row2 > div { flex: 1; }
  .consent { display: flex; align-items: flex-start; gap: 8px; margin-top: 18px; font-size: 12px; color: #555; }
  .consent input { margin-top: 3px; }
  button { margin-top: 20px; width: 100%; padding: 12px; background: #06c755; color: #fff; border: none; border-radius: 10px; font-size: 15px; cursor: pointer; }
  button:disabled { background: #ccc; }
  #status { margin-top: 12px; font-size: 13px; }
  #status.error { color: #c0392b; }
  #status.success { color: #06834a; }
</style>
</head>
<body>
  <div class="card">
    <h1>ลงทะเบียนรับข้อมูลเพิ่มเติม</h1>
    <p class="sub">กรอกข้อมูลด้านล่างเพื่อให้ทีมงานติดต่อกลับและส่งข้อมูล/สิทธิพิเศษให้คุณ</p>
    <form id="reg-form">
      <div class="row2">
        <div>
          <label>ชื่อ *</label>
          <input type="text" id="first_name" required />
        </div>
        <div>
          <label>นามสกุล *</label>
          <input type="text" id="last_name" required />
        </div>
      </div>
      <label>อีเมล *</label>
      <input type="email" id="email" required />
      <label>เบอร์โทร</label>
      <input type="tel" id="phone" />
      <label>วันเกิด</label>
      <input type="date" id="birthday" />
      <div class="consent">
        <input type="checkbox" id="consent" required />
        <span>ยินยอมให้เก็บและใช้ข้อมูลนี้เพื่อติดต่อกลับ ตามนโยบายความเป็นส่วนตัว (PDPA)</span>
      </div>
      <button type="submit" id="submit-btn">ส่งข้อมูล</button>
    </form>
    <div id="status"></div>
  </div>

<script>
  const LIFF_ID = "__LIFF_ID__";
  let userId = null;
  const statusEl = document.getElementById('status');
  const form = document.getElementById('reg-form');
  const submitBtn = document.getElementById('submit-btn');

  function setStatus(msg, cls) {
    statusEl.textContent = msg;
    statusEl.className = cls || '';
  }

  async function main() {
    if (!LIFF_ID) {
      setStatus('ระบบยังไม่ได้ตั้งค่า LIFF_ID กรุณาติดต่อผู้ดูแลระบบ', 'error');
      submitBtn.disabled = true;
      return;
    }
    try {
      await liff.init({ liffId: LIFF_ID });
      if (!liff.isLoggedIn() && !liff.isInClient()) {
        liff.login();
        return;
      }
      const profile = await liff.getProfile();
      userId = profile.userId;
    } catch (err) {
      setStatus('ไม่สามารถยืนยันตัวตนผ่าน LINE ได้ กรุณาเปิดลิงก์นี้จากแชท LINE: ' + err.message, 'error');
      submitBtn.disabled = true;
    }
  }

  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    if (!userId) {
      setStatus('ยังไม่พร้อมส่งข้อมูล กรุณาเปิดลิงก์นี้จากแชท LINE อีกครั้ง', 'error');
      return;
    }
    submitBtn.disabled = true;
    setStatus('กำลังส่งข้อมูล...', '');

    const payload = {
      user_id: userId,
      first_name: document.getElementById('first_name').value.trim(),
      last_name: document.getElementById('last_name').value.trim(),
      email: document.getElementById('email').value.trim(),
      phone: document.getElementById('phone').value.trim(),
      birthday: document.getElementById('birthday').value,
    };

    try {
      const res = await fetch('/api/customer-profile', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      if (!res.ok) {
        const err = await res.text();
        setStatus('เกิดข้อผิดพลาด: ' + err, 'error');
        submitBtn.disabled = false;
        return;
      }
      setStatus('ส่งข้อมูลเรียบร้อยแล้ว ขอบคุณค่ะ/ครับ', 'success');
      form.style.display = 'none';
      setTimeout(() => { if (liff.isInClient && liff.isInClient()) liff.closeWindow(); }, 1500);
    } catch (err) {
      setStatus('เกิดข้อผิดพลาด: ' + err.message, 'error');
      submitBtn.disabled = false;
    }
  });

  main();
</script>
</body>
</html>
"""


@app.get("/register", response_class=HTMLResponse)
def register_page():
    return HTMLResponse(content=REGISTER_PAGE_HTML.replace("__LIFF_ID__", LIFF_ID))


@app.post("/api/conversations/{user_id}/reply")
async def reply_to_user(user_id: str, request: Request):
    body = await request.json()
    text = (body.get("text") or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="text is required")

    if not CHANNEL_ACCESS_TOKEN:
        raise HTTPException(status_code=500, detail="LINE_CHANNEL_ACCESS_TOKEN is not set on the server")

    await push_message(user_id, [{"type": "text", "text": text}])
    save_message(user_id, "", "out", "text", text=text)
    return {"status": "sent"}


@app.post("/api/conversations/{user_id}/reply-media")
async def reply_with_media(request: Request, user_id: str, file: UploadFile = File(...), kind: str = Form(...)):
    if not CHANNEL_ACCESS_TOKEN:
        raise HTTPException(status_code=500, detail="LINE_CHANNEL_ACCESS_TOKEN is not set on the server")

    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="empty file")

    content_type = file.content_type or "application/octet-stream"
    media_id = save_media(content_type, file.filename, data)
    public_url = str(request.base_url).rstrip("/") + f"/media/{media_id}"

    if kind == "image":
        if not content_type.startswith("image/"):
            raise HTTPException(status_code=400, detail="ไฟล์นี้ไม่ใช่รูปภาพ")
        await push_message(
            user_id,
            [{"type": "image", "originalContentUrl": public_url, "previewImageUrl": public_url}],
        )
        save_message(user_id, "", "out", "image", text=file.filename, media_id=media_id)
    else:
        # LINE's Messaging API has no generic "send file" message type, so we
        # send a text message containing a link the customer can tap to download.
        await push_message(
            user_id,
            [{"type": "text", "text": f"📎 {file.filename}\n{public_url}"}],
        )
        save_message(user_id, "", "out", "file", text=file.filename, media_id=media_id)

    return {"status": "sent", "url": public_url}


async def push_message(user_id: str, messages: list):
    url = "https://api.line.me/v2/bot/message/push"
    headers = {
        "Authorization": f"Bearer {CHANNEL_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {"to": user_id, "messages": messages}
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(url, headers=headers, json=payload)
    if resp.status_code != 200:
        raise HTTPException(status_code=resp.status_code, detail=resp.text)


@app.get("/media/{media_id}")
def serve_media(media_id: int):
    row = get_media(media_id)
    if not row:
        raise HTTPException(status_code=404, detail="not found")
    headers = {}
    if row["filename"]:
        headers["Content-Disposition"] = f'inline; filename="{row["filename"]}"'
    return Response(content=bytes(row["data"]), media_type=row["mime_type"], headers=headers)


# Serve the agent UI (static/index.html) for everything else.
app.mount("/", StaticFiles(directory=BASE_DIR / "static", html=True), name="static")
