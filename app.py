import base64
import hashlib
import hmac
import json
import os
import secrets
import time

import bcrypt
import httpx
import psycopg2
import psycopg2.extras
from psycopg2.pool import SimpleConnectionPool
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Request, UploadFile, File, Form
from fastapi.responses import JSONResponse, RedirectResponse, Response, HTMLResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
from pathlib import Path

load_dotenv()

CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")
CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET", "")
DATABASE_URL = os.getenv("DATABASE_URL", "")
LIFF_ID = os.getenv("LIFF_ID", "")

# Used to sign the login session cookie. If not set, a random one is
# generated per process start — that just means everyone gets logged out
# whenever the server restarts/redeploys. Set a fixed value in production
# (see README) so logins survive redeploys.
SECRET_KEY = os.getenv("SECRET_KEY") or secrets.token_hex(32)

# Used once, only if the admin_users table is completely empty, to create
# the very first login. Change this password after logging in (see README).
ADMIN_BOOTSTRAP_USERNAME = os.getenv("ADMIN_BOOTSTRAP_USERNAME", "admin")
ADMIN_BOOTSTRAP_PASSWORD = os.getenv("ADMIN_BOOTSTRAP_PASSWORD", "changeme123")

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
            # Migration: who (which logged-in account) sent each outbound message.
            cur.execute("ALTER TABLE messages ADD COLUMN IF NOT EXISTS sender_user_id INTEGER")
            cur.execute("ALTER TABLE messages ADD COLUMN IF NOT EXISTS sender_name TEXT")
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
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS admin_users (
                    id SERIAL PRIMARY KEY,
                    username TEXT UNIQUE NOT NULL,
                    password_hash TEXT NOT NULL,
                    display_name TEXT NOT NULL,
                    color TEXT NOT NULL DEFAULT '#607D8B',
                    role TEXT NOT NULL DEFAULT 'sales',
                    active BOOLEAN NOT NULL DEFAULT TRUE,
                    created_at DOUBLE PRECISION NOT NULL
                )
                """
            )

            # Migration: profile photo used as the LINE "sender.iconUrl" when
            # this account replies to a customer (see icon-nickname-switch).
            cur.execute("ALTER TABLE admin_users ADD COLUMN IF NOT EXISTS photo_media_id INTEGER REFERENCES media(id)")

            # First-run bootstrap: if there are no accounts at all yet, create
            # one admin account so someone can log in and add the rest.
            cur.execute("SELECT COUNT(*) FROM admin_users")
            if cur.fetchone()[0] == 0:
                cur.execute(
                    """
                    INSERT INTO admin_users (username, password_hash, display_name, color, role, active, created_at)
                    VALUES (%s, %s, %s, %s, 'admin', TRUE, %s)
                    """,
                    (
                        ADMIN_BOOTSTRAP_USERNAME,
                        hash_password(ADMIN_BOOTSTRAP_PASSWORD),
                        "ผู้ดูแลระบบ",
                        "#607D8B",
                        time.time(),
                    ),
                )
    finally:
        get_pool().putconn(conn)


@app.on_event("startup")
def on_startup():
    init_db()


# ---------------------------------------------------------------------------
# Accounts / login
# ---------------------------------------------------------------------------

def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except Exception:
        return False


def get_admin_user_by_username(username: str):
    conn = get_pool().getconn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM admin_users WHERE username = %s", (username,))
            return cur.fetchone()
    finally:
        get_pool().putconn(conn)


def get_admin_user_by_id(user_id: int):
    conn = get_pool().getconn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM admin_users WHERE id = %s", (user_id,))
            return cur.fetchone()
    finally:
        get_pool().putconn(conn)


def list_active_admin_users():
    conn = get_pool().getconn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT id, username, display_name, color, role FROM admin_users WHERE active = TRUE ORDER BY display_name"
            )
            return [dict(r) for r in cur.fetchall()]
    finally:
        get_pool().putconn(conn)


def list_all_admin_users():
    conn = get_pool().getconn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT id, username, display_name, color, role, active, created_at FROM admin_users ORDER BY created_at"
            )
            return [dict(r) for r in cur.fetchall()]
    finally:
        get_pool().putconn(conn)


def get_current_user(request: Request) -> dict:
    """FastAPI dependency: require a logged-in, still-active account.
    Re-checks the database (not just the session cookie) on every call, so a
    deactivated account loses access immediately instead of at next login."""
    user_id = request.session.get("user_id")
    if not user_id:
        raise HTTPException(status_code=401, detail="unauthorized")
    user = get_admin_user_by_id(user_id)
    if not user or not user["active"]:
        raise HTTPException(status_code=401, detail="unauthorized")
    return user


def require_admin(user: dict = Depends(get_current_user)) -> dict:
    if user["role"] != "admin":
        raise HTTPException(status_code=403, detail="admin only")
    return user


def build_sender(current_user: dict, request: Request) -> dict | None:
    """Build a LINE `sender` object (see "Customize icon and display name" in
    the Messaging API docs) so the customer sees which specific account
    replied, instead of just the LINE Official Account's own name/icon.
    Only attached if the account has uploaded a profile photo — LINE's
    sender.iconUrl must be a real reachable https image."""
    if not current_user.get("photo_media_id"):
        return None
    icon_url = str(request.base_url).rstrip("/") + f"/media/{current_user['photo_media_id']}"
    # LINE limits sender.name length; truncate defensively so a long display
    # name never causes the whole send to fail.
    name = (current_user["display_name"] or "")[:20]
    return {"name": name, "iconUrl": icon_url}


# Paths reachable without being logged in. /media/* must stay public because
# LINE's own servers fetch outbound images/files from that URL with no
# browser session, and customers open file-download links directly.
PUBLIC_EXACT_PATHS = {"/webhook", "/register", "/healthz", "/login", "/api/login", "/api/customer-profile"}
PUBLIC_PATH_PREFIXES = ("/media/",)


@app.middleware("http")
async def require_login_middleware(request: Request, call_next):
    path = request.url.path
    if path in PUBLIC_EXACT_PATHS or path.startswith(PUBLIC_PATH_PREFIXES):
        return await call_next(request)
    if not request.session.get("user_id"):
        if path.startswith("/api/"):
            return JSONResponse({"detail": "unauthorized"}, status_code=401)
        return RedirectResponse(url="/login")
    return await call_next(request)


# Added last so it wraps *outside* the middleware above — Starlette runs the
# most-recently-added middleware first, so this populates request.session
# before require_login_middleware reads it.
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY, session_cookie="line_oa_session", max_age=60 * 60 * 24 * 30)


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
    sender_user_id: int | None = None,
    sender_name: str | None = None,
):
    conn = get_pool().getconn()
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO messages (user_id, display_name, direction, msg_type, text, media_id, created_at, line_message_id, sender_user_id, sender_name)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (line_message_id) WHERE line_message_id IS NOT NULL DO NOTHING
                """,
                (
                    user_id, display_name, direction, msg_type, text, media_id,
                    time.time(), line_message_id, sender_user_id, sender_name,
                ),
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
def list_conversations(current_user: dict = Depends(get_current_user)):
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
async def assign_conversation(user_id: str, request: Request, current_user: dict = Depends(get_current_user)):
    body = await request.json()
    assigned_to = body.get("assigned_to")  # an admin_users id (as string), or null to unassign
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
def get_messages(user_id: str, current_user: dict = Depends(get_current_user)):
    conn = get_pool().getconn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT direction, msg_type, text, media_id, created_at, sender_name
                FROM messages WHERE user_id = %s ORDER BY created_at ASC
                """,
                (user_id,),
            )
            return [dict(r) for r in cur.fetchall()]
    finally:
        get_pool().putconn(conn)


@app.get("/api/conversations/{user_id}/notes")
def get_notes(user_id: str, current_user: dict = Depends(get_current_user)):
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
async def add_note(user_id: str, request: Request, current_user: dict = Depends(get_current_user)):
    body = await request.json()
    text = (body.get("text") or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="text is required")
    conn = get_pool().getconn()
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO notes (user_id, author, text, created_at) VALUES (%s, %s, %s, %s)",
                (user_id, current_user["display_name"], text, time.time()),
            )
    finally:
        get_pool().putconn(conn)
    return {"status": "ok"}


@app.post("/api/conversations/{user_id}/notes-media")
async def add_note_with_media(
    user_id: str,
    text: str = Form(""),
    file: UploadFile = File(...),
    current_user: dict = Depends(get_current_user),
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
                (user_id, current_user["display_name"], text.strip(), media_id, time.time()),
            )
    finally:
        get_pool().putconn(conn)
    return {"status": "ok", "media_id": media_id}


@app.get("/api/conversations/{user_id}/profile")
def get_customer_profile(user_id: str, current_user: dict = Depends(get_current_user)):
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
async def send_registration_link(user_id: str, request: Request, current_user: dict = Depends(get_current_user)):
    if not CHANNEL_ACCESS_TOKEN:
        raise HTTPException(status_code=500, detail="LINE_CHANNEL_ACCESS_TOKEN is not set on the server")
    if not LIFF_ID:
        raise HTTPException(status_code=500, detail="LIFF_ID is not set on the server (see README)")

    link = f"https://liff.line.me/{LIFF_ID}"
    text = f"รบกวนกรอกข้อมูลติดต่อเพิ่มเติมที่ลิงก์นี้ด้วยนะคะ/ครับ 🙏\n{link}"
    message = {"type": "text", "text": text}
    sender = build_sender(current_user, request)
    if sender:
        message["sender"] = sender
    await push_message(user_id, [message])
    save_message(
        user_id, "", "out", "text", text=text,
        sender_user_id=current_user["id"], sender_name=current_user["display_name"],
    )
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


# ---------------------------------------------------------------------------
# Login / logout / current account
# ---------------------------------------------------------------------------

LOGIN_PAGE_HTML = """<!DOCTYPE html>
<html lang="th">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1" />
<title>เข้าสู่ระบบ</title>
<style>
  * { box-sizing: border-box; }
  body { font-family: -apple-system, "Segoe UI", Tahoma, sans-serif; margin: 0; padding: 24px 16px; background: #f4f4f4; display: flex; min-height: 100vh; align-items: center; }
  .card { background: #fff; border-radius: 14px; padding: 26px; max-width: 360px; margin: 0 auto; box-shadow: 0 1px 4px rgba(0,0,0,0.08); width: 100%; }
  h1 { font-size: 19px; margin: 0 0 18px; text-align: center; }
  label { display: block; font-size: 13px; margin: 14px 0 4px; color: #333; }
  input[type=text], input[type=password] {
    width: 100%; padding: 10px 12px; border: 1px solid #ccc; border-radius: 8px; font-size: 15px;
  }
  button { margin-top: 20px; width: 100%; padding: 12px; background: #06c755; color: #fff; border: none; border-radius: 10px; font-size: 15px; cursor: pointer; }
  button:disabled { background: #ccc; }
  #status { margin-top: 12px; font-size: 13px; min-height: 18px; }
  #status.error { color: #c0392b; }
</style>
</head>
<body>
  <div class="card">
    <h1>เข้าสู่ระบบ</h1>
    <form id="login-form">
      <label>ชื่อผู้ใช้</label>
      <input type="text" id="username" autocomplete="username" required />
      <label>รหัสผ่าน</label>
      <input type="password" id="password" autocomplete="current-password" required />
      <div id="status"></div>
      <button type="submit" id="submit-btn">เข้าสู่ระบบ</button>
    </form>
  </div>
<script>
  const form = document.getElementById('login-form');
  const statusEl = document.getElementById('status');
  const submitBtn = document.getElementById('submit-btn');

  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    submitBtn.disabled = true;
    statusEl.textContent = '';
    statusEl.className = '';
    const username = document.getElementById('username').value.trim();
    const password = document.getElementById('password').value;
    try {
      const res = await fetch('/api/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username, password }),
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        statusEl.textContent = err.detail || 'เข้าสู่ระบบไม่สำเร็จ';
        statusEl.className = 'error';
        submitBtn.disabled = false;
        return;
      }
      window.location.href = '/';
    } catch (err) {
      statusEl.textContent = 'เกิดข้อผิดพลาด: ' + err.message;
      statusEl.className = 'error';
      submitBtn.disabled = false;
    }
  });
</script>
</body>
</html>
"""


@app.get("/login", response_class=HTMLResponse)
def login_page():
    return HTMLResponse(content=LOGIN_PAGE_HTML)


@app.post("/api/login")
async def api_login(request: Request):
    body = await request.json()
    username = (body.get("username") or "").strip()
    password = body.get("password") or ""
    user = get_admin_user_by_username(username)
    if not user or not user["active"] or not verify_password(password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="ชื่อผู้ใช้หรือรหัสผ่านไม่ถูกต้อง")
    request.session["user_id"] = user["id"]
    return {"status": "ok"}


@app.post("/api/logout")
async def api_logout(request: Request):
    request.session.clear()
    return {"status": "ok"}


@app.get("/api/me")
def api_me(request: Request, current_user: dict = Depends(get_current_user)):
    photo_url = None
    if current_user.get("photo_media_id"):
        photo_url = str(request.base_url).rstrip("/") + f"/media/{current_user['photo_media_id']}"
    return {
        "id": current_user["id"],
        "username": current_user["username"],
        "display_name": current_user["display_name"],
        "color": current_user["color"],
        "role": current_user["role"],
        "photo_url": photo_url,
    }


@app.post("/api/me/photo")
async def upload_my_photo(file: UploadFile = File(...), current_user: dict = Depends(get_current_user)):
    """Self-service profile photo upload. Used as the LINE sender.iconUrl so
    customers see this account's real photo on messages it sends."""
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="empty file")
    content_type = file.content_type or "application/octet-stream"
    if not content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="ต้องเป็นไฟล์รูปภาพเท่านั้น")
    media_id = save_media(content_type, file.filename, data)
    conn = get_pool().getconn()
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE admin_users SET photo_media_id = %s WHERE id = %s",
                (media_id, current_user["id"]),
            )
    finally:
        get_pool().putconn(conn)
    return {"status": "ok"}


