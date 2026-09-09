import sys
import time
import logging
from logging_setup import setup_logging

# Start file logging before anything else emits a log line, so this run's log is complete.
LOG_FILE = setup_logging()

import config
from lm_client import LMStudioClient
from browser import SpeexxBrowser
from speexx_solver import SpeexxSolver

logger = logging.getLogger("Main")

def print_banner():
    print("=" * 65)
    print("    SPEEXX AUTO-SOLVER WITH AI PROXY (GEMINI 3.8 FLASH) & PLAYWRIGHT")
    print("=" * 65)
    print(f" AI Proxy Endpoint      : {config.LM_STUDIO_URL}")
    print(f" Target Model           : {config.LM_STUDIO_MODEL} (via Antigravity Proxy)")
    print(f" Visible Browser        : {'Enabled (Window visible)' if not config.HEADLESS else 'Headless'}")
    print(f" Auto-Login Email       : {config.SPEEXX_EMAIL if config.SPEEXX_EMAIL else 'Not set (will prompt if needed)'}")
    print(f" Pacing (Think Time)    : {config.MIN_THINK_TIME} - {config.MAX_THINK_TIME} seconds / question")
    print(f" Log file (this run)    : {LOG_FILE}")
    print("=" * 65)


def ensure_env_configured():
    """
    Ensures .env exists and has Speexx credentials.
    If .env is missing or credentials are empty, prompts the user once in the terminal and saves it automatically!
    """
    env_file = config.BASE_DIR / ".env"
    need_prompt = False

    if not env_file.exists():
        need_prompt = True
    elif not getattr(config, "SPEEXX_EMAIL", "").strip() or not getattr(config, "SPEEXX_PASSWORD", "").strip():
        need_prompt = True

    if need_prompt:
        print("\n" + "=" * 65)
        print("    🚀 ตั้งค่าเริ่มต้นใช้งานครั้งแรก (First-Time Setup)")
        print("=" * 65)
        print("ระบบตรวจพบว่ายังไม่มีการบันทึกบัญชี Speexx")
        print("กรุณากรอกข้อมูล (ระบบจะสร้าง .env และบันทึกให้อัตโนมัติ ไม่ต้องกรอกซ้ำอีก):\n")
        
        email = input("👉 Speexx Email (เช่น student_id@university.ac.th): ").strip()
        import getpass
        try:
            password = getpass.getpass("👉 Speexx Password: ").strip()
        except Exception:
            password = input("👉 Speexx Password: ").strip()

        # Update running config
        config.SPEEXX_EMAIL = email
        config.SPEEXX_PASSWORD = password

        # Write or update .env
        env_content = (
            f"SPEEXX_EMAIL={email}\n"
            f"SPEEXX_PASSWORD={password}\n"
            f"# AI Proxy Settings (Antigravity Proxy -> Gemini 3.8 Flash)\n"
            f"LM_STUDIO_URL=http://localhost:8000/v1\n"
            f"LM_STUDIO_MODEL=speexx-solver\n"
            f"PROXY_API_KEY={os.getenv('PROXY_API_KEY', 'speexx-proxy-key')}\n"
            f"HEADLESS=false\n"
            f"MIN_THINK_TIME=0.0\n"
            f"MAX_THINK_TIME=0.0\n"
            f"MIN_TYPING_DELAY_MS=20\n"
            f"MAX_TYPING_DELAY_MS=60\n"
            f"PAUSE_BETWEEN_QUESTIONS_MIN=0.5\n"
            f"PAUSE_BETWEEN_QUESTIONS_MAX=1.0\n"
        )
        with open(env_file, "w", encoding="utf-8") as f:
            f.write(env_content)
        print("\n✅ สร้างและบันทึกข้อมูลลง .env เรียบร้อยแล้ว! พร้อมใช้งานทันที\n" + "=" * 65 + "\n")


