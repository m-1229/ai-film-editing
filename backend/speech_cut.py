# -*- coding: utf-8 -*-
"""语音级剪辑 —— 从「说了什么」里找出该剪掉的部分

之前的剪辑只能剪「没声音」（静音）和「画面没变化」，剪不了「有声音但在说废话」。
转写已经能用了（字幕在用），这个模块把转写结果变成剪辑依据。

两件事：
1) 语气词：嗯、啊、呃、那个、就是……孤立出现时剪掉不影响意思
2) 重说：同一句说了两遍、或一句话里词组重复，保留后一遍

两条硬约束：
- **剪点必须落在词边界上**。切掉「嗯」后面的半个字，听感比不剪更糟。
  所以没有词级时间戳时，语气词一律不剪，只做整段的跨句重说。
- **不能剪掉正常说话**。「然后我们开始」里的「然后」是连接词，不是废话；
  只有前后都有停顿（孤立成句）或者连着说了两遍，才算口头禅。

强度分档（和 autocut 的三档一致）：保守只剪最确定的单字语气词，
标准加上孤立的词组，紧凑再把一侧停顿的也算进来。
"""

from difflib import SequenceMatcher

# ---------------- 词表 ----------------

# 单字语气词：只要一侧有明显停顿就可以剪
SOLO_FILLERS = {
    "嗯", "恩", "呃", "额", "唉", "诶", "欸", "唔", "呃",
}

# 句尾也可能是它们的语气词：两侧都要停顿才剪，免得把「好啊」剪成「好」
BOTH_FILLERS = {"啊", "哦", "喔"}

# 词组口头禅：本身有意义，只有「孤立成一句」或「连说两遍」才剪
WEAK_FILLERS = {
    "那个", "这个", "就是", "然后", "所以说", "怎么说呢", "对吧",
    "你知道吗", "那什么", "这样子", "就是呢", "然后呢", "就是说",
    "这一块", "那个啥",
}

_TIER = {}
for _w in SOLO_FILLERS:
    _TIER[_w] = "solo"
for _w in BOTH_FILLERS:
    _TIER[_w] = "both"
for _w in WEAK_FILLERS:
    _TIER[_w] = "weak"

# 一个词组最多由几个转写词拼起来（whisper 有时把「怎么说呢」切成两三个词）
MAX_PHRASE_WORDS = 3
MAX_PHRASE_CHARS = 5

# 三档：数值是「多大的停顿才算孤立」
LEVELS = {
    "conservative": {"solo": 0.25, "both": 0.30, "weak_one": None, "weak_both": None},
    "standard": {"solo": 0.10, "both": 0.15, "weak_one": None, "weak_both": 0.35},
    "tight": {"solo": 0.06, "both": 0.10, "weak_one": 0.45, "weak_both": 0.22},
}

# 剪掉的总量超过全片这个比例，就不动（多半是识别出了问题）
MAX_RATIO = 0.35

_PUNCT = set("，。！？；、,.!?;：:…—-—～~\"'“”‘’()（）《》【】[] \t\r\n")

# 句末标点：用来判断一个停顿是「说完了这句」还是「说到一半卡住了」。
# 粗剪按这两类留不同的气口（句界 0.2s、句中 0.6s），所以要单独记下来——
# _core() 会把标点删掉，删完就分不出来了。
_TERM = ("。", "！", "？", "!", "?", "…")

# 序数词：出现这些差异就不是重说，是在一条一条讲
_ORDINAL = set("一二三四五六七八九十0123456789两")


def _core(t):
    """去掉标点空白，只留能比对的字"""
    return "".join(ch for ch in (t or "") if ch not in _PUNCT)


def _ends_sentence(text):
    """这句是不是以句末标点收尾（=说话人把这句话说完了）"""
    t = (text or "").rstrip()
    return bool(t) and t.endswith(_TERM)


# ---------------- 摊平成词序列 ----------------

def flatten_words(segments):
    """把各段的词摊成一条带时间戳的序列

    没有词级时间戳的段，整段当一个「词」并标 whole=True —
    这种块内部无法定位，语气词检测会跳过它。

    每个词多带一个 term：这个字/词后面是不是句末标点。
    语气词检测不用它，粗剪判断「句界还是卡壳」要用。
    """
    out = []
    for si, seg in enumerate(segments or []):
        got = False
        first = len(out)
        for w in seg.get("words") or []:
            raw = w.get("text") or ""
            t = _core(raw)
            try:
                s, e = float(w["start"]), float(w["end"])
            except (KeyError, TypeError, ValueError):
                continue
            if not t or e <= s:
                continue
            out.append({"start": s, "end": e, "text": t, "seg": si,
                        "term": _ends_sentence(raw)})
            got = True
        if got:
            # 句末标点常常被 whisper 挂在整段文字尾部（甚至单独成一个词），
            # 单个词上看不出来。整段以句末标点收尾时，把最后一个词标成句界。
            if _ends_sentence(seg.get("text")) and len(out) > first:
                out[-1]["term"] = True
            continue
        raw = seg.get("text") or ""
        t = _core(raw)
        try:
            s, e = float(seg["start"]), float(seg["end"])
        except (KeyError, TypeError, ValueError):
            continue
        if t and e > s:
            out.append({"start": s, "end": e, "text": t, "seg": si,
                        "whole": True, "term": _ends_sentence(raw)})
    return out


