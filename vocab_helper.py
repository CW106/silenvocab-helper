import asyncio
import collections
import ctypes
import http.client
import json
import math
import os
import random
import re
import statistics
import sys
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from difflib import SequenceMatcher
import threading
import time
import tkinter as tk
import urllib.parse
import urllib.request

import mss
import numpy as np
import winocr
import zhconv
from PIL import Image

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    ctypes.windll.user32.SetProcessDPIAware()

SCAN_INTERVAL = 0.02
LOOKUP_BUDGET = 0.8  # keep slow dictionaries from blocking the next screenshot
ANSWER_RETRY_SECONDS = 0.45
ANSWER_MAX_ATTEMPTS = 3
MIN_SCORE = 1.0
STRONG_SCORE = 10.0
WEAK_FLOOR = 0.6
WEAK_MARGIN = 0.8
BAG_WEIGHT = 3.0
HOP_WORDS = 4
OTHER_PENALTY = 3.0
HISTORY_BONUS = 2.5
HIST_SIZE = 8
LAYOUT_RATIO = 1.25
UNANSWERED_SHOTS = 20
JUNK_OPTION = ("答案", "離開", "版本", "核對", "傷害", "錯字本", "再來一局", "回首頁", "作答", "命中")
OPTION_MAX_CHARS = 10
FALLBACK_SECONDS = 3.0
ANSWERED_MARKERS = ("等對手", "等手作答", "已作答", "正在核對", "核對答案",
                    "你這題答", "兩邊都答", "你搶到出手權")
STALE_SECONDS = 2.0  # a result this old (slow dictionary) is not clicked: the screen may have changed
ACCURACY = 0.9
QUIZ_WORD = re.compile(r"^[a-z][a-z\-']+( [a-z\-']+)?$")
NOT_OPTION = ("對手", "作答", "處刑者", "進攻者", "防守者", "吸血者", "時間掠奪者", "蓄能者",
              "反擊者", "賭命者", "預言家", "免疫者", "收集者")
OCR_SCALE = 2
CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dict_cache.json")
KNOWN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "known_answers.json")
UA = {"User-Agent": "Mozilla/5.0"}
CJK = re.compile(r"[一-鿿]")
LATIN_WORD = re.compile(r"^[A-Za-z][A-Za-z\-' ]*$")


def _load_json(path):
    """A file that can't be read is moved aside to .bad instead of being overwritten by the next save."""
    try:
        with open(path, encoding="utf-8-sig") as fp:
            data = json.load(fp)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception:
        try:
            os.replace(path, path + ".bad")
        except Exception:
            pass
        return {}


_cache = _load_json(CACHE_FILE)

_miss = set()
_conn = threading.local()
_pool = ThreadPoolExecutor(max_workers=12)
_ocr_pool = ThreadPoolExecutor(max_workers=2)
_lookup_lock = threading.Lock()
_lookup_pending = {}
_save_lock = threading.Lock()
_save_state = {}


def _youdao_json(path):
    for _ in range(2):
        try:
            if getattr(_conn, "c", None) is None:
                _conn.c = http.client.HTTPSConnection("dict.youdao.com", timeout=6)
            _conn.c.request("GET", path, headers=UA)
            return json.loads(_conn.c.getresponse().read().decode("utf-8"))
        except Exception:
            _conn.c = None
    raise ConnectionError("youdao")


def _save_json(path, data, force=False):
    """Write at most every 10 s (or when forced); write a temp file first so a crash can't corrupt it."""
    st = _save_state.setdefault(path, {"dirty": False, "saved": 0.0})
    st["dirty"] = True
    if not force and time.time() - st["saved"] < 10:
        return
    with _save_lock:
        if not st["dirty"]:
            return
        try:
            text = json.dumps(dict(data), ensure_ascii=False, separators=(",", ":"))
            tmp = f"{path}.{os.getpid()}.tmp"
            with open(tmp, "w", encoding="utf-8") as fp:
                fp.write(text)
            os.replace(tmp, path)
            st.update(dirty=False, saved=time.time())
        except Exception:
            pass


def _save_cache(force=False):
    _save_json(CACHE_FILE, _cache, force)


def _get_json(url):
    req = urllib.request.Request(url, headers=UA)
    return json.loads(urllib.request.urlopen(req, timeout=6).read().decode("utf-8"))


def lookup(word):
    word = word.strip().lower()
    if word in _cache:
        return _cache[word]
    if word in _miss:
        return ""
    parts = []
    ok = False
    q = urllib.parse.quote(word)
    try:
        d = _youdao_json(f"/jsonapi_s?doctype=json&jsonversion=4&q={q}")
        ok = True
        ec = d.get("ec") or {}
        for tr in (ec.get("word") or {}).get("trs", []):
            parts.append(tr.get("tran", ""))
        parts += ec.get("web_trans", [])
    except Exception:
        pass
    if not parts:
        try:
            d = _youdao_json(f"/suggest?num=1&doctype=json&q={q}")
            for e in d["data"]["entries"]:
                parts.append(e["explain"])
        except Exception:
            pass
    if not parts:
        try:
            d = _get_json(f"https://api.mymemory.translated.net/get?q={q}&langpair=en|zh-TW")
            parts.append(d["responseData"]["translatedText"])
        except Exception:
            pass
    text = zhconv.convert("；".join(parts), "zh-cn")
    if parts:
        _cache[word] = text
        _save_cache()
    elif ok:
        _miss.add(word)
    return text


def lookup_zh(zh):
    key = "zh:" + clean_zh(zh)
    if key in _cache:
        return _cache[key]
    if key in _miss:
        return []
    words = []
    ok = False
    for q in dict.fromkeys([clean_zh(zh), strip_de(clean_zh(zh))]):
        try:
            d = _youdao_json(f"/jsonapi_s?doctype=json&jsonversion=4&le=en&q={urllib.parse.quote(q)}")
        except Exception:
            continue
        ok = True
        for tr in ((d.get("ce") or {}).get("word") or {}).get("trs", []):
            words.append(tr.get("#text", ""))
        for wt in (d.get("web_trans") or {}).get("web-translation", []):
            if wt.get("key") == q:
                words += [t.get("value", "") for t in wt.get("trans", [])]
    words = [w.strip().lower() for w in words if re.fullmatch(r"[A-Za-z][A-Za-z\-' ]*", w.strip())]
    if words:
        _cache[key] = words
        _save_cache()
    elif ok:
        _miss.add(key)
    return words


def lookup_future(text, reverse=False):
    """Reuse cached results and in-flight requests across screenshots."""
    key = "zh:" + clean_zh(text) if reverse else text.strip().lower()
    with _lookup_lock:
        if key in _cache or key in _miss:
            from concurrent.futures import Future
            future = Future()
            future.set_result(_cache.get(key, [] if reverse else ""))
            return future
        future = _lookup_pending.get(key)
        if future is None or future.done():
            future = _pool.submit(lookup_zh if reverse else lookup, text)
            _lookup_pending[key] = future
            # Drop completed entries without a callback acquiring this lock.
            for old_key, old_future in list(_lookup_pending.items()):
                if old_key != key and old_future.done():
                    del _lookup_pending[old_key]
        return future


def lookup_result(future, deadline, default):
    try:
        return future.result(timeout=max(0.0, deadline - time.monotonic()))
    except TimeoutError:
        return default  # request continues in the background; next scan can reuse it


def _stem(w):
    w = w.lower().strip()
    for suf in ("ation", "ness", "ment", "ing", "ive", "ity", "ous", "ful", "ly", "ed", "es", "al", "er", "e", "s", "y"):
        if w.endswith(suf) and len(w) - len(suf) >= 3:
            return w[: -len(suf)]
    return w


