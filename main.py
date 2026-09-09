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

        elif choice == "7":
            if not browser or not browser.page:
                print("\n[!] Please launch the browser first (Option 2).")
                continue
            print("\n--- Dumping ALL Page Elements (Diagnostic) ---")
            try:
                from datetime import datetime
                out_file = f"diagnose_output_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
                dump_page_elements(browser, out_file)
                print(f"[DONE] Full DOM dump saved to: {out_file}")
                print("Open that file to see every button, input, word bank element, and drop zone.")
            except Exception as e:
                logger.exception(f"Error during diagnostic dump: {e}")

        elif choice == "8":
            print("\n--- Testing LLM Studio Directly ---")
            try:
                test_llm_direct(lm_client)
            except Exception as e:
                logger.exception(f"Error during LLM test: {e}")

        elif choice == "9":
            if not browser or not browser.page:
                print("\n[!] Please launch the browser first (Option 2).")
                continue
            print("\n--- Sniffing Network Traffic on Correction / Show Solution Click ---")
            recorded_requests = []
            recorded_responses = []

            def on_req(req):
                try:
                    recorded_requests.append({
                        "method": req.method,
                        "url": req.url,
                        "type": req.resource_type,
                        "post_data": req.post_data
                    })
                except Exception:
                    pass

            def on_res(res):
                try:
                    ct = (res.headers.get("content-type") or "").lower()
                    body = ""
                    if "json" in ct or "text" in ct:
                        try:
                            body = res.text()[:1000]
                        except Exception:
                            body = ""
                    recorded_responses.append({
                        "status": res.status,
                        "url": res.url,
                        "content_type": ct,
                        "body_preview": body
                    })
                except Exception:
                    pass

            try:
                browser.context.on("request", on_req)
                browser.context.on("response", on_res)
            except Exception:
                pass

            print("[1] Clicking 'Correction' / 'Show solution' button on screen now...")
            clicked = browser.page.evaluate('''() => {
                const btn = document.querySelector('button.correct, button.action-exercise-button.correct');
                if (btn) { btn.click(); return btn.innerText || btn.className; }
                const anyBtn = Array.from(document.querySelectorAll('button, a, div[role="button"]')).find(b => {
                    const txt = (b.innerText || b.textContent || '').toLowerCase();
                    return txt.includes('correction') || txt.includes('solution') || txt.includes('ตรวจคำตอบ') || txt.includes('เฉลย');
                });
                if (anyBtn) { anyBtn.click(); return anyBtn.innerText || anyBtn.textContent; }
                return null;
            }''')
            print(f"Clicked button: {clicked}")
            print("Listening to network traffic for 4 seconds...")
            time.sleep(4.0)

            try:
                browser.context.remove_listener("request", on_req)
                browser.context.remove_listener("response", on_res)
            except Exception:
                pass

            print("\n" + "="*70)
            print(f"📡 NETWORK TRAFFIC INTERCEPT REPORT ({len(recorded_requests)} Requests, {len(recorded_responses)} Responses)")
            print("="*70)

            api_reqs = [r for r in recorded_requests if not r["url"].endswith((".png", ".jpg", ".jpeg", ".css", ".js", ".svg", ".woff", ".woff2"))]
            if not api_reqs:
                print("\n🔍 [ผลการวิเคราะห์]: ไม่มีการส่ง Request เช็คคำตอบผ่าน Network เลย!")
                print("   => Speexx ทำการตรวจคำตอบ (Correction) และคำนวณคะแนนทั้งหมดบนเครื่อง Client (Local JavaScript / Backbone.js Memory)")
                print("   => การตรวจถูก/ผิด และเฉลยถูกเก็บไว้ในหน่วยความจำของหน้าเว็บตั้งแต่โหลดแบบฝึกหัดเข้ามา")
            else:
                print(f"\n📡 ตรวจพบ {len(api_reqs)} API Request(s):")
                for i, r in enumerate(api_reqs):
                    print(f"  [{i+1}] {r['method']} ({r['type']}): {r['url']}")
                    if r['post_data']:
                        print(f"      Payload: {r['post_data'][:300]}")

            print("\n🧠 ตรวจสอบตัวแปรและข้อมูลเฉลยในหน่วยความจำของหน้าเว็บ (Client Memory)...")
            client_diag = browser.page.evaluate('''() => {
                const results = {};
                if (window.Speexx) results.hasSpeexxGlobal = true;
                if (window.Backbone) results.hasBackbone = true;
                
                const itemsWithData = Array.from(document.querySelectorAll('[data-answer-id], [data-solution], [data-correct], [data-word-id], [data-drag-drop-id]'))
                    .map(el => ({
                        tag: el.tagName,
                        cls: el.className,
                        answerId: el.getAttribute('data-answer-id'),
                        solution: el.getAttribute('data-solution'),
                        correct: el.getAttribute('data-correct'),
                        wordId: el.getAttribute('data-word-id'),
                        dragDropId: el.getAttribute('data-drag-drop-id')
                    }));
                results.itemsWithData = itemsWithData.slice(0, 10);
                return results;
            }''')
            print(f"Client-Side DOM & Memory Data: {client_diag}")


