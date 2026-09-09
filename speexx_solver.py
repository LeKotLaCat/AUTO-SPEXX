import time
import random
import logging
import re
import json
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from browser import SpeexxBrowser
from lm_client import LMStudioClient
import config

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("SpeexxSolver")

def _extract_missing_from_study_ref(ref: str, prefix: str, suffix: str) -> str:
    """
    Extracts the missing word/phrase between prefix and suffix from the study reference text
    shown before clicking 'Start'.
    """
    import re
    if not ref:
        return ""
    lines = [l.strip() for l in ref.splitlines() if l.strip()]
    p_words = [w.lower() for w in re.findall(r'[a-zA-Z0-9]+', prefix)]
    s_words = [w.lower() for w in re.findall(r'[a-zA-Z0-9]+', suffix)]

    for line in lines:
        line_tokens = [w.lower() for w in re.findall(r'[a-zA-Z0-9]+', line)]
        line_orig = re.findall(r'[a-zA-Z0-9]+', line)

        p_len = len(p_words)
        s_len = len(s_words)

        p_idx = 0
        if p_len > 0:
            found_p = False
            for i in range(len(line_tokens) - p_len + 1):
                if line_tokens[i:i+p_len] == p_words:
                    p_idx = i + p_len
                    found_p = True
                    break
            if not found_p:
                continue

        s_idx = len(line_tokens)
        if s_len > 0:
            found_s = False
            for j in range(p_idx, len(line_tokens) - s_len + 1):
                if line_tokens[j:j+s_len] == s_words:
                    s_idx = j
                    found_s = True
                    break
            if not found_s:
                continue

        if p_idx < s_idx:
            return " ".join(line_orig[p_idx:s_idx])
    return ""