def _prefix_len(a, b):
    n = 0
    for x, y in zip(a, b):
        if x != y:
            break
        n += 1
    return n


def en_match(word, eng_list):
    word = word.lower().strip()
    best = 0.0
    for i, e in enumerate(eng_list):
        if e == word:
            return 20.0 + 5.0 / (1 + i)
        if word in e.split():
            best = max(best, 10.0)
            continue
        p = _prefix_len(e, word)
        if _stem(e) == _stem(word) or p >= 6 or (p >= 5 and p >= 0.8 * min(len(e), len(word))):
            best = max(best, 8.0)
    return best


def meanings_of(explain):
    explain = re.sub(r"\b[a-z]+\.", " ", explain)
    explain = re.sub(r"[（(【\[].*?[)）】\]]", "", explain)
    items = re.split(r"[；;，,、。/\s…]+", explain)
    return [m for m in items if CJK.search(m)]


def clean_zh(s):
    s = zhconv.convert(re.sub(r"\s+", "", s), "zh-cn")
    s = re.sub(r"[^一-鿿]", "", s)
    return s


def strip_de(s):
    return re.sub(r"^(使|被)|[的地得]$", "", s) or s


def lcsubstr(a, b):
    best = 0
    for i in range(len(a)):
        for j in range(i + best + 1, len(a) + 1):
            if a[i:j] in b:
                best = j - i
            else:
                break
    return best


def score(option_zh, explain):
    o = clean_zh(option_zh)
    if not o:
        return 0.0
    ms = meanings_of(explain)
    best = 0.0
    for cand in {o, strip_de(o)}:
        for m in ms:
            m2 = strip_de(m)
            if cand == m or cand == m2:
                return 10.0
            common = lcsubstr(cand, m)
            if common >= 1:
                s = common / len(cand) + 0.5 * common / max(len(m), 1)
                if common >= 2:
                    s += 1
                best = max(best, s)
    overlap = len(set(o) & set(explain)) / len(set(o))
    return best + 0.1 * overlap


def core(s):
    """clean_zh without grammatical padding: leading 使/被/令, trailing 的/地/得."""
    return re.sub(r"[的地得]$", "", re.sub(r"^(使|被|令)", "", clean_zh(s)))


def _char_weights(texts):
    df = collections.Counter(c for t in texts for c in set(clean_zh(t)))
    return {c: math.log((len(texts) + 1) / (k + 1)) for c, k in df.items()}


_idf = _char_weights([x for x in _cache.values() if isinstance(x, str)])


def bag(option_zh, explain):
    """Share of the option's characters (rare characters count more) found anywhere in the explanation.
    Catches synonyms that share characters but not whole words, e.g. 留意 vs 注意，留心."""
    cs = set(core(option_zh))
    if not cs:
        return 0.0
    ex = set(clean_zh(explain))
    total = sum(_idf.get(c, 8.0) for c in cs)
    return sum(_idf.get(c, 8.0) for c in cs if c in ex) / total


def hop(explain, other_explains):
    """Two steps: the option's English translations, then their Chinese meanings vs the question word's.
    e.g. 順手牽羊 -> snitch -> 偷窃, which shoplift's meanings also contain."""
    mine = {core(m) for m in meanings_of(explain)} - {""}
    best = 0.0
    for ex in other_explains:
        for m in meanings_of(ex):
            m = core(m)
            if not m:
                continue
            if m in mine:
                return 2.0
            if len(m) >= 2 and any(lcsubstr(m, x) >= 2 for x in mine):
                best = 1.0
    return best


def is_real_option(text):
    z = clean_zh(text)
    return (bool(z) and len(z) <= OPTION_MAX_CHARS and z not in _junk
            and not any(w in text for w in NOT_OPTION) and not any(j in text for j in JUNK_OPTION))


# ---- answer memory: confirmed meanings per word + the option sets of the last few quizzes for each word.
# The right meaning shows up in every quiz for its word while the wrong options change, so the option
# that keeps coming back is the answer.
_known = _load_json(KNOWN_FILE)
_known.setdefault("answers", {})
_known.setdefault("seen", {})
_junk = set(_known.get("junk", []))
_owner = collections.defaultdict(set)
for _w, _gs in _known["answers"].items():
    for _g in _gs:
        _owner[_g].add(_w)
_inst = {"word": None, "opts": set()}
_shots = {"n": 0, "words": set()}


def save_known(force=False):
    _save_json(KNOWN_FILE, _known, force)


def _same(a, b):
    return a == b or (min(len(a), len(b)) >= 2 and SequenceMatcher(None, a, b).ratio() >= 0.75)


def _hist_top(word):
    cnt = collections.Counter(c for s in _known["seen"].get(word, ()) for c in s if c not in _junk)
    return cnt.most_common(2)


def history_answer(word):
    """The option seen in at least 2 quizzes for this word and at least twice as often as any other."""
    top = _hist_top(word)
    if not top:
        return None
    n2 = top[1][1] if len(top) > 1 else 0
    sets = len(_known["seen"].get(word, ()))
    return top[0][0] if top[0][1] >= 2 and top[0][1] >= 2 * n2 and 2 * top[0][1] >= sets else None


def history_lead(word):
    top = _hist_top(word)
    if top and top[0][1] >= 2 and (len(top) == 1 or top[0][1] > top[1][1]):
        return top[0][0]
    return None


def _pick_among(texts, acc):
    if not acc:
        return None
    hits = [i for i, t in enumerate(texts) if any(_same(clean_zh(t), a) for a in acc)]
    return hits[0] if len(hits) == 1 else None


def known_pick(word, texts):
    return _pick_among(texts, set(_known["answers"].get(word, ())))


def history_pick(word, texts):
    h = history_answer(word)
    return _pick_among(texts, {h} if h else set())


def add_known(word, text):
    g = clean_zh(text)
    gs = _known["answers"].setdefault(word, [])
    if g and g not in _junk and g not in gs:
        gs.append(g)
        _owner[g].add(word)
        save_known()


def gloss_of_other(word, text):
    return any(w != word for w in _owner.get(clean_zh(text), ()))


def note_instance(word, texts):
    """Keep the fullest option set seen for the current quiz; store it when the next word appears."""
    cs = {clean_zh(t) for t in texts} - _junk - {""}
    if _inst["word"] != word:
        w, prev = _inst["word"], _inst["opts"]
        if w and 3 <= len(prev) <= 6:
            h = _known["seen"].setdefault(w, [])
            h.append(sorted(prev))
            del h[:-HIST_SIZE]
            save_known()
        _inst["word"], _inst["opts"] = word, set()
    if len(cs) > len(_inst["opts"]):
        _inst["opts"] = cs


def save_unanswered(img, word):
    """Keep a few screenshots of real quizzes the helper could not answer, for later diagnosis."""
    if _shots["n"] >= UNANSWERED_SHOTS or word in _shots["words"]:
        return
    _shots["n"] += 1
    _shots["words"].add(word)
    path = os.path.join(STUCK_DIR, f"unanswered_{time.strftime('%m%d_%H%M%S')}_{word}.png")

    def save():
        try:
            os.makedirs(STUCK_DIR, exist_ok=True)
            img.convert("RGB").save(path)
        except Exception:
            pass
    _pool.submit(save)


