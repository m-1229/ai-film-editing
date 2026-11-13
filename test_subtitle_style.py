# -*- coding: utf-8 -*-
"""口播专项第三步 · 字幕样式与词时间对齐 —— 纯逻辑，秒级，不需要视频/模型

守三件事：
A. ASS 样式：描边色真的写进 OutlineColour（BGR 顺序不能错）、底框不透明度映射、
   逐字点亮用的是 libass 原生 karaoke（{\\kf} 只挂在 Dialogue 上，不逐条生成 Dialogue）
B. 词时间对齐：cue 文本是 segment 文本切出来的，按「去标点去空白的字」顺序消费词表；
   切点落在词中间要按字符占比插值；对不上就**退化成整句显示**，绝不错位
C. server._sub_style 透传：新键进得去、老键行为不变

跑法： D:/Python311/python.exe test_subtitle_style.py
"""

import re
import sys

from backend import server
from backend import subtitle as st

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


# ---------------- 工具 ----------------

def style_fields(ass):
    """Style 行 → 字段列表（按 Format 的顺序）"""
    for line in ass.split("\n"):
        if line.startswith("Style:"):
            return [x.strip() for x in line[len("Style:"):].split(",")]
    return []


# Style 行字段下标（和 [V4+ Styles] 的 Format 对齐）
F_NAME, F_FONT, F_SIZE, F_PRIMARY, F_SECONDARY, F_OUTLINE_COL, F_BACK = 0, 1, 2, 3, 4, 5, 6
F_BORDER, F_OUTLINE = 15, 16


def dialogues(ass):
    return [ln for ln in ass.split("\n") if ln.startswith("Dialogue:")]


def kf_marks(ass):
    """取出每条 Dialogue 的 \\kf 厘秒序列"""
    out = []
    for ln in dialogues(ass):
        text = ln.split(",,", 1)[1] if ",," in ln else ""
        out.append([int(x) for x in re.findall(r"\{\\kf(\d+)\}", text)])
    return out


def W(s, e, t):
    return {"start": s, "end": e, "text": t}


def seg(s, e, text, words=None):
    d = {"start": s, "end": e, "text": text}
    if words is not None:
        d["words"] = words
    return d


def words_ok(cue):
    """一条 cue 的 words 是否是「干净」的：落在区间内、单调不交叠、有正时长"""
    ws = cue.get("words") or []
    if not ws:
        return False, "没有 words"
    cs, ce = float(cue["start"]), float(cue["end"])
    prev_end = cs
    for w in ws:
        s, e = float(w["start"]), float(w["end"])
        if s < cs - 1e-6 or e > ce + 1e-6:
            return False, f"词 {s}-{e} 不在 cue {cs}-{ce} 里"
        if e <= s:
            return False, f"词 {s}-{e} 时长非正"
        if s < prev_end - 1e-6:
            return False, f"词 {s} 压到了前一个词的尾巴 {prev_end}"
        prev_end = e
    return True, ""


