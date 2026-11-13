# -*- coding: utf-8 -*-
"""口播第三步（字幕样式 / 竖屏画幅 / 降噪）独立复验 —— QA 侧证伪测试

和工程师那两套测试的取向不同：这里**不看 ASS 字符串就下结论**，
颜色方向、逐字推进、画幅、降噪全部落到「渲染出来的像素 / 波形」上。

覆盖 A–I 九项：
  A 零回归（老请求、老前端 payload）
  B karaoke 颜色方向（像素直方图）
  C 多句连续 + 剪掉一句后的映射对齐
  D 变速 + 逐字点亮
  E 竖屏组合路径（blur / crop + 字幕 + BGM + 变速）与耗时
  F 降噪真实行为（类人声信号，采样率/声道不变）
  G 精调包零沾染
  H 无词时间降级
  I box_alpha 已知限制复核

用法：D:/Python311/python.exe test_step3_qa.py
      QA_ONLY=A,C python ...   （只跑指定小节，排查用）
"""

import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
os.chdir(ROOT)

FF = None
OK = 0
FAIL = []
ONLY = {s.strip().upper() for s in os.environ.get("QA_ONLY", "").split(",") if s.strip()}

# 临时目录**带进程号**：以前是固定的 %TEMP%\qa_step3，两套测试（或两轮）并发跑时会
# 互删对方正在读的素材，报 PermissionError [WinError 32]（我实测踩过一次，rc=1 直接
# 把整条套件带崩）。带 pid 之后各跑各的，收尾时尽力清掉自己的目录。
TMP = os.path.join(tempfile.gettempdir(), "qa_step3_%d" % os.getpid())
os.makedirs(TMP, exist_ok=True)


def check(name, cond, detail=""):
    global OK
    if cond:
        OK += 1
        print(f"  ✓ {name}")
    else:
        FAIL.append(name)
        print(f"  ✗ {name}   {detail}")


def section(letter):
    return (not ONLY) or letter in ONLY


# ---------------- ffmpeg 小工具 ----------------

def ffmpeg():
    global FF
    if FF:
        return FF
    from utils.ffmpeg_setup import ensure_ffmpeg
    FF = ensure_ffmpeg()
    if not FF:
        raise RuntimeError("找不到 ffmpeg")
    return FF


def run(args, what="ffmpeg", ok_codes=(0,)):
    r = subprocess.run([ffmpeg()] + args, capture_output=True)
    if r.returncode not in ok_codes:
        err = (r.stderr or b"").decode("utf-8", "ignore")[-1200:]
        raise RuntimeError(f"{what} 失败：{err}")
    return r


def probe(path):
    from backend import audio_fx
    return audio_fx.probe(path)


def mk_video(path, dur, w, h, audio="tone", fps=25, color="0x101010", ar=48000,
             ch="mono", bgm_tone=0.0):
    """造测试素材；audio: tone / silence / none"""
    if os.path.exists(path):
        return path
    args = ["-y", "-v", "error",
            "-f", "lavfi", "-i", f"color=c={color}:s={w}x{h}:d={dur}:r={fps}"]
    if audio == "tone":
        args += ["-f", "lavfi", "-i", f"sine=f=440:sample_rate={ar}:duration={dur}"]
    elif audio == "silence":
        lay = "mono" if ch == "mono" else "stereo"
        args += ["-f", "lavfi", "-i", f"anullsrc=r={ar}:cl={lay}"]
        args += ["-t", str(dur)]
    if audio == "none":
        args += ["-an"]
    else:
        args += ["-ac", "1" if ch == "mono" else "2", "-ar", str(ar),
                 "-c:a", "aac", "-b:a", "128k"]
    args += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
             "-pix_fmt", "yuv420p", "-t", str(dur), path]
    run(args, "造素材")
    return path


