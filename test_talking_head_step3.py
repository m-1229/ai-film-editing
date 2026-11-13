# -*- coding: utf-8 -*-
"""口播专项第三步 · 真渲染验收（ffmpeg，不需要 whisper）

素材全部用 ffmpeg lavfi 现场合成（不依赖 projects/test_rough.mp4），
本条验的是「画幅 / 字幕样式 / 逐字点亮 / 降噪」这几件事真落到了像素和波形上：

A 基线零回归    不传 frame/denoise → 输出分辨率 = 源分辨率，时长不变
B 竖屏·模糊背景 1080x1920、时长与基线差 <0.1s、音频分段 RMS 差 <0.3dB
C 竖屏·居中裁切 1080x1920，画面 1:1 取自源中间一条（不是拉伸），边缘不虚
D 竖屏 + 字幕   字幕真的落在画面里（底部 80%–95% 有亮像素），且 PlayRes 用的是输出尺寸
E 逐字点亮      黄色像素随时间单调增长（真·逐字），开/关同一时刻像素差 > 3
F 描边色        红色描边让字幕区域 R 通道均值上升
G 降噪          语音段 RMS 变化 ≤1.5dB，静音段底噪下降 ≥4dB，画面不重编
H 已是竖屏      源就是 1080x1920 时跳过变换，不白重编
I 精调包不沾    export_kit 的 clip_*.mp4 仍是源分辨率（用户要拿回剪映改）
J 前端静态      index.html / app.js 里该有的控件与字段都在
K 竖屏折行      18/24 字的字幕在 1080 宽下自己折行（libass 不给中文折行），不切字

跑法： D:/Python311/python.exe test_talking_head_step3.py
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from backend import audio_fx, exporter, renderer, subtitle as submod  # noqa: E402
from utils.ffmpeg_setup import ensure_ffmpeg  # noqa: E402

OK = 0
FAIL = []
FLAGS = 0x08000000 if os.name == "nt" else 0

EXE = ensure_ffmpeg()
WORK = Path(tempfile.mkdtemp(prefix="th3_"))

# 竖屏画幅的输出尺寸（和 renderer.FRAME_W/H 必须一致）
FW, FH = 1080, 1920


def check(name, cond, detail=""):
    global OK
    if cond:
        OK += 1
        print(f"  ✓ {name}")
    else:
        FAIL.append(name)
        print(f"  ✗ {name}   {detail}")


# ---------------- 工具 ----------------

def sh(args):
    """跑一条 ffmpeg/ffprobe，失败直接抛（构建素材用）"""
    r = subprocess.run(args, capture_output=True, creationflags=FLAGS)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or b"").decode("utf-8", "ignore")[-400:])
    return r


def build(path, vfilter, afilter=None):
    """lavfi 现造素材：指定画面源 + 可选音频源"""
    args = [EXE, "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", vfilter]
    if afilter:
        args += ["-f", "lavfi", "-i", afilter, "-shortest"]
    args += ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-r", "25"]
    if afilter:
        args += ["-c:a", "aac"]
    args.append(str(path))
    sh(args)


def dims(path):
    info = audio_fx.probe(str(path))
    return info["width"], info["height"]


def dur(path):
    try:
        return float(audio_fx.probe(str(path)).get("duration") or 0.0)
    except Exception:
        return 0.0


def db(path, start, span):
    """某一段的平均电平（dB）——用 volumedetect 量"""
    r = subprocess.run([EXE, "-hide_banner", "-ss", f"{start:.2f}", "-t", f"{span:.2f}",
                        "-i", str(path), "-vn", "-af", "volumedetect",
                        "-f", "null", "-"], capture_output=True, creationflags=FLAGS)
    text = (r.stderr or b"").decode("utf-8", "ignore")
    m = re.search(r"mean_volume:\s*(-?[\d.]+) dB", text)
    return float(m.group(1)) if m else None


def frame_at(path, t):
    """抠一帧（走 ffmpeg 的精确 seek，比 cv2 定点稳）"""
    png = WORK / f"f_{Path(path).stem}_{str(t).replace('.', '_')}.png"
    sh([EXE, "-y", "-hide_banner", "-loglevel", "error", "-ss", f"{t}",
        "-i", str(path), "-frames:v", "1", str(png)])
    img = cv2.imread(str(png))
    if img is None:
        raise RuntimeError(f"抠帧失败：{path} @ {t}")
    return img


def band(img, lo=0.80, hi=0.95):
    """字幕区（画面底部 80%–95%）"""
    h = img.shape[0]
    return img[int(h * lo):int(h * hi)].astype(np.int16)


def yellow_px(b):
    """高亮色（暖黄 #FFD54A）像素数：R 高、B 低"""
    return int(((b[:, :, 2] > 180) & (b[:, :, 0] < 120)).sum())