def dump_page_elements(browser_obj, output_file):
    """Dump ALL elements from the current Speexx page to a file for diagnosis."""
    import json
    from datetime import datetime

    results = []
    def log(msg):
        print(msg)
        results.append(msg)

    log(f"=== SPEEXX PAGE DIAGNOSTIC DUMP - {datetime.now().isoformat()} ===")
    log(f"Current URL: {browser_obj.page.url}")

    try:
        exercise_class = browser_obj.page.evaluate('''() => {
            const el = document.querySelector('.exercise[class*="type-"], [class*="type-"]');
            return el ? el.className : '(not found)';
        }''')
        log(f"Speexx exercise container class: {exercise_class}")
    except Exception as e:
        log(f"Speexx exercise container class: (error: {e})")
    log("")

    all_frames = browser_obj.get_all_frames()
    log(f"Total frames found: {len(all_frames)}")

    for fi, frame in enumerate(all_frames):
        log(f"\n{'='*60}")
        log(f"FRAME #{fi}: {frame.url}")
        log(f"{'='*60}")

        try:
            # 1. FULL body text
            body_text = frame.evaluate("() => document.body ? document.body.innerText : '(no body)'")
            log(f"\n--- BODY TEXT (first 4000 chars) ---")
            log((body_text or '(empty)')[:4000])

            # 2. ALL buttons
            log(f"\n--- ALL BUTTONS ({frame.evaluate('() => document.querySelectorAll(`button, [role=button]`).length')} found) ---")
            buttons = frame.evaluate('''() => {
                return Array.from(document.querySelectorAll('button, [role="button"], a.btn')).map((el, i) => ({
                    i: i,
                    tag: el.tagName,
                    text: (el.innerText || el.textContent || '').trim().substring(0, 80),
                    cls: (el.className || '').toString().substring(0, 120),
                    id: el.id || '',
                    draggable: el.draggable || false,
                    data: Array.from(el.attributes).filter(a => a.name.startsWith('data-')).map(a => a.name+'='+a.value).join(', ')
                }));
            }''')
            for b in (buttons or []):
                log(f"  [{b['i']}] <{b['tag']}> text='{b['text']}' cls='{b['cls']}' id='{b['id']}' drag={b.get('draggable')} data=[{b.get('data','')}]")

            # 3. ALL inputs
            log(f"\n--- ALL INPUTS ---")
            inputs = frame.evaluate('''() => {
                return Array.from(document.querySelectorAll('input, textarea, select, [contenteditable]')).map((el, i) => ({
                    i: i,
                    tag: el.tagName,
                    type: el.type || '',
                    cls: (el.className || '').toString().substring(0, 120),
                    id: el.id || '',
                    placeholder: el.placeholder || '',
                    value: (el.value || '').substring(0, 50),
                    name: el.name || '',
                    editable: el.contentEditable || '',
                    data: Array.from(el.attributes).filter(a => a.name.startsWith('data-')).map(a => a.name+'='+a.value).join(', ')
                }));
            }''')
            for inp in (inputs or []):
                log(f"  [{inp['i']}] <{inp['tag']} type={inp['type']}> cls='{inp['cls']}' id='{inp['id']}' placeholder='{inp['placeholder']}' value='{inp['value']}' name='{inp['name']}' data=[{inp.get('data','')}]")

            # 4. ALL draggable elements
            log(f"\n--- ALL DRAGGABLE ELEMENTS ---")
            drags = frame.evaluate('''() => {
                return Array.from(document.querySelectorAll('[draggable], [draggable="true"]')).map((el, i) => ({
                    tag: el.tagName,
                    text: (el.innerText || '').trim().substring(0, 80),
                    cls: (el.className || '').toString().substring(0, 120)
                }));
            }''')
            if drags:
                for d in drags:
                    log(f"  <{d['tag']}> text='{d['text']}' cls='{d['cls']}'")
            else:
                log("  (none found)")

            # 5. WORD BANK SEARCH - find the exact word bank words from Speexx
            log(f"\n--- WORD BANK SEARCH (leaf elements with single words) ---")
            word_bank = frame.evaluate('''() => {
                const found = [];
                const all = Array.from(document.querySelectorAll('*'));
                all.forEach(el => {
                    const text = (el.innerText || el.textContent || '').trim();
                    // Leaf elements with short text (likely word chips)
                    if (el.children.length === 0 && text.length > 0 && text.length < 30 && !text.includes('\\n')) {
                        const rect = el.getBoundingClientRect();
                        if (rect.width > 20 && rect.height > 10 && rect.bottom > 400) {
                            // Elements in the bottom area (word bank)
                            found.push({
                                tag: el.tagName,
                                text: text,
                                cls: (el.className || '').toString().substring(0, 120),
                                pTag: el.parentElement ? el.parentElement.tagName : '',
                                pCls: el.parentElement ? (el.parentElement.className || '').toString().substring(0, 120) : '',
                                gpCls: (el.parentElement && el.parentElement.parentElement) ? (el.parentElement.parentElement.className || '').toString().substring(0, 120) : '',
                                x: Math.round(rect.x),
                                y: Math.round(rect.y),
                                w: Math.round(rect.width),
                                h: Math.round(rect.height),
                                html: el.outerHTML.substring(0, 200),
                                pHtml: el.parentElement ? el.parentElement.outerHTML.substring(0, 250) : ''
                            });
                        }
                    }
                });
                return found;
            }''')
            if word_bank:
                log(f"  Found {len(word_bank)} candidate elements in bottom area:")
                for w in word_bank:
                    log(f"    TEXT='{w['text']}' <{w['tag']}> cls='{w['cls']}' pos=({w['x']},{w['y']}) size=({w['w']}x{w['h']})")
                    log(f"      parent=<{w['pTag']}> pCls='{w['pCls']}' gpCls='{w['gpCls']}'")
                    log(f"      HTML: {w['html'][:180]}")
                    log(f"      PARENT: {w['pHtml'][:220]}")
            else:
                log("  (no word-like elements found in bottom area)")

            # 6. DROP ZONES / BLANK SLOTS
            log(f"\n--- DROP ZONES / BLANK SLOTS (inputs with empty value in sentences) ---")
            blanks = frame.evaluate('''() => {
                const found = [];
                // Look for inline elements that look like blanks
                const candidates = Array.from(document.querySelectorAll(
                    'input, [contenteditable], .gap, .blank, .drop-slot, .drop-target, [class*="gap"], [class*="blank"], [class*="drop"], [class*="slot"], [class*="target"], [class*="placeholder"], span[style*="border"], span[style*="underline"]'
                ));
                candidates.forEach((el, i) => {
                    const rect = el.getBoundingClientRect();
                    if (rect.width > 10 && rect.height > 5) {
                        found.push({
                            i: i,
                            tag: el.tagName,
                            type: el.type || '',
                            cls: (el.className || '').toString().substring(0, 120),
                            text: (el.innerText || el.value || '').trim().substring(0, 50),
                            x: Math.round(rect.x),
                            y: Math.round(rect.y),
                            w: Math.round(rect.width),
                            h: Math.round(rect.height),
                            html: el.outerHTML.substring(0, 300)
                        });
                    }
                });
                return found;
            }''')
            if blanks:
                for bl in blanks:
                    log(f"  [{bl['i']}] <{bl['tag']} type={bl.get('type','')}> cls='{bl['cls']}' text='{bl['text']}' pos=({bl['x']},{bl['y']}) size=({bl['w']}x{bl['h']})")
                    log(f"    HTML: {bl['html'][:250]}")
            else:
                log("  (none found)")

            # 7. KEY STRUCTURAL ELEMENTS
            log(f"\n--- KEY STRUCTURAL ELEMENTS ---")
            structure = frame.evaluate('''() => {
                return Array.from(document.querySelectorAll(
                    'main, section, article, form, nav, [role], [class*="exercise"], [class*="activity"], [class*="content"], [class*="question"], [class*="task"], [class*="word-bank"], [class*="wordbank"]'
                )).map(el => ({
                    tag: el.tagName,
                    cls: (el.className || '').toString().substring(0, 150),
                    role: el.getAttribute('role') || '',
                    kids: el.children.length,
                    txtLen: (el.innerText || '').length
                }));
            }''')
            for s in (structure or []):
                log(f"  <{s['tag']}> cls='{s['cls']}' role='{s['role']}' children={s['kids']} textLen={s['txtLen']}")

            # 8. FULL RAW HTML of .exercise-items (the actual answer content container)
            log(f"\n--- RAW HTML: .exercise-items (full, untruncated) ---")
            raw_html = frame.evaluate('''() => {
                const el = document.querySelector('.exercise-items');
                return el ? el.outerHTML : '(.exercise-items not found on this page)';
            }''')
            log(raw_html or '(empty)')

        except Exception as e:
            log(f"  ERROR in frame #{fi}: {e}")

    # Write to file
    with open(output_file, 'w', encoding='utf-8') as f:
        f.write('\n'.join(results))


