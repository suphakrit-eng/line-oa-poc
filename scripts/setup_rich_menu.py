"""
Create/update the "กรอกข้อมูลติดต่อ" (fill in contact info) Rich Menu on a
LINE Official Account, and set it as the default menu for everyone on that OA.

Run this again any time you want to replace the rich menu image or change
the LIFF link it opens (e.g. after redesigning richmenu/contact_form.png).

Usage:
    cd line_oa_poc
    python3 scripts/setup_rich_menu.py                # default channel
    python3 scripts/setup_rich_menu.py peacehome       # a specific channel key
    python3 scripts/setup_rich_menu.py --image richmenu/other.png projectx

Reads channel config the same way app.py does:
  - LINE_CHANNELS_JSON in .env (a JSON array — see README "เพิ่ม LINE OA
    โครงการใหม่") for any channel key other than the default one, or
  - the legacy LINE_CHANNEL_ACCESS_TOKEN / LINE_CHANNEL_SECRET / LIFF_ID /
    LINE_CHANNEL_KEY / LINE_CHANNEL_NAME vars for the original OA.
"""
import json
import os
import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv

BASE_DIR = Path(__file__).parent.parent
load_dotenv(BASE_DIR / ".env")

DEFAULT_CHANNEL_KEY = os.getenv("LINE_CHANNEL_KEY", "default")
DEFAULT_CHANNEL_NAME = os.getenv("LINE_CHANNEL_NAME", "โครงการหลัก")

RICH_MENU_NAME = "กรอกข้อมูลติดต่อ"
CHAT_BAR_TEXT = "กรอกข้อมูล"  # label on the little tab customers tap to open/close the menu


def load_channels() -> dict:
    channels = {}
    raw = os.getenv("LINE_CHANNELS_JSON", "").strip()
    if raw:
        for c in json.loads(raw):
            channels[c["key"]] = {
                "key": c["key"],
                "name": c.get("name", c["key"]),
                "access_token": c["access_token"],
                "secret": c.get("secret", ""),
                "liff_id": c.get("liff_id", ""),
            }
    legacy_token = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")
    if legacy_token and DEFAULT_CHANNEL_KEY not in channels:
        channels[DEFAULT_CHANNEL_KEY] = {
            "key": DEFAULT_CHANNEL_KEY,
            "name": DEFAULT_CHANNEL_NAME,
            "access_token": legacy_token,
            "secret": os.getenv("LINE_CHANNEL_SECRET", ""),
            "liff_id": os.getenv("LIFF_ID", ""),
        }
    return channels


def main():
    args = sys.argv[1:]
    image_path = BASE_DIR / "richmenu" / "contact_form.png"
    if "--image" in args:
        i = args.index("--image")
        image_path = Path(args[i + 1])
        del args[i:i + 2]

    channels = load_channels()
    channel_key = args[0] if args else DEFAULT_CHANNEL_KEY
    if channel_key not in channels:
        available = ", ".join(channels) or "(none configured)"
        sys.exit(f"Unknown channel key '{channel_key}'. Configured channels: {available}")
    ch = channels[channel_key]

    if not ch["access_token"]:
        sys.exit(f"Channel '{channel_key}' has no access_token configured in .env")
    if not ch["liff_id"]:
        sys.exit(f"Channel '{channel_key}' has no liff_id configured in .env (create the LIFF app first — see README)")
    if not image_path.exists():
        sys.exit(f"Rich menu image not found at {image_path}")

    print(f"ตั้งค่า Rich Menu ให้ช่องทาง: {ch['name']} (key={channel_key})")

    headers = {"Authorization": f"Bearer {ch['access_token']}"}
    liff_url = f"https://liff.line.me/{ch['liff_id']}"

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
    with open(image_path, "rb") as f:
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

    print(f"\nเสร็จแล้ว! ลูกค้าของ {ch['name']} จะเห็น Rich Menu นี้ในแชททันที (ลิงก์เปิดฟอร์ม: {liff_url})")


if __name__ == "__main__":
    main()
