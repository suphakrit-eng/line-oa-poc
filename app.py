import base64
import hashlib
import hmac
import json
import os
import sqlite3
import time
from pathlib import Path

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

load_dotenv()

CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")
CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET", "")

BASE_DIR = Path(__file__).parent
DB_PATH = BASE_DIR / "poc.db"

app = FastAPI(title="LINE OA External Chat POC")


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT NOT NULL,
            display_name TEXT,
            direction TEXT NOT NULL, -- 'in' (from customer) or 'out' (from agent)
            text TEXT NOT NULL,
            created_at REAL NOT NULL
        )
        """
    )
    conn.commit()
    conn.close()


init_db()


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def verify_signature(body: bytes, signature: str) -> bool:
    """Verify LINE webhook signature. If CHANNEL_SECRET is not set (local dev
    before LINE credentials exist), skip verification so the POC still runs."""
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


def save_message(user_id: str, display_name: str, direction: str, text: str):
    conn = get_conn()
    conn.execute(
        "INSERT INTO messages (user_id, display_name, direction, text, created_at) VALUES (?, ?, ?, ?, ?)",
        (user_id, display_name, direction, text, time.time()),
    )
    conn.commit()
    conn.close()


def get_known_display_name(user_id: str) -> str:
    conn = get_conn()
    row = conn.execute(
        "SELECT display_name FROM messages WHERE user_id = ? AND display_name IS NOT NULL AND display_name != '' ORDER BY created_at DESC LIMIT 1",
        (user_id,),
    ).fetchone()
    conn.close()
    return row["display_name"] if row else user_id


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
        if event.get("type") == "message" and event.get("message", {}).get("type") == "text":
            user_id = event["source"]["userId"]
            text = event["message"]["text"]
            display_name = await fetch_display_name(user_id)
            save_message(user_id, display_name, "in", text)

    return JSONResponse({"status": "ok"})


@app.get("/api/conversations")
def list_conversations():
    conn = get_conn()
    rows = conn.execute(
        """
        SELECT user_id, MAX(created_at) as last_at
        FROM messages
        GROUP BY user_id
        ORDER BY last_at DESC
        """
    ).fetchall()
    conn.close()
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
    conn = get_conn()
    rows = conn.execute(
        "SELECT direction, text, created_at FROM messages WHERE user_id = ? ORDER BY created_at ASC",
        (user_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.post("/api/conversations/{user_id}/reply")
async def reply_to_user(user_id: str, request: Request):
    body = await request.json()
    text = (body.get("text") or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="text is required")

    if not CHANNEL_ACCESS_TOKEN:
        raise HTTPException(
            status_code=500,
            detail="LINE_CHANNEL_ACCESS_TOKEN is not set on the server",
        )

    url = "https://api.line.me/v2/bot/message/push"
    headers = {
        "Authorization": f"Bearer {CHANNEL_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {"to": user_id, "messages": [{"type": "text", "text": text}]}

    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(url, headers=headers, json=payload)

    if resp.status_code != 200:
        raise HTTPException(status_code=resp.status_code, detail=resp.text)

    save_message(user_id, "", "out", text)
    return {"status": "sent"}


# Serve the agent UI (static/index.html) for everything else.
app.mount("/", StaticFiles(directory=BASE_DIR / "static", html=True), name="static")