def white_px(b):
    return int(((b[:, :, 2] > 180) & (b[:, :, 0] > 180)).sum())


def bright_mask(b, thr=150):
    return b.max(axis=2) > thr


# ---------------- 素材 ----------------

print("== 0. 现场合成素材 ==")
# 彩条：几何/裁切/像素对比用（大面积纯色，压缩噪声小）
BARS = WORK / "bars.mp4"
build(BARS, "smptebars=size=1280x720:rate=25:duration=4", "sine=frequency=440:duration=4")
# 深色底：字幕/点亮/描边用（背景够暗，字幕才检得出来）
TALK = WORK / "talk.mp4"
build(TALK, "color=c=0x204060:s=1280x720:d=4:r=25", "sine=frequency=440:duration=4")
# 语音 + 底噪：降噪用（前 2s 人声段，后 2s 只有白噪）
NOISE = WORK / "noise.mp4"
sh([EXE, "-y", "-hide_banner", "-loglevel", "error",
    "-f", "lavfi", "-i", "color=c=0x204060:s=1280x720:d=4:r=25",
    "-f", "lavfi", "-i", "sine=frequency=440:duration=2:sample_rate=48000",
    "-f", "lavfi", "-i", "anoisesrc=duration=2:amplitude=0.05:sample_rate=48000",
    "-filter_complex", "[1:a][2:a]concat=n=2:v=0:a=1[a]",
    "-map", "0:v", "-map", "[a]", "-c:v", "libx264", "-pix_fmt", "yuv420p",
    "-c:a", "aac", str(NOISE)])
check("素材都造出来了", BARS.exists() and TALK.exists() and NOISE.exists(),
      (BARS.exists(), TALK.exists(), NOISE.exists()))

CUES = [{
    "start": 0.0, "end": 4.0, "text": "今天聊聊口播剪辑的要点",
    "words": [{"start": 0.0, "end": 0.8, "text": "今天"},
              {"start": 0.8, "end": 1.6, "text": "聊聊"},
              {"start": 1.6, "end": 2.4, "text": "口播"},
              {"start": 2.4, "end": 3.2, "text": "剪辑"},
              {"start": 3.2, "end": 4.0, "text": "的要点"}],
}]


def render(tag, src=None, frame=None, subtitle=None, denoise=False):
    """跑一次真渲染，返回 (输出路径, 降级说明列表)"""
    out = WORK / f"{tag}.mp4"
    notes = []
    renderer.render_cut(str(src or BARS), str(out), [(0.0, 4.0)],
                        frame=frame, subtitle=subtitle, denoise=denoise, notes=notes)
    return out, notes


# ---------------- A. 基线：老行为零回归 ----------------

print("\n== A. 不传 frame/denoise：老行为一点没变 ==")
BASE, notes_a = render("base")
check("输出分辨率 = 源分辨率 1280x720", dims(BASE) == (1280, 720), dims(BASE))
check("不传新字段时没有任何降级说明", notes_a == [], notes_a)
check("时长 = 4.0s（±0.1）", abs(dur(BASE) - 4.0) <= 0.1, dur(BASE))

# ---------------- B. 竖屏·模糊背景 ----------------

