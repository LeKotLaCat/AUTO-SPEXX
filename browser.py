import time
import json
import base64
import random
import logging
import re
from pathlib import Path
from playwright.sync_api import sync_playwright, BrowserContext, Page, Frame
import config

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("SpeexxBrowser")


def _norm_text(text: str) -> str:
    """
    Normalise page text for comparison: collapse whitespace and unify curly apostrophes.
    Word-bank chips can hold multi-word phrases ("wouldn't leave"), where the page's
    spacing and apostrophe style will not match what the model typed character for
    character.
    """
    t = (text or "").replace("’", "'").replace("‘", "'")
    return " ".join(t.split()).strip().lower()

EXCLUDED_UI_WORDS = {
    "beach", "boys", "match", "words", "correct", "pictures", "correction",
    "next", "back", "check", "continue", "submit", "exercise", "packet",
    "you", "can", "do", "better", "score", "standard-packet"
}

class SpeexxBrowser:
    def __init__(self):
        self.playwright = None
        self.context: BrowserContext = None
        self.page: Page = None
        self.user_data_dir = config.USER_DATA_DIR
        self.user_data_dir.mkdir(parents=True, exist_ok=True)
        self.cached_media_transcript = ""
        self.captured_audio_urls = []
        self.captured_audio_bytes = {}
        self.last_eps_solutions = {}

    def _handle_network_request(self, request):
        """Intercepts all network requests to monitor API calls and tracking data."""
        try:
            url = request.url
            method = request.method
            res_type = request.resource_type
            post_data = request.post_data

            # Ignore static assets for clean API logging
            if not url.lower().endswith((".png", ".jpg", ".jpeg", ".svg", ".gif", ".css", ".woff", ".woff2", ".ttf")):
                if res_type in ["xhr", "fetch", "document"] or any(k in url.lower() for k in ["api", "tracking", "result", "exercise", "packet", "article", "scorm", "solution", "correct"]):
                    post_info = f" | Payload: {post_data[:500]}" if post_data else ""
                    logger.info(f"🌐 [NETWORK REQUEST] {method} ({res_type}) {url}{post_info}")
        except Exception:
            pass

    def _handle_network_response(self, response):
        """Intercepts network responses to capture audio/video files and inspect API responses."""
        try:
            url = response.url
            content_type = (response.headers.get("content-type") or "").lower()
            status = response.status

            is_audio = (
                url.lower().endswith((".mp3", ".mp4", ".ogg", ".wav", ".m4a", ".aac", ".webm"))
                or ".mp3?" in url.lower() or ".mp4?" in url.lower() or ".ogg?" in url.lower()
                or "audio/" in content_type
                or "video/mp4" in content_type
                or ("/audio/" in url.lower() and status == 200)
                or ("/sound/" in url.lower() and status == 200)
                or ("/speech/" in url.lower() and status == 200)
            )

            if is_audio and not url.lower().endswith((".png", ".jpg", ".jpeg", ".svg", ".gif", ".css", ".js", ".html")):
                if url not in self.captured_audio_urls:
                    logger.info(f"🎧 [Network Interceptor] Captured audio response: {url[:100]}")
                    self.captured_audio_urls.append(url)
                    try:
                        body = response.body()
                        if body and len(body) > 100:
                            self.captured_audio_bytes[url] = body
                            logger.info(f"🎧 [Network Interceptor] Cached {len(body):,} bytes of audio from response.")
                    except Exception:
                        pass
            elif not url.lower().endswith((".png", ".jpg", ".jpeg", ".svg", ".gif", ".css", ".woff", ".woff2", ".ttf")):
                # Log API responses
                if any(k in url.lower() for k in ["api", "tracking", "result", "exercise", "packet", "article", "scorm", "solution", "correct"]):
                    try:
                        resp_text = response.text() if "json" in content_type or "text" in content_type else ""
                        text_preview = resp_text[:500]
                        logger.info(f"🌐 [NETWORK RESPONSE] [{status}] {url} -> {text_preview}")

                                                # Automatically harvest correct solutions from Speexx grading responses!
                        if "eps-correct" in url.lower() and resp_text:
                            extracted = {}
                            try:
                                data = json.loads(resp_text)
                                for k, v in data.items():
                                    if k.startswith("#") and isinstance(v, dict) and "SOLUTION" in v:
                                        sol = v["SOLUTION"]
                                        extracted[k] = sol  # e.g. '#1', '#2', '#3', '#5'
                                        try:
                                            extracted[int(k[1:]) - 1] = sol
                                        except Exception:
                                            pass
                            except Exception:
                                pass
                            if not extracted:
                                sols = re.findall(r'"(#?\d+)":\{[^{}]*?"SOLUTION":"([^"]+)"', resp_text)
                                if sols:
                                    for k, v in sols:
                                        raw_k = k if k.startswith('#') else f'#{k}'
                                        extracted[raw_k] = v
                                        try:
                                            extracted[int(raw_k[1:]) - 1] = v
                                        except Exception:
                                            pass
                            if extracted:
                                self.last_eps_solutions = extracted
                                logger.info(f"🎯 [AUTO-HARVEST EPS SOLUTIONS] Captured {len(self.last_eps_solutions)} true solutions: {self.last_eps_solutions}")
                    except Exception:
                        pass
        except Exception:
            pass

    def launch(self, initial_url: str = "https://portal.speexx.com/"):
        """Launches a visible Chromium browser with persistent profile storage or connects to existing session."""
        logger.info("Starting Visible Browser window...")
        self.playwright = sync_playwright().start()
        
        connected_to_existing = False
        self.browser_cdp = None
        try:
            import urllib.request
            with urllib.request.urlopen("http://127.0.0.1:9222/json/version", timeout=1.0) as resp:
                if resp.status == 200:
                    logger.info("Existing Chrome session detected on port 9222. Connecting directly via CDP...")
                    self.browser_cdp = self.playwright.chromium.connect_over_cdp("http://127.0.0.1:9222")
                    self.context = self.browser_cdp.contexts[0] if self.browser_cdp.contexts else self.browser_cdp.new_context()
                    self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
                    connected_to_existing = True
        except Exception as e:
            logger.debug(f"No existing CDP session on port 9222 ({e}). Launching fresh browser...")

        if not connected_to_existing:
            try:
                self.context = self.playwright.chromium.launch_persistent_context(
                    user_data_dir=str(self.user_data_dir),
                    headless=config.HEADLESS,
                    channel="chrome" if Path("C:/Program Files/Google/Chrome/Application/chrome.exe").exists() else "chromium",
                    args=[
                        "--start-maximized",
                        "--disable-blink-features=AutomationControlled",
                        "--remote-debugging-port=9222"
                    ],
                    viewport=None
                )
            except Exception as e:
                if "already in use" in str(e).lower() or "existing browser session" in str(e).lower():
                    logger.warning("Default browser profile is in use. Launching dedicated session profile...")
                    session_dir = config.USER_DATA_DIR.parent / "browser_profile_session"
                    self.context = self.playwright.chromium.launch_persistent_context(
                        user_data_dir=str(session_dir),
                        headless=config.HEADLESS,
                        channel="chrome" if Path("C:/Program Files/Google/Chrome/Application/chrome.exe").exists() else "chromium",
                        args=["--start-maximized", "--disable-blink-features=AutomationControlled"],
                        viewport=None
                    )
                else:
                    raise e

            self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
        
        # Attach network request & response listeners to monitor API calls and media
        try:
            self.context.on("request", self._handle_network_request)
            self.context.on("response", self._handle_network_response)
        except Exception as e:
            logger.debug(f"Notice on attaching network listeners: {e}")

        # Check if current page needs navigation
        cur_url = self.page.url if self.page else ""
        if not ("speexx.com" in cur_url):
            logger.info(f"Navigating to {initial_url}...")
            try:
                self.page.goto(initial_url, wait_until="domcontentloaded", timeout=60000)
            except Exception as e:
                logger.warning(f"Initial navigation notice: {e}")
        else:
            logger.info(f"Browser page currently on: {cur_url}")
        
        logger.info("Browser window ready!")

    def auto_login(self, email: str = None, password: str = None) -> bool:
        """
        Detects login fields on Speexx / Microsoft SSO login pages and automatically logs in:
        Step 0: Check if already logged into Speexx dashboard
        Step 1: Fill email / username & click Next
        Step 2: Fill password & click Log in
        Step 3: Handle Microsoft 'Stay signed in?' prompt if present
        Step 4: Confirm successful dashboard load
        """
        if not self.page:
            return False

        email = email or config.SPEEXX_EMAIL
        password = password or config.SPEEXX_PASSWORD

        if not email:
            logger.info("Auto-login credentials not configured. Please set SPEEXX_EMAIL in .env.")
            return False

        try:
            logger.info("Checking login state...")

            # Give the page a moment (up to 4s) to resolve initial redirect
            for _ in range(8):
                cur_url = self.page.url
                if "portal.speexx.com" in cur_url and "/login" not in cur_url and cur_url != "https://portal.speexx.com/":
                    logger.info(f"✅ Already logged into Speexx! Current URL: {cur_url}")
                    return True
                if "/login" in cur_url or "microsoftonline.com" in cur_url:
                    break
                time.sleep(0.5)

            # Check again if already logged in (e.g. dashboard elements present)
            cur_url = self.page.url
            if "portal.speexx.com" in cur_url and "/login" not in cur_url and cur_url != "https://portal.speexx.com/":
                try:
                    if not self.page.locator("#userName, input[type='password']").first.is_visible():
                        logger.info(f"✅ Already logged into Speexx! Current URL: {cur_url}")
                        return True
                except Exception:
                    pass

            logger.info(f"Initiating auto-login for {email} (Current URL: {self.page.url})...")

            # STEP 1: Email / Username Field
            email_field = None
            email_selectors = [
                "#userName",                              # Speexx portal exact ID
                "input[name='userName']",                 # Speexx portal exact name
                "input[name='loginfmt']",                 # Microsoft SSO
                "input[type='email']",
                "#username", "#email",
                "input[name='username']", "input[name='email']",
                "input[autocomplete='username']",
                "input[placeholder*='email' i]",
                "input[placeholder*='user' i]",
                "input[aria-label*='email' i]",
                "input[aria-label*='user' i]",
                "form input[type='text']",
                "input[type='text']",
            ]

            matched_selector = None
            for attempt in range(8):
                for sel in email_selectors:
                    try:
                        loc = self.page.locator(sel)
                        if loc.count() > 0 and loc.first.is_visible():
                            email_field = loc.first
                            matched_selector = sel
                            break
                    except Exception:
                        pass
                if email_field:
                    break
                time.sleep(0.5)

            if email_field:
                logger.info(f"Found login field via selector: {matched_selector}")
                logger.info(f"Filling email: {email}")
                email_field.focus()
                email_field.fill(email)
                time.sleep(0.5)

                # Check 'Remember me' if present on the page
                try:
                    rem_box = self.page.locator("#rememberMe, input[name='rememberMe'], input[type='checkbox']").first
                    if rem_box.is_visible() and not rem_box.is_checked():
                        rem_box.check()
                        logger.info("Checked 'Remember me' option.")
                except Exception:
                    pass

                # Click Next button
                next_selectors = [
                    "button.button-login",
                    "button#button-next",
                    "#idSIButton9",
                    "input[type='submit']",
                    "button[type='submit']",
                    "button:has-text('Next')",
                    "button:has-text('ถัดไป')",
                    "a:has-text('Next')",
                ]
                for n_sel in next_selectors:
                    try:
                        btn = self.page.locator(n_sel).first
                        if btn.is_visible():
                            logger.info(f"Clicked Next button via '{n_sel}'...")
                            btn.click()
                            time.sleep(2.0)
                            break
                    except Exception:
                        pass
            else:
                logger.info("No email field visible. Checking if password field is already visible...")

            # STEP 2: Password Field (Wait up to 15s for Speexx or SSO password box)
            logger.info("Waiting for password field to render on screen...")
            pwd_field = None
            pwd_selectors = [
                "input[type='password']",
                "input[name='password']",
                "input[name='passwd']",                   # Microsoft SSO
                "#password",
                "#i0118",                                 # Microsoft SSO password ID
                "input[placeholder*='password' i]",
                "input[aria-label*='password' i]",
            ]

            matched_pwd_sel = None
            for attempt in range(15):
                for sel in pwd_selectors:
                    try:
                        loc = self.page.locator(sel)
                        if loc.count() > 0 and loc.first.is_visible():
                            pwd_field = loc.first
                            matched_pwd_sel = sel
                            break
                    except Exception:
                        pass
                if pwd_field:
                    break

                # Check if we were already redirected into Speexx without needing password
                cur_url = self.page.url
                if "portal.speexx.com" in cur_url and "/login" not in cur_url and cur_url != "https://portal.speexx.com/":
                    logger.info(f"✅ Logged in without password (session restored)! URL: {cur_url}")
                    return True

                time.sleep(1.0)

            if not pwd_field:
                cur_url = self.page.url
                if "portal.speexx.com" in cur_url and "/login" not in cur_url:
                    logger.info(f"✅ Dashboard reached! URL: {cur_url}")
                    return True
                logger.warning(f"Password field never appeared (Current URL: {cur_url}).")
            elif not password:
                logger.warning("Password field found but SPEEXX_PASSWORD is not set in .env.")

            if pwd_field and password:
                logger.info(f"Found password field via '{matched_pwd_sel}', filling password...")
                pwd_field.focus()
                pwd_field.fill(password)
                time.sleep(0.5)

                # Check 'Remember me' if visible on password screen
                try:
                    rem_box = self.page.locator("#rememberMe, input[name='rememberMe']").first
                    if rem_box.is_visible() and not rem_box.is_checked():
                        rem_box.check()
                except Exception:
                    pass

                # Submit login
                login_btn_selectors = [
                    "#button-sign-in",
                    "button[name='sign-in']",
                    "#idSIButton9",
                    "input[type='submit']",
                    "button[type='submit']",
                    "button:has-text('Log in')",
                    "button:has-text('Sign in')",
                    "button:has-text('เข้าสู่ระบบ')",
                ]
                for l_sel in login_btn_selectors:
                    try:
                        l_btn = self.page.locator(l_sel).first
                        if l_btn.is_visible():
                            logger.info(f"Clicked login button via '{l_sel}'!")
                            l_btn.click()
                            time.sleep(2.5)
                            break
                    except Exception:
                        pass

            # STEP 3: Handle Microsoft 'Stay signed in?' prompt ("ลงชื่อเข้าใช้ค้างไว้หรือไม่?")
            for _ in range(5):
                try:
                    stay_btn = self.page.locator("#idSIButton9, input[value='Yes'], button:has-text('Yes'), button:has-text('ใช่'), input[value='ใช่']").first
                    if stay_btn.is_visible():
                        logger.info("Clicked 'Stay signed in?' Yes button!")
                        stay_btn.click()
                        time.sleep(2.0)
                        break
                except Exception:
                    pass
                time.sleep(1.0)

            # STEP 4: Wait for landing on Speexx dashboard
            logger.info("Waiting for Speexx portal dashboard to load...")
            for attempt in range(15):
                cur_url = self.page.url
                if "portal.speexx.com" in cur_url and "/login" not in cur_url and cur_url != "https://portal.speexx.com/":
                    logger.info(f"✅ Successfully logged in to Speexx! Current URL: {cur_url}")
                    return True
                time.sleep(1.0)

            logger.info(f"Auto-login flow completed (Current URL: {self.page.url}).")
            return True

        except Exception as e:
            logger.warning(f"Auto-login notice: {e}")
            return False

    def human_delay(self, min_sec: float = config.MIN_THINK_TIME, max_sec: float = config.MAX_THINK_TIME):
        """Simulates human reading/thinking time with randomized delay (if enabled)."""
        if max_sec <= 0:
            return
        delay = random.uniform(min_sec, max_sec)
        if delay > 0:
            logger.info(f"[Pacing] Pausing for {delay:.2f}s...")
            time.sleep(delay)

    def get_all_frames(self) -> list:
        """Returns a list of all active frames (main frame + iframes)."""
        if not self.page:
            return []
        frames = [self.page.main_frame]
        for f in self.page.frames:
            if f not in frames and not f.is_detached():
                frames.append(f)
        return frames

    def click_audio_or_speaker_if_present(self) -> bool:
        """
        Scans ALL frames for speaker icons, loudspeaker buttons ('🔊'), and dialogue info buttons ('i').
        Clicks ONCE and immediately stops to avoid toggling/closing the dialogue window.
        """
        if not self.page:
            return False

        # Isolate audio capture: clear previous exercise's captured audio tracks!
        self.captured_audio_urls.clear()
        self.captured_audio_bytes.clear()

        clicked_any = False
        time.sleep(1.0)

        # Selectors prioritized by accuracy (AUDIO & SPEAKER ONLY - NO HELP 'i' ICON)
        speaker_selectors = [
            "text='🔊'",
            "button:has-text('🔊')",
            "span:has-text('🔊')",
            "div:has-text('🔊')",
            ".icon-speaker",
            ".icon-volume-up",
            "[class*='speaker']",
            "[class*='loudspeaker']",
            "[class*='audio-player']"
        ]

        # Strategy 1: Playwright Locators - Click ALL visible speakers on the page
        for frame in self.get_all_frames():
            try:
                for sel in speaker_selectors:
                    loc = frame.locator(sel)
                    cnt = loc.count()
                    if cnt > 0:
                        for i in range(cnt):
                            item = loc.nth(i)
                            if item.is_visible():
                                logger.info(f"🔊 Found speaker #{i+1} ('{sel}'). Clicking to trigger audio...")
                                # No force=True: it skips Playwright's wait-for-actionable and
                                # scroll-into-view, so the click lands at a stale point and
                                # silently misses -- the same failure that made the gap-cycle
                                # controls look unclickable earlier.
                                item.click(timeout=3000)
                                clicked_any = True
                                time.sleep(0.8)  # Let audio request fire over network
                    if clicked_any:
                        break  # Found matching selector
                if clicked_any:
                    break
            except Exception as e:
                logger.debug(f"Notice on speaker locator: {e}")

        # Strategy 2: JS evaluate fallback if Strategy 1 did not click
        if not clicked_any:
            for frame in self.get_all_frames():
                try:
                    res = frame.evaluate('''() => {
                        const selectors = [
                            'button.audio', 'button.speaker', 'button.play', '.loudspeaker',
                            '.audio-icon', '.speaker-icon', '.icon-speaker', '.icon-volume-up',
                            '[data-audio]', '[data-sound]', '.audio-player', '.play-btn',
                            'svg.speaker', 'svg.audio', 'i.fa-volume-up', 'i.fa-volume', 'i.icon-speaker'
                        ];
                        
                        const elems = Array.from(document.querySelectorAll(selectors.join(',')));
                        const all = Array.from(document.querySelectorAll('button, div, span, a, svg, i, p, h1, h2, h3'));
                        all.forEach(el => {
                            const txt = (el.innerText || el.textContent || '').trim();
                            if (txt.includes('🔊') || txt.includes('🔉') || txt.includes('LISTEN')) {
                                if (!elems.includes(el)) elems.push(el);
                            }
                        });

                        for (let el of elems) {
                            try {
                                const r = el.getBoundingClientRect();
                                if (r.width > 0 && r.height > 0) {
                                    el.click();
                                    return true; // Click once and exit!
                                }
                            } catch(e) {}
                        }
                        return false;
                    }''')
                    if res:
                        clicked_any = True
                        break
                except Exception:
                    pass

        # Strategy 3: play any HTML5 media directly. Some Speexx exercises expose no
        # clickable speaker at all -- the audio element is driven by script.
        if not clicked_any:
            for frame in self.get_all_frames():
                try:
                    if frame.evaluate('''() => {
                        const media = Array.from(document.querySelectorAll('audio, video'));
                        let played = false;
                        media.forEach(m => { try { m.play(); played = true; } catch (e) {} });
                        return played;
                    }'''):
                        logger.info("🔊 No speaker control found; started HTML5 media directly.")
                        clicked_any = True
                        break
                except Exception:
                    pass

        if clicked_any:
            logger.info("🔊 Speaker/Dialogue button clicked ONCE! Pausing 2.5s to keep transcript window open...")
            time.sleep(2.5)
            return True

        # Nothing was clickable. Log the candidates actually on the page so the next run's
        # log identifies the real control instead of needing another diagnose round --
        # guessing selectors blind has cost several passes already.
        try:
            cands = self.page.evaluate('''() => {
                const out = [];
                const els = Array.from(document.querySelectorAll(
                    'button, a, span, i, svg, div[role="button"], [tabindex]'
                ));
                for (const el of els) {
                    const r = el.getBoundingClientRect();
                    if (r.width <= 0 || r.height <= 0 || r.width > 80 || r.height > 80) continue;
                    const cls = (el.className || '').toString();
                    const label = (el.getAttribute('aria-label') || '') + ' ' +
                                  (el.getAttribute('title') || '') + ' ' +
                                  (el.getAttribute('data-original-title') || '');
                    if (/audio|sound|speak|volume|listen|play|glyph|halfling|icon/i.test(cls + ' ' + label)) {
                        out.push({ tag: el.tagName, cls: cls.substring(0, 90), label: label.trim().substring(0, 60) });
                    }
                }
                return out.slice(0, 12);
            }''')
            if cands:
                logger.warning("🔇 No speaker control matched. Small icon-like candidates on this page:")
                for c in cands:
                    logger.warning(f"     <{c['tag']}> cls='{c['cls']}' label='{c['label']}'")
            else:
                logger.warning("🔇 No speaker control matched, and no icon-like candidates found.")
        except Exception:
            pass

        return False

    def read_grammar_tip(self) -> str:
        """
        Clicks the 'Grammar' / 'Help' / 'Contextual Help' button if present to extract grammar guidance,
        then closes the modal or popover.
        """
        if not self.page:
            return ""
        tip = ""
        help_selectors = [
            'button.instructions-button.help-button',
            '.icon-exercise-contextual-help',
            'button.help-button',
            'button.grammar-button',
            'button:has-text("Grammar")',
            'button:has-text("Help")',
            'a[title*="help" i]',
            'a[title*="grammar" i]'
        ]
        for frame in self.get_all_frames():
            try:
                for sel in help_selectors:
                    btn = frame.locator(sel).first
                    if btn.count() > 0 and btn.is_visible():
                        btn.click(timeout=1500)
                        time.sleep(0.8)
                        modal = frame.locator('.modal-dialog, .grammar-dialog, .modal-content, .popover, .contextual-help, [role="dialog"]:visible').first
                        if modal.count() > 0 and modal.is_visible():
                            tip = modal.inner_text().strip()
                            close_btn = frame.locator('.close, [data-dismiss="modal"], button.close, .close-modal, button:has-text("Close")').first
                            if close_btn.count() > 0 and close_btn.is_visible():
                                close_btn.click(timeout=1500)
                            else:
                                self.page.keyboard.press("Escape")
                            time.sleep(0.3)
                            break
                        else:
                            pop_text = frame.evaluate('''() => {
                                const el = document.querySelector('.popover, .tooltip, .contextual-help-content, .help-content');
                                return el ? el.innerText.trim() : '';
                            }''')
                            if pop_text:
                                tip = pop_text
                                self.page.keyboard.press("Escape")
                                break
                if tip:
                    break
            except Exception as e:
                logger.debug(f"Notice on read_grammar_tip: {e}")
        if tip:
            logger.info(f"📘 Extracted Grammar Tip: {tip[:200]}...")
        return tip

    def click_video_if_present(self) -> bool:
        """
        Detects video players / play buttons on screen (e.g. 'Watch the video...'),
        clicks play ONCE, and saves transcript to self.cached_media_transcript.
        """
        if not self.page:
            return False

        clicked = False
        time.sleep(1.0)

        video_selectors = [
            "button.play", ".vjs-play-control", ".vjs-big-play-button",
            "button[title*='Play']", "div.play-button", "button:has-text('Watch')",
            "video", "iframe[src*='vimeo']", "iframe[src*='youtube']",
            "[class*='video']", "[class*='player']"
        ]

        for frame in self.get_all_frames():
            try:
                for sel in video_selectors:
                    loc = frame.locator(sel)
                    if loc.count() > 0 and loc.first.is_visible():
                        logger.info(f"🎬 Found video play button ('{sel}'). Clicking to play video...")
                        loc.first.click(timeout=3000, force=True)
                        clicked = True
                        break
                if clicked:
                    break
            except Exception:
                pass

        # Try HTML5 <video> play event fallback
        for frame in self.get_all_frames():
            try:
                res = frame.evaluate('''() => {
                    const vids = Array.from(document.querySelectorAll('video'));
                    let played = false;
                    vids.forEach(v => {
                        try { v.play(); played = true; } catch(e) {}
                    });
                    return played;
                }''')
                if res:
                    clicked = True
                    break
            except Exception:
                pass

        # Click 'i' info icon if present to expand transcript text
        self.click_audio_or_speaker_if_present()

        if clicked:
            logger.info("🎬 Video play triggered! Saving media transcript...")
            time.sleep(3.0)
            return True

        return False

    def extract_audio_url(self) -> str:
        """
        Extract the audio/video source URL from the Speexx page using multiple
        robust strategies:
        1. Captured network responses from live Playwright listener
        2. Video.js player JavaScript API (window.videojs)
        3. Browser Performance Resource Timing API
        4. DOM inspection (<audio>, <video>, <source>, [src], [data-sound], [data-src])
        5. Element attribute and JSON search
        6. Active trigger fallback: click Play / Speaker button to force network load, then re-check
        """
        if not self.page:
            return None

        # Strategy 1: Check network intercepted audio URLs
        if self.captured_audio_urls:
            latest_url = self.captured_audio_urls[-1]
            logger.info(f"🔊 Found audio from network interception: {latest_url[:120]}...")
            return latest_url

        def _search_page_dom():
            for frame in self.get_all_frames():
                try:
                    url = frame.evaluate('''() => {
                        // Strategy 2: Video.js global JS API
                        if (window.videojs) {
                            try {
                                if (window.videojs.players) {
                                    for (let k in window.videojs.players) {
                                        const p = window.videojs.players[k];
                                        if (p) {
                                            if (typeof p.currentSrc === 'function' && p.currentSrc()) return p.currentSrc();
                                            if (typeof p.src === 'function' && p.src()) return p.src();
                                            if (p.options_ && p.options_.sources && p.options_.sources.length > 0) {
                                                const s = p.options_.sources[0];
                                                if (s && s.src) return s.src;
                                            }
                                        }
                                    }
                                }
                                if (typeof window.videojs.getAllPlayers === 'function') {
                                    const players = window.videojs.getAllPlayers();
                                    for (let p of players) {
                                        if (typeof p.currentSrc === 'function' && p.currentSrc()) return p.currentSrc();
                                        if (typeof p.src === 'function' && p.src()) return p.src();
                                    }
                                }
                            } catch(e) {}
                        }

                        // Strategy 3: Browser Performance Resource Timing API (all loaded resources)
                        try {
                            const resources = performance.getEntriesByType('resource');
                            for (let i = resources.length - 1; i >= 0; i--) {
                                const name = resources[i].name;
                                if (name.match(/\\.(mp3|mp4|ogg|m4a|wav|webm|aac)($|\\?)/i) || 
                                    (name.includes('/audio/') && !name.match(/\\.(js|css|png|jpg|svg)$/i)) ||
                                    (name.includes('/sound/') && !name.match(/\\.(js|css|png|jpg|svg)$/i)) ||
                                    (name.includes('/speech/') && !name.match(/\\.(js|css|png|jpg|svg)$/i))) {
                                    return name;
                                }
                            }
                        } catch(e) {}

                        // Strategy 4: Video.js tech element
                        const vjsTech = document.querySelector('audio.vjs-tech, video.vjs-tech');
                        if (vjsTech) {
                            if (vjsTech.currentSrc) return vjsTech.currentSrc;
                            if (vjsTech.src) return vjsTech.src;
                            const src = vjsTech.querySelector('source');
                            if (src && src.src) return src.src;
                        }

                        // Strategy 5: Any <audio> or <video> element on the page
                        const mediaList = document.querySelectorAll('audio, video, source');
                        for (let media of mediaList) {
                            if (media.currentSrc) return media.currentSrc;
                            if (media.src) return media.src;
                            const s = media.getAttribute('src') || media.getAttribute('data-src');
                            if (s && s.length > 5) return s;
                        }

                        // Strategy 6: Scan all elements for data-sound / data-audio / data-url attributes
                        const withData = document.querySelectorAll('[data-sound], [data-audio], [data-src], [data-url], [data-audio-url], .loudspeaker, [class*="speaker"], [class*="audio"]');
                        for (let el of withData) {
                            for (let attr of el.attributes) {
                                const val = (attr.value || '').trim();
                                if (val.match(/\\.(mp3|mp4|ogg|m4a|wav|webm|aac)($|\\?)/i) || val.includes('/audio/') || val.includes('/sound/')) {
                                    return val;
                                }
                            }
                        }

                        // Strategy 7: Global JavaScript Speexx object search
                        for (let k of Object.keys(window)) {
                            if (k.toLowerCase().includes('speexx') || k.toLowerCase().includes('exercise') || k.toLowerCase().includes('player') || k.toLowerCase().includes('article')) {
                                try {
                                    const str = JSON.stringify(window[k]);
                                    const match = str.match(/https?:[\\/]+[^"\'\\s]+\\.(mp3|mp4|ogg|m4a|wav)/i);
                                    if (match) {
                                        return match[0];
                                    }
                                } catch(e) {}
                            }
                        }

                        return null;
                    }''')
                    if url:
                        return url
                except Exception as e:
                    logger.debug(f"Notice on DOM audio search: {e}")
            return None

        # First search attempt
        found_url = _search_page_dom()
        if found_url:
            logger.info(f"🔊 Found audio URL via DOM/API: {found_url[:120]}...")
            return found_url

        # Strategy 8 (Active Trigger): If not found, click Play or loudspeaker to force Video.js to load the audio
        logger.info("🔊 Audio URL not found yet. Clicking Play / Loudspeaker to trigger media stream loading...")
        for frame in self.get_all_frames():
            try:
                frame.evaluate('''() => {
                    // Try clicking Video.js play button
                    const playBtn = document.querySelector('.vjs-play-control, .vjs-big-play-button, button.play');
                    if (playBtn) playBtn.click();

                    // Try clicking any speaker / loudspeaker icon
                    const speakers = document.querySelectorAll('.loudspeaker, [class*="speaker"], button:has-text("🔊")');
                    for (let s of speakers) s.click();
                }''')
            except Exception:
                pass

        time.sleep(2.0)

        # Check network interceptor again after triggering play
        if self.captured_audio_urls:
            latest_url = self.captured_audio_urls[-1]
            logger.info(f"🔊 Found audio after triggering Play: {latest_url[:120]}...")
            return latest_url

        # Check DOM again after triggering play
        found_url = _search_page_dom()
        if found_url:
            logger.info(f"🔊 Found audio URL after triggering Play: {found_url[:120]}...")
            return found_url

        logger.warning("No audio source URL found on this page after all strategies.")
        return None

    def download_audio(self, url: str) -> bytes:
        """
        Download an audio file from the given URL using the browser session's
        cookies for authentication, or return cached bytes if already intercepted.

        Args:
            url: The audio file URL (typically from extract_audio_url()).

        Returns:
            Raw audio bytes, or None if the download failed.
        """
        import requests as req

        if not url:
            return None

        # If already intercepted and cached from the network layer, return directly!
        if url in self.captured_audio_bytes:
            data = self.captured_audio_bytes[url]
            logger.info(f"✅ Using network-cached audio ({len(data):,} bytes)")
            return data

        # Grab cookies from the live Playwright browser context so the
        # download request authenticates against Speexx the same way the
        # browser tab does.
        cookies = {}
        try:
            for c in self.context.cookies():
                cookies[c['name']] = c['value']
        except Exception as e:
            logger.debug(f"Could not read browser cookies: {e}")

        logger.info(f"⬇️  Downloading audio ({len(cookies)} cookies)...")

        try:
            # Fix relative URLs if needed
            full_url = url
            if url.startswith('/'):
                from urllib.parse import urljoin
                full_url = urljoin(self.page.url, url)

            resp = req.get(full_url, cookies=cookies, timeout=30, headers={
                'Referer': self.page.url,
                'User-Agent': self.page.evaluate('() => navigator.userAgent'),
            })
            if resp.status_code == 200:
                logger.info(f"✅ Audio downloaded: {len(resp.content):,} bytes")
                return resp.content
            else:
                logger.error(f"Audio download failed: HTTP {resp.status_code}")
                return None
        except Exception as e:
            logger.error(f"Audio download error: {e}")
            return None

    def extract_page_exercise_data(self) -> dict:
        """
        Scrapes visible page text and dialogue popovers across ALL frames.
        """
        if not self.page:
            return {"error": "Browser not initialized"}

        combined_text = ""
        tf_statements = []
        cycle_defs = []
        draggable_words = []
        choices = []
        gap_sentences = []
        num_drop_slots = 0
        num_inputs = 0
        num_gaps = 0
        act_type = "standard"
        speexx_type_class = ""
        scrambled_sentences = []
        table_rows = []
        choice_options = []
        drag_sentences = []
        num_placeholders = 0
        text_inputs = []
        markable_words = []
        mark_instruction = ''
        picture_items = []
        dictation_items = []
        example_text = ''
        exercise_instruction = ''

        for frame in self.get_all_frames():
            try:
                frame_data = frame.evaluate('''() => {
                    // Speexx tags the exercise container with its own authoritative
                    // "type-xxx" class (e.g. type-toggle-solution, type-drag-drop,
                    // type-scrambled-sentence). Read it FIRST so classification below
                    // can trust it instead of guessing from text heuristics.
                    let speexxTypeClass = '';
                    const exerciseEl = document.querySelector('.exercise[class*="type-"], [class*="type-"]');
                    if (exerciseEl) {
                        const match = (exerciseEl.className || '').toString().match(/type-[a-z0-9-]+/);
                        if (match) speexxTypeClass = match[0];
                    }

                    let text = document.body ? (document.body.innerText || '') : '';

                    // Extract text from popovers, modals, tooltips, dialogue boxes, transcripts
                    const popups = Array.from(document.querySelectorAll(
                        '.popover, .modal, .dialogue, .transcript, [role="dialog"], [role="tooltip"], .tooltip, .popover-content, .modal-body, .info-content, .transcript-box, div[class*="transcript"], div[class*="dialogue"], div[class*="info-content"]'
                    ));
                    
                    let popupText = "";
                    popups.forEach(p => {
                        const t = p.innerText ? p.innerText.trim() : '';
                        if (t && !text.includes(t)) {
                            popupText += '\\n' + t;
                        }
                    });

                    if (popupText) {
                        text = "[AUDIO/VIDEO DIALOGUE TRANSCRIPT]:\\n" + popupText + "\\n\\n[EXERCISE CONTENT]:\\n" + text;
                    }

                    const textLower = text.toLowerCase();

                    // Video Intro detection
                    const isVideoIntro = textLower.includes('watch the video') || 
                                         (document.querySelectorAll('video').length > 0 && document.querySelectorAll('input, button.choice').length <= 1);

                    // True / False statements
                    const isTF = textLower.includes('true or false') || 
                                 (document.querySelectorAll('input[type="radio"]').length >= 4);
                    
                    let tfList = [];
                    if (isTF) {
                        const rows = Array.from(document.querySelectorAll('tr, li, .statement-row, .question-row, p, div'));
                        rows.forEach(r => {
                            const t = r.innerText ? r.innerText.trim() : '';
                            if (t.length > 10 && (t.toLowerCase().includes('true') || t.toLowerCase().includes('false')) && !t.includes('Click on')) {
                                const cleanText = t.replace(/\\b(true|false)\\b/gi, '').replace(/\\n/g, ' ').trim();
                                if (cleanText && cleanText.length > 5 && !tfList.includes(cleanText)) {
                                    tfList.push(cleanText);
                                }
                            }
                        });
                    }

                    // Cycle blanks
                    const isCycle = textLower.includes('click on the blanks') || textLower.includes('cycle') || 
                                     document.querySelectorAll('.cycle-blank, button.gap, span.gap, [data-blank]').length > 0;
                    let cycleList = [];
                    if (isCycle) {
                        const lines = text.split('\\n');
                        lines.forEach(l => {
                            const trimmed = l.trim();
                            if (trimmed.length > 5 && !trimmed.toLowerCase().includes('click on') && 
                                !trimmed.toLowerCase().includes('select the answer') && 
                                !trimmed.toLowerCase().includes('pair me up') &&
                                !trimmed.toLowerCase().includes('exercise')) {
                                cycleList.push(trimmed);
                            }
                        });
                    }

                    // Words & Slots
                    // '.drag-drop[data-drag-drop-id]' is Speexx's real word-bank chip class
                    // (jQuery-UI draggable, NOT the native draggable="true" attribute).
                    // VISIBLE chips only. The hidden responsive copies used to be removed
                    // later by de-duplicating the list, but that also deleted words a bank
                    // legitimately repeats (two blanks needing the same word), leaving the
                    // model with fewer words than blanks.
                    const wordChips = Array.from(document.querySelectorAll(
                        '.drag-drop[data-drag-drop-id], .word-chip, .draggable, [draggable="true"], .word-bank-item, .drag-word, .option-chip, .chip'
                    )).filter(el => el.offsetParent !== null && el.getBoundingClientRect().width > 0)
                      .map(el => {
                        const txt = (el.innerText || el.textContent || '').trim();
                        return txt.length > 0 ? txt : '[empty]';
                    });

                    // '.drag-drop-placeholder' is Speexx's real blank-slot class for type-drag-drop.
                    const dropSlots = Array.from(document.querySelectorAll(
                        '.drag-drop-placeholder, .drop-target, .droppable, .drop-slot, .sentence-gap, [data-gap]'
                    ));

                    // DRAG-DROP blank sentences (type-drag-drop): rebuild each sentence with
                    // its blanks marked as globally-numbered ___(n) markers, so the AI is told
                    // exactly how many blanks there are and in what order -- the old approach
                    // guessed prompts by keyword-filtering page_text and produced the wrong count.
                    let dragSentences = [];
                    // Count only VISIBLE placeholders: Speexx renders duplicate hidden copies
                    // of the exercise for responsive breakpoints (same reason the exercise-icon
                    // nav shows up 3x), so a raw querySelectorAll count is inflated (28 vs 14).
                    let numPlaceholders = Array.from(document.querySelectorAll('.drag-drop-placeholder'))
                        .filter(el => el.offsetParent !== null && el.getBoundingClientRect().width > 0).length;
                    if (numPlaceholders > 0) {
                        // Use the VISIBLE items container. Speexx renders hidden responsive
                        // copies, and walking those counted every blank twice -- producing
                        // "___(1) ___(2)" for a single gap and telling the model there were
                        // 14 blanks for 7 words, which it cannot answer at all.
                        const isShown = (el) => el.offsetParent !== null
                            && el.getBoundingClientRect().width > 0;
                        const exerciseItems = Array.from(document.querySelectorAll('.exercise-items'))
                            .filter(isShown)[0] || document.querySelector('.exercise-items');
                        if (exerciseItems) {
                            let blankNo = 0;
                            Array.from(exerciseItems.querySelectorAll('.item'))
                                .filter(isShown)
                                .forEach(item => {
                                let s = '';
                                const walk = (node) => {
                                    Array.from(node.childNodes).forEach(ch => {
                                        if (ch.nodeType === 3) {
                                            s += ch.textContent;
                                        } else if (ch.nodeType === 1) {
                                            const cl = ch.classList || { contains: () => false };
                                            // An empty slot, or a slot already holding a dropped
                                            // chip -- both represent one answer position.
                                            if (cl.contains('drag-drop-placeholder') || cl.contains('drag-drop')) {
                                                if (!isShown(ch)) return;   // skip hidden twin
                                                blankNo += 1;
                                                s += ' ___(' + blankNo + ') ';
                                            } else {
                                                walk(ch);
                                            }
                                        }
                                    });
                                };
                                walk(item);
                                s = s.replace(/\\s+/g, ' ').trim();
                                if (s.includes('___')) dragSentences.push(s);
                            });
                        }
                    }

                    // Global exercise instruction from header/prompt container
                    let exerciseInstruction = '';
                    const instrEl = document.querySelector('#exercise-instructions, .instructions-text, .instructions, .exercise-instructions-container, .exercise-header, .instruction');
                    if (instrEl) exerciseInstruction = (instrEl.innerText || '').trim();

                    // CHOICE LIST (type-multiple-choice): checkbox / radio options.
                    // Grouped by .item so multiple questions on the same page each get their own prompt.
                    let choiceOptions = [];
                    const choiceRoot = document.querySelector('.exercise-items');
                    if (choiceRoot) {
                        Array.from(choiceRoot.querySelectorAll('.item')).forEach((item, itemIdx) => {
                            // Find prompt specifically inside item, BUT NOT inside any label.choice-option
                            let promptText = '';
                            const promptCandidate = Array.from(item.querySelectorAll('.text, .exercise-prompt, h2, h3, h4, p, .prompt'))
                                .find(el => !el.closest('label.choice-option'));
                            if (promptCandidate) {
                                promptText = (promptCandidate.innerText || '').trim();
                            }
                            if (!promptText && exerciseInstruction) {
                                promptText = exerciseInstruction;
                            }
                            Array.from(item.querySelectorAll('label.choice-option')).forEach(lab => {
                                const inp = lab.querySelector('input.choice, input[type="checkbox"], input[type="radio"]');
                                const lbl = lab.querySelector('.choice-option-label, .text');
                                choiceOptions.push({
                                    id: inp ? inp.getAttribute('data-choice-option-id') : '',
                                    text: (lbl ? lbl.innerText : lab.innerText || '').trim(),
                                    checked: inp ? !!inp.checked : false,
                                    type: inp ? (inp.type || '') : '',
                                    // For both checkboxes and radio buttons within an .item, group by item-${itemIdx}
                                    // Speexx gives unique choice-group-1000, 1001 to each checkbox which MUST NOT split them!
                                    group: `item-${itemIdx}`,
                                    prompt: promptText
                                });
                            });
                        });
                    }
                    if (choiceOptions.length === 0) {
                        choiceOptions = Array.from(
                            document.querySelectorAll('label.choice-option')
                        ).filter(el => el.offsetParent !== null).map((lab, i) => {
                            const inp = lab.querySelector('input.choice, input[type="checkbox"], input[type="radio"]');
                            const lbl = lab.querySelector('.choice-option-label, .text');
                            return {
                                id: inp ? inp.getAttribute('data-choice-option-id') : '',
                                text: (lbl ? lbl.innerText : lab.innerText || '').trim(),
                                checked: inp ? !!inp.checked : false,
                                type: inp ? (inp.type || '') : '',
                                group: 'item-0',
                                prompt: exerciseInstruction || ''
                            };
                        }).filter(o => o.text);
                    }

                    // SCRAMBLED TABLE (type-scrambled-table): each .item pairs a FIXED prompt
                    // on the left with a draggable answer cell on the right. The cells are
                    // already all present -- they just sit against the wrong prompts and have
                    // to be reordered, so there is no word bank and nothing to "fill in".
                    let tableRows = [];
                    const itemsRoot = document.querySelector('.exercise-items');
                    if (itemsRoot) {
                        Array.from(itemsRoot.querySelectorAll('.item')).forEach(item => {
                            const cell = item.querySelector('.scrambled-cell[data-scrambled-cell-id]');
                            if (!cell) return;
                            const promptEl = item.querySelector('.text');
                            tableRows.push({
                                prompt: promptEl ? (promptEl.innerText || '').trim() : '',
                                cell_text: (cell.innerText || '').trim(),
                                cell_id: cell.getAttribute('data-scrambled-cell-id')
                            });
                        });
                    }

                    // SCRAMBLED SENTENCE exercise detection (type-scrambled-sentence):
                    // each .scrambled-sentence holds .scrambled-block chips in CURRENT (scrambled) order.
                    // Visible-only, so these indices line up with the ones
                    // solve_scrambled_sentences() drags against.
                    const scrambledSentenceEls = Array.from(document.querySelectorAll('.scrambled-sentence'))
                        .filter(el => el.offsetParent !== null);
                    const scrambledSentences = scrambledSentenceEls.map(sentEl =>
                        Array.from(sentEl.querySelectorAll('.scrambled-block'))
                             .map(b => (b.innerText || b.textContent || '').trim())
                             .filter(t => t.length > 0)
                    ).filter(words => words.length > 0);

                    const choiceElems = Array.from(document.querySelectorAll('input[type="radio"], label.choice, button.choice, .option-button, div[role="radio"]'))
                                             .map(el => el.innerText.trim())
                                             .filter(t => t.length > 0 && t.length < 100);

                    const inputs = document.querySelectorAll('input[type="text"], textarea, [contenteditable="true"]');

                    // GAP-FILL exercise detection (Speexx toggle-solution type)
                    const gapEls = Array.from(document.querySelectorAll('span.gap, .gap.form-control, [data-gap], .gap-container'));
                    let numGaps = document.querySelectorAll('span.gap, .gap.form-control, [data-gap]').length || gapEls.length;
                    let gapSentences = [];
                    if (numGaps > 0) {
                        const items = Array.from(document.querySelectorAll('.exercise-items .item, .exercise-content .item, .exercise .item, .item'))
                            .filter(el => el.querySelector('.gap-container, .gap, span.gap'));
                        if (items.length > 0) {
                            items.forEach((item, idx) => {
                                const clone = item.cloneNode(true);
                                clone.querySelectorAll('.gap-container, .gap, span.gap').forEach(g => {
                                    const ph = document.createTextNode(' ___ ');
                                    if (g.parentNode) g.parentNode.replaceChild(ph, g);
                                });
                                clone.querySelectorAll('.speaker, .icon, button, .input-group-addon, .halflings-icon').forEach(el => el.remove());
                                const s = clone.innerText.replace(/\\s+/g, ' ').trim();
                                if (s.includes('___') && s.length > 5) {
                                    gapSentences.push(s);
                                }
                            });
                        }
                    }

                    // TYPED FILL-IN-THE-BLANKS (Every input element gets its own slot)
                    let textInputs = [];
                    const allInpEls = Array.from(document.querySelectorAll('.exercise-items input.answer, .exercise-items input[type="text"], .exercise-content input.answer, .exercise-content input[type="text"], input.answer, input[type="text"]:not([class*="search"])'));
                    if (allInpEls.length > 0) {
                        allInpEls.forEach((inp, idx) => {
                            const item = inp.closest('.item, .statement-row, li, tr, p, .exercise-content') || inp.parentElement;
                            let beforeInp = [];
                            let afterInp = [];
                            let seenInp = false;

                            function walk(node) {
                                if (node === inp) {
                                    seenInp = true;
                                    return;
                                }
                                if (node.nodeType === 3) { // Node.TEXT_NODE
                                    const t = (node.textContent || '').trim();
                                    if (t) {
                                        if (!seenInp) beforeInp.push(t);
                                        else afterInp.push(t);
                                    }
                                } else if (node.nodeType === 1) { // Node.ELEMENT_NODE
                                    if (node.tagName && ['SCRIPT', 'STYLE'].includes(node.tagName)) return;
                                    Array.from(node.childNodes).forEach(child => walk(child));
                                }
                            }

                            if (item) walk(item);

                            const prefixText = beforeInp.join(' ').replace(/\\s+/g, ' ').trim();
                            const suffixText = afterInp.join(' ').replace(/\\s+/g, ' ').trim();
                            const fullPrompt = `${prefixText} ___ ${suffixText}`.trim();

                            const spk = item ? item.querySelector('button.speaker, [class*="speaker"], [class*="audio-button"], .btn-speaker, button:has(.icon-speaker), .loudspeaker, [class*="volume"]') : null;
                            textInputs.push({
                                index: idx,
                                prompt: fullPrompt,
                                prefix: prefixText,
                                suffix: suffixText,
                                max_length: inp.getAttribute('maxlength') || '',
                                answer_id: inp.getAttribute('data-answer-id') || inp.getAttribute('name') || '',
                                has_speaker: spk !== null
                            });
                        });
                    }

                    // EXAMPLE TEXT extraction (for transformation / rewrite exercises)
                    let exampleText = '';
                    const exampleItem = document.querySelector('.exercise-items .item:not(:has(input)), .exercise-items p:not(:has(input)), .exercise-content .example');
                    if (exampleItem) {
                        exampleText = (exampleItem.innerText || '').trim();
                    }

                    // PICTURE MATCHING (images with drop placeholders)
                    let pictureItems = [];
                    const picItemEls = Array.from(document.querySelectorAll('.exercise-items .item, .layout-grid-trio .item, .picture-item'));
                    picItemEls.forEach((item, idx) => {
                        const img = item.querySelector('img, .picture img');
                        const placeholder = item.querySelector('.drag-drop-placeholder, .drop-target, .image-slot');
                        if (img && placeholder) {
                            const src = img.getAttribute('src') || '';
                            const alt = img.getAttribute('alt') || '';
                            pictureItems.push({
                                index: idx,
                                img_src: src,
                                img_alt: alt
                            });
                        }
                    });

                    // TYPE-MARK-TEXT (Click to highlight words)
                    let markableWords = [];
                    let markInstruction = '';
                    const markInstrEl = document.querySelector('.exercise-instructions-container, .exercise-header, .instruction');
                    if (markInstrEl) markInstruction = markInstrEl.innerText.trim();
                    const wordSpans = Array.from(document.querySelectorAll('.exercise-container .word, .type-mark-text .word, .markable-word'));
                    wordSpans.forEach((w, idx) => {
                        const txt = (w.innerText || '').trim();
                        const isMarked = w.closest('.mark-text') !== null || w.classList.contains('marked');
                        if (txt) {
                            markableWords.push({
                                index: idx,
                                text: txt,
                                is_marked: isMarked
                            });
                        }
                    });

                    // AUDIO DICTATION ("Write down what you hear" with per-row speakers)
                    let dictationItems = [];
                    const dictEls = Array.from(document.querySelectorAll('.exercise-items .item, .dictation-item, .statement-row, li, tr'));
                    dictEls.forEach((item, idx) => {
                        const spk = item.querySelector('button.speaker, [class*="speaker"], [class*="audio-button"], .btn-speaker');
                        const inp = item.querySelector('input[type="text"], input.answer, input:not([type="radio"]):not([type="checkbox"]):not([type="hidden"])');
                        if (spk && inp) {
                            dictationItems.push({
                                index: idx,
                                has_speaker: true
                            });
                        }
                    });

                    const isPronunciation = window.location.href.includes('pronunciation') || textLower.includes('repeat the sentence') || textLower.includes('microphone symbol') || textLower.includes('pronunciation training') || textLower.includes('click on the microphone');
                    const isAudioDictationText = (textLower.includes('what you hear') || textLower.includes('write what you hear') || textLower.includes('write down what you hear')) && !textLower.includes('missing words') && !textLower.includes('in the blanks');
                    let activity = 'standard';
                    if (isPronunciation) {
                        activity = 'pronunciation';
                    } else if (pictureItems.length > 0 && wordChips.length > 0) {
                        activity = 'picture_matching';
                    } else if ((speexxTypeClass === 'type-multiple-choice' || speexxTypeClass === 'type-single-choice' || choiceOptions.length > 0) && choiceOptions.length > 0 && textInputs.length === 0) {
                        activity = 'choice_list';
                    } else if (textInputs.length > 0 && (textLower.includes('missing words') || textLower.includes('in the blanks'))) {
                        activity = 'fill_in_blanks';
                    } else if (dictationItems.length > 0 || (isAudioDictationText && inputs.length > 0)) {
                        activity = 'audio_dictation';
                    } else if (textInputs.length > 0 && (speexxTypeClass === 'type-fill-in-blanks' || textLower.includes('form sentences') || textLower.includes('type') || textLower.includes('verbs in parentheses'))) {
                        activity = 'fill_in_blanks';
                    } else if (speexxTypeClass === 'type-scrambled-table') {
                        activity = 'scrambled_table';
                    } else if (speexxTypeClass === 'type-scrambled-sentence') {
                        activity = 'scrambled_sentence';
                    } else if (speexxTypeClass === 'type-drag-drop') {
                        activity = 'drag_drop';
                    } else if (speexxTypeClass === 'type-mark-text' || (markableWords.length > 0 && textLower.includes('highlight'))) {
                        activity = 'mark_text';
                    } else if (speexxTypeClass === 'type-toggle-solution' || numGaps > 0) {
                        activity = 'gap_fill';
                    } else {
                        if (textInputs.length > 0) activity = 'fill_in_blanks';
                        else if (isVideoIntro && !isTF && !isCycle && wordChips.length === 0) activity = 'video_intro';
                        else if (isTF) activity = 'true_false';
                        else if (numGaps > 0 && gapSentences.length > 0) activity = 'gap_fill';
                        else if (isCycle) activity = 'cycle_blanks';
                        else if (wordChips.length > 0 || dropSlots.length > 0) activity = 'drag_drop';
                        else if (choiceElems.length > 0) activity = 'multiple_choice';
                    }

                    return {
                        text: text,
                        activity: activity,
                        tf_statements: tfList,
                        cycle_defs: cycleList,
                        draggable_words: wordChips,
                        num_drop_slots: dropSlots.length,
                        choices: choiceElems,
                        num_inputs: inputs.length,
                        gap_sentences: gapSentences,
                        num_gaps: numGaps,
                        drag_sentences: dragSentences,
                        num_placeholders: numPlaceholders,
                        scrambled_sentences: scrambledSentences,
                        table_rows: tableRows,
                        choice_options: choiceOptions,
                        text_inputs: textInputs,
                        markable_words: markableWords,
                        mark_instruction: markInstruction,
                        exercise_instruction: exerciseInstruction,
                        picture_items: pictureItems,
                        dictation_items: dictationItems,
                        speexx_type_class: speexxTypeClass,
                        example_text: exampleText
                    };
                }''')

                if frame_data.get("text"):
                    combined_text += "\n" + frame_data.get("text")
                if frame_data.get("tf_statements"):
                    tf_statements.extend(frame_data.get("tf_statements"))
                if frame_data.get("cycle_defs"):
                    cycle_defs.extend(frame_data.get("cycle_defs"))
                if frame_data.get("draggable_words"):
                    draggable_words.extend(frame_data.get("draggable_words"))
                if frame_data.get("choices"):
                    choices.extend(frame_data.get("choices"))
                if frame_data.get("gap_sentences"):
                    gap_sentences.extend(frame_data.get("gap_sentences"))
                if frame_data.get("scrambled_sentences"):
                    scrambled_sentences.extend(frame_data.get("scrambled_sentences"))
                if not table_rows and frame_data.get("table_rows"):
                    table_rows = frame_data.get("table_rows")
                if not choice_options and frame_data.get("choice_options"):
                    choice_options = frame_data.get("choice_options")
                if not drag_sentences and frame_data.get("drag_sentences"):
                    drag_sentences = frame_data.get("drag_sentences")
                if not text_inputs and frame_data.get("text_inputs"):
                    text_inputs = frame_data.get("text_inputs")
                if not markable_words and frame_data.get("markable_words"):
                    markable_words = frame_data.get("markable_words")
                if not mark_instruction and frame_data.get("mark_instruction"):
                    mark_instruction = frame_data.get("mark_instruction")
                if not exercise_instruction and frame_data.get("exercise_instruction"):
                    exercise_instruction = frame_data.get("exercise_instruction")
                if frame_data.get("picture_items"):
                    picture_items.extend(frame_data.get("picture_items"))
                if frame_data.get("dictation_items"):
                    dictation_items.extend(frame_data.get("dictation_items"))
                if not example_text and frame_data.get("example_text"):
                    example_text = frame_data.get("example_text")
                num_placeholders += frame_data.get("num_placeholders", 0)
                num_drop_slots += frame_data.get("num_drop_slots", 0)
                num_inputs += frame_data.get("num_inputs", 0)
                num_gaps += frame_data.get("num_gaps", 0)

                if frame_data.get("activity") != "standard":
                    act_type = frame_data.get("activity")

                if not speexx_type_class and frame_data.get("speexx_type_class"):
                    speexx_type_class = frame_data.get("speexx_type_class")

            except Exception as e:
                logger.debug(f"Frame evaluation notice in extract_page_exercise_data: {e}")

        # Strip UI chrome (button labels, nav text) that leaks into scraped text.
        def _drop_ui_words(items: list) -> list:
            return [t for t in items if t.strip().lower() not in EXCLUDED_UI_WORDS]

        tf_statements = _drop_ui_words(tf_statements)
        cycle_defs = _drop_ui_words(cycle_defs)

        # Deduplicate statements & options
        tf_statements = list(dict.fromkeys(tf_statements))
        cycle_defs = list(dict.fromkeys(cycle_defs))
        # NOT de-duplicated: the chips are already visible-filtered, and a word bank
        # may legitimately offer the same word for two different blanks.
        choices = list(dict.fromkeys(choices))
        gap_sentences = list(dict.fromkeys(gap_sentences))

        return {
            "page_text": combined_text[:8000].strip(),
            "activity_type": act_type,
            "tf_statements": tf_statements,
            "cycle_defs": cycle_defs,
            "draggable_words": draggable_words,
            "num_drop_slots": num_drop_slots,
            "choices": choices,
            "num_inputs": num_inputs,
            "gap_sentences": gap_sentences,
            "num_gaps": num_gaps,
            "drag_sentences": drag_sentences,
            "num_placeholders": num_placeholders,
            "scrambled_sentences": scrambled_sentences,
            "table_rows": table_rows,
            "choice_options": choice_options,
            "text_inputs": text_inputs,
            "markable_words": markable_words,
            "mark_instruction": mark_instruction,
            "exercise_instruction": exercise_instruction,
            "picture_items": picture_items,
            "dictation_items": dictation_items,
            "speexx_type_class": speexx_type_class,
            "example_text": example_text,
            "url": self.page.url
        }

    def _click_gap_by_index(self, frame, index: int) -> bool:
        """
        Advances the Nth Speexx toggle-solution gap to its next candidate answer.

        IMPORTANT: '.gap.form-control' is only the DISPLAY box -- it has no click handler.
        The actual cycle control is its sibling '.input-group-addon' (the small refresh
        icon rendered to the right of the box) inside the shared '.input-group' wrapper:

            <div class="gap-container">
              <div class="input-group">
                <span class="gap form-control">&nbsp;</span>   <- display only
                <span class="input-group-addon">...</span>     <- the real button
              </div>
            </div>

        Clicking the display span is a silent no-op, which is why earlier attempts
        discovered zero options. Target the addon, and fall back to the gap itself only
        if no addon exists (in case other Speexx skins render the gap as the button).
        """
        try:
            return bool(frame.evaluate('''(idx) => {
                const gaps = document.querySelectorAll('span.gap, .gap.form-control, [data-gap]');
                if (idx >= gaps.length) return false;
                const gap = gaps[idx];
                gap.scrollIntoView({ block: 'center', inline: 'center' });

                const group = gap.closest('.input-group') || gap.closest('.gap-container') || gap.parentElement;
                const addon = group ? group.querySelector('.input-group-addon') : null;
                const target = addon || gap;

                // Some jQuery widgets bind to mousedown/mouseup rather than click,
                // so dispatch the full press sequence, then a plain click().
                const opts = { bubbles: true, cancelable: true, view: window };
                target.dispatchEvent(new MouseEvent('mousedown', opts));
                target.dispatchEvent(new MouseEvent('mouseup', opts));
                target.click();
                return true;
            }''', index))
        except Exception as e:
            logger.debug(f"Notice on _click_gap_by_index: {e}")
            return False

    def fill_gaps_by_cycling(self, target_words: list) -> bool:
        """
        For Speexx toggle-solution / gap-fill exercises:
        Cycles each gap element on screen until its displayed text matches target_words[i].
        """
        if not self.page:
            return False

        logger.info(f"Filling {len(target_words)} gap(s) by cycling choices: {target_words}")

        for frame in self.get_all_frames():
            try:
                gaps = frame.locator('span.gap, .gap.form-control, [data-gap]')
                count = gaps.count()
                if count == 0:
                    continue

                logger.info(f"Found {count} gap elements in frame.")

                attempted = 0
                matched_count = 0
                for i in range(min(count, len(target_words))):
                    target = target_words[i]
                    if not target:
                        logger.warning(f"  Gap #{i+1} has no target word (empty AI answer) -- leaving unfilled.")
                        continue
                    attempted += 1
                    clean_target = " ".join(target.split()).lower()
                    gap = gaps.nth(i)

                    matched = False
                    for attempt in range(18):
                        # EXACT match only (whitespace-normalised). Substring matching used
                        # to stop early on a prefix option -- e.g. target "up with" would
                        # "match" the option "up" and leave the wrong preposition in place.
                        # The AI is constrained to pick one of the discovered options, so
                        # the exact string is always reachable by cycling.
                        current_text = " ".join(gap.inner_text().split()).strip().lower()
                        if current_text and current_text == clean_target:
                            logger.info(f"  Gap #{i+1} matched target '{target}' (current text: '{current_text}')")
                            matched = True
                            break
                        self._click_gap_by_index(frame, i)
                        time.sleep(0.15)

                    if matched:
                        matched_count += 1
                    else:
                        logger.warning(f"  Gap #{i+1} reached max clicks without exact match for '{target}' (last text: '{gap.inner_text().strip()}')")

                # SAFETY GUARD: only report success if at least one gap was actually
                # matched -- otherwise every target was empty/unmatched and the caller
                # would silently advance to Next without filling anything.
                if attempted == 0:
                    logger.error("[SAFETY GUARD] Every AI answer was empty -- no gaps attempted. Aborting.")
                    return False
                if matched_count == 0:
                    logger.error("[FAILURE] 0/%d gaps matched! Aborting submission." % attempted)
                    return False

                logger.info(f"Matched {matched_count}/{attempted} gaps.")
                return True
            except Exception as e:
                logger.warning(f"Notice on fill_gaps_by_cycling: {e}")

        return False

    def discover_gap_options(self, max_cycles: int = 15) -> list:
        """
        For Speexx toggle-solution gaps, the candidate words are NOT visible in the DOM
        up front -- each '.gap.form-control' is a click-to-cycle control that only reveals
        one candidate at a time. Clicks through each gap's full cycle to enumerate every
        distinct option it offers (stopping once a value repeats, i.e. the cycle wrapped
        around, or max_cycles is hit), so the AI can be asked to pick from REAL choices
        instead of inventing free-text answers with no grounding.
        Returns a list of option-lists, one per gap, in DOM order. Leaves each gap at
        whatever option it last landed on -- fill_gaps_by_cycling() cycles it further to
        reach the desired target afterward.
        """
        if not self.page:
            return []

        for frame in self.get_all_frames():
            try:
                gaps = frame.locator('span.gap, .gap.form-control, [data-gap]')
                count = gaps.count()
                if count == 0:
                    continue

                all_options = []
                for i in range(count):
                    gap = gaps.nth(i)
                    seen = []
                    for _ in range(max_cycles):
                        # Read BEFORE clicking so the value the gap currently shows is
                        # recorded (including the very first one), then advance. Reading
                        # only after a click drops the initial state and makes the
                        # wrap-around check unreliable.
                        current = gap.inner_text().strip()
                        if current:
                            if current in seen:
                                break  # cycled back to a known option -> full loop seen
                            seen.append(current)
                        self._click_gap_by_index(frame, i)
                        time.sleep(0.15)
                    all_options.append(seen)
                    logger.info(f"  Discovered {len(seen)} option(s) for gap #{i+1}: {seen}")

                return all_options
            except Exception as e:
                logger.warning(f"Notice on discover_gap_options: {e}")

        return []

    def solve_true_false(self, tf_answers: list) -> bool:
        """
        Answers True/False statement list on screen across ALL frames.
        tf_answers: list of dicts [{'statement_index': 0, 'answer': 'true'|'false'}]
        """
        if not self.page:
            return False

        try:
            logger.info(f"Executing True/False clicks for {len(tf_answers)} statements...")

            for item in tf_answers:
                idx = item.get("statement_index", 0)
                ans = str(item.get("answer", "true")).lower().strip()

                for frame in self.get_all_frames():
                    try:
                        res = frame.evaluate('''({statementIdx, answerStr}) => {
                            const rows = Array.from(document.querySelectorAll('tr, li, .statement-row, .question-row, div.row, div.statement, p'));
                            const validRows = rows.filter(r => {
                                return r.querySelectorAll('input[type="radio"], button, label').length >= 2;
                            });

                            if (statementIdx < validRows.length) {
                                const row = validRows[statementIdx];
                                const clickables = Array.from(row.querySelectorAll('label, input[type="radio"], button, span'));
                                for (let c of clickables) {
                                    const txt = (c.innerText || c.value || '').toLowerCase().trim();
                                    if (txt === answerStr || (answerStr === 'true' && txt.includes('true')) || (answerStr === 'false' && txt.includes('false'))) {
                                        c.click();
                                        return true;
                                    }
                                }
                            }

                            // Global radio pairs fallback
                            const radios = Array.from(document.querySelectorAll('input[type="radio"]'));
                            const targetRadioIdx = statementIdx * 2 + (answerStr === 'false' ? 1 : 0);
                            if (targetRadioIdx < radios.length) {
                                radios[targetRadioIdx].click();
                                if (radios[targetRadioIdx].parentElement) radios[targetRadioIdx].parentElement.click();
                                return true;
                            }
                            return false;
                        }''', {"statementIdx": idx, "answerStr": ans})

                        if res:
                            logger.info(f"Clicked True/False [{ans.upper()}] for statement #{idx+1} in frame.")
                            break
                    except Exception:
                        pass

                time.sleep(random.uniform(0.4, 0.8))

            return True
        except Exception as e:
            logger.error(f"Failed solving True/False questions: {e}")
            return False

    def solve_cycle_blanks(self, target_words: list) -> bool:
        """
        Clicks each blank button repeatedly until the displayed word inside blank matches target_words[i].
        Returns True ONLY if at least 1 blank was successfully clicked and matched.
        """
        if not self.page or not target_words:
            logger.warning("[SAFETY GUARD] target_words is empty in solve_cycle_blanks. Returning False.")
            return False

        try:
            logger.info(f"Executing Click-to-Cycle blanks for target words: {target_words}...")
            clicks_performed = 0
            successful_matches = 0

            for idx, target_word in enumerate(target_words):
                target_clean = re.sub(r'[^a-zA-Z0-9\s]', '', target_word.strip().lower())
                logger.info(f"Cycling blank #{idx+1} to reach word '{target_clean}'...")

                matched = False
                for attempt in range(18):
                    for frame in self.get_all_frames():
                        try:
                            res = frame.evaluate('''({blankIdx, targetWord}) => {
                                // Find ALL interactive elements that are candidate cycle boxes or contain '↻'
                                const allElements = Array.from(document.querySelectorAll('*'));
                                
                                let cycleBoxes = allElements.filter(el => {
                                    const r = el.getBoundingClientRect();
                                    if (r.width < 15 || r.height < 10 || r.top <= 0 || r.left <= 0) return false;
                                    
                                    const txt = (el.innerText || el.value || '').trim();
                                    const cls = (el.className || '').toString();
                                    
                                    const isRefresh = txt.includes('↻') || txt.includes('\\u21bb') || txt.includes('cycle');
                                    const isBlankClass = cls.includes('blank') || cls.includes('gap') || cls.includes('select-box') || cls.includes('option');
                                    
                                    if (r.width > 500 || r.height > 120) return false;
                                    return isRefresh || isBlankClass;
                                });

                                // Fallback: find all red border input boxes / buttons next to prompt labels
                                if (cycleBoxes.length === 0) {
                                    cycleBoxes = Array.from(document.querySelectorAll('button, div[role="button"], span.gap, div.gap, div.blank, div[class*="box"]')).filter(el => {
                                        const r = el.getBoundingClientRect();
                                        return r.width > 20 && r.width < 400 && r.height > 15 && r.height < 80 && r.top > 80;
                                    });
                                }

                                // Sort top-to-bottom, left-to-right
                                const sortedBlanks = cycleBoxes.sort((a, b) => {
                                    const ra = a.getBoundingClientRect();
                                    const rb = b.getBoundingClientRect();
                                    if (Math.abs(ra.top - rb.top) > 15) return ra.top - rb.top;
                                    return ra.left - rb.left;
                                });

                                if (blankIdx < sortedBlanks.length) {
                                    const el = sortedBlanks[blankIdx];
                                    const currentRaw = (el.innerText || el.value || '').trim().toLowerCase();
                                    const currentClean = currentRaw.replace(/[^a-z0-9\\s]/g, '').trim();

                                    if (currentClean.length > 0 && targetWord.length > 0) {
                                        if (currentClean === targetWord || currentClean.includes(targetWord) || targetWord.includes(currentClean)) {
                                            return { status: 'matched', text: currentRaw, count: sortedBlanks.length };
                                        }
                                    }

                                    el.click();
                                    return { status: 'clicked', text: currentRaw, count: sortedBlanks.length };
                                }
                                return { status: 'not_found', text: '', count: sortedBlanks.length };
                            }''', {"blankIdx": idx, "targetWord": target_clean})

                            status = res.get("status")
                            curr_txt = res.get("text", "")
                            box_count = res.get("count", 0)

                            if status == "clicked":
                                clicks_performed += 1

                            if status == "matched":
                                logger.info(f"Blank #{idx+1} matched word '{target_clean}' (text: '{curr_txt}')!")
                                matched = True
                                successful_matches += 1
                                break
                        except Exception:
                            pass

                    if matched:
                        break

                    time.sleep(random.uniform(0.3, 0.6))

                if not matched:
                    logger.warning(f"Blank #{idx+1} cycle finished without exact match for '{target_clean}'.")

            if clicks_performed == 0 and successful_matches == 0:
                logger.error("[FAILURE] 0 cycle clicks executed! Speexx cycle elements were not interacted with.")
                return False

            logger.info(f"Successfully performed {clicks_performed} clicks and {successful_matches} matches on cycle blanks.")
            return True

        except Exception as e:
            logger.error(f"Failed solving Cycle Blanks: {e}")
            return False

    def _read_scrambled_blocks(self, frame, s_idx: int) -> list:
        """
        Returns the blocks of visible sentence #s_idx as [{'id':..., 'text':...}] in their
        CURRENT on-screen order. Blocks are addressed by data-scrambled-block-id rather
        than by text, because a sentence can legitimately contain the same word twice
        ("the", "I") and matching on text alone would move the wrong one.
        """
        try:
            return frame.evaluate('''(idx) => {
                const sents = Array.from(document.querySelectorAll('.scrambled-sentence'))
                    .filter(el => el.offsetParent !== null);
                const s = sents[idx];
                if (!s) return [];
                return Array.from(s.querySelectorAll('.scrambled-block')).map(b => ({
                    id: b.getAttribute('data-scrambled-block-id'),
                    text: (b.innerText || b.textContent || '').trim()
                }));
            }''', s_idx) or []
        except Exception as e:
            logger.debug(f"Notice on _read_scrambled_blocks: {e}")
            return []

    def _drag_block_before(self, frame, src_id: str, dst_id: str) -> bool:
        """
        Drags the block src_id so it is inserted just before dst_id.

        jQuery-UI sortable only begins a drag after it sees the pointer move past its
        distance threshold, and it decides the insertion point from successive mousemove
        events -- Playwright's drag_to() fires too few of them, so the drag silently
        no-ops. Drive the mouse manually instead, aiming at the LEFT edge of the target
        so the block is inserted before it rather than after.
        """
        try:
            src = frame.locator(f'[data-scrambled-block-id="{src_id}"]').first
            dst = frame.locator(f'[data-scrambled-block-id="{dst_id}"]').first
            src.scroll_into_view_if_needed()
            sb, db = src.bounding_box(), dst.bounding_box()
            if not sb or not db:
                return False

            sx, sy = sb["x"] + sb["width"] / 2, sb["y"] + sb["height"] / 2
            dx, dy = db["x"] + 2, db["y"] + db["height"] / 2

            mouse = self.page.mouse
            mouse.move(sx, sy)
            time.sleep(0.08)
            mouse.down()
            time.sleep(0.08)
            mouse.move(sx + 12, sy + 4, steps=5)          # clear the drag-start threshold
            mouse.move((sx + dx) / 2, (sy + dy) / 2, steps=10)
            mouse.move(dx, dy, steps=10)
            time.sleep(0.25)                             # settle so sortable commits the slot
            mouse.up()
            time.sleep(0.4)
            return True
        except Exception as e:
            logger.debug(f"Notice on _drag_block_before: {e}")
            return False

    def solve_scrambled_sentences(self, target_orders: list) -> bool:
        """
        For Speexx type-scrambled-sentence exercises: each '.scrambled-sentence' is a
        jQuery-UI sortable holding '.scrambled-block' chips in scrambled order.
        target_orders: one list of block texts per sentence, in the desired order.
        Reorders by selection sort -- for each position left to right, drag the block that
        belongs there into place -- verifying the real DOM order after every move.
        """
        if not self.page or not target_orders:
            return False

        moved_any = False
        for frame in self.get_all_frames():
            try:
                n_sentences = frame.evaluate(
                    "() => Array.from(document.querySelectorAll('.scrambled-sentence'))"
                    ".filter(el => el.offsetParent !== null).length"
                )
                if not n_sentences:
                    continue

                for s_idx in range(min(n_sentences, len(target_orders))):
                    targets = [w.strip() for w in target_orders[s_idx] if w and w.strip()]
                    blocks = self._read_scrambled_blocks(frame, s_idx)
                    if not targets or not blocks:
                        continue

                    logger.info(f"Reordering sentence #{s_idx+1} -> {targets}")

                    # Resolve each target word to a specific block id, consuming matches so
                    # repeated words map to distinct blocks.
                    remaining = list(blocks)
                    target_ids = []
                    for w in targets:
                        hit = next((b for b in remaining if b["text"].strip().lower() == w.lower()), None)
                        if hit is None:
                            hit = next((b for b in remaining
                                        if w.lower() in b["text"].strip().lower()
                                        or b["text"].strip().lower() in w.lower()), None)
                        if hit is None:
                            logger.warning(f"  No block matches '{w}' -- skipping this sentence.")
                            target_ids = []
                            break
                        target_ids.append(hit["id"])
                        remaining.remove(hit)

                    if not target_ids:
                        continue

                    # Strategy 1: Direct DOM Re-ordering + jQuery-UI event trigger (100% exact & reliable)
                    frame.evaluate('''({sIdx, targetIds}) => {
                        const sents = Array.from(document.querySelectorAll('.scrambled-sentence'))
                            .filter(el => el.offsetParent !== null);
                        const container = sents[sIdx];
                        if (!container) return;
                        
                        targetIds.forEach(id => {
                            const block = container.querySelector(`[data-scrambled-block-id="${id}"]`);
                            if (block) {
                                container.appendChild(block);
                            }
                        });
                        
                        // Fire jQuery UI sortable update / change events
                        try {
                            if (window.$ || window.jQuery) {
                                const jq = window.$ || window.jQuery;
                                jq(container).trigger('sortupdate');
                                jq(container).trigger('sortchange');
                                jq(container).trigger('change');
                            }
                        } catch(e) {}
                    }''', {'sIdx': s_idx, 'targetIds': target_ids})
                    time.sleep(0.3)

                    # Check if DOM order matches target
                    final = [b["text"] for b in self._read_scrambled_blocks(frame, s_idx)]
                    if [t.lower() for t in final] == [t.lower() for t in targets]:
                        logger.info(f"  Sentence #{s_idx+1} now matches the target order (via DOM reorder).")
                        moved_any = True
                        continue

                    # Strategy 2: Mouse drag fallback
                    for pos in range(len(target_ids)):
                        current = [b["id"] for b in self._read_scrambled_blocks(frame, s_idx)]
                        if pos >= len(current):
                            break
                        want = target_ids[pos]
                        if current[pos] == want:
                            continue
                        if want not in current:
                            logger.warning(f"  Block for position {pos+1} vanished from the sentence.")
                            continue
                        if self._drag_block_before(frame, want, current[pos]):
                            moved_any = True
                        else:
                            logger.warning(f"  Drag failed while placing position {pos+1}.")

                    final = [b["text"] for b in self._read_scrambled_blocks(frame, s_idx)]
                    if [t.lower() for t in final] == [t.lower() for t in targets]:
                        logger.info(f"  Sentence #{s_idx+1} now matches the target order.")
                        moved_any = True
                    else:
                        logger.warning(f"  Sentence #{s_idx+1} ended as {final}, wanted {targets}.")

                return moved_any
            except Exception as e:
                logger.warning(f"Notice on solve_scrambled_sentences: {e}")

        return moved_any

    def _read_table_cells(self, frame) -> list:
        """
        Current state of a type-scrambled-table exercise as
        [{'prompt':..., 'text':..., 'id':...}] in row order (top to bottom).
        Visible rows only -- Speexx renders hidden duplicates for its responsive
        breakpoints, and acting on a clone moves nothing on screen.
        """
        try:
            return frame.evaluate('''() => {
                const roots = Array.from(document.querySelectorAll('.exercise-items'))
                    .filter(el => el.offsetParent !== null);
                const root = roots[0];
                if (!root) return [];
                return Array.from(root.querySelectorAll('.item'))
                    .filter(el => el.offsetParent !== null)
                    .map(item => {
                        const cell = item.querySelector('.scrambled-cell[data-scrambled-cell-id]');
                        if (!cell) return null;
                        const p = item.querySelector('.text');
                        return {
                            prompt: p ? (p.innerText || '').trim() : '',
                            text: (cell.innerText || '').trim(),
                            id: cell.getAttribute('data-scrambled-cell-id')
                        };
                    }).filter(Boolean);
            }''') or []
        except Exception as e:
            logger.debug(f"Notice on _read_table_cells: {e}")
            return []

    def _drag_cell_onto(self, frame, id_a: str, id_b: str) -> bool:
        """
        Drags cell id_a onto cell id_b with a real, stepped mouse gesture (jQuery-UI
        ignores the single-jump movement Playwright's drag_to() produces).

        Deliberately makes NO assumption about whether the widget swaps the pair or
        inserts-and-shifts: the caller re-reads the DOM afterwards and reacts to what
        actually happened. Assuming "swap" here is what scrambled the whole table.
        """
        try:
            # ':visible' matters: Speexx renders hidden responsive duplicates that reuse
            # the same ids, and dragging a clone is a silent no-op on screen.
            a = frame.locator(f'.scrambled-cell[data-scrambled-cell-id="{id_a}"]:visible').first
            b = frame.locator(f'.scrambled-cell[data-scrambled-cell-id="{id_b}"]:visible').first
            if a.count() == 0 or b.count() == 0:
                logger.warning(f"  Cell {id_a} or {id_b} has no visible element to drag.")
                return False
            a.scroll_into_view_if_needed()
            ab, bb = a.bounding_box(), b.bounding_box()
            if not ab or not bb:
                return False

            sx, sy = ab["x"] + ab["width"] / 2, ab["y"] + ab["height"] / 2
            dx, dy = bb["x"] + bb["width"] / 2, bb["y"] + bb["height"] / 2

            mouse = self.page.mouse
            mouse.move(sx, sy)
            time.sleep(0.08)
            mouse.down()
            time.sleep(0.08)
            # Break the jQuery-UI draggable threshold with a small deliberate nudge
            mouse.move(sx + 15, sy + 5, steps=5)
            mouse.move((sx + dx) / 2, (sy + dy) / 2, steps=12)
            mouse.move(dx, dy, steps=12)
            time.sleep(0.25)  # Hover over destination to let jQuery-UI trigger droppable 'over'
            mouse.up()
            time.sleep(0.4)
            return True
        except Exception as e:
            logger.debug(f"Notice on _drag_cell_onto: {e}")
            return False

    def solve_scrambled_table(self, target_texts: list) -> bool:
        """
        For Speexx type-scrambled-table ("Link the sentences that go together"): every
        answer cell is already on screen, just against the wrong prompt.
        """
        if not self.page or not target_texts:
            return False

        wanted = [_norm_text(t) for t in target_texts]

        for frame in self.get_all_frames():
            rows = self._read_table_cells(frame)
            if not rows:
                continue

            # Check if already 100% correct
            if len(rows) >= len(wanted) and [_norm_text(r["text"]) for r in rows[:len(wanted)]] == wanted:
                logger.info("✅ Scrambled table is already in the correct order!")
                return True

            drags = 0
            probed = False
            for pass_no in range(5):
                rows = self._read_table_cells(frame)
                if [_norm_text(r["text"]) for r in rows[:len(wanted)]] == wanted:
                    break

                for pos in range(min(len(wanted), len(rows))):
                    rows = self._read_table_cells(frame)
                    if pos >= len(rows) or not wanted[pos]:
                        continue
                    if _norm_text(rows[pos]["text"]) == wanted[pos]:
                        continue

                    # Search the WHOLE table for the cell holding wanted answer
                    src = next((i for i, r in enumerate(rows)
                                if _norm_text(r["text"]) == wanted[pos]), None)
                    if src is None:
                        logger.warning(f"  Row #{pos+1}: no cell holds the wanted answer.")
                        continue

                    if self._drag_cell_onto(frame, rows[src]["id"], rows[pos]["id"]):
                        drags += 1
                        time.sleep(0.2)
                        after = self._read_table_cells(frame)

                        if pos < len(after) and _norm_text(after[pos]["text"]) != wanted[pos]:
                            # Try pushing the other cell in reverse
                            if self._drag_cell_onto(frame, rows[pos]["id"], rows[src]["id"]):
                                drags += 1
                                time.sleep(0.2)

            final = self._read_table_cells(frame)
            ok = sum(1 for i, r in enumerate(final)
                     if i < len(wanted) and _norm_text(r["text"]) == wanted[i])
            logger.info(f"Scrambled table: {ok}/{len(wanted)} rows correct after {drags} drag(s).")
            return ok == len(wanted)

        return False

    def set_choice_options(self, wanted_by_items: list) -> int:
        """
        For Speexx type-multiple-choice and True/False radio/checkbox groups:
        Sets the choices PER ITEM (per question) so that choices in one question
        never overwrite or conflict with choices in another question.
        wanted_by_items can be:
          - A list of lists of strings: e.g. [ ['true'], ['false'], ['true'], ... ]
          - Or a list of strings: e.g. ['true', 'false', 'true', ...]
        """
        if not self.page or not wanted_by_items:
            return 0

        # Normalize to list of lists
        normalized = []
        for val in wanted_by_items:
            if isinstance(val, list):
                normalized.append([_norm_text(s) for s in val if s])
            elif isinstance(val, str) and val.strip():
                normalized.append([_norm_text(val)])
            else:
                normalized.append([])

        for frame in self.get_all_frames():
            try:
                # First check if structured items exist
                item_count = frame.evaluate('''() => {
                    return document.querySelectorAll('.exercise-items .item, .item').length;
                }''')

                total_clicked = 0
                if item_count > 0:
                    for item_idx, wanted_list in enumerate(normalized):
                        clicked = frame.evaluate(r'''({itemIdx, wanted}) => {
                            const items = Array.from(document.querySelectorAll('.exercise-items .item, .item'))
                                .filter(el => el.offsetParent !== null && el.querySelector('label.choice-option, input.choice, input[type="radio"], input[type="checkbox"]') !== null);

                            let clickedCount = 0;
                            const norm = (s) => (s || '').toLowerCase().replace(/[^a-z0-9]/g, ' ').replace(/\s+/g, ' ').trim();

                            if (items.length > 0 && itemIdx < items.length) {
                                const item = items[itemIdx];
                                const wantedNorm = (wanted || []).map(norm).filter(Boolean);
                                const labels = Array.from(item.querySelectorAll('label.choice-option'));
                                labels.forEach(lab => {
                                    const inp = lab.querySelector('input');
                                    const lbl = lab.querySelector('.choice-option-label, .text');
                                    const rawText = (lbl ? lbl.innerText : lab.innerText || '').trim();
                                    const textNorm = norm(rawText);
                                    const shouldCheck = wantedNorm.some(w => w === textNorm || (w && (textNorm.includes(w) || w.includes(textNorm))));

                                    if (inp) {
                                        if (shouldCheck !== inp.checked) {
                                            try { lab.scrollIntoView({ behavior: 'smooth', block: 'center' }); } catch(e) {}
                                            lab.click();
                                            if (inp && !inp.checked && shouldCheck) {
                                                inp.checked = true;
                                                inp.dispatchEvent(new Event('change', { bubbles: true }));
                                                inp.dispatchEvent(new Event('input', { bubbles: true }));
                                            }
                                            clickedCount++;
                                        }
                                    }
                                });
                            }
                            return clickedCount;
                        }''', {'itemIdx': item_idx, 'wanted': wanted_list})

                        total_clicked += clicked
                        if item_idx < len(normalized) - 1:
                            sub_delay = random.uniform(config.SUB_ITEM_DELAY_MIN, config.SUB_ITEM_DELAY_MAX)
                            if sub_delay > 0:
                                logger.info(f"[Pacing] Pausing {sub_delay:.2f}s before next choice item...")
                                time.sleep(sub_delay)
                    res = total_clicked
                else:
                    res = frame.evaluate(r'''({itemAnswers}) => {
                        let clickedCount = 0;
                        const norm = (s) => (s || '').toLowerCase().replace(/[^a-z0-9]/g, ' ').replace(/\s+/g, ' ').trim();
                        const allLabels = Array.from(document.querySelectorAll('label.choice-option'))
                            .filter(el => el.offsetParent !== null);
                        const flatWantedNorm = itemAnswers.flat().map(norm).filter(Boolean);
                        allLabels.forEach(lab => {
                            const inp = lab.querySelector('input');
                            const lbl = lab.querySelector('.choice-option-label, .text');
                            const rawText = (lbl ? lbl.innerText : lab.innerText || '').trim();
                            const textNorm = norm(rawText);
                            const shouldCheck = flatWantedNorm.some(w => w === textNorm || (w && (textNorm.includes(w) || w.includes(textNorm))));
                            if (inp && shouldCheck !== inp.checked) {
                                try { lab.scrollIntoView(); } catch(e) {}
                                lab.click();
                                clickedCount++;
                            }
                        });
                        return clickedCount;
                    }''', {'itemAnswers': normalized})

                logger.info(f"Set choice options per item: {res} click action(s) performed.")
                return res
            except Exception as e:
                logger.debug(f"Notice on set_choice_options: {e}")

        return 0

    def solve_true_false(self, answers: list) -> bool:
        """
        Solves True/False exercises.
        answers: list of dicts [{'statement_index': 0, 'answer': 'true'}, ...] or list of strings.
        """
        if not answers:
            return False
        formatted = []
        for a in answers:
            if isinstance(a, dict):
                formatted.append([a.get("answer", "true")])
            elif isinstance(a, str):
                formatted.append([a])
            elif isinstance(a, list):
                formatted.append(a)
        return self.set_choice_options(formatted) > 0

    def tag_drop_slots(self) -> int:
        """
        Stamps every VISIBLE '.drag-drop-placeholder' with data-autoslot="0..n-1" in
        document order, giving each blank a stable identity for the whole placement pass.

        This must run BEFORE any word is placed. Placeholders are consumed as they get
        filled, so a live nth() index silently shifts onto a different blank after each
        drop -- the answers then land in the wrong gaps even though every individual
        placement "succeeded". Returns how many slots were tagged.
        """
        if not self.page:
            return 0

        for frame in self.get_all_frames():
            try:
                count = frame.evaluate('''() => {
                    const slots = Array.from(document.querySelectorAll('.drag-drop-placeholder'))
                        .filter(el => el.offsetParent !== null && el.getBoundingClientRect().width > 0);
                    slots.forEach((el, i) => {
                        el.setAttribute('data-autoslot', String(i));
                        // Stamp the parent too: the placeholder itself is consumed when a
                        // chip is dropped in, but its container survives, which is the only
                        // stable way to ask "which blank did this word actually land in?"
                        if (el.parentElement) {
                            el.parentElement.setAttribute('data-autoslot-parent', String(i));
                        }
                    });
                    return slots.length;
                }''')
                if count:
                    logger.info(f"Tagged {count} drop slot(s) with stable indices.")
                    return count
            except Exception as e:
                logger.debug(f"Notice on tag_drop_slots: {e}")

        return 0

    def place_word_in_slot(self, word: str, slot_idx: int) -> bool:
        """Places target word into blank slot index across all frames."""
        if not self.page:
            return False

        try:
            logger.info(f"Attempting to place word '{word}' into slot #{slot_idx+1}...")

            # Primary strategy for Speexx's real type-drag-drop chips: they're jQuery-UI
            # draggable/droppable (class="drag-drop ... ui-draggable", data-drag-drop-id).
            # jQuery-UI starts a drag only after it observes mouse MOVEMENT past its
            # distance threshold, and it tracks the drop target from successive mousemove
            # events. Playwright's drag_to() (and a bare down/move/up) does too few moves,
            # so the drag silently no-ops -- the chip never leaves the word bank.
            # Drive the mouse manually with interpolated steps instead.
            # Strategy 0: Direct jQuery-UI droppable drop execution (100% exact & reliable)
            for frame in self.get_all_frames():
                try:
                    drop_ok = frame.evaluate('''({slotIdx, word}) => {
                        const jq = window.$ || window.jQuery;
                        if (!jq) return false;
                        const slot = document.querySelector(`[data-autoslot="${slotIdx}"]`);
                        if (!slot) return false;
                        
                        const chips = Array.from(document.querySelectorAll('.draggable-container .drag-drop, .word-bank .drag-drop, .drag-drop'));
                        const isTargetEmpty = (word.toLowerCase() === '[empty]' || word.toLowerCase() === 'empty' || word === '-' || word.toLowerCase() === 'none' || word.trim() === '');
                        const chip = chips.find(c => {
                            const t = (c.innerText || c.textContent || '').trim().toLowerCase();
                            if (isTargetEmpty) {
                                return t === '' && !c.closest('.exercise-items, [data-autoslot-parent]');
                            }
                            return t === word.toLowerCase() && !c.closest('.exercise-items, [data-autoslot-parent]');
                        });
                        if (!chip) return false;
                        
                        const droppable = jq(slot).data('ui-droppable');
                        if (droppable && droppable.options && droppable.options.drop) {
                            droppable.options.drop.call(slot, {}, { draggable: jq(chip) });
                            return true;
                        }
                        return false;
                    }''', {'slotIdx': slot_idx, 'word': word})
                    if drop_ok:
                        logger.info(f"  ✅ Word '{word}' placed into slot #{slot_idx+1} via jQuery-UI droppable.")
                        return True
                except Exception as e:
                    logger.debug(f"Strategy 0 notice: {e}")

            for frame in self.get_all_frames():
                try:
                    # Target the slot by the STABLE data-autoslot stamp applied by
                    # tag_drop_slots(), never by a live nth() index. A filled placeholder
                    # drops out of '.drag-drop-placeholder:visible', so the live list
                    # SHRINKS after every successful placement and nth(slot_idx) silently
                    # starts pointing at a different blank -- which is what scrambled the
                    # answers across the wrong gaps.
                    slot = frame.locator(f'[data-autoslot="{slot_idx}"]')
                    if slot.count() == 0:
                        continue

                    # Select only chips that are CURRENTLY in the word bank (.draggable-container)
                    # and have not yet been placed into a sentence above. Filtering for unplaced
                    # chips is essential when the word bank contains duplicate words (e.g. multiple
                    # 'about' and 'of' chips), otherwise nth(i) repeatedly selects the chip that
                    # was already dropped into an earlier blank.
                    chips = frame.locator(
                        '.draggable-container .drag-drop[data-drag-drop-id]:visible, '
                        '.exercise-bottom-bar-main .drag-drop[data-drag-drop-id]:visible, '
                        '.word-bank .drag-drop[data-drag-drop-id]:visible'
                    )
                    if chips.count() == 0:
                        # Fallback
                        chips = frame.locator('.drag-drop[data-drag-drop-id]:visible')
                    if chips.count() == 0:
                        continue

                    chip_match = None
                    for i in range(chips.count()):
                        c = chips.nth(i)
                        # Verify the chip is actually still in the bank, not inside a sentence
                        in_bank = True
                        try:
                            in_bank = frame.evaluate('''el => {
                                return !el.closest('.exercise-items, .sentence, .item, [data-autoslot-parent]');
                            }''', c.element_handle())
                        except Exception:
                            pass

                        is_target_empty = (word.strip().lower() in ['[empty]', 'empty', '-', 'none', ''])
                        txt = _norm_text(c.inner_text())
                        if in_bank:
                            if is_target_empty and (txt == '' or txt == ' '):
                                chip_match = c
                                break
                            elif not is_target_empty and txt == _norm_text(word):
                                chip_match = c
                                break

                    if not chip_match:
                        logger.warning(f"  No available unplaced chip '{word}' left in word bank.")
                        continue

                    # Pin the chip by its stable id
                    chip_id = chip_match.get_attribute("data-drag-drop-id")
                    if chip_id:
                        chip_match = frame.locator(
                            f'.draggable-container .drag-drop[data-drag-drop-id="{chip_id}"]:visible, '
                            f'.drag-drop[data-drag-drop-id="{chip_id}"]:visible'
                        ).first

                    def _placement() -> dict:
                        """
                        Reports whether the chip left the word bank AND which blank it
                        actually ended up in. 'Left the bank' alone is not success: a
                        misrouted word satisfies that too, which is how 7 words could be
                        reported as placed while sitting in the wrong gaps.
                        """
                        try:
                            return frame.evaluate('''({id, idx}) => {
                                const chip = document.querySelector(`[data-drag-drop-id="${id}"]`);
                                if (!chip) return { gone: true, inBank: false, where: -1 };
                                const inBank = !!chip.closest('.draggable-container');
                                const holder = chip.closest('[data-autoslot-parent]');
                                const where = holder ? parseInt(holder.getAttribute('data-autoslot-parent'), 10) : -1;
                                return { gone: false, inBank: inBank, where: where };
                            }''', {"id": chip_id, "idx": slot_idx}) or {}
                        except Exception:
                            return {}

                    def _landed() -> bool:
                        res = _placement()
                        if not res or res.get("inBank", True):
                            return False
                        where = res.get("where", -1)
                        if where >= 0 and where != slot_idx:
                            logger.warning(
                                f"  '{word}' left the word bank but landed in blank #{where+1}, "
                                f"not #{slot_idx+1}."
                            )
                            return False
                        return True

                    def _ui_state(tag: str):
                        """
                        Dump the live widget state so the log shows the actual click-to-place
                        state machine instead of us inferring it from success/failure ratios.
                        """
                        try:
                            st = frame.evaluate('''(id) => {
                                const chip = document.querySelector(`[data-drag-drop-id="${id}"]`);
                                const active = Array.from(document.querySelectorAll(
                                    '.drag-drop, .drag-drop-placeholder'
                                )).filter(el => /select|active|current|focus|chosen/i.test(el.className))
                                 .map(el => (el.innerText || '(empty)').trim() + ':' + el.className)
                                 .slice(0, 4);
                                return {
                                    chipCls: chip ? chip.className : '(chip gone)',
                                    focus: document.activeElement
                                        ? document.activeElement.className || document.activeElement.tagName
                                        : '(none)',
                                    marked: active
                                };
                            }''', chip_id)
                            logger.info(f"    [state {tag}] chip='{st.get('chipCls')}' "
                                        f"focus='{st.get('focus')}' marked={st.get('marked')}")
                        except Exception:
                            pass

                    def _clear_selection():
                        """
                        Drop any half-finished click-to-place selection. A successful
                        placement leaves the widget in a state where the very next attempt
                        no-ops -- which is exactly why results alternate 1 on / 1 off.
                        Escape alone did not clear it, so also click a neutral part of the
                        page and blur whatever holds focus.
                        """
                        try:
                            self.page.keyboard.press("Escape")
                            frame.evaluate('''() => {
                                if (document.activeElement && document.activeElement.blur) {
                                    document.activeElement.blur();
                                }
                                const h = document.querySelector('.exercise-header, .exercise-content');
                                if (h) h.click();
                            }''')
                            time.sleep(0.25)
                        except Exception:
                            pass

                    # slot->chip is the confirmed order for Speexx's accessible click-to-place
                    # (it lands words in the right blanks). Only this order is used: trying
                    # the reverse too added two more clicks per word and churned the very
                    # selection state we are trying to keep clean.
                    #
                    # Retry the SAME pair a few times. Failures alternate perfectly with
                    # successes -- the widget is left in a state where the next click-pair
                    # is swallowed (the chip click does not even take focus) -- so a second
                    # attempt after clearing and settling generally goes through.
                    placed_ok = False
                    for attempt in range(3):
                        _clear_selection()
                        try:
                            slot.first.click()
                            time.sleep(0.25)
                            chip_match.click()
                            time.sleep(0.6)   # let the swap animation settle before verifying
                            if _landed():
                                logger.info(
                                    f"Click-placed '{word}' into slot #{slot_idx+1} "
                                    f"(verified, attempt {attempt+1})."
                                )
                                _clear_selection()
                                placed_ok = True
                                break
                            _ui_state(f"after failed click-place (attempt {attempt+1})")
                        except Exception as e:
                            logger.debug(f"Notice on click-to-place (attempt {attempt+1}): {e}")
                        time.sleep(0.4)

                    if placed_ok:
                        return True

                    _clear_selection()

                    # Strategy B: synthesise a real jQuery-UI mouse drag. Only reached when
                    # the chip is still in the word bank, so it cannot move a placed chip.
                    chip_match.scroll_into_view_if_needed()
                    src = chip_match.bounding_box()
                    dst = slot.first.bounding_box()
                    if not src or not dst:
                        continue

                    sx, sy = src["x"] + src["width"] / 2, src["y"] + src["height"] / 2
                    dx, dy = dst["x"] + dst["width"] / 2, dst["y"] + dst["height"] / 2

                    mouse = self.page.mouse
                    mouse.move(sx, sy)
                    mouse.down()
                    # A few small jiggles first to clear jQuery-UI's drag-start threshold,
                    # then travel to the target in steps so dragover fires along the way.
                    mouse.move(sx + 6, sy + 6, steps=4)
                    mouse.move((sx + dx) / 2, (sy + dy) / 2, steps=12)
                    mouse.move(dx, dy, steps=12)
                    mouse.move(dx, dy, steps=3)  # settle on the target
                    mouse.up()
                    time.sleep(0.35)

                    if _landed():
                        logger.info(f"Drag-placed word '{word}' into slot #{slot_idx+1} (verified).")
                        return True
                    logger.warning(f"'{word}' did not land in slot #{slot_idx+1} (still in the word bank).")
                except Exception as e:
                    logger.debug(f"Notice on jQuery-UI drag-drop placement: {e}")

            for frame in self.get_all_frames():
                try:
                    click_success = frame.evaluate('''({wordText, slotIndex}) => {
                        const wordChips = Array.from(document.querySelectorAll(
                            '.word-chip, .draggable, [draggable="true"], .word-bank-item, .drag-word, .chip, div[role="button"], span.word'
                        ));

                        let chip = wordChips.find(c => c.innerText.trim().toLowerCase() === wordText.toLowerCase());
                        if (!chip) {
                            chip = wordChips.find(c => c.innerText.trim().toLowerCase().includes(wordText.toLowerCase()));
                        }

                        const dropSlots = Array.from(document.querySelectorAll(
                            '.drop-target, .droppable, .gap, .blank, .drop-slot, .sentence-gap, [data-gap], input[type="text"]'
                        )).sort((a, b) => {
                            const ra = a.getBoundingClientRect();
                            const rb = b.getBoundingClientRect();
                            if (Math.abs(ra.top - rb.top) > 15) return ra.top - rb.top;
                            return ra.left - rb.left;
                        });

                        if (chip && slotIndex < dropSlots.length) {
                            const slot = dropSlots[slotIndex];
                            if (slot.tagName.toLowerCase() === 'input' || slot.tagName.toLowerCase() === 'textarea') {
                                slot.value = wordText;
                                slot.dispatchEvent(new Event('input', { bubbles: true }));
                                slot.dispatchEvent(new Event('change', { bubbles: true }));
                                return true;
                            }

                            chip.click();
                            setTimeout(() => slot.click(), 100);
                            return true;
                        }
                        return false;
                    }''', {"wordText": word, "slotIndex": slot_idx})

                    if click_success:
                        logger.info(f"Placed word '{word}' into slot #{slot_idx+1}!")
                        return True
                except Exception:
                    pass

            return False
        except Exception as e:
            logger.error(f"Failed placing word '{word}' into slot #{slot_idx+1}: {e}")
            return False


    def place_all_picture_words(self, ordered_words: list) -> int:
        """
        Place ALL picture matching words in one pass using chip-first click ordering.
        Speexx Picture Matching uses: click CHIP first → click SLOT second.
        This is the opposite of sentence drag-drop (slot-first).
        Returns how many words were successfully placed.
        """
        if not self.page or not ordered_words:
            return 0

        placed = 0
        for frame in self.get_all_frames():
            try:
                # First: get all placeholders sorted by visual position (top-left to bottom-right)
                slot_count = frame.evaluate("""() => {
                    const slots = Array.from(document.querySelectorAll('.drag-drop-placeholder'))
                        .filter(el => {
                            const r = el.getBoundingClientRect();
                            return r.width > 0 && r.height > 0 && el.offsetParent !== null;
                        })
                        .sort((a, b) => {
                            const ra = a.getBoundingClientRect();
                            const rb = b.getBoundingClientRect();
                            // Sort by row first (allowing 30px tolerance), then by column
                            if (Math.abs(ra.top - rb.top) > 30) return ra.top - rb.top;
                            return ra.left - rb.left;
                        });
                    slots.forEach((el, i) => {
                        el.setAttribute('data-pic-slot', String(i));
                        if (el.parentElement) {
                            el.parentElement.setAttribute('data-pic-slot-parent', String(i));
                        }
                    });
                    return slots.length;
                }""")

                if not slot_count:
                    continue

                logger.info(f"[PictureMatch] Found {slot_count} picture slots, {len(ordered_words)} words to place.")

                for idx, word in enumerate(ordered_words):
                    if not word or idx >= slot_count:
                        continue

                    success = False
                    for attempt in range(4):
                        try:
                            # Clear any lingering selection
                            try:
                                self.page.keyboard.press("Escape")
                                time.sleep(0.15)
                                frame.evaluate("""() => {
                                    if (document.activeElement && document.activeElement.blur) {
                                        document.activeElement.blur();
                                    }
                                }""")
                                time.sleep(0.1)
                            except Exception:
                                pass

                            # Check if slot still needs a word (hasn't been filled)
                            slot_exists = frame.evaluate(f"""() => {{
                                const s = document.querySelector('[data-pic-slot="{idx}"]');
                                return s ? s.offsetParent !== null : false;
                            }}""")
                            
                            if not slot_exists:
                                # Slot consumed (already filled) - check parent
                                already_filled = frame.evaluate(f"""() => {{
                                    const p = document.querySelector('[data-pic-slot-parent="{idx}"]');
                                    if (!p) return false;
                                    const chip = p.querySelector('.drag-drop[data-drag-drop-id]');
                                    return chip ? chip.innerText.trim() : false;
                                }}""")
                                if already_filled:
                                    logger.info(f"  Slot #{idx+1} already filled with '{already_filled}', skipping.")
                                    success = True
                                    break

                            # Find the chip in the word bank
                            chip_sel = frame.evaluate("""(wordText) => {
                                const chips = Array.from(document.querySelectorAll('.drag-drop[data-drag-drop-id]'))
                                    .filter(el => {
                                        // Must be in the bank (not already placed)
                                        const r = el.getBoundingClientRect();
                                        if (r.width <= 0 || r.height <= 0) return false;
                                        // Check it's not inside a filled slot
                                        if (el.closest('[data-pic-slot-parent]')) return false;
                                        // Check it's still in the bank area  
                                        const inBank = el.closest('.draggable-container, .exercise-bottom-bar-main, .word-bank');
                                        return inBank !== null;
                                    });
                                const match = chips.find(c => c.innerText.trim().toLowerCase() === wordText.toLowerCase());
                                return match ? match.getAttribute('data-drag-drop-id') : null;
                            }""", word)

                            if not chip_sel:
                                logger.warning(f"  No chip found for '{word}' in word bank (attempt {attempt+1})")
                                # Try broader search
                                chip_sel = frame.evaluate("""(wordText) => {
                                    const chips = Array.from(document.querySelectorAll('.drag-drop[data-drag-drop-id]'))
                                        .filter(el => {
                                            const r = el.getBoundingClientRect();
                                            if (r.width <= 0 || r.height <= 0) return false;
                                            if (el.closest('[data-pic-slot-parent]')) return false;
                                            return true;
                                        });
                                    const match = chips.find(c => c.innerText.trim().toLowerCase() === wordText.toLowerCase());
                                    return match ? match.getAttribute('data-drag-drop-id') : null;
                                }""", word)
                                if not chip_sel:
                                    logger.warning(f"  Still no chip for '{word}' even with broader search")
                                    continue

                            # Strategy A: CHIP first, then SLOT (correct for Picture Matching)
                            chip_loc = frame.locator(f'.drag-drop[data-drag-drop-id="{chip_sel}"]').first
                            slot_loc = frame.locator(f'[data-pic-slot="{idx}"]').first

                            # Scroll both into view
                            try:
                                chip_loc.scroll_into_view_if_needed()
                                time.sleep(0.15)
                            except Exception:
                                pass

                            # Click chip FIRST
                            chip_loc.click()
                            time.sleep(0.3)

                            # Scroll slot into view
                            try:
                                slot_loc.scroll_into_view_if_needed()
                                time.sleep(0.15)
                            except Exception:
                                pass

                            # Click slot SECOND
                            slot_loc.click()
                            time.sleep(0.5)

                            # Verify placement
                            landed = frame.evaluate(f"""(chipId) => {{
                                const chip = document.querySelector(`[data-drag-drop-id="${{chipId}}"]`);
                                if (!chip) return true; // chip consumed = placed
                                const parent = chip.closest('[data-pic-slot-parent]');
                                if (parent) {{
                                    const parentIdx = parseInt(parent.getAttribute('data-pic-slot-parent'), 10);
                                    return parentIdx === {idx};
                                }}
                                // Check if chip left the bank
                                const inBank = chip.closest('.draggable-container, .exercise-bottom-bar-main, .word-bank');
                                return !inBank;
                            }}""", chip_sel)

                            if landed:
                                logger.info(f"  ✅ Placed '{word}' → slot #{idx+1} (attempt {attempt+1})")
                                success = True
                                break
                            else:
                                logger.warning(f"  ❌ '{word}' did NOT land in slot #{idx+1} (attempt {attempt+1})")

                                # Strategy B: Try SLOT first then CHIP (reverse order)
                                try:
                                    self.page.keyboard.press("Escape")
                                    time.sleep(0.2)
                                except Exception:
                                    pass

                                slot_loc2 = frame.locator(f'[data-pic-slot="{idx}"]')
                                if slot_loc2.count() > 0:
                                    slot_loc2.first.click()
                                    time.sleep(0.3)
                                    chip_loc2 = frame.locator(f'.drag-drop[data-drag-drop-id="{chip_sel}"]').first
                                    chip_loc2.click()
                                    time.sleep(0.5)

                                    landed2 = frame.evaluate(f"""(chipId) => {{
                                        const chip = document.querySelector(`[data-drag-drop-id="${{chipId}}"]`);
                                        if (!chip) return true;
                                        const inBank = chip.closest('.draggable-container, .exercise-bottom-bar-main, .word-bank');
                                        return !inBank;
                                    }}""", chip_sel)

                                    if landed2:
                                        logger.info(f"  ✅ Placed '{word}' → slot #{idx+1} (reverse click, attempt {attempt+1})")
                                        success = True
                                        break

                                # Strategy C: Real mouse drag
                                try:
                                    self.page.keyboard.press("Escape")
                                    time.sleep(0.15)
                                    chip_loc3 = frame.locator(f'.drag-drop[data-drag-drop-id="{chip_sel}"]').first
                                    slot_loc3 = frame.locator(f'[data-pic-slot="{idx}"]')
                                    if slot_loc3.count() > 0:
                                        chip_loc3.scroll_into_view_if_needed()
                                        src = chip_loc3.bounding_box()
                                        dst = slot_loc3.first.bounding_box()
                                        if src and dst:
                                            sx = src["x"] + src["width"] / 2
                                            sy = src["y"] + src["height"] / 2
                                            dx = dst["x"] + dst["width"] / 2
                                            dy = dst["y"] + dst["height"] / 2
                                            mouse = self.page.mouse
                                            mouse.move(sx, sy)
                                            mouse.down()
                                            mouse.move(sx + 5, sy + 5, steps=3)
                                            mouse.move((sx+dx)/2, (sy+dy)/2, steps=10)
                                            mouse.move(dx, dy, steps=10)
                                            mouse.move(dx, dy, steps=2)
                                            mouse.up()
                                            time.sleep(0.4)
                                            logger.info(f"  Attempted mouse drag for '{word}' → slot #{idx+1}")
                                except Exception as drag_e:
                                    logger.debug(f"  Drag fallback error: {drag_e}")

                        except Exception as e:
                            logger.debug(f"  place_all attempt {attempt+1} error: {e}")
                            time.sleep(0.3)

                    if success:
                        placed += 1
                    else:
                        logger.warning(f"  ⚠️ Failed to place '{word}' in slot #{idx+1} after all attempts")

                if placed > 0:
                    return placed

            except Exception as e:
                logger.debug(f"place_all_picture_words frame error: {e}")

        return placed

    def fill_text_inputs(self, answers: list) -> bool:
        """Fills text input fields across frames with human-like typing delay."""
        if not self.page or not answers:
            return False
        try:
            for frame in self.get_all_frames():
                try:
                    inputs = frame.locator("input[type='text'], textarea, [contenteditable='true']").all()
                    for idx, inp in enumerate(inputs):
                        if idx < len(answers) and inp.is_visible():
                            ans = str(answers[idx])
                            inp.focus()
                            inp.clear()
                            for char in ans:
                                inp.type(char, delay=random.randint(config.MIN_TYPING_DELAY_MS, config.MAX_TYPING_DELAY_MS))
                            logger.info(f"Filled text input #{idx+1} with: '{ans}'")
                            time.sleep(0.5)
                except Exception:
                    pass
            return True
        except Exception as e:
            logger.error(f"Failed filling text inputs: {e}")
            return False

    def click_choice_option(self, option_text: str) -> bool:
        """Clicks a choice option by matching text across frames."""
        if not self.page:
            return False
        try:
            logger.info(f"Clicking choice option: '{option_text}'")
            for frame in self.get_all_frames():
                try:
                    loc = frame.locator(f"text='{option_text}'").first
                    if loc.is_visible():
                        loc.click()
                        return True

                    clicked = frame.evaluate('''({targetText}) => {
                        const elems = Array.from(document.querySelectorAll('label, button, input[type="radio"], div.choice, span.choice, div.option'));
                        for (let el of elems) {
                            if (el.innerText && el.innerText.trim().toLowerCase().includes(targetText.toLowerCase())) {
                                el.click();
                                return true;
                            }
                        }
                        return false;
                    }''', {"targetText": option_text})
                    if clicked:
                        return True
                except Exception:
                    pass
            return False
        except Exception as e:
            logger.error(f"Failed to click choice option '{option_text}': {e}")
            return False

    def drag_and_drop_to_image_slot(self, word: str, slot_idx: int) -> bool:
        """Drags word element to specified image slot index across frames."""
        if not self.page:
            return False
        try:
            logger.info(f"Dragging '{word}' to slot index {slot_idx}...")
            for frame in self.get_all_frames():
                try:
                    dragged = frame.evaluate('''({wordText, slotIndex}) => {
                        const draggables = Array.from(document.querySelectorAll('[draggable="true"], .draggable, .word-item, .drag-item'));
                        const dropSlots = Array.from(document.querySelectorAll('.drop-target, .image-slot, .dropzone, figure, .picture-card'));

                        let source = draggables.find(d => d.innerText.trim().toLowerCase() === wordText.toLowerCase());
                        let target = dropSlots[slotIndex];

                        if (source && target) {
                            const dataTransfer = new DataTransfer();
                            source.dispatchEvent(new DragEvent('dragstart', { dataTransfer, bubbles: true }));
                            target.dispatchEvent(new DragEvent('dragover', { dataTransfer, bubbles: true }));
                            target.dispatchEvent(new DragEvent('drop', { dataTransfer, bubbles: true }));
                            source.dispatchEvent(new DragEvent('dragend', { dataTransfer, bubbles: true }));
                            return true;
                        }
                        return false;
                    }''', {"wordText": word, "slotIndex": slot_idx})
                    if dragged:
                        return True
                except Exception:
                    pass
            return False
        except Exception as e:
            logger.error(f"Failed drag & drop for '{word}': {e}")
            return False

    def get_grammar_help(self) -> str:
        """Clicks Grammar button if present on screen, extracts rules/examples text, and closes popup."""
        if not self.page:
            return ""
        try:
            page = self.page
            grammar_btn = page.locator("button:has-text('Grammar'), a:has-text('Grammar'), .btn-warning:has-text('Grammar')").first
            if grammar_btn.count() > 0 and grammar_btn.is_visible():
                logger.info("📘 Found Grammar button! Opening grammar guidelines...")
                grammar_btn.click()
                time.sleep(0.8)
                popup_text = page.evaluate('''() => {
                    const modal = document.querySelector('.modal-content, .popover-content, .grammar-content, .tooltip, .popover');
                    return modal ? (modal.innerText || modal.textContent || '').trim() : '';
                }''')
                # Close popup / modal
                close_btn = page.locator(".modal-content .close, button:has-text('Close'), button:has-text('✕')").first
                if close_btn.count() > 0 and close_btn.is_visible():
                    close_btn.click()
                else:
                    grammar_btn.click()
                time.sleep(0.5)
                logger.info(f"📘 Extracted Grammar rules ({len(popup_text)} chars).")
                return popup_text
        except Exception as e:
            logger.debug(f"Notice on grammar helper: {e}")
        return ""

    def fill_typed_inputs(self, answers: list) -> bool:
        """Fills typed answers into input.answer / input[type='text']."""
        if not self.page:
            return False
        try:
            page = self.page
            inputs = page.locator(".exercise-items input.answer, .exercise-items input[type='text']")
            count = inputs.count()
            logger.info(f"Filling {len(answers)} text inputs on screen (found {count} inputs)...")
            for idx, ans in enumerate(answers):
                if idx < count and ans:
                    clean_ans = str(ans).strip()
                    inp = inputs.nth(idx)
                    inp.scroll_into_view_if_needed()
                    inp.click()
                    inp.fill(clean_ans)
                    page.evaluate('''(data) => {
                        const el = document.querySelectorAll(".exercise-items input.answer, .exercise-items input[type='text']")[data.idx];
                        if (el) {
                            el.value = data.val;
                            el.dispatchEvent(new Event('input', { bubbles: true }));
                            el.dispatchEvent(new Event('change', { bubbles: true }));
                        }
                    }''', {'idx': idx, 'val': clean_ans})
                    if idx < len(answers) - 1:
                        sub_delay = random.uniform(config.SUB_ITEM_DELAY_MIN, config.SUB_ITEM_DELAY_MAX)
                        if sub_delay > 0:
                            logger.info(f"[Pacing] Pausing {sub_delay:.2f}s before filling next blank...")
                            time.sleep(sub_delay)
                    else:
                        time.sleep(0.3)
            return True
        except Exception as e:
            logger.error(f"Error filling text inputs: {e}")
            return False

    def close(self):
        """Closes browser context and playwright."""
        if self.context:
            self.context.close()
        if self.playwright:
            self.playwright.stop()
        logger.info("Browser closed.")

    def click_mark_words(self, target_phrases: list) -> int:
        """
        In type-mark-text exercises, clicks words/phrases to toggle their selection.
        1. Accurately maps target phrases/words to word element indices using continuous character mapping.
        2. Groups word elements into their parent clickable units (.mark-text parent or word itself).
        3. Clicks each clickable unit exactly once if its current marked state differs from target state.
        """
        import time
        if not self.page or not target_phrases:
            return 0
        try:
            total_toggled = 0
            for frame in self.get_all_frames():
                plan = frame.evaluate('''({targets}) => {
                    const cleanTargets = targets.map(t => t.toLowerCase().replace(/[^a-z0-9\\s]/g, ' ').replace(/\\s+/g, ' ').trim()).filter(Boolean);

                    const allWordEls = Array.from(document.querySelectorAll(
                        '.exercise-container .word, .type-mark-text .word, .exercise .word, .markable-word'
                    )).filter(el => el.offsetParent !== null || el.getBoundingClientRect().width > 0);

                    const wordEls = allWordEls.filter(el => !el.querySelector('.word'));
                    if (wordEls.length === 0) return { toggled: 0, details: [], totalWords: 0, targetCount: 0 };

                    let cleanFullNoSpace = '';
                    const charToWordIdx = [];
                    wordEls.forEach((el, idx) => {
                        const raw = (el.innerText || el.textContent || '').trim();
                        const cleanW = raw.toLowerCase().replace(/[^a-z0-9]/g, '');
                        for (let i = 0; i < cleanW.length; i++) {
                            cleanFullNoSpace += cleanW[i];
                            charToWordIdx.push(idx);
                        }
                    });

                    const targetWordIndices = new Set();
                    for (const tgt of cleanTargets) {
                        const cleanTgt = tgt.replace(/[^a-z0-9]/g, '');
                        if (!cleanTgt) continue;
                        let pos = 0;
                        while (true) {
                            const found = cleanFullNoSpace.indexOf(cleanTgt, pos);
                            if (found === -1) break;
                            for (let c = found; c < found + cleanTgt.length; c++) {
                                targetWordIndices.add(charToWordIdx[c]);
                            }
                            pos = found + cleanTgt.length;
                        }
                    }

                    // Group words into their clickable units (closest .mark-text, or the word itself)
                    const unitMap = new Map();
                    wordEls.forEach((el, idx) => {
                        const unit = el.closest('.mark-text') || el;
                        const isTarget = targetWordIndices.has(idx);
                        const isMarked = unit.classList.contains('marked-text') || el.classList.contains('marked-text');
                        if (!unitMap.has(unit)) {
                            unitMap.set(unit, {
                                element: unit,
                                shouldMark: isTarget,
                                isMarked: isMarked,
                                text: (unit.innerText || el.innerText || '').trim()
                            });
                        } else {
                            const entry = unitMap.get(unit);
                            if (isTarget) entry.shouldMark = true;
                            if (isMarked) entry.isMarked = true;
                        }
                    });

                    let toggledCount = 0;
                    const toggledDetails = [];
                    unitMap.forEach((entry) => {
                        if (entry.shouldMark && !entry.isMarked) {
                            entry.element.click();
                            toggledCount++;
                            toggledDetails.push(`Marked: '${entry.text}'`);
                        } else if (!entry.shouldMark && entry.isMarked) {
                            entry.element.click();
                            toggledCount++;
                            toggledDetails.push(`Unmarked: '${entry.text}'`);
                        }
                    });

                    return {
                        toggled: toggledCount,
                        details: toggledDetails,
                        totalWords: wordEls.length,
                        targetCount: targetWordIndices.size
                    };
                }''', {"targets": target_phrases})

                if not plan:
                    continue

                toggled = plan.get("toggled", 0)
                details = plan.get("details", [])
                target_count = plan.get("targetCount", 0)
                total_words = plan.get("totalWords", 0)

                logger.info(f"Mark-text analysis: {target_count}/{total_words} words targeted. Toggled {toggled} unit(s).")
                for d in details[:10]:
                    logger.info(f"  [Mark-Text] {d}")
                total_toggled += toggled

            time.sleep(0.3)
            logger.info(f"Mark-text: Successfully executed {total_toggled} unit toggle action(s).")
            return total_toggled
        except Exception as e:
            logger.exception(f"Error clicking mark words: {e}")
            return 0

if __name__ == "__main__":
    browser = SpeexxBrowser()
    browser.launch("https://portal.speexx.com/")
    print("Press Enter in terminal to close browser...")
    input()
    browser.close()