@app.post("/api/me/change-password")
async def change_my_password(request: Request, current_user: dict = Depends(get_current_user)):
    body = await request.json()
    current_password = body.get("current_password") or ""
    new_password = body.get("new_password") or ""
    if len(new_password) < 6:
        raise HTTPException(status_code=400, detail="รหัสผ่านใหม่ต้องมีอย่างน้อย 6 ตัวอักษร")
    if not verify_password(current_password, current_user["password_hash"]):
        raise HTTPException(status_code=400, detail="รหัสผ่านเดิมไม่ถูกต้อง")
    conn = get_pool().getconn()
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE admin_users SET password_hash = %s WHERE id = %s",
                (hash_password(new_password), current_user["id"]),
            )
    finally:
        get_pool().putconn(conn)
    return {"status": "ok"}


@app.get("/api/team")
def get_team(current_user: dict = Depends(get_current_user)):
    """Active accounts, for the assign-conversation dropdown. Any logged-in
    account can see who else is on the team, but only admins can manage
    accounts (see /api/admin/users below)."""
    return list_active_admin_users()


# ---------------------------------------------------------------------------
# Admin: manage accounts
# ---------------------------------------------------------------------------

@app.get("/api/admin/users")
def admin_list_users(current_user: dict = Depends(require_admin)):
    return list_all_admin_users()


