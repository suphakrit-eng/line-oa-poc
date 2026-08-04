"""
Create/update the "กรอกข้อมูลติดต่อ" (fill in contact info) Rich Menu on the
LINE Official Account, and set it as the default menu for everyone.

Run this again any time you want to replace the rich menu image or change
the LIFF link it opens (e.g. after redesigning richmenu/contact_form.png).

Usage:
    cd line_oa_poc
    python3 scripts/setup_rich_menu.py

Requires LINE_CHANNEL_ACCESS_TOKEN and LIFF_ID to be set in .env (same file
used by app.py).
"""
import os
import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv

BASE_DIR = Path(__file__).parent.parent
load_dotenv(BASE_DIR / ".env")

CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")
LIFF_ID = os.getenv("LIFF_ID", "")
IMAGE_PATH = BASE_DIR / "richmenu" / "contact_form.png"

RICH_MENU_NAME = "กรอกข้อมูลติดต่อ"
CHAT_BAR_TEXT = "กรอกข้อมูล"  # label on the little tab customers tap to open/close the menu


def main():
    if not CHANNEL_ACCESS_TOKEN:
        sys.exit("LINE_CHANNEL_ACCESS_TOKEN is not set in .env")
    if not LIFF_ID:
        sys.exit("LIFF_ID is not set in .env (create the LIFF app first — see README ขั้นตอนที่ 4)")
    if not IMAGE_PATH.exists():
        sys.exit(f"Rich menu image not found at {IMAGE_PATH}")

    headers = {"Authorization": f"Bearer {CHANNEL_ACCESS_TOKEN}"}
    liff_url = f"https://liff.line.me/{LIFF_ID}"

    rich_menu_body = {
        "size": {"width": 2500, "height": 843},
        "selected": True,
        "name": RICH_MENU_NAME,
        "chatBarText": CHAT_BAR_TEXT,
        "areas": [
            {
                "bounds": {"x": 0, "y": 0, "width": 2500, "height": 843},
                "action": {"type": "uri", "uri": liff_url},
            }
        ],
    }

    print("1/4 กำลังลบ rich menu เก่า (ถ้ามี)...")
    existing = httpx.get("https://api.line.me/v2/bot/richmenu/list", headers=headers, timeout=15).json()
    for menu in existing.get("richmenus", []):
        httpx.delete(f"https://api.line.me/v2/bot/richmenu/{menu['richMenuId']}", headers=headers, timeout=15)
        print(f"   ลบ {menu['richMenuId']} ({menu.get('name')})")

    print("2/4 กำลังสร้าง rich menu ใหม่...")
    resp = httpx.post(
        "https://api.line.me/v2/bot/richmenu",
        headers={**headers, "Content-Type": "application/json"},
        json=rich_menu_body,
        timeout=15,
    )
    resp.raise_for_status()
    rich_menu_id = resp.json()["richMenuId"]
    print(f"   richMenuId = {rich_menu_id}")

    print("3/4 กำลังอัปโหลดรูปภาพ...")
    with open(IMAGE_PATH, "rb") as f:
        resp = httpx.post(
            f"https://api-data.line.me/v2/bot/richmenu/{rich_menu_id}/content",
            headers={**headers, "Content-Type": "image/png"},
            content=f.read(),
            timeout=30,
        )
    resp.raise_for_status()

    print("4/4 กำลังตั้งเป็นเมนูเริ่มต้นสำหรับทุกคน...")
    resp = httpx.post(
        f"https://api.line.me/v2/bot/user/all/richmenu/{rich_menu_id}",
        headers={**headers, "Content-Type": "application/json"},
        content=b"",
        timeout=15,
    )
    resp.raise_for_status()

    print("\nเสร็จแล้ว! ลูกค้าจะเห็น Rich Menu นี้ในแชททันที (ลิงก์เปิดฟอร์ม: " + liff_url + ")")


if __name__ == "__main__":
    main()
