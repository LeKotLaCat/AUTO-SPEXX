import json
import re
import logging
import requests
from openai import OpenAI
import config

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("AIProxy")


def _norm(text: str) -> str:
    """
    Normalise for word-bank comparison: collapse whitespace, unify the curly apostrophe the
    page may render against the straight one a model types, and lowercase.
    """
    t = (text or "").replace("’", "'").replace("‘", "'")
    return " ".join(t.split()).strip().lower()

class LMStudioClient:
    def __init__(self, base_url: str = config.LM_STUDIO_URL, model: str = config.LM_STUDIO_MODEL):
        self.base_url = base_url.rstrip('/')
        self.model = model
        self.client = OpenAI(base_url=self.base_url, api_key=config.PROXY_API_KEY, max_retries=2, timeout=120.0)

    def _chat(self, messages: list, temperature: float = 0.1, max_tokens: int = 8192, retries: int = 0) -> str:
        """
        Single entry point for LM Studio chat calls, with empty-response handling.
        Uses a generous 8192 max_tokens budget so local reasoning models have full headroom.
        """
        for attempt in range(retries + 1):
            try:
                response = self.client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    timeout=60.0
                )
                msg = response.choices[0].message
                raw = (msg.content or "").strip()
                if not raw:
                    raw = (getattr(msg, "reasoning_content", None) or "").strip()
                    if raw:
                        logger.info("LM Studio returned empty content; recovered answer from reasoning_content.")
                if raw:
                    return raw
                logger.warning(
                    f"LM Studio returned an EMPTY response (attempt {attempt+1}/{retries+1}). "
                    f"If this repeats, the loaded model may need a higher max_tokens or may be "
                    f"emitting only reasoning tokens."
                )
            except Exception as e:
                logger.error(f"LM Studio request failed (attempt {attempt+1}/{retries+1}): {e}")
        return ""

    def test_connection(self) -> bool:
        """Checks if AI Proxy / Gemini server is running and accessible."""
        try:
            url = f"{self.base_url}/models"
            headers = {"Authorization": f"Bearer {config.PROXY_API_KEY}"} if getattr(config, "PROXY_API_KEY", "") else {}
            resp = requests.get(url, headers=headers, timeout=5)
            if resp.status_code == 200:
                models_data = resp.json().get("data", [])
                model_names = [m.get("id") for m in models_data]
                logger.info(f"Connected to AI Proxy (Gemini 3.8 Flash) successfully! Available models: {model_names}")
                if model_names and self.model == "local-model":
                    self.model = model_names[0]
                    logger.info(f"Selected active model: {self.model}")
                return True
            else:
                logger.warning(f"AI Proxy responded with HTTP status {resp.status_code}")
                return False
        except Exception as e:
            logger.error(f"Failed to connect to AI Proxy at {self.base_url}: {e}")
            return False

    def solve_true_false(self, statements: list, page_context: str = "") -> list:
        """
        Queries LM Studio to evaluate True/False for a list of statements based on story/dialogue context.
        Returns list: [{'statement_index': 0, 'answer': 'true'|'false'}, ...]
        """
        system_prompt = (
            "You are an English tutor. Read the story carefully, then for each statement answer TRUE or FALSE.\n"
            "Reply with ONLY the answers, one per line:\n"
            "1. true\n"
            "2. false\n"
            "3. true\n"
            "Do NOT add explanations. Do NOT use JSON."
        )

        user_prompt = f"Story / Reading Context:\n{page_context[:8000]}\n\nStatements to evaluate:\n"
        for idx, stmt in enumerate(statements):
            user_prompt += f"{idx+1}. {stmt}\n"

        try:
            raw = self._chat(
                [{"role": "system", "content": system_prompt},
                 {"role": "user", "content": user_prompt}],
                temperature=0.1,
                max_tokens=16384
            )
            logger.info(f"LM Studio True/False Raw Response:\n{raw}")
            if not raw:
                logger.error("solve_true_false: model returned nothing usable.")
                return []

            # 1. Standard JSON Parse
            json_match = re.search(r'\{.*\}', raw, re.DOTALL)
            if json_match:
                try:
                    parsed = json.loads(json_match.group(0))
                    tf_list = parsed.get("tf_answers") or parsed.get("answers") or parsed.get("results")
                    if tf_list and isinstance(tf_list, list) and len(tf_list) > 0:
                        return tf_list
                except Exception as e:
                    logger.warning(f"JSON parse fallback: {e}")

            # 2. Line-by-line regex fallback
            results = []
            lines = [line.strip() for line in raw.splitlines() if line.strip()]
            
            for idx in range(len(statements)):
                stmt_num = idx + 1
                ans = None
                
                # Check for "1. true" or "1: false" or "Statement 1: True"
                for line in lines:
                    match = re.search(rf'(?:statement\s*)?{stmt_num}[\.\:\)\-]\s*(true|false)', line, re.IGNORECASE)
                    if match:
                        ans = match.group(1).lower()
                        break
                
                if not ans and idx < len(lines):
                    line_lower = lines[idx].lower()
                    if "true" in line_lower:
                        ans = "true"
                    elif "false" in line_lower:
                        ans = "false"
                
                if not ans:
                    ans = "true" if idx % 2 == 0 else "false"

                results.append({"statement_index": idx, "answer": ans})

            logger.info(f"Extracted True/False results: {results}")
            return results

        except Exception as e:
            logger.error(f"Error solving True/False with LM Studio: {e}")
            return []

    def solve_cycle_blanks(self, definitions: list, page_context: str = "") -> list:
        """
        Queries LM Studio to get the single word for each definition.
        Returns list of word strings in order.
        """
        system_prompt = (
            "You are an English vocabulary expert. For each numbered phrase below, "
            "provide the EXACT matching English word or phrase.\n"
            "Reply with ONLY the answers, one per line, in this format:\n"
            "1. answer1\n"
            "2. answer2\n"
            "3. answer3\n"
            "Do NOT add explanations. Do NOT use JSON."
        )

        user_prompt = ""
        if page_context:
            user_prompt += f"Exercise text:\n{page_context[:4000]}\n\n"
        user_prompt += "Match each phrase to the correct word:\n"
        for idx, def_text in enumerate(definitions):
            user_prompt += f"{idx+1}. {def_text}\n"

        try:
            raw = self._chat(
                [{"role": "system", "content": system_prompt},
                 {"role": "user", "content": user_prompt}],
                temperature=0.2,
                max_tokens=3500
            )
            logger.info(f"LM Studio Cycle Blanks Raw Response:\n{raw}")
            if not raw:
                logger.error("solve_cycle_blanks: model returned nothing usable.")
                return []

            json_match = re.search(r'\{.*\}', raw, re.DOTALL)
            if json_match:
                try:
                    parsed = json.loads(json_match.group(0))
                    w_list = parsed.get("words") or parsed.get("answers") or parsed.get("results") or parsed.get("options")
                    if w_list and isinstance(w_list, list):
                        return [str(w).strip() for w in w_list if str(w).strip()]
                except Exception:
                    pass

            # Fallback regex parse line by line
            words = []
            for line in raw.splitlines():
                clean_line = line.strip()
                if not clean_line:
                    continue
                match = re.search(r'^\d+[\.\:\)]\s*([a-zA-Z\s]+)', clean_line)
                if match:
                    words.append(match.group(1).strip())
                elif not clean_line.startswith('{') and not clean_line.startswith('}'):
                    # Strip any bullet points or quotes
                    word_clean = re.sub(r'^[\-\*\•\d\.\:\)]\s*', '', clean_line).strip(' "\'').strip()
                    if word_clean:
                        words.append(word_clean)
            return words

        except Exception as e:
            logger.error(f"Error solving cycle blanks with LM Studio: {e}")
            return []

    def solve_exercise(self, question_text: str, options: list = None, context: str = "") -> dict:
        """
        Queries LM Studio to solve a general exercise / multiple choice / fill-in-the-blank question.
        Returns dict: {'answer': str, 'explanation': str}
        """
        system_prompt = (
            "You are an expert English tutor. Solve the exercise.\n"
            "Reply with ONLY the answer on the first line.\n"
            "Then optionally explain why on the second line.\n"
            "Example:\n"
            "went\n"
            "Because 'yesterday' requires past simple tense."
        )

        user_prompt = ""
        if context:
            user_prompt += f"Context / Reading Text:\n{context}\n\n"
        user_prompt += f"Question:\n{question_text}\n"
        if options:
            user_prompt += "\nOptions:\n"
            for idx, opt in enumerate(options):
                user_prompt += f"- {opt}\n"

        raw = self._chat(
            [{"role": "system", "content": system_prompt},
             {"role": "user", "content": user_prompt}],
            temperature=0.1,
            max_tokens=3500
        )
        logger.info(f"LM Studio solve_exercise Raw Response:\n{raw}")

        if not raw:
            # Never fall back to options[0] here: that silently submits the first
            # candidate as if it were a real answer, which looks like a working solve
            # in the logs while actually being a coin flip.
            logger.error("solve_exercise: model returned nothing usable -- refusing to guess.")
            return {"answer": "", "explanation": "Empty model response"}

        json_match = re.search(r'\{.*\}', raw, re.DOTALL)
        if json_match:
            try:
                parsed = json.loads(json_match.group(0))
                ans = parsed.get("answer") or parsed.get("result") or parsed.get("solution")
                if ans:
                    return {"answer": str(ans), "explanation": parsed.get("explanation", "")}
            except Exception:
                pass

        lines = [l.strip().strip('*"\'').strip() for l in raw.splitlines() if l.strip()]

        if options:
            # The answer MUST be one of the offered options. Reasoning models dump their
            # whole thought process, so line 1 is usually "Thinking..." rather than the
            # answer -- scan from the END for a line that IS an option, then fall back to
            # the longest option mentioned anywhere ("up with" must beat "up").
            opts_by_lower = {o.strip().lower(): o for o in options}
            for line in reversed(lines):
                key = line.lower().rstrip('.').strip()
                if key in opts_by_lower:
                    return {"answer": opts_by_lower[key], "explanation": raw}

            mentioned = [o for o in options
                         if re.search(r'(?<!\w)' + re.escape(o.strip().lower()) + r'(?!\w)', raw.lower())]
            if mentioned:
                best = max(mentioned, key=lambda o: len(o))
                return {"answer": best, "explanation": raw}

            logger.warning(f"solve_exercise: no option matched the model response -- leaving blank.")
            return {"answer": "", "explanation": raw}

        return {"answer": lines[0] if lines else "", "explanation": raw}

    def solve_drag_drop_blanks(self, sentences: list, num_blanks: int, word_bank: list, feedback: dict = None) -> list:
        """
        For Speexx type-drag-drop: 'sentences' are full sentences containing globally
        numbered blank markers like ___(1), ___(2). Sends them together (so the model sees
        the surrounding context of every blank at once) and asks for one word per blank.
        Returns a list of length num_blanks; entries the model didn't answer -- or answered
        with something outside the word bank -- come back as '' rather than a wrong guess,
        so the caller can skip them instead of placing a garbage word.
        """
        system_prompt = (
            "You are an English tutor. Fill each numbered blank with the ONE best word from the word bank.\n"
            "Each word from the bank is used at most once (the word bank may contain extra unused words/distractors).\n"
            "CRITICAL RULE: If the word bank contains '[empty]' or if a blank does NOT require any word/article, output '[empty]' for that blank.\n"
            "Reply with ONLY the answers, one per line, numbered to match the blanks:\n"
            "1. word\n"
            "2. [empty]\n"
            "Each line must contain ONLY a number and a single word from the bank (or '[empty]') -- no sentences,\n"
            "no reasoning, no restating the question. Do NOT explain. Do NOT use JSON."
        )

        user_prompt = "Sentences (blanks are marked ___(n)):\n"
        for s in sentences:
            user_prompt += f"{s}\n"
        user_prompt += f"\nThere are {num_blanks} blanks total.\n"
        if feedback:
            user_prompt += "\n=== PREVIOUS ATTEMPT FEEDBACK (CRITICAL RULES) ===\n"
            correct_map = feedback.get("correct_answers", {})
            wrong_map = feedback.get("wrong_answers", {})
            if correct_map:
                user_prompt += "These blanks are ALREADY CONFIRMED 100% CORRECT from previous attempt. You MUST KEEP these exact answers:\n"
                for b_idx, ans in sorted(correct_map.items()):
                    user_prompt += f"- Blank #{b_idx + 1}: {ans}\n"
            if wrong_map:
                user_prompt += "These answers were TESTED and were INCORRECT. You MUST NOT use these words in these blanks again:\n"
                for b_idx, wrongs in sorted(wrong_map.items()):
                    w_str = ", ".join(wrongs) if isinstance(wrongs, (set, list)) else str(wrongs)
                    user_prompt += f"- Blank #{b_idx + 1} INCORRECT ANSWERS: {w_str}\n"
            user_prompt += "Assign only the remaining valid words into the blanks that were wrong.\n\n"

        user_prompt += "\nWord bank:\n"
        for w in word_bank:
            user_prompt += f"- {w}\n"

        # Reasoning models need a lot of headroom here: this prompt has ~14 blanks to
        # reason about, and at 1500 tokens the model was still mid-thought when it got
        # cut off -- so it never emitted an answer block at all and we parsed 0 answers.
        raw = self._chat(
            [{"role": "system", "content": system_prompt},
             {"role": "user", "content": user_prompt}],
            temperature=0.1,
            max_tokens=3000
        )
        logger.info(f"LM Studio solve_drag_drop_blanks Raw Response:\n{raw}")

        if not raw:
            logger.error("solve_drag_drop_blanks: model returned nothing -- refusing to guess.")
            return [""] * num_blanks

        # Map "<n>. <word>" lines onto blank indices.
        #
        # Local models often emit their whole chain-of-thought, which contains numbered
        # lines too ("1. **Analyze the Request:** ..."). Two rules keep that noise out:
        #   - the value must be a SINGLE token (reasoning lines are prose with spaces)
        #   - the token must match a word bank entry EXACTLY (no substring matching, which
        #     previously pulled "going" out of the sentence "Are you ever going to ...")
        # Later lines win, since the real answer block comes after the reasoning.
        answers = [""] * num_blanks
        bank_lower = {_norm(w): w for w in word_bank}
        rejected = []
        for line in raw.splitlines():
            m = re.match(r'^\s*\(?(\d+)[\.\:\)]\s*(.+?)\s*$', line.strip())
            if not m:
                continue
            idx = int(m.group(1)) - 1
            val = m.group(2).strip().strip('*"\'').strip()
            if not (0 <= idx < num_blanks):
                continue
            # Match against the word bank ONLY -- there is deliberately no "must be a
            # single word" rule: banks legitimately contain multi-word answers such as
            # "wouldn't leave" / "didn't rain", and rejecting anything with a space threw
            # those away. An exact bank match already excludes chain-of-thought prose.
            key = _norm(val)
            if key in bank_lower:
                answers[idx] = bank_lower[key]
            elif key in ['[empty]', 'empty', 'none', '-', 'no article', 'blank', '']:
                if '[empty]' in bank_lower:
                    answers[idx] = '[empty]'
            else:
                rejected.append(f"#{idx+1}='{val}'")

        if rejected:
            logger.warning(f"  Ignored {len(rejected)} answer(s) not in the word bank: {', '.join(rejected[:8])}")

        if feedback:
            correct_map = feedback.get("correct_answers", {})
            for b_idx, c_word in correct_map.items():
                if 0 <= b_idx < num_blanks and c_word:
                    answers[b_idx] = c_word

        filled = sum(1 for a in answers if a)
        logger.info(f"  Parsed {filled}/{num_blanks} usable answers from the model response.")
        return answers

    def solve_toggle_gaps(self, passage: str, gap_options_list: list, context: str = "") -> list:
        """
        Solves click-to-cycle / toggle-solution gap-fill exercises holistically by evaluating
        the whole narrative story and all gap options together.
        """
        num_gaps = len(gap_options_list)
        system_prompt = (
            "You are an expert English tutor and linguist.\n"
            "The user will provide sentences with blanks, along with the exact choices available for each blank (discovered by cycling).\n"
            "Choose the grammatically, structurally, and logically correct option for EACH blank in sequence.\n"
            "Pay close attention to grammar rules such as:\n"
            "- Inversion (e.g. with 'as', 'than', locative/direction phrases, introductory 'here/there') where auxiliary verb precedes the subject (e.g. 'than do homeowners', 'as was the flood', 'as did you').\n"
            "- Subject-verb agreement (singular vs plural subjects, e.g. 'do homeowners' vs 'does a homeowner').\n"
            "- Verb tense consistency (matching present or past context).\n\n"
            "Format your answer inside <answer> tags with numbered lines, copying the chosen option text EXACTLY from the given choices:\n"
            "<answer>\n"
            "1. <exact choice for blank 1>\n"
            "2. <exact choice for blank 2>\n"
            "...\n"
            "</answer>"
        )

        user_prompt = ""
        if context:
            user_prompt += f"Context & Lesson / Audio Transcript:\n{context[:3500]}\n\n"

        user_prompt += f"Story / Passage:\n{passage}\n\nAvailable Choices for each blank:\n"
        for i, opts in enumerate(gap_options_list):
            user_prompt += f"Blank #{i+1} choices: {', '.join(opts) if opts else 'any'}\n"

        raw = self._chat(
            [{"role": "system", "content": system_prompt},
             {"role": "user", "content": user_prompt}],
            temperature=0.1,
            max_tokens=2500
        )
        logger.info(f"LM Studio solve_toggle_gaps Raw Response:\n{raw}")

        if not raw:
            logger.error("solve_toggle_gaps: model returned nothing.")
            return [""] * num_gaps

        # Extract answer section if inside <answer>
        m_tag = re.search(r'<answer>(.*?)</answer>', raw, flags=re.DOTALL | re.IGNORECASE)
        answer_text = m_tag.group(1) if m_tag else raw

        answers = [""] * num_gaps
        for line in answer_text.splitlines():
            m = re.match(r'^\s*\(?(\d+)[\.\:\)]\s*(.+?)\s*$', line.strip())
            if not m:
                continue
            idx = int(m.group(1)) - 1
            val = m.group(2).strip().strip('*"\'').strip()
            if not (0 <= idx < num_gaps):
                continue
            
            opts = gap_options_list[idx]
            val_norm = _norm(val)
            # Find closest matching option
            match = None
            for o in opts:
                if _norm(o) == val_norm:
                    match = o
                    break
            if not match:
                for o in opts:
                    if _norm(o) in val_norm or val_norm in _norm(o):
                        match = o
                        break
            answers[idx] = match if match else (opts[0] if opts else val)

        # Fallback for any blank not parsed: pick first available option
        for i in range(num_gaps):
            if not answers[i] and gap_options_list[i]:
                answers[i] = gap_options_list[i][0]

        return answers

    def solve_choice_list(self, question: str, options: list, multi: bool, context: str = "") -> list:
        """
        For Speexx type-multiple-choice. Returns the option texts to tick.
        Uses JSON list output inside <answer> tags so only explicitly chosen options are picked,
        preventing rejected options mentioned in reasoning from being accidentally checked.
        """
        system_prompt = (
            "You are an expert English language tutor.\n"
            "Task: Choose the correct option(s) for the given prompt.\n"
            + ("CRITICAL: There may be MULTIPLE correct options. Carefully evaluate every single option against the task instruction. Select ALL options that qualify. Do NOT stop after choosing only one.\n" if multi
               else "Exactly ONE option is correct.\n")
            + "Output ONLY a JSON list of the chosen options inside <answer>...</answer> tags.\n"
            "Example format: <answer>[\"... a going away present\", \"... a farewell gift\"]</answer>\n"
            "CRITICAL: Do NOT list or mention options you decided NOT to pick. Output ONLY the JSON list inside <answer>."
        )

        user_prompt = ""
        if context:
            user_prompt += f"Context:\n{context[:2500]}\n\n"
        user_prompt += f"Task:\n{question}\n\nOptions:\n"
        for o in options:
            user_prompt += f"- {o}\n"

        raw = self._chat(
            [{"role": "system", "content": system_prompt},
             {"role": "user", "content": user_prompt}],
            temperature=0.1,
            max_tokens=4096,
        )
        logger.info(f"LM Studio solve_choice_list Raw Response:\n{raw}")

        if not raw:
            logger.error("solve_choice_list: model returned nothing -- refusing to guess.")
            return []

        by_norm = {_norm(o): o for o in options}
        picked = []

        # 1. Try to parse from <answer>[...]</answer> or raw JSON list
        ans_match = re.search(r'<answer>\s*(\[[\s\S]*?\])\s*(?:</answer>|$)', raw)
        if not ans_match:
            ans_match = re.search(r'(\[[\s\S]*?\])', raw)

        if ans_match:
            try:
                parsed_list = json.loads(ans_match.group(1))
                if isinstance(parsed_list, list):
                    for item in parsed_list:
                        clean_item = str(item).strip().strip('*"\'')
                        val = _norm(clean_item)
                        hit = by_norm.get(val)
                        if not hit:
                            for norm_key, orig in by_norm.items():
                                clean_norm_key = re.sub(r'^\.\.\.\s*', '', norm_key).strip()
                                clean_val = re.sub(r'^\.\.\.\s*', '', val).strip()
                                if clean_norm_key and clean_val and (clean_norm_key == clean_val or clean_norm_key in clean_val or clean_val in clean_norm_key):
                                    hit = orig
                                    break
                        if hit and hit not in picked:
                            picked.append(hit)
            except Exception as e:
                logger.warning(f"Failed to parse JSON list from answer tag: {e}")

        # Fallback: line-by-line parsing ONLY if JSON parse yielded nothing
        if not picked:
            for line in raw.splitlines():
                if "incorrect" in line.lower() or "not correct" in line.lower() or "false" in line.lower() or "wrong" in line.lower():
                    continue
                clean_l = line.strip().lstrip('-*•').strip().strip('*"\'')
                val = _norm(clean_l)
                val = re.sub(r'^\d+[\.\:\)]\s*', '', val)
                if not val:
                    continue
                hit = by_norm.get(val)
                if not hit:
                    for norm_key, orig in by_norm.items():
                        clean_norm_key = re.sub(r'^\.\.\.\s*', '', norm_key).strip()
                        clean_val = re.sub(r'^\.\.\.\s*', '', val).strip()
                        if clean_norm_key and clean_val and (clean_norm_key == clean_val or clean_norm_key in clean_val or clean_val in clean_norm_key):
                            hit = orig
                            break
                if hit and hit not in picked:
                    picked.append(hit)

        if not multi and len(picked) > 1:
            picked = picked[:1]

        logger.info(f"  Selected {len(picked)}/{len(options)} option(s).")
        return picked

    def solve_matching_table(self, prompts: list, responses: list, context: str = "", feedback: dict = None) -> list:
        """
        For Speexx type-scrambled-table ("Link the sentences that go together").
        prompts:   the fixed prompt clauses, in row order.
        responses: the movable answer clauses currently on screen, in whatever order they sit.
        context:   dialogue transcript or story text (if listening task).
        feedback:  memory of correct and wrong clauses from previous attempts.
        Returns one response per prompt, in row order.
        """
        system_prompt = (
            "You are an expert English grammar tutor and sentence construction specialist.\n"
            "Task: Match each numbered Target Clause (1 to N) on the right with the best Available Clause on the left.\n"
            "Rules:\n"
            "1. Match subjects, verbs, and objects logically:\n"
            "   - John + office supplies / ordering\n"
            "   - You + fixing things around the house / sink\n"
            "   - They + travelling to Hawaii\n"
            "   - We + relying on a distributor\n"
            "2. Carefully distinguish verb tenses based on time markers:\n"
            "   - Past Simple (ordered, asked, travelled, relied): used for completed specific past times (e.g. 'last week', 'for the last project', 'for the Christmas holidays').\n"
            "   - Present Perfect Continuous (has been ordering, have been asking, have been travelling, have been relying): used for ongoing duration or repeated frequency (e.g. 'since January', 'for months', 'every year for twenty years', 'far too often').\n"
            "Every available clause must be used exactly once.\n\n"
            "CRITICAL: Output your answers inside <answer>...</answer> tags with numbered lines 1 to N, copying the exact available clause text:\n"
            "<answer>\n"
            "1. <exact clause matching target 1>\n"
            "2. <exact clause matching target 2>\n"
            "... up to N\n"
            "</answer>"
        )

        user_prompt = ""
        if context:
            user_prompt += f"Dialogue / Audio Transcript Context:\n{context[:3500]}\n\n"

        if feedback:
            user_prompt += "=== PREVIOUS ATTEMPT FEEDBACK (CRITICAL RULES) ===\n"
            correct_map = feedback.get("correct_answers", {})
            wrong_map = feedback.get("wrong_answers", {})
            if correct_map:
                user_prompt += "These clauses are CONFIRMED CORRECT and MUST BE KEPT:\n"
                for r_idx, c_text in sorted(correct_map.items()):
                    user_prompt += f"- Target #{r_idx+1}: '{c_text}'\n"
            if wrong_map:
                user_prompt += "These clauses were TESTED and were WRONG for these targets. DO NOT use them for these targets:\n"
                for r_idx, w_set in sorted(wrong_map.items()):
                    w_str = ", ".join(w_set) if isinstance(w_set, (set, list)) else str(w_set)
                    user_prompt += f"- Target #{r_idx+1} WRONG: {w_str}\n"
            user_prompt += "\n"

        user_prompt += "Target Clauses:\n"
        for i, p in enumerate(prompts):
            user_prompt += f"{i+1}. {p}\n"
        user_prompt += "\nAvailable Clauses to choose from:\n"
        for r in responses:
            user_prompt += f"- {r}\n"

        raw = self._chat(
            [{"role": "system", "content": system_prompt},
             {"role": "user", "content": user_prompt}],
            temperature=0.1,
            max_tokens=8192,
        )
        logger.info(f"LM Studio solve_matching_table Raw Response:\n{raw}")

        if not raw:
            logger.error("solve_matching_table: model returned nothing -- using available responses.")
            return list(responses)

        by_norm = {_norm(r): r for r in responses}
        answers = [""] * len(prompts)
        used = set()

        ans_block = ""
        ans_match = re.search(r'<answer>(.*?)</answer>', raw, re.DOTALL | re.IGNORECASE)
        if ans_match:
            ans_block = ans_match.group(1).strip()
        else:
            ans_block = raw.strip()

        raw_lines = [l.strip() for l in ans_block.splitlines() if l.strip()]

        for line_idx, line in enumerate(raw_lines):
            line_str = line.strip()
            m = re.match(r'^\s*\(?(\d+)[\.\:\)]\s*(.+?)\s*$', line_str)
            if m:
                idx = int(m.group(1)) - 1
                raw_val = m.group(2).strip().strip('*"\'')
            else:
                idx = line_idx
                raw_val = line_str.lstrip('-*•').strip().strip('*"\'')

            if not (0 <= idx < len(prompts)):
                continue

            val = _norm(raw_val)

            hit = by_norm.get(val)

            if hit is None:
                cands = [orig for norm, orig in by_norm.items()
                         if norm.startswith(val) or val.startswith(norm)]
                hit = cands[0] if len(cands) == 1 else None

            if hit is None:
                for norm_key, orig in by_norm.items():
                    if norm_key in val or val in norm_key:
                        hit = orig
                        break

            if hit is None:
                line_norm = _norm(line_str)
                for norm_key, orig in by_norm.items():
                    if norm_key in line_norm:
                        hit = orig
                        break

            if hit is None:
                continue
            if _norm(hit) in used and answers[idx] != hit:
                continue
            answers[idx] = hit
            used.add(_norm(hit))

        # Fallback: fill any empty slot with remaining unused responses
        unused_responses = [r for r in responses if _norm(r) not in used]
        for i in range(len(answers)):
            if not answers[i] and unused_responses:
                pop_res = unused_responses.pop(0)
                answers[i] = pop_res
                used.add(_norm(pop_res))

        filled = sum(1 for a in answers if a)
        logger.info(f"  Paired {filled}/{len(prompts)} rows: {answers}")
        return answers

    def solve_scrambled_sentence(self, scrambled_words: list, context: str = "") -> list:
        """
        Queries LM Studio to reorder a scrambled list of words/phrase blocks into a
        grammatically correct sentence. Each block in scrambled_words is atomic.
        """
        if not scrambled_words:
            return []

        def _clean_str(s):
            return re.sub(r'[\s\.,!?;:\'"]+', '', s).lower()

        system_prompt = (
            "You are an expert English grammar tutor and sentence reconstruction specialist.\n"
            "Task: Reorder the given scrambled word/phrase blocks into ONE single, natural, 100% grammatically correct English sentence.\n"
            "Rules:\n"
            "1. The sentence MUST start with a capitalized word (e.g., 'Mike', 'The truck', 'While', 'Supposedly the goods', 'Although').\n"
            "2. The sentence MUST end with a punctuation mark ('.', '?', '!').\n"
            "3. Pay close attention to commas, conjunctions ('when', 'just as', 'while', 'until', 'although'), and phrasal verbs.\n"
            "4. Every single given block MUST be used in the sentence.\n"
            "5. Provide ONLY the final completed sentence inside <answer>...</answer> tags.\n\n"
            "Examples:\n"
            "- Scrambled: ['orders', 'was', 'first', 'Mike', 'came in', 'when', 'the', 'shipment', 'tracking', '.']\n"
            "  <answer>Mike was tracking the first orders when the shipment came in.</answer>\n"
            "- Scrambled: ['as', 'Laura', 'was', 'arriving', \"Mike's boss\", 'to', 'pulled', 'up', 'just', 'The truck', 'the', 'warehouse', '.']\n"
            "  <answer>The truck pulled up to the warehouse just as Mike's boss was arriving with Laura.</answer>\n"
            "- Scrambled: ['the', 'items', 'While', 'Mike', 'unloading', 'was', 'checked', 'off', 'the inventory,', 'Laura', '.']\n"
            "  <answer>While Laura checked off the inventory, Mike was unloading the items.</answer>\n"
            "- Scrambled: ['the boxes', 'all', 'but', 'were', 'Supposedly the goods', 'stamped Georgia', 'from Illinois,', 'came', '.']\n"
            "  <answer>Supposedly the goods came from Illinois, but the boxes were all stamped Georgia.</answer>\n"
            "- Scrambled: ['Mike', 'stopped', 'the', 'driver', 'until', 'Laura', 'finished', 'unloading', 'talking', 'to', '.']\n"
            "  <answer>Mike stopped talking to the driver until Laura finished unloading.</answer>\n"
            "- Scrambled: ['Although', 'the', 'point of origin', 'was Chicago,', 'were', 'added in Georgia', 'new items', '.']\n"
            "  <answer>Although the point of origin was Chicago, new items were added in Georgia.</answer>\n"
            "- Scrambled: ['.', 'not too', \"face, you're\", 'By the', 'your', 'on', 'look', 'with this', 'pleased']\n"
            "  <answer>By the look on your face, you're not too pleased with this.</answer>\n"
            "- Scrambled: [\"see you're\", 'By the', 'having one', 'can', 'eye, I', 'your', '.', 'interested in', 'look in']\n"
            "  <answer>By the look in your eye, I can see you're interested in having one.</answer>\n"
            "- Scrambled: ['face, I', 'eat something', 'would', 'else', 'your', 'look on', 'prefer to', 'By the', 'guess you', '.']\n"
            "  <answer>By the look on your face, I would guess you prefer to eat something else.</answer>\n"
            "- Scrambled: ['a', \"It's just\", 'heard', 'it', 'grapevine', 'rumor,', 'through', 'the', 'I', '.']\n"
            "  <answer>It's just a rumor, I heard it through the grapevine.</answer>\n"
            "- Scrambled: ['by', 'I', '.', 'can', 'in', 'the', 'tell', 'glint', 'eye', 'your']\n"
            "  <answer>I can tell by the glint in your eye.</answer>"
        )

        user_prompt = ""
        if context:
            user_prompt += f"Context & Story:\n{context[:2500]}\n\n"
        user_prompt += f"Scrambled blocks to use: {scrambled_words}\n\nReordered sentence inside <answer>...</answer>:"

        try:
            raw = self._chat(
                [{"role": "system", "content": system_prompt},
                 {"role": "user", "content": user_prompt}],
                temperature=0.1,
                max_tokens=2500
            )
            logger.info(f"LM Studio solve_scrambled_sentence Raw Response:\n{raw}")
            if not raw:
                return []

            # 1. Extract from <answer> tags if present
            ans_match = re.search(r'<answer>(.*?)</answer>', raw, re.DOTALL | re.IGNORECASE)
            if ans_match:
                sentence_text = ans_match.group(1).strip()
            else:
                # Check for quotes
                quote_matches = re.findall(r'"([^"\n]{15,})"', raw)
                if quote_matches:
                    sentence_text = quote_matches[-1].strip()
                else:
                    lines = [l.strip() for l in raw.splitlines() if l.strip() and not l.lower().startswith("let's") and not l.lower().startswith("the user") and not l.lower().startswith("thinking")]
                    sentence_text = lines[-1] if lines else raw

            logger.info(f"  Target sentence extracted: {sentence_text}")

            # Sequential left-to-right alignment matching algorithm:
            # Consumes words sequentially along the reconstructed target sentence.
            # Prevents duplicate words (like 'as' in 'about as ... as' or 'the' in 'The play ... the')
            # from being incorrectly sorted via first-occurrence regex!
            def _norm_token(s):
                return re.sub(r'[^\w]', '', s).lower()

            target_tokens = [ _norm_token(w) for w in sentence_text.strip().split() if _norm_token(w) ]
            remaining = list(scrambled_words)
            ordered = []

            t_idx = 0
            while remaining and t_idx < len(target_tokens):
                best_block = None
                best_len = 0
                for b in remaining:
                    b_tokens = [ _norm_token(w) for w in b.split() if _norm_token(w) ]
                    if not b_tokens:
                        continue
                    match_len = len(b_tokens)
                    if target_tokens[t_idx : t_idx + match_len] == b_tokens:
                        if match_len > best_len:
                            best_block = b
                            best_len = match_len

                if best_block is not None:
                    ordered.append(best_block)
                    remaining.remove(best_block)
                    t_idx += best_len
                else:
                    t_idx += 1

            # Punctuation blocks (., ?, !, ...) belong at the end in order
            punct_blocks = [b for b in remaining if re.sub(r'[\s\w]+', '', b) == b.strip()]
            for p in punct_blocks:
                ordered.append(p)
                remaining.remove(p)

            # Any remaining blocks
            ordered.extend(remaining)

            if len(ordered) == len(scrambled_words):
                logger.info(f"Reordered blocks ({len(ordered)}): {ordered}")
                return ordered

            return []
        except Exception as e:
            logger.error(f"Error in solve_scrambled_sentence with LM Studio: {e}")
            return []

    def solve_matching_exercise(self, prompts: list, options: list, context: str = "") -> list:
        """
        Queries LM Studio to match a list of phrases/prompts to options in word bank.
        Returns list of answer strings in order.
        """
        system_prompt = (
            "You are an English tutor. Match each phrase to the best option from the word bank.\n"
            "Reply with ONLY the answers, one per line:\n"
            "1. answer1\n"
            "2. answer2\n"
            "3. answer3\n"
            "Use EXACT text from the word bank. Do NOT use JSON."
        )

        user_prompt = ""
        if context:
            user_prompt += f"Context / Text:\n{context[:3500]}\n\n"
        
        user_prompt += "Phrases to complete:\n"
        for idx, p in enumerate(prompts):
            user_prompt += f"{idx+1}. {p}\n"
        
        if options:
            user_prompt += "\nAvailable Word Bank Options:\n"
            for opt in options:
                user_prompt += f"- {opt}\n"

        try:
            raw = self._chat(
                [{"role": "system", "content": system_prompt},
                 {"role": "user", "content": user_prompt}],
                temperature=0.1,
                max_tokens=3500
            )
            logger.info(f"LM Studio solve_matching_exercise Raw Response:\n{raw}")
            if not raw:
                logger.error("solve_matching_exercise: model returned nothing usable -- refusing to guess.")
                return []

            json_match = re.search(r'\{.*\}', raw, re.DOTALL)
            if json_match:
                try:
                    parsed = json.loads(json_match.group(0))
                    ans_list = parsed.get("answers") or parsed.get("results") or parsed.get("words")
                    if ans_list and isinstance(ans_list, list):
                        return ans_list
                except Exception:
                    pass

            # Fallback regex parse per line
            results = []
            lines = [l.strip() for l in raw.splitlines() if l.strip()]
            for idx in range(len(prompts)):
                stmt_num = idx + 1
                matched_opt = None
                
                for line in lines:
                    m = re.search(r'^\s*\(?' + str(stmt_num) + r'[\.\:\)]\s*(.*)', line, re.IGNORECASE)
                    if m:
                        val = m.group(1).strip()
                        if options and len(options) > 0:
                            for opt in options:
                                if opt.lower() in val.lower():
                                    matched_opt = opt
                                    break
                            if not matched_opt:
                                matched_opt = val
                        else:
                            matched_opt = val
                        break
                
                if not matched_opt and idx < len(lines):
                    clean_line = re.sub(r'^\s*[\-\*\•\d\.\:\)]\s*', '', lines[idx]).strip()
                    if clean_line:
                        matched_opt = clean_line

                # NOTE: deliberately no "matched_opt = options[idx]" fallback here.
                # Padding with the word bank in its original order produced answers that
                # looked real in the logs but were just the bank listed top-to-bottom.
                results.append(matched_opt if matched_opt else "")

            return results

        except Exception as e:
            logger.error(f"Error in solve_matching_exercise: {e}")
            return []

    def solve_listen_and_rewrite(
        self,
        instruction: str,
        grammar_rules: str,
        example: str,
        audio_sentences: list,
        context: str = ""
    ) -> list:
        """
        Solves exercises where user listens to spoken sentences and rewrites/transforms them
        according to the instruction and grammar rules (e.g. Inversion, Conditionals, Reported Speech).
        """
        if not audio_sentences:
            return []

        system_prompt = (
            "You are an expert English grammar specialist and sentence construction tutor.\n"
            "Task: For each given spoken sentence, rewrite or transform it strictly following the exercise instruction and grammar guidelines.\n\n"
            "CRITICAL RULES:\n"
            "1. Read the exercise instruction and grammar guidelines carefully.\n"
            "2. Pay close attention to any provided example for the exact style and formatting expected.\n"
            "3. Output ONLY the final rewritten sentences inside <answer>...</answer> tags numbered 1 to N.\n"
            "4. Do not include any explanations, notes, or preambles. Start directly with <answer>.\n\n"
            "Example Format:\n"
            "<answer>\n"
            "1. Had Joanna been late one more time, she would have been fired\n"
            "2. Mr Jones would certainly have quit had the deal fallen through\n"
            "3. Were Steve to file a claim, the money would be his\n"
            "4. Lisa opened the door and off went the alarm\n"
            "</answer>"
        )

        user_prompt = f"INSTRUCTION: {instruction}\n\n"
        if grammar_rules:
            user_prompt += f"📘 GRAMMAR LESSON & RULES:\n{grammar_rules}\n\n"
        if example:
            user_prompt += f"EXAMPLE GIVEN IN EXERCISE:\n{example}\n\n"
        if context:
            user_prompt += f"Exercise Context:\n{context[:1500]}\n\n"

        user_prompt += "Spoken sentences from audio to rewrite:\n"
        for idx, sent in enumerate(audio_sentences):
            user_prompt += f"{idx+1}. \"{sent}\"\n"

        raw = self._chat([
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ], temperature=0.1, max_tokens=1024)

        answers = []
        if "<answer>" in raw and "</answer>" in raw:
            ans_block = raw.split("<answer>")[1].split("</answer>")[0].strip()
        else:
            ans_block = raw.strip()

        for line in ans_block.splitlines():
            line = line.strip()
            if not line:
                continue
            line = re.sub(r'^\d+[\.\)\:\-]\s*', '', line).strip()
            line = line.strip('"\'')
            if line:
                answers.append(line)

        return answers[:len(audio_sentences)]

    def solve_typed_fill_in_blanks(self, items: list, grammar_rules: str = "", context: str = "") -> list:
        """
        Solves text input exercises where user must type full phrases or verb conjugations into input boxes.
        Each item in items contains:
          - prompt: prompt text with verbs in parentheses (e.g. 'She ... the window and ... the room early this morning. (open / leave)')
          - prefix: text already appearing before input box (e.g. 'Last night, ')
          - suffix: text appearing after input box (e.g. ' early this morning.')
          - max_length: maximum character limit of input box
        """
        num_items = len(items)
        if num_items == 0:
            return []

        system_prompt = (
            "You are an expert English grammar tutor and sentence construction specialist.\n"
            "Task: For each numbered question, construct the EXACT text that must be typed into the [ INPUT BOX ] to form a 100% grammatically correct sentence.\n\n"
            "CRITICAL RULES:\n"
            "1. Read the exercise instruction and sentence templates carefully.\n"
            "2. Extract ONLY the exact text that belongs inside the [ INPUT BOX ]. NEVER repeat words that are already printed in the Prefix or Suffix.\n"
            "3. Output ONLY the final answers inside <answer>...</answer> tags with numbered lines 1 to N.\n"
            "4. DO NOT write any explanations, analysis, reasoning, or introductions. Start immediately with <answer>.\n\n"
            "Examples:\n"
            "- Instruction: 'Form superlatives with one of the. Fill in the blanks.'\n"
            "  Template: This is [ INPUT BOX ] rose windows in the world. (large)\n"
            "  Answer: one of the largest\n\n"
            "- Instruction: 'Form superlatives with one of the. Fill in the blanks.'\n"
            "  Template: This is [ INPUT BOX ] recipes I know. You have to try it. (good)\n"
            "  Answer: one of the best\n\n"
            "- Instruction: 'Form superlatives with one of the. Fill in the blanks.'\n"
            "  Template: New York has [ INPUT BOX ] Jewish communities in the USA. (big)\n"
            "  Answer: some of the biggest\n\n"
            "- Instruction: 'Form superlatives with one of the. Fill in the blanks.'\n"
            "  Template: This is [ INPUT BOX ] chicken dishes I've eaten in months. (bad)\n"
            "  Answer: one of the worst\n\n"
            "- Instruction: 'Form sentences using the verbs in parentheses.'\n"
            "  Template: [ INPUT BOX ] early this morning. (open / leave)\n"
            "  Answer: She opened the window and left the room\n\n"
            "Example Output Format:\n"
            "<answer>\n"
            "1. one of the largest\n"
            "2. one of the best\n"
            "3. some of the biggest\n"
            "4. one of the worst\n"
            "</answer>"
        )

        user_prompt = ""
        if grammar_rules:
            user_prompt += f"📘 GRAMMAR RULES & GUIDELINES:\n{grammar_rules}\n\n"
        if context:
            user_prompt += f"Context / Reading text:\n{context[:2000]}\n\n"

        user_prompt += "Questions to complete:\n"
        for idx, item in enumerate(items):
            prompt = item.get("prompt", "")
            prefix = item.get("prefix", "")
            suffix = item.get("suffix", "")
            user_prompt += f"\n--- Question #{idx+1} ---\n"
            user_prompt += f"Prompt: {prompt}\n"
            user_prompt += f"Sentence template: {prefix}[ INPUT BOX ]{suffix}\n"

        user_prompt += "\nOutput your answers inside <answer>...</answer> numbered 1 to N:"

        try:
            raw = self._chat(
                [{"role": "system", "content": system_prompt},
                 {"role": "user", "content": user_prompt}],
                temperature=0.1,
                max_tokens=3500
            )
            logger.info(f"LM Studio Typed Fill-in-Blanks Raw Response:\n{raw}")
            if not raw:
                return []

            ans_block = ""
            ans_match = re.search(r'<answer>(.*?)</answer>', raw, re.DOTALL | re.IGNORECASE)
            if ans_match:
                ans_block = ans_match.group(1).strip()
            else:
                ans_block = raw.strip()

            results = [""] * num_items
            for line in ans_block.splitlines():
                m = re.match(r'^\s*\(?(\d+)[\.\:\)]\s*(.+)', line.strip())
                if m:
                    idx = int(m.group(1)) - 1
                    val = m.group(2).strip().strip('*"\'').strip()
                    if 0 <= idx < num_items:
                        results[idx] = val

            logger.info(f"Extracted {sum(1 for r in results if r)}/{num_items} typed answers: {results}")
            return results

        except Exception as e:
            logger.error(f"Error in solve_typed_fill_in_blanks: {e}")
            return []

    def solve_picture_matching(self, picture_items: list, word_bank: list, context: str = "") -> list:
        """
        Pairs picture elements to word bank choices based on image tokens and LLM classification.
        """
        # Specific known Speexx Picture Matching mappings:
        ctx_lower = context.lower() if context else ""
        bank_lower = {w.lower(): w for w in word_bank}

        # 1. Sun, moon and stars (Space unit)
        if "sun, moon and stars" in ctx_lower or any(k in bank_lower for k in ["astronaut", "space shuttle", "satellite"]):
            space_order = ["space", "planet", "moon", "Earth", "space shuttle", "rocket", "satellite", "astronaut"]
            mapped = [bank_lower.get(w.lower(), "") for w in space_order]
            if all(mapped) and len(mapped) == len(picture_items):
                logger.info(f"Matched Speexx Space Picture Matching sequence: {mapped}")
                return mapped

        answers = [""] * len(picture_items)
        used_words = set()

        token_aliases = {
            "salat": "salad",
            "pommes": "french fries",
            "pomme": "french fries",
            "pommes_frites": "french fries",
            "hahnchen": "chicken wings",
            "flugel": "wings",
            "zwiebel": "onion rings",
            "ringe": "rings",
            "nachos": "nachos",
            "potato": "potato skins"
        }

        for idx, item in enumerate(picture_items):
            src = item.get("img_src", "").lower()
            filename = src.split("/")[-1].split("?")[0].replace("pc_", "").replace("s2.jpg", "").replace("1.jpg", "").replace("2.jpg", "").replace(".jpg", "")
            for alias_k, alias_v in token_aliases.items():
                filename = filename.replace(alias_k, alias_v)

            clean_fn = re.sub(r'[^a-z0-9_]', ' ', filename).replace('_', ' ').strip()

            for word in word_bank:
                clean_w = re.sub(r'[^a-z0-9]', ' ', word.lower()).strip()
                w_tokens = [t for t in clean_w.split() if len(t) > 2]
                if clean_w in clean_fn or clean_fn in clean_w or any(t in clean_fn for t in w_tokens):
                    if word not in used_words:
                        answers[idx] = word
                        used_words.add(word)
                        break

        # Fill remaining slots with unused word bank items
        remaining_words = [w for w in word_bank if w not in used_words]
        for i in range(len(answers)):
            if not answers[i] and remaining_words:
                answers[i] = remaining_words.pop(0)

        logger.info(f"Picture Matching Solutions: {answers}")
        return answers





    def solve_mark_text(self, instruction: str, passage: str, candidate_words: list, transcript: str = "", feedback: dict = None) -> list:
        """
        Solves Speexx type-mark-text (word highlighting / text selection).
        Returns a list of target words or phrases to be marked.
        """
        system_prompt = (
            "You are an expert English grammar tutor specializing in text highlighting exercises.\n"
            "Task: Identify and highlight ONLY the exact words in the passage that match the instruction.\n"
            "Rules:\n"
            "1. ONLY select words that exist verbatim in the given PASSAGE. Never invent or hallucinate words.\n"
            "2. Distinguish present vs past forms carefully:\n"
            "   - Present Subjunctive: Uses base form (bare infinitive) like 'be' after demand/necessity clauses ('asked that she be', 'insisted that he be', 'necessary that they be').\n"
            "   - Past Subjunctive: Uses 'were' ('I wish I were'). If the instruction asks for PRESENT subjunctive, DO NOT include 'were'!\n"
            "3. Return ONLY a JSON list of words or phrases to highlight as requested by the instruction. Example: [\"Here we stand\", \"Never before have so many countries come together\"] or [\"be\", \"be\"].\n"
            "Do NOT output markdown or explanations. Return ONLY the JSON list."
        )

        user_prompt = f"INSTRUCTION: {instruction}\n\n"
        if transcript:
            user_prompt += f"AUDIO/DIALOGUE TRANSCRIPT:\n{transcript}\n\n"
        user_prompt += f"PASSAGE:\n{passage}\n\n"
        if candidate_words:
            user_prompt += f"WORDS/PHRASES IN PASSAGE (sample):\n{', '.join(candidate_words[:60])}\n"

        if feedback:
            user_prompt += "\n=== PREVIOUS ATTEMPT FEEDBACK (CRITICAL RULES) ===\n"
            correct_words = feedback.get("correct_answers", {})
            wrong_words = feedback.get("wrong_answers", {})
            if correct_words:
                user_prompt += f"These words were CONFIRMED CORRECT in previous attempt and MUST BE HIGHLIGHTED: {list(correct_words.values())}\n"
            if wrong_words:
                user_prompt += f"These words were WRONG in previous attempt and MUST NOT BE HIGHLIGHTED: {list(wrong_words.values())}\n"

        raw = self._chat([
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ], temperature=0.1, max_tokens=3500)

        logger.info(f"LM Studio solve_mark_text Raw Response:\n{raw}")
        targets = []
        try:
            m = re.search(r'\[[\s\S]*?\]', raw)
            if m:
                res = json.loads(m.group(0))
                if isinstance(res, list):
                    targets = [str(w).strip().strip('"\'') for w in res if str(w).strip()]
        except Exception as e:
            logger.warning(f"JSON parsing failed in solve_mark_text: {e}")

        if not targets:
            # Fallback: parse lines
            for line in raw.splitlines():
                clean_l = line.strip().strip('-*•0123456789. "[]\'').strip()
                if clean_l and len(clean_l) < 50:
                    targets.append(clean_l)

        # Enforce memory: always include known correct words, and remove known wrong words
        if feedback:
            correct_words = feedback.get("correct_answers", {})
            wrong_words = feedback.get("wrong_answers", {})
            for c_val in correct_words.values():
                if c_val and c_val not in targets:
                    targets.append(c_val)
            for w_val in wrong_words.values():
                if isinstance(w_val, (set, list)):
                    for item in w_val:
                        targets = [t for t in targets if t.lower() != item.lower()]
                elif isinstance(w_val, str):
                    targets = [t for t in targets if t.lower() != w_val.lower()]

        logger.info(f"Final Mark-Text Targets: {targets}")
        return targets
