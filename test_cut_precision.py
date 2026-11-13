# -*- coding: utf-8 -*-
"""剪点精度回归 —— 纯逻辑，不需要视频、不需要模型

守三件事（P0-0 第二步）：
1) 静音检测的粒度降下来之后，三档阈值真的能分出差别（以前 1.0s 卡死，两档空转）
2) 有词级时间戳时，句界只留 0.2s、句中卡壳留 0.6s，且剪点绝不切进词里
3) 没有词级时间戳时，静音剪法必须和改动前**逐字一致**（这是防回归的底线）
"""

import sys

from backend import autocut
from backend import speech_cut as sc

OK = 0
FAIL = []


def check(name, cond, detail=""):
    global OK
    if cond:
        OK += 1
        print(f"  ✓ {name}")
    else:
        FAIL.append(name)
        print(f"  ✗ {name}   {detail}")


# ---------------- 构造分析结果 ----------------

def W(s, e, t="字", term=False):
    """一个词：term 表示它后面有句末标点（这句话说完了）"""
    return {"start": s, "end": e, "text": t, "term": term}


def A(duration, silent, words=None, has_audio=True):
    a = {
        "duration": duration,
        "silent": [{"start": s, "end": e} for s, e in silent],
        "redundant": [], "blank": [], "static": [],
        "has_audio": has_audio,
    }
    if words is not None:
        a["speech"] = {"filler": [], "retake": [], "word_level": True, "words": words}
    return a


def cuts(analysis, strength="standard"):
    return autocut.decide(analysis, strength)["cut_ranges"]


def reasons(analysis, strength="standard"):
    return [d["reason"] for d in autocut.decide(analysis, strength)["decisions"]
            if d["type"] == "silence"]


# 改动前的静音逻辑，原样抄一份用来对照「没词级时必须一模一样」
def old_silence_cuts(analysis, p):
    dur = float(analysis["duration"])
    out = []
    for seg in analysis.get("silent") or []:
        s, e = float(seg["start"]), float(seg["end"])
        length = e - s
        if length < p["silence_min"]:
            continue
        s2 = s if s <= 0.05 else s + p["silence_pad"]
        e2 = e if e >= dur - 0.05 else e - p["silence_pad"]
        if e2 - s2 < p["min_cut"]:
            continue
        out.append((round(s2, 2), round(e2, 2)))
    return [[s, e] for s, e in
            autocut._merge(out, join_gap=p["join_gap"])]


print("\n== 1. 没有词级数据：和改动前逐字一致 ==")

cases = [
    ("中段两条静音", A(20.0, [(3.0, 5.0), (12.0, 14.5)])),
    ("开头结尾静音", A(15.0, [(0.0, 2.0), (13.0, 15.0)])),
    ("贴边 0.05 的静音", A(10.0, [(0.04, 3.0), (7.0, 9.96)])),
    ("一堆短静音", A(12.0, [(1.0, 1.8), (3.0, 4.2), (6.0, 6.6), (9.0, 11.5)])),
]
for st in ("conservative", "standard", "tight"):
    p = autocut.STRENGTHS[st]
    for name, a in cases:
        got, exp = cuts(a, st), old_silence_cuts(a, p)
        check(f"{st} · {name}", got == exp, f"期望 {exp} 实际 {got}")

check("无音轨不剪静音", cuts(A(10.0, [(2.0, 5.0)], has_audio=False)) == [],
      str(cuts(A(10.0, [(2.0, 5.0)], has_audio=False))))

# 空 speech（有 key 但没 words）也必须走旧逻辑
a_empty = A(12.0, [(3.0, 6.0)])
a_empty["speech"] = {"filler": [], "retake": [], "word_level": False}
check("有 speech 段但没 words → 旧逻辑",
      cuts(a_empty) == old_silence_cuts(a_empty, autocut.STRENGTHS["standard"]),
      str(cuts(a_empty)))


print("\n== 2. 句界：上一句说完了，只留 0.2s ==")

a = A(10.0, [(3.0, 5.0)], words=[W(1.0, 3.0, "第一句", True), W(5.0, 7.0, "第二句")])
c = cuts(a)
check("标准档句界 2.0s → 剪 [3.2, 4.8]", c == [[3.2, 4.8]], str(c))
check("原因写明句界", any("句界" in r and "0.20" in r for r in reasons(a)), str(reasons(a)))