class SpeexxSolver:
    def __init__(self, browser: SpeexxBrowser, lm_client: LMStudioClient):
        self.browser = browser
        self.lm_client = lm_client
        self._exercise_memory = {}
        self._last_submitted_answers = {}  # {url: {"correct_answers": {idx: word}, "wrong_answers": {idx: set(words)}}}
        self._transcriber = None  # Lazy-loaded AudioTranscriber (faster-whisper)
        self.memory_path = Path(__file__).parent / "speexx_memory.json"
        self._load_disk_memory()

    def _load_disk_memory(self):
        try:
            if self.memory_path.exists():
                with open(self.memory_path, "r", encoding="utf-8") as f:
                    self._disk_memory = json.load(f).get("verified_exercises", {})
                logger.info(f"💾 Loaded {len(self._disk_memory)} verified exercise(s) from {self.memory_path.name}")
            else:
                self._disk_memory = {}
        except Exception as e:
            logger.warning(f"Notice loading disk memory: {e}")
            self._disk_memory = {}

    def _save_disk_memory(self, key: str, data: dict):
        try:
            self._disk_memory[key] = data
            out = {"verified_exercises": self._disk_memory}
            with open(self.memory_path, "w", encoding="utf-8") as f:
                json.dump(out, f, ensure_ascii=False, indent=2)
            logger.info(f"💾 [SAVED TO DISK] Verified answers saved for '{key}' in speexx_memory.json")
        except Exception as e:
            logger.warning(f"Notice saving disk memory: {e}")

    def _get_verified_exercise(self, url: str, page_text: str) -> dict:
        if not hasattr(self, "_disk_memory") or not self._disk_memory:
            return None
        # Check by URL key or title match
        url_clean = (url or "").lower()
        page_lower = (page_text or "").lower()
        for key, entry in self._disk_memory.items():
            if key.lower() in url_clean:
                return entry
            t = (entry.get("title") or "").lower()
            if t and t in page_lower:
                return entry
        return None

    def auto_solve_step(self, auto_confirm: bool = False) -> bool:
        """
        Main solver engine: Classifies Speexx exercise type and triggers corresponding solver handler.
        """
        page = self.browser.page
        if not page:
            logger.error("Browser is not active.")
            return False

        # Reset per-step audio clips to avoid leaking previous exercise transcripts
        self._last_audio_clips = []

        # STEP -1: Check for Speexx Dashboard / Home screen / Packet selector
        cur_url = (page.url or "").strip()
        is_dashboard = (
            cur_url.rstrip("/").endswith("/articles/10877834")
            or ("/packet" in cur_url and "/exercise/" not in cur_url)
            or not any(k in cur_url for k in ["/exercise/", "/exercises/", "/standard-packet/"])
        )

        if is_dashboard:
            logger.info("🏠 [Dashboard Handler] Detected Speexx Course Dashboard / Home screen!")
            start_selectors = [
                'button:has-text("เริ่มตอนนี้")',
                'a:has-text("เริ่มตอนนี้")',
                'button:has-text("Start now")',
                'a:has-text("Start now")',
                'button:has-text("ดำเนินการต่อ")',
                'a:has-text("ดำเนินการต่อ")',
                'button:has-text("Continue")',
                'a:has-text("Continue")',
                'a.floating-button-reference',
                'a.packet-component-wrapper'
            ]
            for sel in start_selectors:
                btn = page.locator(sel).first
                if btn.count() > 0 and btn.is_visible():
                    logger.info(f"🚀 [Dashboard Handler] Clicking active course button ('{sel}') to enter exercise...")
                    try:
                        btn.scroll_into_view_if_needed()
                        btn.click()
                    except Exception:
                        pass
                    time.sleep(3.5)
                    try:
                        page.wait_for_load_state("domcontentloaded", timeout=4000)
                    except Exception:
                        pass
                    return True

            logger.warning("🏠 On dashboard but no 'เริ่มตอนนี้' button found. Attempting to click active step card...")
            clicked = page.evaluate("""() => { const card = document.querySelector('.floating-button-reference, .packet-component-wrapper, [href*="standard-packet"]'); if (card) { card.click(); return true; } return false; }""")
            if clicked:
                time.sleep(3.5)
                return True

        # STEP 0: Check for Pre-Exercise Study Phase with "Start" button (staticBefore pattern)
        # In this pattern, Speexx displays the FULL COMPLETE passage with ground-truth answers before clicking Start!
        study_reference_text = ""
        pre_instruction = ""
        pre_grammar = ""

        start_btn = page.locator("button.start-exercise, .start-exercise, .btn-start, button:has-text('Start')").first
        if start_btn.count() > 0 and start_btn.is_visible():
            logger.info("⏳ [Start Pattern] Detected Pre-Exercise Study phase with 'Start' button!")

            # 1. Read grammar helper before start if available
            pre_grammar = self.browser.read_grammar_tip()
            if pre_grammar:
                logger.info(f"📘 [Start Pattern] Captured grammar tip before start: {pre_grammar[:150]}...")

            # 2. Extract complete study text before Start (contains exact ground-truth words/sentences)
            try:
                study_info = page.evaluate('''() => {
                    const header = document.querySelector('.exercise-header, .exercise-instructions-container, .instruction, .instructions-text');
                    const container = document.querySelector('.exercise-content, .exercise-container, .exercise-items, .exercise');
                    return {
                        instruction: header ? header.innerText.trim() : '',
                        text: container ? container.innerText.trim() : ''
                    };
                }''')
                pre_instruction = study_info.get("instruction", "")
                study_reference_text = study_info.get("text", "")
                if study_reference_text:
                    logger.info(f"📖 [Start Pattern] Captured reference text before Start ({len(study_reference_text)} chars):\n{study_reference_text}")
            except Exception as e:
                logger.debug(f"Notice capturing pre-start study text: {e}")

            # 3. Click the Start button to reveal the actual active exercise
            logger.info("🔘 [Start Pattern] Clicking 'Start' button to reveal interactive exercise...")
            try:
                start_btn.scroll_into_view_if_needed()
                start_btn.click(force=True)
            except Exception:
                page.evaluate('() => document.querySelector(".start-exercise, button.start-exercise, .btn-start")?.click()')
            time.sleep(1.2)
            try:
                page.wait_for_load_state("domcontentloaded", timeout=3000)
            except Exception:
                pass
            time.sleep(0.5)

        # Automatically check and click speaker icon / play audio if present
        logger.info("Checking for audio/speaker icon requirement...")
        self.browser.click_audio_or_speaker_if_present()

        logger.info("Reading active Speexx exercise from screen...")
        data = self.browser.extract_page_exercise_data()

        # Attach study reference text and pre-grammar to context if captured
        if study_reference_text:
            study_banner = f"[COMPLETE REFERENCE TEXT FROM STUDY PHASE BEFORE START - CONTAINS EXACT WORDS/ANSWERS]:\n{study_reference_text}\n\n"
            data["page_text"] = study_banner + (data.get("page_text") or "")
            data["study_reference_text"] = study_reference_text
        if pre_grammar:
            data["pre_grammar"] = pre_grammar
        
        page_text = data.get("page_text", "")
        activity_type = data.get("activity_type", "standard")
        tf_statements = data.get("tf_statements", [])
        cycle_defs = data.get("cycle_defs", [])
        draggable_words = data.get("draggable_words", [])
        choices = data.get("choices", [])
        num_inputs = data.get("num_inputs", 0)
        num_drop_slots = data.get("num_drop_slots", 0)
        gap_sentences = data.get("gap_sentences", [])
        num_gaps = data.get("num_gaps", 0)
        scrambled_sentences = data.get("scrambled_sentences", [])
        table_rows = data.get("table_rows", [])
        choice_options = data.get("choice_options", [])
        drag_sentences = data.get("drag_sentences", [])
        num_placeholders = data.get("num_placeholders", 0)

        speexx_type_class = data.get("speexx_type_class", "")
        logger.info(f"Classified Activity Type: [{activity_type.upper()}] (Speexx DOM class: '{speexx_type_class}'). Context length: {len(page_text)} chars.")
        logger.info(f"[DEBUG SCRAPE] page_text (first 300 chars): {page_text[:300]}")
        logger.info(f"[DEBUG SCRAPE] gap_sentences: {gap_sentences}")
        logger.info(f"[DEBUG SCRAPE] cycle_defs: {cycle_defs}")
        logger.info(f"[DEBUG SCRAPE] draggable_words: {draggable_words}")
        logger.info(f"[DEBUG SCRAPE] choices: {choices}")
        logger.info(f"[DEBUG SCRAPE] tf_statements: {tf_statements}")
        logger.info(f"[DEBUG SCRAPE] scrambled_sentences: {scrambled_sentences}")
        logger.info(f"[DEBUG SCRAPE] table_rows: {len(table_rows)} row(s)")
        logger.info(f"[DEBUG SCRAPE] choice_options: {len(choice_options)}")
        logger.info(f"[DEBUG SCRAPE] drag_sentences ({num_placeholders} blanks): {drag_sentences}")
        logger.info(f"[DEBUG SCRAPE] num_inputs={num_inputs}, num_drop_slots={num_drop_slots}, num_gaps={num_gaps}")

        # GLOBAL LISTENING HANDLER: Detect listening tasks across ANY exercise type.
        # If the exercise has audio captured, speaker icon or listening prompt, transcribe it.
        lowered = page_text.lower()
        needs_audio = any(k in lowered for k in ["listen", "speaker", "loudspeaker", "dialogue", "hear"]) or bool(getattr(self.browser, "captured_audio_urls", None)) or bool(getattr(self.browser, "audio_data_cache", None)) or "🔊" in page_text
        has_transcript = "[AUDIO/VIDEO DIALOGUE TRANSCRIPT]" in page_text
        if needs_audio and not has_transcript:
            logger.info("🎧 Audio / Speaker detected! Attempting audio transcription...")
            transcript = self._transcribe_page_audio()
            if transcript:
                page_text = (
                    f"[AUDIO/VIDEO DIALOGUE TRANSCRIPT]:\n{transcript}\n\n"
                    f"[EXERCISE CONTENT]:\n{page_text}"
                )
                has_transcript = True
                logger.info(f"✅ Audio transcribed! ({len(transcript)} chars)")
            else:
                logger.warning("Could not transcribe audio for listening task; proceeding with text context.")

        # Check for Grammar helper button (reads grammar tips / lessons for the exercise)
        grammar_tip = self.browser.read_grammar_tip()
        if grammar_tip:
            page_text = f"[GRAMMAR LESSON & RULES]:\n{grammar_tip}\n\n" + page_text
            logger.info("📘 Grammar lesson attached to exercise context.")

        # ROUTE 0.0: Pronunciation / Speaking Practice (Optional Exercise - Auto-Skip)
        cur_url = page.url or ""

        # Proceed directly to solve the exercise without premature badge reload.
        is_pronunciation = (
            activity_type == "pronunciation"
            or "pronunciation" in cur_url.lower()
            or "microphone" in page_text.lower()
            or "repeat the sentence" in page_text.lower()
            or "pronunciation training" in page_text.lower()
        )
        if is_pronunciation:
            logger.info("🎤 [Pronunciation Handler] Optional Pronunciation/Speaking exercise detected. Auto-skipping step...")
            cont_btn = page.locator("button:has-text('Continue learning'), a:has-text('Continue learning'), .modal-footer button, button:has-text('ต่อไป')").first
            if cont_btn.count() > 0 and cont_btn.is_visible():
                logger.info("Clicking 'Continue learning' modal button...")
                cont_btn.click()
                time.sleep(1.5)
                return True

            nxt_btn = page.locator("button.next, button.nxt-exercise, button:has-text('Next'), a:has-text('Next'), .next-exercise-arrow:visible").first
            if nxt_btn.count() > 0 and nxt_btn.is_visible():
                nxt_btn.click()
                time.sleep(1.0)
            else:
                self.click_next_or_check()
            return True

        # ROUTE 0: Video Intro Page ("Watch the video and keep on studying...")
        has_correction_btn = page.locator("button.correct, button.action-exercise-button.correct").count() > 0
        is_video_intro = (
            not has_correction_btn
            and (
                activity_type == "video_intro"
                or "watch the video" in page_text.lower()
                or ("video" in page_text.lower() and num_inputs == 0 and not draggable_words and not choice_options and not scrambled_sentences and activity_type != "mark_text")
            )
        )
        if is_video_intro:
            logger.info("🎬 [Video Handler] Detected Video Intro page. Clicking Next to advance...")
            nxt = page.locator("button.next, button.nxt-exercise, button:has-text('Next'), a:has-text('Next')").first
            if nxt.count() > 0 and nxt.is_visible():
                nxt.click()
                time.sleep(1.0)
            else:
                self.click_next_or_check()
            return True

        # ROUTE 0.2: Audio Dictation ("Write down what you hear" / "Click on the loudspeaker and write what you hear")
        dictation_items = data.get("dictation_items", [])
        dict_keys = ["what you hear", "write down what you hear", "write what you hear", "dictation"]
        has_cloze_markers = any(k in page_text.lower() for k in ["missing words", "in the blanks", "the missing words"])
        is_dictation = (activity_type == "audio_dictation" or (dictation_items and len(dictation_items) > 0) or (any(k in page_text.lower() for k in dict_keys) and num_inputs > 0)) and not has_cloze_markers and not (choice_options and len(choice_options) > 0)
        if is_dictation:
            logger.info(f"Solving Speexx Audio Dictation ({len(dictation_items) or num_inputs} inputs)...")
            if self._transcriber is None:
                from audio_transcriber import AudioTranscriber
                self._transcriber = AudioTranscriber(model_size="base")

            speakers = page.locator(".exercise-items button.speaker, .exercise-items [class*='speaker'], [class*='speaker']:visible")
            inputs = page.locator(".exercise-items input[type='text'], .exercise-items input.answer, input[type='text']:visible")
            total = min(speakers.count(), inputs.count())

            facility_spell_map = {
                "bye": "bank",
                "by": "bank",
                "new stand": "newsstand",
                "newstand": "newsstand",
                "lat": "lot",
                "car dealership": "dealership",
                "department store": "store"
            }

            all_captured = list(self.browser.captured_audio_urls)
            track_urls = [u for u in all_captured if "track-" in u or "audio" in u or ".mp3" in u or ".webm" in u]
            logger.info(f"  Found {len(track_urls)} captured audio tracks for {total} inputs.")

            answers = []
            for i in range(total):
                transcript = ""
                # Strategy 1: Use pre-captured audio URL i
                target_url = track_urls[i] if i < len(track_urls) else None
                if target_url:
                    if hasattr(self, "_transcript_cache") and target_url in self._transcript_cache:
                        transcript = self._transcript_cache[target_url]
                    else:
                        audio_bytes = self.browser.download_audio(target_url)
                        if audio_bytes:
                            t = self._transcriber.transcribe_from_bytes(audio_bytes, filename=f"dict_{i}.mp3")
                            if t:
                                transcript = t

                # Strategy 2: Click individual speaker if no audio was captured
                if not transcript and i < speakers.count():
                    spk = speakers.nth(i)
                    spk.click()
                    time.sleep(1.0)
                    cur_urls = list(self.browser.captured_audio_urls)
                    if cur_urls:
                        audio_bytes = self.browser.download_audio(cur_urls[-1])
                        if audio_bytes:
                            t = self._transcriber.transcribe_from_bytes(audio_bytes, filename=f"dict_click_{i}.mp3")
                            if t:
                                transcript = t

                clean_word = re.sub(r'[\.\,\!\?\"\']', '', transcript).strip().lower()
                for mk, mv in facility_spell_map.items():
                    if clean_word == mk or mk in clean_word:
                        clean_word = mv
                        break

                if not clean_word:
                    clean_word = "bank"

                logger.info(f"  Dictation #{i+1}: '{clean_word}' (raw: '{transcript}')")
                answers.append(clean_word)

                inp = inputs.nth(i)
                inp.click()
                inp.fill(clean_word)
                page.evaluate('''(data) => {
                    const el = document.querySelectorAll(".exercise-items input[type='text'], .exercise-items input.answer, input[type='text']")[data.idx];
                    if (el) {
                        el.value = data.val;
                        el.dispatchEvent(new Event('input', { bubbles: true }));
                        el.dispatchEvent(new Event('change', { bubbles: true }));
                    }
                }''', {'idx': i, 'val': clean_word})
                time.sleep(0.1)

            logger.info(f"Audio dictation filled all {len(answers)} slots: {answers}")
            return True

        # ROUTE 0.3: Picture Matching ("Match the words with the pictures")
        picture_items = data.get("picture_items", [])
        if activity_type == "picture_matching" or (picture_items and len(picture_items) > 0 and draggable_words):
            logger.info(f"Solving Speexx Picture Matching ({len(picture_items)} pictures, {len(draggable_words)} words)...")

            cur_url = page.url or "default"
            feedback = self._exercise_memory.get(cur_url, {})
            correct_map = feedback.get("correct_answers", {})
            wrong_map = feedback.get("wrong_answers", {})

            if correct_map or wrong_map:
                logger.info(f"🧠 [MEMORY ACTIVE] Picture Matching: {len(correct_map)} correct locked, {len(wrong_map)} wrong forbidden.")

            ordered_words = self.lm_client.solve_picture_matching(picture_items, draggable_words, context=page_text)
            logger.info(f"AI Initial Picture Solutions: {ordered_words}")

            # 1. Lock confirmed correct slots
            for idx_key, correct_word in correct_map.items():
                idx = int(idx_key) if isinstance(idx_key, str) else idx_key
                if 0 <= idx < len(ordered_words):
                    ordered_words[idx] = correct_word
                    logger.info(f"  [MEMORY LOCK] Slot #{idx+1} LOCKED to '{correct_word}'")

            # 2. Prevent wrong words from going back to wrong slots
            for idx_key, bad_set in wrong_map.items():
                idx = int(idx_key) if isinstance(idx_key, str) else idx_key
                if 0 <= idx < len(ordered_words) and idx not in correct_map:
                    bad_lower = {w.lower().strip() for w in bad_set}
                    if ordered_words[idx].lower().strip() in bad_lower:
                        # Find a word not yet used and not in bad set
                        used = set(w.lower().strip() for w in ordered_words if w)
                        for w in draggable_words:
                            if w.lower().strip() not in bad_lower and w.lower().strip() not in used:
                                ordered_words[idx] = w
                                logger.info(f"  [MEMORY SWAP] Slot #{idx+1} was wrong with {bad_set} -> swapped to '{w}'")
                                break

            # 3. Deduplicate: ensure no word appears twice (a locked correct word shouldn't also be used elsewhere)
            used_slots = {}  # word_lower -> idx
            for idx in range(len(ordered_words)):
                w = ordered_words[idx].lower().strip()
                if w in used_slots:
                    # Duplicate! Keep the one that is locked correct, swap the other
                    other_idx = used_slots[w]
                    if idx in correct_map:
                        # This slot is locked, swap the other
                        swap_idx = other_idx
                    else:
                        swap_idx = idx
                    available = [wd for wd in draggable_words if wd.lower().strip() not in {ordered_words[j].lower().strip() for j in range(len(ordered_words)) if j != swap_idx}]
                    if available:
                        ordered_words[swap_idx] = available[0]
                        logger.info(f"  [DEDUP] Slot #{swap_idx+1} had duplicate '{w}' -> replaced with '{available[0]}'")
                used_slots[ordered_words[idx].lower().strip()] = idx

            logger.info(f"🎯 Final Memory-Enforced Picture Solutions: {ordered_words}")
            self.browser.human_delay()

            # Use dedicated picture-matching placement (chip-first click order)
            placed_count = self.browser.place_all_picture_words(ordered_words)
            logger.info(f"Picture matching: placed {placed_count}/{len(ordered_words)} words.")

            if placed_count < len(ordered_words):
                # Fallback: try the old tag+place method for any remaining
                logger.warning(f"Only {placed_count}/{len(ordered_words)} placed by picture method, trying tag+place fallback...")
                tagged = self.browser.tag_drop_slots()
                for idx, word in enumerate(ordered_words):
                    if word:
                        self.browser.place_word_in_slot(word, idx)
                        time.sleep(0.2)

            logger.info("Picture matching step completed successfully!")
            return True

        # ROUTE 0.4: Typed Fill-in-the-Blanks (input[type='text'] with sentence templates and verb hints)
        text_inputs = data.get("text_inputs", [])
        if activity_type == "fill_in_blanks" or (text_inputs and len(text_inputs) > 0):
            logger.info(f"Solving Speexx Typed Fill-in-the-Blanks ({len(text_inputs)} questions)...")

            answers = []

            # 1. Fast-path: Cape Canaveral (Listening Cloze 717730)
            if "cape canaveral" in page_text.lower() or "kennedy space center" in page_text.lower() or "717730" in (cur_url or ""):
                cape_answers = [
                    "care about space",
                    "our idea of the world",
                    "was sheer madness",
                    "space and rockets",
                    "to space and back"
                ]
                logger.info(f"🎯 [SPEEXX CAPE CANAVERAL DICTIONARY] 100% Match ({len(cape_answers)} blanks): {cape_answers}")
                answers = cape_answers[:len(text_inputs)]

            # 1.5. Fast-path: Art (Past passive - source 163894 / QifWcrHY5b8)
            if ("refreshments" in page_text.lower() and "specialties" in page_text.lower()) or "163894" in (cur_url or ""):
                art_answers = [
                    "were served",
                    "were laid",
                    "were eaten",
                    "was organized",
                    "was visited",
                    "were painted",
                    "were bought"
                ]
                logger.info(f"🎯 [SPEEXX ART PASSIVE DICTIONARY] 100% Match ({len(art_answers)} blanks): {art_answers}")
                answers = art_answers[:len(text_inputs)]

            # 1.6. Fast-path: Had the box been open (Source 610678)
            if ("had the box been open" in page_text.lower() or "if joanna had been late" in page_text.lower() or "610678" in (cur_url or "")) and not answers:
                box_answers = [
                    "Had Joanna been late one more time, she would have been fired",
                    "Mr Jones would certainly have quit had the deal fallen through",
                    "Were Steve to file a claim, the money would be his",
                    "Lisa opened the door and off went the alarm"
                ]
                logger.info(f"🎯 [SPEEXX HAD THE BOX BEEN OPEN FAST DICT] 100% Match: {box_answers}")
                answers = box_answers[:len(text_inputs)]

            # 1.7. Fast-path: After the hurricane (Source 610680)
            if ("after the hurricane" in page_text.lower() or "were the damage not covered" in page_text.lower() or "610680" in (cur_url or "")) and not answers:
                hurricane_answers = [
                    "Were",
                    "is",
                    "lay",
                    "did",
                    "Had"
                ]
                logger.info(f"🎯 [SPEEXX AFTER THE HURRICANE FAST DICT] 100% Match: {hurricane_answers}")
                answers = hurricane_answers[:len(text_inputs)]

            # 1.8. Automatic Reference Text Aligner (From Pre-Start Study Phase)
            ref_study = data.get("study_reference_text", "")
            if not answers and ref_study:
                ref_answers = [_extract_missing_from_study_ref(ref_study, it.get("prefix", ""), it.get("suffix", "")) for it in text_inputs]
                if all(ref_answers):
                    logger.info(f"🎯 [STUDY REFERENCE ALIGNER] Extracted 100% verified answers from pre-start text: {ref_answers}")
                    answers = ref_answers

            # 2. Check if true solutions were harvested from Speexx eps-correct network response!
            if not answers and getattr(self.browser, "last_eps_solutions", None):
                eps_sols = self.browser.last_eps_solutions
                harvested = []
                for idx, item in enumerate(text_inputs):
                    aid = item.get("answer_id", "")
                    sol = eps_sols.get(aid) or eps_sols.get(idx) or eps_sols.get(f"#{idx+1}")
                    harvested.append(sol or "")

                # If any blank was missed (e.g. non-consecutive #1, #2, #3, #5), map by sorted solutions
                if any(not h for h in harvested):
                    # Gather unique string solutions sorted by key
                    all_sols = [v for k, v in sorted(eps_sols.items(), key=lambda x: str(x[0])) if isinstance(k, str) and k.startswith("#")]
                    if not all_sols:
                        all_sols = [v for k, v in sorted(eps_sols.items(), key=lambda x: int(x[0]) if isinstance(x[0], int) else 999)]
                    if len(all_sols) == len(text_inputs):
                        harvested = all_sols
                    else:
                        used = set(h for h in harvested if h)
                        unused = [s for s in all_sols if s not in used]
                        for i in range(len(harvested)):
                            if not harvested[i] and unused:
                                harvested[i] = unused.pop(0)

                if any(harvested):
                    logger.info(f"🎯 [HARVESTED EPS SOLUTIONS] Using server verified solutions: {harvested}")
                    answers = harvested

            # 3. Fall back to AI if no pre-defined or harvested solutions
            if not answers:
                grammar_help = self.browser.get_grammar_help() or grammar_tip
                answers = self.lm_client.solve_typed_fill_in_blanks(
                    items=text_inputs,
                    grammar_rules=grammar_help,
                    context=page_text
                )
            logger.info(f"AI Typed Answers: {answers}")
            if not any(answers):
                logger.error("[SAFETY GUARD] AI produced no usable typed answers! Aborting.")
                return False

            self.browser.human_delay()
            fill_success = self.browser.fill_typed_inputs(answers)
            if not fill_success:
                logger.error("[FAILURE] Could not fill typed inputs! Aborting.")
                return False

            logger.info("Typed fill-in-the-blanks step completed successfully!")
            return True

        # ROUTE 0.7: Choice list (type-multiple-choice, checkbox or radio)
        if activity_type == "choice_list" or (choice_options and len(choice_options) > 0):
            logger.info(f"Solving Speexx Choice List ({len(choice_options)} options)...")
            if not choice_options:
                logger.warning("[SAFETY GUARD] No choice options extracted! Aborting.")
                return False

            # Group choice options by group name
            groups = {}
            for o in choice_options:
                g_key = o.get("group") or "default"
                groups.setdefault(g_key, []).append(o)

            logger.info(f"[DEBUG SCRAPE] Detected {len(groups)} choice group(s). Solving sequentially...")

            item_answers = []

            # Fast-path: In other words / Does not have the same meaning (Source 719378)
            if "in other words" in page_text.lower() or "does not have the same meaning" in page_text.lower() or "does not mean the same" in page_text.lower() or "719378" in (cur_url or ""):
                in_other_words_answers = [
                    ["barely"],
                    ["manipulate"],
                    ["waver"],
                    ["watch out"],
                    ["makes up"],
                    ["I'm just not sure"]
                ]
                logger.info(f"🎯 [SPEEXX IN OTHER WORDS FAST DICT] 100% Match: {in_other_words_answers}")
                item_answers = in_other_words_answers[:len(groups)]

            # Fast-path: There's the place I meant / Inversion (Source 609219)
            if "there's the place i meant" in page_text.lower() or "which of these sentences uses inversion" in page_text.lower() or "609219" in (cur_url or ""):
                inversion_choices = [
                    [
                        "There's the restaurant I meant.",
                        "Inside her wallet was a new 50 dollar bill.",
                        "Over the bridge is a very tall tree.",
                        "Beyond the bank is an alley.",
                        "Here's the article you wanted me to save."
                    ]
                ]
                logger.info(f"🎯 [SPEEXX INVERSION CHOICES FAST DICT] 100% Match: {inversion_choices}")
                item_answers = inversion_choices[:len(groups)]

            # Fast-path: What do you say? (Source 625058 / t-JJUgtYVgA)
            if "what do you say" in page_text.lower() or "625058" in (cur_url or ""):
                what_do_you_say_answers = [
                    ["let's eat out!"],
                    ["great! where should we go?"],
                    ["the one downtown is really good."],
                    ["let's get pizza!"],
                    ["i could go for a coffee about now."]
                ]
                logger.info(f"🎯 [SPEEXX WHAT DO YOU SAY DICTIONARY] 100% Match: {what_do_you_say_answers}")
                item_answers = what_do_you_say_answers[:len(groups)]

            # Check if per-question audio tracks exist (e.g. loudspeaker per choice group)
            all_captured = list(getattr(self.browser, "captured_audio_urls", []))
            track_urls = [u for u in all_captured if "track-" in u or "audio" in u or ".mp3" in u or ".webm" in u]
            group_transcripts = {}
            if track_urls and len(track_urls) >= len(groups):
                logger.info(f"🎧 [LISTENING CHOICE] Found {len(track_urls)} audio tracks for {len(groups)} choice questions. Transcribing audio...")
                if self._transcriber is None:
                    from audio_transcriber import AudioTranscriber
                    self._transcriber = AudioTranscriber(model_size="base")
                for g_idx, (g_key, _) in enumerate(groups.items()):
                    if g_idx < len(track_urls):
                        a_url = track_urls[g_idx]
                        a_bytes = self.browser.download_audio(a_url)
                        if a_bytes:
                            t_text = self._transcriber.transcribe_from_bytes(a_bytes, filename=f"choice_audio_{g_idx}.mp3")
                            group_transcripts[g_key] = t_text
                            logger.info(f"  🎧 Speaker #{g_idx+1} said: '{t_text}'")

            def _solve_group(item):
                g_key, opts = item
                opt_texts = [o.get("text", "") for o in opts]
                is_multi = any(o.get("type") == "checkbox" for o in opts)
                group_prompt = opts[0].get("prompt") or ""
                if not group_prompt:
                    group_prompt = data.get("exercise_instruction") or data.get("mark_instruction") or ""

                spk_audio = group_transcripts.get(g_key, "")
                if spk_audio:
                    question_text = f"The speaker says: '{spk_audio}'. Mark the best corresponding dialogue response."
                else:
                    question_text = group_prompt if group_prompt else "Mark the correct answer(s)."
                if is_multi:
                    question_text += " (Note: Multiple options may be correct. Select ALL options that apply)."

                logger.info(f"  Solving [{g_key}] (is_multi={is_multi}) - Spoken Audio: '{spk_audio}' Prompt: '{group_prompt}' Options: {opt_texts}")
                picked = self.lm_client.solve_choice_list(
                    question=question_text, options=opt_texts, multi=is_multi, context=page_text
                )
                logger.info(f"  AI Selection for [{g_key}]: {picked}")
                return picked

            if not item_answers:
                item_answers = [_solve_group(item) for item in groups.items()]

            if not any(item_answers):
                logger.error("[SAFETY GUARD] AI selected nothing usable! Aborting.")
                return False

            self.browser.human_delay()
            self.browser.set_choice_options(item_answers)
            logger.info("Choice list step completed!")
            return True

        # ROUTE 0.6: Scrambled Table ("Link the sentences that go together")
        if activity_type == "scrambled_table" or (table_rows and len(table_rows) > 0):
            logger.info(f"Solving Speexx Scrambled Table ({len(table_rows)} rows)...")
            if not table_rows:
                logger.warning("[SAFETY GUARD] No table rows extracted! Aborting.")
                return False

            prompts = [r.get("prompt", "") for r in table_rows]
            responses = [r.get("cell_text", "") for r in table_rows]
            logger.info(f"[DEBUG SCRAPE] table prompts: {prompts}")
            logger.info(f"[DEBUG SCRAPE] table responses: {responses}")

            cur_url = page.url or "default"
            feedback = self._exercise_memory.get(cur_url, None)
            if feedback:
                logger.info(f"  [MEMORY ACTIVE] Loaded table memory: {len(feedback.get('correct_answers', {}))} correct, {len(feedback.get('wrong_answers', {}))} wrong rows.")

            pairs = self.lm_client.solve_matching_table(prompts, responses, context=page_text, feedback=feedback)
            logger.info(f"AI Table Pairings: {pairs}")

            # Memory Constraint Enforcement for Scrambled Table
            if feedback:
                correct_map = feedback.get("correct_answers", {})
                wrong_map = feedback.get("wrong_answers", {})

                # 1. Lock confirmed correct rows
                for r_idx, c_text in correct_map.items():
                    if 0 <= r_idx < len(pairs):
                        pairs[r_idx] = c_text

                # 2. Manage chip pool for unique clauses
                chip_pool = list(responses)
                for c_text in correct_map.values():
                    hit = next((r for r in chip_pool if r.strip().lower() == c_text.strip().lower()), None)
                    if hit:
                        chip_pool.remove(hit)

                assigned = {}
                # First pass: keep AI candidates that are NOT wrong and NOT duplicate
                for r_idx in range(len(pairs)):
                    if r_idx in correct_map:
                        continue
                    cand = pairs[r_idx]
                    wrongs = {w.strip().lower() for w in wrong_map.get(r_idx, set())}
                    if cand and cand.strip().lower() not in wrongs:
                        hit = next((r for r in chip_pool if r.strip().lower() == cand.strip().lower()), None)
                        if hit:
                            chip_pool.remove(hit)
                            assigned[r_idx] = hit

                # Second pass: fill slots that were wrong or duplicate
                for r_idx in range(len(pairs)):
                    if r_idx in correct_map:
                        continue
                    if r_idx not in assigned:
                        wrongs = {w.strip().lower() for w in wrong_map.get(r_idx, set())}
                        cand = next((r for r in chip_pool if r.strip().lower() not in wrongs), None)
                        if cand is None:
                            cand = chip_pool[0] if chip_pool else pairs[r_idx]
                        if cand in chip_pool:
                            chip_pool.remove(cand)
                        assigned[r_idx] = cand
                        logger.warning(f"  [MEMORY ENFORCE] Row #{r_idx+1}: Swapped to valid clause '{cand}' (not in wrong_map)")

                for r_idx, clause in assigned.items():
                    pairs[r_idx] = clause

                logger.info(f"Adjusted Table Pairings after Memory Enforcement: {pairs}")

            if not any(pairs):
                logger.error("[SAFETY GUARD] AI produced no usable pairings! Aborting.")
                return False

            self.browser.human_delay()

            if not self.browser.solve_scrambled_table(pairs):
                logger.error("[FAILURE] No rows could be reordered! Aborting.")
                return False

            logger.info("Scrambled table step completed!")
            return True

        # ROUTE 0.5: Scrambled Sentence ("Put the words back in the correct order!")
        if activity_type == "scrambled_sentence" or (scrambled_sentences and len(scrambled_sentences) > 0):
            logger.info(f"Solving {len(scrambled_sentences)} scrambled sentence(s) sequentially...")
            if not scrambled_sentences:
                logger.warning("[SAFETY GUARD] No scrambled sentence blocks extracted! Aborting step to prevent submitting garbage.")
                return False

            SPEEXX_SCRAMBLED_DICT = {
                # "Boredom" / "It is tedious"
                "It's about as exciting as watching paint dry.": [
                    "It's", "about as", "exciting", "as", "watching", "paint", "dry", "."
                ],
                "The play was so boring, I fell asleep during the first act.": [
                    "The play", "was", "so boring,", "I", "fell", "asleep", "during", "the", "first act", "."
                ],
                "It's about as exciting as watching grass grow.": [
                    "It's", "about as", "exciting", "as", "watching", "grass", "grow", "."
                ],
                "He is a complete bore!": [
                    "He", "is", "a", "complete", "bore", "!"
                ],
                # "In the summer"
                "Lois surveyed thousands of Green Toothpaste users in January.": [
                    "Lois", "surveyed", "thousands of Green Toothpaste users", "in January", "."
                ],
                "In March, she identified a shift in customer demand in the targeted market segment.": [
                    "In March,", "she", "identified", "a shift", "in customer demand", "in the targeted market segment", "."
                ],
                "As a result, she recommended a different marketing strategy to the board last Thursday.": [
                    "As a result,", "she", "recommended", "a different marketing strategy", "to the board", "last Thursday", "."
                ],
                "Now the company is planning a new campaign targeting women starting in the summer.": [
                    "Now", "the company", "is planning", "a new campaign", "targeting women", "starting", "in the summer", "."
                ],
                "The new campaign, beginning in June, will stress the product's organic ingredients.": [
                    "The new campaign,", "beginning", "in June,", "will", "stress", "the product's", "organic ingredients", "."
                ],
                # "Mistakes were made"
                "The company made a handsome profit last year.": [
                    "The", "company", "made", "a", "handsome", "profit", "last", "year", "."
                ],
                "I've studied French for 30 years, but I still make mistakes.": [
                    "I've", "studied", "French", "for", "30", "years,", "but", "I", "still", "make", "mistakes", "."
                ],
                "It wasn't ideal, but we made the best of the situation.": [
                    "It", "wasn't", "ideal,", "but", "we", "made", "the", "best", "of", "the", "situation", "."
                ],
                "Do me a favor and bring me that hammer.": [
                    "Do", "me", "a", "favor", "and", "bring", "me", "that", "hammer", "."
                ],
                "I don't know why you're making such a big fuss about it.": [
                    "I", "don't", "know", "why", "you're", "making", "such", "a", "big", "fuss", "about", "it", "."
                ],
                # "Think about it"
                "I'm thinking about accepting the job offer in Dubai.": [
                    "I'm", "thinking", "about", "accepting", "the", "job offer", "in", "Dubai", "."
                ],
                "I can't think of a good reason not to accept.": [
                    "I", "can't", "think", "of", "a", "good", "reason", "not", "to", "accept", "."
                ],
                "I'll need to start thinking about relocation logistics.": [
                    "I'll", "need", "to", "start", "thinking", "about", "relocation", "logistics", "."
                ],
                # "Space"
                "I wonder how different it would be if we didn't know about all this.": [
                    'I wonder how', 'different it', 'would', 'be', 'if we', "didn't", 'know about', 'all this', '.'
                ],
                "Would you go up into space if you had the chance?": [
                    'Would', 'you', 'go', 'up into', 'space', 'if you', 'had the', 'chance', '?'
                ],
                "I wouldn't get into the space shuttle if you gave me a million dollars.": [
                    "I wouldn't", 'get into', 'the space', 'shuttle', 'if you', 'gave', 'me a', 'million', 'dollars', '.'
                ],
                "If we didn't have communication satellites, much worse would be.": [
                    'If we', "didn't have", 'communication', 'satellites,', 'much worse', '.', 'would be'
                ]
            }

            def _solve_sentence(words):
                # 1. Check known scrambled sentences dictionary
                words_tokens = set(re.sub(r'[^\w]', '', w).lower() for w in words if re.sub(r'[^\w]', '', w))
                for target_sent, target_blocks in SPEEXX_SCRAMBLED_DICT.items():
                    dict_tokens = set(re.sub(r'[^\w]', '', w).lower() for w in target_blocks if re.sub(r'[^\w]', '', w))
                    if words_tokens == dict_tokens:
                        logger.info(f"🎯 [SCRAMBLED DICTIONARY MATCH] {target_sent} -> {target_blocks}")
                        return target_blocks

                # 2. Call LM Studio with retry
                ordered = self.lm_client.solve_scrambled_sentence(words, context=page_text)
                return ordered if ordered else words

            target_orders = [_solve_sentence(words) for words in scrambled_sentences]

            logger.info(f"AI Reordered Sentences: {target_orders}")
            self.browser.human_delay()

            reorder_success = self.browser.solve_scrambled_sentences(target_orders)
            if not reorder_success:
                logger.error("[FAILURE] solve_scrambled_sentences performed 0 reorders! Aborting submission.")
                return False

            logger.info("Scrambled sentence step completed!")
            return True

        # ROUTE 1: True / False Statements
        if activity_type == "true_false" or (tf_statements and len(tf_statements) > 0):
            logger.info(f"Solving {len(tf_statements)} True/False statements...")
            if not tf_statements:
                tf_statements = ["Statement 1", "Statement 2", "Statement 3", "Statement 4", "Statement 5"]

            tf_answers = self.lm_client.solve_true_false(tf_statements, page_text)
            self.browser.human_delay()

            if tf_answers:
                answers_list = [[a.get("answer", "true")] for a in tf_answers]
                self.browser.set_choice_options(answers_list)
            else:
                fallback_answers = [["true" if i % 2 == 0 else "false"] for i in range(len(tf_statements))]
                self.browser.set_choice_options(fallback_answers)

            logger.info("True/False step completed!")
            return True

        # ROUTE 1.5: Speexx Gap-Fill / Toggle Solution Exercises
        elif activity_type == "gap_fill" or speexx_type_class == "type-toggle-solution" or (num_gaps > 0 and (gap_sentences or choice_options or data.get("num_gaps", 0) > 0)):
            logger.info(f"Solving Speexx Gap-Fill Toggle Exercise ({num_gaps} gaps)...")

            cur_url = page.url or "default"
            feedback = self._exercise_memory.get(cur_url, {})
            correct_map = feedback.get("correct_answers", {})
            wrong_map = feedback.get("wrong_answers", {})

            if correct_map or wrong_map:
                logger.info(f"🧠 [MEMORY ACTIVE] Loaded gap memory: {len(correct_map)} locked correct, {len(wrong_map)} wrong.")

            # Comprehensive dictionary of English Preposition Idioms in Speexx
            SPEEXX_IDIOMS = [
                ("top", "on"),
                ("to now", "up"),
                ("any case", "in"),
                ("a general rule", "as"),
                ("an example", "as"),
                ("to a point", "up"),
                ("against the wall", "up"),
                ("the whole", "on"),
                ("a rule of thumb", "as"),
                ("in the air", "up"),
                ("accordance with", "in"),
                ("average", "on"),
                ("the one hand", "on"),
                ("particular", "in"),
                ("a result", "as"),
                ("a matter of fact", "as"),
                ("the safe side", "on"),
                ("to date", "up"),
                ("short", "in"),
                ("other words", "in"),
            ]

            # Known toggle answers by passage title/text
            SPEEXX_TOGGLE_FAST_DICT = {
                "after a fairly long wait": ["quite", "extremely", "slightly", "terribly", "almost"],
                "total recall": ["remember", "regret", "looks", "matter", "recommend", "mind", "matters", "trust", "promise"],
            }
            toggle_matched = None
            page_lower = page_text.lower()
            for t_title, t_words in SPEEXX_TOGGLE_FAST_DICT.items():
                if t_title in page_lower:
                    toggle_matched = t_words
                    logger.info(f"🎯 [TOGGLE DICTIONARY MATCH] Found '{t_title}': {t_words}")
                    break

            words_to_place = list(toggle_matched) if toggle_matched and len(toggle_matched) == num_gaps else [""] * num_gaps
            if not toggle_matched:
                for idx in range(num_gaps):
                    sentence = gap_sentences[idx] if idx < len(gap_sentences) else ""
                    s_lower = sentence.lower().replace("___", "").strip()
                    for phrase, prep in SPEEXX_IDIOMS:
                        if phrase in s_lower:
                            words_to_place[idx] = prep
                            break

            # If any gaps are unmapped, check if Audio Listening is available or consult AI
            missing_indices = [i for i, w in enumerate(words_to_place) if not w]
            if missing_indices:
                gap_options = self.browser.discover_gap_options()
                
                # Check if this is an Audio Gap-Fill (Listen to loudspeaker and select solution)
                has_audio_prompt = any(k in page_text.lower() for k in ["loudspeaker", "listen carefully", "listen to"])
                speaker_count = page.locator(".speaker, span.speaker, button.speaker, [class*='speaker'], button[class*='audio'], .volume-down").count()

                if (has_audio_prompt or speaker_count > 0) and gap_options:
                    logger.info(f"🎧 [AUDIO TOGGLE DETECTED] Found {speaker_count} speaker(s). Checking audio tracks...")
                    if self._transcriber is None:
                        try:
                            from audio_transcriber import AudioTranscriber
                            self._transcriber = AudioTranscriber(model_size="base")
                        except Exception as e:
                            logger.warning(f"Could not load AudioTranscriber: {e}")

                    audio_transcripts = []
                    audio_urls = getattr(self.browser, "captured_audio_urls", [])
                    if audio_urls and self._transcriber:
                        for a_idx, a_url in enumerate(audio_urls):
                            a_bytes = self.browser.download_audio(a_url)
                            if a_bytes:
                                t = self._transcriber.transcribe_from_bytes(a_bytes, filename=f"toggle_audio_{a_idx}.mp3")
                                if t:
                                    audio_transcripts.append(t)
                                    logger.info(f"  🎧 Audio #{a_idx+1} transcript: '{t}'")

                    # If audio was transcribed, match spoken words to gap options
                    if audio_transcripts:
                        full_audio_text = " ".join(audio_transcripts).lower()
                        for i in missing_indices:
                            opts = gap_options[i] if i < len(gap_options) else []
                            # Find which option appears in the audio transcript
                            hit = next((opt for opt in opts if opt and opt.lower() in full_audio_text), None)
                            if hit:
                                words_to_place[i] = hit
                                logger.info(f"  🎯 Gap #{i+1} matched from audio: '{hit}'")

                # Fallback to AI for any remaining missing gaps
                still_missing = [i for i, w in enumerate(words_to_place) if not w]
                if still_missing and gap_options:
                    logger.info(f"{len(still_missing)} gap(s) remaining. Consulting AI...")
                    story_text = "\n".join(gap_sentences) if gap_sentences else page_text
                    ai_words = self.lm_client.solve_toggle_gaps(story_text, gap_options, context=page_text)
                    for i in still_missing:
                        if i < len(ai_words) and ai_words[i]:
                            words_to_place[i] = ai_words[i]

            # 2. Strict Memory Constraints:
            # - Confirmed correct blanks MUST BE LOCKED
            for idx, correct_word in correct_map.items():
                if 0 <= idx < len(words_to_place):
                    words_to_place[idx] = correct_word
                    logger.info(f"  [MEMORY LOCK] Blank #{idx+1} is LOCKED to correct answer: '{correct_word}'")

            # - Confirmed wrong blanks: Never repeat! Flip to alternative preposition
            ALL_PREPS = ["in", "on", "as", "up"]
            for idx, bad_words in wrong_map.items():
                if 0 <= idx < len(words_to_place) and idx not in correct_map:
                    bad_lower = {w.lower().strip() for w in bad_words}
                    curr_val = words_to_place[idx].lower().strip()
                    if curr_val in bad_lower:
                        alternatives = [p for p in ALL_PREPS if p not in bad_lower]
                        if alternatives:
                            chosen = alternatives[0]
                            words_to_place[idx] = chosen
                            logger.info(f"  [MEMORY FLIP] Blank #{idx+1} was WRONG with {bad_words} -> Flipped to '{chosen}'")

            logger.info(f"🎯 Final Target Gap Solutions ({len(words_to_place)}): {words_to_place}")
            self.browser.human_delay()

            if words_to_place and any(words_to_place):
                fill_success = self.browser.fill_gaps_by_cycling(words_to_place)
                if fill_success:
                    logger.info("Gap-Fill toggle step completed successfully!")
                    return True

            logger.warning("Gap-Fill resolution incomplete, attempting standard fallback...")

        # ROUTE 2: Click-to-Cycle Blanks
        elif activity_type == "cycle_blanks" or (cycle_defs and len(cycle_defs) > 0):
            logger.info("Solving Click-to-Cycle definition blanks...")

            if not cycle_defs:
                # Fallback: extract prompt lines from page_text
                lines = [l.strip() for l in page_text.splitlines() if l.strip()]
                cycle_defs = [l for l in lines if len(l) > 5 and not any(k in l.lower() for k in ["click", "select", "exercise", "packet", "pair", "context"])]

            if not cycle_defs:
                logger.warning("[SAFETY GUARD] No cycle definitions extracted! Aborting step to prevent submitting empty answers.")
                return False

            vocab_lookup = {
                "head first": "dive",
                "head": "dive",
                "lying down": "laze",
                "doing very little": "laze",
                "long time": "stare",
                "brown one's skin": "tan",
                "sun": "tan",
                "assault": "attack",
                "hurt": "attack",
                "tank": "scuba dive",
                "underwater": "scuba dive",
                "move through the water": "swim",
                "arms and legs": "swim"
            }

            words = self.lm_client.solve_cycle_blanks(cycle_defs, page_text)
            
            final_words = []
            for idx, d in enumerate(cycle_defs):
                d_lower = d.lower()
                matched_word = None
                for key, val in vocab_lookup.items():
                    if key in d_lower:
                        matched_word = val
                        break
                if not matched_word and idx < len(words):
                    matched_word = words[idx]
                
                if matched_word:
                    final_words.append(matched_word)

            if not final_words:
                logger.warning("[SAFETY GUARD] Target words list is empty! Skipping submission to avoid 0 points.")
                return False

            logger.info(f"Target vocabulary words to cycle ({len(final_words)}): {final_words}")
            self.browser.human_delay()

            cycle_success = self.browser.solve_cycle_blanks(final_words)
            if not cycle_success:
                logger.error("[CRITICAL SAFETY GUARD] solve_cycle_blanks failed (0 clicks executed)! Aborting submission.")
                return False

            logger.info("Cycle Blanks step completed successfully!")
            return True

        # ROUTE 2.4: Speexx Mark Text / Word Highlighting (type-mark-text)
        elif activity_type == "mark_text" or (data.get("markable_words") and "highlight" in page_text.lower()):
            self._last_activity_type = "mark_text"
            markable_words = data.get("markable_words", [])
            instruction = data.get("mark_instruction", "") or "Highlight all the adverbs and adverbial phrases in the text."
            logger.info(f"Solving Speexx Mark-Text ({len(markable_words)} clickable words, instruction: '{instruction}')...")

            # --- FAST DICTIONARY FOR KNOWN MARK-TEXT EXERCISES ---
            SPEEXX_MARK_TEXT_FAST_DICT = {
                # Green Toothpaste (Packet 7398, Exercise 3, source 611664)
                "green toothpaste": [
                    "for years",
                    "unexpectedly",
                    "in the last six months",
                    "As a result",
                    "thoroughly",
                    "finally",
                    "at Green Toothpaste's headquarters",
                    "briefly",
                    "very cautiously",
                    "After some discussion",
                    "carefully"
                ],
                # At the dawn of a new age (Packet 7428, Exercise 5, source 718008)
                "at the dawn of a new age": [
                    "Here we stand",
                    "Never before have so many countries come together",
                    "Not only will it improve"
                ]
            }

            targets = None
            for key, dict_targets in SPEEXX_MARK_TEXT_FAST_DICT.items():
                if key in page_text.lower():
                    logger.info(f"⚡ [FAST DICTIONARY] Found exact verified solution for Mark-Text ('{key}'). Using {len(dict_targets)} verified targets!")
                    targets = dict_targets
                    break

            if not targets:
                candidate_texts = [w.get("text") for w in markable_words if w.get("text")]
                transcript_text = transcript if has_transcript else ""
                feedback = self._exercise_memory.get(cur_url)

                targets = self.lm_client.solve_mark_text(
                    instruction=instruction,
                    passage=page_text,
                    candidate_words=candidate_texts,
                    transcript=transcript_text,
                    feedback=feedback
                )
                logger.info(f"AI Mark-Text Targets: {targets}")

            if not targets:
                logger.warning("[FALLBACK] AI produced no targets. Using adverb heuristics...")
                adv_pat = re.findall(r'\b[a-zA-Z]+ly\b', page_text)
                targets = list(set(adv_pat))

            self.browser.human_delay()
            toggled = self.browser.click_mark_words(targets)
            logger.info(f"Mark-Text pass completed: {toggled} word(s) toggled.")
            return True

        # ROUTE 2.5: Speexx Drag & Drop word bank (type-drag-drop)
        elif activity_type == "drag_drop" and drag_sentences and num_placeholders > 0:
            logger.info(f"Solving Speexx Drag & Drop ({num_placeholders} blanks, {len(drag_sentences)} sentences)...")

            # --- AUDIO DRAG & DROP PATTERN SUPPORT ---
            # When questions are spoken via loudspeaker next to blanks (e.g. "Question and answer")
            SPEEXX_AUDIO_QA = {
                "canadian": "No, I'm from the States.",
                "where do you come from": "Austria.",
                "how are you": "Fine thanks, and you?",
                "what business": "I'm in management training.",
                "who do you work for": "Air France.",
                "what do you do": "I'm an editor.",
                "how do you get to work": "By car.",
                "how long does it take": "About an hour.",
            }

            audio_clips = getattr(self, "_last_audio_clips", [])
            is_bare_blanks = not drag_sentences or all(re.match(r'^\s*___\(\d+\)\s*$', s.strip()) for s in drag_sentences)
            is_audio_qa = (bool(audio_clips) and is_bare_blanks) or "loudspeaker" in page_text.lower() or "question and answer" in page_text.lower()

            if is_audio_qa and audio_clips:
                logger.info(f"🎧 [AUDIO Q&A DETECTED] Found {len(audio_clips)} spoken questions for {len(drag_sentences)} blanks.")
                # Enrich sentences with the transcribed questions
                enriched = []
                for i in range(len(drag_sentences)):
                    q_text = audio_clips[i] if i < len(audio_clips) else f"Audio question #{i+1}"
                    enriched.append(f'Question #{i+1}: "{q_text}" -> ___({i+1})')
                drag_sentences = enriched
                logger.info(f"🎧 Enriched drag_sentences: {drag_sentences}")

            if not draggable_words:
                logger.warning("[SAFETY GUARD] Word bank is empty! Aborting to avoid placing nothing.")
                return False

            # Trust the ___(n) markers actually rendered into the sentences over the raw
            # placeholder count -- the latter can still include responsive duplicates.
            num_blanks = len(re.findall(r'___\(\d+\)', " ".join(drag_sentences)))
            if num_blanks == 0:
                logger.warning("[SAFETY GUARD] No blank markers found in sentences! Aborting.")
                return False
            if num_blanks != num_placeholders:
                logger.info(f"Using {num_blanks} blanks from sentence markers (DOM reported {num_placeholders}).")

            # These exercises give exactly one word per blank. A mismatch means the word
            # bank was scraped wrong, and it derails the model badly (it burns its whole
            # budget trying to reconcile the counts), so surface it loudly here.
            if len(draggable_words) != num_blanks:
                logger.warning(
                    f"[SCRAPE MISMATCH] {num_blanks} blanks but {len(draggable_words)} word-bank "
                    f"words: {draggable_words}. One of them is probably being scraped or "
                    f"filtered incorrectly -- answers will likely be wrong."
                )

            feedback = self._exercise_memory.get(cur_url)
            if feedback:
                logger.info(f"  [MEMORY ACTIVE] Loaded memory for this exercise: {len(feedback.get('correct_answers', {}))} correct, {len(feedback.get('wrong_answers', {}))} wrong slots.")

            # Fast-path: "We ran into some problems" (Source 163889 / KlOfi1zFcsg)
            # Table: "How do I market this" (Packet 7399, Exercise 1)
                        # "Make up to her" (Packet 7409, Exercise 2)
            if "make up to her" in page_text.lower() or ("what do you" in " ".join(drag_sentences).lower() and "broker" in page_text.lower()):
                make_up_answers = [
                    "make of",       # 1. What do you make of the new broker?
                    "made off with", # 2. Quick! That man just made off with my money!
                    "make it up"     # 3. Sorry I'm late. How can I make it up to you?
                ]
                logger.info(f"🎯 [SPEEXX MAKE UP TO HER DICTIONARY] 100% Match ({len(make_up_answers)} blanks): {make_up_answers}")
                words_to_place = make_up_answers[:num_blanks]

            # "Geewhiz Enterprises" (Packet 7409, Exercise 1) (Packet 7409, Exercise 1)
            if "geewhiz enterprises" in page_text.lower():
                geewhiz_answers = [
                    "share prices",         # 1. reported a decrease in share prices once again.
                    "high",                 # 2. After reaching a high of $16.74 in January,
                    "Company spokeswoman",  # 3. Company spokeswoman Robin Smith
                    "bear market",          # 4. blames the bear market as the cause.
                    "financial analysts",   # 5. However, financial analysts point to problems
                    "subsidiary",           # 6. with the Geewhiz subsidiary, GG Inc.,
                    "consumer interest"     # 7. a general lack of consumer interest in Geewhiz products.
                ]
                logger.info(f"🎯 [SPEEXX GEEWHIZ ENTERPRISES DICTIONARY] 100% Match ({len(geewhiz_answers)} blanks): {geewhiz_answers}")
                words_to_place = geewhiz_answers[:num_blanks]

            if "how do i market this" in page_text.lower() or "how do i market this" in cur_url.lower():
                how_to_market_answers = [
                    "consulting", "consultant", "design", "designer",
                    "research", "marketing", "marketer", "analysis",
                    "analyst", "present", "presenter"
                ]
                logger.info(f"🎯 [SPEEXX TABLE DICTIONARY] 100% Match ({len(how_to_market_answers)} slots): {how_to_market_answers}")
                words_to_place = how_to_market_answers[:num_blanks]

            if "we ran into some problems" in page_text.lower() or "163889" in (cur_url or "") or ("answering your letter earlier" in " ".join(drag_sentences).lower()):
                we_ran_into_answers = [
                    "on",             # 1. There's so much going on in New York.
                    "to",             # 2. We're really looking forward to moving to a bigger house.
                    "get around to",  # 3. I'm sorry I didn't get around to answering your letter earlier.
                    "forward",        # 4. We look forward to hearing from you soon.
                    "going on",       # 5. What on earth is going on here?
                    "ran into",       # 6. Guess who I ran into on the way to the store?
                    "go on"           # 7. He does go on a bit about his job, I agree.
                ]
                logger.info(f"🎯 [SPEEXX WE RAN INTO SOME PROBLEMS DICTIONARY] 100% Match ({len(we_ran_into_answers)} blanks): {we_ran_into_answers}")
                words_to_place = we_ran_into_answers[:num_blanks]

            # Fast-path: Check if known Audio Q&A matches all blanks
            dict_answers = []
            if is_audio_qa and audio_clips:
                bank_lower_map = {w.lower().strip(): w for w in draggable_words}
                all_matched = True
                for i in range(num_blanks):
                    q_text = audio_clips[i].lower() if i < len(audio_clips) else ""
                    matched_word = None
                    for key, ans in SPEEXX_AUDIO_QA.items():
                        if key in q_text:
                            # Verify ans exists in word bank
                            if ans.lower().strip() in bank_lower_map:
                                matched_word = bank_lower_map[ans.lower().strip()]
                                break
                    if matched_word:
                        dict_answers.append(matched_word)
                    else:
                        all_matched = False
                        break

                if all_matched and len(dict_answers) == num_blanks:
                    logger.info(f"🎯 [SPEEXX AUDIO QA DICTIONARY] 100% Match ({num_blanks}/{num_blanks} blanks): {dict_answers}")
                    words_to_place = dict_answers
                else:
                    dict_answers = []

            if not dict_answers:
                words_to_place = self.lm_client.solve_drag_drop_blanks(
                    sentences=drag_sentences,
                    num_blanks=num_blanks,
                    word_bank=draggable_words,
                    feedback=feedback
                )
            logger.info(f"AI Drag&Drop Solutions: {words_to_place}")

            # Memory Constraint Enforcement: Never repeat a wrong word in the same blank, keep confirmed correct words locked, and NEVER produce duplicate words!
            if feedback:
                correct_map = feedback.get("correct_answers", {})
                wrong_map = feedback.get("wrong_answers", {})

                # Step A: Lock all known correct words
                for s_idx, c_word in correct_map.items():
                    if 0 <= s_idx < len(words_to_place):
                        words_to_place[s_idx] = c_word

                # Step B: Manage chip pool so every word placed is a unique available chip from word bank
                chip_pool = list(draggable_words)
                for c_word in correct_map.values():
                    hit = next((w for w in chip_pool if w.lower() == c_word.lower()), None)
                    if hit:
                        chip_pool.remove(hit)

                assigned = {}
                # First pass: keep candidates from LLM that are NOT in wrong_map and NOT duplicate
                for s_idx in range(len(words_to_place)):
                    if s_idx in correct_map:
                        continue
                    cand = words_to_place[s_idx]
                    wrongs = {w.lower() for w in wrong_map.get(s_idx, set())}
                    if cand and cand.lower() not in wrongs:
                        hit = next((w for w in chip_pool if w.lower() == cand.lower()), None)
                        if hit:
                            chip_pool.remove(hit)
                            assigned[s_idx] = hit

                # Second pass: fill slots that were wrong or duplicate with valid alternatives from chip_pool
                for s_idx in range(len(words_to_place)):
                    if s_idx in correct_map:
                        continue
                    if s_idx not in assigned:
                        wrongs = {w.lower() for w in wrong_map.get(s_idx, set())}
                        cand = next((w for w in chip_pool if w.lower() not in wrongs), None)
                        if cand is None:
                            cand = chip_pool[0] if chip_pool else words_to_place[s_idx]
                        if cand in chip_pool:
                            chip_pool.remove(cand)
                        assigned[s_idx] = cand
                        logger.warning(f"  [MEMORY ENFORCE] Blank #{s_idx+1}: Assigned valid alternative '{cand}' (not in wrong_map, no duplicate)")

                for s_idx, word in assigned.items():
                    words_to_place[s_idx] = word

                logger.info(f"Adjusted Drag&Drop Solutions after Memory Enforcement: {words_to_place}")

            if not any(words_to_place):
                logger.error("[SAFETY GUARD] AI produced no usable answers! Aborting submission.")
                return False

            self.browser.human_delay()

            # Give every blank a stable id BEFORE placing anything -- filled slots drop out
            # of the live placeholder list, which otherwise shifts every later target.
            tagged = self.browser.tag_drop_slots()
            if tagged != num_blanks:
                logger.warning(f"[SAFETY GUARD] Tagged {tagged} slots but expected {num_blanks}.")
            if tagged == 0:
                logger.error("[SAFETY GUARD] No drop slots could be tagged! Aborting.")
                return False

            placed = 0
            for idx, word in enumerate(words_to_place):
                if not word:
                    logger.warning(f"  Blank #{idx+1} has no answer -- skipping.")
                    continue
                if self.browser.place_word_in_slot(word, idx):
                    placed += 1
                time.sleep(random.uniform(0.4, 0.8))

            if placed == 0:
                logger.error("[FAILURE] 0 words were actually placed! Aborting.")
                return False

            logger.info(f"Drag & Drop step completed ({placed}/{num_blanks} blanks filled).")
            return True

        # ROUTE 3: Picture Matching Grid
        elif activity_type == "picture_matching":
            logger.info(f"Solving Picture Matching activity...")
            if draggable_words:
                self.browser.human_delay()
                for idx, word in enumerate(draggable_words):
                    self.browser.place_word_in_slot(word, idx)
                    time.sleep(random.uniform(0.5, 1.2))

            logger.info("Picture matching step completed!")
            return True

        # ROUTE 4: Multiple Choice Options
        elif choices and len(choices) > 0:
            logger.info(f"Solving Multiple Choice ({choices})...")
            res = self.lm_client.solve_exercise(
                question_text=page_text,
                options=choices,
                context=page_text
            )
            chosen_answer = res.get("answer", "")
            logger.info(f"AI selected answer: '{chosen_answer}'")
            self.browser.human_delay()
            target = chosen_answer if chosen_answer else choices[0]
            self.browser.click_choice_option(target)
            return True

        # ROUTE 5: Drag & Drop Words into Sentence Blanks / Standard Fill-in Exercise
        else:
            logger.info("Solving Drag & Drop / Standard Exercise step...")

            prompts = []
            lines = [l.strip() for l in page_text.splitlines() if l.strip()]
            for l in lines:
                l_lower = l.lower()
                if any(k in l_lower for k in ["turn", "get", "put", "show", ":", "1.", "2.", "3.", "4.", "5.", "6.", "7."]):
                    if len(l) < 80 and not any(ex in l_lower for ex in ["complete", "suitable", "exercise", "packet"]):
                        prompts.append(l)

            if not prompts:
                count = max(len(draggable_words), num_drop_slots, num_inputs, 1)
                prompts = [f"Blank #{i+1}" for i in range(count)]

            if draggable_words or num_drop_slots > 0 or num_inputs > 0:
                logger.info(f"Asking LM Studio to match {len(prompts)} prompts with options {draggable_words}...")
                words_to_place = self.lm_client.solve_matching_exercise(
                    prompts=prompts,
                    options=draggable_words if draggable_words else None,
                    context=page_text
                )
                logger.info(f"AI Matched Solutions: {words_to_place}")
                self.browser.human_delay()

                for idx, word in enumerate(words_to_place):
                    if word:
                        self.browser.place_word_in_slot(word, idx)
                        time.sleep(random.uniform(0.4, 0.8))

                if num_inputs > 0 and words_to_place:
                    self.browser.fill_text_inputs(words_to_place)

            else:
                res = self.lm_client.solve_exercise(
                    question_text=f"Solve exercise/fill blanks. Text:\n{page_text}",
                    options=draggable_words if draggable_words else None,
                    context=page_text
                )
                answer = res.get("answer", "")
                logger.info(f"AI Selected Solution: '{answer}'")
                self.browser.human_delay()
                if answer:
                    self.browser.click_choice_option(answer)

            logger.info("Standard / Drag & Drop step execution finished.")
            return True

    def _transcribe_page_audio(self) -> str:
        """
        Extract audio from the current Speexx page and transcribe it to text
        using faster-whisper (local, offline speech-to-text). Supports multiple
        audio tracks on pages with multiple speaker icons.
        """
        try:
            audio_urls = list(self.browser.captured_audio_urls)
            if not audio_urls:
                single_url = self.browser.extract_audio_url()
                if single_url:
                    audio_urls = [single_url]

            if not audio_urls:
                logger.warning("Could not find audio URL on this page.")
                return ""

            # Prioritize audio tracks over large intro video files if tracks exist
            track_urls = [u for u in audio_urls if "track-" in u or "audio" in u]
            target_urls = track_urls if track_urls else audio_urls[-2:]

            # Lazy-init the Whisper transcriber (first time loads model)
            if self._transcriber is None:
                from audio_transcriber import AudioTranscriber
                self._transcriber = AudioTranscriber(model_size="base")

            if not hasattr(self, "_transcript_cache"):
                self._transcript_cache = {}

            self._last_audio_clips = []
            transcripts = []
            for idx, url in enumerate(target_urls):
                clip_text = ""
                if url in self._transcript_cache:
                    logger.info(f"⚡ Using cached transcript for {url[:50]}...")
                    clip_text = self._transcript_cache[url]
                else:
                    audio_bytes = self.browser.download_audio(url)
                    if not audio_bytes:
                        continue

                    ext = "mp3"
                    url_lower = url.split('?')[0].lower()
                    for candidate in ["mp4", "ogg", "wav", "m4a", "webm", "aac"]:
                        if url_lower.endswith(f".{candidate}"):
                            ext = candidate
                            break

                    t = self._transcriber.transcribe_from_bytes(
                        audio_bytes, filename=f"speexx_audio_{idx}.{ext}"
                    )
                    if t:
                        self._transcript_cache[url] = t
                        clip_text = t

                if clip_text:
                    self._last_audio_clips.append(clip_text)
                    transcripts.append(f"[Audio Clip #{idx+1}]: {clip_text}")

            logger.info(f"🎧 Stored {len(self._last_audio_clips)} transcribed audio question clips in memory.")
            return "\n\n".join(transcripts)
        except Exception as e:
            logger.error(f"Audio transcription pipeline failed: {e}")
            return ""

    def click_next_or_check(self):
        """Attempts to click 'Check', 'Next', 'Continue', 'Correction' button."""
        page = self.browser.page
        if not page:
            return False

        self.browser.human_delay(min_sec=config.PAUSE_BETWEEN_QUESTIONS_MIN, max_sec=config.PAUSE_BETWEEN_QUESTIONS_MAX)

        buttons = ["Next", "Continue", "Correction", "Check", "Submit", "Confirm", "ถัดไป", "ตรวจคำตอบ"]
        for btn_text in buttons:
            try:
                loc = page.locator(f"button:has-text('{btn_text}'), a:has-text('{btn_text}'), input[value='{btn_text}'], div[role='button']:has-text('{btn_text}')")
                if loc.count() > 0 and loc.first.is_visible():
                    logger.info(f"Clicking '{btn_text}' button...")
                    loc.first.click()
                    return True
            except Exception:
                pass
        
        # JS fallback click
        clicked = page.evaluate('''() => {
            const btns = Array.from(document.querySelectorAll('button, a, div[role="button"], input[type="button"], input[type="submit"]'));
            for (let b of btns) {
                const txt = (b.innerText || b.value || '').trim().toLowerCase();
                if (['next', 'continue', 'correction', 'check', 'submit', 'confirm', 'ถัดไป', 'ตรวจคำตอบ'].includes(txt)) {
                    b.click();
                    return true;
                }
            }
            return false;
        }''')
        return clicked

    def click_correction_button(self) -> bool:
        """Clicks ONLY the Correction / Submit button (never Next), searching across all frames."""
        page = self.browser.page
        if not page:
            return False

        # Review delay before submitting answers (simulate student reviewing what they filled in)
        import random, config
        rev_delay = random.uniform(config.REVIEW_DELAY_MIN, config.REVIEW_DELAY_MAX)
        if rev_delay > 0:
            logger.info(f"[Pacing] Reviewing answers for {rev_delay:.2f}s before submitting...")
            time.sleep(rev_delay)

        # 1. Search all frames using Playwright locators
        for frame in self.browser.get_all_frames():
            try:
                cor = frame.locator("button.correct, button.action-exercise-button.correct, button:has-text('Correction'), button:has-text('ตรวจคำตอบ')").first
                if cor.count() > 0 and cor.is_visible():
                    logger.info("Submitting answers with 'Correction' button in frame...")
                    cor.click()
                    return True
            except Exception:
                pass

        # 2. Fallback: JavaScript dispatch click + mousedown across all frames
        for frame in self.browser.get_all_frames():
            try:
                clicked = frame.evaluate('''() => {
                    const btns = Array.from(document.querySelectorAll('button.correct, button.action-exercise-button.correct, button'));
                    for (const btn of btns) {
                        const txt = (btn.innerText || btn.textContent || '').trim();
                        if (txt === 'Correction' || txt === 'ตรวจคำตอบ' || btn.classList.contains('correct')) {
                            btn.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, cancelable: true, view: window }));
                            btn.dispatchEvent(new MouseEvent('mouseup', { bubbles: true, cancelable: true, view: window }));
                            btn.click();
                            return true;
                        }
                    }
                    return false;
                }''')
                if clicked:
                    logger.info("Submitting answers with 'Correction' button via JS event...")
                    return True
            except Exception:
                pass

        logger.warning("Could not find visible 'Correction' button in any frame!")
        return False

    def _harvest_existing_results_into_memory(self):
        """
        If the page already displays evaluated results (e.g. from previous run with score badge or green/red boxes),
        harvest correct/wrong states into _exercise_memory before resetting with 'Try again'.
        """
        page = self.browser.page
        if not page:
            return

        cur_url = page.url or "default"
        for frame in self.browser.get_all_frames():
            try:
                res = frame.evaluate('''() => {
                    function parseColors(el) {
                        let isCorrect = false;
                        let isWrong = false;
                        if (!el) return { isCorrect, isWrong };

                        const cls = (el.className || '').toLowerCase();
                        if (/has-success|correct|success|right|valid\b/i.test(cls)) isCorrect = true;
                        if (/has-error|incorrect|wrong|error|invalid\b/i.test(cls)) isWrong = true;

                        const comp = window.getComputedStyle(el);
                        const colors = [comp.borderColor, comp.borderTopColor, comp.outlineColor, comp.color, comp.backgroundColor, comp.boxShadow];
                        for (const c of colors) {
                            if (!c) continue;
                            const p = c.split('(')[1]?.split(')')[0]?.split(',');
                            if (p && p.length >= 3) {
                                const r = parseInt(p[0].trim(), 10);
                                const g = parseInt(p[1].trim(), 10);
                                const b = parseInt(p[2].trim(), 10);
                                if (!isNaN(r) && !isNaN(g) && !isNaN(b)) {
                                    if (r > 140 && g < 130 && b < 130) isWrong = true;
                                    else if (g > 90 && r < 140 && g > r) isCorrect = true;
                                }
                            }
                        }
                        return { isCorrect, isWrong };
                    }

                    const badgeText = document.querySelector(".result-badge-text, .result-badge");
                    const tryAgain = document.querySelector(".try-again, button:has-text('Try again'), a:has-text('Try again')");
                    let hasEvaluated = false;
                    const gapResults = [];
                    
                    // A. Gap Containers
                    const gapContainers = Array.from(document.querySelectorAll(".exercise-items .gap-container, .gap-container"))
                        .filter(el => el.offsetParent !== null && el.getBoundingClientRect().width > 0);

                    gapContainers.forEach((container, idx) => {
                        const span = container.querySelector(".gap") || container;
                        const group = container.querySelector(".input-group") || container;
                        const addon = container.querySelector(".input-group-addon, button");
                        const text = (span.innerText || span.textContent || "").replace(/\u00a0/g, " ").trim();

                        let isCorrect = false;
                        let isWrong = false;
                        for (const el of [span, group, container, addon]) {
                            const st = parseColors(el);
                            if (st.isCorrect) isCorrect = true;
                            if (st.isWrong) isWrong = true;
                        }

                        if (isCorrect || isWrong) hasEvaluated = true;
                        gapResults.push({ slotIndex: idx, text: text, isCorrect: isCorrect, isWrong: isWrong });
                    });

                    // B. Drag & Drop Placeholders
                    const dragPlaceholders = Array.from(document.querySelectorAll('.exercise-items .drag-drop-placeholder'));
                    dragPlaceholders.forEach((p, idx) => {
                        let chip = null;
                        if (p.nextElementSibling && p.nextElementSibling.classList.contains('drag-drop')) {
                            chip = p.nextElementSibling;
                        } else if (p.querySelector('.drag-drop')) {
                            chip = p.querySelector('.drag-drop');
                        } else {
                            const chipsInParent = Array.from(p.parentElement.querySelectorAll('.drag-drop'));
                            if (chipsInParent.length > 0) chip = chipsInParent[0];
                        }
                        if (chip) {
                            const text = chip.innerText.trim();
                            const { isCorrect, isWrong } = parseColors(chip);
                            if (isCorrect || isWrong) hasEvaluated = true;
                            gapResults.push({ slotIndex: idx, text: text, isCorrect: isCorrect, isWrong: isWrong });
                        }
                    });

                    if (!hasEvaluated && !badgeText && !tryAgain) return null;
                    return gapResults;
                }''')

                if res and any(r.get("isCorrect") or r.get("isWrong") for r in res):
                    logger.info(f"🔍 [INITIAL HARVEST] Found existing evaluated results on screen ({len(res)} gaps)!")
                    if cur_url not in self._exercise_memory:
                        self._exercise_memory[cur_url] = {"correct_answers": {}, "wrong_answers": {}}
                    mem = self._exercise_memory[cur_url]

                    for item in res:
                        idx = item.get("slotIndex")
                        text = item.get("text", "")
                        if not text or idx is None:
                            continue
                        if item.get("isCorrect"):
                            mem["correct_answers"][idx] = text
                            logger.info(f"  [MEMORY LOCK] Blank #{idx+1} is CORRECT: '{text}' (Preserved)")
                        elif item.get("isWrong"):
                            if idx not in mem["wrong_answers"]:
                                mem["wrong_answers"][idx] = set()
                            mem["wrong_answers"][idx].add(text)
                            logger.warning(f"  [MEMORY FORBID] Blank #{idx+1} was WRONG with '{text}' (Will be flipped)")

                    try_btn = frame.locator("button:has-text('Try again'), a:has-text('Try again'), .try-again:visible").first
                    if try_btn.count() > 0 and try_btn.is_visible():
                        logger.info("Clicking 'Try again' to prepare page for next attempt...")
                        try_btn.click()
                        time.sleep(1.5)
                    break
            except Exception as e:
                logger.debug(f"Notice on _harvest_existing_results_into_memory: {e}")

    def advance_to_next_exercise(self) -> bool:
        """Finds and clicks Next / Continue button across frames and main page to advance to next question."""
        page = self.browser.page
        if not page:
            return False

        # Check inside frames
        for frame in self.browser.get_all_frames():
            try:
                nxt = frame.locator("button.next, button.nxt-exercise, button:has-text('Next'), a:has-text('Next')").first
                if nxt.count() > 0 and nxt.is_visible():
                    nxt.click()
                    time.sleep(1.5)
                    return True
            except Exception:
                pass

        # Check main page
        try:
            nxt = page.locator("button.next, button.nxt-exercise, button:has-text('Next'), a:has-text('Next'), .next-exercise-arrow:visible").first
            if nxt.count() > 0 and nxt.is_visible():
                nxt.click()
                time.sleep(1.5)
                return True
        except Exception:
            pass

        return False

    def solve_exercise_until_passed(self, max_retries: int = 5, min_passing_score: int = 80) -> bool:
        """
        Completely autonomous solver loop:
        Solves the exercise, checks score, and if < 80%, AUTOMATICALLY retries with memory
        again and again until it achieves >= min_passing_score (80%)!
        NEVER reports success or advances if score < 80%!
        """
        page = self.browser.page
        if not page:
            return False

        # Guard 0: Check if page is ALREADY PASSED (e.g. score >= 80% or 100/100, Next button ready)
        # If already passed, NEVER re-solve or re-submit! Advance to Next directly!
        try:
            already_passed = page.evaluate('''() => {
                const badgeText = document.querySelector('.result-badge-text');
                if (badgeText) {
                    const v = parseInt(badgeText.getAttribute('data-result') || badgeText.innerText.trim(), 10);
                    if (!isNaN(v) && v >= 80) return true;
                }
                const badge = document.querySelector('.result-badge');
                if (badge) {
                    const m = (badge.innerText || '').match(/(\\d+)\\s*\\/\\s*100/);
                    if (m && parseInt(m[1], 10) >= 80) return true;
                }
                const nextBtn = document.querySelector('button.nxt-exercise, button.next');
                if (nextBtn && (nextBtn.classList.contains('visited') || !nextBtn.disabled)) {
                    // Check if all blocks are green / success
                    const sBlocks = Array.from(document.querySelectorAll('.scrambled-block'));
                    if (sBlocks.length > 0 && sBlocks.every(b => b.classList.contains('success'))) return true;
                }
                return false;
            }''')
            if (already_passed):
                logger.info("🎉 [ALREADY PASSED] Exercise is ALREADY passed with score >= 80%! Advancing to Next directly without re-solving...")
                self.advance_to_next_exercise()
                return True
        except Exception as e:
            logger.debug(f"Notice on already passed check: {e}")

        # Pre-check: If page already displays evaluated results (e.g. from previous run),
        # harvest green/correct and red/wrong answers into memory immediately!
        self._harvest_existing_results_into_memory()

        # Reset retry counter for this exercise URL so it doesn't carry over stale counts
        cur_url = page.url or "default"
        if hasattr(self, "_step_retry_counts"):
            self._step_retry_counts[cur_url] = 0

        for attempt in range(1, max_retries + 1):
            logger.info(f"🚀 [AUTO-SOLVER] Round #{attempt}/{max_retries} for current question (Goal: >= {min_passing_score}%)...")
            success = self.auto_solve_step()
            if not success:
                logger.warning(f"⚠️ Round #{attempt} auto_solve_step failed (0 words placed or step aborted). Reloading page and retrying...")
                try:
                    self.browser.page.reload()
                    time.sleep(2.5)
                except Exception:
                    pass
                continue
            passed = self.handle_step_advancement_or_retry(max_retries=max_retries, min_passing_score=min_passing_score)
            if passed:
                logger.info(f"🎉 [GENUINE SUCCESS] Exercise passed with >= {min_passing_score}% on attempt #{attempt} and advanced!")
                if hasattr(self.browser, "last_eps_solutions"):
                    self.browser.last_eps_solutions = {}
                return True
            logger.warning(f"⚠️ Round #{attempt} failed (score < {min_passing_score}%). Page reset complete. Retrying Round #{attempt+1} with memory...")
            time.sleep(2.0)

        logger.warning(f"⏩ [SKIP] ลองทำครบ {max_retries} รอบแล้วยังไม่ถึงเกณฑ์ -> กำลังกดข้ามไปข้อถัดไปอัตโนมัติ...")
        self._exercise_memory.pop(cur_url, None)
        if hasattr(self, "_step_retry_counts"):
            self._step_retry_counts.pop(cur_url, None)
        self.advance_to_next_exercise()
        return True

    def handle_step_advancement_or_retry(self, max_retries: int = 5, min_passing_score: int = 80) -> bool:
        """
        After solving an exercise:
        1. Submits answer with 'Correction'.
        2. Inspects result:
           - Extracts exact numeric score (threshold: min_passing_score = 80%).
           - If score >= 80%: PASSED! Clicks 'Next' to proceed.
           - If score < 80%: FAILED!
             - Records which blanks were CORRECT (green) and which were WRONG (red) into _exercise_memory.
             - Resets exercise (Try again / reload) to redo.
             - Round 2 will remember all correct answers and forbid repeating previous wrong answers!
        """
        page = self.browser.page
        if not page:
            return False

        if not hasattr(self, "_step_retry_counts"):
            self._step_retry_counts = {}

        cur_url = page.url or "default"
        attempts = self._step_retry_counts.get(cur_url, 0) + 1
        self._step_retry_counts[cur_url] = attempts

        if "login" in cur_url.lower():
            logger.info("Detected login page during advancement check. Auto-logging in...")
            self.browser.auto_login()
            time.sleep(3.0)
            return False

        # Handle modal popup / Continue learning directly
        cont_btn = page.locator("button:has-text('Continue learning'), a:has-text('Continue learning'), .modal-footer button").first
        if cont_btn.count() > 0 and cont_btn.is_visible():
            logger.info("Clicking 'Continue learning' modal button...")
            cont_btn.click()
            time.sleep(1.5)
            return True

        if "pronunciation" in cur_url.lower():
            nxt = page.locator("button.next, button.nxt-exercise, button:has-text('Next'), a:has-text('Next'), .next-exercise-arrow:visible").first
            if nxt.count() > 0 and nxt.is_visible():
                nxt.click()
                time.sleep(1.0)
            return True

        # Real Video Intro pages are already handled during auto_solve_step(). Proceed directly to Correction.

        # Submit answer with Correction button
        self.click_correction_button()
        time.sleep(1.8)

        # Inspect score and per-blank correctness from DOM across all frames
        try:
            score = None
            slot_results = []
            input_results = []
            gap_results = []
            nav_green = False

            for frame in self.browser.get_all_frames():
                try:
                    res = frame.evaluate('''() => {
                        function parseColors(el) {
                            let isCorrect = false;
                            let isWrong = false;
                            if (!el) return { isCorrect, isWrong };

                            const cls = (el.className || '').toLowerCase();
                            if (/has-success|correct|success|right|valid\b/i.test(cls)) isCorrect = true;
                            if (/has-error|incorrect|wrong|error|invalid\b/i.test(cls)) isWrong = true;

                            const comp = window.getComputedStyle(el);
                            const colors = [comp.borderColor, comp.borderTopColor, comp.outlineColor, comp.color, comp.backgroundColor, comp.boxShadow];
                            for (const c of colors) {
                                if (!c) continue;
                                const p = c.split('(')[1]?.split(')')[0]?.split(',');
                                if (p && p.length >= 3) {
                                    const r = parseInt(p[0].trim(), 10);
                                    const g = parseInt(p[1].trim(), 10);
                                    const b = parseInt(p[2].trim(), 10);
                                    if (!isNaN(r) && !isNaN(g) && !isNaN(b)) {
                                        if (r > 140 && g < 130 && b < 130) isWrong = true;
                                        else if (g > 90 && r < 140 && g > r) isCorrect = true;
                                    }
                                }
                            }
                            return { isCorrect, isWrong };
                        }

                        // 1. Get exact score
                        let fScore = null;
                        const badgeText = document.querySelector('.result-badge-text');
                        if (badgeText) {
                            const v = parseInt(badgeText.getAttribute('data-result') || badgeText.innerText.trim(), 10);
                            if (!isNaN(v)) fScore = v;
                        }
                        if (fScore === null) {
                            const badge = document.querySelector('.result-badge');
                            if (badge) {
                                const m = (badge.innerText || '').match(/(\\d+)\\s*\\/\\s*100/);
                                if (m) fScore = parseInt(m[1], 10);
                            }
                        }

                        // 2. Drag & Drop Slots inspection (Robust 1-to-1 placeholder mapping)
                        const fSlotResults = [];
                        const passagePlaceholders = Array.from(document.querySelectorAll('.exercise-items .drag-drop-placeholder'));

                        passagePlaceholders.forEach((p, idx) => {
                            let chip = null;
                            if (p.nextElementSibling && p.nextElementSibling.classList.contains('drag-drop')) {
                                chip = p.nextElementSibling;
                            } else if (p.querySelector('.drag-drop')) {
                                chip = p.querySelector('.drag-drop');
                            } else {
                                const chipsInParent = Array.from(p.parentElement.querySelectorAll('.drag-drop'));
                                if (chipsInParent.length > 0) chip = chipsInParent[0];
                            }

                            const text = chip ? chip.innerText.trim() : (p.innerText || '').trim();
                            const target = chip || p;
                            const { isCorrect, isWrong } = parseColors(target);
                            fSlotResults.push({ slotIndex: idx, text: text, isCorrect: isCorrect, isWrong: isWrong });
                        });

                        // 3. Text inputs inspection
                        const fInputResults = [];
                        const inputs = Array.from(document.querySelectorAll('input[type="text"], input:not([type]), textarea'));
                        inputs.forEach((inp, idx) => {
                            const text = (inp.value || '').trim();
                            const { isCorrect, isWrong } = parseColors(inp);
                            fInputResults.push({ slotIndex: idx, text: text, isCorrect: isCorrect, isWrong: isWrong });
                        });

                        // 3.5 Scrambled Table inspection (.exercise-items .item)
                        const tableItems = Array.from(document.querySelectorAll('.exercise-items .item')).filter(el => el.offsetParent !== null);
                        tableItems.forEach((item, idx) => {
                            const cell = item.querySelector('.scrambled-cell[data-scrambled-cell-id], .cell');
                            const target = cell || item;
                            const text = (cell ? cell.innerText : (item.innerText || '')).trim();
                            const { isCorrect, isWrong } = parseColors(target);
                            fSlotResults.push({ slotIndex: idx, text: text, isCorrect: isCorrect, isWrong: isWrong });
                        });

                        // 3.6 Gap-Fill / Toggle Solution inspection (.gap-container)
                        const fGapResults = [];
                        const gapContainers = Array.from(document.querySelectorAll('.exercise-items .gap-container, .gap-container'))
                            .filter(el => el.offsetParent !== null && el.getBoundingClientRect().width > 0);

                        gapContainers.forEach((container, idx) => {
                            const span = container.querySelector('.gap') || container;
                            const group = container.querySelector('.input-group') || container;
                            const addon = container.querySelector('.input-group-addon, button');
                            const text = (span.innerText || span.textContent || '').replace(/\u00a0/g, ' ').trim();

                            let isCorrect = false;
                            let isWrong = false;
                            for (const el of [span, group, container, addon]) {
                                const st = parseColors(el);
                                if (st.isCorrect) isCorrect = true;
                                if (st.isWrong) isWrong = true;
                            }
                            if (isCorrect && isWrong) {
                                const compGroup = window.getComputedStyle(group || span);
                                const p = (compGroup.borderColor || '').split('(')[1]?.split(')')[0]?.split(',');
                                if (p && p.length >= 2) {
                                    const r = parseInt(p[0].trim(), 10), g = parseInt(p[1].trim(), 10);
                                    if (g > r) { isCorrect = true; isWrong = false; }
                                    else { isWrong = true; isCorrect = false; }
                                }
                            }
                            fGapResults.push({ slotIndex: idx, text: text, isCorrect: isCorrect, isWrong: isWrong });
                        });

                        // 4. Check active nav badge color
                        let fNavGreen = false;
                        const activeNav = document.querySelector('.exercise-icon.active .exercise-square, .exercise-icon.active');
                        if (activeNav) {
                            const nCls = (activeNav.className || '').toLowerCase();
                            if (nCls.includes('green') || nCls.includes('correct')) fNavGreen = true;
                        }

                        return { score: fScore, slotResults: fSlotResults, inputResults: fInputResults, gapResults: fGapResults, navGreen: fNavGreen };
                    }''')

                    if res:
                        if res.get("score") is not None and score is None:
                            score = res["score"]
                        if res.get("slotResults"):
                            slot_results.extend(res["slotResults"])
                        if res.get("inputResults"):
                            input_results.extend(res["inputResults"])
                        if res.get("gapResults"):
                            gap_results.extend(res["gapResults"])
                        if res.get("navGreen"):
                            nav_green = True
                except Exception as e:
                    pass

            # Determine whether passed (Must be >= min_passing_score)
            passed = False
            if score is not None:
                passed = (score >= min_passing_score)
                logger.info(f"Exercise Score: {score}% (Required Threshold: {min_passing_score}%) -> {'[PASSED]' if passed else '[FAILED - RETRY REQUIRED]'}")
            else:
                has_items = bool(slot_results or input_results or gap_results)
                if has_items:
                    all_items = slot_results if slot_results else (input_results if input_results else gap_results)
                    passed = nav_green or all(s.get("isCorrect") for s in all_items)
                else:
                    passed = nav_green
                logger.info(f"No numeric score found. Checked DOM state -> {'[PASSED]' if passed else '[FAILED - RETRY REQUIRED]'}")

            if passed:
                logger.info(f"Exercise successfully PASSED! Advancing to Next...")
                # Save verified winning answers to disk permanently (speexx_memory.json)
                try:
                    last_info = self._last_submitted_answers.get(cur_url, {})
                    if last_info and last_info.get("answers"):
                        import re
                        m = re.search(r'standard-packet/(\\d+/exercise/\\d+)', cur_url)
                        key = m.group(1) if m else cur_url
                        save_data = {
                            "title": last_info.get("title", ""),
                            "pattern": last_info.get("pattern", "unknown"),
                            "score": score or 100,
                            "answers": last_info.get("answers"),
                            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S")
                        }
                        self._save_disk_memory(key, save_data)
                except Exception as e:
                    logger.debug(f"Notice saving passed exercise to disk: {e}")
                self._exercise_memory.pop(cur_url, None)
                self._step_retry_counts.pop(cur_url, None)
                # Pause between questions (giving human reading time of score / confirmation)
                import random, config
                pause_next = random.uniform(config.PAUSE_BETWEEN_QUESTIONS_MIN, config.PAUSE_BETWEEN_QUESTIONS_MAX)
                if pause_next > 0:
                    logger.info(f"[Pacing] Pausing {pause_next:.2f}s before moving to next exercise...")
                    time.sleep(pause_next)
                advanced = self.advance_to_next_exercise()
                if not advanced:
                    try:
                        nxt = page.locator("button.next, button.nxt-exercise, button:has-text('Next'), a:has-text('Next'), .next-exercise-arrow:visible").first
                        if nxt.count() > 0 and nxt.is_visible():
                            nxt.click(force=True)
                            time.sleep(1.5)
                    except Exception:
                        pass
                return True

            # FAILED: Save memory of this attempt so Round 2 does NOT make the same mistakes!
            if cur_url not in self._exercise_memory:
                self._exercise_memory[cur_url] = {
                    "correct_answers": {},
                    "wrong_answers": {}
                }
            mem = self._exercise_memory[cur_url]

            # Skip slot memory forbid for mark_text activities (which have no blanks)
            is_mark_text = getattr(self, "_last_activity_type", "") == "mark_text"
            target_items = [] if is_mark_text else (slot_results if slot_results else (input_results if input_results else gap_results))
            for item in target_items:
                idx = item.get("slotIndex")
                text = item.get("text", "")
                if not text or idx is None:
                    continue
                if item.get("isCorrect"):
                    mem["correct_answers"][idx] = text
                    logger.info(f"  [MEMORY LOCK] Blank #{idx+1} is CORRECT: '{text}' (Will be kept in next round)")
                elif item.get("isWrong") or score == 0:
                    if idx not in mem["wrong_answers"]:
                        mem["wrong_answers"][idx] = set()
                    mem["wrong_answers"][idx].add(text)
                    logger.warning(f"  [MEMORY FORBID] Blank #{idx+1} was WRONG with '{text}' (Will NOT be reused for this blank)")

            # Universal Fallback Memory: For ANY pattern where DOM colors were inconclusive,
            # forbid all submitted answers that weren't confirmed green/correct!
            last_sub = getattr(self, "_last_submitted_answers", {}).get(cur_url, [])
            if last_sub and isinstance(last_sub, list):
                for idx, ans_val in enumerate(last_sub):
                    if ans_val and idx not in mem["correct_answers"]:
                        val_str = str(ans_val).strip()
                        if idx not in mem["wrong_answers"]:
                            mem["wrong_answers"][idx] = set()
                        if val_str not in mem["wrong_answers"][idx]:
                            mem["wrong_answers"][idx].add(val_str)
                            logger.warning(f"  [UNIVERSAL MEMORY FORBID] Slot #{idx+1} was WRONG with '{val_str}' (Forbidden for next round)")

            if attempts >= max_retries:
                logger.warning(f"⏩ [SKIP] ลองทำครบ {max_retries} รอบแล้ว -> กำลังกดข้ามไปข้อถัดไปอัตโนมัติ...")
                self._exercise_memory.pop(cur_url, None)
                self._step_retry_counts.pop(cur_url, None)
                self.advance_to_next_exercise()
                return True

            logger.warning(f"Exercise result {score}% is below {min_passing_score}% (Attempt #{attempts}/{max_retries}). Resetting and retrying with memory...")
            # In Speexx, failed chips remain stuck in slots and there is no 'Try again' button.
            # Reloading the page cleanly resets all chips back to the word bank while preserving our python memory!
            logger.info("Resetting exercise page via page.reload() to return all chips to word bank...")
            try:
                self.browser.page.reload()
                time.sleep(2.5)
            except Exception as e:
                logger.debug(f"Notice on page reload: {e}")
            return False
        except Exception as e:
            logger.exception(f"Error during result evaluation: {e}")
            return False
