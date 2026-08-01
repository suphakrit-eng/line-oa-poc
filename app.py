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
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pathlib import Path

load_dotenv()

CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")
CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET", "")
DATABASE_URL = os.getenv("DATABASE_URL", "")

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
                    created_at DOUBLE PRECISION NOT NULL
                )
                """
            )
    finally:
        get_pool().putconn(conn)


@app.on_event("startup")
def on_startup():
    init_db()


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
):
    conn = get_pool().getconn()
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO messages (user_id, display_name, direction, msg_type, text, media_id, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (user_id, display_name, direction, msg_type, text, media_id, time.time()),
            )
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
        display_name = await fetch_display_name(user_id)

        if msg_type == "text":
            save_message(user_id, display_name, "in", "text", text=message.get("text", ""))

        elif msg_type in ("image", "file", "video", "audio"):
            try:
                data, content_type = await download_line_content(message["id"])
            except Exception:
                save_message(
                    user_id, display_name, "in", "text",
                    text=f"[ไม่สามารถดาวน์โหลด {msg_type} จากลูกค้าได้]",
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
                text=filename, media_id=media_id,
            )

        else:
            save_message(
                user_id, display_name, "in", "text",
                text=f"[ลูกค้าส่ง {msg_type} มา — ยังไม่รองรับการแสดงผลประเภทนี้]",
            )

    return JSONResponse({"status": "ok"})


@app.get("/api/conversations")
def list_conversations():
    conn = get_pool().getconn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT user_id, MAX(created_at) as last_at
                FROM messages
                GROUP BY user_id
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
            }
        )
    return result


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