c_cons = cuts(a, "conservative")
check("保守档句界留 0.35 → [3.35, 4.65]", c_cons == [[3.35, 4.65]], str(c_cons))

# 句界 + 长静音：剪得更狠，但不至于留 1 秒空场
a_long = A(12.0, [(3.0, 8.0)], words=[W(1.0, 3.0, "第一句", True), W(8.0, 9.0, "第二句")])
c = cuts(a_long)
check("句界 5s 长空场只留 0.4s", c == [[3.2, 7.8]], str(c))


print("\n== 3. 句中卡壳：留 0.6s 换气 ==")

a = A(10.0, [(3.0, 4.6)], words=[W(1.0, 3.0, "说到一半", False), W(4.6, 6.0, "接着说")])
c = cuts(a)
check("1.6s 卡壳剪掉 1.0s（留 0.6）", abs((c[0][1] - c[0][0]) - 1.0) <= 0.02, str(c))
kept = 1.6 - (c[0][1] - c[0][0])
check("剪完还剩 0.6s 换气口", abs(kept - 0.60) <= 0.02, f"实际留 {kept:.2f}s")
check("原因写明卡壳", any("卡壳" in r for r in reasons(a)), str(reasons(a)))

# 卡壳但停顿刚好等于换气口：没有可剪的，别硬剪
a_short = A(10.0, [(3.0, 3.6)], words=[W(1.0, 3.0, "说到一半", False),
                                       W(3.6, 5.0, "接着说")])
check("紧凑档 0.6s 卡壳正好等于换气口 → 不剪",
      cuts(a_short, "tight") == [], str(cuts(a_short, "tight")))

a_short2 = A(10.0, [(3.0, 4.0)], words=[W(1.0, 3.0, "说到一半", False),
                                        W(4.0, 5.0, "接着说")])
c = cuts(a_short2)
check("1.0s 卡壳剪 0.4s、留 0.6s",
      c and abs((c[0][1] - c[0][0]) - 0.4) <= 0.02, str(c))

# 紧凑档卡壳留 0.55，比标准更狠
c_tight = cuts(a, "tight")
check("紧凑档卡壳留 0.55", abs(1.6 - (c_tight[0][1] - c_tight[0][0]) - 0.55) <= 0.02,
      str(c_tight))


print("\n== 4. 剪点避让词边界（不切半个字） ==")

# 检测出来的静音比词边界早了 0.3s：剪点必须推到词尾 +0.04 之后
a = A(10.0, [(3.0, 5.0)], words=[W(1.0, 3.3, "上一句", True), W(5.0, 7.0, "下一句")])
c = cuts(a)
check("静音边界切进词里 → 剪点推到词尾之后",
      c and c[0][0] >= 3.3 + autocut.WORD_GUARD - 1e-6, str(c))

# 下一个词从 4.7 就开始（静音检测多报了）：剪点必须收在词首之前
a2 = A(10.0, [(3.0, 5.0)], words=[W(1.0, 2.8, "上一句", True), W(4.7, 7.0, "下一句")])
c2 = cuts(a2)
check("静音尾巴盖住后一个词 → 剪点收在词首之前",
      c2 and c2[0][1] <= 4.7 - autocut.WORD_GUARD + 1e-6, str(c2))

# 开头/结尾的静音也要避让词：原来这两处只按固定 pad 算，检测差几十毫秒就切进字里
a_head = A(10.0, [(0.0, 2.0)], words=[W(1.7, 3.0, "开头这句", False)])
c = cuts(a_head, "tight")
check("开头静音不切到第一个词",
      c and c[0][1] <= 1.7 - autocut.WORD_GUARD + 1e-6, str(c))

a_tail = A(10.0, [(8.0, 10.0)], words=[W(5.0, 8.3, "最后一句", True)])
c = cuts(a_tail, "tight")
check("结尾静音不切到最后一个词",
      c and c[0][0] >= 8.3 + autocut.WORD_GUARD - 1e-6, str(c))

# 整段静音都落在词里（检测明显错位）→ 宁可跳过不剪
a3 = A(10.0, [(3.0, 4.0)], words=[W(1.0, 5.0, "一整个长词", False), W(5.2, 6.0, "后")])
check("静音整段落在词内 → 跳过，不切词",
      cuts(a3) == [], str(cuts(a3)))