def ensure_proxy_running():
    """Checks if Antigravity Proxy is running on port 8000; if not, starts it automatically."""
    import requests
    import subprocess
    try:
        r = requests.get("http://127.0.0.1:8000/healthz", timeout=1.0)
        if r.status_code == 200:
            print("[Auto-Startup] Antigravity Proxy is already running and ready! (Gemini 3.8 Flash)")
            return True
    except Exception:
        pass

    print("[Auto-Startup] Starting Antigravity Proxy (Gemini 3.8 Flash) in background...")
    proxy_dir = config.BASE_DIR / "antigravity_Proxy"
    env_file = proxy_dir / ".env"
    
    # If proxy .env doesn't exist (e.g. after git clone), auto-create it with pre-configured working settings
    if not env_file.exists():
        proxy_content = (
            f"PROXY_API_KEYS={os.getenv('PROXY_API_KEY', 'speexx-proxy-key')}\n"
            "PROXY_MODELS=speexx-solver=gemini-2.5-flash\n"
            "PROXY_UPSTREAM=gemini\n"
            f"PROXY_UPSTREAM_API_KEY={os.getenv('GEMINI_API_KEY', '')}\n"
            "PROXY_UPSTREAM_TIMEOUT_SEC=120\n"
            "PROXY_MAX_CONCURRENCY=4\n"
            "PROXY_SCHEMA_MODE=native\n"
            "PROXY_TOKEN_STORE=/data/session.json\n"
        )
        try:
            with open(env_file, "w", encoding="utf-8") as f:
                f.write(proxy_content)
            print("  [Auto-Setup] Generated antigravity_Proxy/.env with Gemini 3.8 Flash")
        except Exception as e:
            logger.warning(f"Could not auto-generate proxy .env: {e}")

    env_vars = os.environ.copy()
    if env_file.exists():
        with open(env_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    k, _, v = line.partition("=")
                    env_vars[k.strip()] = v.strip().strip('"').strip("'")

    subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8000", "--log-level", "warning"],
        cwd=str(proxy_dir),
        env=env_vars,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == 'nt' else 0
    )
    for _ in range(12):
        time.sleep(0.5)
        try:
            r = requests.get("http://127.0.0.1:8000/healthz", timeout=1.0)
            if r.status_code == 200:
                print("✅ Antigravity Proxy started and ready!")
                return True
        except Exception:
            pass
    print("⚠️ Could not verify Antigravity Proxy on port 8000. You can run start_proxy.bat manually.")
    return False

