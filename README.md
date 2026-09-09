# Speexx Auto-Solver with AI & Playwright

โปรแกรมช่วยทำแบบฝึกหัด Speexx อัตโนมัติ รองรับทั้งการต่อผ่าน **AI Proxy (Gemini Flash)** เพื่อความเร็วและความแม่นยำสูงสุด หรือ **Local LLM (LM Studio / Ollama)** ทำงานออฟไลน์ในเครื่อง พร้อมระบบควบคุมหน้าต่าง Chromium แบบมองเห็นได้จริง (Visible Browser) และระบบสุ่มหน่วงเวลาเลียนแบบมนุษย์ (Human Pacing & Anti-Detection)

---

## 🛠️ โหมดการเชื่อมต่อ AI (เลือกใช้งานแบบใดแบบหนึ่ง)

### ทางเลือกที่ 1: ใช้งานร่วมกับ AI Proxy (Antigravity Proxy -> Gemini Flash) ⚡ [แนะนำ]
เหมาะสำหรับคนที่ต้องการความแม่นยำสูง คิดเร็ว และไม่กินทรัพยากรการ์ดจอในเครื่อง:
1. คัดลอกไฟล์ sample.env เป็น .env
2. กำหนดค่าใน .env:
```env
# ชี้ endpoint ไปที่ Proxy (พอร์ตมาตรฐาน 8000)
LM_STUDIO_URL=http://localhost:8000/v1
LM_STUDIO_MODEL=speexx-solver
PROXY_API_KEY=speexx-proxy-key

# ใส่ Gemini API Key ของคุณ (หากใช้ Proxy ที่ส่งต่อไปยัง Google Gemini)
GEMINI_API_KEY=your_gemini_api_key_here
```
3. **การทำงานร่วมกับ Proxy:**
   - โปรแกรมมีระบบ **Auto-Startup**: เมื่อรัน main.py หรือเลือกเมนูทดสอบ หากตรวจพบว่า Proxy ยังไม่ทำงาน ระบบจะพยายามเริ่มต้น Proxy ให้โดยอัตโนมัติ
   - หรือหากต้องการรัน Proxy เองล่วงหน้า สามารถสั่งรัน Proxy server ให้อยู่บน http://localhost:8000

---

### ทางเลือกที่ 2: ใช้งานผ่าน LM Studio (Local LLM ในเครื่อง) 🖥️
เหมาะสำหรับคนที่ต้องการรันออฟไลน์ ไม่ต้องการใช้ API Key หรืออินเทอร์เน็ตภายนอก:
1. ดาวน์โหลดและติดตั้ง **LM Studio**
2. โหลดโมเดลภาษาที่แนะนำ เช่น Qwen2.5-7b-instruct, Gemma-2-9b-it หรือโมเดล Instruction อื่นๆ
3. กดปุ่ม **Start Server** ในหน้า Developer ของ LM Studio (พอร์ตมาตรฐานคือ 1234)
4. ตั้งค่าใน .env:
```env
LM_STUDIO_URL=http://localhost:1234/v1
LM_STUDIO_MODEL=local-model
PROXY_API_KEY=lm-studio
```

---

## 🚀 วิธีการติดตั้งและการเริ่มใช้งาน

### วิธีที่ 1: ดับเบิ้ลคลิก `run.bat` (ง่ายที่สุด)
1. ดับเบิ้ลคลิกไฟล์ `run.bat`
2. ระบบจะทำการตรวจสอบและติดตั้งไลบรารีจาก `requirements.txt` และติดตั้งเบราว์เซอร์ Chromium ให้อัตโนมัติ
3. หน้าต่างเมนู CLI จะเปิดขึ้นมาพร้อมใช้งาน

### วิธีที่ 2: รันผ่าน Terminal / Command Prompt
```bash
# 1. ติดตั้งไลบรารีที่จำเป็น
pip install -r requirements.txt

# 2. ติดตั้งเบราว์เซอร์ Playwright
python -m playwright install chromium

# 3. เริ่มต้นโปรแกรม
python main.py
```

---

## 🎯 ขั้นตอนการรันทำแบบฝึกหัด

1. **เลือกเมนู 1 (Test AI / Proxy Connection)**: 
   - เพื่อตรวจสอบว่า Proxy หรือ LM Studio พร้อมตอบคำถามภาษาอังกฤษ
2. **เลือกเมนู 2 (Launch Visible Browser)**: 
   - หน้าต่าง Chromium จะเปิดขึ้นมาบนหน้าจอ
   - ล็อกอินเข้าสู่ระบบ Speexx Portal ในหน้านี้ตามปกติ (หรือตั้งค่ารหัสผ่านใน .env เพื่อ Auto-Login)
   - เปิดไปยังหน้าแบบฝึกหัดหรือ Packet ที่ต้องการทำ
3. **เลือกเมนู 3 หรือ 4**:
   - 3: **Solve Single Exercise** - สั่งแก้ทีละ 1 ข้อ เหมาะสำหรับตรวจสอบความแม่นยำ
   - 4: **Continuous Autonomous Loop** - รันอัตโนมัติต่อเนื่องยาวๆ ข้ามข้อ และกดส่งเอง พร้อมระบบสุ่มหน่วงเวลาป้องกันการตรวจจับ
4. **หากต้องการล้างแคชเบราว์เซอร์**: สามารถดับเบิ้ลคลิก clean_cache.bat ได้ตลอดเวลา

---

## ⚙️ คุณสมบัติการควบคุมความเร็ว (Anti-Detection / Human Pacing)

คุณสามารถปรับแต่งพฤติกรรมการพิมพ์และการหน่วงเวลาเพื่อความปลอดภัยได้ในเมนู 5 หรือแก้ไขใน .env / config.py:
- MIN_THINK_TIME / MAX_THINK_TIME: เวลาอ่านโจทย์และคิดก่อนเริ่มตอบ (วินาที)
- SUB_ITEM_DELAY_MIN / SUB_ITEM_DELAY_MAX: การหน่วงเวลาระหว่างแต่ละข้อย่อย (วินาที)
- REVIEW_DELAY_MIN / REVIEW_DELAY_MAX: เวลาตรวจทานคำตอบก่อนกดส่ง Correction (วินาที)
- MIN_TYPING_DELAY_MS / MAX_TYPING_DELAY_MS: ความเร็วในการพิมพ์ทีละตัวอักษร (มิลลิวินาที)
- PAUSE_BETWEEN_QUESTIONS_MIN / PAUSE_BETWEEN_QUESTIONS_MAX: ระยะเวลาพักก่อนกด Next ไปยังข้อถัดไป (วินาที)
