"""
SPEEXX DIAGNOSTIC TOOL
======================
Connects to the existing Playwright browser, dumps ALL elements from the Speexx exercise page,
and tests LLM Studio directly. Output goes to diagnose_output.txt AND console.

Usage: 
1. First run main.py and launch browser (option 2), navigate to an exercise page
2. Then run: python diagnose.py
"""
import json
import time
import sys
import os
from datetime import datetime

sys.stdout.reconfigure(encoding='utf-8')

OUTPUT_FILE = "diagnose_output.txt"

def log(msg):
    """Print to console AND write to file"""
    print(msg)
    with open(OUTPUT_FILE, 'a', encoding='utf-8') as f:
        f.write(msg + '\n')

def main():
    # Clear output file
    with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
        f.write(f"=== SPEEXX DIAGNOSTIC DUMP - {datetime.now().isoformat()} ===\n\n")

    # ---- PART 1: Connect to browser and dump DOM ----
    log("=" * 80)
    log("PART 1: DUMPING ALL ELEMENTS FROM SPEEXX PAGE")
    log("=" * 80)

    try:
        from playwright.sync_api import sync_playwright
        
        pw = sync_playwright().start()
        # Connect to the existing browser that main.py launched
        # Try to find an existing browser context
        browser = pw.chromium.connect_over_cdp("http://localhost:9222")
        contexts = browser.contexts
        if not contexts:
            log("ERROR: No browser contexts found! Make sure main.py launched the browser first.")
            return
        
        context = contexts[0]
        pages = context.pages
        if not pages:
            log("ERROR: No pages found!")
            return
            
        page = pages[0]
        log(f"Connected to page: {page.url}")
        log("")

    except Exception as e:
        log(f"Cannot connect via CDP. Launching fresh browser instead...")
        log(f"Error: {e}")
        log("")
        log("Falling back to launching a new browser to the Speexx portal...")
        
        try:
            from playwright.sync_api import sync_playwright
            pw = sync_playwright().start()
            browser = pw.chromium.launch(headless=False)
            context = browser.new_context()
            page = context.new_page()
            page.goto("https://portal.speexx.com/", wait_until="domcontentloaded", timeout=30000)
            log("Launched new browser. Please navigate to the exercise page and press Enter here.")
            input("Press Enter when you're on the exercise page...")
        except Exception as e2:
            log(f"FATAL: Cannot launch browser: {e2}")
            log("")
            log("Alternative: Running DOM dump via existing browser.py module...")
            run_via_browser_module()
            return

    # Now dump the full DOM
    dump_page_elements(page)
    
    # ---- PART 2: Test LLM directly ----
    log("")
    log("=" * 80)
    log("PART 2: TESTING LLM STUDIO DIRECTLY")
    log("=" * 80)
    test_llm_direct()

    log("")
    log("=" * 80)
    log(f"DIAGNOSTIC COMPLETE! Full output saved to: {OUTPUT_FILE}")
    log("=" * 80)


def run_via_browser_module():
    """Fallback: Use the existing browser module to dump DOM"""
    log("Attempting to use existing browser.py module...")
    try:
        import browser as br
        import config
        
        speexx_browser = br.SpeexxBrowser()
        speexx_browser.launch(headless=False, initial_url="https://portal.speexx.com/")
        log("Browser launched. Navigate to exercise page and press Enter...")
        input("Press Enter when ready...")
        
        dump_page_elements(speexx_browser.page)
        test_llm_direct()
        
        speexx_browser.close()
    except Exception as e:
        log(f"ERROR in browser module fallback: {e}")