@app.post("/api/admin/users")
async def admin_create_user(request: Request, current_user: dict = Depends(require_admin)):
    body = await request.json()
    username = (body.get("username") or "").strip()
    password = body.get("password") or ""
    display_name = (body.get("display_name") or "").strip()
    color = (body.get("color") or "#607D8B").strip()
    role = body.get("role") if body.get("role") in ("admin", "sales") else "sales"

    if not username or not password or not display_name:
        raise HTTPException(status_code=400, detail="username, password, display_name are required")
    if len(password) < 6:
        raise HTTPException(status_code=400, detail="รหัสผ่านต้องมีอย่างน้อย 6 ตัวอักษร")

    conn = get_pool().getconn()
    try:
        with conn, conn.cursor() as cur:
            try:
                cur.execute(
                    """
                    INSERT INTO admin_users (username, password_hash, display_name, color, role, active, created_at)
                    VALUES (%s, %s, %s, %s, %s, TRUE, %s)
                    """,
                    (username, hash_password(password), display_name, color, role, time.time()),
                )
            except psycopg2.IntegrityError:
                raise HTTPException(status_code=400, detail="username นี้มีอยู่แล้ว")
    finally:
        get_pool().putconn(conn)
    return {"status": "ok"}


@app.post("/api/admin/users/{target_id}/deactivate")
def admin_deactivate_user(target_id: int, current_user: dict = Depends(require_admin)):
    if target_id == current_user["id"]:
        raise HTTPException(status_code=400, detail="ปิดการใช้งานบัญชีตัวเองไม่ได้")
    conn = get_pool().getconn()
    try:
        with conn, conn.cursor() as cur:
            cur.execute("UPDATE admin_users SET active = FALSE WHERE id = %s", (target_id,))
    finally:
        get_pool().putconn(conn)
    return {"status": "ok"}