# 所有剪点都必须离开词边界 0.04s 以上
a4 = A(20.0, [(3.0, 5.0), (9.0, 11.0), (15.0, 17.0)],
       words=[W(1.0, 3.05, "一", True), W(4.99, 7.0, "二", False),
              W(8.9, 9.1, "三", False), W(10.9, 12.0, "四", True),
              W(14.8, 16.9, "五", False), W(17.05, 19.0, "六", True)])
c4 = cuts(a4)
words = a4["speech"]["words"]
bad = []
for s, e in c4:
    for w in words:
        if (w["start"] < s < w["end"]) or (w["start"] < e < w["end"]):
            bad.append((s, e, w["start"], w["end"]))
check("三条剪点全部落在词间隙里", not bad, str(bad))


print("\n== 5. 三档阈值真的分开了（原来 1.0s 卡死，两档空转） ==")

a = A(20.0, [(2.0, 2.6), (5.0, 5.9), (8.0, 9.4)],
      words=[W(0.5, 1.9, "一", True), W(2.6, 4.9, "二", False), W(5.9, 7.9, "三", True),
             W(9.4, 12.0, "四", False)])
n = {st: len(cuts(a, st)) for st in ("conservative", "standard", "tight")}
check("紧凑 3 条（0.6/0.9/1.4 全剪）", n["tight"] == 3, str(n))
check("标准 2 条（0.6 不剪）", n["standard"] == 2, str(n))
check("保守 1 条（只剪 1.4s）", n["conservative"] == 1, str(n))

# 检测粒度：analyze 传 0.5 时，0.6s 的停顿才可能被走到 decide
import inspect
check("紧凑档 silence_min = 0.55", autocut.STRENGTHS["tight"]["silence_min"] == 0.55,
      str(autocut.STRENGTHS["tight"]["silence_min"]))
from backend import ai_analyzer
asig = inspect.signature(ai_analyzer.AIAnalyzer.analyze)
check("AIAnalyzer.analyze 支持 min_silence", "min_silence" in asig.parameters, str(asig))


print("\n== 6. flatten_words 带上句末标记 ==")

segs = [{"start": 0.0, "end": 2.0, "text": "第一句。", "words": [
    {"start": 0.0, "end": 1.0, "text": "第一"},
    {"start": 1.0, "end": 2.0, "text": "句。"},
]}, {"start": 2.5, "end": 4.0, "text": "第二句", "words": [
    {"start": 2.5, "end": 3.2, "text": "第二"},
    {"start": 3.2, "end": 4.0, "text": "句"},
]}]
ws = sc.flatten_words(segs)
check("词数 4", len(ws) == 4, str(len(ws)))
check("句号那个词 term=True", ws[1]["term"] is True, str(ws[1]))
check("没标点的词 term=False", ws[3]["term"] is False, str(ws[3]))
check("语气词字段没被破坏", all("text" in w and "start" in w for w in ws))

# 句末标点常挂在整段文字尾部（甚至单独成一个词），单看词看不出来
segs3 = [{"start": 0.0, "end": 2.0, "text": "第一句。",
          "words": [{"start": 0.0, "end": 1.0, "text": "第一"},
                    {"start": 1.0, "end": 2.0, "text": "句"}]},
         {"start": 2.5, "end": 4.0, "text": "第二句",
          "words": [{"start": 2.5, "end": 3.0, "text": "第二"},
                    {"start": 3.0, "end": 4.0, "text": "句"}]}]
ws3 = sc.flatten_words(segs3)
check("整段以句号收尾 → 段末词标句界", ws3[1]["term"] is True, str(ws3[1]))
check("下一段没有句号 → 段末词不算句界", ws3[3]["term"] is False, str(ws3[3]))
check("段中词一律不算句界", ws3[0]["term"] is False and ws3[2]["term"] is False)

# 无词级时整段当词，也要带 term
segs2 = [{"start": 0.0, "end": 3.0, "text": "整段话！"}]
ws2 = sc.flatten_words(segs2)
check("整段当词时也带 term", ws2 and ws2[0]["term"] is True and ws2[0].get("whole"),
      str(ws2))


print("\n== 7. 硬切剪口的 6ms 音频微淡 ==")

from backend import renderer