def dump_page_elements(page):
    """Dump ALL elements from the page across all frames"""
    
    all_frames = [page.main_frame()] + page.main_frame().child_frames
    log(f"\nTotal frames found: {len(all_frames)}")
    
    for fi, frame in enumerate(all_frames):
        log(f"\n{'='*60}")
        log(f"FRAME #{fi}: {frame.url}")
        log(f"{'='*60}")
        
        try:
            # 1. FULL innerText of body
            body_text = frame.evaluate("() => document.body ? document.body.innerText : 'NO BODY'")
            log(f"\n--- BODY INNERTEXT (first 3000 chars) ---")
            log(body_text[:3000] if body_text else "(empty)")
            
            # 2. ALL buttons with their text, classes, and attributes
            log(f"\n--- ALL BUTTONS ---")
            buttons = frame.evaluate('''() => {
                return Array.from(document.querySelectorAll('button, [role="button"], a.btn')).map((el, i) => ({
                    index: i,
                    tag: el.tagName,
                    text: (el.innerText || el.textContent || '').trim().substring(0, 100),
                    classes: el.className,
                    id: el.id,
                    type: el.type || '',
                    draggable: el.draggable,
                    role: el.getAttribute('role') || '',
                    dataAttrs: Array.from(el.attributes).filter(a => a.name.startsWith('data-')).map(a => a.name + '=' + a.value).join(', ')
                }));
            }''')
            for b in (buttons or []):
                log(f"  [{b['index']}] <{b['tag']}> text='{b['text']}' class='{b['classes']}' id='{b['id']}' draggable={b.get('draggable')} data=[{b.get('dataAttrs','')}]")
            
            # 3. ALL inputs
            log(f"\n--- ALL INPUTS ---")
            inputs = frame.evaluate('''() => {
                return Array.from(document.querySelectorAll('input, textarea, select, [contenteditable]')).map((el, i) => ({
                    index: i,
                    tag: el.tagName,
                    type: el.type || '',
                    classes: el.className,
                    id: el.id,
                    placeholder: el.placeholder || '',
                    value: el.value || '',
                    contentEditable: el.contentEditable,
                    name: el.name || '',
                    dataAttrs: Array.from(el.attributes).filter(a => a.name.startsWith('data-')).map(a => a.name + '=' + a.value).join(', ')
                }));
            }''')
            for inp in (inputs or []):
                log(f"  [{inp['index']}] <{inp['tag']} type={inp['type']}> class='{inp['classes']}' id='{inp['id']}' placeholder='{inp['placeholder']}' value='{inp['value']}' name='{inp['name']}' data=[{inp.get('dataAttrs','')}]")
            
            # 4. ALL draggable elements
            log(f"\n--- ALL DRAGGABLE ELEMENTS ---")
            draggables = frame.evaluate('''() => {
                return Array.from(document.querySelectorAll('[draggable], [draggable="true"]')).map((el, i) => ({
                    index: i,
                    tag: el.tagName,
                    text: (el.innerText || el.textContent || '').trim().substring(0, 100),
                    classes: el.className,
                    id: el.id
                }));
            }''')
            if draggables:
                for d in draggables:
                    log(f"  [{d['index']}] <{d['tag']}> text='{d['text']}' class='{d['classes']}'")
            else:
                log("  (none found)")
            
            # 5. Word bank / chip-like elements (broad search)
            log(f"\n--- WORD BANK / CHIP ELEMENTS (broad search) ---")
            chips = frame.evaluate('''() => {
                // Search for ANY element that looks like a word bank chip
                const selectors = [
                    '.word-chip', '.draggable', '.word-bank-item', '.drag-word', 
                    '.option-chip', '.chip', '.word', '.tag', '.pill', '.badge',
                    '[class*="chip"]', '[class*="word"]', '[class*="drag"]', '[class*="option"]',
                    '[class*="bank"]', '[class*="answer"]', '[class*="token"]',
                    '[class*="pill"]', '[class*="badge"]', '[class*="label"]',
                    '[class*="item"]'
                ];
                const found = [];
                const seen = new Set();
                
                selectors.forEach(sel => {
                    try {
                        document.querySelectorAll(sel).forEach(el => {
                            const key = el.outerHTML.substring(0, 200);
                            if (!seen.has(key)) {
                                seen.add(key);
                                const text = (el.innerText || el.textContent || '').trim();
                                if (text.length > 0 && text.length < 50) {
                                    found.push({
                                        selector: sel,
                                        tag: el.tagName,
                                        text: text,
                                        classes: el.className,
                                        id: el.id,
                                        parentClasses: el.parentElement ? el.parentElement.className : '',
                                        outerHTML: el.outerHTML.substring(0, 300)
                                    });
                                }
                            }
                        });
                    } catch(e) {}
                });
                
                return found;
            }''')
            if chips:
                for c in chips:
                    log(f"  MATCH selector='{c['selector']}' tag=<{c['tag']}> text='{c['text']}' class='{c['classes']}' parent='{c['parentClasses']}'")
                    log(f"    HTML: {c['outerHTML'][:200]}")
            else:
                log("  (none found with standard selectors)")
            
            # 6. Drop zones / blank slots (broad search)
            log(f"\n--- DROP ZONES / BLANK SLOTS (broad search) ---")
            drops = frame.evaluate('''() => {
                const selectors = [
                    '.drop-target', '.droppable', '.gap', '.blank', '.drop-slot',
                    '.sentence-gap', '[data-gap]', '[class*="drop"]', '[class*="gap"]',
                    '[class*="blank"]', '[class*="slot"]', '[class*="target"]',
                    '[class*="placeholder"]', 'span:empty', 'div:empty'
                ];
                const found = [];
                const seen = new Set();
                
                selectors.forEach(sel => {
                    try {
                        document.querySelectorAll(sel).forEach(el => {
                            const key = el.outerHTML.substring(0, 200);
                            if (!seen.has(key)) {
                                seen.add(key);
                                found.push({
                                    selector: sel,
                                    tag: el.tagName,
                                    text: (el.innerText || el.textContent || '').trim().substring(0, 50),
                                    classes: el.className,
                                    outerHTML: el.outerHTML.substring(0, 300)
                                });
                            }
                        });
                    } catch(e) {}
                });
                
                return found;
            }''')
            if drops:
                for d in drops:
                    log(f"  MATCH selector='{d['selector']}' tag=<{d['tag']}> text='{d['text']}' class='{d['classes']}'")
                    log(f"    HTML: {d['outerHTML'][:200]}")
            else:
                log("  (none found)")
            
            # 7. FULL HTML structure dump (outer containers only, not every div)
            log(f"\n--- KEY STRUCTURAL ELEMENTS ---")
            structure = frame.evaluate('''() => {
                const elems = Array.from(document.querySelectorAll('main, section, article, form, nav, [role], [class*="exercise"], [class*="activity"], [class*="content"], [class*="question"], [class*="task"]'));
                return elems.map(el => ({
                    tag: el.tagName,
                    classes: el.className ? el.className.toString().substring(0, 150) : '',
                    role: el.getAttribute('role') || '',
                    childCount: el.children.length,
                    textLen: (el.innerText || '').length
                }));
            }''')
            for s in (structure or []):
                log(f"  <{s['tag']}> class='{s['classes']}' role='{s['role']}' children={s['childCount']} textLen={s['textLen']}")
            
            # 8. ALL elements with click event listeners (heuristic)
            log(f"\n--- ELEMENTS WITH SPECIFIC TEXT FROM WORD BANK (screenshot reference) ---")
            word_bank_words = ['permission', 'done', 'said', 'tickets', 'late', 'letter', 
                              'serious', 'nerves', 'going', 'head', 'lost', 'talking', 'you', 'dirty']
            word_elements = frame.evaluate('''(words) => {
                const found = [];
                const all = Array.from(document.querySelectorAll('*'));
                all.forEach(el => {
                    const text = (el.innerText || el.textContent || '').trim();
                    if (words.includes(text.toLowerCase()) && el.children.length === 0) {
                        found.push({
                            tag: el.tagName,
                            text: text,
                            classes: el.className ? el.className.toString() : '',
                            id: el.id,
                            parentTag: el.parentElement ? el.parentElement.tagName : '',
                            parentClasses: el.parentElement ? (el.parentElement.className ? el.parentElement.className.toString() : '') : '',
                            grandParentClasses: el.parentElement && el.parentElement.parentElement ? (el.parentElement.parentElement.className ? el.parentElement.parentElement.className.toString() : '') : '',
                            outerHTML: el.outerHTML.substring(0, 300),
                            parentHTML: el.parentElement ? el.parentElement.outerHTML.substring(0, 300) : ''
                        });
                    }
                });
                return found;
            }''', word_bank_words)
            
            if word_elements:
                for w in word_elements:
                    log(f"  WORD: '{w['text']}' tag=<{w['tag']}> class='{w['classes']}' parent=<{w['parentTag']}> parentClass='{w['parentClasses']}' gpClass='{w['grandParentClasses']}'")
                    log(f"    HTML: {w['outerHTML'][:250]}")
                    log(f"    PARENT HTML: {w['parentHTML'][:250]}")
            else:
                log("  (no word bank words found as leaf elements)")
            
        except Exception as e:
            log(f"  ERROR in frame #{fi}: {e}")


