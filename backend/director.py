# -*- coding: utf-8 -*-
"""
需求导演（Director）—— 把一句人话翻译成剪辑方案

职责拆成两半：
1) parse_demand(text)   自然语言 → 结构化意图
   - 内置中文规则解析（离线、零成本、行为可预测）
   - 配了大模型就走大模型，失败自动退回规则
2) build_plan(analysis, intent)  意图 + 画面数据 → 剪辑方案
   - 复用 autocut.decide 的三档参数
   - 叠加「目标时长」「按类型开关」「精确时间区间」
   - 卡点混剪走独立的排镜逻辑（backend/beat.py 提供节拍）

意图结构（也是大模型要返回的 JSON）：
{
  "style": "auto|talking_head|vlog|beat|highlight|tutorial",
  "strength": "conservative|standard|tight" | null,
  "target": 60.0 | null,           # 目标时长（秒）
  "allow": ["silence","dup","static","blank"],   # 允许剪掉哪些类型
  "cuts": [{"start":1.0,"end":3.0,"reason":"..."}],   # 明确要剪掉
  "keep": [{"start":10.0,"end":40.0}],                # 明确只保留
  "shot_beats": 2 | null,          # 卡点：每个镜头几拍
  "shot_len": 2.5 | null,          # 卡点：每个镜头几秒
  "notes": ["..."],                # 做不到的诉求，必须如实告知
}
"""

import json
import os
import re
from pathlib import Path

from backend.autocut import DEFAULT_STRENGTH, STRENGTHS, build_keep_ranges, decide, _merge

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "config.json"

# ---------------- 剪辑风格 ----------------

STYLES = {
    "auto": {
        "label": "智能",
        "desc": "按画面和声音自己判断",
        "strength_bias": None,
        "allow": None,
    },
    "talking_head": {
        "label": "口播精简",
        "desc": "剪掉停顿、空场、重录，留下干货",
        "strength_bias": "standard",
        "allow": {"silence", "static", "blank", "dup"},
    },
    "vlog": {
        "label": "Vlog 记录",
        "desc": "保留氛围和呼吸，只剪明显空场",
        "strength_bias": "conservative",
        "allow": {"silence", "blank", "dup"},
    },
    "highlight": {
        "label": "高光快剪",
        "desc": "短镜头快切，只留精华",
        "strength_bias": "tight",
        "allow": {"silence", "static", "blank", "dup"},
    },
    "tutorial": {
        "label": "教程讲解",
        "desc": "保留每个步骤，剪掉重复演示",
        "strength_bias": "standard",
        "allow": {"silence", "blank", "dup"},
    },
    "beat": {
        "label": "卡点混剪",
        "desc": "跟着音乐节拍换镜头",
        "strength_bias": "tight",
        "allow": set(),
    },
}

STYLE_WORDS = [
    ("beat", ("卡点", "踩点", "卡节奏", "卡音乐", "卡着", "跟着音乐", "混剪", "节奏剪辑", "燃向")),
    ("highlight", ("高光", "集锦", "精华", "精彩片段", "快剪", "快切", "短视频")),
    ("tutorial", ("教程", "教学", "课程", "操作演示", "步骤演示")),
    ("talking_head", ("口播", "口说", "讲解", "播报", "干货", "知识", "讲书", "直播回放", "对镜")),
    ("vlog", ("vlog", "旅行", "日常", "记录", "生活", "游记", "流水账")),
]

TIGHT_WORDS = ("紧凑", "快节奏", "节奏快", "快一点", "快点", "干脆", "利落", "别拖",
               "短平快", "精简", "狠一点", "剪狠", "紧一点", "掐掉")
CONSERVATIVE_WORDS = ("保守", "别太狠", "别剪太多", "少剪", "轻一点", "留一点", "宽松",
                      "慢一点", "自然一点", "别动太多", "温柔")

TYPE_WORDS = {
    "silence": ("静音", "停顿", "空白", "空场", "沉默", "没声音", "不说话"),
    "static": ("静止", "画面不动", "死画面", "定机位", "不动的地方"),
    "dup": ("重复", "重录", "口误重来", "重说", "同一段", "说了两遍"),
    "blank": ("黑场", "黑屏", "白场", "纯色", "黑帧", "无信息"),
}
TYPE_LABEL = {"silence": "静音/停顿", "static": "静止画面", "dup": "重复内容", "blank": "黑场/纯色"}

EXCLUDE_WORDS = ("不要剪", "别剪", "不剪", "不用剪", "别动", "不用管", "不要动",
                 "忽略", "保留", "留着", "留下来", "别删", "别处理")
ONLY_WORDS = ("只剪", "只看", "仅剪", "只处理", "只去掉")

NEG_WORDS = ("不要", "别要", "去掉", "剪掉", "删掉", "砍掉", "剪去", "删了", "不要了", "减掉")
KEEP_WORDS = ("保留", "留着", "留下", "只要", "剩下", "留出")
HEAD_WORDS = ("开头", "片头", "前面", "开头前")
TAIL_WORDS = ("结尾", "最后", "末尾", "片尾", "后面", "尾段")
TARGET_WORDS = ("控制", "剪成", "剪到", "压到", "缩短", "目标", "时长", "总共", "一共",
                "不超过", "以内", "之内", "左右", "只要", "留", "成片")

UNSUPPORTED = {
    "配音": "AI 配音（本工具只剪辑现有素材）",
    "滤镜": "滤镜调色",
    "调色": "滤镜调色",
    "调色板": "滤镜调色",
    "竖屏": "改画幅/裁竖屏",
    "横屏": "改画幅/裁横屏",
    "水印": "去水印",
    "封面": "生成封面",
    "降噪": "音频降噪",
    "美颜": "美颜磨皮",
    "标题": "标题条/贴图",
    "贴图": "贴图/贴纸",
}

_CN_DIGIT = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
             "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_CN_CHARS = "".join(_CN_DIGIT) + "十百半"

_DUR_RE = re.compile(
    rf"(?P<num>\d+(?:\.\d+)?|[{_CN_CHARS}]+)\s*(?P<half>个?半)?\s*"
    rf"(?P<unit>小时|钟头|分钟|分|秒钟|秒|sec|s)",
    re.IGNORECASE,
)


# ---------------- 基础工具 ----------------

def _cn2num(s):
    s = (s or "").strip()
    if not s:
        return 0.0
    try:
        return float(s)
    except ValueError:
        pass
    if s == "半":
        return 0.5
    total = 0.0
    if "十" in s:
        left, _, right = s.partition("十")
        total = (_CN_DIGIT.get(left, 1) if left else 1) * 10
        total += _CN_DIGIT.get(right, 0) if right else 0
        if right == "半":
            total += 0.5
        return float(total)
    for ch in s:
        if ch == "半":
            total += 0.5
        else:
            total += _CN_DIGIT.get(ch, 0)
    return float(total)


def _find_durations(text):
    """找出文本里所有「时长」提法，返回 [{start,end,sec}]（按位置排序、相邻合并）"""
    items = []

    for m in re.finditer(r"(\d{1,2})\s*[:：]\s*(\d{1,2})", text):
        items.append({
            "start": m.start(), "end": m.end(),
            "sec": int(m.group(1)) * 60 + int(m.group(2)),
        })

    for m in _DUR_RE.finditer(text):
        if any(o["start"] <= m.start() < o["end"] for o in items):
            continue
        unit = m.group("unit").lower()
        mul = 3600 if unit in ("小时", "钟头") else 60 if unit in ("分钟", "分") else 1
        sec = _cn2num(m.group("num")) * mul
        end = m.end()
        if m.group("half"):
            sec += mul / 2.0
        # 「1分半」「两分半钟」：半 落在单位后面
        if not m.group("half") and text[end:end + 1] == "半" and mul >= 60:
            sec += mul / 2.0
            end += 1
        items.append({"start": m.start(), "end": end, "sec": round(sec, 2),
                      "unit": unit})

    items.sort(key=lambda x: x["start"])

    # 「1分30秒」这种连写合成一个时长
    merged = []
    for it in items:
        if merged:
            prev = merged[-1]
            if prev["end"] == it["start"] and prev.get("unit") in ("分钟", "分", "小时", "钟头"):
                prev["sec"] = round(prev["sec"] + it["sec"], 2)
                prev["end"] = it["end"]
                prev["unit"] = it.get("unit")
                continue
        merged.append(dict(it))
    return merged


_RANGE_SEP = re.compile(r"^\s*(?:到|至|-|—|–|~|〜|－)\s*$")


def _window(text, start, end, n=10):
    return text[max(0, start - n):start], text[end:end + n]


