# -*- coding: utf-8 -*-
"""
AI 自动粗剪决策引擎

输入分析结果（镜头切点 / 静音 / 重复 / 静止 / 空白），
输出「要剪掉哪些区间、每处为什么剪」，全程不需要人介入。

档位决定剪多狠：
- conservative 保守：只剪明显的废话和空场，镜头基本不动
- standard     标准：常规口播/记录片的粗剪节奏
- tight        紧凑：短视频节奏，几乎不留气口
"""

STRENGTHS = {
    "conservative": {
        "label": "保守",
        "desc": "只剪明显多余的部分，气口留得足",
        "silence_min": 1.3,    # 超过这么长的静音才处理
        "silence_pad": 0.40,   # 静音两端各留一点，避免剪断呼吸
        "sentence_pad": 0.35,  # 有词级时间戳时：句号处只留这么多（没词级时仍用 silence_pad）
        "breath": 0.90,        # 有词级时间戳时：句中卡壳留这么长的换气口
        "static_min": 6.0,     # 静止画面超过这么久才压
        "static_keep": 4.0,    # 静止画面保留前面这么多秒
        "blank_min": 0.35,
        "min_cut": 0.25,       # 太短的碎剪不要
        "join_gap": 0.70,      # 两个剪点之间不足这么久就合并，别留碎渣
    },
    "standard": {
        "label": "标准",
        "desc": "常规粗剪，节奏自然",
        "silence_min": 0.85,
        "silence_pad": 0.25,
        "sentence_pad": 0.20,
        "breath": 0.60,
        "static_min": 4.0,
        "static_keep": 2.5,
        "blank_min": 0.25,
        "min_cut": 0.20,
        "join_gap": 0.55,
    },
    "tight": {
        "label": "紧凑",
        "desc": "短视频节奏，几乎不留气口",
        "silence_min": 0.55,
        "silence_pad": 0.12,
        "sentence_pad": 0.20,
        "breath": 0.55,
        "static_min": 2.5,
        "static_keep": 1.5,
        "blank_min": 0.18,
        "min_cut": 0.15,
        "join_gap": 0.40,
    },
}

DEFAULT_STRENGTH = "standard"

# 剪点离词的边界至少留这么多，避免把半个字切掉（口播对「吞字」特别敏感）
WORD_GUARD = 0.04


def _words_around(words, s, e):
    """找出这段静音前后的两个词 → (prev, next)，缺一个是 None

    words 是按时间排好的扁平词表（speech_cut.flatten_words 的输出）。
    不看静音的边界落在词里还是词外：prev 取「最后一个起点在静音前的词」，
    next 取「第一个起点在静音后的词」，这样剪点才能被词边界兜住——
    检测出来的静音边界和转写出来的词边界本来就会差几十毫秒。
    """
    prev_w = nxt_w = None
    for w in words:
        try:
            ws = float(w["start"])
        except (KeyError, TypeError, ValueError):
            continue
        if ws < s:
            prev_w = w
        else:
            nxt_w = w
            break
    return prev_w, nxt_w



def _merge(ranges, join_gap=0.0):
    """合并重叠 / 挨得很近的区间"""
    if not ranges:
        return []
    rs = sorted((float(s), float(e)) for s, e in ranges if e > s)
    out = [list(rs[0])]
    for s, e in rs[1:]:
        if s <= out[-1][1] + join_gap:
            out[-1][1] = max(out[-1][1], e)
        else:
            out.append([s, e])
    return [(round(s, 2), round(e, 2)) for s, e in out]


def build_keep_ranges(duration, cut_ranges, min_keep=0.15):
    """从总时长里挖掉 cut_ranges，返回保留区间"""
    if duration <= 0:
        return []
    cuts = _merge([(max(0.0, s), min(duration, e)) for s, e in cut_ranges])
    keep = []
    cursor = 0.0
    for s, e in cuts:
        if s - cursor > min_keep:
            keep.append((round(cursor, 2), round(s, 2)))
        cursor = max(cursor, e)
    if duration - cursor > min_keep:
        keep.append((round(cursor, 2), round(duration, 2)))
    return keep