def test_llm_direct():
    """Test LLM Studio directly with a simple prompt"""
    log("")
    log("Testing LLM Studio directly with simple English exercise...")
    
    try:
        from openai import OpenAI
        import config
        
        client = OpenAI(
            base_url=config.LM_STUDIO_URL,
            api_key="lm-studio"
        )
        
        # Test 1: Ultra-simple prompt
        log("\n--- TEST 1: Ultra-simple English word ---")
        response = client.chat.completions.create(
            model=config.LM_STUDIO_MODEL,
            messages=[
                {"role": "user", "content": "What is the past tense of 'go'? Reply with ONLY the word, nothing else."}
            ],
            temperature=0.1,
            max_tokens=50
        )
        raw1 = response.choices[0].message.content.strip() if response.choices[0].message.content else "(NULL)"
        log(f"  Response: '{raw1}'")
        log(f"  Length: {len(raw1)}")
        
        # Test 2: Exercise-style prompt  
        log("\n--- TEST 2: Fill-in-the-blank exercise ---")
        response2 = client.chat.completions.create(
            model=config.LM_STUDIO_MODEL,
            messages=[
                {"role": "system", "content": "You are an English tutor. Reply with ONLY the answer word."},
                {"role": "user", "content": "Fill in the blank: 'The situation is getting ___.' Options: serious, late, done, lost. Which word fits best?"}
            ],
            temperature=0.2,
            max_tokens=50
        )
        raw2 = response2.choices[0].message.content.strip() if response2.choices[0].message.content else "(NULL)"
        log(f"  Response: '{raw2}'")
        log(f"  Length: {len(raw2)}")
        
        # Test 3: Multi-answer numbered list
        log("\n--- TEST 3: Multiple blanks exercise ---")
        response3 = client.chat.completions.create(
            model=config.LM_STUDIO_MODEL,
            messages=[
                {"role": "system", "content": "Reply with ONLY the answers, one per line."},
                {"role": "user", "content": """Fill in each blank with the correct word from: permission, done, said, tickets, late, letter, serious, nerves, going, head, lost, talking, you, dirty

1. The situation is getting ___.
2. I got an interesting ___ this morning.
3. Can you get me two ___ for the concert?"""}
            ],
            temperature=0.2,
            max_tokens=100
        )
        raw3 = response3.choices[0].message.content.strip() if response3.choices[0].message.content else "(NULL)"
        log(f"  Response: '{raw3}'")
        log(f"  Length: {len(raw3)}")
        
        # Test 4: Raw completion check
        log("\n--- TEST 4: Raw model info ---")
        log(f"  Model name: {config.LM_STUDIO_MODEL}")
        log(f"  API URL: {config.LM_STUDIO_URL}")
        try:
            models = client.models.list()
            for m in models.data:
                log(f"  Available model: {m.id}")
        except Exception as e:
            log(f"  Could not list models: {e}")
        
    except Exception as e:
        log(f"LLM TEST ERROR: {e}")
        import traceback
        log(traceback.format_exc())


if __name__ == "__main__":
    main()