def _has(words, s):
    return any(w in s for w in words)


def _classify_times(text, dur):
    """把时长提法归类成 target / cuts / keeps"""
    items = _find_durations(text)
    cuts, keeps = [], []
    used = set()

    # 1) 区间：10秒到40秒 / 从1:20到2:00
    for i in range(len(items) - 1):
        a, b = items[i], items[i + 1]
        if _RANGE_SEP.match(text[a["end"]:b["start"]]):
            pre = text[max(0, a["start"] - 6):a["start"]]
            post = text[b["end"]:b["end"] + 8]
            if _has(NEG_WORDS, post) or _has(("剪掉", "去掉", "不要"), pre):
                cuts.append({"start": a["sec"], "end": b["sec"],
                             "reason": "按你的要求剪掉这段"})
            else:
                keeps.append({"start": a["sec"], "end": b["sec"]})
            used.add(i)
            used.add(i + 1)

    strong_target, weak_target = None, None
    type_words_flat = tuple(w for ws in TYPE_WORDS.values() for w in ws)

    for i, d in enumerate(items):
        if i in used:
            continue
        pre = text[max(0, d["start"] - 8):d["start"]]
        post = text[d["end"]:d["end"] + 6]
        neg = _has(NEG_WORDS, post) or _has(NEG_WORDS, pre)
        keep = _has(KEEP_WORDS, post) or _has(KEEP_WORDS, pre)
        head = _has(HEAD_WORDS, pre)
        tail = _has(TAIL_WORDS, pre)

        if (head or tail) and (neg or keep):
            if head:
                if keep and not neg:
                    keeps.append({"start": 0.0, "end": d["sec"]})
                else:
                    cuts.append({"start": 0.0, "end": d["sec"],
                                 "reason": f"按你的要求，开头 {d['sec']:.1f}s 不要"})
            else:
                # 结尾的位置依赖视频总时长，放到 build_plan 里再算
                if keep and not neg:
                    keeps.append({"anchor": "tail", "len": d["sec"]})
                else:
                    cuts.append({"anchor": "tail", "len": d["sec"],
                                 "reason": f"按你的要求，结尾 {d['sec']:.1f}s 不要"})
            used.add(i)
            continue

        if neg:
            continue

        # 「静音超过 1.5 秒的都剪」——这是判定阈值，不是目标时长
        if _has(type_words_flat, pre):
            continue

        if _has(TARGET_WORDS, pre) or _has(("以内", "之内", "左右", "别超过"), post):
            if strong_target is None:
                strong_target = float(d["sec"])
            used.add(i)
        elif re.match(r"^\s*(?:[，。；,;！!？?]|$)", post) and weak_target is None:
            # 「卡点混剪45秒」这种没有引导词、但独立成句的，当目标时长
            weak_target = float(d["sec"])
            used.add(i)

    return (strong_target if strong_target is not None else weak_target), cuts, keeps


def _parse_types(text):
    """类型开关：返回 (allow_set, touched)"""
    allow = {"silence", "static", "dup", "blank"}
    exclude, only = set(), set()

    for clause in re.split(r"[，。；,;！!？?\n]", text):
        if not clause.strip():
            continue
        hit = set()
        for t, words in TYPE_WORDS.items():
            if any(w in clause for w in words):
                hit.add(t)
        if not hit:
            continue
        if any(w in clause for w in EXCLUDE_WORDS):
            exclude |= hit
        elif any(w in clause for w in ONLY_WORDS):
            only |= hit

    if only:
        allow = set(only)
    allow -= exclude
    touched = bool(only or exclude)
    return (allow if touched else {"silence", "static", "dup", "blank"}), touched


def _parse_style(text):
    low = text.lower()
    for style, words in STYLE_WORDS:
        for w in words:
            if w.lower() in low:
                return style
    return "auto"


def _parse_strength(text):
    if any(w in text for w in TIGHT_WORDS):
        return "tight"
    if any(w in text for w in CONSERVATIVE_WORDS):
        return "conservative"
    return None


def _parse_beat(text):
    beats, length = None, None
    m = re.search(r"每\s*([0-9]+|[一二两三四五六七八九十]+)\s*拍", text)
    if m:
        v = _cn2num(m.group(1))
        if 1 <= v <= 16:
            beats = int(v)
    m2 = re.search(r"每(?:个)?镜头\s*(\d+(?:\.\d+)?)\s*秒", text)
    if m2:
        v = float(m2.group(1))
        if 0.3 <= v <= 20:
            length = v
    return beats, length


# ---------------- 新增能力：字幕 / BGM / 变速 / 转场 ----------------

# 字幕
SUB_ON = ("加字幕", "上字幕", "配字幕", "带字幕", "自动字幕", "生成字幕", "做字幕",
          "加个字幕", "上字幕", "字幕", "打轴")
SUB_OFF = ("不要字幕", "不加字幕", "别加字幕", "不用字幕", "去掉字幕", "无字幕",
           "别上字幕", "不要字")
SUB_BIG = ("大字", "字体大", "大一点", "大点", "醒目", "显眼")
SUB_SMALL = ("小字", "字体小", "小一点", "小点", "别太大", "不要太显眼")

# BGM
BGM_ON = ("换bgm", "加bgm", "配乐", "背景音乐", "加音乐", "铺音乐", "配个音乐",
          "换音乐", "加个音乐", "bgm", "配一首", "来点音乐", "垫个音乐", "换个背景音乐")
BGM_OFF = ("不要bgm", "不要音乐", "不加音乐", "去掉bgm", "不用音乐", "别加音乐",
           "不要背景音乐", "原声就行")
BGM_NO_ORIG = ("不要原声", "去掉原声", "只要音乐", "只要bgm", "原声不要",
               "去掉人声以外的", "只留音乐", "关掉原声")
BGM_QUIET = ("音乐小", "小声", "轻一点", "音乐轻", "bgm小", "别太吵", "垫一下")
BGM_LOUD = ("音乐大", "大声", "音乐响", "bgm大", "明显一点")

# 变速
SPEED_NUM = re.compile(r"(\d+(?:\.\d+)?|[一二两三四五六七八九十半]+)\s*(?:倍速|倍|×|x(?=\s|$|，|。))")
SLOW_WORDS = ("放慢", "慢放", "慢动作", "减速", "拖慢", "变慢", "调慢")
FAST_WORDS = ("加速", "快放", "加快", "提速", "变快", "快进", "调快")

# 转场
TRANS_ON = ("加转场", "转场", "淡入淡出", "叠化", "溶解", "黑场过渡", "白场过渡",
            "擦除", "滑动", "圆形展开", "像素化", "过渡效果", "加个过渡")
TRANS_OFF = ("不要转场", "不加转场", "别加转场", "不用转场", "硬切", "直接切")

TRANS_KIND = [
    ("black", ("黑场", "黑屏过渡")),
    ("white", ("白场", "闪白")),
    ("dissolve", ("溶解",)),
    ("wipe", ("擦除", "划像")),
    ("slide", ("滑动", "推移")),
    ("circle", ("圆形", "圆圈")),
    ("pixel", ("像素",)),
    ("smooth", ("平滑",)),
    ("fade", ("淡入淡出", "叠化", "渐变", "淡化")),
]
TRANS_KIND_MAP = {k: k for k, _ in TRANS_KIND}
TRANS_LABEL = {"fade": "叠化", "dissolve": "溶解", "black": "黑场过渡", "white": "白场过渡",
               "wipe": "擦除", "slide": "滑动", "circle": "圆形展开",
               "pixel": "像素化", "smooth": "平滑推移"}

COLOR_WORDS = [
    ("#FFD700", ("黄色", "金色", "黄字")),
    ("#FF5A5A", ("红色", "红字")),
    ("#6FB7FF", ("蓝色", "蓝字")),
    ("#7BE0A0", ("绿色", "绿字")),
    ("#111111", ("黑色", "黑字")),
    ("#FFFFFF", ("白色", "白字")),
]


def _parse_subtitle(text):
    """字幕：要不要 / 大小 / 位置 / 颜色"""
    low = text.lower()
    if not any(w in low for w in SUB_ON) and not any(w in low for w in SUB_OFF):
        return None

    on = not any(w in low for w in SUB_OFF)
    # 「不要字幕」也含「字幕」，必须先判 off
    if any(w in low for w in SUB_OFF):
        on = False

    spec = {"on": on}
    if not on:
        return spec

    size = None
    if any(w in low for w in SUB_BIG):
        size = "large"
    elif any(w in low for w in SUB_SMALL):
        size = "small"
    if size:
        spec["size"] = size

    for pos, words in (("top", ("字幕放上", "字幕在上", "顶部", "上方")),
                       ("center", ("居中", "中间显示")),
                       ("bottom", ("底部", "放在下面", "最下面"))):
        if any(w in low for w in words):
            spec["position"] = pos
            break

    for color, words in COLOR_WORDS:
        if any(w in low for w in words):
            spec["color"] = color
            break

    if "描边" in low or "黑边" in low:
        spec["outline"] = 3.2
    if "底框" in low or "背景框" in low or "半透明" in low:
        spec["box"] = True
    return spec


