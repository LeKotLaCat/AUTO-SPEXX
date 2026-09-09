# Speexx Auto-Solver with Local AI (LM Studio) & Playwright

คู่มือการติดตั้งและใช้งานโปรแกรมช่วยทำแบบฝึกหัด Speexx อัตโนมัติ โดยใช้ AI ในเครื่อง (LM Studio / Local LLM) ร่วมกับ Visible Browser (เปิดหน้าต่าง Chromium ให้ดูสดๆ บนหน้าจอ)

---

## 🛠️ ความต้องการของระบบ (Prerequisites)

1. **LM Studio / Local LLM** 
   - ดาวน์โหลดและติดตั้ง LM Studio
   - โหลดโมเดลภาษา เช่น Gemma-2-9b-it, Qwen2.5-7b-instruct หรือโมเดลที่คุณต้องการ
   - เปิด Local Server ใน LM Studio (ปุ่ม **Start Server** อยู่ที่ http://localhost:1234 หรือ http://localhost:8000/v1)
2. **Python 3.10+**

---

## 🚀 วิธีการใช้งาน

### วิธีที่ 1: ดับเบิ้ลคลิก 
un.bat
1. ดับเบิ้ลคลิกไฟล์ 
un.bat
2. ระบบจะทำการตรวจสอบและติดตั้ง dependencies จาก 
equirements.txt และ playwright chromium ให้อัตโนมัติ
3. หน้าต่างเมนู CLI จะเปิดขึ้นมา

### วิธีที่ 2: รันผ่าน Terminal / Command Prompt
`ash
pip install -r requirements.txt
python -m playwright install chromium
python main.py
`

---

## 🎯 ขั้นตอนการรันทำแบบฝึกหัด

1. **เลือกเมนู 1 (Test LM Studio Connection)**: 
   - เพื่อตรวจสอบว่า Local LLM Server เปิดเรียบร้อยแล้วและ AI สามารถตอบคำถามภาษาอังกฤษได้
2. **เลือกเมนู 2 (Launch Visible Browser)**: 
   - หน้าต่าง Chromium จะเปิดขึ้นมาบนหน้าจอ
   - ล็อกอินเข้าใช้งาน Speexx Portal ในหน้าต่างนี้ตามปกติ
   - เปิดเข้าไปยังหน้าแบบฝึกหัด/packet ที่ต้องการทำ
3. **เลือกเมนู 3 หรือ 4**:
   - 3: ทำทีละ 1 ข้อเพื่อตรวจสอบการทำงาน
   - 4: รันแบบอัตโนมัติต่อเนื่อง (Auto-Loop) พร้อมระบบสุ่ม Delay หน่วงเวลาอ่านโจทย์เพื่อความเป็นธรรมชาติและป้องกันการตรวจจับ

---

## ⚙️ คุณสมบัติควบคุมความเร็ว (Anti-Detection / Human Pacing)

คุณสามารถปรับแต่งเวลาในการอ่านโจทย์และการพิมพ์ได้ในเมนู 5 หรือแก้ไขใน config.py หรือ .env:
- MIN_THINK_TIME: เวลาอ่านโจทย์ขั้นต่ำ (วินาที)
- MAX_THINK_TIME: เวลาอ่านโจทย์สูงสุด (วินาที)
- SUB_ITEM_DELAY_MIN / SUB_ITEM_DELAY_MAX: หน่วงเวลาระหว่างข้อย่อย (วินาที)
- MIN_TYPING_DELAY_MS / MAX_TYPING_DELAY_MS: หน่วงเวลาการพิมพ์ทีละตัวอักษร (มิลลิวินาที)
