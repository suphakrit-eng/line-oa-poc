# LINE OA External Chat POC

โปรเจกต์ทดสอบแนวคิด: ลูกค้าทัก LINE OA → ข้อความเข้าระบบภายนอก (หน้า UI นี้) → agent พิมพ์ตอบในหน้า UI → ข้อความถูกส่งกลับไปที่ลูกค้าทาง LINE จริง

โครงสร้าง: FastAPI (`app.py`) เก็บข้อความใน SQLite (`poc.db`) และเสิร์ฟหน้า UI ง่ายๆ ที่ `static/index.html`

---

## ขั้นตอนที่ 1: สร้าง LINE Messaging API Channel

1. เข้า https://developers.line.biz/console/ แล้ว login ด้วยบัญชี LINE
2. สร้าง **Provider** ใหม่ (ถ้ายังไม่มี) — ใส่ชื่อบริษัท/โปรเจกต์
3. ในหน้า Provider กด **Create a new channel** → เลือก **Messaging API**
4. กรอกข้อมูลช่อง (ชื่อบอท, ไอคอน, หมวดหมู่) แล้วสร้าง
5. เข้าไปที่ channel ที่สร้าง → แท็บ **Messaging API**:
   - เลื่อนลงไปที่ **Channel access token** กด **Issue** เพื่อออก token แบบ long-lived → คัดลอกเก็บไว้
   - แท็บ **Basic settings** → คัดลอก **Channel secret**
6. ในหน้า Messaging API เดียวกัน **สแกน QR code** ด้วย LINE บนมือถือ เพื่อเพิ่มบอทเป็นเพื่อน (ใช้ทดสอบ)
7. ปิดฟีเจอร์ auto-reply ที่ชนกับ POC: เข้า https://manager.line.biz เลือกบัญชีนี้ → **Settings > Response settings** → ปิด **Greeting messages** และ **Auto-reply messages**, เปิด **Webhooks**

---

## ขั้นตอนที่ 2: รันในเครื่องตัวเอง (ทดสอบก่อน deploy)

```bash
cd line_oa_poc
python3 -m venv venv && source venv/bin/activate   # หรือข้ามถ้าไม่อยากใช้ venv
pip install -r requirements.txt

cp .env.example .env
# แก้ .env ใส่ LINE_CHANNEL_ACCESS_TOKEN และ LINE_CHANNEL_SECRET ที่ได้จากขั้นตอนที่ 1

uvicorn app:app --reload --port 8000
```

เปิด http://localhost:8000 จะเห็นหน้า UI (ยังไม่มีข้อความ เพราะ LINE ยังยิง webhook มาที่ localhost ไม่ได้)

ถ้าต้องการทดสอบในเครื่องจริงกับ LINE ต้องเปิด public URL ชั่วคราวด้วย `ngrok http 8000` แล้วเอา URL ที่ได้ (เช่น `https://xxxx.ngrok-free.app/webhook`) ไปใส่ใน Messaging API tab > **Webhook URL** แล้วกด **Verify**

---

## ขั้นตอนที่ 3: Deploy ขึ้นออนไลน์ฟรี (Render.com)

Render มี free web service tier ไม่ต้องใส่บัตรเครดิต (มีข้อจำกัด: sleep เมื่อไม่มีคนใช้ ~15 นาที ตื่นมาช้าประมาณ 30-50 วิ — ยอมรับได้สำหรับ POC/demo)

1. Push โฟลเดอร์ `line_oa_poc` นี้ขึ้น GitHub repo (public หรือ private ก็ได้)
2. ไปที่ https://render.com → สมัคร/login (ผูกกับ GitHub ได้เลย)
3. กด **New +** → **Web Service** → เลือก repo ที่ push ไว้
4. Render จะอ่าน `render.yaml` ในโปรเจกต์อัตโนมัติ (Build command / Start command ตั้งไว้ให้แล้ว) หรือถ้าไม่ auto-detect ให้กรอกเอง:
   - Build Command: `pip install -r requirements.txt`
   - Start Command: `uvicorn app:app --host 0.0.0.0 --port $PORT`
5. เลือก plan **Free**
6. ใส่ Environment Variables:
   - `LINE_CHANNEL_ACCESS_TOKEN` = token จากขั้นตอนที่ 1
   - `LINE_CHANNEL_SECRET` = secret จากขั้นตอนที่ 1
7. กด **Create Web Service** รอ build เสร็จ จะได้ URL แบบ `https://line-oa-poc-xxxx.onrender.com`
8. กลับไปที่ LINE Developers Console → Messaging API tab → **Webhook URL** ใส่ `https://line-oa-poc-xxxx.onrender.com/webhook` → กด **Verify** → เปิด toggle **Use webhook**
9. เปิด `https://line-oa-poc-xxxx.onrender.com` ในเบราว์เซอร์ — นี่คือหน้า UI ที่ส่งให้คนอื่นทดสอบได้เลย (ยังไม่มี login/สิทธิ์ ใครมีลิงก์เห็นแชททั้งหมด — พอสำหรับ POC แต่ไม่ควรใช้ข้อมูลลูกค้าจริง)

ทดสอบ: ทักบอทผ่าน LINE จากมือถือ → ข้อความจะโผล่ในหน้า UI ภายในไม่กี่วินาที → พิมพ์ตอบในหน้า UI → ข้อความไปเด้งที่ LINE จริงของลูกค้า

---

## ข้อจำกัดของ POC นี้ (ไม่เหมาะกับ production)

- ไม่มีระบบ login/สิทธิ์ผู้ใช้งาน — ใครมีลิงก์เห็นแชททุกคน
- ใช้ SQLite ไฟล์เดียว บน Render free tier disk เป็น ephemeral (ข้อมูลอาจหายเมื่อ redeploy) — พอสำหรับ demo ไม่พอสำหรับเก็บข้อมูลจริง
- ตอบกลับผ่าน Push API ล้วน (ไม่ใช้ reply token) มีโควต้าข้อความฟรีต่อเดือนจำกัด ใช้เกินมีค่าใช้จ่าย เช็คโควต้าได้ที่ LINE OA Manager
- ไม่รองรับรูปภาพ/ไฟล์/สติกเกอร์ รองรับข้อความตัวอักษรอย่างเดียว
- ไม่มี retry/queue เมื่อ LINE API ล้มเหลว