f = renderer._cut_afade(2.0)
check("2s 的段带 in/out 两道 afade",
      "afade=t=in:st=0:d=0.006" in f and "afade=t=out:st=1.994:d=0.006" in f, f)
check("太短的段不加（会整段静音）", renderer._cut_afade(0.01) == "",
      renderer._cut_afade(0.01))
check("长度缺失时不加", renderer._cut_afade(0) == "" and renderer._cut_afade(None) == "")
# 微淡时长必须是运行时可关的（默认值写在函数签名里会绑死，测试/临时关闭都改不动）
check("显式传 0 能关掉微淡", renderer._cut_afade(2.0, 0) == "", renderer._cut_afade(2.0, 0))
check("显式传 10ms 就按 10ms 算", "d=0.010" in renderer._cut_afade(2.0, 0.01),
      renderer._cut_afade(2.0, 0.01))

# 拼接时真的把滤镜塞进 filter_complex
seen = {}


def fake_xff(exe, args, what, cancel_flag=None):
    seen["args"] = args
    # 造一个空文件让上层不报「没拼出来」
    out = args[-1]
    with open(out, "wb") as fh:
        fh.write(b"\0")


orig_xff = renderer._xff
try:
    renderer._xff = fake_xff
    renderer._concat_plain("ffmpeg", ["a.mp4", "b.mp4", "c.mp4"], "o.mp4", True,
                           afmt="aformat=sample_rates=48000:channel_layouts=mono",
                           lengths=[1.0, 2.0, 3.0])
finally:
    renderer._xff = orig_xff

fc = seen["args"][seen["args"].index("-filter_complex") + 1]
check("第 1 段只按 1.0s 算尾巴", "afade=t=out:st=0.994" in fc, fc)
check("第 2 段按 2.0s 算", "afade=t=out:st=1.994" in fc, fc)
check("第 3 段按 3.0s 算", "afade=t=out:st=2.994" in fc, fc)
check("每段都有 3 组淡入", fc.count("afade=t=in:st=0:d=0.006") == 3, fc)


print("\n== 8. 精调包不能沾上微淡（要给剪映留干净素材） ==")

import inspect
from backend import exporter

src_clips = inspect.getsource(exporter.export_clips)
check("切素材不重编码音频以外的处理（无 afade）", "afade" not in src_clips,
      "export_clips 里出现了 afade")
check("切素材仍是 stream copy 语义（有 -c:v libx264 / -c:a aac）",
      "libx264" in src_clips and "aac" in src_clips)
check("renderer 的微淡常量没被 exporter 引用", "CUT_AFADE" not in inspect.getsource(exporter))


print("\n== 9. 「按需求剪」这条链路也拿得到词表 ==")

import copy
from backend import director, subtitle

_CANNED = [{"start": 0.8, "end": 6.0, "text": "今天介绍一下。",
            "words": [{"start": 0.8, "end": 2.0, "text": "今天介绍"},
                      {"start": 2.0, "end": 6.0, "text": "一下"}]}]
_orig = subtitle.transcribe
try:
    subtitle.transcribe = lambda *a, **k: copy.deepcopy(_CANNED)
    _ana = {"duration": 20.48, "has_audio": True, "scenes": [], "redundant": [],
            "static": [], "blank": [], "silent": [{"start": 6.1, "end": 7.5}]}
    _out = director._with_speech(
        _ana, {"speech": {"on": True, "kinds": ["filler"]}, "strength": "standard"},
        "fake.mp4", 20.48)
finally:
    subtitle.transcribe = _orig

_sp = _out.get("speech") or {}
check("_with_speech 把词表挂上了", bool(_sp.get("words")), str(list(_sp)))
check("词表带 term 标记", (_sp.get("words") or [{}])[-1].get("term") is True,
      str((_sp.get("words") or [{}])[-1]))
check("没词覆盖的一侧不乱猜（仍走固定 pad）",
      autocut.decide(_out)["cut_ranges"] == [[6.35, 7.25]],
      str(autocut.decide(_out)["cut_ranges"]))

print("\n" + "=" * 40)
if FAIL:
    print(f"通过 {OK} 项，失败 {len(FAIL)} 项：")
    for n in FAIL:
        print("  -", n)
    sys.exit(1)
print(f"通过 {OK} 项，全部通过")