def decide_english(word, texts, layout_ok, deadline=None):
    """Pick the Chinese option for an English word. Returns (index or None, scores, explanation).
    Memory is only used on screens that look like a real quiz."""
    hit = known_pick(word, texts) if layout_ok else None
    if hit is not None:
        return hit, [100.0 if i == hit else 0.0 for i in range(len(texts))], "（記住的答案）"
    if deadline is None:
        deadline = time.monotonic() + LOOKUP_BUDGET
    f_explain = lookup_future(word)
    f_rev = [lookup_future(t, reverse=True) for t in texts]
    explain = lookup_result(f_explain, deadline, "")
    revs = [lookup_result(f, deadline, []) for f in f_rev]
    scores = [score(t, explain) + en_match(word, r) + BAG_WEIGHT * bag(t, explain) for t, r in zip(texts, revs)]
    if explain and max(scores) < STRONG_SCORE:
        futs = {e: lookup_future(e) for r in revs for e in r[:HOP_WORDS]}
        exps = {e: lookup_result(f, deadline, "") for e, f in futs.items()}
        scores = [s + hop(explain, [exps[e] for e in r[:HOP_WORDS]]) for s, r in zip(scores, revs)]
    lead = history_lead(word) if layout_ok else None
    for i, t in enumerate(texts):
        if layout_ok and scores[i] < STRONG_SCORE and gloss_of_other(word, t):
            scores[i] -= OTHER_PENALTY
        if lead and clean_zh(t) == lead:
            scores[i] += HISTORY_BONUS
    order = sorted(range(len(texts)), key=scores.__getitem__, reverse=True)
    top, second = scores[order[0]], scores[order[1]]
    if top >= STRONG_SCORE:
        # only remember a clear winner; two dictionary meanings on screen means we might have picked wrong
        if layout_ok and second < STRONG_SCORE and f_explain.done() and all(f.done() for f in f_rev):
            add_known(word, texts[order[0]])
        return order[0], scores, explain
    # the option that kept coming back in earlier quizzes of this word, when the dictionary has no strong match
    hh = history_pick(word, texts) if layout_ok else None
    if hh is not None:  # weak pick: show() waits for a second frame, so a late-read real answer can win
        return hh, scores, "（之前出現過的答案）"
    if layout_ok and top >= WEAK_FLOOR and top - second >= WEAK_MARGIN:
        return order[0], scores, explain
    return None, scores, explain


def ocr(img, lang, k=OCR_SCALE):
    if k != 1:
        img = img.resize((img.width * k, img.height * k), Image.LANCZOS)
    res = asyncio.run(winocr.recognize_pil(img, lang))
    lines = []
    for ln in res.lines:
        ws = ln.words
        if not ws:
            continue
        words = [(w.text, (w.bounding_rect.x / k, w.bounding_rect.y / k,
                           (w.bounding_rect.x + w.bounding_rect.width) / k,
                           (w.bounding_rect.y + w.bounding_rect.height) / k)) for w in ws]
        x0 = min(b[0] for _, b in words)
        y0 = min(b[1] for _, b in words)
        x1 = max(b[2] for _, b in words)
        y1 = max(b[3] for _, b in words)
        text = "".join(w.text for w in ws) if CJK.search(ln.text) else ln.text
        lines.append({"text": text.strip(), "box": (x0, y0, x1, y1), "h": y1 - y0, "words": words})
    return lines


def is_quiz_word(w):
    """A quiz word has at least 3 letters: 2-letter reads like cc or y' are always timer digits or names."""
    return bool(QUIZ_WORD.match(w)) and len(re.sub(r"[^a-z]", "", w)) >= 3


def quiz_layout(q, lines):
    """A real quiz: 3-6 options under the big word, in one column, spaced evenly and far apart
    (the answer buttons are tall). Room lists and menus have options scattered or packed tightly."""
    if not 3 <= len(lines) <= 6:
        return False
    h = statistics.median(l["h"] for l in lines)
    if q["h"] < LAYOUT_RATIO * h:
        return False
    xs = [l["box"][0] for l in lines]
    cs = [(l["box"][0] + l["box"][2]) / 2 for l in lines]
    if min(max(xs) - min(xs), max(cs) - min(cs)) > 3 * h:
        return False
    ys = sorted(l["box"][1] for l in lines)
    gaps = [b - a for a, b in zip(ys, ys[1:])]
    g = min(gaps)
    # a gap may be a multiple of the step when OCR missed an option in between
    return g >= 2.5 * h and all(abs(x / g - round(x / g)) <= 0.15 for x in gaps)


def binarize(img):
    """Pure black/white copy: removes the game's falling-rain background, which makes OCR skip options."""
    g = np.asarray(img.convert("L"))
    return Image.fromarray(((g > 120) * 255).astype(np.uint8)).convert("RGBA")


def analyse(img):
    f_zh = _ocr_pool.submit(ocr, img, "zh-Hant-TW")
    en_lines = ocr(img, "en")
    zh_lines = f_zh.result()
    deadline = time.monotonic() + LOOKUP_BUDGET
    if is_end_screen(zh_lines):
        return None
    latin = [l for l in en_lines if LATIN_WORD.match(l["text"]) and len(l["text"]) >= 2]
    chinese = [l for l in zh_lines if CJK.search(l["text"]) and len(clean_zh(l["text"])) <= OPTION_MAX_CHARS]

    # after answering, the game shows 等對手作答: the question is done, never guess on it
    waiting = any(m in _norm(l["text"]) for l in zh_lines for m in ANSWERED_MARKERS)

    if latin and len(chinese) >= 2:
        q = max(latin, key=lambda l: (l["h"], -l["box"][1]))
        word = q["text"].strip()
        f_explain = None
        mine = set(_known["answers"].get(word, ()))

        def keep(text):
            nonlocal f_explain
            # a real answer can look like a game button (版本, 投降, 對手...): keep it when it is exactly
            # a dictionary meaning of this word or its remembered answer
            if is_real_option(text):
                return True
            if not is_quiz_word(word) or "作答" in text or not clean_zh(text):
                return False
            if clean_zh(text) in mine:
                return True
            if f_explain is None:
                f_explain = lookup_future(word)
            return score(text, lookup_result(f_explain, deadline, "")) >= STRONG_SCORE

        real = [l for l in chinese if is_real_option(l["text"])]
        # a real quiz: one big English word with 3-6 much smaller options stacked under it
        below = [l for l in chinese if l["box"][1] >= q["box"][3] - 2 and keep(l["text"])]
        layout_ok = is_quiz_word(word) and quiz_layout(q, below)
        if (not layout_ok and not waiting and is_quiz_word(word) and len(below) == 2
                and q["h"] >= LAYOUT_RATIO * statistics.median(l["h"] for l in below)):
            # Two readable options are insufficient to click; try recovering the missing buttons.
            extra = [l for l in ocr(binarize(img), "zh-Hant-TW")
                     if CJK.search(l["text"]) and len(clean_zh(l["text"])) <= OPTION_MAX_CHARS
                     and l["box"][1] >= q["box"][3] - 2 and keep(l["text"])
                     and not any(abs(l["box"][1] - o["box"][1]) < o["h"] for o in below)]
            more = sorted(below + extra, key=lambda l: (l["box"][1], l["box"][0]))
            if extra and quiz_layout(q, more):
                below, layout_ok = more, True
        opts = sorted(below if layout_ok else real, key=lambda l: (l["box"][1], l["box"][0]))
        if len(opts) < 2:
            return None
        idx, scores, explain = decide_english(word, [o["text"] for o in opts], layout_ok, deadline)
        if layout_ok and not waiting and (idx is None or scores[idx] < STRONG_SCORE):
            # not sure: read the options again in black and white and add any the first read missed
            extra = [l for l in ocr(binarize(img), "zh-Hant-TW")
                     if CJK.search(l["text"]) and len(clean_zh(l["text"])) <= OPTION_MAX_CHARS
                     and l["box"][1] >= q["box"][3] - 2 and keep(l["text"])
                     and not any(abs(l["box"][1] - o["box"][1]) < o["h"] for o in opts)]
            more = sorted(opts + extra, key=lambda l: (l["box"][1], l["box"][0]))
            if extra and quiz_layout(q, more):
                opts = more
                idx, scores, explain = decide_english(word, [o["text"] for o in opts], True, deadline)
        if not layout_ok and idx is None:
            return None  # not a quiz screen (or one we can't read): nothing to show, nothing to guess
        if layout_ok:
            note_instance(word, [o["text"] for o in opts])
            if idx is None and not waiting:
                save_unanswered(img, word)
        flag = "wait" if waiting else "quiz" if layout_ok else ""
        if not explain and idx is None:
            return word, opts, None, "(查不到這個字)", scores, flag
        return word, opts, idx, explain, scores, flag

    if chinese and len(latin) >= 2:
        q = max(chinese, key=lambda l: (l["h"], -l["box"][1]))
        below = [l for l in latin if l["box"][1] >= q["box"][3] - 2 and QUIZ_WORD.match(l["text"].strip().lower())]
        layout_ok = bool(3 <= len(below) <= 6 and q["h"] >= LAYOUT_RATIO * statistics.median(l["h"] for l in below))
        opts = sorted(below if layout_ok else latin, key=lambda l: (l["box"][1], l["box"][0]))
        flag = "wait" if waiting else "quiz" if layout_ok else ""
        owners = _owner.get(clean_zh(q["text"]), ()) if layout_ok else ()
        hits = [i for i, o in enumerate(opts) if o["text"].strip().lower() in owners]
        if len(hits) == 1:
            return (q["text"], opts, hits[0], "（記住的答案）",
                    [100.0 if i == hits[0] else 0.0 for i in range(len(opts))], flag)
        f_rev = lookup_future(q["text"], reverse=True)
        f_fwd = [lookup_future(o["text"]) for o in opts]
        rev = lookup_result(f_rev, deadline, [])
        scores = [score(q["text"], lookup_result(f, deadline, "")) + en_match(o["text"], rev)
                  for o, f in zip(opts, f_fwd)]
        # on a screen that doesn't look like a quiz, only answer when the match is certain
        if max(scores) < (MIN_SCORE if layout_ok else STRONG_SCORE):
            return None
        return q["text"], opts, max(range(len(opts)), key=scores.__getitem__), "", scores, flag
    return None