print("\n== B. 竖屏·模糊背景 ==")
BLUR, notes_b = render("blur", frame={"mode": "blur"})
check("输出 1080x1920", dims(BLUR) == (FW, FH), dims(BLUR))
check("时长与不竖屏差 <0.1s", abs(dur(BLUR) - dur(BASE)) < 0.1, (dur(BLUR), dur(BASE)))
check("一次编码搞定，没有降级", notes_b == [], notes_b)

seg_diffs = []
for start in (0.0, 1.0, 2.0, 3.0):
    a, b = db(BASE, start, 1.0), db(BLUR, start, 1.0)
    if a is not None and b is not None:
        seg_diffs.append(abs(a - b))
check("音频 4 段 RMS 差都 <0.3dB", seg_diffs and max(seg_diffs) < 0.3,
      [round(x, 3) for x in seg_diffs])

# ---------------- C. 竖屏·居中裁切 ----------------

print("\n== C. 竖屏·居中裁切（是裁切，不是拉伸） ==")
CROP, notes_c = render("crop", frame={"mode": "crop"})
check("输出 1080x1920", dims(CROP) == (FW, FH), dims(CROP))
check("时长与不竖屏差 <0.1s", abs(dur(CROP) - dur(BASE)) < 0.1, (dur(CROP), dur(BASE)))


def crop_src_xy(xo, yo, sw=1280, sh_=720):
    """裁切后输出的 (xo,yo) 对应源片哪个像素

    scale=1080:1920:increase 是按 max(1080/w, 1920/h) 等比放大再居中裁，
    所以输出坐标和源坐标是严格 1:1 的比例关系（没有拉伸）。
    """
    k = max(FW / sw, FH / sh_)
    scaled_w = sw * k
    off_x = (scaled_w - FW) / 2.0
    return (xo + off_x) / k, yo / k


src_c = frame_at(BARS, 2.0)
out_c = frame_at(CROP, 2.0)
cs, ce = crop_src_xy(540, 960)
check("画面中心像素与原片中心一致",
      np.abs(src_c[int(round(ce)), int(round(cs))].astype(int) - out_c[960, 540].astype(int)).max() <= 14,
      (src_c[int(round(ce)), int(round(cs))], out_c[960, 540]))

off = []
for xo, yo in ((200, 960), (900, 700), (300, 400)):
    sx, sy = crop_src_xy(xo, yo)
    d = np.abs(src_c[int(round(sy)), int(round(sx))].astype(int) - out_c[yo, xo].astype(int)).max()
    off.append(int(d))
check("偏离中心的 3 个点也是 1:1 对应（证明是裁切不是缩放）", max(off) <= 14, off)

# 模糊背景的边缘是虚的，裁切的边缘是实的
edge_crop = out_c[960, 5:60].astype(np.float32)
check("裁切边缘不虚（方差明显高于模糊背景）",
      edge_crop.std() > frame_at(BLUR, 2.0)[960, 5:60].astype(np.float32).std(),
      (edge_crop.std(), frame_at(BLUR, 2.0)[960, 5:60].astype(np.float32).std()))

# ---------------- D. 竖屏 + 字幕：PlayRes 必须是输出尺寸 ----------------

print("\n== D. 竖屏 + 字幕同开 ==")
VSUB, notes_d = render("vsub", src=TALK, frame={"mode": "blur"},
                       subtitle={"cues": CUES, "style": {}})
check("输出 1080x1920", dims(VSUB) == (FW, FH), dims(VSUB))
check("画幅与字幕合进同一次编码（没有退回两次）", notes_d == [], notes_d)

sub_band = band(frame_at(VSUB, 2.0))
mask = bright_mask(sub_band)
check("底部 80%–95% 区域真的有字幕（亮像素 > 200）", int(mask.sum()) > 200, int(mask.sum()))
rows = np.where(mask.any(axis=1))[0]
cols = np.where(mask.any(axis=0))[0]
sub_w = int(cols.max() - cols.min() + 1) if len(cols) else 0
sub_h = int(rows.max() - rows.min() + 1) if len(rows) else 0
# PlayRes 写成源尺寸(1280x720)的话字号会被缩掉约一半，行宽会掉到 450 左右
check("字幕行宽 ≈ 输出宽度的八成（PlayRes 用的是 1080x1920）", sub_w >= 700,
      f"宽 {sub_w} 高 {sub_h}")