@app.post("/api/admin/users/{target_id}/activate")
def admin_activate_user(target_id: int, current_user: dict = Depends(require_admin)):
    conn = get_pool().getconn()
    try:
        with conn, conn.cursor() as cur:
            cur.execute("UPDATE admin_users SET active = TRUE WHERE id = %s", (target_id,))
    finally:
        get_pool().putconn(conn)
    return {"status": "ok"}


@app.post("/api/admin/users/{target_id}/reset-password")
async def admin_reset_password(target_id: int, request: Request, current_user: dict = Depends(require_admin)):
    body = await request.json()
    new_password = body.get("new_password") or ""
    if len(new_password) < 6:
        raise HTTPException(status_code=400, detail="รหัสผ่านต้องมีอย่างน้อย 6 ตัวอักษร")
    conn = get_pool().getconn()
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE admin_users SET password_hash = %s WHERE id = %s",
                (hash_password(new_password), target_id),
            )
    finally:
        get_pool().putconn(conn)
    return {"status": "ok"}


ADMIN_PAGE_HTML = """<!DOCTYPE html>
<html lang="th">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1" />
<title>จัดการบัญชี</title>
<style>
  * { box-sizing: border-box; }
  body { font-family: -apple-system, "Segoe UI", Tahoma, sans-serif; margin: 0; padding: 20px 16px 60px; background: #f4f4f4; }
  .wrap { max-width: 640px; margin: 0 auto; }
  a.back { display: inline-block; margin-bottom: 14px; color: #06834a; text-decoration: none; font-size: 14px; }
  h1 { font-size: 19px; margin: 0 0 16px; }
  .card { background: #fff; border-radius: 12px; padding: 16px; margin-bottom: 18px; box-shadow: 0 1px 4px rgba(0,0,0,0.06); }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th, td { text-align: left; padding: 8px 6px; border-bottom: 1px solid #eee; }
  .badge { display: inline-block; padding: 2px 8px; border-radius: 10px; font-size: 11px; color: #fff; }
  .badge.inactive { background: #bbb; }
  .role-tag { font-size: 11px; color: #888; }
  button { padding: 6px 10px; border: none; border-radius: 6px; cursor: pointer; font-size: 12px; margin-right: 4px; }
  .btn-deactivate { background: #ffe0e0; color: #c0392b; }
  .btn-activate { background: #e0f5e0; color: #06834a; }
  .btn-reset { background: #eee; color: #333; }
  label { display: block; font-size: 13px; margin: 12px 0 4px; color: #333; }
  input[type=text], input[type=password], select {
    width: 100%; padding: 9px 10px; border: 1px solid #ccc; border-radius: 8px; font-size: 14px;
  }
  .row2 { display: flex; gap: 10px; }
  .row2 > div { flex: 1; }
  #add-btn { margin-top: 16px; padding: 10px 18px; background: #06c755; color: #fff; border-radius: 8px; font-size: 14px; }
  #add-status { margin-top: 10px; font-size: 13px; }
  #add-status.error { color: #c0392b; }
  #add-status.success { color: #06834a; }
</style>
</head>
<body>
<div class="wrap">
  <a class="back" href="/">‹ กลับหน้าแชท</a>
  <h1>จัดการบัญชี</h1>

  <div class="card">
    <table id="users-table">
      <thead><tr><th>ชื่อที่แสดง</th><th>Username</th><th>สิทธิ์</th><th>สถานะ</th><th>จัดการ</th></tr></thead>
      <tbody id="users-tbody"></tbody>
    </table>
  </div>

  <div class="card">
    <strong>เพิ่มบัญชีใหม่</strong>
    <form id="add-form">
      <div class="row2">
        <div><label>ชื่อที่แสดง</label><input type="text" id="new-display-name" required /></div>
        <div><label>สี badge</label><input type="text" id="new-color" value="#2196F3" /></div>
      </div>
      <label>Username</label>
      <input type="text" id="new-username" required />
      <label>รหัสผ่าน (อย่างน้อย 6 ตัวอักษร)</label>
      <input type="password" id="new-password" required />
      <label>สิทธิ์</label>
      <select id="new-role">
        <option value="sales">sales (พนักงานทั่วไป)</option>
        <option value="admin">admin (จัดการบัญชีได้)</option>
      </select>
      <button type="submit" id="add-btn">เพิ่มบัญชี</button>
      <div id="add-status"></div>
    </form>
  </div>
</div>

<script>
function escapeHtml(str) {
  if (str == null) return '';
  return String(str).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

async function loadUsers() {
  const res = await fetch('/api/admin/users');
  if (res.status === 403) {
    document.querySelector('.wrap').innerHTML = '<p>คุณไม่มีสิทธิ์เข้าหน้านี้</p>';
    return;
  }
  const users = await res.json();
  const tbody = document.getElementById('users-tbody');
  tbody.innerHTML = users.map(u => `
    <tr>
      <td><span class="badge ${u.active ? '' : 'inactive'}" style="background:${u.active ? escapeHtml(u.color) : '#bbb'}">${escapeHtml(u.display_name)}</span></td>
      <td>${escapeHtml(u.username)}</td>
      <td><span class="role-tag">${escapeHtml(u.role)}</span></td>
      <td>${u.active ? 'ใช้งานอยู่' : 'ปิดใช้งาน'}</td>
      <td>
        ${u.active
          ? `<button class="btn-deactivate" data-id="${u.id}" data-action="deactivate">ปิดใช้งาน</button>`
          : `<button class="btn-activate" data-id="${u.id}" data-action="activate">เปิดใช้งาน</button>`}
        <button class="btn-reset" data-id="${u.id}" data-action="reset">รีเซ็ตรหัสผ่าน</button>
      </td>
    </tr>
  `).join('');

  tbody.querySelectorAll('button').forEach(btn => {
    btn.addEventListener('click', () => handleAction(btn.dataset.id, btn.dataset.action));
  });
}

async function handleAction(id, action) {
  if (action === 'reset') {
    const newPassword = prompt('ตั้งรหัสผ่านใหม่ (อย่างน้อย 6 ตัวอักษร):');
    if (!newPassword) return;
    const res = await fetch(`/api/admin/users/${id}/reset-password`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ new_password: newPassword }),
    });
    if (!res.ok) { alert('ทำไม่สำเร็จ: ' + await res.text()); return; }
    alert('รีเซ็ตรหัสผ่านแล้ว');
    return;
  }
  if (action === 'deactivate' && !confirm('ปิดใช้งานบัญชีนี้?')) return;
  const res = await fetch(`/api/admin/users/${id}/${action}`, { method: 'POST' });
  if (!res.ok) { alert('ทำไม่สำเร็จ: ' + await res.text()); return; }
  loadUsers();
}

document.getElementById('add-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const statusEl = document.getElementById('add-status');
  statusEl.textContent = '';
  statusEl.className = '';
  const payload = {
    username: document.getElementById('new-username').value.trim(),
    password: document.getElementById('new-password').value,
    display_name: document.getElementById('new-display-name').value.trim(),
    color: document.getElementById('new-color').value.trim() || '#607D8B',
    role: document.getElementById('new-role').value,
  };
  const res = await fetch('/api/admin/users', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    statusEl.textContent = err.detail || 'เพิ่มบัญชีไม่สำเร็จ';
    statusEl.className = 'error';
    return;
  }
  statusEl.textContent = 'เพิ่มบัญชีเรียบร้อย';
  statusEl.className = 'success';
  document.getElementById('add-form').reset();
  document.getElementById('new-color').value = '#2196F3';
  loadUsers();
});

loadUsers();
</script>
</body>
</html>
"""