BTN_REPLAY = "再來一局"
BTN_CHAR = "處刑者"
BTN_LOCK = "鎖定"
BTN_SHIELD = "護盾"
BTN_TANK = "硬吃"
BTN_ATTACK = "攻擊"
BTN_CONTINUE = "繼續"
BTN_SUBMIT = "送出"
BTN_CLOSE = "關閉"
DAILY_MARKER = "每日簽到"
BTN_BATTLE = "對戰"
RANK_CARD = "本賽季"
RANK_CARD_EXTRA_CHARS = 20
RANK_CARD_COOLDOWN = 10
BTN_RANKED = "開始排位"
IDLE_REFRESH_SECONDS = 60
RECOVER_SECONDS = 30
SUBMIT_EXTRA_CHARS = 12
GAME_SCAN_INTERVAL = 0.15
GAME_OCR_SCALE = 1
CLICK_COOLDOWN = 1.0
STUCK_SECONDS = 25
STUCK_SHOTS = 30  # at most this many stuck screenshots per run (each is ~1 MB)
STUCK_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "stuck")
BTN_EXTRA_CHARS = 6
END_MARKERS = ("距離下一階", "錯字本", "要記一下", "本局結算", "再來一局")
GRAY_MIN, GRAY_MAX = 40, 100
GRAY_BLUE_TINT = 11
GRAY_MIN_WIDTH = 0.5
GRAY_MIN_ROWS = 3


def is_end_screen(lines):
    text = "".join(_norm(l["text"]) for l in lines).replace("-", "一")
    return any(m in text for m in END_MARKERS)


def find_gray_button(img):
    a = np.asarray(img.convert("RGB"), dtype=np.int16)
    r, g, b = a[..., 0], a[..., 1], a[..., 2]
    mask = (r >= GRAY_MIN) & (r <= GRAY_MAX) & (abs(g - r) <= 8) & (b - r <= GRAY_BLUE_TINT) & (b - r >= -3)
    rows = np.where(mask.sum(axis=1) >= GRAY_MIN_WIDTH * a.shape[1])[0]
    if len(rows) < GRAY_MIN_ROWS:
        return None
    end = len(rows) - 1
    start = end
    while start > 0 and rows[start - 1] >= rows[start] - 2:
        start -= 1
    y0, y1 = rows[start], rows[end]
    if y1 - y0 + 1 < GRAY_MIN_ROWS:
        return None
    cols = np.where(mask[y0:y1 + 1].any(axis=0))[0]
    return int((cols[0] + cols[-1]) / 2), int((y0 + y1) / 2), y1 >= a.shape[0] - 3


OCR_FIXES = str.maketrans({"-": "一", "—": "一", "－": "一", "頀": "護"})


def _norm(s):
    return zhconv.convert(re.sub(r"\s+", "", s), "zh-tw").translate(OCR_FIXES)


def find_button(lines, kw, extra=None):
    extra = BTN_EXTRA_CHARS if extra is None else extra
    for ln in lines:
        joined, owner = "", []
        for i, (t, _) in enumerate(ln["words"]):
            t = _norm(t)
            joined += t
            owner += [i] * len(t)
        pos = joined.find(kw)
        if pos < 0 or len(joined) > len(kw) + extra:
            continue
        boxes = [ln["words"][i][1] for i in sorted(set(owner[pos:pos + len(kw)]))]
        x0 = min(b[0] for b in boxes)
        y0 = min(b[1] for b in boxes)
        x1 = max(b[2] for b in boxes)
        y1 = max(b[3] for b in boxes)
        return int((x0 + x1) / 2), int((y0 + y1) / 2)
    return None


def exclude_from_capture(win):
    try:
        win.update_idletasks()
        hwnd = ctypes.windll.user32.GetAncestor(win.winfo_id(), 2)
        ctypes.windll.user32.SetWindowDisplayAffinity(hwnd, 0x11)
    except Exception:
        pass


LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "answers_log.txt")


def log(q, opts, idx, scores):
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as fp:
            parts = [f"{'★' if i == idx else ' '}{o['text']}({s:.1f})" for i, (o, s) in enumerate(zip(opts, scores))]
            fp.write(f"{time.strftime('%H:%M:%S')}  {q}  ->  {'  '.join(parts)}\n")
    except Exception:
        pass


GAME_LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "game_log.txt")


def glog(msg):
    try:
        with open(GAME_LOG_FILE, "a", encoding="utf-8") as fp:
            fp.write(f"{time.strftime('%m-%d %H:%M:%S')}  {msg}\n")
    except Exception:
        pass


class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


CURVE_SPEEDS = ((0.06, 0.10), (0.10, 0.16), (0.16, 0.25))
CURVE_JITTER = 1.5