def has_word_level(segments):
    return any(s.get("words") for s in (segments or []))


def _gap_before(words, i):
    if i <= 0:
        return 99.0
    return max(0.0, words[i]["start"] - words[i - 1]["end"])


def _gap_after(words, j):
    if j + 1 >= len(words):
        return 99.0
    return max(0.0, words[j + 1]["start"] - words[j]["end"])


# ---------------- 语气词 ----------------

def match_filler(words, i):
    """从第 i 个词起能匹配到的最长语气词 → (末尾下标, 档位, 原文)"""
    buf, best = "", None
    for k in range(MAX_PHRASE_WORDS):
        j = i + k
        if j >= len(words) or words[j].get("whole"):
            break
        buf += words[j]["text"]
        if buf in _TIER:
            best = (j, _TIER[buf], buf)
        if len(buf) >= MAX_PHRASE_CHARS:
            break
    return best


def detect_fillers(segments, strength="standard"):
    """语气词 → 可剪区间（带原因）"""
    words = flatten_words(segments)
    if not words:
        return []
    lv = LEVELS.get(strength) or LEVELS["standard"]
    spans = []
    i = 0
    while i < len(words):
        hit = match_filler(words, i)
        if not hit:
            i += 1
            continue

        j, tier, text = hit
        # 同一个词连着说两遍（那个那个 / 嗯嗯）—短促重复一定是废话
        run_end, repeats = j, 1
        while run_end + 1 < len(words):
            nxt = match_filler(words, run_end + 1)
            if nxt and nxt[2] == text:
                run_end, repeats = nxt[0], repeats + 1
            else:
                break

        gb = _gap_before(words, i)
        ga = _gap_after(words, run_end)

        if repeats >= 2:
            ok, why = True, f"「{text}」连着说了 {repeats} 遍，留一遍就够"
        elif tier == "solo":
            ok = max(gb, ga) >= lv["solo"]
            why = f"语气词「{text}」，前后有停顿，剪掉不影响意思"
        elif tier == "both":
            ok = min(gb, ga) >= lv["both"]
            why = f"语气词「{text}」，这里是单独一句，剪掉不影响意思"
        else:  # weak
            ok = False
            why = f"口头禅「{text}」，这里单独说了一下"
            if lv["weak_both"] is not None and min(gb, ga) >= lv["weak_both"]:
                ok = True
            elif (lv["weak_one"] is not None and max(gb, ga) >= lv["weak_one"]
                  and min(gb, ga) >= 0.08):
                ok = True

        if ok:
            # 往两侧的停顿里各探一点点，但绝不吃进相邻的词
            start = words[i]["start"] - min(0.06, gb * 0.35)
            end = words[run_end]["end"] + min(0.06, ga * 0.35)
            spans.append({"start": round(max(0.0, start), 3),
                          "end": round(end, 3),
                          "reason": why,
                          "text": text,
                          "kind": "filler"})
        i = run_end + 1

    return merge_spans(spans)


# ---------------- 重说 ----------------

# _TERM / _ORDINAL 定义在文件顶部（粗剪判断句界也要用）


def _diff_chars(ta, tb):
    """两句里各自多出来的字"""
    return set(ta) ^ set(tb)


def _retake_reason(ta, tb, raw_a=""):
    """前一句是不是「白说的」—— 后一句更完整/几乎一样才算

    最容易误判的是「我们讲第一点 / 我们讲第二点」：字数一样、只差一个字，
    相似度 85% 以上，但这是两条并列内容，剪掉前一条就丢内容了。
    所以：差异里出现序数词/数字一律不算重说。
    """
    if ta == tb:
        return "这句原话说了两遍，保留后一遍"

    complete = (raw_a or "").strip().endswith(_TERM)
    # 前一句是后一句的开头（说了一半停下重来）—— 前一句必须没说完
    if (not complete and len(ta) >= 4 and ta in tb
            and 0 < len(tb) - len(ta) <= 12):
        return "后面把这句话补完整了，前一遍只起了个头"

    if len(ta) < 6 or len(tb) < 6 or abs(len(ta) - len(tb)) > 4:
        return ""
    if _diff_chars(ta, tb) & _ORDINAL:
        return ""
    # 前一句说完整了（有句末标点）就不是重说，是正常的下一句
    if complete:
        return ""

    r = SequenceMatcher(None, ta, tb).ratio()
    if r >= 0.85:
        return f"前后两句几乎一样（{r * 100:.0f}%），前一遍没说完，保留后一遍"
    return ""


