import os
from pathlib import Path
from dotenv import load_dotenv

# Load .env if present
load_dotenv()

BASE_DIR = Path(__file__).parent.resolve()

# LM Studio Settings
LM_STUDIO_URL = os.getenv("LM_STUDIO_URL", "http://localhost:1234/v1")
# Default model name; LM Studio will usually route requests to whatever model is currently loaded in LM Studio if set to local-model
LM_STUDIO_MODEL = os.getenv("LM_STUDIO_MODEL", "local-model")

# Auto-login credentials for Speexx Portal
SPEEXX_EMAIL = os.getenv("SPEEXX_EMAIL", "")
SPEEXX_PASSWORD = os.getenv("SPEEXX_PASSWORD", "")
PROXY_API_KEY = os.getenv("PROXY_API_KEY", "lm-studio")

# Browser & Human-like Pacing Settings
HEADLESS = os.getenv("HEADLESS", "false").lower() == "true"
USER_DATA_DIR = BASE_DIR / "browser_profile"

# Pacing / Anti-Detection Settings (in seconds)
# เวลาคิด/อ่านโจทย์ก่อนตอบข้อนั้นๆ (Think Time)
MIN_THINK_TIME = float(os.getenv('MIN_THINK_TIME', '3.0'))
MAX_THINK_TIME = float(os.getenv('MAX_THINK_TIME', '6.0'))

# เวลาเว้นช่วงระหว่างแต่ละข้อย่อย (เช่น ช่องว่าง 1 ไป 2, หรือ กาช้อยส์ 1 ไป 2)
SUB_ITEM_DELAY_MIN = float(os.getenv('SUB_ITEM_DELAY_MIN', '2.0'))
SUB_ITEM_DELAY_MAX = float(os.getenv('SUB_ITEM_DELAY_MAX', '4.0'))

# เวลาตรวจทานคำตอบก่อนกดปุ่ม Correction (ตรวจคำตอบ)
REVIEW_DELAY_MIN = float(os.getenv('REVIEW_DELAY_MIN', '2.0'))
REVIEW_DELAY_MAX = float(os.getenv('REVIEW_DELAY_MAX', '4.0'))

# Typing speed delay (in milliseconds per character)
MIN_TYPING_DELAY_MS = int(os.getenv('MIN_TYPING_DELAY_MS', '20'))
MAX_TYPING_DELAY_MS = int(os.getenv('MAX_TYPING_DELAY_MS', '60'))

# Pause between clicking next question (seconds) - เวลาหยุดพักหลังทำเสร็จ ก่อนกด Next
PAUSE_BETWEEN_QUESTIONS_MIN = float(os.getenv('PAUSE_BETWEEN_QUESTIONS_MIN', '3.0'))
PAUSE_BETWEEN_QUESTIONS_MAX = float(os.getenv('PAUSE_BETWEEN_QUESTIONS_MAX', '6.0'))
