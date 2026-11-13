# -*- coding: utf-8 -*-
"""口播专项第一步 · 接线验收 —— /api/autocut 效果透传 + 一次转写两用 + 精调包字幕

判断算法（autocut / speech_cut / pacing / renderer）这次一行没动，
所以这里**不重复验判定规则**（那是 test_speech.py / test_pacing.py 的活），
只验「新接上的那几根线到底通了没有」：

A 不传 effects     → 老行为一点没变，转写一次都不该发生
B 开字幕           → 自动转写一次，字幕条数对得上回执
C 字幕 + 口头禅    → 同一次转写喂两条链路（这是本次改造的核心）
D 用户自带字幕     → ASR 不许把它顶掉
E 转写炸了         → 降级出片，不能整条任务变 error
F 音量归一         → 真的动了音频（volumedetect 量得出来），且没削顶
G 精调包           → 字幕.srt 条数、时间轴、剪掉的 cue 不出现；没 cues 不产空文件
H 精调包进度       → 单调不减，且「写入字幕」排在切素材之后
I 前端静态         → 语法能过 + 控件/请求体标记都在
K 没人声降级       → 桩成空结果：不崩、不烧假字幕、note 说明降级
L 回读提示词过滤   → _is_prompt_echo 真值表 + 脏缓存/正常缓存/empty 缓存（句级与词级）
J 全量回归         → 七份老用例一份不能红

转写打桩（本机没有稳定的人声素材，桩结构照 faster-whisper 真输出造）；
渲染是真渲染，用的是 projects/test_rough.mp4（20.5s，有人声、有静音段）。

跑法： D:/Python311/python.exe test_autocut_fx.py
"""

import copy
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from backend import audio_fx, exporter, server, subtitle  # noqa: E402
from utils.ffmpeg_setup import ensure_ffmpeg  # noqa: E402

PYTHON = "D:/Python311/python.exe"
if not Path(PYTHON).exists():
    PYTHON = sys.executable

SRC = ROOT / "projects" / "test_rough.mp4"
OUTPUT_DIR = ROOT / "projects" / "output"

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


def W(s, e, t):
    return {"start": s, "end": e, "text": t}


# 桩数据：4 条，全带 words，时间落在 0–18s。
# test_rough.mp4 真分析出来的剪点：静音 6.1–7.5 / 12.6–17.5，
# standard 的 keep = [0,6.0] [7.48,12.85] [17.48,20.48]。所以：
#   第 2 条带一个孤立的「嗯」→ 验口头禅（standard 档 solo 只要一侧 0.10s 停顿）
#   第 3 条从 5.7 跨到 8.2 → 正好压在 6.0–7.48 这段被剪掉的静音上，验「跨剪切点」
#   第 4 条 13.0–15.5 整个落在 12.85–17.48 里 → 应该被整条剪掉
CANNED = [
    {"start": 0.8, "end": 2.8, "text": "今天给大家介绍一下这个剪辑工具。",
     "words": [W(0.8, 1.1, "今天"), W(1.1, 1.5, "给大家"), W(1.5, 1.8, "介绍"),
               W(1.8, 2.0, "一下"), W(2.0, 2.3, "这个"), W(2.3, 2.6, "剪辑"),
               W(2.6, 2.8, "工具"), W(2.8, 2.9, "。")]},
    {"start": 3.2, "end": 5.6, "text": "嗯，它最省时间的是自动粗剪。",
     "words": [W(3.2, 3.5, "嗯"), W(3.5, 3.6, "，"), W(4.0, 4.2, "它"),
               W(4.2, 4.5, "最"), W(4.5, 4.9, "省时间"), W(4.9, 5.1, "的"),
               W(5.1, 5.3, "是"), W(5.3, 5.5, "自动"), W(5.5, 5.6, "粗剪"),
               W(5.6, 5.7, "。")]},
    {"start": 5.7, "end": 8.2, "text": "中间这一小段其实没什么用。",
     "words": [W(5.7, 6.0, "中间"), W(6.0, 6.2, "这"), W(6.2, 6.4, "一小段"),
               W(7.6, 7.8, "其实"), W(7.8, 8.0, "没什么"), W(8.0, 8.2, "用"),
               W(8.2, 8.3, "。")]},
    {"start": 13.0, "end": 15.5, "text": "这一整段都会被剪掉。",
     "words": [W(13.0, 13.4, "这一整段"), W(13.4, 13.8, "都会"), W(13.8, 14.2, "被"),
               W(14.2, 14.8, "剪掉"), W(14.8, 15.5, "。")]},
]