check("字幕行高 ≈ 字号量级（70–140px）", 60 <= sub_h <= 140, sub_h)
check("字幕落在底部（不越出安全区）", rows.max() < sub_band.shape[0], (rows.min(), rows.max()))

# ---------------- E. 逐字点亮 ----------------

print("\n== E. 逐字点亮（libass 原生 karaoke）真落片 ==")
VHI, notes_e = render("vhi", src=TALK, frame={"mode": "blur"},
                      subtitle={"cues": CUES, "style": {"highlight": True}})
check("点亮版也是 1080x1920 / 一次编码", dims(VHI) == (FW, FH) and notes_e == [],
      (dims(VHI), notes_e))

counts = []
for t in (0.5, 1.5, 2.5, 3.4):
    counts.append(yellow_px(band(frame_at(VHI, t))))
check("高亮像素随时间单调增长（真·逐字，不是整句染色）",
      all(counts[i] < counts[i + 1] for i in range(len(counts) - 1)), counts)
check("片头几乎还没点亮、片尾基本都点亮了", counts[0] < counts[-1] * 0.3, counts)

d_hi = float(np.abs(band(frame_at(VHI, 3.4)) - band(frame_at(VSUB, 3.4))).mean())
check("同一时刻开/关逐字点亮，字幕区像素差 > 3", d_hi > 3.0, round(d_hi, 2))

# 没词时间的字幕（导入的 SRT）不能出现「点亮」也不会崩
SrtCues = [{"start": 0.0, "end": 4.0, "text": "导入的字幕没有词时间"}]
VNOW, notes_nw = render("vnowords", src=TALK, frame={"mode": "blur"},
                        subtitle={"cues": SrtCues, "style": {"highlight": True}})
b_nw = band(frame_at(VNOW, 2.0))
check("没有词时间时退化成整句显示（全白，不是全黄）",
      white_px(b_nw) > 200 and yellow_px(b_nw) == 0, (white_px(b_nw), yellow_px(b_nw)))
check("没有词时间也不崩、不降级报错", notes_nw == [], notes_nw)

# ---------------- F. 描边色 ----------------

print("\n== F. 描边色真的改了像素 ==")
VBLK, _ = render("vblk", src=TALK, subtitle={"cues": CUES,
                                            "style": {"outline_color": "#000000", "outline": 5}})
VRED, _ = render("vred", src=TALK, subtitle={"cues": CUES,
                                            "style": {"outline_color": "#FF0000", "outline": 5}})
r_black = float(band(frame_at(VBLK, 2.0))[:, :, 2].mean())
r_red = float(band(frame_at(VRED, 2.0))[:, :, 2].mean())
check("红色描边让字幕区 R 通道均值上升", r_red > r_black + 2,
      f"black R={r_black:.2f} red R={r_red:.2f}")

# 底框：真的画上了一条暗带（和不开底框的竖屏版逐像素比，尺寸必须一致）
VBOX, _ = render("vbox", src=TALK, frame={"mode": "blur"},
                 subtitle={"cues": CUES, "style": {"box": True, "box_alpha": 62}})
b_box = band(frame_at(VBOX, 2.0))
b_nobox = band(frame_at(VSUB, 2.0))
dark_box = int((b_box.max(axis=2) < 40).sum())
dark_nobox = int((b_nobox.max(axis=2) < 40).sum())
# 不开底框时暗像素只有文字描边；开了底框会多出整条暗带
check("底框真的画上了（字幕后面出现整条暗带）",
      dark_box > dark_nobox + 15000, (dark_box, dark_nobox))