def main():
    print_banner()
    ensure_proxy_running()
    lm_client = LMStudioClient()
    browser = None
    solver = None

    # Automatic startup: Launch visible browser and auto-login immediately
    print("\n[Auto-Startup] Launching browser & logging into Speexx...")
    try:
        browser = SpeexxBrowser()
        browser.launch("https://portal.speexx.com/")
        solver = SpeexxSolver(browser, lm_client)
        if config.SPEEXX_EMAIL:
            ok = browser.auto_login()
            if ok:
                print("✅ Auto-login completed successfully (Logged in)!")
            else:
                print("⚠️ Auto-login did not finish automatically. Please verify browser window.")
        else:
            print("ℹ️ SPEEXX_EMAIL is not set in .env. Please configure credentials if needed.")
    except Exception as e:
        logger.error(f"Auto-launch notice: {e}")
        print("Browser auto-launch notice. You can use Option 2 to re-open if needed.")

    while True:
        print("\n[MAIN MENU]")
        print("1. Test AI Proxy Connection & Gemini Model")
        print("2. Re-launch / Re-login Browser (Already running by default)")
        print("3. Solve Current Question (Single Step)")
        print("4. Start Auto-Loop Solver (Continuous with Pacing Delays)")
        print("5. Set Auto-Login Credentials / Adjust Delays")
        print("6. Exit")
        print("7. [DIAG] Dump ALL Page Elements to diagnose_output.txt")
        print("8. [DIAG] Test LLM Directly (bypass scraper)")
        print("9. [DIAG] Sniff Network Traffic on Correction / Show Solution Click")
        
        try:
            choice = input("\nSelect option (1-9): ").strip()
        except KeyboardInterrupt:
            print("\nExiting Speexx Auto-Solver (Ctrl+C pressed)...")
            if browser:
                browser.close()
            sys.exit(0)

        if choice == "1":
            print("\n--- Testing Connection to AI Proxy ---")
            if lm_client.test_connection():
                print("[SUCCESS] AI Proxy is running and ready!")
                print("Testing sample English grammar question...")
                res = lm_client.solve_exercise(
                    question_text="They ______ to the library yesterday afternoon.",
                    options=["go", "went", "going", "gone"],
                    context="Past simple tense test"
                )
                print(f"AI Selected Answer : {res.get('answer')}")
                print(f"AI Explanation     : {res.get('explanation')}")
            else:
                print("[ERROR] Cannot connect to AI Proxy. Make sure proxy is running (start_proxy.bat).")
                print("Run start_proxy.bat to start the Antigravity Proxy server first.")

        elif choice == "2":
            print("\n--- Re-launching / Checking Visible Browser ---")
            if not browser or not browser.page or browser.page.is_closed():
                browser = SpeexxBrowser()
                browser.launch("https://portal.speexx.com/")
                solver = SpeexxSolver(browser, lm_client)
                
                # Check for configured email or ask user
                if not config.SPEEXX_EMAIL:
                    print("\n[Auto-Login Configuration]")
                    email_in = input("Enter your Speexx Email (press Enter to skip): ").strip()
                    if email_in:
                        config.SPEEXX_EMAIL = email_in
                        config.SPEEXX_PASSWORD = input("Enter your Password: ").strip()

                if config.SPEEXX_EMAIL:
                    browser.auto_login()
            else:
                print("Browser is already active. Attempting auto-login if needed...")
                if config.SPEEXX_EMAIL:
                    browser.auto_login()
            print("Please check your desktop screen for the browser window.")

        elif choice == "3":
            if not browser or not browser.page:
                print("\n[!] Please launch the browser first (Option 2).")
                continue
            print("\n--- Solving Current Exercise (Auto-Retrying autonomously until >= 80%) ---")
            try:
                passed = solver.solve_exercise_until_passed(max_retries=5, min_passing_score=80)
                if passed:
                    print("\n[SUCCESS] Exercise passed >= 80% and advanced!")
                else:
                    print("\n[ALERT] Reached maximum attempts (5). Skipped/advanced to next exercise.")
            except KeyboardInterrupt:
                print("\n[PAUSED] Exercise solving stopped immediately by user (Ctrl+C).")
            except Exception as e:
                logger.exception(f"Error during step execution: {e}")

        elif choice == "4":
            if not browser or not browser.page:
                print("\n[!] Please launch the browser first (Option 2).")
                continue
            print("\n--- Starting Continuous Auto-Solver (Autonomous until >= 80% on each exercise) ---")
            print("Press CTRL+C in this terminal at any time to pause auto-solver.")
            try:
                count = 0
                while True:
                    count += 1
                    print(f"\n>>> [Continuous Solver: Exercise Step #{count}] <<<")
                    passed = solver.solve_exercise_until_passed(max_retries=5, min_passing_score=80)
                    if not passed:
                        print("Could not pass exercise after 5 retries. Skipped to next exercise automatically...")
                        time.sleep(3.0)
            except KeyboardInterrupt:
                print("\nAuto-solver paused by user.")
            except Exception as e:
                logger.exception(f"Error during continuous auto-solver loop: {e}")

        elif choice == "5":
            print("\n--- Auto-Login & Pacing Settings ---")
            email_in = input(f"Enter Speexx Email (current: {config.SPEEXX_EMAIL or 'Not set'}): ").strip()
            if email_in:
                config.SPEEXX_EMAIL = email_in
                config.SPEEXX_PASSWORD = input("Enter Password: ").strip()
            
            try:
                min_t = float(input(f"Enter Minimum Think Time in seconds (current: {config.MIN_THINK_TIME}): ") or config.MIN_THINK_TIME)
                max_t = float(input(f"Enter Maximum Think Time in seconds (current: {config.MAX_THINK_TIME}): ") or config.MAX_THINK_TIME)
                config.MIN_THINK_TIME = max(1.0, min_t)
                config.MAX_THINK_TIME = max(config.MIN_THINK_TIME, max_t)
                print(f"[UPDATED] Credentials & Pacing updated successfully.")
            except ValueError:
                print("Invalid pacing input.")

        elif choice == "6":
            print("\nExiting Speexx Auto-Solver...")
            print(f"Full log of this run saved to: {LOG_FILE}")
            print("Attach that file when reporting a problem -- it has every step, not just what fit on screen.")
            if browser:
                browser.close()
            sys.exit(0)

        else:
            print("\n[!] Invalid option. Please select 1-6.")


if __name__ == "__main__":
    main()