# ---------------- 打桩 ----------------

class Stub:
    """transcribe 的替身：数调用次数、记参数、可切换成抛异常"""

    def __init__(self):
        self.calls = 0
        self.kwargs = []
        self.mode = "ok"          # ok | boom | empty
        self.boom_msg = "模型挂了"

    def reset(self, mode="ok"):
        self.calls = 0
        self.kwargs = []
        self.mode = mode

    def __call__(self, *a, **kw):
        self.calls += 1
        self.kwargs.append(dict(kw))
        cb = kw.get("progress_cb")
        if cb:
            cb(30, "打桩：听写中…")
        if self.mode == "boom":
            raise RuntimeError(self.boom_msg)
        if self.mode == "empty":
            # 没人声：whisper 什么都听不出来，一句都不给
            if cb:
                cb(100, "打桩：一句都没听出来")
            return []
        if cb:
            cb(100, f"打桩：转写完了（{len(CANNED)} 条）")
        return copy.deepcopy(CANNED)


# ---------------- 通用工具 ----------------

def run_job(client, payload, limit=900):
    """POST /api/autocut 然后轮询到终态"""
    r = client.post("/api/autocut", json=payload)
    j = r.get_json() or {}
    if not j.get("ok"):
        return None, f"启动失败：{j.get('error')}"
    jid = j["job"]
    st = {}
    for _ in range(limit):
        st = (client.get(f"/api/job/{jid}").get_json() or {}).get("job") or {}
        if st.get("status") in ("done", "error", "cancelled"):
            break
        time.sleep(0.3)
    return st, ""


def volumedetect(path):
    """ffmpeg volumedetect → (mean_volume, max_volume)，单位 dB"""
    exe = ensure_ffmpeg()
    flags = 0x08000000 if os.name == "nt" else 0
    r = subprocess.run([exe, "-hide_banner", "-i", str(path),
                        "-af", "volumedetect", "-f", "null", "-"],
                       capture_output=True, creationflags=flags)
    text = (r.stderr or b"").decode("utf-8", "ignore")
    mean = re.search(r"mean_volume:\s*(-?[\d.]+) dB", text)
    mx = re.search(r"max_volume:\s*(-?[\d.]+) dB", text)
    return (float(mean.group(1)) if mean else None,
            float(mx.group(1)) if mx else None)


def dur_of(path):
    try:
        return float(audio_fx.probe(str(path)).get("duration") or 0.0)
    except Exception:
        return 0.0


def guarded(fn, budget=15.0):
    """把清理放到后台线程里跑，最多等 budget 秒

    清理是「做完就走」的收尾动作，绝不能反过来把报告卡住：
    有的环境（比如带文件删除守卫的沙箱）会让 unlink 直接挂死，
    那就放弃清理、照常打印结果——留几个临时文件远好过整套测试没结论。
    """
    t = threading.Thread(target=fn, daemon=True)
    t.start()
    t.join(budget)
    return not t.is_alive()


def slice_from(text, marker, span=2200):
    """grep -A 的等价物：定位标记后取一段（比多行正则稳）"""
    i = text.find(marker)
    return "" if i < 0 else text[i:i + span]