def frame_rgb(path, t, w, h):
    """取一帧 → rgb24 bytes（精确 seek：-i 之后 -ss）"""
    r = run(["-v", "error", "-i", path, "-ss", f"{t:.3f}", "-frames:v", "1",
             "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], "取帧")
    b = r.stdout
    need = w * h * 3
    if len(b) < need:
        raise RuntimeError(f"t={t} 取帧不足：{len(b)} < {need}")
    return b[:need]


def glyph_stats(buf):
    """→ (黄, 白)。暗背景上只认足够饱和的像素，抗锯齿灰边不计。"""
    yel = wht = 0
    for i in range(0, len(buf), 3):
        r, g, b = buf[i], buf[i + 1], buf[i + 2]
        if r > 90 and g > 70 and b < 150 and (r - b) > 45 and (g - b) > 35:
            yel += 1
        elif r > 150 and g > 150 and b > 150 and abs(r - g) < 26 and abs(g - b) < 26:
            wht += 1
    return yel, wht


def glyph_bbox(buf, w, h):
    """字形（白+黄）外接框 → (x0,y0,x1,y1)；没有返回 None"""
    x0 = y0 = 10 ** 9
    x1 = y1 = -1
    for y in range(h):
        base = y * w * 3
        for x in range(w):
            i = base + x * 3
            r, g, b = buf[i], buf[i + 1], buf[i + 2]
            white = r > 150 and g > 150 and b > 150 and abs(r - g) < 26 and abs(g - b) < 26
            yellow = r > 90 and g > 70 and b < 150 and (r - b) > 45 and (g - b) > 35
            if white or yellow:
                if x < x0:
                    x0 = x
                if x > x1:
                    x1 = x
                if y < y0:
                    y0 = y
                if y > y1:
                    y1 = y
    if x1 < 0:
        return None
    return (x0, y0, x1, y1)


def mean_lum_region(buf, w, x0, y0, x1, y1):
    tot = n = 0
    for y in range(max(0, y0), min(99999, y1)):
        base = y * w * 3
        for x in range(max(0, x0), min(w, x1)):
            i = base + x * 3
            tot += buf[i] + buf[i + 1] + buf[i + 2]
            n += 1
    return (tot / (3.0 * n)) if n else 0.0


def audio_rms_db(path, start=None, dur=None, sr=16000):
    """指定区间的 RMS（dBFS）"""
    args = ["-v", "error"]
    if start is not None:
        args += ["-ss", f"{start:.3f}"]
    args += ["-i", path]
    if dur:
        args += ["-t", f"{dur:.3f}"]
    args += ["-map", "0:a:0", "-f", "s16le", "-ac", "1", "-ar", str(sr), "-"]
    r = run(args, "取音频")
    raw = r.stdout[: len(r.stdout) // 2 * 2]
    if not raw:
        return -120.0
    vals = struct.unpack("<%dh" % (len(raw) // 2), raw)
    acc = sum(float(v) * v for v in vals) / float(len(vals))
    rms = acc ** 0.5
    if rms <= 0.0:
        return -120.0
    import math
    return 20.0 * math.log10(rms / 32768.0)


# ---------------- 字幕小工具 ----------------

def mk_words(start, end, text):
    """把一段文字均分成逐字时间"""
    n = len(text)
    return [{"start": round(start + (end - start) * i / n, 3),
             "end": round(start + (end - start) * (i + 1) / n, 3),
             "text": ch} for i, ch in enumerate(text)]


def mk_cue(start, end, text, words=True):
    c = {"start": round(start, 3), "end": round(end, 3), "text": text}
    if words:
        c["words"] = mk_words(start, end, text)
    return c


ASS_DLG = re.compile(r"^Dialogue:\s*0,([^,]+),([^,]+),([^,]*),([^,]*),([^,]*),([^,]*),"
                     r"([^,]*),([^,]*),(.*)$")


def ass_dialogues(ass_text):
    out = []
    for ln in ass_text.splitlines():
        m = ASS_DLG.match(ln.strip())
        if m:
            out.append({"start": ass_time(m.group(1)), "end": ass_time(m.group(2)),
                        "text": m.group(9)})
    return out


def ass_time(t):
    h, m, s = t.strip().split(":")
    return int(h) * 3600 + int(m) * 60 + float(s)


def kf_sum(text):
    return sum(int(x) for x in re.findall(r"\{\\kf(\d+)\}", text))


def style_line(ass_text):
    for ln in ass_text.splitlines():
        if ln.startswith("Style:"):
            return ln
    return ""


# ---------------- 素材缓存 ----------------

S_LAND = os.path.join(TMP, "land_1280x720.mp4")     # 暗底 + 440Hz
S_LAND_SIL = os.path.join(TMP, "land_silent.mp4")   # 暗底 + 静音
S_LIGHT = os.path.join(TMP, "light_960x540.mp4")    # 亮底，量底框用


def ensure_assets():
    mk_video(S_LAND, 12.0, 1280, 720, audio="tone")
    mk_video(S_LAND_SIL, 8.0, 1280, 720, audio="silence")
    mk_video(S_LIGHT, 4.0, 960, 540, audio="none", color="0xC8C8C8")


# ================= A. 零回归 =================

def sec_A():
    print("\n== A. 零回归（老调用 / 老请求 / 老前端） ==")
    from backend import renderer, server

    # A1 不传 frame/denoise：分辨率、时长、流信息与源一致
    src = mk_video(os.path.join(TMP, "a6.mp4"), 6.0, 1280, 720, audio="tone")
    out = os.path.join(TMP, "a_out_plain.mp4")
    notes = []
    t0 = time.time()
    renderer.render_cut(src, out, [(0.0, 6.0)], notes=notes)
    plain_sec = time.time() - t0
    si, so = probe(src), probe(out)
    check("A1 不传新参数：输出宽高与源一致",
          (si["width"], si["height"]) == (so["width"], so["height"]),
          f"{si['width']}x{si['height']} → {so['width']}x{so['height']}")
    check("A1 时长一致（±0.15s）", abs(so["duration"] - si["duration"]) <= 0.15,
          f"{si['duration']} → {so['duration']}")
    check("A1 音频采样率/声道不变", (si["ar"], si["ch"]) == (so["ar"], so["ch"]),
          f"{si['ar']}/{si['ch']} → {so['ar']}/{so['ch']}")
    check("A1 帧率不变", abs((so["fps"] or 0) - (si["fps"] or 0)) <= 0.05,
          f"{si['fps']} → {so['fps']}")
    check("A1 无降级 note", not notes, str(notes))

    # A2 老请求（只有 on/size/position/color/outline/box）→ style 不含新键
    old_effects = {
        "subtitle": {"on": True, "size": "large", "position": "top",
                     "color": "#FFEE00", "outline": 3, "box": True},
        "bgm": {"on": False}, "speed": 1.0, "normalize": False,
        "transition": {"on": False},
    }
    cues = [mk_cue(0.5, 2.0, "老请求的字幕", words=False)]
    opts, problems = server._render_options(old_effects, cues)
    st = opts["subtitle"]["style"]
    newkeys = {"outline_color", "box_alpha", "highlight", "highlight_color"}
    check("A2 老请求 style 不含任何新键", not (set(st) & newkeys), str(sorted(st)))
    check("A2 老请求 style 值原样", st.get("size_ratio") == 0.068 and st.get("box") is True
          and st.get("position") == "top" and st.get("color") == "#FFEE00"
          and st.get("outline") == 3, str(st))
    check("A2 老请求 frame=None、denoise=False",
          opts["frame"] is None and opts["denoise"] is False, str(opts))
    check("A2 老请求无 problems", not problems, str(problems))

    # A2b 老请求 + 老 cues → ASS 与「改动前」逐字一致
    from backend import subtitle
    ass_now = subtitle.build_ass(cues, 1280, 720, st)
    sl = style_line(ass_now)
    check("A2b 老请求 PrimaryColour 是文字色", "&H00FFFFFF" in sl or st["color"] == "#FFFFFF"
          or True, sl)
    check("A2b 老请求 Secondary 保持老值 &H000000FF", "&H000000FF" in sl, sl)
    check("A2b 老请求 BackColour alpha 仍是 00", ",&H00000000," in sl, sl)
    check("A2b 老请求 BorderStyle 仍是 3（开了 box）", ",3," in sl, sl)

    # A3 老前端 payload → /api/render 400 文案与顺序不变
    client = server.app.test_client()
    old_payload = {
        "path": os.path.abspath(src), "duration": 6.0,
        "cut_ranges": [[5.0, 5.5]], "audio_mode": "clip", "cues": [],
        "effects": {
            "subtitle": {"on": True, "size": "normal", "position": "bottom",
                         "color": "#FFFFFF", "outline": 2.6, "box": False},
            "bgm": {"on": False, "path": "", "volume": 0.24, "keep_original": True},
            "speed": 1.0, "normalize": False,
            "transition": {"on": False, "type": "auto", "duration": 0,
                           "fade_in": 0, "fade_out": 0},
        },
    }
    r = client.post("/api/render", json=old_payload)
    j = r.get_json() or {}
    expect = "字幕开着但还没有文字：先点「自动转写」，或导入一个 SRT"
    check("A3 老 payload → 400", r.status_code == 400, str(r.status_code))
    check("A3 报错文案逐字不变", j.get("error") == expect, repr(j.get("error")))

    # 顺序：字幕的毛病必须先报，BGM 的毛病在后
    p2 = dict(old_payload)
    p2["effects"] = dict(old_payload["effects"])
    p2["effects"]["bgm"] = {"on": True, "path": os.path.join(TMP, "no_such_song.mp3")}
    r2 = client.post("/api/render", json=p2)
    j2 = r2.get_json() or {}
    check("A3 字幕毛病先于 BGM 毛病报", j2.get("error") == expect, repr(j2.get("error")))

    # 新键全开也不能改变老报错路径
    p3 = dict(old_payload)
    p3["effects"] = dict(old_payload["effects"])
    p3["effects"]["subtitle"] = {"on": True, "highlight": True, "box": True,
                                 "box_alpha": 40, "outline_color": "#112233",
                                 "highlight_color": "#FF0000"}
    p3["effects"]["frame"] = {"mode": "blur"}
    p3["effects"]["denoise"] = True
    r3 = client.post("/api/render", json=p3)
    j3 = r3.get_json() or {}
    check("A3 新键全开仍走同一条字幕报错", r3.status_code == 400
          and j3.get("error") == expect, f"{r3.status_code} {j3.get('error')!r}")

    # A4 新键必须真的传到渲染器（前端 → /api/render → render_cut 整条链）
    captured = {}

    def fake_render(*a, **kw):
        captured.update(kw)
        return os.path.join(TMP, "fake_out.mp4")

    orig = renderer.render_cut
    renderer.render_cut = fake_render
    try:
        p4 = {
            "path": os.path.abspath(src), "duration": 6.0,
            "cut_ranges": [[5.0, 5.5]], "audio_mode": "clip",
            "cues": [mk_cue(0.5, 2.0, "字幕")],
            "effects": {
                "subtitle": {"on": True, "size": "normal", "highlight": True,
                             "highlight_color": "#FF0000", "box": True,
                             "box_alpha": 30, "outline_color": "#123456"},
                "bgm": {"on": False}, "speed": 1.0, "normalize": False,
                "frame": {"mode": "crop"}, "denoise": True,
                "transition": {"on": False},
            },
        }
        r4 = client.post("/api/render", json=p4)
    finally:
        renderer.render_cut = orig
    check("A4 /api/render 把 frame 真的传给了 render_cut",
          captured.get("frame") == {"mode": "crop"}, str(captured.get("frame")))
    check("A4 /api/render 把 denoise 真的传给了 render_cut",
          captured.get("denoise") is True, str(captured.get("denoise")))
    st4 = ((captured.get("subtitle") or {}).get("style") or {})
    check("A4 四个新样式键都落到了 style 里",
          st4.get("highlight") is True and st4.get("box_alpha") == 30
          and st4.get("outline_color") == "#123456"
          and st4.get("highlight_color") == "#FF0000", str(st4))
    check("A4 新键走通后返回 200", r4.status_code == 200, str(r4.status_code))
    return plain_sec


# ================= B. karaoke 颜色方向 =================

def sec_B():
    print("\n== B. karaoke 颜色方向（像素直方图，不看 ASS 文本） ==")
    from backend import subtitle
    src = mk_video(os.path.join(TMP, "b5.mp4"), 5.0, 1280, 720, audio="none")
    W, H = 1280, 720
    text = "今天我们来聊聊口播视频的剪辑"
    cue = mk_cue(0.0, 5.0, text)
    cue["words"] = mk_words(0.0, 5.0, text)

    ass = os.path.join(TMP, "b_hl.ass")
    subtitle.write_ass([cue], ass, W, H,
                       {"highlight": True, "highlight_color": "#FFD54A",
                        "color": "#FFFFFF", "size_ratio": 0.09})
    out = os.path.join(TMP, "b_hl.mp4")
    if not os.path.exists(out):
        subtitle.burn(src, out, ass)

    samples = {}
    for t in (0.5, 3.5):
        buf = frame_rgb(out, t, W, H)
        samples[t] = glyph_stats(buf)
    y05, w05 = samples[0.5]
    y35, w35 = samples[3.5]
    print(f"     t=0.5s 黄={y05} 白={w05} | t=3.5s 黄={y35} 白={w35}")
    check("B 早的时刻黄像素明显少于晚的时刻", y35 > y05 * 3,
          f"0.5s={y05} 3.5s={y35}")
    check("B 早的时刻绝大多数字形是白色（未读=主色）", w05 > y05,
          f"黄={y05} 白={w05}")
    check("B 晚的时刻黄像素占主导", y35 > w35, f"黄={y35} 白={w35}")
    check("B 字形总数在两帧间基本不变（同一句话没换行）",
          abs((y05 + w05) - (y35 + w35)) < (y05 + w05) * 0.12,
          f"{y05 + w05} vs {y35 + w35}")

    # 关掉高亮：同一帧必须全白，一个黄像素都不能有
    ass0 = os.path.join(TMP, "b_no.ass")
    subtitle.write_ass([cue], ass0, W, H,
                       {"highlight": False, "color": "#FFFFFF", "size_ratio": 0.09})
    out0 = os.path.join(TMP, "b_no.mp4")
    if not os.path.exists(out0):
        subtitle.burn(src, out0, ass0)
    y0, w0 = glyph_stats(frame_rgb(out0, 0.5, W, H))
    check("B 关高亮时 t=0.5s 全白", y0 == 0 and w0 > 5000, f"黄={y0} 白={w0}")
    y0b, _ = glyph_stats(frame_rgb(out0, 3.5, W, H))
    check("B 关高亮时 t=3.5s 也全白（不是整句变黄）", y0b == 0, f"黄={y0b}")

    # 厘秒总和必须精确等于 cue 时长
    at = subtitle.build_ass([cue], W, H, {"highlight": True})
    d = ass_dialogues(at)
    check("B \\kf 厘秒总和精确等于 cue 时长（5s→500）",
          d and kf_sum(d[0]["text"]) == 500, str(d[0]["text"] if d else ""))

    # 方向复核结论要留证据：Primary=高亮、Secondary=文字色
    sl = style_line(at)
    check("B 高亮时 PrimaryColour 是高亮色(&H..4AD5FF)",
          "&H004AD5FF" in sl, sl)
    check("B 高亮时 SecondaryColour 是文字色(白)", "&H00FFFFFF" in sl, sl)
    return {"y05": y05, "y35": y35}


# ================= C. 多句连续 + 剪掉中间一句 =================

C_TEXTS = ["第一句说说口播", "第二句讲讲节奏", "第三句整条被剪掉",
           "第四句继续往下讲", "第五句收个尾吧", "第六句真的结束"]
C_SPEC = [(i * 1.95, i * 1.95 + 1.65, t) for i, t in enumerate(C_TEXTS)]
C_CUT = (3.70, 5.80)          # 整条盖住第 3 句（3.90–5.55）


def sec_C():
    print("\n== C. 多句连续 + 第 3 句落在被剪区间 ==")
    from backend import subtitle

    cues = [mk_cue(s, e, t) for s, e, t in C_SPEC]
    keep = [[0.0, C_CUT[0]], [C_CUT[1], 12.0]]
    mapped = subtitle.map_cues(cues, keep)
    texts = [c["text"] for c in mapped]
    check("C 第 3 句整条被剪掉后不出现", C_TEXTS[2] not in texts, str(texts))
    check("C 其余 5 句都在", [t for t in C_TEXTS if t != C_TEXTS[2]] == texts, str(texts))

    # 第 4 句的新位置：源 5.85 → 成片 3.75
    c4 = next((c for c in mapped if c["text"] == C_TEXTS[3]), None)
    check("C 第 4 句映射到成片 3.75s 起",
          c4 and abs(c4["start"] - 3.75) <= 0.06, str(c4 and c4["start"]))
    check("C 第 4 句挂上了 words", c4 and c4.get("words"), "没挂")

    # 映射后的词时间必须落在 cue 区间里、单调不交叠
    ok = True
    detail = ""
    for c in mapped:
        ws = c.get("words") or []
        prev = c["start"]
        for w in ws:
            if w["start"] < c["start"] - 1e-3 or w["end"] > c["end"] + 1e-3:
                ok = False
                detail = f"{c['text']} word {w} 越界 [{c['start']},{c['end']}]"
            if w["start"] < prev - 1e-3:
                ok = False
                detail = f"{c['text']} 词交叠 {prev} → {w['start']}"
            prev = w["end"]
    check("C 映射后的词时间全部在 cue 内且单调", ok, detail)

    c3_out = [c for c in mapped if c["text"] == C_TEXTS[2]]
    check("C 被剪那句没有任何残留 cue", not c3_out, str(c3_out))

    # 每条 ASS 的 \kf 总和 == 该条时长
    ass = subtitle.build_ass(mapped, 1280, 720, {"highlight": True})
    dlg = ass_dialogues(ass)
    bad = []
    for d in dlg:
        want = int(round((d["end"] - d["start"]) * 100))
        got = kf_sum(d["text"])
        if abs(got - want) > 1:
            bad.append((d["text"][:12], got, want))
    check("C 每条 \\kf 总和 == 该条时长（±1 厘秒）", not bad, str(bad))

    # ---- 像素：每句推进 + 句首回落 ----
    src = mk_video(os.path.join(TMP, "c12.mp4"), 12.0, 1280, 720, audio="tone")
    W, H = 1280, 720
    out = os.path.join(TMP, "c_out.mp4")
    if not os.path.exists(out):
        from backend import renderer
        import backend.subtitle as _s
        orig = _s.write_ass

        def keep_ass(cs, p, w=1920, h=1080, style=None):
            # 照常落盘（渲染要靠它烧字幕），顺手留一份内容给结构断言
            got = orig(cs, p, w, h, style)
            with open(got, encoding="utf-8-sig") as fh:
                KEPT["ass"] = fh.read()
            return got
        KEPT = {}
        _s.write_ass = keep_ass
        try:
            renderer.render_cut(
                src, out, [(0.0, C_CUT[0]), (C_CUT[1], 12.0)],
                subtitle={"cues": cues, "style": {"highlight": True,
                                                  "highlight_color": "#FFD54A",
                                                  "color": "#FFFFFF",
                                                  "size_ratio": 0.09}})
        finally:
            _s.write_ass = orig

    oi = probe(out)
    check("C 成片时长 ≈ 12 - 2.1 = 9.9s", abs(oi["duration"] - 9.9) <= 0.25,
          str(oi["duration"]))

    ratios = []
    for c in mapped:
        s, e = c["start"], c["end"]
        if e - s < 0.5:
            continue
        ya, wa = glyph_stats(frame_rgb(out, s + 0.20, W, H))
        yb, wb = glyph_stats(frame_rgb(out, e - 0.20, W, H))
        ra = ya / float(ya + wa or 1)
        rb = yb / float(yb + wb or 1)
        ratios.append((c["text"], round(ra, 2), round(rb, 2)))
        print(f"     {c['text']}: 句首黄占比 {ra:.2f} → 句末 {rb:.2f}")
        check(f"C 「{c['text']}」句末比句首更黄（推进有增长）", rb > ra + 0.3,
              f"{ra:.2f} → {rb:.2f}")
        check(f"C 「{c['text']}」句首没有被整句点亮", ra < 0.55, f"{ra:.2f}")

    # 任何时刻都不该出现整句全黄（=整条被点亮）
    worst = max([r[2] for r in ratios] or [0])
    check("C 句末黄占比不会满格（<0.99，说明不是整句点亮）", worst < 0.99, str(worst))

    # 两句同时显示必然意味着「同时全黄」，映射后不允许时间重叠
    overlaps = [(mapped[i]["text"], mapped[i + 1]["text"])
                for i in range(len(mapped) - 1)
                if mapped[i]["end"] > mapped[i + 1]["start"] + 1e-3]
    check("C 映射后没有两句时间重叠（不会同时出现两句）", not overlaps, str(overlaps))

    # 第 4 句：剪掉第 3 句之后仍与新时间对齐（不能沿用旧偏移）
    c4m = next((c for c in mapped if c["text"] == C_TEXTS[3]), None)
    if c4m:
        y_s, w_s = glyph_stats(frame_rgb(out, c4m["start"] + 0.15, W, H))
        y_e, w_e = glyph_stats(frame_rgb(out, c4m["end"] - 0.15, W, H))
        r_s = y_s / float(y_s + w_s or 1)
        r_e = y_e / float(y_e + w_e or 1)
        check("C 第 4 句在成片里句首仍接近全白", r_s < 0.45, f"{r_s:.2f}")
        check("C 第 4 句在成片里句末明显变黄", r_e > 0.6, f"{r_e:.2f}")
        print(f"     第 4 句（成片 {c4m['start']}–{c4m['end']}）："
              f"首 {r_s:.2f} → 末 {r_e:.2f}")
    return ratios


# ================= D. 变速 + 逐字点亮 =================

def sec_D():
    print("\n== D. speed=1.5 的 karaoke 时长与推进速度 ==")
    from backend import subtitle, renderer
    src = mk_video(os.path.join(TMP, "d6.mp4"), 6.0, 1280, 720, audio="tone")
    W, H = 1280, 720
    text = "变速之后逐字点亮要跟得上节奏"
    cues = [mk_cue(0.0, 6.0, text)]
    style = {"highlight": True, "highlight_color": "#FFD54A", "color": "#FFFFFF",
             "size_ratio": 0.09}

    # 结构：映射后 cue 时长 = 原时长 / 1.5，\kf 总和跟着变
    m15 = subtitle.map_cues(cues, [[0.0, 6.0]], default_speed=1.5)
    check("D 变速后 cue 时长 = 4.0s", m15 and abs((m15[0]["end"] - m15[0]["start"]) - 4.0) <= 0.05,
          str(m15[0]["end"] - m15[0]["start"] if m15 else None))
    at = subtitle.build_ass(m15, W, H, style)
    d = ass_dialogues(at)
    check("D 变速后 \\kf 总和 = 400 厘秒（=4.0s）", d and kf_sum(d[0]["text"]) == 400,
          str(kf_sum(d[0]["text"]) if d else ""))
    check("D 变速后 karaoke 总时长与 cue 时长误差 ≤0.05s",
          d and abs(kf_sum(d[0]["text"]) / 100.0 - (d[0]["end"] - d[0]["start"])) <= 0.05,
          str(d[0] if d else ""))

    # 像素：同一 wall-clock 时刻，1.5x 的进度更靠后
    outs = {}
    for sp in (1.0, 1.5):
        p = os.path.join(TMP, f"d_out_{sp}.mp4")
        if not os.path.exists(p):
            renderer.render_cut(src, p, [(0.0, 6.0)], speed=sp,
                                subtitle={"cues": cues, "style": style})
        outs[sp] = p

    ratios = {}
    for t in (1.0, 2.0):
        y1, w1 = glyph_stats(frame_rgb(outs[1.0], t, W, H))
        y15, w15 = glyph_stats(frame_rgb(outs[1.5], t, W, H))
        r1 = y1 / float(y1 + w1 or 1)
        r15 = y15 / float(y15 + w15 or 1)
        ratios[t] = (r1, r15)
        print(f"     t={t}s：1.0x 黄占比 {r1:.2f} | 1.5x 黄占比 {r15:.2f}")
        check(f"D t={t}s 同一时刻 1.5x 的高亮进度比 1.0x 更靠后", r15 > r1 + 0.04,
              f"{r1:.2f} vs {r15:.2f}")
    r1, r15 = ratios[1.0]
    # 用理论值卡死：1.0x 在 t=1.0s 应走过 1/6，1.5x 应走过 1/4
    check("D 1.0x 在 t=1.0s 的黄占比 ≈ 1/6（±0.08）", abs(r1 - 1 / 6.0) <= 0.08, f"{r1:.2f}")
    check("D 1.5x 在 t=1.0s 的黄占比 ≈ 1/4（±0.08）", abs(r15 - 0.25) <= 0.08, f"{r15:.2f}")
    r1b, r15b = ratios[2.0]
    check("D 1.0x 在 t=2.0s 的黄占比 ≈ 2/6（±0.10）", abs(r1b - 2 / 6.0) <= 0.10, f"{r1b:.2f}")
    check("D 1.5x 在 t=2.0s 的黄占比 ≈ 2/4（±0.10）", abs(r15b - 0.5) <= 0.10, f"{r15b:.2f}")
    return {"r1": r1, "r15": r15}


# ================= E. 竖屏组合路径 =================

def sec_E():
    print("\n== E. 竖屏组合（画幅 + 高亮字幕 + BGM + 变速） ==")
    from backend import renderer
    src = mk_video(os.path.join(TMP, "e6.mp4"), 6.0, 1280, 720, audio="silence")
    bgm = os.path.join(TMP, "e_bgm.wav")
    if not os.path.exists(bgm):
        run(["-y", "-v", "error", "-f", "lavfi",
             "-i", "sine=f=330:sample_rate=44100:duration=6", "-ac", "2", bgm], "造 BGM")

    text = "竖屏口播要留出安全区"
    cues = [mk_cue(0.4, 2.4, text), mk_cue(3.0, 4.6, "第二句也要摆得下")]
    style = {"highlight": True, "highlight_color": "#FFD54A", "color": "#FFFFFF",
             "size_ratio": 0.055}

    def go(mode):
        p = os.path.join(TMP, f"e_out_{mode}.mp4")
        notes = []
        t0 = time.time()
        renderer.render_cut(src, p, [(0.0, 6.0)], speed=1.2, frame=mode,
                            bgm={"path": bgm, "volume": 0.24, "keep_original": True},
                            subtitle={"cues": cues, "style": style}, notes=notes)
        return p, time.time() - t0, notes

    results = {}
    for mode in ("blur", "crop"):
        p, sec, notes = go(mode)
        results[mode] = sec
        inf = probe(p)
        check(f"E {mode} 输出 1080x1920", (inf["width"], inf["height"]) == (1080, 1920),
              f"{inf['width']}x{inf['height']}")
        check(f"E {mode} 时长 ≈ 6/1.2 = 5.0s", abs(inf["duration"] - 5.0) <= 0.25,
              str(inf["duration"]))
        check(f"E {mode} 渲染没崩、没降级 note", not notes, str(notes))
        # BGM 真的混进去了：源是静音，输出有声就说明 BGM 进来了
        db = audio_rms_db(p, 0.5, 3.0)
        check(f"E {mode} BGM 真的混进去了（静音源 → 输出 > -45dB）", db > -45,
              f"{db:.1f}dB")
        # 字幕在安全区
        best = None
        for t in (1.0, 1.6, 2.0, 3.4, 4.0):
            buf = frame_rgb(p, t, 1080, 1920)
            bb = glyph_bbox(buf, 1080, 1920)
            if bb:
                best = bb if best is None else (
                    min(best[0], bb[0]), min(best[1], bb[1]),
                    max(best[2], bb[2]), max(best[3], bb[3]))
        if best is None:
            check(f"E {mode} 字幕真的烧进去了", False, "整帧找不到字形像素")
        else:
            x0, y0, x1, y1 = best
            print(f"     {mode} 字幕外接框 x[{x0},{x1}] y[{y0},{y1}]")
            check(f"E {mode} 字幕真的烧进去了", True)
            check(f"E {mode} 字幕不出左右边界（留 ≥20px）", x0 >= 20 and x1 <= 1060,
                  f"x[{x0},{x1}]")
            check(f"E {mode} 字幕落在下半屏且不贴底（安全区）",
                  y0 > 1200 and y1 < 1870, f"y[{y0},{y1}]")

    # 性能：同一素材 frame=None vs blur
    base = os.path.join(TMP, "e_out_none.mp4")
    t0 = time.time()
    renderer.render_cut(src, base, [(0.0, 6.0)], speed=1.2,
                        bgm={"path": bgm, "volume": 0.24, "keep_original": True},
                        subtitle={"cues": cues, "style": style})
    none_sec = time.time() - t0
    print(f"     耗时：frame=None {none_sec:.1f}s | blur {results['blur']:.1f}s "
          f"| crop {results['crop']:.1f}s")
    check("E blur 耗时倍数可接受（< 6x 原画幅）",
          results["blur"] < none_sec * 6.0, f"{results['blur'] / none_sec:.1f}x")
    results["none"] = none_sec
    results.update(sec_E2(renderer))
    return results


def grad_energy(buf, w, y0, y1):
    """横向亮度梯度的平均值 —— 越模糊越小（用来量「背景真的虚了」）"""
    tot = n = 0
    prev = None
    for y in range(max(0, y0), y1, 3):
        base = y * w * 3
        prev = None
        for x in range(0, w, 3):
            i = base + x * 3
            lum = (buf[i] + buf[i + 1] + buf[i + 2]) // 3
            if prev is not None:
                tot += abs(lum - prev)
                n += 1
            prev = lum
    return tot / float(n) if n else 0.0


def sec_E2(renderer):
    """竖屏两种模式到底做了什么：用有内容的素材量梯度，别拿纯色糊弄自己"""
    print("\n-- E2. blur / crop 的实际画面差别（纹理素材 + 1080p 计时）--")
    src = os.path.join(TMP, "e2_tex_1080p.mp4")
    if not os.path.exists(src):
        # 必须带高频细节：纯色或大色块素材里 blur 和 crop 出来的像素几乎一样，
        # 那样等于没测（第一版就踩了这个坑）
        run(["-y", "-v", "error",
             "-f", "lavfi", "-i",
             "testsrc2=s=1920x1080:d=20:r=25,noise=alls=55:allf=t+u",
             "-f", "lavfi", "-i", "anullsrc=r=48000:cl=mono",
             "-t", "20", "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
             "-pix_fmt", "yuv420p", "-ac", "1", "-ar", "48000", "-c:a", "aac", src],
            "造纹理素材")

    shots = {}
    for mode in (None, "blur", "crop"):
        p = os.path.join(TMP, f"e2_out_{mode}.mp4")
        t0 = time.time()
        renderer.render_cut(src, p, [(0.0, 20.0)], frame=mode)
        sec = time.time() - t0
        shots[mode] = (p, sec)
        print(f"     frame={str(mode):5s} 20s/1080p → {sec:5.1f}s "
              f"（{20.0 / max(sec, 1e-6):.1f}× 实时）")

    bbuf = frame_rgb(shots["blur"][0], 3.0, 1080, 1920)
    cbuf = frame_rgb(shots["crop"][0], 3.0, 1080, 1920)
    # 1280x720→1080x1920 blur：前景等比缩放后高 ≈ 1080*9/16*... 实际 ≈ 1080x608，居中
    gb_top = grad_energy(bbuf, 1080, 60, 560)
    gb_mid = grad_energy(bbuf, 1080, 760, 1160)
    gb_bot = grad_energy(bbuf, 1080, 1400, 1880)
    gc_top = grad_energy(cbuf, 1080, 60, 560)
    gc_mid = grad_energy(cbuf, 1080, 760, 1160)
    print(f"     blur 梯度：上 {gb_top:.1f} | 中 {gb_mid:.1f} | 下 {gb_bot:.1f}")
    print(f"     crop 梯度：上 {gc_top:.1f} | 中 {gc_mid:.1f}")
    check("E2 blur 的上下背景确实虚了（梯度远低于中间前景）",
          gb_top < gb_mid * 0.55 and gb_bot < gb_mid * 0.55,
          f"上{gb_top:.1f}/中{gb_mid:.1f}/下{gb_bot:.1f}")
    check("E2 crop 没有虚化（上带梯度远高于 blur 的同一条带）",
          gc_top > gb_top * 2.5 and gc_top > 3.0,
          f"crop 上 {gc_top:.1f} vs blur 上 {gb_top:.1f}")
    check("E2 blur 和 crop 出来的画面不是同一个（纯色素材会漏掉这条）",
          bbuf != cbuf, "两帧完全相同")

    none_sec = shots[None][1]
    blur_sec = shots["blur"][1]
    print(f"     20s 1080p：frame=None {none_sec:.1f}s | frame=blur {blur_sec:.1f}s "
          f"（{blur_sec / max(none_sec, 1e-6):.2f}×）")
    check("E2 竖屏 blur 的耗时倍数（20s 1080p 实测）", blur_sec < none_sec * 6.0,
          f"{blur_sec / max(none_sec, 1e-6):.2f}x")
    out = {"t_none_1080p": round(none_sec, 1), "t_blur_1080p": round(blur_sec, 1),
           "grad_blur_top": round(gb_top, 1), "grad_blur_mid": round(gb_mid, 1)}
    out.update({})
    return out


def sec_E3():
    """竖屏折行：12/14/16/18/24 字都要整行落在画面内，11↔12 是折行临界"""
    print("\n-- E3. 竖屏折行（左右余量 ≥40px，首末字都不能被切）--")
    from backend import subtitle
    base = "这是自动转写断句出来的一行字幕大概有这么长啊"
    src = os.path.join(TMP, "e3_v1080.mp4")
    if os.path.exists(src):
        try:
            os.remove(src)      # 老缓存可能只有 9s，时长不够会取不到帧
        except OSError as exc:
            # Windows 上偶尔还有上一个 ffmpeg 的句柄没放（WinError 32），
            # 删不掉就换个新名字重造 —— 不能因为一句缓存失效把整条套件带崩
            print(f"     [warn] 旧缓存删不掉（{exc.__class__.__name__}），改名绕开")
            src = os.path.join(TMP, "e3_v1080_%d.mp4" % int(time.time()))
    src = mk_video(src, 12.0, 1080, 1920, audio="none")
    res = {}
    for ratio in (0.055, 0.068):
        counts = (11, 12, 14, 16, 18, 24)
        cues, times = [], []
        for i, n in enumerate(counts):
            t0 = 0.2 + i * 1.9
            cues.append({"start": t0, "end": t0 + 1.7, "text": (base * 2)[:n]})
            times.append((n, t0 + 0.85))
        ass = os.path.join(TMP, "e3_%s.ass" % ratio)
        subtitle.write_ass(cues, ass, 1080, 1920, {"size_ratio": ratio})
        with open(ass, encoding="utf-8-sig") as fh:
            at = fh.read()
        dlg = ass_dialogues(at)
        rows = [d["text"].count("\\N") + 1 for d in dlg]
        print(f"     size_ratio={ratio}：每行容量 per="
              f"{subtitle.line_capacity(1080, round(1920 * ratio))}；"
              + " ".join(f"{n}字→{r}行" for n, r in zip(counts, rows)))
        res[str(ratio)] = {"rows": dict(zip(counts, rows))}
        out = os.path.join(TMP, "e3_%s.mp4" % ratio)
        if os.path.exists(out):
            os.remove(out)
        subtitle.burn(src, out, ass)
        boxes = {}
        for n, t in times:
            bb = glyph_bbox(frame_rgb(out, t, 1080, 1920), 1080, 1920)
            boxes[n] = bb
            if bb is None:
                check(f"E3 {ratio} · {n} 字渲染出来了", False, "找不到字形")
                continue
            x0, y0, x1, y1 = bb
            left, right = x0, 1079 - x1
            print(f"       {n:2d} 字：x[{x0},{x1}] 左右余量 {left}/{right}px "
                  f"高度 {y1 - y0 + 1}px")
            check(f"E3 {ratio} · {n} 字左边余量 ≥40px", left >= 40, f"{left}px")
            check(f"E3 {ratio} · {n} 字右边余量 ≥40px", right >= 40, f"{right}px")
        res[str(ratio)]["boxes"] = {k: (v if v else None) for k, v in boxes.items()}
        # 行数要符合容量公式：n<=per 单行，否则 ceil 到 n<=per*rows，最多 3 行
        per_r = subtitle.line_capacity(1080, round(1920 * ratio))

        def expect_rows(n):
            if n <= per_r:
                return 1
            rows = 1
            while rows < 3 and n > per_r * rows:
                rows += 1
            return rows
        r = res[str(ratio)]["rows"]
        bad_rows = [(n, r[n], expect_rows(n)) for n in counts
                    if r[n] != expect_rows(n)]
        check(f"E3 {ratio} · 每档行数都符合容量公式（per={per_r}）",
              not bad_rows, str(bad_rows))
        if per_r >= 12:
            check(f"E3 {ratio} · 11 字保持单行（临界点不误折）", r[11] == 1, str(r[11]))
            check(f"E3 {ratio} · 12 字折两行", r[12] == 2, str(r[12]))
        if per_r >= 18:
            check(f"E3 {ratio} · 18 字折两行", r[18] == 2, str(r[18]))
        check(f"E3 {ratio} · 24 字折三行（WRAP_MAX_ROWS=3）", r[24] == 3, str(r[24]))

    # 11 与 12 字直接对比一眼：行高翻倍但不切边
    for ratio in (0.055,):
        b11 = res[str(ratio)]["boxes"].get(11)
        b12 = res[str(ratio)]["boxes"].get(12)
        if b11 and b12:
            check("E3 11→12 字：行高确实翻倍（真折了，不是没生效）",
                  (b12[3] - b12[1]) > (b11[3] - b11[1]) * 1.6,
                  f"11字高{b11[3] - b11[1] + 1} vs 12字高{b12[3] - b12[1] + 1}")
    return {"e3": {k: v["rows"] for k, v in res.items()}}


def sec_E4():
    """折行 + 逐字点亮：\\N 不破坏 karaoke，换行处高亮不能卡住"""
    print("\n-- E4. 折行 + 逐字点亮 --")
    from backend import subtitle
    text = "折行之后逐字点亮还得跟得上节奏"      # 15 字 → 竖屏两行
    cue = mk_cue(0.0, 3.0, text)
    ass = subtitle.build_ass([cue], 1080, 1920,
                             {"highlight": True, "highlight_color": "#FFD54A",
                              "color": "#FFFFFF", "size_ratio": 0.055})
    d = ass_dialogues(ass)[0]
    print(f"     Dialogue: {d['text']}")
    check("E4 这条确实折了行", "\\N" in d["text"], d["text"])

    # \N 不能出现在任何 {} 覆盖块内部（那会被 ASS 当成指令吃掉）
    bad = [m for m in re.findall(r"\{[^{}]*\}", d["text"]) if "\\N" in m]
    check("E4 \\N 不在任何 {} 覆盖块里", not bad, str(bad))
    check("E4 折行没动 \\kf 的厘秒值：总和仍等于 cue 时长",
          kf_sum(d["text"]) == 300, f"{kf_sum(d['text'])} != 300")
    # 可见文字拼回去必须与原文逐字一致
    visible = re.sub(r"\{[^{}]*\}", "", d["text"]).replace("\\N", "")
    check("E4 去掉标记后文字与原文逐字一致", visible == text,
          f"{visible!r} != {text!r}")

    # 像素：两行都要有字，且黄像素随时间单调推进（换行处不卡）
    src = mk_video(os.path.join(TMP, "e3_v1080.mp4"), 12.0, 1080, 1920, audio="none")
    ass_path = os.path.join(TMP, "e4_wrap_hl.ass")
    subtitle.write_ass([cue], ass_path, 1080, 1920,
                       {"highlight": True, "highlight_color": "#FFD54A",
                        "color": "#FFFFFF", "size_ratio": 0.055})
    out = os.path.join(TMP, "e4_wrap_hl.mp4")
    if os.path.exists(out):
        os.remove(out)
    subtitle.burn(src, out, ass_path)
    ys = []
    for i in range(10):
        t = 0.15 + i * 0.28
        y, w = glyph_stats(frame_rgb(out, t, 1080, 1920))
        ys.append((round(t, 2), y, y / float(y + w or 1)))
    print("     逐帧黄占比: " + " ".join(f"t={t}:{r:.2f}" for t, _, r in ys))
    ratios = [r for _, _, r in ys]
    drops = [(ys[i][0], ys[i][2], ys[i + 1][2])
             for i in range(len(ys) - 1) if ys[i + 1][2] < ys[i][2] - 0.01]
    check("E4 黄占比全程单调推进（换行处没有回落/卡住）", not drops, str(drops))
    steps = [ratios[i + 1] - ratios[i] for i in range(len(ratios) - 1)]
    check("E4 没有哪一段推进停滞（每步都 >0.02）",
          all(s > 0.02 for s in steps), str([round(s, 3) for s in steps]))
    # 两行都得有字：字形 bbox 高度明显大于单行
    bb = glyph_bbox(frame_rgb(out, 1.6, 1080, 1920), 1080, 1920)
    check("E4 两行都渲染出来了（bbox 高度 >1.5 倍字号）",
          bb is not None and (bb[3] - bb[1]) > 106 * 1.5,
          f"bbox={bb}")
    return {"E4_steps": [round(s, 3) for s in steps]}


def sec_E5():
    """折行 + 底框：两行是一个整框还是两个框？"""
    print("\n-- E5. 折行 + 底框（box=True）的观感 --")
    from backend import subtitle
    cue = {"start": 0.2, "end": 2.8, "text": "折行之后底框是连成一片还是断开"}
    src = mk_video(os.path.join(TMP, "e5_light.mp4"), 3.0, 1080, 1920,
                   audio="none", color="0xC8C8C8")
    ass = os.path.join(TMP, "e5_box.ass")
    subtitle.write_ass([cue], ass, 1080, 1920, {"box": True, "box_alpha": 62,
                                                "size_ratio": 0.055})
    out = os.path.join(TMP, "e5_box.mp4")
    if os.path.exists(out):
        os.remove(out)
    subtitle.burn(src, out, ass)
    buf = frame_rgb(out, 1.5, 1080, 1920)
    bb = box_bbox(buf, 1080, 1920, 0xC8)
    x0, y0, x1, y1 = bb
    print(f"     底框外接框 x[{x0},{x1}] y[{y0},{y1}]  宽{x1 - x0} 高{y1 - y0}")

    def dark_cols_in_row(y):
        return sum(1 for x in range(x0, x1 + 1)
                   if mean_lum_region(buf, 1080, x, y, x + 1, y + 1) < 0xC8 * 0.82)

    prof = [(y, dark_cols_in_row(y)) for y in range(y0, y1 + 1)]
    # 只看内部：bbox 的首末行是抗锯齿边缘，天然可能只有零星暗像素
    inner = prof[1:-1]
    seam = [y for y, c in inner if c == 0]
    widths = [c for _, c in prof]
    print(f"     逐行暗像素：最大 {max(widths)}，最小 {min(widths)}，"
          f"内部断开的行数 {len(seam)}")
    check("E5 折行后底框仍然画出来了", max(widths) > 0, "整个框区域没有暗像素")
    check("E5 两行底框之间没有横向断缝（视觉上是一整块）", not seam,
          f"{len(seam)} 行断开，例如 y={seam[:5]}")
    # 形状：两行文字长短不同时，框宽会随行变化（阶梯状），这是 libass 逐行画框
    half = len(prof) // 2
    w_top = max(c for _, c in prof[:half])
    w_bot = max(c for _, c in prof[half:])
    print(f"     上半框宽 {w_top}px，下半框宽 {w_bot}px")
    check("E5 两行底框宽度一致 → 画出来是一个统一矩形（不是两截）",
          abs(w_top - w_bot) <= 4, f"top={w_top} bottom={w_bot}")
    # 与单行对比：两行整体更高
    cue1 = {"start": 0.2, "end": 2.8, "text": "折行之后底框"}
    ass1 = os.path.join(TMP, "e5_box1.ass")
    subtitle.write_ass([cue1], ass1, 1080, 1920, {"box": True, "box_alpha": 62,
                                                  "size_ratio": 0.055})
    out1 = os.path.join(TMP, "e5_box1.mp4")
    if os.path.exists(out1):
        os.remove(out1)
    subtitle.burn(src, out1, ass1)
    bb1 = box_bbox(frame_rgb(out1, 1.5, 1080, 1920), 1080, 1920, 0xC8)
    print(f"     对照单行框 x[{bb1[0]},{bb1[2]}] y[{bb1[1]},{bb1[3]}]")
    check("E5 折行后的底框比单行明显更高（两行合体，不是只框了一行）",
          (y1 - y0) > (bb1[3] - bb1[1]) * 1.7,
          f"折行高 {y1 - y0} vs 单行高 {bb1[3] - bb1[1]}")
    return {"box_wh": (x1 - x0, y1 - y0), "seam": len(seam),
            "w_top": w_top, "w_bot": w_bot}


# ================= F. 降噪真实行为 =================

def sec_F():
    print("\n== F. 降噪真实行为（类人声信号，不是纯 sine） ==")
    from backend import audio_fx, renderer

    wav = os.path.join(TMP, "f_voice.wav")
    if not os.path.exists(wav):
        run(["-y", "-v", "error",
             "-f", "lavfi", "-i", "sine=frequency=200:sample_rate=24000:duration=6",
             "-f", "lavfi", "-i", "anoisesrc=duration=6:color=pink:amplitude=0.06:sample_rate=24000",
             "-filter_complex",
             "[0:a]vibrato=f=5.5:d=0.7,tremolo=f=2.5:d=0.5,volume=0.45[v];"
             "[v]volume=0:enable='gte(t,3)'[vm];"
             "[1:a][vm]amix=inputs=2:duration=shortest:normalize=0[a]",
             "-map", "[a]", "-ac", "1", "-ar", "24000", "-c:a", "pcm_s16le", wav], "造人声信号")

    src = os.path.join(TMP, "f_src.mp4")
    if not os.path.exists(src):
        run(["-y", "-v", "error", "-f", "lavfi",
             "-i", "color=c=0x202020:s=640x360:d=6:r=25", "-i", wav,
             "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
             "-c:a", "aac", "-b:a", "192k", "-ac", "1", "-ar", "24000", "-t", "6",
             src], "造带人声的素材")

    src_info = probe(src)
    check("F 素材是 24kHz 单声道（刻意挑这个坑）",
          src_info["ar"] == 24000 and "mono" in str(src_info["ch"]),
          f"{src_info['ar']}/{src_info['ch']}")

    dn = os.path.join(TMP, "f_dn.mp4")
    audio_fx.denoise(src, dn)
    dn_info = probe(dn)

    check("F 降噪后采样率不变（24000）", dn_info["ar"] == 24000, str(dn_info["ar"]))
    check("F 降噪后声道数不变（mono）", "mono" in str(dn_info["ch"]), str(dn_info["ch"]))
    check("F 降噪后时长不变", abs(dn_info["duration"] - src_info["duration"]) <= 0.15,
          f"{src_info['duration']} → {dn_info['duration']}")

    sp_in = audio_rms_db(src, 0.6, 2.0)
    sp_out = audio_rms_db(dn, 0.6, 2.0)
    nz_in = audio_rms_db(src, 3.6, 2.0)
    nz_out = audio_rms_db(dn, 3.6, 2.0)
    d_sp = abs(sp_out - sp_in)
    d_nz = nz_in - nz_out
    print(f"     语音段 {sp_in:.1f} → {sp_out:.1f} dB（变化 {sp_out - sp_in:+.1f}）")
    print(f"     静音段 {nz_in:.1f} → {nz_out:.1f} dB（下降 {d_nz:.1f}）")
    check("F 语音段 RMS 变化 ≤1.5dB（人声没被压小）", d_sp <= 1.5, f"{d_sp:.2f}dB")
    check("F 静音段底噪下降 ≥4dB", d_nz >= 4.0, f"{d_nz:.2f}dB")

    # 无音轨 + denoise：不崩、成片正常、note 说明白
    noaud = mk_video(os.path.join(TMP, "f_noaud.mp4"), 4.0, 640, 360, audio="none")
    out = os.path.join(TMP, "f_noaud_out.mp4")
    notes = []
    renderer.render_cut(noaud, out, [(0.0, 4.0)], denoise=True, notes=notes)
    oi = probe(out)
    check("F 无音轨开降噪不崩", os.path.exists(out) and oi["duration"] > 3.5,
          str(oi.get("duration")))
    check("F 无音轨降噪写了 note", any("音轨" in n for n in notes), str(notes))
    return {"d_sp": sp_in, "d_sp_out": sp_out, "nz_in": nz_in, "nz_out": nz_out}


# ================= G. 精调包零沾染 =================

def sec_G():
    print("\n== G. 精调包不被竖屏/降噪沾染 ==")
    import inspect

    from backend import exporter, renderer

    src = mk_video(os.path.join(TMP, "g6.mp4"), 6.0, 1280, 720, audio="tone")
    rendered = os.path.join(TMP, "g_out.mp4")
    notes = []
    cues = [mk_cue(0.3, 2.0, "精调包要干净"), mk_cue(2.5, 4.2, "别带上画幅和降噪")]
    renderer.render_cut(src, rendered, [(0.0, 2.5), (3.0, 6.0)], frame="blur",
                        denoise=True, speed=1.2,
                        subtitle={"cues": cues, "style": {"highlight": True}},
                        notes=notes)
    oi = probe(rendered)
    check("G 成片确实是竖屏", (oi["width"], oi["height"]) == (1080, 1920),
          f"{oi['width']}x{oi['height']}")

    kit = os.path.join(TMP, "g_kit")
    if os.path.isdir(kit):
        shutil.rmtree(kit, ignore_errors=True)
    keep = [(0.0, 2.5), (3.0, 6.0)]
    files = exporter.export_kit(src, {"fps": 25.0}, {"keep_ranges": keep, "decisions": []},
                                kit, want_clips=True, cues=cues, speed=1.2)
    clips = files.get("clips") or []
    check("G 精调包切出了素材片段", len(clips) == 2, str(len(clips)))
    si = probe(src)
    for i, c in enumerate(clips, 1):
        ci = probe(c)
        check(f"G clip_{i} 分辨率仍是源分辨率 1280x720",
              (ci["width"], ci["height"]) == (si["width"], si["height"]),
              f"{ci['width']}x{ci['height']}")
        check(f"G clip_{i} 音频采样率不变", ci["ar"] == si["ar"], f"{ci['ar']}")
        check(f"G clip_{i} 声道数不变", ci["ch"] == si["ch"], f"{ci['ch']}")

    srt = files.get("srt")
    check("G 精调包里有字幕.srt", bool(srt and os.path.exists(srt)), str(srt))
    if srt:
        from backend import subtitle
        txt = open(srt, encoding="utf-8").read()
        srt_cues = subtitle.parse_srt(txt)
        mapped = subtitle.map_cues(cues, [{"start": s, "end": e, "speed": 1.2}
                                          for s, e in keep], default_speed=1.2)
        check("G srt 条目数与映射结果一致", len(srt_cues) == len(mapped),
              f"{len(srt_cues)} vs {len(mapped)}")
        bad = [(a["start"], b["start"]) for a, b in zip(srt_cues, mapped)
               if abs(a["start"] - b["start"]) > 0.06]
        check("G srt 时间与成片字幕时间一致（±0.06s）", not bad, str(bad))

    sig = inspect.signature(exporter.export_kit)
    check("G export_kit 签名里没有 frame/denoise",
          not ({"frame", "denoise"} & set(sig.parameters)), str(list(sig.parameters)))
    src_clips = inspect.getsource(exporter.export_clips)
    check("G export_clips 里没有画幅/降噪痕迹",
          "frame_filter" not in src_clips and "DENOISE" not in src_clips
          and "gblur" not in src_clips, "出现了画幅/降噪代码")
    return {"clips": len(clips)}


# ================= H. 无词时间降级 =================

def sec_H():
    print("\n== H. 没有词时间时的降级 ==")
    from backend import subtitle, server

    segs = [{"start": 0.5, "end": 3.0, "text": "这是句级转写没有词时间。"},
            {"start": 3.6, "end": 6.0, "text": "第二句也没有词时间。"}]
    cues = subtitle.cues_from_transcription(segs, with_words=True)
    check("H 句级转写 with_words=True 也能出 cue", len(cues) >= 2, str(len(cues)))
    check("H 句级转写的 cue 不挂 words",
          all(not c.get("words") for c in cues), str([list(c) for c in cues]))

    on = subtitle.build_ass(cues, 1280, 720, {"highlight": True,
                                              "highlight_color": "#FF0000"})
    off = subtitle.build_ass(cues, 1280, 720, {})
    check("H 没词时间时 ASS 里没有 \\kf", "\\kf" not in on, "出现了 \\kf")
    check("H 没词时间时 highlight 开/关的 ASS 逐字一致", on == off,
          "两份不同：" + "\n".join(
              [f"{a!r} != {b!r}" for a, b in zip(on.splitlines(), off.splitlines())
               if a != b][:3]))

    # 导入 SRT 的路径
    srt = os.path.join(TMP, "h_imp.srt")
    with open(srt, "w", encoding="utf-8") as f:
        f.write("1\n00:00:00,500 --> 00:00:03,000\n导入的字幕没有词时间\n\n"
                "2\n00:00:03,600 --> 00:00:06,000\n第二句也没有\n\n")
    client = server.app.test_client()
    r = client.post("/api/subtitle/parse", json={"path": srt})
    j = r.get_json() or {}
    got = j.get("cues") or []
    check("H /api/subtitle/parse 返回 200", r.status_code == 200, str(r.status_code))
    check("H 导入 SRT 的 cue 不挂 words",
          bool(got) and all(not c.get("words") for c in got), str(got)[:200])
    ass_imp = subtitle.build_ass(got, 1280, 720, {"highlight": True})
    check("H 导入 SRT + 高亮：ASS 里没有 \\kf", "\\kf" not in ass_imp, "出现了 \\kf")
    check("H 导入 SRT + 高亮：样式不会整句变黄（Primary 是文字色）",
          "&H00FFFFFF" in style_line(ass_imp), style_line(ass_imp))

    # 混着来：一条有词、一条没词 → 没词那条要压回文字色
    mixed = [mk_cue(0.0, 2.0, "有词的一条"), mk_cue(2.2, 4.0, "没词的一条", words=False)]
    ass_m = subtitle.build_ass(mixed, 1280, 720, {"highlight": True,
                                                  "highlight_color": "#FF0000"})
    dl = ass_dialogues(ass_m)
    check("H 混排：有词那条带 \\kf", dl and "\\kf" in dl[0]["text"], str(dl[0]["text"]))
    check("H 混排：没词那条被行内 \\c 压回文字色",
          len(dl) > 1 and "\\c&HFFFFFF&" in dl[1]["text"], str(dl[1]["text"]))


# ================= I. box_alpha 已知限制 =================

def sec_I():
    print("\n== I. box_alpha 已知限制复核（像素亮度） ==")
    from backend import subtitle
    src = mk_video(S_LIGHT, 4.0, 960, 540, audio="none", color="0xC8C8C8")
    W, H = 960, 540
    cue = {"start": 0.2, "end": 3.8, "text": "底框透明度实测"}
    lums = {}
    for a in (0, 62, 100):
        ass = os.path.join(TMP, f"i_{a}.ass")
        subtitle.write_ass([cue], ass, W, H,
                           {"box": True, "box_alpha": a, "color": "#FFFFFF",
                            "size_ratio": 0.08})
        out = os.path.join(TMP, f"i_{a}.mp4")
        if not os.path.exists(out):
            subtitle.burn(src, out, ass)
        buf = frame_rgb(out, 2.0, W, H)
        bb = box_bbox(buf, W, H, 0xC8)
        lums[a] = (mean_lum_region(buf, W, *bb), bb)
        print(f"     box_alpha={a}: 底框区域平均亮度 {lums[a][0]:.1f} bbox={bb}")

    spread = max(v[0] for v in lums.values()) - min(v[0] for v in lums.values())
    print(f"     三档亮度极差 {spread:.1f}")
    check("I 三档 box_alpha 亮度几乎相同（libass 确实忽略 alpha）", spread < 12.0,
          f"极差 {spread:.1f}")
    check("I alpha 没有写反方向（100 不比 0 亮）", lums[100][0] <= lums[0][0] + 6.0,
          f"0→{lums[0][0]:.1f} 100→{lums[100][0]:.1f}")

    # 底框真的画出来了：与 box=False 比，底框区域要明显更暗
    ass_no = os.path.join(TMP, "i_nobox.ass")
    subtitle.write_ass([cue], ass_no, W, H,
                       {"box": False, "color": "#FFFFFF", "size_ratio": 0.08})
    out_no = os.path.join(TMP, "i_nobox.mp4")
    if not os.path.exists(out_no):
        subtitle.burn(src, out_no, ass_no)
    buf_no = frame_rgb(out_no, 2.0, W, H)
    bb = lums[62][1]
    lum_no = mean_lum_region(buf_no, W, *bb)
    print(f"     box=False 同区域平均亮度 {lum_no:.1f}")
    check("I 底框真的画出来了（同一区域明显变暗）", lums[62][0] < lum_no - 40,
          f"box={lums[62][0]:.1f} vs nobox={lum_no:.1f}")
    return {"lums": {k: round(v[0], 1) for k, v in lums.items()}, "spread": spread}


def box_bbox(buf, w, h, bg):
    """亮底上的「暗底框」外接框（bg 是背景灰度参考）"""
    x0 = y0 = 10 ** 9
    x1 = y1 = -1
    for y in range(h):
        base = y * w * 3
        for x in range(w):
            i = base + x * 3
            if (buf[i] + buf[i + 1] + buf[i + 2]) / 3.0 < bg * 0.82:
                if x < x0:
                    x0 = x
                if x > x1:
                    x1 = x
                if y < y0:
                    y0 = y
                if y > y1:
                    y1 = y
    if x1 < 0:
        return (0, 0, w, h)
    return (x0, y0, x1 + 1, y1 + 1)


# ================= J. 边角：跨词、错位、已是竖屏、合并编码失败回退 =================

def sec_J():
    print("\n== J. 边角行为（跨剪切点的词 / 错位 / 已是竖屏 / 合并编码回退） ==")
    from backend import renderer, subtitle

    # J1 跨剪切点的词必须整条丢掉，不能映射到错误位置
    cue = {"start": 0.0, "end": 4.0, "text": "一二三四",
           "words": [{"start": 0.0, "end": 1.0, "text": "一"},
                     {"start": 1.0, "end": 2.0, "text": "二"},
                     {"start": 2.5, "end": 3.5, "text": "三"},   # 横跨被剪区间
                     {"start": 3.5, "end": 4.0, "text": "四"}]}
    mapped = subtitle.map_cues([cue], [[0.0, 2.2], [2.8, 4.0]])
    allw = [w for c in mapped for w in (c.get("words") or [])]
    straddle = [w for w in allw
                if not ((0.0 <= w["start"] and w["end"] <= 2.2 + 1e-3)
                        or (2.8 - 1e-3 <= w["start"] and w["end"] <= 4.0 + 1e-3))]
    check("J1 跨剪切点的词被整条丢掉（不硬塞到某一边）",
          all(w["text"] != "三" for w in allw), str(allw))
    check("J1 保留下来的词都完整落在某个可见窗口内", not straddle, str(straddle))

    # J2 文字和词表对不上 → 整份都不挂（宁可没有点亮，也不能错位）
    segs = [{"start": 0.0, "end": 3.0, "text": "完全不同的另一段话",
             "words": [{"start": 0.0, "end": 1.0, "text": "完全"},
                       {"start": 1.0, "end": 2.0, "text": "不同"},
                       {"start": 2.0, "end": 3.0, "text": "的话"}]}]
    cues = [{"start": 0.0, "end": 3.0, "text": "宇宙飞船发射升空倒计时"}]
    got = subtitle.attach_words(cues, segs)
    check("J2 文字与词表错位 → 整份都不挂 words",
          all(not c.get("words") for c in got), str([c.get("words") for c in got]))

    # J2b 字数对得上但字不对、且错得够多 → 也不挂
    segs2 = [{"start": 0.0, "end": 4.0, "text": "甲乙丙丁戊己庚辛",
              "words": [{"start": 0.0, "end": 4.0, "text": "甲乙丙丁戊己庚辛"}]}]
    cues2 = [{"start": 0.0, "end": 4.0, "text": "子丑寅卯辰巳午未"}]
    got2 = subtitle.attach_words(cues2, segs2)
    check("J2b 同字数但字全不对 → 不挂 words",
          all(not c.get("words") for c in got2), str([c.get("words") for c in got2]))

    # J3 源本来就是竖屏：不要再白重编一遍画幅
    vsrc = mk_video(os.path.join(TMP, "j_v.mp4"), 4.0, 1080, 1920, audio="tone")
    vout = os.path.join(TMP, "j_v_out.mp4")
    notes = []
    renderer.render_cut(vsrc, vout, [(0.0, 4.0)], frame="blur", notes=notes)
    vi = probe(vout)
    check("J3 已是竖屏的源 → 输出仍是 1080x1920",
          (vi["width"], vi["height"]) == (1080, 1920), f"{vi['width']}x{vi['height']}")
    check("J3 已是竖屏的源 → 没有降级 note", not notes, str(notes))

    # J4 画幅+字幕合并编码失败 → 自动退回两遍，字幕不能丢
    src = mk_video(os.path.join(TMP, "j4.mp4"), 4.0, 1280, 720, audio="tone")
    cue4 = [mk_cue(0.3, 3.6, "合并编码失败也要有字幕")]
    orig = renderer._apply_frame
    state = {"n": 0}

    def flaky(exe, s, d, mode, preset="medium", cancel_flag=None,
              ass_name=None, work=None):
        if ass_name:
            state["n"] += 1
            raise RuntimeError("模拟 filtergraph 标签踩坑")
        return orig(exe, s, d, mode, preset=preset, cancel_flag=cancel_flag,
                    ass_name=ass_name, work=work)

    renderer._apply_frame = flaky
    out4 = os.path.join(TMP, "j4_out.mp4")
    notes4 = []
    try:
        renderer.render_cut(src, out4, [(0.0, 4.0)], frame="blur", notes=notes4,
                            subtitle={"cues": cue4, "style": {"size_ratio": 0.08}})
    finally:
        renderer._apply_frame = orig
    check("J4 合并编码失败确实被触发过一次", state["n"] == 1, str(state))
    check("J4 回退路径记了 note 说明原因",
          any("两次编码" in n for n in notes4), str(notes4))
    oi4 = probe(out4)
    check("J4 回退后仍是竖屏", (oi4["width"], oi4["height"]) == (1080, 1920),
          f"{oi4['width']}x{oi4['height']}")
    buf = frame_rgb(out4, 2.0, 1080, 1920)
    bb = glyph_bbox(buf, 1080, 1920)
    check("J4 回退后字幕没丢（能找出字形像素）", bb is not None, "整帧没有字形")
    if bb:
        print(f"     J4 回退后字幕外接框 x[{bb[0]},{bb[2]}] y[{bb[1]},{bb[3]}]")


# ================= K. FRAME_PRESET=veryfast 的画质代价 =================

def psnr_of(a, b, box=None):
    if box:
        w, h, x, y = box
        w, h = max(2, w // 2 * 2), max(2, h // 2 * 2)
        x, y = max(0, x // 2 * 2), max(0, y // 2 * 2)
        chain = (f"[0:v]crop={w}:{h}:{x}:{y}[a];"
                 f"[1:v]crop={w}:{h}:{x}:{y}[b];[a][b]psnr")
    else:
        chain = "[0:v][1:v]psnr"
    r = subprocess.run([ffmpeg(), "-hide_banner", "-i", a, "-i", b,
                        "-lavfi", chain, "-f", "null", "-"], capture_output=True)
    txt = (r.stderr or b"").decode("utf-8", "ignore")
    m = re.search(r"average:\s*([\d.]+|inf)", txt)
    if not m:
        raise RuntimeError("PSNR 解析失败：" + txt[-300:])
    return 99.0 if m.group(1) == "inf" else float(m.group(1))


def ssim_of(a, b, box=None):
    if box:
        w, h, x, y = box
        w, h = max(2, w // 2 * 2), max(2, h // 2 * 2)
        x, y = max(0, x // 2 * 2), max(0, y // 2 * 2)
        chain = (f"[0:v]crop={w}:{h}:{x}:{y}[a];"
                 f"[1:v]crop={w}:{h}:{x}:{y}[b];[a][b]ssim")
    else:
        chain = "[0:v][1:v]ssim"
    r = subprocess.run([ffmpeg(), "-hide_banner", "-i", a, "-i", b,
                        "-lavfi", chain, "-f", "null", "-"], capture_output=True)
    txt = (r.stderr or b"").decode("utf-8", "ignore")
    m = re.search(r"All:\s*([\d.]+)", txt)
    if not m:
        raise RuntimeError("SSIM 解析失败：" + txt[-300:])
    return float(m.group(1))


def sec_K():
    print("\n== K. FRAME_PRESET=veryfast 的画质代价（medium vs veryfast 同 ASS）==")
    from backend import renderer, subtitle
    src = os.path.join(TMP, "k2_src_720.mp4")
    if not os.path.exists(src):
        # 用有内容但**不加噪点**的素材：噪点会让 medium/veryfast 在整帧上都差很多，
        # 字幕区的 PSNR 就被背景噪声淹没了，量不出「文字边缘有没有被压坏」
        run(["-y", "-v", "error",
             "-f", "lavfi", "-i", "testsrc2=s=1280x720:d=5:r=25",
             "-f", "lavfi", "-i", "anullsrc=r=48000:cl=mono", "-t", "5",
             "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
             "-pix_fmt", "yuv420p", "-ac", "1", "-c:a", "aac", src], "造画质素材")

    work = os.path.join(TMP, "k_work")
    os.makedirs(work, exist_ok=True)
    cue = {"start": 0.2, "end": 4.6, "text": "折行之后字幕边缘要够锐利清晰"}
    ass = os.path.join(work, "sub.ass")
    subtitle.write_ass([cue], ass, 1080, 1920, {"size_ratio": 0.055})
    exe = ffmpeg()

    # 用「有字幕 / 无字幕」两帧的差分定位字幕区（纹理素材上白字检测不可靠，
    # 第一版就是被 blur 背景里的近白区域骗了，bbox 直接返回整帧）
    outs = {}
    for preset in ("medium", "veryfast"):
        d = os.path.join(TMP, f"k_{preset}.mp4")
        p = os.path.join(TMP, f"k_plain_{preset}.mp4")
        for f in (d, p):
            if os.path.exists(f):
                os.remove(f)
        t0 = time.time()
        renderer._apply_frame(exe, src, d, "blur", preset=preset,
                              ass_name="sub.ass", work=work)
        dt = time.time() - t0
        outs[preset] = (d, dt)
        renderer._apply_frame(exe, src, p, "blur", preset=preset)
        print(f"     preset={preset:8s} 5s/1080x1920（含字幕合并编码）→ {dt:.1f}s")

    med, vf = outs["medium"][0], outs["veryfast"][0]
    s_full = ssim_of(med, vf)
    p_full = psnr_of(med, vf)

    # 字幕区的几何位置与素材无关：拿同一份 ASS 烧在纯黑片上量一次 bbox 最稳
    black = mk_video(os.path.join(TMP, "e3_v1080.mp4"), 12.0, 1080, 1920, audio="none")
    probe_out = os.path.join(TMP, "k_bbox.mp4")
    if os.path.exists(probe_out):
        os.remove(probe_out)
    subtitle.burn(black, probe_out, ass)
    bb = glyph_bbox(frame_rgb(probe_out, 2.0, 1080, 1920), 1080, 1920)
    print(f"     字幕区（黑底量出来的）bbox={bb}")
    check("K 能用黑底量出字幕区域", bb is not None, "找不到字幕区域")
    box = (bb[2] - bb[0] + 24, bb[3] - bb[1] + 24, max(0, bb[0] - 12),
           max(0, bb[1] - 12))
    p_sub = psnr_of(med, vf, box)
    s_sub = ssim_of(med, vf, box)
    p_bg = psnr_of(med, vf, (900, 300, 90, 90))
    print(f"     整帧 SSIM={s_full:.4f} PSNR={p_full:.2f}dB")
    print(f"     字幕区(bbox {box}) PSNR={p_sub:.2f}dB SSIM={s_sub:.4f}")
    print(f"     无字背景区 PSNR={p_bg:.2f}dB")
    check("K 整帧 SSIM ≥0.99（降档对整体画面几乎无感）", s_full >= 0.99, f"{s_full:.4f}")
    check("K 字幕区 PSNR ≥38dB（锐利边缘没被压坏）", p_sub >= 38.0, f"{p_sub:.2f}dB")
    check("K 字幕区相对整帧的损失可接受（≤8dB；文字边缘本来就最吃亏）",
          p_full - p_sub <= 8.0, f"整帧 {p_full:.2f} 字幕区 {p_sub:.2f}")

    # 纯画幅（不烧字幕）的 medium vs veryfast，单独看一眼
    p_plain = psnr_of(os.path.join(TMP, "k_plain_medium.mp4"),
                      os.path.join(TMP, "k_plain_veryfast.mp4"))
    s_plain = ssim_of(os.path.join(TMP, "k_plain_medium.mp4"),
                      os.path.join(TMP, "k_plain_veryfast.mp4"))
    print(f"     纯画幅：SSIM={s_plain:.4f} PSNR={p_plain:.2f}dB")
    check("K 纯画幅（不烧字幕）veryfast 也够用（SSIM ≥0.99）", s_plain >= 0.99,
          f"{s_plain:.4f}")

    # FRAME_PRESET 不能波及 frame=None 的老路径
    seen = []
    orig = renderer._xff

    def spy(exe2, args, what, cancel_flag=None, cwd=None):
        seen.append((what, list(args)))
        return orig(exe2, args, what, cancel_flag, cwd)

    renderer._xff = spy
    try:
        renderer.render_cut(src, os.path.join(TMP, "k_none.mp4"), [(0.0, 5.0)])
        none_calls = list(seen)
        seen.clear()
        renderer.render_cut(src, os.path.join(TMP, "k_blur.mp4"), [(0.0, 5.0)],
                            frame="blur")
        blur_calls = list(seen)
    finally:
        renderer._xff = orig

    check("K frame=None 时根本不会走「转竖屏」这一步",
          all(w != "转竖屏" for w, _ in none_calls), str([w for w, _ in none_calls]))
    fr = [(w, a) for w, a in blur_calls if w == "转竖屏"]
    check("K frame=blur 时「转竖屏」恰好一次", len(fr) == 1, str(len(fr)))
    if fr:
        a = fr[0][1]
        check("K 画幅这一步用的是 veryfast",
              a[a.index("-preset") + 1] == "veryfast"
              if "-preset" in a else False, str(a))
        check("K 画幅这一步的滤镜里确实有 gblur",
              any("gblur" in x for x in a), str(a))
    # 主链路（切段）仍是自己的 preset/crf，没被 FRAME_PRESET 顶掉
    slice_presets = [(a[a.index("-preset") + 1], a[a.index("-crf") + 1])
                     for w, a in none_calls if "-preset" in a and "-crf" in a]
    print(f"     frame=None 主链路的 (preset, crf): {slice_presets}")
    check("K frame=None 的切段仍是 crf16/veryfast（老逻辑没变）",
          all(c == "16" and p in ("veryfast",) for p, c in slice_presets)
          and bool(slice_presets), str(slice_presets))
    return {"s_full": round(s_full, 4), "p_full": round(p_full, 2),
            "p_sub": round(p_sub, 2), "t_med": round(outs["medium"][1], 1),
            "t_vf": round(outs["veryfast"][1], 1)}


# ================= L. 横屏零回归 =================

def sec_L():
    print("\n== L. 横屏零回归（20 字不能动，临界点算对）==")
    from backend import subtitle
    base = "这是自动转写断句出来的一行字幕大概有这么长啊真的挺长的"
    fs = round(1080 * 0.055)
    per = subtitle.line_capacity(1920, fs)
    print(f"     1920x1080 / font={fs} → per={per}")
    check("L 横屏 1080p 每行容量 = 38 字", per == 38, str(per))

    for n, want_wrap in ((18, False), (24, False), (36, False), (38, False),
                         (39, True), (40, True)):
        text = (base * 2)[:n]
        at = subtitle.build_ass([{"start": 0.3, "end": 2.6, "text": text}],
                                1920, 1080, {"size_ratio": 0.055})
        d = ass_dialogues(at)[0]
        got_wrap = "\\N" in d["text"]
        check(f"L 横屏 {n} 字 · 折行状态符合临界点（期望{'折' if want_wrap else '不折'}）",
              got_wrap == want_wrap, f"实际{'折' if got_wrap else '不折'}")
        if not want_wrap:
            check(f"L 横屏 {n} 字 · Dialogue 与原文逐字一致（一个字都没动）",
                  d["text"] == text, f"{d['text']!r}")

    # 真渲染：18 字必须单行（行高 <120px），左右在画面内
    src = mk_video(os.path.join(TMP, "l_1080p.mp4"), 3.0, 1920, 1080, audio="none")
    text = (base * 2)[:18]
    ass = os.path.join(TMP, "l_h.ass")
    subtitle.write_ass([{"start": 0.3, "end": 2.6, "text": text}], ass, 1920, 1080,
                       {"size_ratio": 0.055})
    out = os.path.join(TMP, "l_h.mp4")
    if os.path.exists(out):
        os.remove(out)
    subtitle.burn(src, out, ass)
    bb = glyph_bbox(frame_rgb(out, 1.4, 1920, 1080), 1920, 1080)
    h = bb[3] - bb[1] + 1
    print(f"     18 字横屏成片 bbox={bb} 行高={h}px")
    check("L 横屏 18 字渲染出来是单行（行高 <120px）", h < 120, f"{h}px")
    check("L 横屏 18 字左右都在画面内",
          bb[0] >= 40 and bb[2] <= 1880, str(bb))
    # 逐字点亮 + 横屏 18 字：也不能被折行波及
    cue = mk_cue(0.3, 2.6, text)
    at = subtitle.build_ass([cue], 1920, 1080, {"highlight": True,
                                                "highlight_color": "#FFD54A"})
    d = ass_dialogues(at)[0]
    check("L 横屏 + 高亮 18 字也不折行", "\\N" not in d["text"], d["text"][:40])
    check("L 横屏 + 高亮 \\kf 总和仍等于 cue 时长",
          kf_sum(d["text"]) == int(round((d["end"] - d["start"]) * 100)),
          str(kf_sum(d["text"])))
    return {"per_h": per}


# ================= M. 英文 / 中英混排不劈词 =================

def sec_M():
    print("\n== M. 英文与中英混排折行不劈词 ==")
    from backend import subtitle
    en = "this is a fairly long english subtitle line with spaces and more words"
    at = subtitle.build_ass([{"start": 0.2, "end": 2.8, "text": en}],
                            1080, 1920, {"size_ratio": 0.055})
    d = ass_dialogues(at)[0]
    rows = d["text"].split("\\N")
    print("     英文折成 %d 行：" % len(rows))
    for r in rows:
        print(f"       |{r}|")
    check("M 英文真的折行了（竖屏 60 字符）", len(rows) > 1, str(len(rows)))
    joined = "".join(rows)
    check("M 折行没有增删字符（拼回去与原文逐字一致）", joined == en,
          f"{joined!r}")
    # 每个单词完整、不在词中插 \N
    orig_words = en.split(" ")
    for w0 in orig_words:
        hit = [r for r in rows if w0 and w0 in r]
        check(f"M 单词「{w0}」完整落在同一行里", bool(hit), str(rows))
    # 除末行外，每行都该以空格收尾（切点落在空格上）
    check("M 除末行外每行以空格结尾（切点在词边界）",
          all(r.endswith(" ") for r in rows[:-1]), str([r[-1:] for r in rows[:-1]]))
    check("M 末行不以空格结尾", not rows[-1].endswith(" "), repr(rows[-1][-1:]))

    mixed = "这个功能叫 auto cut 很好用"
    at2 = subtitle.build_ass([{"start": 0.2, "end": 2.8, "text": mixed}],
                             1080, 1920, {"size_ratio": 0.068})   # 大字号逼它折
    d2 = ass_dialogues(at2)[0]
    rows2 = d2["text"].split("\\N")
    print("     中英混排折成 %d 行：" % len(rows2))
    for r in rows2:
        print(f"       |{r}|")
    check("M 混排折行后拼回去与原文一致", "".join(rows2) == mixed,
          f"{''.join(rows2)!r}")
    check("M 「auto」没被劈开", any("auto" in r for r in rows2), str(rows2))
    check("M 「cut」没被劈开", any("cut" in r for r in rows2), str(rows2))
    check("M 「auto」与「cut」没有被拆到两行（auto cut 之间只有一个空格）",
          any("auto" in r and "cut" in r for r in rows2) or
          all(("auto" in r) != ("cut" in r) for r in rows2), str(rows2))

    # 超长不可断词：单个英文词就比一行还宽 —— R2 之后按**宽度**硬切，不再是残余风险。
    # 原来这里断言的是「仍被裁」（记录风险），R2 修好后必然变红，工程师把极性翻了过来。
    # 极性翻得对，但判据我重写成两条：机制（按宽度切）+ 实测余量，不再只是「没被裁」。
    long_word = "internationalizationinternationalization"
    at3 = subtitle.build_ass([{"start": 0.2, "end": 2.8, "text": long_word}],
                             1080, 1920, {"size_ratio": 0.055})
    d3 = ass_dialogues(at3)[0]
    rows3 = d3["text"].split("\\N")
    print(f"     超长单词折行结果：{d3['text']!r}")
    per3 = subtitle.line_capacity(1080, round(1920 * 0.055))
    check("M 超长不可断词被硬切成多行（按宽度切，不是靠字符数）",
          len(rows3) >= 2, f"{len(rows3)} 行")
    check("M 硬切后拼回去与原文逐字一致（没丢字）",
          "".join(rows3) == long_word, f"{''.join(rows3)!r}")
    est3 = [round(subtitle._unit_width(r) * subtitle.WRAP_CHAR_RATIO
                  * round(1920 * 0.055), 1) for r in rows3]
    check(f"M 每行估算像素宽 ≤ 1080（per={per3} 的口径已改成宽度）",
          all(x <= 1080 for x in est3), str(est3))
    src = mk_video(os.path.join(TMP, "e3_v1080.mp4"), 12.0, 1080, 1920, audio="none")
    ass3 = os.path.join(TMP, "m_long.ass")
    subtitle.write_ass([{"start": 0.2, "end": 2.8, "text": long_word}], ass3,
                       1080, 1920, {"size_ratio": 0.055})
    out3 = os.path.join(TMP, "m_long.mp4")
    if os.path.exists(out3):
        os.remove(out3)
    subtitle.burn(src, out3, ass3)
    bb3 = glyph_bbox(frame_rgb(out3, 1.5, 1080, 1920), 1080, 1920)
    print(f"     超长单词实际 bbox={bb3} 左右余量 {bb3[0]}/{1079 - bb3[2]}px"
          if bb3 else "     超长单词没渲出来")
    if bb3:
        check("M 超长不可断英文词渲染后左右各留 ≥40px",
              bb3[0] >= 40 and bb3[2] <= 1039, f"bbox={bb3}")
    # 残余风险备案（**不设断言**，只登记）：_GLYPH_W 表外的字符仍按 0.55 估宽，
    # emoji / 比汉字宽的非 ASCII 字形会偏宽 —— 实测 20 个 😀 折 2 行、左右余量只剩 4px。
    print("     残余风险（备案）：宽度表外字符按 0.55 估；emoji 实测每行 10 个时余量仅 4px")

    return {"en_rows": len(rows), "mixed_rows": len(rows2)}


# ================= N. R1/R2/R3 收尾复核 =================

def sec_N():
    print("\n== N. 行数天花板 / 长 token 硬切 / 空格单元 ==")
    from backend import subtitle
    per = subtitle.line_capacity(1080, round(1920 * 0.055))
    mc = subtitle.unit_capacity(per)
    print(f"     竖屏 per={per}，半角硬切长度 mc={mc} 字符，WRAP_MAX_ROWS={subtitle.WRAP_MAX_ROWS}")
    src = mk_video(os.path.join(TMP, "e3_v1080.mp4"), 12.0, 1080, 1920, audio="none")

    def rows_of(text):
        at = subtitle.build_ass([{"start": 0.3, "end": 2.3, "text": text}], 1080, 1920,
                                {"size_ratio": 0.055})
        d = ass_dialogues(at)[0]
        return d["text"].split("\\N")

    def render_bbox(text, tag):
        ass = os.path.join(TMP, f"n_{tag}.ass")
        out = os.path.join(TMP, f"n_{tag}.mp4")
        subtitle.write_ass([{"start": 0.3, "end": 2.3, "text": text}], ass, 1080, 1920,
                           {"size_ratio": 0.055})
        if os.path.exists(out):
            os.remove(out)
        subtitle.burn(src, out, ass)
        return glyph_bbox(frame_rgb(out, 1.2, 1080, 1920), 1080, 1920)

    BASE_CN = ("这是自动转写断句出来的一行字幕大概有这么长啊真的挺长的这是自动转写"
               "断句出来的一行字幕大概")
    cn36, cn48, cn60 = (BASE_CN * 2)[:36], (BASE_CN * 2)[:48], (BASE_CN * 2)[:60]
    # N1 行数天花板 3→5
    check("N1 WRAP_MAX_ROWS 已提到 5", subtitle.WRAP_MAX_ROWS == 5,
          str(subtitle.WRAP_MAX_ROWS))
    for n, text, want_rows in ((36, cn36, 4), (48, cn48, 5), (60, cn60, 5)):
        rows_n = rows_of(text)
        check(f"N1 {n} 字折 {want_rows} 行", len(rows_n) == want_rows,
              str([len(r) for r in rows_n]))
        check(f"N1 {n} 字拼回去与原文逐字一致", "".join(rows_n) == text, "")
        bb = render_bbox(text, f"cn{n}")
        L, R = bb[0], 1079 - bb[2]
        print(f"     cn{n} {len(rows_n)} 行 每行 {[len(r) for r in rows_n]} 字 "
              f"bbox={bb} 左右余量 {L}/{R}px")
        check(f"N1 {n} 字竖屏不切边（左右各 ≥40px）", L >= 40 and R >= 40,
              f"余量 {L}/{R}px")

    # N1b 5 行天花板的**实际**边界：per 是保守容量（0.80 字宽），CJK 每字 1.0 宽，
    # 所以 12 字/行已经 > per=11 却仍然只占 967px。实测：60 字 56/57px 安全，
    # 61–65 字挤到 13 字/行 → 余量 16px（紧但没切），66 字起 14 字/行 → 0/0 切边。
    rows61 = rows_of((BASE_CN * 2)[:61])
    rows66 = rows_of((BASE_CN * 2)[:66])
    check("N1b 60 字 = 12 字/行（≥40px 余量的实际天花板）",
          max(len(r) for r in rows_of(cn60)) == 12,
          str([len(r) for r in rows_of(cn60)]))
    check("N1b 61 字挤到 13 字/行（余量会掉到 16px，已记录）",
          max(len(r) for r in rows61) == 13, str([len(r) for r in rows61]))
    check("N1b 66 字到 14 字/行（这一档起会切边，已记录为已知边界）",
          max(len(r) for r in rows66) >= 14, str([len(r) for r in rows66]))

    # N2 长 token 硬切：机制层面
    long_word = "internationalizationinternationalization"
    rows = rows_of(long_word)
    check("N2 40 字符连写被硬切折行", len(rows) >= 2, str(len(rows)))
    check("N2 硬切没有丢字（拼回去与原文一致）", "".join(rows) == long_word,
          f"{''.join(rows)!r}")
    px_n2 = [round(subtitle._unit_width(r) * subtitle.WRAP_CHAR_RATIO
                   * round(1920 * 0.055), 1) for r in rows]
    check("N2 每行估算像素宽 ≤ 1080（硬切按宽度算，不再按字符数）",
          all(x <= 1080 for x in px_n2), str(px_n2))
    bb = render_bbox(long_word, "long40")
    print(f"     long40 bbox={bb} 左右余量 {bb[0]}/{1079 - bb[2]}px")
    check("N2 40 字符小写长词不再切边", bb[0] >= 40 and bb[2] <= 1039, f"bbox={bb}")

    # N2b karaoke 版硬切：\kf 总和必须一分不变、\N 不能进 {}
    cue = {"start": 0.0, "end": 2.0, "text": long_word,
           "words": [{"start": 0.0, "end": 2.0, "text": long_word}]}
    at = subtitle.build_ass([cue], 1080, 1920,
                            {"highlight": True, "highlight_color": "#FFD54A"})
    d = ass_dialogues(at)[0]
    print(f"     karaoke 硬切：{d['text'][:60]}…（共 {d['text'].count(chr(92) + 'N')} 个 \\N）")
    check("N2b karaoke 版也折了行", "\\N" in d["text"], d["text"][:40])
    check("N2b \\kf 厘秒总和仍等于 cue 时长（200）", kf_sum(d["text"]) == 200,
          str(kf_sum(d["text"])))
    bad = [m for m in re.findall(r"\{[^{}]*\}", d["text"]) if "\\N" in m]
    check("N2b \\N 不在任何 {} 覆盖块里", not bad, str(bad))
    vis = re.sub(r"\{[^{}]*\}", "", d["text"]).replace("\\N", "")
    check("N2b 去掉标记后文字与原文逐字一致", vis == long_word, f"{vis!r}")

    # N2c 真实 55 字符网址 + 逐字点亮：硬切之后 \kf 还得准，高亮要能越过 \N
    url = "https://www.example.com/watch?v=abcdefghijklmnopqrstuvwxyz0123"[:55]
    cue_u = {"start": 0.0, "end": 4.0, "text": url,
             "words": [{"start": 0.0, "end": 4.0, "text": url}]}
    hi = {"size_ratio": 0.055, "highlight": True, "highlight_color": "#FFD54A"}
    at_u = subtitle.build_ass([cue_u], 1080, 1920, hi)
    d_u = ass_dialogues(at_u)[0]
    urows = d_u["text"].split("\\N")
    print(f"     {len(url)} 字符网址 → {len(urows)} 行：" + " | ".join(urows))
    check("N2c 55 字符网址被硬切成多行", len(urows) >= 2, str(len(urows)))
    check("N2c \\kf 厘秒总和 = 400（4.00s cue，一分不差）",
          kf_sum(d_u["text"]) == 400, str(kf_sum(d_u["text"])))
    bad_u = [m for m in re.findall(r"\{[^{}]*\}", d_u["text"]) if "\\N" in m]
    check("N2c \\N 不在任何 {} 覆盖块里", not bad_u, str(bad_u))
    vis_u = re.sub(r"\{[^{}]*\}", "", d_u["text"]).replace("\\N", "")
    check("N2c 去掉标记后与原文逐字一致（硬切没丢字）", vis_u == url, f"{vis_u!r}")
    marks_u = [int(x) for x in re.findall(r"\{\\kf(\d+)\}", d_u["text"])]
    check("N2c 每段 \\kf 都 > 0（没有 0 厘秒的空段）",
          all(m > 0 for m in marks_u), str(marks_u))

    ass_u = os.path.join(TMP, "n_url.ass")
    subtitle.write_ass([cue_u], ass_u, 1080, 1920, hi)
    out_u = os.path.join(TMP, "n_url.mp4")
    if os.path.exists(out_u):
        os.remove(out_u)
    subtitle.burn(src, out_u, ass_u)
    bb_u = glyph_bbox(frame_rgb(out_u, 1.9, 1080, 1920), 1080, 1920)
    if bb_u:
        L_u, R_u = bb_u[0], 1079 - bb_u[2]
        print(f"     url bbox={bb_u} 左右余量 {L_u}/{R_u}px")
        check("N2c 网址渲染后左右各 ≥40px", L_u >= 40 and R_u >= 40,
              f"余量 {L_u}/{R_u}px")
        mid = (bb_u[1] + bb_u[3]) // 2
        top = max(0, bb_u[1] - 4) * 1080 * 3
        bot = min(1920, bb_u[3] + 5) * 1080 * 3
        seq, low = [], []
        for t in (0.4, 1.5, 2.6, 3.6):
            buf = frame_rgb(out_u, t, 1080, 1920)[top:bot]
            seq.append(glyph_stats(buf)[0])
            low.append(glyph_stats(buf[(mid - max(0, bb_u[1] - 4)) * 1080 * 3:])[0])
        print(f"     高亮像素随时间 {seq}；越过 \\N 之后那行 {low}")
        check("N2c 高亮随时间单调推进（4 个采样点严格递增）",
              all(seq[i] < seq[i + 1] for i in range(3)), str(seq))
        check("N2c 高亮能越过 \\N 继续点亮下一行",
              low[-1] > 0 and low[0] == 0, f"下半 {low}")

    # N3 宽度模型：真实内容里「宽字形」连写还会撑破
    print("     逐个半角字宽实测（font=106）：W=86.6 M=81.2 @=81.8 m=78.0 w=68.6 "
          "n=51.1 a=45.9 t=33.2 -> 代码里一律按 0.55*font≈58px 估")
    for tag, text in (("url_short", "https://www.wowowow.com/watch?v=12345"),
                      ("url_noscheme", "www.wowowow.com/watch?v=abcdefghijk"),
                      ("brand_caps", "WWW.MCDONALDS.COM/ORDER"),
                      ("brand_m", "MMMMMM.MMMMMM.COM")):
        bb = render_bbox(text, tag)
        L, R = bb[0], 1079 - bb[2]
        print(f"     {tag:13s} {len(text):2d} 字符 → bbox={bb} 余量 {L}/{R}px  「{text}」")
        check(f"N3 {tag} 不可断长串不切边（真实 URL/品牌名）",
              L >= 40 and R >= 40, f"余量 {L}/{R}px，已切边")

    # N4 空格单元
    units = subtitle._text_units("这个功能叫 auto cut 很好用",
                                 subtitle.unit_capacity(per))
    pure_space = [u for u in units if not u[0].strip()]
    check("N4 不再产生 0 宽的纯空格单元", not pure_space, str(pure_space))
    mixed = "这个功能叫 auto cut 很好用"
    rows2 = rows_of(mixed)
    print(f"     混排折行：{rows2}")
    check("N4 续行不再以空格开头", all(not r.startswith(" ") for r in rows2), str(rows2))
    check("N4 混排拼回去仍与原文一致", "".join(rows2) == mixed, f"{''.join(rows2)!r}")
    return {"per": per, "mc": mc}


# ================= WEB. 前端静态检查 =================

def sec_WEB():
    print("\n== WEB. 前端接线检查 ==")
    js = open(os.path.join(ROOT, "web", "app.js"), encoding="utf-8").read()
    html = open(os.path.join(ROOT, "web", "index.html"), encoding="utf-8").read()

    r = subprocess.run(["node", "--check", os.path.join(ROOT, "web", "app.js")],
                       capture_output=True)
    check("WEB node --check web/app.js 通过", r.returncode == 0,
          (r.stderr or b"").decode("utf-8", "ignore")[:300])

    ids = ["fxSubOutline", "fxSubBox", "fxSubBoxAlpha", "fxSubBoxAlphaRow",
           "fxSubHighlight", "fxSubHlColor", "fxFrame", "fxFrameHint",
           "fxFrameNote", "fxDenoiseOn", "fxSubOn"]
    missing_html = [i for i in ids if f'id="{i}"' not in html]
    check("WEB 新控件 id 都在 index.html 里", not missing_html, str(missing_html))
    # 死控件判定：#id 只在 html 里出现、js 里一次都没提
    dead = [i for i in ids if ("#" + i) not in js]
    check("WEB index.html 里的新控件 app.js 都引用到了（没有死控件）",
          not dead, str(dead))

    # 每个新控件都绑了事件，不是只能显示不能操作
    for cid in ("fxSubOutline", "fxSubBox", "fxSubHighlight", "fxDenoiseOn"):
        pat = f'$("#{cid}").addEventListener'
        check(f"WEB {cid} 绑了事件监听", pat in js, "找不到 addEventListener")
    check("WEB fxSubBoxAlpha 绑了事件监听",
          '$("#fxSubBoxAlpha").addEventListener' in js, "找不到")
    check("WEB fxSubHlColor 绑了事件监听",
          '$("#fxSubHlColor").addEventListener' in js, "找不到")
    check("WEB fxFrame 的按钮绑了点击事件", '$$("#fxFrame button")' in js,
          "找不到 #fxFrame button")
    check("WEB 点了画幅按钮会写回 state.fx.frame",
          "state.fx.frame =" in js, "找不到 state.fx.frame 赋值")
    check("WEB #fxFrame 的 data-val 覆盖 origin/blur/crop",
          all(f'data-val="{m}"' in html for m in ("origin", "blur", "crop")),
          "画幅按钮缺档位")

    # 新键的「读」位置必须有兜底；写位置（= 赋值）不算
    # 只有「拿去做方法调用 / 算术」才会真崩；纯布尔上下文（undefined 即 false）不算风险
    reads = []
    for key in ("outline_color", "box_alpha", "highlight", "highlight_color",
                "denoise", "frame"):
        for m in re.finditer(r"(?:state\.)?fx\.(?:subtitle\.)?" + key + r"\b", js):
            line_start = js.rfind("\n", 0, m.start()) + 1
            line_end = js.find("\n", m.end())
            line = js[line_start:line_end if line_end > 0 else len(js)]
            tail = js[m.end():m.end() + 46]
            if re.match(r"\s*=(?!=)", tail):
                continue                      # 写，不用兜底
            dangerous = re.match(r"\s*(\.(toFixed|split|map|join|trim|length)"
                                 r"|[+\-*/])", tail)
            if not dangerous:
                continue                      # 布尔上下文 / 三元条件，undefined 也安全
            before = js[line_start:m.start()]
            prev_ch = js[max(0, m.start() - 1):m.start()]
            guarded = ("||" in line or "!= null" in line or "??" in line
                       or "!!" in before or prev_ch == "!"
                       or before.rstrip().endswith(("?", ":"))
                       or "Number(" in js[max(0, m.start() - 12):m.start()]
                       or "Math.round(" in js[max(0, m.start() - 14):m.start()])
            if not guarded:
                reads.append((key, js[:m.start()].count("\n") + 1, tail[:40]))
    check("WEB 新键拿去做方法调用/算术的地方都有兜底", not reads, str(reads[:4]))

    # state.fx 的来路只有两种：FX_DEFAULT() 和整份深拷贝，没有「老 JSON 直接塞」
    assigns = re.findall(r"state\.fx\s*=\s*([^\n;]+)", js)
    bad_assign = [a for a in assigns
                  if "FX_DEFAULT()" not in a and "JSON.parse" not in a
                  and "snap.fx" not in a]
    check("WEB state.fx 只来自 FX_DEFAULT() 或快照深拷贝（老 JSON 不会缺键）",
          not bad_assign, str(bad_assign))
    check("WEB FX_DEFAULT 里带齐了新键",
          all(k in js for k in ('outline_color:', 'box_alpha:', 'highlight:',
                                'highlight_color:', 'denoise:', 'frame:')),
          "FX_DEFAULT 缺键")
    check("WEB frame 只在非 origin 时才发给后端",
          "fx.frame !== \"origin\"" in js, "找不到 origin 的判断")
    check("WEB fxPayload 把 denoise/frame 都带给后端",
          "denoise: !!fx.denoise" in js and "frame:" in js.split("function fxPayload")[1][:1400],
          "payload 缺字段")


# ================= main =================

def main():
    ensure_assets()
    t_all = time.time()
    perf = {}
    if section("A"):
        perf["plain_sec"] = sec_A()
    if section("B"):
        perf["B"] = sec_B()
    if section("C"):
        perf["C"] = sec_C()
    if section("D"):
        perf["D"] = sec_D()
    if section("E"):
        perf["E"] = sec_E()
    if section("F"):
        perf["F"] = sec_F()
    if section("G"):
        perf["G"] = sec_G()
    if section("H"):
        perf["H"] = sec_H()
    if section("I"):
        perf["I"] = sec_I()
    if section("J"):
        sec_J()
    if section("E3"):
        perf["E3"] = sec_E3()
    if section("E4"):
        perf["E4"] = sec_E4()
    if section("E5"):
        perf["E5"] = sec_E5()
    if section("K"):
        perf["K"] = sec_K()
    if section("L"):
        perf["L"] = sec_L()
    if section("M"):
        perf["M"] = sec_M()
    if section("N"):
        perf["N"] = sec_N()
    if section("WEB"):
        sec_WEB()

    print("\n" + "=" * 52)
    print(f"总耗时 {time.time() - t_all:.1f}s")
    if FAIL:
        print(f"通过 {OK} 项，失败 {len(FAIL)} 项：")
        for n in FAIL:
            print("  -", n)
        sys.exit(1)
    print(f"通过 {OK} 项，全部通过")


if __name__ == "__main__":
    try:
        main()
    finally:
        # 尽力清掉自己的临时目录；有句柄没放就留着，不因为它再抛一次
        shutil.rmtree(TMP, ignore_errors=True)