def main():
    print("== 1. 描边色写进 OutlineColour（ASS 是 BGR 顺序） ==")
    cues = [{"start": 0.0, "end": 1.0, "text": "描边测试"}]

    ass = st.build_ass(cues, 1920, 1080, {"outline_color": "#FF0000"})
    f = style_fields(ass)
    check("红色描边 → &H000000FF（BGR 顺序）", f[F_OUTLINE_COL] == "&H000000FF", f[F_OUTLINE_COL])

    ass = st.build_ass(cues, 1920, 1080, {"outline_color": "#00FF00"})
    check("绿色描边 → &H0000FF00", style_fields(ass)[F_OUTLINE_COL] == "&H0000FF00",
          style_fields(ass)[F_OUTLINE_COL])

    ass = st.build_ass(cues, 1920, 1080, {"outline_color": "#0000FF"})
    check("蓝色描边 → &H00FF0000", style_fields(ass)[F_OUTLINE_COL] == "&H00FF0000",
          style_fields(ass)[F_OUTLINE_COL])

    # 不传就还是老的黑色描边，逐字不变
    check("不传描边色 = 老行为（&H00000000）", style_fields(st.build_ass(cues))[F_OUTLINE_COL]
          == "&H00000000", style_fields(st.build_ass(cues))[F_OUTLINE_COL])

    # outline=0 → Outline 字段 0.0（libass 就不画描边了）
    check("outline=0 → Outline 字段 0.0",
          style_fields(st.build_ass(cues, 1920, 1080, {"outline": 0}))[F_OUTLINE] == "0.0",
          style_fields(st.build_ass(cues, 1920, 1080, {"outline": 0}))[F_OUTLINE])

    print("\n== 2. 底框不透明度 → ASS alpha（越小越不透明） ==")
    for pct, want in ((62, 0x61), (0, 0xFF), (100, 0x00), (50, 0x80), (80, 0x33)):
        ass = st.build_ass(cues, 1920, 1080, {"box": True, "box_alpha": pct})
        f = style_fields(ass)
        check(f"box_alpha={pct} → BackColour &H{want:02X}000000",
              f[F_BACK] == f"&H{want:02X}000000", f[F_BACK])
        check(f"box_alpha={pct} → BorderStyle=3", f[F_BORDER] == "3", f[F_BORDER])

    check("默认 62 → &H61000000",
          style_fields(st.build_ass(cues, 1920, 1080, {"box": True}))[F_BACK]
          == "&H61000000", style_fields(st.build_ass(cues, 1920, 1080, {"box": True}))[F_BACK])
    check("不开底框 → BorderStyle=1 且 BackColour &H00000000",
          style_fields(st.build_ass(cues))[F_BORDER] == "1"
          and style_fields(st.build_ass(cues))[F_BACK] == "&H00000000",
          style_fields(st.build_ass(cues)))

    print("\n== 3. 逐字点亮：PrimaryColour=已读(=高亮)，SecondaryColour=未读 ==")
    # 本项目 ffmpeg 实测：karaoke 未念到的字用 Secondary，念到的用 Primary。
    # 所以「已读=高亮色」必须写在 Primary 上（截图证据见提交报告）。
    kcue = {"start": 0.0, "end": 2.0, "text": "今天聊聊",
            "words": [W(0.0, 0.8, "今天"), W(0.8, 2.0, "聊聊")]}
    ass = st.build_ass([kcue], 1920, 1080, {"highlight": True})
    f = style_fields(ass)
    check("PrimaryColour = 高亮色 #FFD54A（已读）", f[F_PRIMARY] == "&H004AD5FF", f[F_PRIMARY])
    check("SecondaryColour = 文字色 #FFFFFF（未读）", f[F_SECONDARY] == "&H00FFFFFF",
          f[F_SECONDARY])

    ass = st.build_ass([kcue], 1920, 1080,
                       {"highlight": True, "highlight_color": "#00FF00", "color": "#EEEEEE"})
    f = style_fields(ass)
    check("自定义高亮色 → PrimaryColour", f[F_PRIMARY] == "&H0000FF00", f[F_PRIMARY])
    check("自定义文字色 → SecondaryColour", f[F_SECONDARY] == "&H00EEEEEE", f[F_SECONDARY])

    check("不开 highlight → 还是老的两色（Primary=文字色 / Secondary=&H000000FF）",
          style_fields(st.build_ass([kcue]))[F_PRIMARY] == "&H00FFFFFF"
          and style_fields(st.build_ass([kcue]))[F_SECONDARY] == "&H000000FF",
          style_fields(st.build_ass([kcue])))

    print("\n== 4. \\kf 厘秒总和必须精确等于 cue 时长（误差 ≤ 0.02s） ==")
    cases = [
        ("两词", {"start": 1.0, "end": 2.5, "text": "今天聊聊",
                  "words": [W(1.0, 1.6, "今天"), W(1.6, 2.5, "聊聊")]}),
        ("三词带零头", {"start": 0.33, "end": 1.97, "text": "一二三",
                        "words": [W(0.33, 0.61, "一"), W(0.61, 1.03, "二"),
                                  W(1.03, 1.97, "三")]}),
        ("末词不到 cue 尾（余量补末词）", {"start": 0.0, "end": 2.0, "text": "甲乙",
                                          "words": [W(0.0, 0.5, "甲"), W(0.5, 1.2, "乙")]}),
        ("5 个词各 0.07s（舍入最容易出错）", {"start": 0.0, "end": 0.35, "text": "一二三四五",
                                            "words": [W(0.0, 0.07, "一"), W(0.07, 0.14, "二"),
                                                      W(0.14, 0.21, "三"), W(0.21, 0.28, "四"),
                                                      W(0.28, 0.35, "五")]}),
    ]
    for name, cue in cases:
        ass = st.build_ass([cue], 1920, 1080, {"highlight": True})
        marks = kf_marks(ass)
        total = sum(marks[0]) if marks else 0
        dur = cue["end"] - cue["start"]
        check(f"{name}：\\kf 总和 {total}cs ≈ {dur:.2f}s",
              marks and abs(total / 100.0 - dur) <= 0.02,
              f"{marks} vs {dur}")
        check(f"{name}：词数 = \\kf 个数", marks and len(marks[0]) == len(
            [w for w in cue["words"] if w["text"].strip()]), marks)

    print("\n== 5. 没有词时间就绝不出现 \\kf（自动降级） ==")
    ass = st.build_ass([{"start": 0.0, "end": 2.0, "text": "整句显示"}], 1920, 1080,
                       {"highlight": True})
    check("无 words → 没有 \\kf", "\\kf" not in ass, dialogues(ass))
    check("无 words → 文字原样出现", "整句显示" in ass)
    check("无 words → 仍然只有一条 Dialogue", len(dialogues(ass)) == 1, dialogues(ass))
    check("无 words 时开不开 highlight 的 ASS 逐字一致（不会整句变黄）",
          ass == st.build_ass([{"start": 0.0, "end": 2.0, "text": "整句显示"}], 1920, 1080),
          ass)

    ass = st.build_ass([{"start": 0.0, "end": 2.0, "text": "坏词表",
                         "words": [W(5.0, 6.0, "坏词")]}], 1920, 1080, {"highlight": True})
    check("词时间全在 cue 外面 → 不用（宁可不亮）", "\\kf" not in ass, dialogues(ass))

    # 一条能亮、一条没有词时间：没词那条要用行内 \c 压回文字色
    mixed = st.build_ass([
        {"start": 0.0, "end": 2.0, "text": "能亮的一条",
         "words": [W(0.0, 1.0, "能亮的"), W(1.0, 2.0, "一条")]},
        {"start": 2.5, "end": 4.0, "text": "不能亮的一条"},
    ], 1920, 1080, {"highlight": True})
    dl = dialogues(mixed)
    check("混合：一条有 \\kf、一条没有", "\\kf" in dl[0] and "\\kf" not in dl[1], dl)
    check("混合：没词那条用行内 \\c 回到文字色（白色 &HFFFFFF&）",
          "{\\c&HFFFFFF&}" in dl[1], dl[1])
    check("混合：有词那条不带行内 \\c", "\\c" not in dl[0].split(",,", 1)[1].split("}")[0], dl[0])

    print("\n== 6. 词时间对齐（8 组样例） ==")

    # 6.1 正常句子：标点一致
    cues = st.cues_from_transcription(
        [seg(0.0, 3.0, "今天聊聊口播剪辑。",
             [W(0.0, 0.5, "今天"), W(0.5, 1.0, "聊聊"), W(1.0, 1.8, "口播"),
              W(1.8, 2.8, "剪辑"), W(2.8, 3.0, "。")])],
        max_chars=20, with_words=True)
    check("正常句子：一条 cue 一个整句", len(cues) == 1, cues)
    ok, why = words_ok(cues[0])
    check("正常句子：words 落在区间内且单调", ok, why)
    check("正常句子：纯标点词不占时间", len(cues[0].get("words") or []) == 4,
          cues[0].get("words"))

    # 6.2 标点不一致（半角/全角、多标点、没标点）
    cues = st.cues_from_transcription(
        [seg(0.0, 3.0, "第一句, 第二句; 第三句",
             [W(0.0, 1.0, "第一句"), W(1.0, 2.0, "第二句"), W(2.0, 3.0, "第三句")])],
        max_chars=20, with_words=True)
    ok, why = words_ok(cues[0])
    check("标点不一致：仍能对上（半角标点被忽略）", ok and len(cues[0].get("words") or []) == 3,
          why)

    # 6.3 切点落在词中间：一个词被两条 cue 共用
    txt = "今天我们一起来聊聊口播剪辑里的常见问题"
    wl = [W(0.0, 0.5, "今天"), W(0.5, 1.0, "我们"), W(1.0, 1.5, "一起"),
          W(1.5, 2.0, "来聊聊"), W(2.0, 2.5, "口播"), W(2.5, 3.0, "剪辑里"),
          W(3.0, 3.5, "的"), W(3.5, 4.5, "常见问题")]
    cues = st.cues_from_transcription([seg(0.0, 4.5, txt, wl)], max_chars=6, with_words=True)
    check("超长句被切成多条", len(cues) >= 2, [c["text"] for c in cues])
    ok_all = all(words_ok(c)[0] for c in cues)
    check("切点落在词中：每条 words 都干净", ok_all,
          [(c["text"], words_ok(c)[1]) for c in cues])
    shared = [w["text"] for c in cues for w in (c.get("words") or []) if w["text"] == "口播"]
    check("被切开的词两条 cue 各取一部分", len(shared) == 2, shared)

    # 6.4 单字 cue（whisper 偶尔一个字一个字地吐，比如一句「嗯」单独成段）
    txt1 = "嗯我们今天聊聊"
    cues = st.cues_from_transcription([
        seg(0.0, 0.6, "嗯", [W(0.0, 0.6, "嗯")]),
        # 间隔 0.6s > 聚合阈值 0.45s，两条不会被并成一句
        seg(1.2, 3.2, "我们今天聊聊", [W(1.2, 1.8, "我们"), W(1.8, 2.4, "今天"),
                                      W(2.4, 3.2, "聊聊")]),
    ], max_chars=16, with_words=True)
    single = [c for c in cues if len(st._sig(c["text"])) == 1]
    check("能拿到单字 cue", len(single) >= 1, [c["text"] for c in cues])
    check("单字 cue 也挂得上词", all(c.get("words") for c in single),
          [(c["text"], c.get("words")) for c in single])
    check("单字 cue 的 words 只有 1 个词", all(len(c["words"]) == 1 for c in single),
          [(c["text"], c.get("words")) for c in single])
    check("单字 cue 的 words 也是干净的", all(words_ok(c)[0] for c in single),
          [(c["text"], words_ok(c)[1]) for c in single])
    check("单字 cue 的 \\kf 总和都等于各自时长",
          all(abs(sum(m) / 100.0 - (c["end"] - c["start"])) <= 0.02
              for m, c in zip(kf_marks(st.build_ass(cues, 1920, 1080, {"highlight": True})),
                              cues)),
          kf_marks(st.build_ass(cues, 1920, 1080, {"highlight": True})))
    check("单字 cue 之后的整句也对得上（跨 segment 不串行）",
          all(words_ok(c)[0] for c in cues), [(c["text"], words_ok(c)[1]) for c in cues])
    check("单字 cue 的文本没被别的段污染", any(c["text"] == "嗯" for c in cues), txt1)

    # 6.5 词表只有一半 → 整份降级，绝不错位
    cues = st.cues_from_transcription(
        [seg(0.0, 4.0, "第一句说完了。第二句也说完。",
             [W(0.0, 2.0, "第一句说完了")])],
        max_chars=8, with_words=True)
    check("词表只有一半 → 一条都不挂（宁可不高亮）",
          all(not c.get("words") for c in cues), [c.get("words") for c in cues])
    check("词表只有一半 → 文字还在", all(c.get("text") for c in cues), cues)

    # 6.6 空词表 / 没有 words 字段（句级转写）
    cues = st.cues_from_transcription([seg(0.0, 2.0, "句级转写的结果")],
                                      max_chars=20, with_words=True)
    check("句级转写 → 不挂 words 也不报错", cues and not cues[0].get("words"), cues)
    check("句级转写 → 文字正常", cues and cues[0]["text"] == "句级转写的结果", cues)

    # 6.7 英文混排
    cues = st.cues_from_transcription(
        [seg(0.0, 2.0, "我们用 AI 剪辑 video 的要点",
             [W(0.0, 0.6, "我们"), W(0.6, 0.8, "用"), W(0.8, 1.2, " AI"),
              W(1.2, 1.6, "剪辑"), W(1.6, 2.0, " video 的要点")])],
        max_chars=40, with_words=True)
    check("英文混排：挂上 5 个词", len(cues[0].get("words") or []) == 5, cues[0].get("words"))
    ok, why = words_ok(cues[0])
    check("英文混排：words 干净", ok, why)
    ass = st.build_ass(cues, 1920, 1080, {"highlight": True})
    check("英文混排：\\kf 总和仍等于 cue 时长",
          abs(sum(kf_marks(ass)[0]) / 100.0 - 2.0) <= 0.02, kf_marks(ass))
    check("英文混排：英文词之间补回空格", " AI" in ass or "AI" in ass, dialogues(ass))

    # 6.8 用户手改过文字（词表对不上）→ 降级
    cues = st.attach_words(
        [{"start": 0.0, "end": 3.0, "text": "完全换掉的一整句文案"}],
        [seg(0.0, 3.0, "原来的话", [W(0.0, 1.5, "原来的"), W(1.5, 3.0, "话")])])
    check("词表和字幕对不上 → 不挂（绝不错位）", not cues[0].get("words"), cues[0])

    print("\n== 7. attach_words 不改入参、可重复调用 ==")
    src = [{"start": 0.0, "end": 2.0, "text": "今天聊聊",
            "words": [W(0.0, 1.0, "今天"), W(1.0, 2.0, "聊聊")]}]
    base = [{"start": 0.0, "end": 2.0, "text": "今天聊聊"}]
    out1 = st.attach_words(base, src)
    out2 = st.attach_words(base, src)
    check("入参没被写脏", "words" not in base[0], base[0])
    check("两次结果一致", out1 == out2, (out1, out2))
    check("有词时间的 cue 能被识别", st.has_word_times(out1) and not st.has_word_times(base))

    print("\n== 8. _sub_style 透传（新键进得去，老键不变） ==")
    old = server._sub_style({"size": "large", "position": "top", "color": "#FFD700",
                             "box": True, "outline": 3})
    check("老键：size_ratio/position/color/box/outline 都在",
          old.get("size_ratio") == 0.068 and old.get("position") == "top"
          and old.get("color") == "#FFD700" and old.get("box") is True
          and old.get("outline") == 3, old)
    check("没传新键 → style 里不出现它们（老请求零差异）",
          not ({"outline_color", "box_alpha", "highlight", "highlight_color"} & set(old)), old)

    new = server._sub_style({"on": True, "outline_color": "#112233", "box_alpha": 40,
                             "highlight": True, "highlight_color": "#00FF00"})
    check("outline_color 透传", new.get("outline_color") == "#112233", new)
    check("box_alpha 透传并夹到 0–100", new.get("box_alpha") == 40.0, new)
    check("highlight 透传", new.get("highlight") is True, new)
    check("highlight_color 透传", new.get("highlight_color") == "#00FF00", new)
    check("box_alpha 越界被夹紧",
          server._sub_style({"box_alpha": 999}).get("box_alpha") == 100.0
          and server._sub_style({"box_alpha": -5}).get("box_alpha") == 0.0,
          server._sub_style({"box_alpha": 999}))
    check("box_alpha 传垃圾值不炸也不塞进去",
          "box_alpha" not in server._sub_style({"box_alpha": "abc"}),
          server._sub_style({"box_alpha": "abc"}))
    check("highlight=False 就不塞（保持老 ASS）",
          "highlight" not in server._sub_style({"highlight": False}),
          server._sub_style({"highlight": False}))

    print("\n== 9. 画幅配置解析（老请求零回归） ==")
    from backend import renderer

    check("不传 → None", server._frame_cfg(None) is None, server._frame_cfg(None))
    check('"origin" → None', server._frame_cfg("origin") is None, server._frame_cfg("origin"))
    check('{"mode":"origin"} → None', server._frame_cfg({"mode": "origin"}) is None,
          server._frame_cfg({"mode": "origin"}))
    check('"blur" → {"mode":"blur"}', server._frame_cfg("blur") == {"mode": "blur"},
          server._frame_cfg("blur"))
    check('{"mode":"crop"} → {"mode":"crop"}', server._frame_cfg({"mode": "crop"})
          == {"mode": "crop"}, server._frame_cfg({"mode": "crop"}))
    check("乱写的值 → None（当没传）", server._frame_cfg({"mode": "zoom"}) is None,
          server._frame_cfg({"mode": "zoom"}))
    check("renderer 也认同一套",
          renderer.normalize_frame({"mode": "blur"}) == "blur"
          and renderer.normalize_frame("origin") is None
          and renderer.normalize_frame(None) is None,
          (renderer.normalize_frame({"mode": "blur"}), renderer.normalize_frame("origin")))

    print("\n== 10. _render_options 新老字段 ==")
    opts, problems = server._render_options({}, [{"start": 0, "end": 1, "text": "x"}])
    check("不传 effects → frame=None / denoise=False（老行为）",
          opts.get("frame") is None and opts.get("denoise") is False, opts)
    check("不传 effects → 没有 problems", problems == [], problems)

    opts, _ = server._render_options({"frame": "blur", "denoise": True}, [])
    check("frame/denoise 解析进 opts", opts["frame"] == {"mode": "blur"}
          and opts["denoise"] is True, opts)

    # ---------------- 11. 折行（本机 libass 不给中文折行，只能自己插 \N） ----------------
    print("\n== 11. 竖屏折行（libass 没有 CJK 断行能力）==")
    BASE = "这是自动转写断句出来的一行字幕大概有这么长的句子"   # 24 字
    VW, VH = 1080, 1920
    per_v = st.line_capacity(VW, 106)      # 1920*0.055 ≈ 106
    per_h = st.line_capacity(1920, 59)     # 1080*0.055 ≈ 59
    check("1080 竖屏一行容量 11 字（每字按 0.80*font_size 算）", per_v == 11, per_v)
    check("1920 横屏一行容量 38 字（MarginL/R 固定 60，不随画面宽缩放）", per_h == 38, per_h)
    check("容量下限是 4（画面再窄也不会一字一行）", st.line_capacity(200, 400) == 4,
          st.line_capacity(200, 400))

    def rows_of(text, w, h, ratio=0.055):
        font = max(14, int(round(h * ratio)))
        return st._wrap_plain(text, st.line_capacity(w, font)).split("\\N")

    for n in (12, 14, 18, 24):
        rows = rows_of(BASE[:n], VW, VH)
        ws = [len(r) for r in rows]
        check(f"竖屏 {n} 字：{len(rows)} 行、每行都不超容量、字数不丢",
              sum(ws) == n and max(ws) <= per_v, ws)
        check(f"竖屏 {n} 字：各行字数差 ≤ 2（不出 17+1）", max(ws) - min(ws) <= 2, ws)
    check("竖屏 24 字 = 3 行（ceil(24/11)，不到上限）",
          len(rows_of(BASE, VW, VH)) == 3, rows_of(BASE, VW, VH))
    check("竖屏 11 字（= 容量）不折行",
          len(rows_of(BASE[:11], VW, VH)) == 1, rows_of(BASE[:11], VW, VH))
    check("竖屏 8 字不折行", len(rows_of(BASE[:8], VW, VH)) == 1, rows_of(BASE[:8], VW, VH))

    # ---- R1：导入 SRT 常有 40–60 字一行，行数天花板必须够 ----
    LONG = BASE * 3                                   # 72 字，够切到 60
    check("行数上限已从 3 提到 5", st.WRAP_MAX_ROWS == 5, st.WRAP_MAX_ROWS)
    for n in (36, 48, 60):
        rows = rows_of(LONG[:n], VW, VH)
        ws = [len(r) for r in rows]
        want = -(-n // per_v)                          # ceil(total/per)
        check(f"竖屏 {n} 字：行数 = ceil({n}/11) = {want}（≤5）",
              len(rows) == min(want, st.WRAP_MAX_ROWS), (len(rows), ws))
        check(f"竖屏 {n} 字：每行都不超 12 字（12×80=960px，刚好等于可用宽度）",
              max(ws) <= 12 and sum(ws) == n, ws)
        check(f"竖屏 {n} 字：各行字数差 ≤ 2", max(ws) - min(ws) <= 2, ws)
    check("竖屏 60 字不会被压成 3 行（旧行为：每行 20 字 → 顶满切边）",
          len(rows_of(LONG[:60], VW, VH)) == 5, rows_of(LONG[:60], VW, VH))

    # ---- R2：没有空格的超长 token（网址 / 长单词）按**宽度**硬切 ----
    URL = "https://example.com/a/very/long/path/without/any/spaces"[:40]
    check("40 字符连写没有空格", " " not in URL and len(URL) == 40, URL)
    urows = st._wrap_plain(URL, per_v).split("\\N")
    check("40 字符连写：硬切成多行（不再整行溢出）", len(urows) >= 2, urows)
    check("40 字符连写：每行宽度都 ≤ per（按宽度算，不是按字符数）",
          all(st._unit_width(r) <= per_v + 1e-9 for r in urows),
          [(r, round(st._unit_width(r), 2)) for r in urows])
    check("40 字符连写：拼回去与原文逐字一致", "".join(urows) == URL, urows)
    check("短单词不受硬切影响（internationalization 在横屏一行放得下）",
          "\\N" not in st._wrap_plain("internationalization", per_h),
          st._wrap_plain("internationalization", per_h))

    # ---- R2b：宽字形（W/M/@/m）不能用 0.55 估——QA 实测 W=1.08 个汉字宽 ----
    for ch, want in (("W", 1.10), ("M", 1.05), ("@", 1.05), ("m", 1.00), ("n", 0.65),
                     ("a", 0.60), ("t", 0.45), ("i", 0.30), ("l", 0.30)):
        got = st._unit_width(ch)
        check(f"半角宽度表：{ch!r} ≥ {want}（实测取上界，不许低估）", got >= want,
              f"{got} < {want}")
    check("汉字仍按 1.0（折行口径没被宽度表带偏）",
          st._unit_width("中") == 1.0 and st._unit_width("，") == 1.0,
          (st._unit_width("中"), st._unit_width("，")))
    wide_cases = ("MMMMMM.MMMMMM.COM", "WWW.MCDONALDS.COM/ORDER",
                  "www.wowowow.com/watch?v=abcdefghijk",
                  "https://www.wowowow.com/watch?v=12345")
    for t in wide_cases:
        wr = st._wrap_plain(t, per_v).split("\\N")
        check(f"宽字形长串「{t[:14]}…」折行后每行 ≤ per、拼回一致",
              all(st._unit_width(r) <= per_v + 1e-9 for r in wr) and "".join(wr) == t,
              [(r, round(st._unit_width(r), 2)) for r in wr])
    check("17 个 M 真的触发了折行（旧宽度模型下连折行都不触发）",
          len(st._wrap_plain("MMMMMM.MMMMMM.COM", per_v).split("\\N")) >= 2,
          st._wrap_plain("MMMMMM.MMMMMM.COM", per_v))
    # unit_capacity 的标定约束：最宽字形连续 unit_capacity(per) 个必须放得下一行
    mc_v = st.unit_capacity(per_v)
    check("连续 10 个 W（最宽字形）放得下一行",
          st._unit_width("W" * mc_v) <= per_v + 1e-9 and mc_v == 10,
          (mc_v, round(st._unit_width("W" * mc_v), 2)))
    check("连续 10 个 W 渲染宽度 < 可用宽度 960px（按每汉字 80.2px 折算）",
          st._unit_width("W" * mc_v) * 80.2 < 960, round(st._unit_width("W" * mc_v) * 80.2, 1))
    check("11 个 W 就会折行（容量边界不多给）",
          len(st._wrap_plain("W" * 11, per_v).split("\\N")) >= 2,
          st._wrap_plain("W" * 11, per_v))

    # ---- R3：中文后的空格并进前一个单元，续行不以空格开头 ----
    MIX = "这个功能叫 auto cut 很好用"
    mrows = st._wrap_plain(MIX, per_v).split("\\N")
    check("中英混排：折了行", len(mrows) >= 2, mrows)
    check("中英混排：续行不以空格开头（居中不会偏半格）",
          all(not r.startswith(" ") for r in mrows[1:]), mrows)
    check("中英混排：拼回去与原文逐字一致", "".join(mrows) == MIX, mrows)
    check("中英混排：没有纯空格的折行单元",
          not [u for u in st._text_units(MIX, per_v) if not u[0].strip()],
          [u[0] for u in st._text_units(MIX, per_v)])
    check("纯英文仍然行尾留空格（QA 断言的行为不许破）",
          all(r.endswith(" ") for r in st._wrap_plain(
              "this is a fairly long english subtitle line", 10).split("\\N")[:-1]),
          st._wrap_plain("this is a fairly long english subtitle line", 10))

    # 横屏零回归：18/24 字都放得下，一个 \N 都不许插，Dialogue 与老输出逐字一致
    cue18 = [{"start": 0.3, "end": 2.6, "text": BASE[:18]}]
    ass_h = st.build_ass(cue18, 1920, 1080, {"size_ratio": 0.055})
    check("横屏 1920 · 18 字不折行（Dialogue 里没有 \\N）",
          dialogues(ass_h) and "\\N" not in dialogues(ass_h)[0], dialogues(ass_h))
    check("横屏 1920 · 18 字 Dialogue 与改动前逐字一致",
          dialogues(ass_h) == ["Dialogue: 0,0:00:00.30,0:00:02.60,Default,,0,0,0,,"
                               + BASE[:18]], dialogues(ass_h))
    ass_h24 = st.build_ass([{"start": 0.3, "end": 2.6, "text": BASE}], 1920, 1080,
                           {"size_ratio": 0.055})
    check("横屏 1920 · 24 字（软上限）也不折行",
          dialogues(ass_h24) and "\\N" not in dialogues(ass_h24)[0], dialogues(ass_h24))
    ass_h36 = st.build_ass([{"start": 0.3, "end": 2.6, "text": LONG[:36]}], 1920, 1080,
                           {"size_ratio": 0.055})
    check("横屏 1920 · 36 字仍不折行（临界点没被 R1 抬走）",
          dialogues(ass_h36) and "\\N" not in dialogues(ass_h36)[0], dialogues(ass_h36))
    check("横屏 1920 · 36 字 Dialogue 逐字等于原文",
          dialogues(ass_h36) == ["Dialogue: 0,0:00:00.30,0:00:02.60,Default,,0,0,0,,"
                                 + LONG[:36]], dialogues(ass_h36))
    ass_h40 = st.build_ass([{"start": 0.3, "end": 2.6, "text": LONG[:40]}], 1920, 1080,
                           {"size_ratio": 0.055})
    check("横屏 1920 · 40 字仍然折行（临界点还在 38）",
          dialogues(ass_h40) and "\\N" in dialogues(ass_h40)[0], dialogues(ass_h40))
    check("横屏 1920 · 40 字折成 2 行 20+20",
          dialogues(ass_h40) and
          [len(x) for x in dialogues(ass_h40)[0].split(",", 9)[9].split("\\N")] == [20, 20],
          dialogues(ass_h40))

    # 英文整词不拆：切点只落在空格
    latin = "this is a fairly long english subtitle line"
    lrows = st._wrap_plain(latin, 10).split("\\N")
    check("英文按词折行，不从词中间断开（每行的词都能在原文里找到）",
          len(lrows) > 1 and all(set(r.split()) <= set(latin.split()) for r in lrows), lrows)
    check("英文折行只切在空格后面（末尾的空格 libass 不渲染，不影响观感）",
          all(r.endswith(" ") or i == len(lrows) - 1 for i, r in enumerate(lrows)), lrows)

    # 折行 + 逐字点亮：\N 只切在词块之间，\kf 总和不变
    def dtext(ln):
        """Dialogue 行 → 纯 Text（Layer/Start/End/Style/Name/MarginL/R/V/Effect 共 9 个逗号）"""
        return ln.split(",", 9)[9] if ln.count(",") >= 9 else ""

    texts = ["这是", "自动转写", "断句出来", "的一行字幕", "大概有这么长"]
    cs, ce = 4.2, 10.2
    step = (ce - cs) / len(texts)
    ws_list = [{"start": cs + i * step, "end": cs + (i + 1) * step, "text": t}
               for i, t in enumerate(texts)]
    kcue = {"start": cs, "end": ce, "text": "".join(texts), "words": ws_list}
    kass = st.build_ass([kcue], VW, VH, {"size_ratio": 0.055, "highlight": True})
    kbody = dtext(dialogues(kass)[0])
    ksum = sum(int(x) for x in re.findall(r"\{\\kf(\d+)\}", kbody))
    check("折行 + 逐字点亮：\\kf 厘秒总和仍精确等于 cue 时长（误差 ≤0.02s）",
          abs(ksum / 100.0 - (ce - cs)) <= 0.02, f"{ksum}cs vs {(ce - cs) * 100:.0f}cs")
    check("折行 + 逐字点亮：\\N 没落进 {} 覆盖块里",
          not re.search(r"\{[^{}]*\\N[^{}]*\}", kbody), kbody)
    kvis = re.sub(r"\{[^{}]*\}", "", kbody.replace("\\N", ""))
    check("折行 + 逐字点亮：真的折了 2 行、可视字一个没丢（21 字）",
          len(kbody.split("\\N")) == 2 and len(kvis) == 21, (kbody, len(kvis)))
    krows = [len(re.sub(r"\{[^{}]*\}", "", r)) for r in kbody.split("\\N")]
    check("折行 + 逐字点亮：各行字数差 ≤ 2", max(krows) - min(krows) <= 2, krows)
    check("折行 + 逐字点亮：词块没被 \\N 劈开（每行都以 } 收尾的完整词块结构）",
          all(r.count("{") == r.count("}") for r in kbody.split("\\N")), kbody)

    # 没词时间的那条：行内 \c 只用一次，且在最前面
    kass2 = st.build_ass([kcue, {"start": 11.0, "end": 13.0, "text": BASE[:18]}],
                         VW, VH, {"size_ratio": 0.055, "highlight": True})
    d2 = dialogues(kass2)
    check("混合场景：没词时间那条先压 \c 再折行（行内覆盖仍在开头）",
          len(d2) == 2 and dtext(d2[1]).startswith("{\\c") and
          dtext(d2[1]).count("{\\c") == 1, dtext(d2[1]) if len(d2) > 1 else d2)

    # 原文自带换行：\N 保留，且不会叠成两个
    nl = st.build_ass([{"start": 0, "end": 2, "text": "第一行\n第二行"}], 1920, 1080, {})
    check("原文里的换行照旧变成 \\N（不叠成空行）",
          "第一行\\N第二行" in dialogues(nl)[0] and "\\N\\N" not in dialogues(nl)[0],
          dialogues(nl))

    print("\n" + "=" * 40)
    if FAIL:
        print(f"通过 {OK} 项，失败 {len(FAIL)} 项：")
        for n in FAIL:
            print("  -", n)
        sys.exit(1)
    print(f"通过 {OK} 项，全部通过")


if __name__ == "__main__":
    main()