@app.get("/admin", response_class=HTMLResponse)
def admin_page(request: Request):
    user_id = request.session.get("user_id")
    user = get_admin_user_by_id(user_id) if user_id else None
    if not user or not user["active"]:
        return RedirectResponse(url="/login")
    if user["role"] != "admin":
        return HTMLResponse(content="<p style='font-family:sans-serif;padding:24px'>คุณไม่มีสิทธิ์เข้าหน้านี้</p>", status_code=403)
    return HTMLResponse(content=ADMIN_PAGE_HTML)


@app.post("/api/conversations/{user_id}/reply")
async def reply_to_user(user_id: str, request: Request, current_user: dict = Depends(get_current_user)):
    body = await request.json()
    text = (body.get("text") or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="text is required")

    if not CHANNEL_ACCESS_TOKEN:
        raise HTTPException(status_code=500, detail="LINE_CHANNEL_ACCESS_TOKEN is not set on the server")

    message = {"type": "text", "text": text}
    sender = build_sender(current_user, request)
    if sender:
        message["sender"] = sender
    await push_message(user_id, [message])
    save_message(
        user_id, "", "out", "text", text=text,
        sender_user_id=current_user["id"], sender_name=current_user["display_name"],
    )
    return {"status": "sent"}


@app.post("/api/conversations/{user_id}/reply-media")
async def reply_with_media(
    request: Request,
    user_id: str,
    file: UploadFile = File(...),
    kind: str = Form(...),
    current_user: dict = Depends(get_current_user),
):
    if not CHANNEL_ACCESS_TOKEN:
        raise HTTPException(status_code=500, detail="LINE_CHANNEL_ACCESS_TOKEN is not set on the server")

    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="empty file")

    content_type = file.content_type or "application/octet-stream"
    media_id = save_media(content_type, file.filename, data)
    public_url = str(request.base_url).rstrip("/") + f"/media/{media_id}"
    sender = build_sender(current_user, request)

    if kind == "image":
        if not content_type.startswith("image/"):
            raise HTTPException(status_code=400, detail="ไฟล์นี้ไม่ใช่รูปภาพ")
        message = {"type": "image", "originalContentUrl": public_url, "previewImageUrl": public_url}
        if sender:
            message["sender"] = sender
        await push_message(user_id, [message])
        save_message(
            user_id, "", "out", "image", text=file.filename, media_id=media_id,
            sender_user_id=current_user["id"], sender_name=current_user["display_name"],
        )
    else:
        # LINE's Messaging API has no generic "send file" message type, so we
        # send a text message containing a link the customer can tap to download.
        message = {"type": "text", "text": f"📎 {file.filename}\n{public_url}"}
        if sender:
            message["sender"] = sender
        await push_message(user_id, [message])
        save_message(
            user_id, "", "out", "file", text=file.filename, media_id=media_id,
            sender_user_id=current_user["id"], sender_name=current_user["display_name"],
        )

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