def decide(analysis, strength=DEFAULT_STRENGTH, params=None):
    """把分析结果转成剪辑决策

    analysis: {
        duration, scenes[], silent[], redundant[], static[], blank[],
        has_audio
    }
    params: 自定义参数（不传就用 strength 对应的三档预设）。
            「按需求剪」为了凑目标时长会逐级收紧参数，走这里覆盖。
    """
    p = params or STRENGTHS.get(strength) or STRENGTHS[DEFAULT_STRENGTH]
    label = p["label"]
    dur = float(analysis.get("duration") or 0)
    decisions = []

    if dur <= 0:
        return {"cut_ranges": [], "decisions": [], "before": 0, "after": 0,
                "saved": 0, "strength": strength, "strength_label": label,
                "warning": "视频时长异常，无法决策"}

    def add(kind, start, end, reason, min_len=None):
        start = max(0.0, min(dur, float(start)))
        end = max(0.0, min(dur, float(end)))
        # 语气词天然很短（0.2～0.5s），用常规 min_cut 会被全部滤掉，单独给下限
        if end - start < (p["min_cut"] if min_len is None else min_len):
            return
        decisions.append({
            "type": kind,
            "start": round(start, 2),
            "end": round(end, 2),
            "saved": round(end - start, 2),
            "reason": reason,
        })

    # ---- 1. 静音：两端各留 pad，只剪中间 ----
    #      有词级时间戳时按「这句说完了还是说到一半卡住了」分两种留法：
    #      句界只留 0.2s（两句之间本来就该紧），句中卡壳留 0.6s 换气；
    #      剪点一律避让词边界，宁可少剪也不切半个字。
    #      没有词级时间戳（没勾口头禅/字幕）就走原来的固定 pad，一个数都不变。
    words = (analysis.get("speech") or {}).get("words") or []
    if analysis.get("has_audio", True):
        for seg in analysis.get("silent") or []:
            s, e = float(seg["start"]), float(seg["end"])
            length = e - s
            if length < p["silence_min"]:
                continue
            # 开头的静音只留尾部 pad；结尾的静音只留头部 pad
            at_head = s <= 0.05
            at_tail = e >= dur - 0.05
            s2 = s if at_head else s + p["silence_pad"]
            e2 = e if at_tail else e - p["silence_pad"]
            why = ""
            if words:
                prev_w, next_w = _words_around(words, s, e)
                if at_head:
                    # 开头：只留尾部 pad，但后一个词不能被切到
                    if next_w is not None:
                        e2 = min(e2, float(next_w["start"]) - WORD_GUARD)
                elif at_tail:
                    # 结尾：只留头部 pad，前一个词也不能被切到
                    if prev_w is not None:
                        s2 = max(s2, float(prev_w["end"]) + WORD_GUARD)
                elif prev_w is not None and next_w is not None:
                    if prev_w.get("term"):
                        # 上一句说完了：句界只留一个短气口
                        pad = float(p.get("sentence_pad", 0.20))
                        s2, e2 = s + pad, e - pad
                        why = f"句界只留 {pad:.2f}s 气口"
                    else:
                        # 话没说完就停住：留够换气，别把人剪得喘不上气
                        breath = float(p.get("breath", 0.60))
                        head = min(0.25, breath * 0.4)
                        if length <= breath + p["min_cut"]:
                            continue
                        s2, e2 = s + head, e - (breath - head)
                        why = f"句中卡壳留 {breath:.2f}s 换气"
                    # 剪点避让词边界
                    s2 = max(s2, float(prev_w["end"]) + WORD_GUARD)
                    e2 = min(e2, float(next_w["start"]) - WORD_GUARD)
            if e2 - s2 < p["min_cut"]:
                continue
            where = "开头" if at_head else ("结尾" if at_tail else "中间")
            if why:
                reason = f"{where}空场 {length:.1f}s，{why}，剪掉其中 {e2 - s2:.1f}s"
            else:
                reason = (f"{where}空场 {length:.1f}s，剪掉其中 {e2 - s2:.1f}s，"
                          f"两端各留 {p['silence_pad']}s 气口")
            add("silence", s2, e2, reason)

    # ---- 2. 重复内容：整段剪 ----
    for seg in analysis.get("redundant") or []:
        add("dup", seg["start"], seg["end"],
            f"与 {float(seg.get('similar_to') or 0):.1f}s 处画面重复"
            f"（相似度 {float(seg.get('similarity') or 0) * 100:.0f}%），属于重录或重复镜头")

    # ---- 3. 黑场 / 白场 / 纯色 ----
    for seg in analysis.get("blank") or []:
        s, e = float(seg["start"]), float(seg["end"])
        length = e - s
        mean = float(seg.get("mean", 0))
        is_bw = mean < 28 or mean > 232     # 纯黑 / 纯白：多长都是废画面
        if not is_bw and length > 3.0:
            # 长时长的彩色纯色往往是作者有意的画面（纯色背景、示意板），别乱剪
            continue
        if length < p["blank_min"]:
            continue
        kind = "黑场" if mean < 28 else ("白场" if mean > 232 else "纯色画面")
        add("blank", s, e, f"{kind} {length:.1f}s，没有信息量")

    # ---- 4. 静止画面：只有「画面不动 + 没声音」才算死时间 ----
    #     固定机位口播、PPT 讲解时画面也是静止的，但有人在说话，不能剪
    static_all = analysis.get("static") or []
    static_total = sum(float(x["end"]) - float(x["start"]) for x in static_all)
    if dur > 0 and static_total / dur > 0.55:
        # 整片都不动 = 静态素材（图片视频 / 长截图），静止不是缺陷
        static_all = []

    if static_all:
        if analysis.get("has_audio", True):
            silences = _merge([(float(x["start"]), float(x["end"]))
                               for x in (analysis.get("silent") or [])])
            dead = []
            for st in static_all:
                for ss, se in silences:
                    a = max(float(st["start"]), ss)
                    b = min(float(st["end"]), se)
                    if b - a >= p["static_min"]:
                        dead.append((a, b))
        else:
            dead = [(float(x["start"]), float(x["end"])) for x in static_all
                    if float(x["end"]) - float(x["start"]) >= p["static_min"]]

        for a, b in dead:
            add("static", a + p["static_keep"], b,
                f"画面静止且无声 {b - a:.1f}s，保留前 {p['static_keep']}s，压掉后 {b - a - p['static_keep']:.1f}s")

    # ---- 5. 语音级：口头禅 / 重说 ----
    #      和静音不同，这里剪的是「有声但没信息」的部分，
    #      区间是 speech_cut 按词边界算好的，直接采信，不再留气口。
    speech = analysis.get("speech") or {}
    for seg in speech.get("filler") or []:
        add("filler", seg["start"], seg["end"],
            seg.get("reason") or "语气词，剪掉不影响意思", min_len=0.06)
    for seg in speech.get("retake") or []:
        add("retake", seg["start"], seg["end"],
            seg.get("reason") or "重复说了一遍，保留后一遍", min_len=0.12)

    # ---- 合并 & 统计 ----
    raw = [(d["start"], d["end"]) for d in decisions]
    cut_ranges = _merge(raw, join_gap=p["join_gap"])

    # 合并后重算每条的「被吞并」情况，用于展示
    total_cut = sum(e - s for s, e in cut_ranges)
    keep = build_keep_ranges(dur, cut_ranges, min_keep=0.30)
    kept = round(sum(e - s for s, e in keep), 2)

    # 剪得太狠时给个提醒（比如参数误设成紧凑 + 视频本身全是静音）
    warning = ""
    if not keep:
        warning = "全部内容都会被剪掉，请降低强度后重试"
    elif total_cut / dur > 0.7:
        warning = f"这次剪掉了 {total_cut / dur * 100:.0f}%，量比较大，建议先看成片再决定"

    # 按时间排序，方便前端逐条展示
    decisions.sort(key=lambda d: d["start"])

    return {
        "cut_ranges": [[round(s, 2), round(e, 2)] for s, e in cut_ranges],
        "decisions": decisions,
        "keep_ranges": [[s, e] for s, e in keep],
        "before": round(dur, 2),
        "after": kept,
        "saved": round(total_cut, 2),
        "strength": strength,
        "strength_label": label,
        "warning": warning,
        "speech": {
            "filler": len(speech.get("filler") or []),
            "retake": len(speech.get("retake") or []),
            "note": speech.get("note") or "",
            "word_level": bool(speech.get("word_level")),
        },
    }