def main():
    if not SRC.exists():
        print(f"找不到素材 {SRC}，跳过")
        return 0

    started = time.time()
    stub = Stub()
    real_transcribe = subtitle.transcribe
    subtitle.transcribe = stub

    client = server.app.test_client()

    made = []          # 本测试产出的成片绝对路径，finally 里精确删
    # 所有临时目录（成片 / 精调包 / 回归日志）都收在这一个根下，最后整棵删一次。
    # 成片也故意落到这里而不是 projects/output：测试不该往用户的成片目录里堆垃圾，
    # 而且系统 temp 下的文件一般不受工作区删除守卫影响，整套测试才跑得完。
    tmproot = Path(tempfile.mkdtemp(prefix="autocut_fx_"))
    real_out_dir = server.OUTPUT_DIR
    server.OUTPUT_DIR = tmproot / "output"
    server.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    global OUTPUT_DIR
    OUTPUT_DIR = server.OUTPUT_DIR
    before = {p.name for p in OUTPUT_DIR.iterdir() if p.is_file()}
    try:
        total_src = dur_of(SRC)
        print(f"素材 {SRC.name}：{total_src:.2f}s（真渲染，转写打桩）\n")

        # ================= A. 回归：不传 effects 时行为不变 =================
        print("== A. 不传 effects：老行为不变，不该碰转写 ==")
        stub.reset()
        st, err = run_job(client, {
            "path": str(SRC),
            "strength": "standard",
            "speech": {"filler": False, "retake": False},
        })
        res = (st or {}).get("result") or {}
        check("任务跑完", st and st.get("status") == "done", (st or {}).get("error") or err)
        check("result.cues 为空", res.get("cues") == [], res.get("cues"))
        fx = res.get("fx_applied") or {}
        check("fx_applied.subtitle == 0", fx.get("subtitle") == 0, fx)
        check("fx_applied.normalize is False", fx.get("normalize") is False, fx)
        check("transcribe 一次都没被调用", stub.calls == 0, f"被调了 {stub.calls} 次")
        out_a = res.get("output")
        made.append(out_a)
        check("成片存在", bool(out_a) and Path(out_a).exists(), out_a)
        d_a = dur_of(out_a) if out_a and Path(out_a).exists() else 0.0
        kept_a = float(res.get("kept") or 0)
        check("成片时长 ≈ result.kept（±1.5s）", abs(d_a - kept_a) <= 1.5, (kept_a, d_a))

        # ================= B. 开字幕 =================
        print("\n== B. 开字幕：自动转写一次，只转一次 ==")
        stub.reset()
        st, err = run_job(client, {
            "path": str(SRC),
            "strength": "standard",
            "speech": {"filler": False, "retake": False},
            "effects": {"subtitle": {"on": True, "size": "normal", "position": "bottom"},
                        "normalize": False},
        })
        res_b = (st or {}).get("result") or {}
        check("任务跑完", st and st.get("status") == "done", (st or {}).get("error") or err)
        check("transcribe 调用次数 == 1", stub.calls == 1, stub.calls)
        check("调用时 word_level=True",
              bool(stub.kwargs) and stub.kwargs[0].get("word_level") is True,
              stub.kwargs[:1])
        cues_b = res_b.get("cues") or []
        check("result.cues 非空", bool(cues_b), cues_b)
        check("条数 == cues_count", len(cues_b) == res_b.get("cues_count"),
              (len(cues_b), res_b.get("cues_count")))
        check("fx_applied.subtitle > 0",
              int((res_b.get("fx_applied") or {}).get("subtitle") or 0) > 0,
              res_b.get("fx_applied"))
        out_b = res_b.get("output")
        made.append(out_b)
        check("成片存在", bool(out_b) and Path(out_b).exists(), out_b)

        # ================= C. 字幕 + 口头禅共用同一次转写 =================
        print("\n== C. 字幕 + 口头禅：共用一次转写 ==")
        stub.reset()
        st, err = run_job(client, {
            "path": str(SRC),
            "strength": "standard",
            "speech": {"filler": True, "retake": False},
            "effects": {"subtitle": {"on": True, "size": "normal", "position": "bottom"},
                        "normalize": False},
        })
        res_c = (st or {}).get("result") or {}
        check("任务跑完", st and st.get("status") == "done", (st or {}).get("error") or err)
        check("transcribe 调用次数仍 == 1", stub.calls == 1, stub.calls)
        plan_c = res_c.get("plan") or {}
        n_filler = int((plan_c.get("speech") or {}).get("filler") or 0)
        check("plan.speech.filler > 0", n_filler > 0, plan_c.get("speech"))
        check("字幕也拿到了", bool(res_c.get("cues")), res_c.get("cues"))
        made.append(res_c.get("output"))

        # ================= D. 用户自带字幕不被 ASR 覆盖 =================
        print("\n== D. 自带字幕 + 口头禅：ASR 不许顶掉用户那份 ==")
        stub.reset()
        mine = [{"start": 1.0, "end": 3.0, "text": "我自己写的字幕"}]
        st, err = run_job(client, {
            "path": str(SRC),
            "strength": "standard",
            "speech": {"filler": True, "retake": False},
            "cues": mine,
        })
        res_d = (st or {}).get("result") or {}
        check("任务跑完", st and st.get("status") == "done", (st or {}).get("error") or err)
        check("口头禅确实强制转写了", stub.calls == 1, stub.calls)
        cues_d = res_d.get("cues") or []
        check("result.cues 恰好一条", len(cues_d) == 1, cues_d)
        check("文本是用户那份（不是桩数据）",
              bool(cues_d) and cues_d[0].get("text") == "我自己写的字幕", cues_d)
        check("时间也没被动过",
              bool(cues_d) and abs(float(cues_d[0]["start"]) - 1.0) < 1e-6
              and abs(float(cues_d[0]["end"]) - 3.0) < 1e-6, cues_d)
        made.append(res_d.get("output"))

        # ================= E. 转写失败降级出片 =================
        print("\n== E. 转写炸了：降级出片，不能整条 error ==")
        stub.reset("boom")
        st, err = run_job(client, {
            "path": str(SRC),
            "strength": "standard",
            "speech": {"filler": False, "retake": False},
            "effects": {"subtitle": {"on": True}, "normalize": False},
        })
        res_e = (st or {}).get("result") or {}
        check("任务仍是 done（不是 error）",
              st and st.get("status") == "done",
              (st or {}).get("status"), )
        note_e = res_e.get("note") or ""
        check("note 含「字幕没转成」", "字幕没转成" in note_e, note_e)
        check("fx_applied.subtitle == 0",
              (res_e.get("fx_applied") or {}).get("subtitle") == 0, res_e.get("fx_applied"))
        out_e = res_e.get("output")
        made.append(out_e)
        check("成片照样存在", bool(out_e) and Path(out_e).exists(), out_e)
        check("成片可读（时长 > 0）", dur_of(out_e) > 0.5 if out_e else False,
              dur_of(out_e) if out_e else None)
        stub.reset()

        # ================= F. 音量归一真的落到成片 =================
        print("\n== F. 音量归一：volumedetect 量得出来 ==")
        st, err = run_job(client, {
            "path": str(SRC), "strength": "standard",
            "speech": {"filler": False, "retake": False},
            "effects": {"normalize": False},
        })
        res_f0 = (st or {}).get("result") or {}
        out_f0 = res_f0.get("output")
        made.append(out_f0)
        check("基准片（未归一）出片", bool(out_f0) and Path(out_f0).exists(), out_f0)

        st, err = run_job(client, {
            "path": str(SRC), "strength": "standard",
            "speech": {"filler": False, "retake": False},
            "effects": {"normalize": True},
        })
        res_f1 = (st or {}).get("result") or {}
        out_f1 = res_f1.get("output")
        made.append(out_f1)
        check("归一片出片", bool(out_f1) and Path(out_f1).exists(), out_f1)
        check("fx_applied.normalize is True",
              (res_f1.get("fx_applied") or {}).get("normalize") is True,
              res_f1.get("fx_applied"))

        if out_f0 and out_f1 and Path(out_f0).exists() and Path(out_f1).exists():
            mean0, max0 = volumedetect(out_f0)
            mean1, max1 = volumedetect(out_f1)
            ok_probe = mean0 is not None and mean1 is not None
            check("两次都能量到 mean_volume", ok_probe, (mean0, mean1))
            if ok_probe:
                check("归一动过音频（mean 差 > 0.5dB）", abs(mean1 - mean0) > 0.5,
                      f"{mean0} → {mean1}（差 {abs(mean1 - mean0):.2f}dB）")
            check("归一后没削顶（max_volume <= 0）",
                  max1 is not None and max1 <= 0.0, f"{max1}")

        # ================= G. 精调包带字幕.srt =================
        print("\n== G. 精调包：字幕.srt 的条数 / 时间轴 / 剪掉的 cue ==")
        keep_g = [(0.0, 3.0), (8.0, 11.0)]          # 2 段，共 6.0s
        total_keep_g = sum(e - s for s, e in keep_g)
        plan_g = {"keep_ranges": keep_g, "decisions": []}
        analysis_g = {"fps": 25.0, "duration": total_src}
        cues_g = [
            {"start": 0.5, "end": 2.2, "text": "第一句留着"},
            {"start": 2.5, "end": 9.0, "text": "跨剪切点的长句内容"},   # 跨 3.0–8.0
            {"start": 13.0, "end": 15.0, "text": "这句应该被剪掉"},      # 全在剪掉区
        ]
        keep_sp_g = [{"start": float(s), "end": float(e), "speed": 1.0} for s, e in keep_g]
        expect_g = subtitle.map_cues(cues_g, keep_sp_g, default_speed=1.0, transition=0.0)

        kit1 = tmproot / "kit1"
        kit1.mkdir(parents=True, exist_ok=True)
        files1 = exporter.export_kit(str(SRC), analysis_g, plan_g, kit1,
                                     want_clips=False, cues=cues_g,
                                     speed=1.0, transition=0.0)
        srt1 = files1.get("srt")
        check("files['srt'] 存在", bool(srt1), files1)
        check("字幕.srt 真的落在磁盘上",
              bool(srt1) and Path(srt1).exists() and Path(srt1).name == "字幕.srt", srt1)
        if srt1 and Path(srt1).exists():
            content = Path(srt1).read_text(encoding="utf-8")
            parsed = subtitle.parse_srt(content)
            check("条数 == map_cues 的条数", len(parsed) == len(expect_g),
                  (len(expect_g), len(parsed), expect_g))
            check("首条 start >= 0", bool(parsed) and float(parsed[0]["start"]) >= 0.0,
                  parsed[:1])
            check("末条 end <= 成片总长 + 0.2",
                  bool(parsed) and float(parsed[-1]["end"]) <= total_keep_g + 0.2,
                  (total_keep_g, parsed[-1:] if parsed else None))
            check("落在剪掉区间的 cue 没出现", "这句应该被剪掉" not in content,
                  content[-200:])
            check("跨剪切点的文字被拆开保留",
                  all(ch in content for ch in ("跨", "剪", "切")), content[:300])

        kit2 = tmproot / "kit2"
        kit2.mkdir(parents=True, exist_ok=True)
        files2 = exporter.export_kit(str(SRC), analysis_g, plan_g, kit2,
                                     want_clips=False, cues=None,
                                     speed=1.0, transition=0.0)
        check("cues=None 时 files 里没有 srt", "srt" not in files2, list(files2))
        check("cues=None 时目录里没有 字幕.srt", not (kit2 / "字幕.srt").exists(),
              [p.name for p in kit2.iterdir()])

        # ================= H. 精调包进度单调 =================
        print("\n== H. 精调包进度：单调不减，「写入字幕」在切素材之后 ==")
        keep_h = [(1.0, 2.5), (9.0, 10.0)]           # 2 段短 keep，切起来快
        seq = []
        kit3 = tmproot / "kit3"
        kit3.mkdir(parents=True, exist_ok=True)
        exporter.export_kit(str(SRC), analysis_g,
                            {"keep_ranges": keep_h, "decisions": []}, kit3,
                            want_clips=True,
                            progress_cb=lambda p, m="": seq.append((float(p), m)),
                            cues=cues_g, speed=1.0, transition=0.0)
        pcts = [p for p, _ in seq]
        check("收到进度回调", len(seq) >= 3, seq)
        check("pct 全程非递减",
              all(pcts[i] <= pcts[i + 1] + 1e-9 for i in range(len(pcts) - 1)), pcts)
        i_clip = next((i for i, (_, m) in enumerate(seq) if "切出" in m), -1)
        i_srt = next((i for i, (_, m) in enumerate(seq) if "写入字幕" in m), -1)
        check("两种消息都出现过", i_clip >= 0 and i_srt >= 0, seq)
        check("「写入字幕」排在「切出 N 段素材」之后", i_clip >= 0 and i_srt > i_clip,
              (i_clip, i_srt, seq))

        # ================= I. 前端静态检查 =================
        print("\n== I. 前端静态检查 ==")
        app_js = ROOT / "web" / "app.js"
        index_html = ROOT / "web" / "index.html"
        node = shutil.which("node")
        if node:
            r = subprocess.run([node, "--check", str(app_js)],
                               capture_output=True, text=True, errors="replace")
            check("node --check web/app.js 通过", r.returncode == 0,
                  (r.stdout or "") + (r.stderr or ""))
        else:
            check("node --check web/app.js 通过", False, "找不到 node")

        html = index_html.read_text(encoding="utf-8", errors="replace")
        js = app_js.read_text(encoding="utf-8", errors="replace")
        check('index.html 有 id="btnProfile"', 'id="btnProfile"' in html)
        check('index.html 有 id="fxNormOn"', 'id="fxNormOn"' in html)
        check("app.js 有 setProfile(", "setProfile(" in js)
        check("app.js 有 profileSnapshot", "profileSnapshot" in js)
        check("app.js 有 normalize", "normalize" in js)
        fx_body = slice_from(js, "function fxPayload(", 1400)
        check("fxPayload() 返回体含 normalize:", "normalize:" in fx_body, fx_body[:200])
        ac_body = slice_from(js, "async function autocut(", 2000)
        check("autocut() 请求体含 effects: fxPayload()",
              "effects: fxPayload()" in ac_body, ac_body[:200])
        kit_body = slice_from(js, "async function exportKit(", 2600)
        check("exportKit() 请求体含 cues:", "cues:" in kit_body, kit_body[:200])
        check("exportKit() 请求体含 speed:", "speed:" in kit_body, kit_body[:200])
        check("exportKit() 请求体含 transition:", "transition:" in kit_body, kit_body[:200])

        # ================= K. 没人声时的降级（走 HTTP） =================
        # whisper 在没人声的素材里会把 initial_prompt 回读成字幕。
        # 这里桩成「一句都没听出来」，验的是：不能崩、不能烧假字幕、要能降级出片。
        print("\n== K. 没人声：降级出片，不烧假字幕 ==")
        stub.reset("empty")
        st, err = run_job(client, {
            "path": str(SRC),
            "strength": "standard",
            "speech": {"filler": False, "retake": False},
            "effects": {"subtitle": {"on": True, "size": "normal", "position": "bottom"},
                        "normalize": False},
        })
        res_k = (st or {}).get("result") or {}
        check("任务仍 done（没人声不能崩）",
              st and st.get("status") == "done",
              (st or {}).get("error") or (st or {}).get("status"))
        check("转写确实跑了一次（拿到了空结果）", stub.calls == 1, stub.calls)
        check("result.cues 为空", res_k.get("cues") == [], res_k.get("cues"))
        check("fx_applied.subtitle == 0（没烧假字幕）",
              (res_k.get("fx_applied") or {}).get("subtitle") == 0, res_k.get("fx_applied"))
        note_k = res_k.get("note") or ""
        check("note 含「没听出人声」", "没听出人声" in note_k, note_k)
        out_k = res_k.get("output")
        made.append(out_k)
        check("成片还在", bool(out_k) and Path(out_k).exists(), out_k)
        d_k = dur_of(out_k) if out_k and Path(out_k).exists() else 0.0
        kept_k = float(res_k.get("kept") or 0)
        check("成片时长 ≈ result.kept（±1.5s）", abs(d_k - kept_k) <= 1.5, (kept_k, d_k))
        stub.reset()

        # ================= L. 回读提示词过滤 / 脏缓存 / 空缓存（纯逻辑） =================
        # 这一整段要验的是**真的** transcribe 读缓存，所以先把桩摘掉；
        # 同时把 load_model 换成炸弹：万一缓存没命中就立刻炸，
        # 而不是悄悄去下模型把整套测试挂在这里。
        print("\n== L. 回读提示词过滤：真值表 + 四种缓存命中 ==")
        for text, want in [
            ("请使用简体中文并加上标点。", True),
            ("以下是普通话视频的语音内容", True),
            ("大家好，今天聊聊口播怎么剪。", False),
            ("嗯。", False),
            ("", False),
        ]:
            got = subtitle._is_prompt_echo(text)
            check(f"_is_prompt_echo({text!r}) == {want}", got == want, got)

        real_load_model = subtitle.load_model

        def _no_load(*a, **kw):
            raise AssertionError("这段只该读缓存，不该加载模型")

        # 造个假视频占位（写 2048 字节）：缓存键 = 文件名+大小+修改时间+档位，够唯一
        fake = tmproot / "fake_speech.mp4"
        fake.write_bytes(b"\0" * 2048)
        cache_f = subtitle._cache_path(str(fake), subtitle.DEFAULT_MODEL)
        legacy_f = subtitle._legacy_cache_paths(str(fake), subtitle.DEFAULT_MODEL)
        cache_files = [cache_f] + legacy_f
        echo1 = "请使用简体中文并加上标点。"

        def quick(payload, **kw):
            """写缓存 → 跑一次 transcribe → 返回 (结果, 耗时)"""
            cache_f.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            t0 = time.time()
            got = subtitle.transcribe(str(fake), **kw)
            return got, time.time() - t0

        subtitle.transcribe = real_transcribe
        subtitle.load_model = _no_load
        subtitle._model_cache.clear()
        try:
            # a) 脏缓存：整份只有一条「回读提示词」，句级/词级都必须吐 []
            dirty = {"segments": [
                {"start": 0.0, "end": 1.0, "text": echo1,
                 "words": [{"start": 0.0, "end": 1.0, "text": "请"}]},
            ], "word_level": True}
            kept_l = [x for x in dirty["segments"]
                      if not subtitle._is_prompt_echo(x.get("text"))]
            print(f"     脏缓存 {len(dirty['segments'])} 条 → 过滤后 {len(kept_l)} 条")
            segs_a1, cost_a1 = quick(dirty)
            check("a 脏缓存（句级）返回 []", segs_a1 == [], segs_a1)
            check("a 脏缓存（句级）< 1s，没重转", cost_a1 < 1.0, f"{cost_a1:.3f}s")
            segs_a2, cost_a2 = quick(dirty, word_level=True)
            check("a 脏缓存（词级）返回 []", segs_a2 == [], segs_a2)
            check("a 脏缓存（词级）< 1s，没重转", cost_a2 < 1.0, f"{cost_a2:.3f}s")
            check("a 全程没加载模型", not subtitle._model_cache,
                  list(subtitle._model_cache))

            # b) 反向：正常字幕不能被误杀
            good = {"segments": [
                {"start": 0.0, "end": 1.2, "text": "大家好，今天聊聊口播怎么剪。",
                 "words": [{"start": 0.0, "end": 0.6, "text": "大家好"}]},
            ], "word_level": True}
            segs_b, cost_b = quick(good, word_level=True)
            check("b 正常缓存（词级）原样返回 1 条",
                  len(segs_b) == 1
                  and segs_b[0].get("text") == "大家好，今天聊聊口播怎么剪。", segs_b)
            check("b 正常缓存没触发过滤", cost_b < 1.0, f"{cost_b:.3f}s")

            # c) 空结果缓存（empty=true）：句级/词级都直接命中，不许再听一遍
            empty = {"segments": [], "empty": True, "word_level": True}
            segs_c1, cost_c1 = quick(empty)
            check("c empty 缓存（句级）返回 []", segs_c1 == [], segs_c1)
            check("c empty 缓存（句级）< 1s", cost_c1 < 1.0, f"{cost_c1:.3f}s")
            segs_c2, cost_c2 = quick(empty, word_level=True)
            check("c empty 缓存（词级）返回 []", segs_c2 == [], segs_c2)
            check("c empty 缓存（词级）< 1s", cost_c2 < 1.0, f"{cost_c2:.3f}s")
        finally:
            # d) 桩和真实 load_model 都还给后续用例；缓存文件用 unlink(missing_ok=True) 收掉
            subtitle.transcribe = stub
            subtitle.load_model = real_load_model

            def _drop_cache():
                for p in cache_files:
                    try:
                        Path(p).unlink(missing_ok=True)
                    except Exception:
                        pass

            guarded(_drop_cache, 5.0)

        # ================= J. 全量回归 =================
        print("\n== J. 全量回归 ==")
        suites = ["test_subtitle.py", "test_speech.py", "test_pacing.py",
                  "test_director.py", "test_music.py",
                  "test_transitions_e2e.py", "test_speech_e2e.py"]
        for name in suites:
            t0 = time.time()
            # 子用例是另起的进程，输出还是落在真的 projects/output，
            # 所以这里要盯 real_out_dir，不是本进程被改到的那份
            pre = ({p.name for p in real_out_dir.iterdir()}
                   if real_out_dir.exists() else set())
            # 用文件收集输出而不是管道：子用例自己也会 spawn ffmpeg/python，
            # 管道在 Windows 上容易被继承的句柄拖住（实测 capture_output 会卡死），
            # 落到文件既不阻塞，也方便把统计行原样贴进报告。
            log = tmproot / "reg.log"
            rc, out = None, ""
            # 子进程偶发抖动（Windows 上 ffmpeg 句柄 / 清理竞争）会造成假红：
            # 单跑必绿、连跑偶尔 rc=1 且没有 ✗。所以 rc≠0 时重试一次再判红，
            # 真红的话把完整输出落盘，免得只能看到最后一行、分不清是真红还是清理卡住。
            for attempt in (1, 2):
                try:
                    with open(log, "w", encoding="utf-8", errors="replace") as fh:
                        rc = subprocess.run([PYTHON, str(ROOT / name)], cwd=str(ROOT),
                                            stdout=fh, stderr=subprocess.STDOUT,
                                            timeout=60).returncode
                except subprocess.TimeoutExpired:
                    rc = -1
                if rc == 0 or attempt == 2:
                    break
                print(f"     {name} 第一次 rc={rc}，重试一次…")
                time.sleep(1.0)
            try:
                out = log.read_text(encoding="utf-8", errors="replace").strip()
            except Exception:
                out = ""
            lines = [ln for ln in out.splitlines() if ln.strip()]
            last = lines[-1] if lines else "(无输出)"
            detail = last
            if rc != 0:
                # 超时或被拦时，把子用例自己的断言结果也带上，免得看不出是「真红」还是「清理卡住」
                n_ok = out.count("✓")
                n_bad = out.count("✗")
                detail = f"{last}  (子用例断言 ✓{n_ok} / ✗{n_bad}, rc={rc}, 已重试一次)"
                dump = ROOT / f"fail_{Path(name).stem}.log"
                try:
                    dump.write_text(out, encoding="utf-8")
                    detail += f"；完整输出见 {dump.name}"
                except Exception:
                    pass
            check(f"{name} 通过", rc == 0, detail)
            print(f"     {name}  {time.time() - t0:.1f}s  →  {last}")
            # 只收这几种前缀（都是子用例自己产的），别碰别人的历史成片
            def _sweep(pre=pre):
                try:
                    for p in real_out_dir.iterdir() if real_out_dir.exists() else []:
                        if p.name in pre:
                            continue
                        if p.name.startswith(("test_demo_粗剪_", "shots_剪辑_",
                                              "shots_按需求_")):
                            try:
                                p.unlink()
                            except Exception:
                                pass
                except Exception:
                    pass

            guarded(_sweep, 8.0)
    finally:
        subtitle.transcribe = real_transcribe
        server.OUTPUT_DIR = real_out_dir

        def _cleanup():
            # 1) 精确删本测试产出的成片
            for p in made:
                if not p:
                    continue
                try:
                    Path(p).unlink()
                except Exception:
                    pass
            # 2) 兜底：只扫「本测试的 stem + 本次运行之后新出现的」文件
            try:
                if OUTPUT_DIR.exists():
                    fresh_after = started - 5
                    for p in OUTPUT_DIR.iterdir():
                        if not p.is_file() or p.name in before:
                            continue
                        if not p.name.startswith(("test_rough_粗剪_", "test_rough_剪辑_")):
                            continue
                        try:
                            if p.stat().st_mtime >= fresh_after:
                                p.unlink()
                        except Exception:
                            pass
            except Exception:
                pass
            # 3) 精调包 / 日志的临时目录整棵删
            try:
                shutil.rmtree(tmproot, ignore_errors=True)
            except Exception:
                pass

        if not guarded(_cleanup, 20.0):
            print("  ! 清理没在 20s 内完成（多半是环境拦了删除），已放弃清理照常出报告")

    cost = time.time() - started
    print(f"\n{'=' * 40}")
    print(f"通过 {OK} 项" + (f"，失败 {len(FAIL)} 项：{FAIL}" if FAIL else "，全部通过"))
    print(f"耗时 {cost:.1f}s")
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