def _bezier_path(sx, sy, x, y, dur):
    dx, dy = x - sx, y - sy
    b1 = random.uniform(-0.4, 0.4)
    b2 = random.uniform(-0.4, 0.4)
    p1 = (sx + dx * random.uniform(0.2, 0.45) - dy * b1, sy + dy * random.uniform(0.2, 0.45) + dx * b1)
    p2 = (sx + dx * random.uniform(0.55, 0.85) - dy * b2, sy + dy * random.uniform(0.55, 0.85) + dx * b2)
    steps = max(8, int(dur / random.uniform(0.006, 0.012)))
    k = random.uniform(1.5, 3.0)
    for i in range(1, steps + 1):
        s = i / steps
        t = s ** k / (s ** k + (1 - s) ** k)
        m = 1 - t
        bx = m ** 3 * sx + 3 * m * m * t * p1[0] + 3 * m * t * t * p2[0] + t ** 3 * x
        by = m ** 3 * sy + 3 * m * m * t * p1[1] + 3 * m * t * t * p2[1] + t ** 3 * y
        j = CURVE_JITTER * (1 - t)
        yield bx + random.uniform(-j, j), by + random.uniform(-j, j), dur / steps * random.uniform(0.7, 1.3)


def curve_move(x, y):
    u = ctypes.windll.user32
    p = _POINT()
    u.GetCursorPos(ctypes.byref(p))
    sx, sy = p.x, p.y
    dist = ((x - sx) ** 2 + (y - sy) ** 2) ** 0.5
    if dist < 3:
        u.SetCursorPos(x, y)
        return
    dur = random.uniform(*random.choice(CURVE_SPEEDS)) * min(1.5, max(0.6, dist / 600))
    legs = [(x, y, dur)]
    if dist > 150 and random.random() < 0.3:
        over = random.uniform(0.02, 0.06)
        ox = x + (x - sx) * over + random.uniform(-4, 4)
        oy = y + (y - sy) * over + random.uniform(-4, 4)
        legs = [(ox, oy, dur * 0.85), (x, y, dur * 0.25)]
    for tx, ty, leg_dur in legs:
        for bx, by, dt in _bezier_path(sx, sy, tx, ty, leg_dur):
            u.SetCursorPos(int(round(bx)), int(round(by)))
            time.sleep(dt)
        sx, sy = tx, ty
    u.SetCursorPos(x, y)


CLICK_DELAY = (0.01, 0.5)
CLICK_DELAY_CHANCE = 0.2
_mouse = threading.Lock()


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", ctypes.c_long), ("dy", ctypes.c_long),
                ("mouseData", ctypes.c_ulong), ("dwFlags", ctypes.c_ulong),
                ("time", ctypes.c_ulong), ("dwExtraInfo", ctypes.c_size_t)]


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", ctypes.c_ushort), ("wScan", ctypes.c_ushort),
                ("dwFlags", ctypes.c_ulong), ("time", ctypes.c_ulong),
                ("dwExtraInfo", ctypes.c_size_t)]


class _HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", ctypes.c_ulong), ("wParamL", ctypes.c_ushort),
                ("wParamH", ctypes.c_ushort)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT), ("hi", _HARDWAREINPUT)]


class _INPUT(ctypes.Structure):
    _anonymous_ = ("data",)
    _fields_ = [("type", ctypes.c_ulong), ("data", _INPUTUNION)]


def click_at(x, y, fast=False, guard=None):
    """Send a complete click, checking cancellation and whether Windows accepted it."""
    u = ctypes.WinDLL("user32", use_last_error=True)
    u.SendInput.argtypes = [ctypes.c_uint, ctypes.POINTER(_INPUT), ctypes.c_int]
    u.SendInput.restype = ctypes.c_uint
    u.WindowFromPoint.argtypes = [_POINT]
    u.WindowFromPoint.restype = ctypes.c_void_p
    u.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
    if not fast and random.random() < CLICK_DELAY_CHANCE:
        time.sleep(random.uniform(*CLICK_DELAY))
    with _mouse:
        if (guard is not None and not guard()) or u.GetAsyncKeyState(0x77) & 0x8000:
            return False
        if fast:
            if not u.SetCursorPos(x, y):
                raise OSError("無法移動滑鼠到答案位置")
            time.sleep(0.015)  # let the browser process pointer movement before button-down
        else:
            curve_move(x, y)
            time.sleep(0.03)
        if (guard is not None and not guard()) or u.GetAsyncKeyState(0x77) & 0x8000:
            return False
        p = _POINT()
        if not u.GetCursorPos(ctypes.byref(p)) or abs(p.x - x) > 3 or abs(p.y - y) > 3:
            raise OSError("滑鼠位置被移動，取消這次點擊")
        hwnd = u.WindowFromPoint(_POINT(x, y))
        pid = ctypes.c_ulong()
        if hwnd:
            u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value == os.getpid():
                raise OSError("小幫手視窗擋住答案，請將小幫手移開")
        down, up = _INPUT(), _INPUT()
        down.mi.dwFlags, up.mi.dwFlags = 0x0002, 0x0004
        ctypes.set_last_error(0)
        if u.SendInput(1, ctypes.byref(down), ctypes.sizeof(_INPUT)) != 1:
            raise OSError(f"Windows 未接受滑鼠按下（錯誤 {ctypes.get_last_error()}）")
        try:
            time.sleep(0.025 if fast else 0.03)
        finally:
            if u.SendInput(1, ctypes.byref(up), ctypes.sizeof(_INPUT)) != 1:
                u.mouse_event(0x0004, 0, 0, 0, 0)  # do not leave the button held after a failed release
                raise OSError(f"Windows 未接受滑鼠放開（錯誤 {ctypes.get_last_error()}）")
    return True


def refresh_page(x, y):
    u = ctypes.windll.user32
    u.WindowFromPoint.restype = ctypes.c_void_p
    u.GetAncestor.restype = ctypes.c_void_p
    with _mouse:
        hwnd = u.WindowFromPoint(_POINT(x, y))
        if hwnd:
            top = u.GetAncestor(ctypes.c_void_p(hwnd), 2)
            u.keybd_event(0x12, 0, 0, 0)
            u.keybd_event(0x12, 0, 2, 0)
            u.SetForegroundWindow(ctypes.c_void_p(top))
            time.sleep(0.2)
        fg = u.GetForegroundWindow()
        buf = ctypes.create_unicode_buffer(256)
        u.GetWindowTextW(fg, buf, 256)
        u.keybd_event(0x74, 0, 0, 0)
        time.sleep(0.05)
        u.keybd_event(0x74, 0, 2, 0)
    return buf.value


def scroll_at(x, y, notches=-5):
    u = ctypes.windll.user32
    with _mouse:
        curve_move(x, y)
        u.mouse_event(0x0800, 0, 0, ctypes.c_ulong(notches * 120 & 0xFFFFFFFF), 0)


class RegionSelector:
    def __init__(self, root, callback):
        self.cb = callback
        with mss.mss() as s:
            mon = s.monitors[0]
        self.ox, self.oy = mon["left"], mon["top"]
        self.top = tk.Toplevel(root)
        self.top.overrideredirect(True)
        self.top.geometry(f"{mon['width']}x{mon['height']}+{mon['left']}+{mon['top']}")
        self.top.attributes("-alpha", 0.3)
        self.top.attributes("-topmost", True)
        self.c = tk.Canvas(self.top, bg="black", cursor="cross", highlightthickness=0)
        self.c.pack(fill="both", expand=True)
        self.c.create_text(mon["width"] // 2, 60, text="拖曳框選題目區域 (Esc 取消)",
                           fill="white", font=("Microsoft JhengHei", 24, "bold"))
        self.c.bind("<ButtonPress-1>", self.down)
        self.c.bind("<B1-Motion>", self.move)
        self.c.bind("<ButtonRelease-1>", self.up)
        self.top.bind("<Escape>", lambda e: self.top.destroy())
        self.top.focus_force()
        self.rect = None

    def down(self, e):
        self.sx, self.sy = e.x, e.y
        self.rect = self.c.create_rectangle(e.x, e.y, e.x, e.y, outline="#3f3", width=3)

    def move(self, e):
        self.c.coords(self.rect, self.sx, self.sy, e.x, e.y)

    def up(self, e):
        x0, x1 = sorted((self.sx, e.x))
        y0, y1 = sorted((self.sy, e.y))
        self.top.destroy()
        if x1 - x0 > 20 and y1 - y0 > 20:
            self.cb({"left": x0 + self.ox, "top": y0 + self.oy, "width": x1 - x0, "height": y1 - y0})