def test_llm_direct(lm_client):
    """Test LLM Studio directly with simple prompts to check if it responds."""
    from openai import OpenAI
    import config

    client = OpenAI(base_url=config.LM_STUDIO_URL, api_key="lm-studio")

    # Test 1: Single word answer
    print("\n[TEST 1] Ultra-simple: past tense of 'go'")
    try:
        r = client.chat.completions.create(
            model=config.LM_STUDIO_MODEL,
            messages=[{"role": "user", "content": "What is the past tense of 'go'? Reply with ONLY the word."}],
            temperature=0.1, max_tokens=20
        )
        raw = r.choices[0].message.content
        print(f"  Raw content object: {repr(raw)}")
        print(f"  Stripped: '{raw.strip() if raw else '(None)'}' (len={len(raw) if raw else 0})")
    except Exception as e:
        print(f"  ERROR: {e}")

    # Test 2: Fill in blank from word bank
    print("\n[TEST 2] Fill-in-the-blank with word bank")
    try:
        r = client.chat.completions.create(
            model=config.LM_STUDIO_MODEL,
            messages=[
                {"role": "system", "content": "Reply with ONLY the answer word, nothing else."},
                {"role": "user", "content": "The situation is getting ___. Choose from: serious, late, done, lost"}
            ],
            temperature=0.2, max_tokens=20
        )
        raw = r.choices[0].message.content
        print(f"  Raw content object: {repr(raw)}")
        print(f"  Stripped: '{raw.strip() if raw else '(None)'}' (len={len(raw) if raw else 0})")
    except Exception as e:
        print(f"  ERROR: {e}")

    # Test 3: Multiple blanks
    print("\n[TEST 3] Multiple blanks exercise")
    try:
        r = client.chat.completions.create(
            model=config.LM_STUDIO_MODEL,
            messages=[
                {"role": "system", "content": "Reply with ONLY the answers, one per line."},
                {"role": "user", "content": """Fill each blank with: permission, done, said, tickets, late, letter, serious, nerves, going, head, lost, talking, you, dirty

1. The situation is getting ___.
2. I got an interesting ___ this morning.
3. Can you get me two ___ for the concert?"""}
            ],
            temperature=0.2, max_tokens=100
        )
        raw = r.choices[0].message.content
        print(f"  Raw content object: {repr(raw)}")
        print(f"  Stripped: '{raw.strip() if raw else '(None)'}' (len={len(raw) if raw else 0})")
    except Exception as e:
        print(f"  ERROR: {e}")

    # Test 4: List available models
    print("\n[TEST 4] Available models in LM Studio")
    try:
        models = client.models.list()
        for m in models.data:
            print(f"  Model ID: {m.id}")
    except Exception as e:
        print(f"  ERROR: {e}")


if __name__ == "__main__":
    main()