def _parse_bgm(text):
    """BGM：要不要 / 指定文件 / 音量 / 要不要原声"""
    low = text.lower()
    hit_on = any(w in low for w in BGM_ON)
    hit_off = any(w in low for w in BGM_OFF)
    hit_no_orig = any(w in low for w in BGM_NO_ORIG)
    # 点名的文件名（带扩展名）
    m = re.search(r"[「\"'《]?([^\s，。；、\"'」》]{1,40}\.(?:mp3|wav|m4a|flac|aac|ogg|wma))",
                  text, re.I)

    if not (hit_on or hit_off or hit_no_orig or m):
        return None

    spec = {"on": not hit_off}
    if m:
        spec["query"] = m.group(1)
        spec["on"] = True

    if hit_no_orig:
        spec["keep_original"] = False
        spec["on"] = True
    if any(w in low for w in BGM_QUIET):
        spec["volume"] = 0.14
    elif any(w in low for w in BGM_LOUD):
        spec["volume"] = 0.40

    if "循环" in low:
        spec["loop"] = True
    return spec


def _find_spans(text):
    """「10秒到20秒」「10 到 20 秒」这类区间（数字可以只有一个带单位）"""
    spans = []
    pat = re.compile(r"(\d+(?:\.\d+)?)\s*(?:秒|s|S)?\s*(?:到|至|-|—|–|~|〜)\s*"
                     r"(\d+(?:\.\d+)?)\s*(?:秒|s|S)?")
    for m in pat.finditer(text):
        a, b = float(m.group(1)), float(m.group(2))
        if b > a and b - a <= 7200:
            spans.append({"start": m.start(), "end": m.end(),
                          "sec_start": a, "sec_end": b})
    return spans


def _parse_speed(text):
    """变速：返回 (全局倍速, 区间变速列表)

    按分句判断——「10到20秒放慢到0.5倍」里区间和倍速在同一个分句，
    就当成区间变速；只有一个倍速词就是整体变速。
    """
    spans = _find_spans(text)
    zones = []
    speed = None

    for mc in re.finditer(r"[^，。；,;！!？?\n]+", text):
        clause = mc.group(0)
        c_start, c_end = mc.span()
        low = clause.lower()

        m = SPEED_NUM.search(clause)
        val = None
        if m:
            try:
                val = float(m.group(1))
            except ValueError:
                val = _cn2num(m.group(1))
            if val and not (0.1 <= val <= 10):
                val = None
        if val is None:
            if any(w in low for w in SLOW_WORDS):
                val = 0.8
            elif any(w in low for w in FAST_WORDS):
                val = 1.5
        if val is None:
            continue

        zone_hit = next((sp for sp in spans
                         if sp["start"] >= c_start and sp["end"] <= c_end), None)
        if zone_hit:
            zones.append({"start": zone_hit["sec_start"], "end": zone_hit["sec_end"],
                          "speed": round(val, 3)})
        elif speed is None:
            speed = round(val, 3)

    return speed, zones


def _parse_transition(text):
    """转场：类型 + 时长

    只说了「加转场」没说类型 → type="auto"，由渲染时按接缝自己判断：
    同一镜头里剪掉停顿的地方硬切，换了镜头才过渡。全片统一一种反而假。
    """
    low = text.lower()
    if any(w in low for w in TRANS_OFF):
        return {"on": False, "type": None}
    if not any(w in low for w in TRANS_ON):
        return None

    kind = "auto"
    for k, words in TRANS_KIND:
        if any(w in low for w in words):
            kind = k
            break

    dur = 0.5 if kind != "auto" else 0.0
    if any(w in low for w in ("转场短", "过渡快", "转场快", "短一点", "干脆")):
        dur = 0.3
    elif any(w in low for w in ("转场长", "过渡慢", "转场慢", "慢一点", "柔和")):
        dur = 0.9
    m = re.search(r"(?:转场|过渡)[^。]{0,10}?(\d+(?:\.\d+)?)\s*秒", text)
    if m:
        v = float(m.group(1))
        if 0.1 <= v <= 3:
            dur = round(v, 2)

    return {"on": True, "type": kind, "duration": dur}


def _parse_fades(text):
    """片头淡入 / 片尾淡出：跟「转场」是两件事，可以只淡入不转场

    返回 {"fade_in": 秒, "fade_out": 秒} 里的 0/1/2 项，没提到就返回 None。
    「不要片头淡入」这种否定给 0，明确表示「别淡」。
    """
    out = {}
    for key, word, default in (("fade_in", "淡入", 0.6), ("fade_out", "淡出", 0.8)):
        m = re.search(word, text)
        if not m:
            continue
        before = text[max(0, m.start() - 6):m.start()]
        if any(w in before for w in ("不要", "别", "不用", "去掉", "取消")):
            out[key] = 0.0
            continue
        # 时长只认紧跟在后面那几个字，别把「剪成 60 秒」当成淡入时长
        after = text[m.end():m.end() + 6]
        dm = re.search(r"(\d+(?:\.\d+)?)\s*秒", after)
        val = float(dm.group(1)) if dm else default
        out[key] = round(max(0.0, min(2.0, val)), 2)
    return out or None


def _parse_speech(text):
    """语音级剪辑：口头禅 / 重说

    这两类要转写，比其他决策慢得多，所以只在明确提到时才开，
    不跟「加字幕」联动——加字幕不代表想剪内容。
    """
    t = text or ""
    if re.search(r"(别|不要|不用|先别|留着|留下)[^，。]{0,4}(口头禅|语气词|嗯啊|重说)", t):
        return {"on": False, "kinds": []}

    kinds = []
    if re.search(r"口头禅|语气词|填充词|嗯啊|呃啊|废话|口水话|啰嗦", t):
        kinds.append("filler")
    if re.search(r"重说|重复说|说了两遍|说错|口误|重录|重新说|卡壳", t):
        kinds.append("retake")
    # 「按说话内容剪」「连语气词一起剪」——两种都要
    if not kinds and re.search(r"按(说话|语音|内容)剪|语音级|连废话", t):
        kinds = ["filler", "retake"]
    return {"on": True, "kinds": kinds} if kinds else None


def _parse_notes(text):
    notes = []
    for key, what in UNSUPPORTED.items():
        if key in text and what not in notes:
            notes.append(what)
    # 「调个色」「调下颜色」这种口语，词表匹配不到
    if re.search(r"调[个下]?\s*(色|颜色)|滤镜|LUT", text, re.I) and "滤镜调色" not in notes:
        notes.append("滤镜调色")
    # 按说话内容挑片段 —— 能转写字幕了，但还不能按语义检索片段
    if re.search(r"(讲|说|提到|聊)[^，。]{0,6}(那段|的部分|的地方|的片段)", text):
        notes.append("按「说了什么」挑片段（现在能转写字幕，还不会按语义挑片段）")
    return notes


def _mask_words(text, words, gap=5):
    """把「淡入 1 秒」里的秒数抹成空格（保持长度和位置不变）

    「片头淡入 1 秒」里的 1 秒是淡入时长，不是成片目标时长；
    「片尾淡出 2 秒」同理。不抹掉的话会被时长解析抢走，出现「目标 1 秒」这种鬼话。
    只抹紧跟其后的那个秒数，不动别处——「加淡入淡出，剪成 60 秒」里的 60 秒还得留着。
    """
    chars = list(text)
    for w in words:
        pat = re.escape(w) + r"[^，。、,;；\d]{0,%d}(\d+(?:\.\d+)?\s*(?:秒钟|秒))" % gap
        for m in re.finditer(pat, text):
            for k in range(m.start(1), m.end(1)):
                chars[k] = " "
    return "".join(chars)