class App:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("單字小幫手")
        self.root.attributes("-topmost", True)
        self.root.configure(bg="#1e1e1e")
        self.region = None
        self.running = False
        self.last_key = None

        f = ("Microsoft JhengHei", 11)
        bar = tk.Frame(self.root, bg="#1e1e1e")
        bar.pack(fill="x", padx=8, pady=6)
        tk.Button(bar, text="① 框選區域", font=f, command=self.pick).pack(side="left")
        self.btn = tk.Button(bar, text="② 開始", font=f, command=self.toggle, state="disabled")
        self.btn.pack(side="left", padx=6)
        self.auto = tk.BooleanVar(value=False)
        tk.Checkbutton(bar, text="自動點答案", variable=self.auto, font=f, fg="white",
                       bg="#1e1e1e", selectcolor="#333", activebackground="#1e1e1e",
                       activeforeground="white", command=self.set_auto).pack(side="left")
        self.pending_key = None
        self.clicked_key = None
        self.cur_q, self.cur_q_since, self.clicked_q = None, 0.0, None
        self.answer_attempts = 0
        self.answer_last_attempt = 0.0
        self.answer_choice = None
        self.gen = 0
        self.none_since = None

        bar2 = tk.Frame(self.root, bg="#1e1e1e")
        bar2.pack(fill="x", padx=8, pady=(0, 6))
        tk.Button(bar2, text="框選遊戲畫面", font=f, command=self.pick_game).pack(side="left")
        self.game_var = tk.BooleanVar(value=False)
        tk.Checkbutton(bar2, text="自動打遊戲", variable=self.game_var, font=f, fg="white",
                       bg="#1e1e1e", selectcolor="#333", activebackground="#1e1e1e",
                       activeforeground="white",
                       command=self.set_game_on).pack(side="left", padx=6)
        self.game_on = False
        self.quiz_active = False
        self.recovering_until = 0.0
        self.rank_card_clicked = False
        self.game_region = None
        self.char_picked = False
        self.last_game_scan = 0.0
        self.last_click = {}
        self.end_screen_until = 0.0
        self.action_at = 0.0
        self.action_block_until = 0.0
        self.last_action = time.time()
        self.stuck_saved = False
        self.stuck_count = 0

        self.word = tk.Label(self.root, text="先框選題目區域", font=("Segoe UI", 20, "bold"),
                             fg="white", bg="#1e1e1e")
        self.word.pack(anchor="w", padx=10)
        self.ans = tk.Label(self.root, text="", font=("Microsoft JhengHei", 22, "bold"),
                            fg="#4cff6a", bg="#1e1e1e")
        self.ans.pack(anchor="w", padx=10)
        self.expl = tk.Label(self.root, text="", font=("Microsoft JhengHei", 10), fg="#aaa",
                             bg="#1e1e1e", wraplength=340, justify="left")
        self.expl.pack(anchor="w", padx=10, pady=(0, 8))
        self.game_lbl = tk.Label(self.root, text="", font=("Microsoft JhengHei", 10), fg="#7cc4ff",
                                 bg="#1e1e1e")
        self.game_lbl.pack(anchor="w", padx=10)
        self.click_lbl = tk.Label(self.root, text="自動點答案：未勾選", font=("Microsoft JhengHei", 10),
                                  fg="#ffc66d", bg="#1e1e1e", wraplength=395, justify="left")
        self.click_lbl.pack(anchor="w", padx=10, pady=(2, 0))
        tk.Label(self.root, text="F8 = 緊急停止", font=("Microsoft JhengHei", 9), fg="#777",
                 bg="#1e1e1e").pack(anchor="w", padx=10)
        self.root.geometry("420x335+30+30")
        exclude_from_capture(self.root)

        self.marker = tk.Toplevel(self.root)
        self.marker.overrideredirect(True)
        self.marker.attributes("-topmost", True)
        tk.Label(self.marker, text="✔ 答案", font=("Microsoft JhengHei", 16, "bold"),
                 fg="white", bg="#16a34a", padx=10, pady=2).pack()
        self.marker.withdraw()
        exclude_from_capture(self.marker)

    def pick(self):
        self.root.withdraw()
        self.root.after(200, lambda: RegionSelector(self.root, self.set_region))
        self.root.after(250, self.root.deiconify)

    def set_region(self, r):
        self.region = r
        self.btn.config(state="normal")
        self.word.config(text="區域已設定，按「開始」")
        if not self.running:
            self.toggle()

    def pick_game(self):
        self.root.withdraw()
        self.root.after(200, lambda: RegionSelector(self.root, self.set_game_region))
        self.root.after(250, self.root.deiconify)

    def set_game_region(self, r):
        self.game_region = r
        self.game_lbl.config(text="遊戲畫面已設定")

    def set_game_on(self):
        self.game_on = self.game_var.get()
        self.last_action = time.time()

    def reset_answer(self):
        self.pending_key = self.clicked_key = None
        self.clicked_q = None
        self.answer_attempts = 0
        self.answer_last_attempt = 0.0
        self.answer_choice = None

    def set_auto(self):
        self.pending_key = None
        # Explicitly re-enabling the checkbox allows another attempt after a failed click.
        self.answer_attempts = 0
        self.answer_last_attempt = 0.0
        self.answer_choice = None
        self.click_lbl.config(text="自動點答案：已開啟" if self.auto.get() else "自動點答案：未勾選")

    def game_step(self, sct, quiz_active):
        now = time.time()
        if now - self.last_game_scan < GAME_SCAN_INTERVAL:
            return
        self.last_game_scan = now
        region = dict(self.game_region or sct.monitors[1])
        shot = sct.grab(region)
        img = Image.frombytes("RGB", shot.size, shot.rgb).convert("RGBA")
        lines = ocr(img, "zh-Hant-TW", GAME_OCR_SCALE)
        if now - self.last_action > STUCK_SECONDS and not self.stuck_saved and self.stuck_count < STUCK_SHOTS:
            self.stuck_saved = True
            self.stuck_count += 1
            try:
                os.makedirs(STUCK_DIR, exist_ok=True)
                fn = os.path.join(STUCK_DIR, time.strftime("%m%d_%H%M%S") + ".png")
                img.convert("RGB").save(fn)
                self.root.after(0, lambda: self.game_lbl.config(text=f"⚠ 卡住 {STUCK_SECONDS} 秒，截圖存到 stuck/{os.path.basename(fn)}"))
            except Exception:
                pass
        if now - self.last_action > IDLE_REFRESH_SECONDS:
            title = refresh_page(region["left"] + region["width"] // 2, region["top"] + region["height"] // 2)
            glog(f"{IDLE_REFRESH_SECONDS} 秒沒動作 -> 按 F5，前景視窗：{title}")
            self.last_action, self.stuck_saved = time.time(), False
            self.recovering_until = time.time() + RECOVER_SECONDS
            self.rank_card_clicked = False
            self.last_click["recover_scroll"] = time.time() + 2
            self.char_picked = False
            self.root.after(0, lambda: self.game_lbl.config(text=f"🔄 {IDLE_REFRESH_SECONDS} 秒沒動作，重新整理網頁"))
            return
        if now < self.recovering_until:
            cx, cy = region["left"] + region["width"] // 2, region["top"] + region["height"] // 2
            mon = next((m for m in sct.monitors[1:] if m["left"] <= cx < m["left"] + m["width"]
                        and m["top"] <= cy < m["top"] + m["height"]), sct.monitors[1])
            full = sct.grab(mon)
            full_lines = ocr(Image.frombytes("RGB", full.size, full.rgb).convert("RGBA"), "zh-Hant-TW",
                             2 if max(full.size) * 2 <= 8000 else 1)
            kws = [BTN_RANKED]
            if not self.rank_card_clicked:
                kws.append(RANK_CARD)
            if any(DAILY_MARKER in _norm(l["text"]) for l in full_lines):
                kws.insert(0, BTN_CLOSE)
            for kw in kws:
                pos = find_button(full_lines, kw, RANK_CARD_EXTRA_CHARS if kw == RANK_CARD else None)
                if pos and now - self.last_click.get(kw, 0) >= CLICK_COOLDOWN:
                    if kw == RANK_CARD:
                        self.rank_card_clicked = True
                        self.last_click["recover_scroll"] = time.time() + 1
                    click_at(mon["left"] + pos[0], mon["top"] + pos[1])
                    self.last_click[kw] = self.last_action = time.time()
                    glog(f"恢復中：點了 {kw} @ {mon['left'] + pos[0]},{mon['top'] + pos[1]}")
                    if kw == BTN_RANKED:
                        self.recovering_until = 0.0
                    self.root.after(0, lambda kw=kw: self.game_lbl.config(text=f"🎮 點了：{kw}  ({time.strftime('%H:%M:%S')})"))
                    return
            if now - self.last_click.get("recover_scroll", 0) >= 1.0:
                self.last_click["recover_scroll"] = now
                glog("恢復中：找不到 段位卡片/開始排位，往下捲 | " + " / ".join(_norm(l["text"]) for l in full_lines)[-300:])
                scroll_at(cx, cy, -10)
            return
        found = {kw: find_button(lines, kw)
                 for kw in (BTN_REPLAY, BTN_CHAR, BTN_LOCK, BTN_SHIELD, BTN_TANK, BTN_ATTACK, BTN_CONTINUE)}
        found[BTN_SUBMIT] = find_button(lines, BTN_SUBMIT, SUBMIT_EXTRA_CHARS)
        shield_empty = any(BTN_SHIELD in t and "(0)" in t for t in (_norm(l["text"]) for l in lines))

        def press(kw, pos=None):
            if now - self.last_click.get(kw, 0) < CLICK_COOLDOWN:
                return False
            x, y = pos or found[kw]
            if not click_at(region["left"] + x, region["top"] + y,
                            guard=lambda: self.running and self.game_on and not self.quiz_active
                            and time.time() - now <= STALE_SECONDS):
                return False
            self.last_click[kw] = self.last_action = time.time()
            self.stuck_saved = False
            glog(f"點了 {kw}")
            self.root.after(0, lambda: self.game_lbl.config(text=f"🎮 點了：{kw}  ({time.strftime('%H:%M:%S')})"))
            return True

        if any(DAILY_MARKER in _norm(l["text"]) for l in lines):
            found[BTN_CLOSE] = find_button(lines, BTN_CLOSE)
            if found[BTN_CLOSE]:
                return press(BTN_CLOSE)
        if found[BTN_REPLAY]:
            self.char_picked = False
            self.end_screen_until = now + 1.5
            return press(BTN_REPLAY)
        if found[BTN_CONTINUE]:
            return press(BTN_CONTINUE)
        found[BTN_RANKED] = find_button(lines, BTN_RANKED)
        if found[BTN_RANKED] and not quiz_active:
            return press(BTN_RANKED)
        if is_end_screen(lines):
            self.end_screen_until = now + 1.5
            gray = find_gray_button(img)
            if gray and not gray[2]:
                self.char_picked = False
                return press(BTN_REPLAY + " (灰色按鈕)", gray[:2])
            if now - self.last_click.get("scroll", 0) >= 0.6:
                self.last_click["scroll"] = now
                scroll_at(region["left"] + region["width"] // 2, region["top"] + region["height"] // 2)
            return
        if quiz_active or self.quiz_active:
            return
        card = find_button(lines, RANK_CARD, RANK_CARD_EXTRA_CHARS)
        if card and now - self.last_click.get(RANK_CARD, 0) >= RANK_CARD_COOLDOWN:
            return press(RANK_CARD, card)
        if found[BTN_CHAR] and not self.char_picked:
            if press(BTN_CHAR):
                self.char_picked = True
            return
        if found[BTN_LOCK] and self.char_picked:
            return press(BTN_LOCK)
        if found[BTN_SUBMIT] and now - self.action_at < 5:
            if press(BTN_SUBMIT):
                self.action_at = 0.0
                self.action_block_until = time.time() + 2
            return
        if now < self.action_block_until:
            return
        for kw in (BTN_SHIELD, BTN_TANK, BTN_ATTACK):
            if kw == BTN_SHIELD and shield_empty:
                continue
            if found[kw]:
                if press(kw):
                    self.action_at = time.time()
                return

    def toggle(self):
        self.running = not self.running
        self.gen += 1  # threads from an earlier start see a new generation and stop
        self.btn.config(text="暫停" if self.running else "② 開始")
        if self.running:
            threading.Thread(target=self.loop, args=(self.gen,), daemon=True).start()
            threading.Thread(target=self.game_loop, args=(self.gen,), daemon=True).start()
        else:
            self.marker.withdraw()

    def game_loop(self, gen):
        with mss.mss() as sct:
            while self.running and gen == self.gen:
                if self.game_on:
                    try:
                        self.game_step(sct, self.quiz_active)
                    except Exception as e:
                        self.root.after(0, lambda e=e: self.game_lbl.config(text=f"錯誤: {e}"))
                time.sleep(0.03)

    def loop(self, gen):
        with mss.mss() as sct:
            while self.running and gen == self.gen:
                try:
                    region = dict(self.region)
                    t_shot = time.time()
                    shot = sct.grab(region)
                    img = Image.frombytes("RGB", shot.size, shot.rgb).convert("RGBA")
                    result = analyse(img)
                    if not self.running or gen != self.gen:
                        return
                    # a quiz is on screen (even one we can't answer): keep the game clicker's hands off
                    self.quiz_active = bool(result and result[5] == "quiz")
                    self.root.after(0, self.show, region, result, 0.0, t_shot, gen)
                except Exception as e:
                    self.root.after(0, lambda e=e: self.expl.config(text=f"錯誤: {e}"))
                scan_until = time.monotonic() + SCAN_INTERVAL
                while time.monotonic() < scan_until:
                    if ctypes.windll.user32.GetAsyncKeyState(0x77) & 0x8000:
                        self.root.after(0, self.stop_by_hotkey)
                        return
                    time.sleep(min(0.01, max(0.0, scan_until - time.monotonic())))

    def stop_by_hotkey(self):
        if self.running:
            self.toggle()
        self.word.config(text="已按 F8 停止")

    def show(self, region, result, age=0.0, shot_at=None, gen=None):
        if not self.running or (gen is not None and gen != self.gen):
            return
        now = time.time()
        if shot_at is not None:
            age = now - shot_at
        if not result:
            # forget what we clicked only after the quiz has been gone for a while, not on one unread frame
            if self.none_since is None:
                self.none_since = now
            elif now - self.none_since > 1.5:
                self.reset_answer()
                self.cur_q = None
            self.word.config(text="(沒偵測到題目)")
            self.ans.config(text="")
            self.marker.withdraw()
            self.click_lbl.config(text="等待題目" if self.auto.get() else "自動點答案：未勾選")
            return
        self.none_since = None
        q, opts, idx, explain, scores, flag = result
        self.word.config(text=q)
        score_txt = "  ".join(f"{op['text']}:{s:.1f}" for op, s in zip(opts, scores))
        self.expl.config(text=score_txt + "\n" + zhconv.convert(explain, "zh-tw")[:120])
        key = (q, tuple(op["text"] for op in opts), idx)
        if key != self.last_key:
            self.last_key = key
            if idx is not None or QUIZ_WORD.match(q):
                log(q, opts, idx, scores)
        if q != self.cur_q:
            self.reset_answer()
            self.cur_q, self.cur_q_since = q, now
        if flag == "wait":
            self.clicked_q = q  # already answered (by us or by hand): don't guess on it
        # click only on a screen laid out like a real quiz, with a fresh result
        can_click = flag == "quiz" and age <= STALE_SECONDS
        if not self.auto.get():
            self.click_lbl.config(text="自動點答案：未勾選（目前只顯示答案）")
        elif flag == "wait":
            self.click_lbl.config(text="已偵測到作答完成，不再點擊")
        elif flag != "quiz":
            self.click_lbl.config(text="選項位置尚未確認，重新辨識中")
        elif age > STALE_SECONDS:
            self.click_lbl.config(text="畫面已過期，等待新的辨識結果")
        if can_click and idx is None and self.try_fallback(region, q, opts, scores, now, shot_at, gen):
            return
        if idx is None:
            self.ans.config(text="?")
            self.marker.withdraw()
            if self.auto.get() and can_click and self.answer_attempts:
                self.click_answer(region, q, opts, 0, now, shot_at, gen, guess=True)
            return
        o = opts[idx]
        self.ans.config(text=f"答案：{o['text']}")
        x0, y0, x1, y1 = o["box"]
        self.marker.update_idletasks()
        mh = self.marker.winfo_reqheight()
        mx = region["left"] + int(x1) + 20
        my = region["top"] + int((y0 + y1) / 2) - mh // 2
        self.marker.geometry(f"+{mx}+{my}")
        self.marker.deiconify()
        self.marker.lift()

        if time.time() < self.end_screen_until:
            self.marker.withdraw()
            return
        ans_key = (q, clean_zh(o["text"]))
        if self.auto.get() and can_click and self.clicked_q != q:
            if ans_key == self.pending_key or scores[idx] >= STRONG_SCORE or self.answer_attempts:
                self.click_answer(region, q, opts, idx, now, shot_at, gen)
            else:
                self.click_lbl.config(text="正在確認答案，下一次辨識後點擊")
            self.pending_key = ans_key

    def click_answer(self, region, q, opts, idx, now, shot_at=None, gen=None, guess=False):
        """Retry only on fresh quiz frames, preserving the originally clicked option."""
        if self.clicked_q == q:
            return False
        if self.answer_attempts >= ANSWER_MAX_ATTEMPTS:
            self.click_lbl.config(text=f"已嘗試 {ANSWER_MAX_ATTEMPTS} 次，仍未確認；請檢查視窗遮擋或重新勾選自動點答案")
            return False
        if self.answer_attempts and now - self.answer_last_attempt < ANSWER_RETRY_SECONDS:
            return False
        if self.answer_choice is not None:
            hits = [i for i, o in enumerate(opts) if clean_zh(o["text"]) == self.answer_choice]
            if len(hits) != 1:
                self.click_lbl.config(text="等待原選項重新辨識，暫不重試")
                return False
            idx = hits[0]
        elif not guess:
            wrong = [i for i, op in enumerate(opts)
                     if i != idx and not any(w in _norm(op["text"]) for w in NOT_OPTION)]
            if wrong and random.random() >= ACCURACY:
                idx = random.choice(wrong)
        o = opts[idx]
        x0, y0, x1, y1 = o["box"]
        cx, cy = int((x0 + x1) / 2), int((y0 + y1) / 2)
        if not (0 <= cx < region["width"] and 0 <= cy < region["height"]):
            self.click_lbl.config(text="答案位置超出框選區域，請重新框選")
            return False
        self.answer_choice = clean_zh(o["text"])
        self.answer_attempts += 1
        self.answer_last_attempt = now
        self.marker.withdraw()  # the floating answer label must not intercept the click
        def fresh():
            return (self.running and self.auto.get() and (gen is None or gen == self.gen)
                    and self.cur_q == q and self.clicked_q != q
                    and time.time() >= self.end_screen_until
                    and (shot_at is None or time.time() - shot_at <= STALE_SECONDS))
        try:
            sent = click_at(region["left"] + cx, region["top"] + cy, fast=True, guard=fresh)
        except OSError as e:
            self.click_lbl.config(text=f"點擊失敗：{e}")
            glog(f"答案點擊失敗 {q} @ {region['left'] + cx},{region['top'] + cy}：{e}")
            return False
        if not sent:
            # Cancellation did not send an input event, so it does not use up a retry.
            self.answer_attempts -= 1
            self.click_lbl.config(text="已取消過期或停止的點擊，等待新畫面")
            return False
        self.clicked_key = (q, self.answer_choice)
        self.answer_last_attempt = self.last_action = time.time()
        self.stuck_saved = False
        self.click_lbl.config(text=f"已點「{o['text']}」（第 {self.answer_attempts} 次），等待作答確認")
        glog(f"答案點擊 {q} -> {o['text']} @ {region['left'] + cx},{region['top'] + cy} 第{self.answer_attempts}次")
        return True

    def try_fallback(self, region, q, opts, scores, now, shot_at=None, gen=None):
        """同一題超過 FALLBACK_SECONDS 秒還沒點，就選分數最高的選項 (不作答也是錯，猜一個還有機會)。"""
        if not self.auto.get() or self.clicked_q == q or self.answer_attempts or now - self.cur_q_since < FALLBACK_SECONDS:
            return False
        if now < self.end_screen_until or not is_quiz_word(q):
            return False
        texts = [_norm(op["text"]) for op in opts]
        cand = [i for i, t in enumerate(texts) if not any(w in t for w in NOT_OPTION)]
        if not 2 <= len(cand) <= 6:
            return False
        i = max(cand, key=scores.__getitem__)
        if not self.click_answer(region, q, opts, i, now, shot_at, gen, guess=True):
            return False
        self.ans.config(text=f"猜：{opts[i]['text']}")
        self.game_lbl.config(text=f"⏱ {FALLBACK_SECONDS:.0f} 秒沒把握，猜 {opts[i]['text']} ({scores[i]:.1f})")
        return True

    def run(self):
        self.root.mainloop()
        self.running = False
        self.gen += 1
        time.sleep(0.3)
        for _ in range(3):
            save_known(force=True)
            _save_cache(force=True)
            if not any(st["dirty"] for st in _save_state.values()):
                break
            time.sleep(0.5)


if __name__ == "__main__":
    # only one copy at a time: two copies fight over the mouse and overwrite each other's memory file
    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _k32.CreateMutexW.restype = ctypes.c_void_p
    _single = _k32.CreateMutexW(None, False, "Local\\vocab_helper_single_instance")
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        ctypes.windll.user32.MessageBoxW(None, "單字小幫手已經在執行了，不用再開一個。", "單字小幫手", 0x40)
        sys.exit(0)
    App().run()