# 已知限制：本机 ffmpeg/libass 会忽略 BorderStyle=3 的 BackColour alpha，
# 所以 box_alpha 写对了但画面不变。这里只记录，不当作断言（换播放器/新版 libass 可能就生效了）。
VBOX10, _ = render("vbox10", src=TALK, frame={"mode": "blur"},
                   subtitle={"cues": CUES, "style": {"box": True, "box_alpha": 10}})
d_alpha = float(np.abs(band(frame_at(VBOX10, 2.0)) - b_box).mean())
print(f"  ! 已知限制：box_alpha 10 vs 62 的画面差 {d_alpha:.3f}（本机 libass 忽略底框 alpha，"
      f"ASS 里的值是对的，见 test_subtitle_style.py）")

# ---------------- G. 降噪 ----------------

print("\n== G. 轻度降噪 ==")
DN_OFF, _ = render("dn_off", src=NOISE)
DN_ON, notes_g = render("dn_on", src=NOISE, denoise=True)
check("降噪版分辨率不变（-c:v copy，画面不重编）", dims(DN_ON) == dims(NOISE),
      (dims(DN_ON), dims(NOISE)))
sp_off, sp_on = db(DN_OFF, 0.5, 1.0), db(DN_ON, 0.5, 1.0)
ns_off, ns_on = db(DN_OFF, 2.5, 1.0), db(DN_ON, 2.5, 1.0)
check("语音段 RMS 变化 ≤1.5dB", sp_off is not None and sp_on is not None
      and abs(sp_on - sp_off) <= 1.5, f"{sp_off} → {sp_on}")
check("静音段底噪下降 ≥4dB", ns_off is not None and ns_on is not None
      and (ns_off - ns_on) >= 4.0, f"{ns_off} → {ns_on}")
check("降噪没触发降级说明", notes_g == [], notes_g)

# 没有音轨的视频：降噪要能降级而不是整条失败
SILENT = WORK / "silent.mp4"
sh([EXE, "-y", "-hide_banner", "-loglevel", "error",
    "-f", "lavfi", "-i", "color=c=0x204060:s=640x360:d=1:r=25",
    "-c:v", "libx264", "-pix_fmt", "yuv420p", str(SILENT)])
SIL_OUT = WORK / "silent_dn.mp4"
sil_notes = []
renderer.render_cut(str(SILENT), str(SIL_OUT), [(0.0, 1.0)], denoise=True, notes=sil_notes)
check("没音轨时降噪跳过、照常出片", SIL_OUT.exists() and any("没有音轨" in n for n in sil_notes),
      sil_notes)

# ---------------- H. 源已是竖屏：跳过变换 ----------------

print("\n== H. 源本身就是 9:16 → 跳过画幅变换 ==")
VSRC = WORK / "vsrc.mp4"
build(VSRC, "smptebars=size=1080x1920:rate=25:duration=1",
      "sine=frequency=440:duration=1")
VOUT = WORK / "vsrc_out.mp4"
vn = []
renderer.render_cut(str(VSRC), str(VOUT), [(0.0, 1.0)], frame={"mode": "blur"}, notes=vn)
check("输出仍是 1080x1920（没有变形/二次裁切）", dims(VOUT) == (FW, FH), dims(VOUT))
check("既没报错也没降级说明（走了跳过分支）", vn == [], vn)

# ---------------- I. 精调包不沾竖屏/降噪 ----------------

print("\n== I. 精调包（导出剪映用）不许沾竖屏/降噪 ==")
KIT = WORK / "kit"
files = exporter.export_kit(str(BARS), {"fps": 25.0},
                            {"keep_ranges": [[0.0, 2.0]], "decisions": []},
                            KIT, want_clips=True)
check("切出了 1 个素材", len(files.get("clips") or []) == 1, files.get("clips"))
if files.get("clips"):
    cd = dims(files["clips"][0])
    check("clip_01.mp4 分辨率仍是源分辨率 1280x720（没被竖屏影响）", cd == (1280, 720), cd)