def parse_demand_rules(text, duration=0.0):
    """纯规则解析，不联网也能用"""
    text = (text or "").strip()
    dur = float(duration or 0.0)
    # 淡入/淡出的秒数先让开，别被当成目标时长或要剪掉的区间
    time_text = _mask_words(text, ("淡入", "淡出"))
    target, cuts, keeps = _classify_times(time_text, dur if dur > 0 else 10 ** 6)
    allow, touched = _parse_types(text)
    beats, shot_len = _parse_beat(text)
    style = _parse_style(text)
    speed, speed_zones = _parse_speed(text)

    return {
        "style": style,
        "strength": _parse_strength(text),
        "target": target,
        "allow": allow,
        "type_touched": touched,
        "cuts": cuts,
        "keep": keeps,
        "shot_beats": beats,
        "shot_len": shot_len,
        "subtitle": _parse_subtitle(text),
        "bgm": _parse_bgm(text),
        "speed": speed,
        "speed_zones": speed_zones,
        "transition": _parse_transition(text),
        "fades": _parse_fades(text),
        "speech": _parse_speech(text),
        "notes": _parse_notes(text),
        "raw": text,
        "source": "rule",
    }


# ---------------- 大模型（可选） ----------------

def load_settings():
    cfg = {"llm": {"base_url": "", "api_key": "", "model": "", "enabled": True}}
    try:
        if CONFIG_PATH.exists():
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and isinstance(data.get("llm"), dict):
                cfg["llm"].update({k: v for k, v in data["llm"].items()
                                   if k in ("base_url", "api_key", "model", "enabled")})
    except Exception:
        pass

    # 环境变量优先，方便不改文件临时试
    cfg["llm"]["base_url"] = os.environ.get("LLM_BASE_URL") or cfg["llm"]["base_url"]
    cfg["llm"]["api_key"] = os.environ.get("LLM_API_KEY") or cfg["llm"]["api_key"]
    cfg["llm"]["model"] = os.environ.get("LLM_MODEL") or cfg["llm"]["model"]
    return cfg


def save_settings(settings):
    llm = (settings or {}).get("llm") or {}
    cur = load_settings()
    merged = dict(cur["llm"])
    for k in ("base_url", "api_key", "model", "enabled"):
        if k in llm and llm[k] is not None:
            # 前端回传掩码值时不要覆盖真 key
            if k == "api_key" and str(llm[k]).startswith("****"):
                continue
            merged[k] = llm[k]
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump({"llm": merged}, f, ensure_ascii=False, indent=2)
    return merged


def mask_settings():
    cfg = load_settings()["llm"]
    key = cfg.get("api_key") or ""
    return {
        "base_url": cfg.get("base_url") or "",
        "model": cfg.get("model") or "",
        "api_key": ("****" + key[-4:]) if len(key) > 8 else ("****" if key else ""),
        "enabled": bool(cfg.get("enabled", True)),
        "ready": bool(key and (cfg.get("base_url") or "").strip()),
    }


def llm_ready():
    cfg = load_settings()["llm"]
    return bool(cfg.get("api_key") and (cfg.get("base_url") or "").strip() and cfg.get("enabled", True))


_LLM_SYSTEM = """你是一个视频剪辑需求解析器。用户会用中文说明他想怎么剪，你只输出 JSON，不要任何解释。

JSON 结构：
{
  "style": "auto|talking_head|vlog|beat|highlight|tutorial",
  "strength": "conservative|standard|tight" 或 null,
  "target": 数字（目标时长秒）或 null,
  "allow": ["silence","dup","static","blank"],
  "cuts": [{"start": 秒, "end": 秒}],
  "keep": [{"start": 秒, "end": 秒}],
  "shot_beats": 数字或 null,
  "shot_len": 数字或 null,
  "subtitle": {"on": true, "size": "small|normal|large", "position": "bottom|top|center", "color": "#RRGGBB"} 或 null,
  "bgm": {"on": true, "query": "用户点名的文件名或风格关键词", "volume": 0.24, "keep_original": true} 或 null,
  "speed": 数字（全局倍速）或 null,
  "speed_zones": [{"start": 秒, "end": 秒, "speed": 数字}],
  "transition": {"on": true, "type": "auto|fade|dissolve|black|white|wipe|slide|circle|pixel|smooth|none", "duration": 秒} 或 null,
  "fades": {"fade_in": 秒, "fade_out": 秒} 或 null,
  "speech": {"on": true, "kinds": ["filler","retake"]} 或 null,
  "notes": ["做不到的诉求，简明说明"]
}

字段含义：
- style：卡点/混剪/踩点=beat；口播/讲解/知识= talking_head；旅行/日常/vlog= vlog；高光/精华/快剪=highlight；教程/教学=tutorial；其他=auto
- strength：紧凑/快节奏=tight；保守/别剪太多=conservative；没提=null
- target：用户要求的目标时长（秒），"一分钟"=60
- allow：用户允许剪掉的类型。默认四类全给。用户说"别剪静音"就去掉 silence；"只剪黑场"就只留 blank
- cuts：用户明确要剪掉的时间段
- keep：用户明确只想要的时间段（其余全剪）
- shot_beats：卡点时每个镜头几拍（"每2拍"=2），没提=null
- shot_len：卡点时每个镜头几秒，没提=null
- subtitle：加字幕/上字幕就给 {"on": true}；"不要字幕"给 {"on": false}；没提=null。字号大小、位置、颜色能听出来就填
- bgm：换配乐/加背景音乐就给 {"on": true}；"不要音乐"给 {"on": false}；没提=null。"不要原声"填 keep_original=false
- speed：整体倍速（"1.2倍速"=1.2、"放慢"≈0.8、"加速"≈1.5），没提=null
- speed_zones：只对某个时间段变速时才填，例如"10到20秒放慢到0.5倍"
- transition：加转场/叠化就给 {"on": true}（type 留 auto，由工具按画面自动挑）；"硬切不要转场"给 {"on": false}；没提=null
- fades：只说片头淡入/片尾淡出、没要求接缝转场时才单独给，例如{"fade_in": 1}；说了"不要片头淡入"给 {"fade_in": 0}
- speech：要剪"口头禅/语气词/嗯啊"给 kinds 含 "filler"；要剪"重说/说重了/口误/说了两遍"给 kinds 含 "retake"；两种都要就都写。没提=null（要转写，很慢，别自作主张开）
- notes：无法做到的诉求（如改画幅、调色、配音、去水印），用一句中文说明

只输出 JSON。"""