def detect_retakes(segments):
    """重说 → 可剪区间（带原因）

    两种：
    - 跨句：连着两段说的话几乎一样（口播里「重来一遍」最典型）
    - 句内：一句话里同一个词组说了两遍（「我们这个我们这个产品」）
    """
    segs = [s for s in (segments or []) if _core(s.get("text"))]
    spans = []

    for i in range(len(segs) - 1):
        a, b = segs[i], segs[i + 1]
        ta, tb = _core(a["text"]), _core(b["text"])
        if len(ta) < 4 or len(tb) < 4:
            continue
        try:
            gap = float(b["start"]) - float(a["end"])
        except (KeyError, TypeError, ValueError):
            continue
        if gap > 1.6:          # 停太久就是在说下一句，不是重说
            continue
        why = _retake_reason(ta, tb, a.get("text"))
        if why:
            spans.append({"start": round(float(a["start"]), 3),
                          "end": round(float(a["end"]), 3),
                          "reason": why, "kind": "retake"})

    for seg in segs:
        hit = _repeat_inside(seg)
        if hit:
            spans.append(hit)

    return merge_spans(spans)


def _repeat_inside(seg):
    """一句话里重复的词组，剪前一次"""
    words = flatten_words([seg])
    if len(words) < 4 or any(w.get("whole") for w in words):
        return None
    texts = [w["text"] for w in words]

    # 词组越长越可信；上限按句长一半取，否则「我们今天讲这个」只会认出前 6 个字
    for size in range(min(9, len(texts) // 2), 1, -1):
        for i in range(0, len(texts) - size * 2 + 1):
            for j in range(i + size, min(len(texts) - size + 1, i + size + 9)):
                if texts[i:i + size] != texts[j:j + size]:
                    continue
                chars = "".join(texts[i:i + size])
                if len(chars) < 3:
                    continue
                start, end = words[i]["start"], words[j]["start"]
                if end - start < 0.15:
                    continue
                return {"start": round(start, 3), "end": round(end, 3),
                        "reason": f"「{chars}」在这句里说了两遍，保留后一遍",
                        "kind": "retake"}
    return None


# ---------------- 合并 / 入口 ----------------

def merge_spans(spans):
    """重叠或几乎挨着的区间合起来（「嗯，啊」这种连着的语气词）"""
    if not spans:
        return []
    rs = sorted(spans, key=lambda x: (x["start"], x["end"]))
    out = [dict(rs[0])]
    for s in rs[1:]:
        prev = out[-1]
        if s["start"] <= prev["end"] + 0.12:
            if s["end"] > prev["end"]:
                prev["end"] = s["end"]
        else:
            out.append(dict(s))
    for x in out:
        x["end"] = round(x["end"], 3)
    return out


def analyze(segments, strength="standard", kinds=("filler", "retake"), duration=0.0):
    """转写结果 → 建议剪掉的语音区间

    返回 {"filler": [...], "retake": [...], "word_level": bool, "note": str}
    """
    kinds = tuple(kinds or ())
    word_level = has_word_level(segments)
    res = {"filler": [], "retake": [], "word_level": word_level, "note": ""}

    if "filler" in kinds:
        if word_level:
            res["filler"] = detect_fillers(segments, strength)
        else:
            res["note"] = ("这次没拿到词级时间戳，语气词没敢剪（会切断半个字），"
                           "只做了整句重说检测")
    if "retake" in kinds:
        res["retake"] = detect_retakes(segments)

    allsp = res["filler"] + res["retake"]
    dur = float(duration or 0)
    total = sum(x["end"] - x["start"] for x in allsp)
    if dur > 0 and total > dur * MAX_RATIO:
        # 剪掉三成以上要么是识别崩了，要么是这人真的每句都有口头禅——先拦住
        res["note"] = (f"口头禅/重说加起来有 {total:.0f}s，超过全片 {MAX_RATIO * 100:.0f}%，"
                       "这次没剪，建议先看看转写对不对")
        res["filler"], res["retake"] = [], []

    res["saved"] = round(total, 2)
    return res


def summarize(speech):
    """给回执用的一句话"""
    if not speech:
        return ""
    nf = len(speech.get("filler") or [])
    nr = len(speech.get("retake") or [])
    if not nf and not nr:
        return ""
    bits = []
    if nf:
        bits.append(f"{nf} 处口头禅")
    if nr:
        bits.append(f"{nr} 处重说")
    return "顺手剪掉了" + "、".join(bits)