check("export_kit 签名里根本没有 frame/denoise",
      "frame" not in str(exporter.export_kit.__code__.co_varnames)
      and "denoise" not in str(exporter.export_kit.__code__.co_varnames),
      exporter.export_kit.__code__.co_varnames)

# ---------------- J. 前端静态检查 ----------------

print("\n== J. 前端静态检查 ==")
html = (ROOT / "web" / "index.html").read_text(encoding="utf-8", errors="replace")
js = (ROOT / "web" / "app.js").read_text(encoding="utf-8", errors="replace")
check('index.html 有画幅三选一 #fxFrame', 'id="fxFrame"' in html)
check("index.html 画幅三个选项齐了",
      all(f'data-val="{v}"' in html for v in ("origin", "blur", "crop")))
check('index.html 有口腔杂音抑制开关 #fxDenoiseOn', 'id="fxDenoiseOn"' in html)
check("index.html 有描边色控件 #fxSubOutline", 'id="fxSubOutline"' in html)
check("index.html 有底框开关与不透明度 #fxSubBox / #fxSubBoxAlpha",
      'id="fxSubBox"' in html and 'id="fxSubBoxAlpha"' in html)
check("index.html 有逐字点亮与高亮色 #fxSubHighlight / #fxSubHlColor",
      'id="fxSubHighlight"' in html and 'id="fxSubHlColor"' in html)

check("FX_DEFAULT 里有 frame: origin", 'frame: "origin"' in js)
check("FX_DEFAULT 里有 denoise", "denoise: false" in js)
payload = js[js.find("function fxPayload("):js.find("function fxPayload(") + 1800]
check("fxPayload() 带 frame", "frame:" in payload, payload[:120])
check("fxPayload() 带 denoise", "denoise:" in payload, payload[:120])
check("fxPayload() 带字幕新样式键",
      all(k in payload for k in ("outline_color", "box:", "box_alpha", "highlight:",
                                 "highlight_color")), payload[:300])

node = shutil.which("node")
if node:
    r = subprocess.run([node, "--check", str(ROOT / "web" / "app.js")],
                       capture_output=True, text=True, errors="replace")
    check("node --check app.js 通过", r.returncode == 0, (r.stdout or "") + (r.stderr or ""))
else:
    check("node --check app.js 通过", False, "找不到 node")

# ---------------- K. 竖屏长句折行（P1：libass 不给中文折行） ----------------

print("\n== K. 竖屏长句折行（18 字不许切边） ==")
LONG = ("这是自动转写断句出来的一行字幕大概有这么长的句子" * 2)[:18]
VWRAP, notes_k = render("vwrap", src=TALK, frame={"mode": "blur"},
                        subtitle={"cues": [{"start": 0.0, "end": 4.0, "text": LONG}],
                                  "style": {}})
check("18 字竖屏渲染没降级", notes_k == [], notes_k)
sub_k = band(frame_at(VWRAP, 2.0))
mk = bright_mask(sub_k)
r_k = np.where(mk.any(axis=1))[0]
c_k = np.where(mk.any(axis=0))[0]
kw = int(c_k.max() - c_k.min() + 1) if len(c_k) else 0
kh = int(r_k.max() - r_k.min() + 1) if len(r_k) else 0
check("18 字竖屏：字幕外接框宽度 ≤ 1000px（画面 1080）", 0 < kw <= 1000, kw)
check("18 字竖屏：左右各留 ≥40px（不切字）",
      len(c_k) and c_k.min() >= 40 and (FW - 1 - c_k.max()) >= 40,
      (int(c_k.min()), int(FW - 1 - c_k.max())) if len(c_k) else None)
check("18 字竖屏：真的折成了 2 行（行高 ≥120px）", kh >= 120, kh)

# 24 字（断句软上限）折 3 行也不许越界
LONG24 = ("这是自动转写断句出来的一行字幕大概有这么长的句子" * 2)[:24]
VWRAP3, _ = render("vwrap3", src=TALK, frame={"mode": "blur"},
                   subtitle={"cues": [{"start": 0.0, "end": 4.0, "text": LONG24}],
                             "style": {}})