def _call_llm(text, context):
    import requests

    cfg = load_settings()["llm"]
    base = (cfg.get("base_url") or "").rstrip("/")
    if base.endswith("/chat/completions"):
        url = base
    else:
        url = base + "/chat/completions"

    user = f"视频信息：{json.dumps(context, ensure_ascii=False)}\n剪辑需求：{text}"
    payload = {
        "model": cfg.get("model") or "gpt-4o-mini",
        "messages": [
            {"role": "system", "content": _LLM_SYSTEM},
            {"role": "user", "content": user},
        ],
        "temperature": 0.1,
    }
    headers = {"Authorization": "Bearer " + (cfg.get("api_key") or ""),
               "Content-Type": "application/json"}

    r = requests.post(url, json=payload, headers=headers, timeout=25)
    if r.status_code >= 400:
        # 有些兼容接口不认 response_format / 模型名不同，直接把错误抛给上层
        raise RuntimeError(f"接口返回 {r.status_code}：{r.text[:180]}")
    data = r.json()
    content = (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""
    content = content.strip()
    if content.startswith("```"):
        content = re.sub(r"^```[a-zA-Z]*\s*", "", content)
        content = re.sub(r"```$", "", content).strip()
    return json.loads(content)


def _coerce_llm(raw):
    """把大模型返回的东西收紧成安全、合法的意图字段"""
    out = {}
    style = str(raw.get("style") or "").strip()
    out["style"] = style if style in STYLES else None

    st = str(raw.get("strength") or "").strip()
    out["strength"] = st if st in STRENGTHS else None

    try:
        t = float(raw.get("target")) if raw.get("target") not in (None, "", 0) else None
        out["target"] = t if t and 1 <= t <= 36000 else None
    except (TypeError, ValueError):
        out["target"] = None

    allow = raw.get("allow")
    if isinstance(allow, list):
        picked = {a for a in allow if a in TYPE_LABEL}
        out["allow"] = picked
        out["type_touched"] = True
    else:
        out["allow"] = None
        out["type_touched"] = False

    def _ranges(key):
        vals = raw.get(key)
        if not isinstance(vals, list):
            return None
        got = []
        for v in vals:
            if not isinstance(v, dict):
                continue
            try:
                s = float(v.get("start"))
                e = float(v.get("end"))
            except (TypeError, ValueError):
                continue
            if e - s >= 0.2:
                got.append({"start": round(max(0.0, s), 2), "end": round(e, 2),
                            "reason": str(v.get("reason") or "按你的要求")})
        return got or None

    out["cuts"] = _ranges("cuts")
    out["keep"] = _ranges("keep")

    for key, lo, hi in (("shot_beats", 1, 16), ("shot_len", 0.3, 20)):
        try:
            v = float(raw.get(key))
            out[key] = v if lo <= v <= hi else None
        except (TypeError, ValueError):
            out[key] = None

    notes = raw.get("notes")
    out["notes"] = [str(n) for n in notes if str(n).strip()] if isinstance(notes, list) else []

    # ---- 新增能力 ----
    sub = raw.get("subtitle")
    if isinstance(sub, dict):
        s = {"on": bool(sub.get("on"))}
        size = str(sub.get("size") or "").strip()
        if size in ("small", "normal", "large"):
            s["size"] = size
        pos = str(sub.get("position") or "").strip()
        if pos in ("top", "center", "bottom"):
            s["position"] = pos
        c = str(sub.get("color") or "").strip()
        if re.fullmatch(r"#[0-9A-Fa-f]{6}", c):
            s["color"] = c.upper()
        out["subtitle"] = s
    else:
        out["subtitle"] = None

    bgm = raw.get("bgm")
    if isinstance(bgm, dict):
        b = {"on": bool(bgm.get("on"))}
        q = str(bgm.get("query") or "").strip()
        if q:
            b["query"] = q[:80]
        try:
            v = float(bgm.get("volume"))
            if 0 <= v <= 2:
                b["volume"] = round(v, 3)
        except (TypeError, ValueError):
            pass
        if "keep_original" in bgm:
            b["keep_original"] = bool(bgm["keep_original"])
        if "duck" in bgm:
            b["duck"] = bool(bgm["duck"])
        out["bgm"] = b
    else:
        out["bgm"] = None

    try:
        sp = float(raw.get("speed"))
        out["speed"] = round(sp, 3) if 0.1 <= sp <= 10 else None
    except (TypeError, ValueError):
        out["speed"] = None

    zones = []
    if isinstance(raw.get("speed_zones"), list):
        for z in raw["speed_zones"]:
            if not isinstance(z, dict):
                continue
            try:
                a, b, v = float(z["start"]), float(z["end"]), float(z.get("speed") or 1)
            except (TypeError, ValueError, KeyError):
                continue
            if b - a >= 0.2 and 0.1 <= v <= 10:
                zones.append({"start": round(a, 2), "end": round(b, 2), "speed": round(v, 3)})
    out["speed_zones"] = zones

    tr = raw.get("transition")
    if isinstance(tr, dict):
        if tr.get("on") is False:
            out["transition"] = {"on": False, "type": None}
        else:
            k = str(tr.get("type") or "fade").strip().lower()
            if k not in TRANS_KIND_MAP:
                k = "fade"
            try:
                d = float(tr.get("duration") or 0.5)
            except (TypeError, ValueError):
                d = 0.5
            out["transition"] = {"on": True, "type": k,
                                 "duration": round(max(0.1, min(3.0, d)), 2)}
    else:
        out["transition"] = None

    fades = raw.get("fades")
    if isinstance(fades, dict):
        f = {}
        for key in ("fade_in", "fade_out"):
            try:
                v = fades.get(key)
                if v is not None:
                    f[key] = round(max(0.0, min(2.0, float(v))), 2)
            except (TypeError, ValueError):
                pass
        out["fades"] = f or None
    else:
        out["fades"] = None

    spc = raw.get("speech")
    if isinstance(spc, dict):
        if spc.get("on") is False:
            out["speech"] = {"on": False, "kinds": []}
        else:
            kinds = [k for k in (spc.get("kinds") or [])
                     if k in ("filler", "retake")]
            out["speech"] = {"on": True, "kinds": kinds} if kinds else None
    else:
        out["speech"] = None
    return out


def parse_demand(text, duration=0.0, context=None):
    """规则解析 + （可选）大模型解析，大模型优先、失败退回规则"""
    intent = parse_demand_rules(text, duration)
    if not llm_ready():
        return intent, "rule"

    try:
        ctx = {"duration_sec": round(float(duration or 0), 1)}
        ctx.update(context or {})
        raw = _call_llm(text, ctx)
        llm = _coerce_llm(raw if isinstance(raw, dict) else {})

        for key in ("style", "strength", "target", "shot_beats", "shot_len",
                    "subtitle", "bgm", "speed", "transition", "fades", "speech"):
            if llm.get(key) is not None:
                intent[key] = llm[key]
        if llm.get("speed_zones"):
            intent["speed_zones"] = llm["speed_zones"]
        if llm.get("type_touched") and llm.get("allow"):
            intent["allow"] = set(llm["allow"])
            intent["type_touched"] = True
        if llm.get("cuts"):
            intent["cuts"] = llm["cuts"]
        if llm.get("keep"):
            intent["keep"] = llm["keep"]
        # 两边的「做不到」都保留，去重
        for n in llm.get("notes") or []:
            if n not in intent["notes"]:
                intent["notes"].append(n)
        intent["source"] = "llm"
        return intent, "llm"
    except Exception as e:
        msg = str(e)
        for bad in ("Max retries", "Connection", "timed out", "拒绝", "502", "Name or service"):
            if bad in msg:
                msg = "连不上接口地址"
                break
        intent["notes"].append(f"大模型没连上（{msg[:36]}），已按本地规则理解")
        intent["source"] = "rule-fallback"
        return intent, "rule-fallback"


# ---------------- 方案生成 ----------------

def _complement(zones, dur, min_gap=0.05):
    """给出要保留的区域，返回其余需要剪掉的区域"""
    out = []
    cur = 0.0
    for s, e in _merge([(max(0.0, s), min(dur, e)) for s, e in zones]):
        if s - cur > min_gap:
            out.append((cur, s))
        cur = max(cur, e)
    if dur - cur > min_gap:
        out.append((cur, dur))
    return out


def _build(dur, cuts, decisions, strength, label, intent, extra=None, audio_mode="clip"):
    merged = _merge([(max(0.0, s), min(dur, e)) for s, e in cuts if e > s])
    keep = build_keep_ranges(dur, merged, min_keep=0.30)
    saved = round(sum(e - s for s, e in merged), 2)
    kept = round(sum(e - s for s, e in keep), 2)

    warning = ""
    if not keep:
        warning = "全部内容都会被剪掉，需求太狠了，换个说法试试"
    elif dur > 0 and saved / dur > 0.8:
        warning = f"这次剪掉了 {saved / dur * 100:.0f}%，比例很高，建议先看一遍成片"
    elif dur > 0 and saved / dur > 0.6:
        warning = f"剪掉了 {saved / dur * 100:.0f}%，量不小"

    plan = {
        "cut_ranges": [[round(s, 2), round(e, 2)] for s, e in merged],
        "keep_ranges": [[s, e] for s, e in keep],
        "decisions": sorted(decisions, key=lambda d: d["start"]),
        "before": round(dur, 2),
        "after": kept,
        "saved": saved,
        "strength": strength,
        "strength_label": label,
        "warning": warning,
        "style": intent.get("style") or "auto",
        "style_label": STYLES.get(intent.get("style") or "auto", STYLES["auto"])["label"],
        "audio_mode": audio_mode,
        "target": intent.get("target"),
    }
    if extra:
        plan.update(extra)
    return plan


def _ladder(strength):
    """目标时长压不下来时，逐级加狠"""
    base = dict(STRENGTHS.get(strength) or STRENGTHS[DEFAULT_STRENGTH])
    out = []
    for k in (0.82, 0.66, 0.5):
        p = dict(base)
        p["silence_min"] = round(max(0.25, base["silence_min"] * k), 2)
        p["silence_pad"] = round(max(0.05, base["silence_pad"] * k), 2)
        # 有词级时间戳时用的是这两个（句界 / 句中换气），不跟着收紧的话
        # 「按需求压到 N 秒」在有字幕的片子上会压不到位
        p["sentence_pad"] = round(max(0.05, base.get("sentence_pad", 0.20) * k), 2)
        p["breath"] = round(max(0.30, base.get("breath", 0.60) * k), 2)
        p["static_min"] = round(max(1.2, base["static_min"] * k), 2)
        p["static_keep"] = round(max(0.6, base["static_keep"] * k), 2)
        p["blank_min"] = round(max(0.10, base["blank_min"] * k), 2)
        p["min_cut"] = round(max(0.12, base["min_cut"] * k), 2)
        p["join_gap"] = round(max(0.25, base["join_gap"] * k), 2)
        p["label"] = base["label"]
        out.append(p)
    return out


def _relax(dur, decisions, target):
    """剪太狠了，按优先级把剪点放回去（先放静音，最后才放黑场）"""
    ds = [dict(d) for d in decisions]
    for tier in ("silence", "static", "dup", "blank"):
        while ds:
            cuts = _merge([(d["start"], d["end"]) for d in ds])
            after = dur - sum(e - s for s, e in cuts)
            if after >= target * 0.95:
                return ds
            pool = [d for d in ds if d["type"] == tier]
            if not pool:
                break
            # 先放回「小气口」：多个短停顿比一个大静音自然，说话也更连贯
            ds.remove(min(pool, key=lambda d: d["saved"]))
    return ds


def _smooth_plan(plan, dur, min_shot=0.5, max_gap=1.2, max_merged=3.0):
    """把「眨眼就没」的碎镜头合掉

    0.3 秒的画面还没看清就切走了，是生硬感的来源之一。这里只处理真正的闪切：
    保留段短于 0.5s、两边都只是小间隙（剪口气），且合并后不超过 3 秒才合——
    不然会把一串短句串成一个大长镜，反而更拖。

    返回 (plan, 说明文案)
    """
    keep = [tuple(k) for k in (plan.get("keep_ranges") or [])]
    decisions = list(plan.get("decisions") or [])
    if len(keep) < 3 or not decisions:
        return plan, ""

    drop = []      # 取消掉的剪点（原片区间）
    merged = [list(keep[0])]
    i, n = 1, len(keep)
    while i < n:
        s, e = keep[i]
        short = (e - s) < min_shot and i < n - 1
        if short:
            gap_l = s - merged[-1][1]
            gap_r = keep[i + 1][0] - e
            if gap_l <= max_gap and gap_r <= max_gap \
                    and (keep[i + 1][1] - merged[-1][0]) <= max_merged:
                drop.append((merged[-1][1], s))
                drop.append((e, keep[i + 1][0]))
                merged[-1][1] = keep[i + 1][1]
                i += 2
                continue
        merged.append([s, e])
        i += 1

    if not drop:
        return plan, ""

    def hit(d):
        return any(min(float(d["end"]), b) - max(float(d["start"]), a) > 0.05
                   for a, b in drop)

    kept = [d for d in decisions if not hit(d)]
    if not kept:
        return plan, ""

    plan = dict(plan)
    plan["decisions"] = kept
    note = f"顺手合掉了 {len(drop) // 2} 个眨眼就没的闪切"
    return plan, note


def _shot_stats(plan):
    """镜长分布，用于回执里说清「节奏长什么样」（按成片后的长度算）"""
    lens = []
    for r in (plan.get("keep_ranges") or []):
        if isinstance(r, dict):
            d = (float(r.get("end") or 0) - float(r.get("start") or 0)) / max(
                0.1, float(r.get("speed") or 1.0))
        else:
            d = float(r[1]) - float(r[0])
        if d > 0:
            lens.append(d)
    if not lens:
        return ""
    return (f"{len(lens)} 个镜头，最长 {max(lens):.1f}s / 最短 {min(lens):.1f}s"
            f" / 平均 {sum(lens) / len(lens):.1f}s")


def _resolve(rng, dur):
    """把「结尾 5 秒」这类依赖总时长的区间算成绝对时间"""
    if rng.get("anchor") == "tail":
        n = float(rng.get("len") or 0)
        return max(0.0, dur - n), dur
    s = max(0.0, float(rng.get("start") or 0))
    e = min(dur, float(rng.get("end") or 0))
    return s, e


def _apply_manual(dur, plan, intent):
    """叠加用户明确点名的剪掉/保留区间"""
    decisions = list(plan.get("decisions") or [])
    cuts = [(d["start"], d["end"]) for d in decisions]

    keep_only = intent.get("keep") or []
    if keep_only:
        zones = _merge([_resolve(k, dur) for k in keep_only])
        zones = [(s, e) for s, e in zones if e - s >= 0.2]
        if zones:
            cuts = _complement(zones, dur)
            span = "、".join(f"{s:.1f}s–{e:.1f}s" for s, e in zones[:3])
            decisions = [{
                "type": "manual",
                "start": round(s, 2),
                "end": round(e, 2),
                "saved": round(e - s, 2),
                "reason": f"你要求只保留 {span}，其余剪掉",
            } for s, e in cuts]
            return cuts, decisions

    for c in intent.get("cuts") or []:
        s, e = _resolve(c, dur)
        if e - s < 0.2:
            continue
        cuts.append((s, e))
        decisions.append({
            "type": "manual", "start": round(s, 2), "end": round(e, 2),
            "saved": round(e - s, 2),
            "reason": c.get("reason") or "按你的要求剪掉这段",
        })
    return cuts, decisions


def _beat_plan(analysis, intent, video_path, dur, progress_cb=None):
    """卡点混剪：按节拍等长排镜头"""
    from backend.beat import detect_beats

    if progress_cb:
        progress_cb(58, "听音乐找节拍…")
    info = detect_beats(video_path, dur) if video_path else {"ok": False, "bpm": 0, "beats": []}

    bpm = float(info.get("bpm") or 0) or 120.0
    beat_int = 60.0 / bpm
    per = int(intent.get("shot_beats") or 2)
    shot_len = float(intent.get("shot_len") or (beat_int * per))
    shot_len = max(0.4, min(12.0, shot_len))

    target = float(intent.get("target") or 0) or min(dur, 60.0)
    target = max(1.0, min(target, dur))

    bounds = [0.0] + sorted(float(s["start"]) for s in (analysis.get("scenes") or [])) + [dur]
    shots = []
    for i in range(len(bounds) - 1):
        a, b = bounds[i], bounds[i + 1]
        if b - a >= 0.4:
            shots.append((a, b))

    # 镜头不够凑目标时长时，把每个镜头拉长到「拍数 × 节拍」，别硬凑成十几秒的片
    if not intent.get("shot_len") and shots and target > 0:
        import math

        slots = len(shots) * 6
        if slots * shot_len < target:
            need_beats = int(math.ceil(target / max(1, slots) / beat_int))
            shot_len = round(min(16, max(per, need_beats)) * beat_int, 3)

    blanks = [(float(x["start"]), float(x["end"])) for x in (analysis.get("blank") or [])]

    def is_blank_moment(t):
        return any(s <= t <= e for s, e in blanks)

    picked, seen, total = [], set(), 0.0
    # 一轮不够就换个入点再来一轮（混剪里重复用镜头很正常），最多 6 轮
    for ratio in (0.0, 0.5, 0.25, 0.75, 0.12, 0.88):
        if total >= target - 0.05:
            break
        for a, b in shots:
            if total >= target - 0.05:
                break
            if is_blank_moment(a + (b - a) * ratio):
                continue
            start = a + (b - a) * ratio
            length = min(shot_len, b - start)
            if length < 0.35:
                continue
            key = (round(start, 1), round(length, 1))
            if key in seen:
                continue
            seen.add(key)
            picked.append((round(start, 2), round(start + length, 2)))
            total += length

    if not picked:
        # 一个镜头都没挑出来：退回标准决策，别让用户空手而归
        plan = decide(analysis, intent.get("strength") or "tight")
        plan.update(style="beat", style_label=STYLES["beat"]["label"],
                    audio_mode="clip", target=target,
                    beat={"ok": False, "bpm": bpm, "reason": "没排上镜头"})
        return plan

    picked.sort()
    keep = [(s, min(e, dur)) for s, e in picked if e > s]
    keep = _merge(keep)
    cuts = _complement(keep, dur, min_gap=0.15)

    decisions = [{
        "type": "beat",
        "start": round(s, 2),
        "end": round(e, 2),
        "saved": round(e - s, 2),
        "reason": ("按节拍跳过" if info.get("ok") else "按固定节奏跳过") + f" {e - s:.1f}s",
    } for s, e in cuts]

    note = ""
    if not info.get("ok"):
        note = f"没听出稳定节拍（{info.get('reason') or '原因不明'}），改用每 {shot_len:.1f}s 换一个镜头"
    short = ""
    if total < target * 0.9:
        short = f"镜头不够，只排出 {total:.0f}s（目标 {target:.0f}s）"

    extra = {
        "beat": {
            "ok": bool(info.get("ok")),
            "bpm": round(bpm, 1),
            "shot_len": round(shot_len, 2),
            "shots_used": len(keep),
            "note": note or short,
        },
        "audio_mode": "full",
    }
    plan = _build(dur, cuts, decisions, "tight", STYLES["beat"]["label"], intent,
                  extra=extra, audio_mode="full")
    plan["keep_ranges"] = [[round(s, 2), round(e, 2)] for s, e in keep]
    tips = [t for t in (plan.get("warning"), short) if t]
    plan["warning"] = "；".join(dict.fromkeys(tips))
    return plan


def _plan_core(analysis, intent, video_path=None, duration=None, progress_cb=None):
    """意图 + 画面数据 → 剪辑方案（结构与 autocut.decide 兼容）"""
    dur = float(duration or analysis.get("duration") or 0)
    if dur <= 0:
        return {"cut_ranges": [], "keep_ranges": [], "decisions": [], "before": 0,
                "after": 0, "saved": 0, "warning": "视频时长异常，无法决策",
                "style": intent.get("style") or "auto", "audio_mode": "clip"}

    style = intent.get("style") or "auto"
    cfg = STYLES.get(style) or STYLES["auto"]
    strength = intent.get("strength") or cfg.get("strength_bias") or DEFAULT_STRENGTH
    if strength not in STRENGTHS:
        strength = DEFAULT_STRENGTH

    if style == "beat":
        return _beat_plan(analysis, intent, video_path, dur, progress_cb)

    allow = set(intent.get("allow") or TYPE_LABEL)
    if not intent.get("type_touched") and cfg.get("allow"):
        allow &= set(cfg["allow"])

    filtered = dict(analysis)
    if "silence" not in allow:
        filtered["silent"] = []
    if "dup" not in allow:
        filtered["redundant"] = []
    if "static" not in allow:
        filtered["static"] = []
    if "blank" not in allow:
        filtered["blank"] = []

    plan = decide(filtered, strength)
    speech_stats = plan.get("speech") or {}
    plan, pacing_note = _smooth_plan(plan, dur)
    cuts, decisions = _apply_manual(dur, plan, intent)
    plan = _build(dur, cuts, decisions, strength, STRENGTHS[strength]["label"], intent,
                  extra={"strength": strength,
                         "strength_label": STRENGTHS[strength]["label"],
                         "speech": speech_stats})
    if pacing_note:
        plan["pacing_note"] = pacing_note

    target = intent.get("target")
    if not target:
        return plan

    target = float(target)
    if plan["after"] <= target + 0.3:
        if plan["after"] < target * 0.92 and plan["decisions"]:
            kept_decisions = _relax(dur, plan["decisions"], target)
            cuts2 = [(d["start"], d["end"]) for d in kept_decisions]
            relaxed = _build(dur, cuts2, kept_decisions, strength,
                             STRENGTHS[strength]["label"], intent,
                             extra={"speech": plan.get("speech") or {}})
            if relaxed["after"] > plan["after"]:
                relaxed["target_note"] = f"为了凑到你想要的 {target:.0f}s，放回了一部分内容"
                return relaxed
        return plan

    # 需要更狠：逐级收紧（取剪得最狠的那一版，参数之间不保证单调）
    best = plan
    for p in _ladder(strength):
        cand = decide(filtered, strength, params=p)
        c2, d2 = _apply_manual(dur, cand, intent)
        cand2 = _build(dur, c2, d2, strength, STRENGTHS[strength]["label"], intent,
                       extra={"speech": cand.get("speech") or {}})
        if cand2["after"] <= target + 0.3:
            cand2["target_note"] = f"为了压到 {target:.0f}s，节奏比「{STRENGTHS[strength]['label']}」更紧一档"
            return cand2
        if cand2["after"] < best["after"]:
            best = cand2

    plan = best

    plan["target_note"] = f"目标 {target:.0f}s 没达到：素材不够，最狠只到 {plan['after']:.0f}s"
    return plan


def _eff_transition(trans, segs):
    """转场实际吃掉的总时长（和渲染器的算法一致）

    自动模式下每个接缝多长要等切片后看画面才知道，这里按
    「一半接缝有转场、每个 0.35s」估一个，用于提前预告成片长度。
    `type="none"` 是「只要首尾淡入淡出、接缝全硬切」，不吃时长。
    """
    if not trans or not trans.get("on") or len(segs) < 2:
        return 0.0
    n = len(segs)
    kind = str(trans.get("type") or "auto")
    if kind == "none":
        return 0.0
    if kind in ("auto", ""):
        return round(0.35 * (n - 1) * 0.55, 2)
    d = float(trans.get("duration") or 0.5)
    lens = [(e - s) / max(0.1, sp) for s, e, sp in segs]
    d = min(d, min(lens) * 0.45)
    return d * (n - 1)


def _apply_effects(plan, intent, dur):
    """把变速落进 keep_ranges，并把字幕/BGM/转场/首尾淡入淡出挂到方案上"""
    keep = plan.get("keep_ranges") or []
    speed = float(intent.get("speed") or 1.0)
    zones = intent.get("speed_zones") or []
    trans = intent.get("transition")
    fades = intent.get("fades") or {}
    gripes = []

    base_speed = speed if abs(speed - 1.0) > 1e-6 else 1.0
    need_speed = abs(speed - 1.0) > 1e-6 or bool(zones)
    need_trans = bool(trans and trans.get("on"))

    # 只说了「片头淡入 1 秒」没说转场：接缝全硬切，但首尾该淡还得淡
    if fades and not need_trans:
        trans = {"on": True, "type": "none", "duration": 0.0}
        need_trans = True
    if need_trans and fades:
        trans = dict(trans or {}, **fades)

    # 区间变速会凭空多出接缝，在这些地方加转场很怪，索性不做并说明
    # （首尾淡入淡出跟接缝无关，留着不影响）
    if zones and need_trans and str(trans.get("type") or "") != "none":
        trans = ({"on": True, "type": "none", "duration": 0.0, **fades} if fades
                 else {"on": False, "type": None})
        need_trans = bool(fades)
        gripes.append("区间变速的地方不合适加转场，这次没加——要转场就把倍速改成整片统一")

    if keep and (need_speed or need_trans):
        segs = [[float(s), float(e), base_speed] for s, e in keep]

        for z in zones:
            zs, ze = float(z["start"]), float(z["end"])
            zsp = float(z["speed"])
            nxt = []
            for s, e, sp in segs:
                if ze <= s + 1e-6 or zs >= e - 1e-6:
                    nxt.append([s, e, sp])
                    continue
                if s < zs - 1e-6:
                    nxt.append([s, min(zs, e), sp])
                a, b = max(s, zs), min(e, ze)
                if b - a > 0.05:
                    nxt.append([a, b, zsp])
                if e > ze + 1e-6:
                    nxt.append([max(ze, s), e, sp])
            segs = nxt

        segs = [s for s in segs if s[1] - s[0] > 0.05]

        if need_speed:
            plan["keep_ranges"] = [{"start": round(s, 3), "end": round(e, 3),
                                    "speed": round(sp, 3)} for s, e, sp in segs]
            plan["speed"] = base_speed
            if zones:
                plan["speed_zones"] = zones
        elif need_trans:
            plan["keep_ranges"] = [{"start": round(s, 3), "end": round(e, 3),
                                    "speed": 1.0} for s, e, sp in segs]

        total = sum((e - s) / max(0.1, sp) for s, e, sp in segs)
        total -= _eff_transition(trans, segs)
        # saved 保持「从原片剪掉了多少」，变速不改这个数，否则用户会看不懂
        plan["after"] = round(max(0.0, total), 2)

    plan["subtitle"] = intent.get("subtitle")
    plan["bgm"] = intent.get("bgm")
    plan["transition"] = trans if need_trans else None
    if gripes:
        plan["warning"] = "；".join([x for x in [plan.get("warning")] + gripes if x])
    return plan


def _with_speech(analysis, intent, video_path, duration, progress_cb=None):
    """用户要求剪口头禅/重说时，补一段转写结果进 analysis

    转写是这条链路上最慢的一步（CPU 上接近实时），所以：
    - 只有明确要求才跑
    - 卡点混剪不跑（那套逻辑按节拍挑镜头，语音级剪辑会被覆盖掉）
    - 失败只记一笔，不阻断出片
    """
    sp = intent.get("speech") or {}
    kinds = tuple(sp.get("kinds") or ()) if sp.get("on") else ()
    if not kinds or not video_path:
        return analysis
    if (intent.get("style") or "auto") == "beat":
        return analysis
    if not analysis.get("has_audio", True):
        return analysis

    if progress_cb:
        progress_cb(30, "听写中，找口头禅和重说…")
    try:
        from backend import speech_cut, subtitle

        segs = subtitle.transcribe(video_path, word_level="filler" in kinds)
        speech = speech_cut.analyze(
            segs, intent.get("strength") or DEFAULT_STRENGTH, kinds,
            duration=float(duration or analysis.get("duration") or 0))
        # 词边界：静音剪点要靠它分「句界 / 句中卡壳」（autocut.decide 读这个）
        if speech.get("word_level"):
            speech["words"] = speech_cut.flatten_words(segs)
    except Exception as e:
        out = dict(analysis)
        out["speech"] = {"filler": [], "retake": [], "word_level": False,
                         "note": f"转写没成功（{e}），这次没剪口头禅"}
        return out

    out = dict(analysis)
    out["speech"] = speech
    return out


def build_plan(analysis, intent, video_path=None, duration=None, progress_cb=None):
    """对外的方案入口：先按常规剪，再把变速等效果叠上去"""
    dur = float(duration or analysis.get("duration") or 0)
    speed = float(intent.get("speed") or 1.0)

    analysis = _with_speech(analysis, intent, video_path, dur, progress_cb)

    # 「60 秒 + 1.2 倍速」要剪到 72 秒，变速后才刚好 60 秒
    core_intent = intent
    if intent.get("target") and abs(speed - 1.0) > 1e-6:
        core_intent = dict(intent)
        core_intent["target"] = round(float(intent["target"]) * speed, 2)

    plan = _plan_core(analysis, core_intent, video_path, duration, progress_cb)
    plan = _apply_effects(plan, intent, dur)
    return _retarget_notes(plan, intent)


def _retarget_notes(plan, intent):
    """变速会改变成片时长，目标时长的提示必须用最终数字重写

    否则会出现「目标 18 秒……最狠只到 26 秒」这种自相矛盾的回执：
    26s 是剪完还没变速的时长，变速后其实已经压到 21.5s 了。
    """
    target = intent.get("target")
    note = plan.get("target_note")
    if not target or not note:
        return plan

    tgt = float(target)
    after = float(plan.get("after") or 0)

    # 差在 2%（且至少 0.6s）以内就算达标，别为 0.5s 的误差唱反调
    reached = abs(after - tgt) <= max(0.6, tgt * 0.02)
    if reached:
        plan.pop("target_note", None)
        return plan

    if after > tgt:
        plan["target_note"] = f"目标 {tgt:.0f}s 没达到：素材不够，最狠只到 {after:.0f}s"
    else:
        plan["target_note"] = f"目标 {tgt:.0f}s 没达到：全片变速后也只有 {after:.0f}s"
    return plan


# ---------------- 回执文案 ----------------

def describe(intent, plan=None):
    """把理解结果说回给用户，让 TA 确认「我理解对了吗」"""
    style = intent.get("style") or "auto"
    label = STYLES.get(style, STYLES["auto"])["label"]
    bits = []

    if style != "auto":
        bits.append(f"**{label}**" + ("，" + STYLES[style]["desc"] if STYLES[style]["desc"] else ""))

    st = intent.get("strength")
    if st in STRENGTHS:
        bits.append(f"{STRENGTHS[st]['label']}节奏")

    target = intent.get("target")
    if target:
        bits.append(f"目标 {target:.0f} 秒")

    if intent.get("type_touched"):
        allow = set(intent.get("allow") or [])
        if not allow:
            bits.append("不剪任何自动检出的内容")
        elif len(allow) <= 2:
            bits.append("只剪 " + "、".join(TYPE_LABEL[a] for a in sorted(allow)))
        else:
            excluded = [TYPE_LABEL[t] for t in ("silence", "static", "dup", "blank")
                        if t not in allow]
            bits.append("不剪 " + "、".join(excluded))

    if intent.get("cuts"):
        real = [c for c in intent["cuts"]
                if c.get("anchor") == "tail" or (c.get("end", 0) - c.get("start", 0)) >= 0.5]
        bits.append(f"{len(real)} 段指定区间剪掉")
    if intent.get("keep"):
        bits.append("只保留你点名的区间")

    if style == "beat":
        per = intent.get("shot_beats")
        ln = intent.get("shot_len")
        if ln:
            bits.append(f"每 {ln:g}s 换一个镜头")
        elif per:
            bits.append(f"每 {per} 拍换一个镜头")
        else:
            bits.append("每 2 拍换一个镜头")

    # 字幕 / BGM / 变速 / 转场
    sub = intent.get("subtitle")
    if sub:
        if sub.get("on"):
            det = []
            if sub.get("size") == "large":
                det.append("大字")
            elif sub.get("size") == "small":
                det.append("小字")
            if sub.get("position") == "top":
                det.append("放顶部")
            elif sub.get("position") == "center":
                det.append("居中")
            if sub.get("color"):
                det.append(sub["color"])
            bits.append("加字幕" + ("（" + "、".join(det) + "）" if det else ""))
        else:
            bits.append("不加字幕")

    bgm = intent.get("bgm")
    if bgm:
        if bgm.get("on"):
            det = []
            if bgm.get("query"):
                det.append(bgm["query"])
            if bgm.get("keep_original") is False:
                det.append("去掉原声")
            elif bgm.get("volume") and bgm["volume"] < 0.2:
                det.append("音乐很轻")
            elif bgm.get("volume") and bgm["volume"] > 0.35:
                det.append("音乐明显")
            bits.append("换 BGM" + ("（" + "、".join(det) + "）" if det else ""))
        else:
            bits.append("不加 BGM")

    sp = intent.get("speed")
    if sp and abs(float(sp) - 1.0) > 1e-6:
        bits.append(f"全片 {float(sp):g} 倍速")
    zones = intent.get("speed_zones") or []
    for z in zones[:3]:
        bits.append(f"{z['start']:.0f}–{z['end']:.0f}s 用 {z['speed']:g} 倍速")

    tr = intent.get("transition")
    if tr:
        if tr.get("on"):
            kind = str(tr.get("type") or "auto")
            if kind in ("auto", ""):
                bits.append("转场按接缝自动安排（同一镜头里剪停顿就硬切，换了镜头才过渡，"
                            "换的幅度大就用黑场或溶解）")
            elif kind == "none":
                bits.append("接缝都硬切")
            else:
                bits.append(f"{TRANS_LABEL.get(kind, '转场')}转场 {tr.get('duration', 0.5):g}s")
        else:
            bits.append("不用转场，硬切")

    fades = intent.get("fades") or {}
    if "fade_in" in fades:
        bits.append("片头不淡入" if fades["fade_in"] <= 0
                    else f"片头淡入 {fades['fade_in']:g}s")
    if "fade_out" in fades:
        bits.append("片尾不淡出" if fades["fade_out"] <= 0
                    else f"片尾淡出 {fades['fade_out']:g}s")

    spc = intent.get("speech") or {}
    if spc.get("on") and spc.get("kinds"):
        names = {"filler": "口头禅", "retake": "重说"}
        bits.append("剪掉" + "和".join(names[k] for k in spc["kinds"] if k in names))

    if not bits:
        head = "没抓到具体的要求，我先按标准档剪一遍——你可以说「紧凑一点」「控制在 60 秒」「开头 3 秒不要」「卡点混剪 45 秒」。"
    else:
        head = "明白：" + " · ".join(bits) + "。"

    if plan:
        after = plan.get("after") or 0
        saved = plan.get("saved") or 0
        if plan.get("decisions") or after:
            extra = [f"原片 {plan.get('before', 0):.0f}s"]
            if saved >= 0.5:
                extra.append(f"剪掉 {saved:.1f}s")
            sp = plan.get("speed")
            if sp and abs(float(sp) - 1.0) > 1e-6:
                extra.append(f"{float(sp):g} 倍速")
            if plan.get("transition"):
                tr = plan["transition"]
                kind = str(tr.get("type") or "auto")
                if kind == "none":
                    extra.append("首尾淡入淡出")
                else:
                    extra.append("自动转场" if kind in ("auto", "") else "带转场")
            head += f"成片约 {after:.1f}s（{'，'.join(extra)}）。"
        shots = _shot_stats(plan)
        if shots:
            head += f"剪成 {shots}。"
        sp_stats = plan.get("speech") or {}
        if sp_stats.get("filler") or sp_stats.get("retake"):
            got = []
            if sp_stats.get("filler"):
                got.append(f"{sp_stats['filler']} 处口头禅")
            if sp_stats.get("retake"):
                got.append(f"{sp_stats['retake']} 处重说")
            head += f"其中剪掉了{'、'.join(got)}。"
        if sp_stats.get("note"):
            head += sp_stats["note"] + "。"
        if plan.get("pacing_note"):
            head += plan["pacing_note"] + "。"
        if plan.get("beat", {}).get("note"):
            head += plan["beat"]["note"] + "。"
        if plan.get("target_note"):
            head += plan["target_note"] + "。"
        if plan.get("warning"):
            head += "注意：" + plan["warning"] + "。"

    notes = intent.get("notes") or []
    if notes:
        head += "\n做不到的：" + "；".join(dict.fromkeys(notes)) + "。"
    return head