sub_k3 = band(frame_at(VWRAP3, 2.0))
m3 = bright_mask(sub_k3)
c3 = np.where(m3.any(axis=0))[0]
w3 = int(c3.max() - c3.min() + 1) if len(c3) else 0
h3 = int(np.where(m3.any(axis=1))[0].max() - np.where(m3.any(axis=1))[0].min() + 1) if len(c3) else 0
check("24 字竖屏：宽度仍 ≤1000（折 3 行没越界）", 0 < w3 <= 1000, w3)
check("24 字竖屏：行块比 18 字更高（3 行 ≈ 1.5 倍）", h3 > kh * 1.2, (h3, kh))

# 横屏 18 字：不折行（ASS 里没有 \N），字幕仍是一行
HW, notes_h = render("hwrap", src=TALK, frame=None,
                     subtitle={"cues": [{"start": 0.0, "end": 4.0, "text": LONG}],
                               "style": {}})
sub_h2 = frame_at(HW, 2.0)[int(720 * 0.80):int(720 * 0.95)].astype(np.int16)
mh = bright_mask(sub_h2)
rh = np.where(mh.any(axis=1))[0]
hh = int(rh.max() - rh.min() + 1) if len(rh) else 0
check("横屏 18 字仍是一行（行高 <120px，没被折行逻辑波及）", 0 < hh < 120, hh)
ass_h = submod.build_ass([{"start": 0.0, "end": 4.0, "text": LONG}], 1920, 1080, {})
check("横屏 18 字的 ASS 里没有任何 \\N", "\\N" not in ass_h, ass_h.split("[Events]")[-1])

# ---- R1/R2/R3 边界：导入 SRT 的 40–60 字长行、无空格长串、中英混排 ----
print("\n== K2. 导入 SRT 的长行：36/48/60 字 + 无空格长串 ==")
LONG3 = ("这是自动转写断句出来的一行字幕大概有这么长的句子" * 3)
URL = "https://example.com/a/very/long/path/without/any/spaces"
MIX = "这个功能叫 auto cut 很好用"
K2 = [(LONG3[:36], 0.1), (LONG3[:48], 0.9), (LONG3[:60], 1.7), (URL, 2.5), (MIX, 3.3)]
k2_cues = [{"start": t, "end": t + 0.7, "text": s} for s, t in K2]
VK2, notes_k2 = render("vk2", src=TALK, frame={"mode": "blur"},
                       subtitle={"cues": k2_cues, "style": {}})
check("长行渲染没降级", notes_k2 == [], notes_k2)
for text, t in K2:
    png = WORK / ("k2_%s.png" % str(t).replace(".", "_"))
    sh([EXE, "-y", "-hide_banner", "-loglevel", "error", "-ss", f"{t + 0.35:.2f}",
        "-i", str(VK2), "-frames:v", "1", str(png)])
    im = cv2.imread(str(png)).astype(np.int16)
    bd = im[int(FH * 0.62):int(FH * 0.97)]          # 5 行字幕会往上长，band 放宽
    mm = bd.max(axis=2) > 150
    cc = np.where(mm.any(axis=0))[0]
    rr = np.where(mm.any(axis=1))[0]
    if not len(cc):
        check(f"「{text[:6]}…」({len(text)} 字) 有字幕", False, "没找到字幕像素")
        continue
    lw, rw = int(cc.min()), int(FW - 1 - cc.max())
    bh = int(rr.max() - rr.min() + 1)
    check(f"「{text[:6]}…」({len(text)} 字符)：左右余量 ≥40px",
          lw >= 40 and rw >= 40, (lw, rw, bh))

# ---------------- 汇总 ----------------

print("\n" + "=" * 40)
try:
    shutil.rmtree(WORK, ignore_errors=True)
except Exception:
    pass
if FAIL:
    print(f"通过 {OK} 项，失败 {len(FAIL)} 项：")
    for n in FAIL:
        print("  -", n)
    sys.exit(1)
print(f"通过 {OK} 项，全部通过")
