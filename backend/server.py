# -*- coding: utf-8 -*-
"""
本地 Web 服务 - 给前端界面提供接口

设计要点：
- 所有重活（分析、渲染）都在后台线程跑，前端轮询任务进度
- 视频通过 Range 请求流式播放，拖进度条不卡
- 前端每 2 秒心跳一次，窗口关掉后服务自动退出，不留后台僵尸进程
"""

import base64
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from flask import Flask, jsonify, request, send_file, send_from_directory

PROJECT_ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = PROJECT_ROOT / "web"
OUTPUT_DIR = PROJECT_ROOT / "projects" / "output"
VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".flv", ".wmv", ".webm", ".m4v"}

app = Flask(__name__, static_folder=None)


def _log(msg):
    """pythonw 下没有 stdout，print 会抛异常"""
    try:
        print(msg)
    except Exception:
        pass


# ---------------- 任务管理 ----------------
_JOBS = {}
_JOBS_LOCK = threading.Lock()


def new_job(kind):
    jid = uuid.uuid4().hex[:12]
    with _JOBS_LOCK:
        _JOBS[jid] = {
            "id": jid,
            "kind": kind,
            "status": "running",
            "progress": 0,
            "message": "准备中…",
            "result": None,
            "error": None,
            "cancelled": False,
            "created": time.time(),
        }
    return jid


def update_job(jid, **kw):
    with _JOBS_LOCK:
        job = _JOBS.get(jid)
        if job:
            # 进度只增不减：渲染时视频/音频两条回调并行，否则百分比会来回跳
            if "progress" in kw and "status" not in kw:
                prev = job.get("progress") or 0
                if kw["progress"] < prev:
                    kw["progress"] = prev
            job.update(kw)


def job_state(jid):
    with _JOBS_LOCK:
        job = _JOBS.get(jid)
        return dict(job) if job else None


def is_cancelled(jid):
    job = job_state(jid)
    return bool(job and job["cancelled"])


def _clean_old_jobs(max_age=3600):
    now = time.time()
    with _JOBS_LOCK:
        for k in [k for k, v in _JOBS.items() if now - v["created"] > max_age]:
            _JOBS.pop(k, None)


# ---------------- 分析结果缓存 ----------------
# 换档位重剪时不该重扫一遍全片，按「路径 + 修改时间 + 大小」缓存
_ANALYSIS_CACHE = {}
_CACHE_LOCK = threading.Lock()


def _cache_key(path):
    try:
        st = os.stat(path)
        return (str(Path(path).resolve()), int(st.st_mtime), st.st_size)
    except Exception:
        return None


def _cache_get(key):
    if not key:
        return None
    with _CACHE_LOCK:
        return _ANALYSIS_CACHE.get(key)


def _cache_put(key, analysis):
    if not key:
        return
    with _CACHE_LOCK:
        if len(_ANALYSIS_CACHE) > 4:
            _ANALYSIS_CACHE.clear()
        _ANALYSIS_CACHE[key] = analysis


def _public_analysis(analysis):
    """回给前端的分析结果：去掉逐词时间戳

    decide() 要靠 words 判断「句界还是卡壳」，但前端用不上（长片几千条），
    回传只会撑大响应体，所以出门前摘掉。
    """
    speech = analysis.get("speech")
    if not isinstance(speech, dict) or "words" not in speech:
        return analysis
    out = dict(analysis)
    out["speech"] = {k: v for k, v in speech.items() if k != "words"}
    return out


# ---------------- 工具 ----------------
def _is_video(path):
    p = Path(path)
    return p.exists() and p.is_file() and p.suffix.lower() in VIDEO_EXTS


def _python_exe():
    cand = Path(r"D:\Python311\python.exe")
    if cand.exists():
        return str(cand)
    return sys.executable


def _run_hidden(cmd, timeout=300):
    flags = 0x08000000 if os.name == "nt" else 0
    return subprocess.run(cmd, capture_output=True, timeout=timeout, creationflags=flags)


PICK_SCRIPT = r"""
import sys, json, tkinter as tk
from tkinter import filedialog
KIND = sys.argv[1] if len(sys.argv) > 1 else 'video'
MULTI = len(sys.argv) > 2 and sys.argv[2] == 'multi'
TYPES = {
    'video': [('视频文件', '*.mp4 *.mov *.avi *.mkv *.flv *.wmv *.webm'), ('所有文件', '*.*')],
    'audio': [('音频文件', '*.mp3 *.wav *.m4a *.aac *.flac *.ogg *.opus *.wma'), ('所有文件', '*.*')],
    'subtitle': [('字幕文件', '*.srt *.vtt'), ('所有文件', '*.*')],
}
root = tk.Tk()
root.withdraw()
root.attributes('-topmost', True)
root.update()
ft = TYPES.get(KIND, TYPES['video'])
if MULTI:
    got = filedialog.askopenfilenames(title='选择文件（可多选）', filetypes=ft)
    if isinstance(got, str):
        got = [got] if got else []
    out = [p for p in got if p]
else:
    p = filedialog.askopenfilename(title='选择文件', filetypes=ft)
    out = [p] if p else []
root.destroy()
print(json.dumps(out, ensure_ascii=True))
"""


# ---------------- 页面 ----------------
@app.route("/")
def index():
    return send_from_directory(WEB_DIR, "index.html")


@app.route("/web/<path:name>")
def web_assets(name):
    resp = send_from_directory(WEB_DIR, name)
    resp.headers["Cache-Control"] = "no-store"
    return resp


# ---------------- 接口 ----------------
@app.get("/api/ping")
def api_ping():
    global LAST_PING
    LAST_PING = time.time()
    return jsonify(ok=True)


@app.post("/api/pick")
def api_pick():
    """弹出 Windows 原生选文件对话框（不经过浏览器上传，不复制文件）

    body: {"kind": "video" | "audio" | "subtitle", "multi": true}
    返回：{ok, paths: [...], path: 第一个}
    """
    data = request.get_json(force=True, silent=True) or {}
    kind = str(data.get("kind") or "video").strip().lower()
    if kind not in ("video", "audio", "subtitle"):
        kind = "video"
    multi = bool(data.get("multi"))
    try:
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        argv = [_python_exe(), "-c", PICK_SCRIPT, kind] + (["multi"] if multi else [])
        r = subprocess.run(argv, capture_output=True,
                           timeout=600, env=env, cwd=str(PROJECT_ROOT))
        raw = (r.stdout or b"").decode("utf-8", "ignore").strip()
        try:
            paths = json.loads(raw) if raw else []
        except Exception:
            paths = [raw] if raw else []
        paths = [str(p) for p in paths if p]
        if not paths:
            return jsonify(ok=False, cancelled=True, paths=[])
        return jsonify(ok=True, paths=paths, path=paths[0])
    except Exception as e:
        return jsonify(ok=False, error=str(e))


@app.get("/api/info")
def api_info():
    from backend.video_processor import VideoProcessor

    path = request.args.get("path", "")
    if not _is_video(path):
        return jsonify(ok=False, error="文件不存在或不是视频"), 400
    try:
        info = VideoProcessor(path).get_video_info()
        info["size_mb"] = round(Path(path).stat().st_size / 1048576, 2)
        info["name"] = Path(path).name
        return jsonify(ok=True, info=info)
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 400


@app.get("/api/filmstrip")
def api_filmstrip():
    """时间轴胶片条：等距抽帧返回 base64 缩略图"""
    import cv2

    path = request.args.get("path", "")
    try:
        count = max(4, min(24, int(request.args.get("count", 14))))
    except ValueError:
        count = 14
    if not _is_video(path):
        return jsonify(ok=False, error="文件不存在"), 400

    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        return jsonify(ok=False, error="无法读取视频"), 400
    fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    frames = []
    try:
        for i in range(count):
            fno = int(total * (i + 0.5) / count)
            cap.set(cv2.CAP_PROP_POS_FRAMES, fno)
            ok, frame = cap.read()
            if not ok:
                frames.append(None)
                continue
            h, w = frame.shape[:2]
            nh = 96
            nw = max(1, int(w * nh / h))
            small = cv2.resize(frame, (nw, nh))
            ok2, buf = cv2.imencode(".jpg", small, [int(cv2.IMWRITE_JPEG_QUALITY), 70])
            frames.append("data:image/jpeg;base64," + base64.b64encode(buf.tobytes()).decode() if ok2 else None)
    finally:
        cap.release()
    return jsonify(ok=True, frames=frames, fps=fps, duration=total / fps if fps else 0)


@app.route("/api/video")
def api_video():
    path = request.args.get("path", "")
    if not _is_video(path):
        return "not found", 404
    return send_file(path, conditional=True)


@app.post("/api/analyze")
def api_analyze():
    from backend.ai_analyzer import AIAnalyzer

    data = request.get_json(force=True, silent=True) or {}
    path = data.get("path", "")
    if not _is_video(path):
        return jsonify(ok=False, error="文件不存在"), 400

    _clean_old_jobs()
    jid = new_job("analyze")

    def cb(pct, msg=""):
        update_job(jid, progress=round(pct, 1), message=msg or "分析中…")

    def work():
        try:
            analyzer = AIAnalyzer(path)
            suggestions = analyzer.analyze(progress_cb=cb, cancel_flag=lambda: is_cancelled(jid))
            if is_cancelled(jid):
                update_job(jid, status="cancelled", message="已取消")
                return
            update_job(jid, status="done", progress=100, message="分析完成", result={
                "scenes": analyzer.scenes,
                "silent": analyzer.silent_segments,
                "redundant": analyzer.redundant_segments,
                "static": analyzer.static_segments,
                "blank": analyzer.blank_segments,
                "has_audio": analyzer.has_audio,
                "suggestions": suggestions,
                "duration": round(analyzer.duration, 2),
                "fps": round(analyzer.fps, 2),
            })
        except AIAnalyzer.Cancelled:
            update_job(jid, status="cancelled", message="已取消")
        except Exception as e:
            import traceback

            update_job(jid, status="error", error=str(e), message="分析失败",
                       result={"traceback": traceback.format_exc()})

    threading.Thread(target=work, daemon=True).start()
    return jsonify(ok=True, job=jid)


def _speech_kinds(spec):
    """面板/请求里的语音级开关 → 要检测的类型

    spec 可以是 {"filler": True, "retake": True}，也可以是 True（两种都要）
    """
    if not spec:
        return ()
    if spec is True:
        return ("filler", "retake")
    if isinstance(spec, (list, tuple)):
        want = set(str(x) for x in spec)
    else:
        want = {k for k, v in spec.items() if v}
    return tuple(k for k in ("filler", "retake") if k in want)


def _enrich_speech(analysis, path, kinds, strength, jid=None, progress_cb=None, segs=None):
    """给分析结果补上 speech 段（口头禅 / 重说）

    转写很慢，所以只在用户勾了才跑；失败不阻断出片，只记一笔说明。
    结果不写回分析缓存——缓存是「画面分析」，语音是另一条链路。
    segs 传入时直接用（一次转写两处复用：语音级 + 字幕），不再调 transcribe。
    """
    if not kinds:
        return analysis
    out = dict(analysis)
    if not analysis.get("has_audio", True):
        out["speech"] = {"filler": [], "retake": [],
                         "note": "这个视频没有音轨，语音级剪辑跳过", "word_level": False}
        return out

    try:
        from backend import speech_cut, subtitle

        def cb(pct, msg=""):
            if progress_cb:
                progress_cb(pct, msg)

        if segs is None:
            segs = subtitle.transcribe(
                path, word_level="filler" in kinds,
                progress_cb=cb, cancel_flag=(lambda: is_cancelled(jid)) if jid else None)
        out["speech"] = speech_cut.analyze(
            segs, strength, kinds, duration=float(analysis.get("duration") or 0))
        # 词边界留给 decide：句界只留 0.2s、句中卡壳留 0.6s 都靠它判断
        if out["speech"].get("word_level"):
            out["speech"]["words"] = speech_cut.flatten_words(segs)
    except Exception as e:
        out["speech"] = {"filler": [], "retake": [],
                         "note": f"转写没成功（{e}），这次没剪口头禅",
                         "word_level": False, "error": str(e)}
    return out


def _speech_and_cues(analysis, path, kinds, strength, jid, progress_cb, need_cues,
                     max_chars=16):
    """一次词级转写，同时喂语音级剪辑和字幕

    返回 (analysis, cues, note)：
      analysis  补好 speech 段的分析结果
      cues      原片时间的字幕条目（失败就是 []）
      note      降级原因，没有降级就是空串

    词级转写是句级结果的超集（每条多带 "words"），所以只转一遍：
    口头禅要词级，字幕也能直接拿它断句，不必把同一段音频听两遍。
    """
    kinds = tuple(kinds or ())
    if not kinds and not need_cues:
        return analysis, [], ""

    if not analysis.get("has_audio", True):
        # 没音轨：转写无从下手，字幕也就没有文字
        out = analysis
        if kinds:
            out = dict(analysis)
            out["speech"] = {"filler": [], "retake": [],
                             "note": "这个视频没有音轨，语音级剪辑跳过",
                             "word_level": False}
        return out, [], "这个视频没有音轨，听不了词，字幕和口头禅都跳过了"

    from backend import speech_cut, subtitle

    need_words = ("filler" in kinds) or bool(need_cues)

    def cb(pct, msg=""):
        if progress_cb:
            progress_cb(pct, msg)

    segs, err = None, ""
    try:
        segs = subtitle.transcribe(
            path, word_level=need_words, progress_cb=cb,
            cancel_flag=(lambda: is_cancelled(jid)) if jid else None)
    except Exception as e:
        # 取消也走这里：调用方紧接着会查 is_cancelled，不必在这儿抛
        segs, err = None, str(e)[:120]

    cues = []
    if segs:
        try:
            # with_words：能对上就挂词时间（逐字点亮要用），对不上自动降级
            cues = subtitle.cues_from_transcription(segs, max_chars=max_chars,
                                                    with_words=True)
        except Exception:
            cues = []

    if kinds:
        if segs is None:
            out = dict(analysis)
            out["speech"] = {"filler": [], "retake": [],
                             "note": f"转写没成功（{err}），这次没剪口头禅",
                             "word_level": False, "error": err}
        else:
            out = _enrich_speech(analysis, path, kinds, strength, jid, progress_cb, segs=segs)
    else:
        out = analysis
        if segs:
            # 只开了字幕、没勾口头禅：词表也顺手留下，静音剪点就能按句界/卡壳区分
            out = dict(analysis)
            wl = speech_cut.has_word_level(segs)
            speech = {"filler": [], "retake": [], "word_level": wl, "note": ""}
            if wl:
                speech["words"] = speech_cut.flatten_words(segs)
            out["speech"] = speech

    note = ""
    if err:
        note = (f"字幕没转成（{err}），这次只出了画面" if need_cues
                else f"转写没成功（{err}），口头禅这次没剪")
    elif need_cues and not cues:
        note = "这段没听出人声，字幕没用上（这次只出了画面）"
    return out, cues, note


def _transition_cfg(spec):
    """面板转场设置 → 渲染器参数

    on=False 且没设首尾淡入淡出 → None（纯硬切）
    type="auto" → 渲染时按接缝自己判断，duration 只是期望值
    type="none" → 接缝全硬切，只留首尾淡入淡出（关掉转场但想要片头淡入就是这种）
    """
    spec = spec or {}

    def sec(v):
        try:
            return round(max(0.0, min(2.0, float(v))), 2)
        except (TypeError, ValueError):
            return None

    fin, fout = sec(spec.get("fade_in")), sec(spec.get("fade_out"))

    if not spec.get("on"):
        if not (fin or fout):
            return None
        return {"type": "none", "duration": 0.0,
                "fade_in": fin or 0.0, "fade_out": fout or 0.0}

    kind = str(spec.get("type") or "auto").strip().lower()
    if kind in ("", "off"):
        kind = "auto"
    if kind == "none":
        return {"type": "none", "duration": 0.0,
                "fade_in": fin if fin is not None else 0.0,
                "fade_out": fout if fout is not None else 0.0}

    try:
        dur = round(float(spec.get("duration") or 0.5), 2)
    except (TypeError, ValueError):
        dur = 0.5
    out = {"type": kind, "duration": max(0.1, min(3.0, dur))}
    # 没传就让渲染器按类型给默认值（auto 才默认淡入淡出）
    if fin is not None:
        out["fade_in"] = fin
    if fout is not None:
        out["fade_out"] = fout
    return out


def _frame_cfg(spec):
    """画幅配置 → None | {"mode": "blur"|"crop"}

    前端可能发字符串 "origin"/"blur"/"crop"，也可能发 {"mode": ...}；
    老前端不发这个字段 → None，渲染结果与今天逐字一致。
    """
    if not spec:
        return None
    if isinstance(spec, str):
        mode = spec.strip().lower()
    elif isinstance(spec, dict):
        mode = str(spec.get("mode") or "").strip().lower()
    else:
        return None
    if mode in ("", "origin", "none", "off", "source"):
        return None
    if mode not in ("blur", "crop"):
        return None
    return {"mode": mode}


def _render_options(effects, cues_in):
    """效果配置 → 渲染参数。返回 (opts, problems)

    problems: [(field, message)]，field ∈ {"subtitle","bgm"}；
              /api/render 按原样报 400，/api/autocut 降级并在 note 里说明。
    opts: {"speed":float, "transition":dict|None, "bgm":dict|None,
           "subtitle":dict|None, "normalize":bool,
           "frame":dict|None, "denoise":bool}
    """
    effects = effects or {}
    sub_cfg = effects.get("subtitle") or {}
    bgm_cfg = effects.get("bgm") or {}
    tr_cfg = effects.get("transition") or {}

    problems = []

    try:
        speed = float(effects.get("speed") or 1.0)
    except (TypeError, ValueError):
        speed = 1.0
    speed = round(max(0.1, min(10.0, speed)), 3)

    subtitle = None
    if sub_cfg.get("on"):
        if cues_in:
            subtitle = {"cues": cues_in, "style": _sub_style(sub_cfg)}
        else:
            problems.append(("subtitle",
                             "字幕开着但还没有文字：先点「自动转写」，或导入一个 SRT"))

    bgm = None
    if bgm_cfg.get("on"):
        bgm_path = bgm_cfg.get("path") or _resolve_bgm(bgm_cfg) or ""
        if bgm_path:
            bgm = {"path": bgm_path,
                   "volume": float(bgm_cfg.get("volume") or 0.24),
                   "keep_original": bgm_cfg.get("keep_original", True)}
        else:
            problems.append(("bgm",
                             "没找到可用的 BGM：把音乐放进 music 文件夹，或点「选文件」挑一首"))

    opts = {
        "speed": speed,
        "transition": _transition_cfg(tr_cfg),
        "bgm": bgm,
        "subtitle": subtitle,
        "normalize": bool(effects.get("normalize")),
        # 竖屏 9:16：缺省 None（原画幅），老工程 JSON 不传就完全不受影响
        "frame": _frame_cfg(effects.get("frame")),
        # 轻度降噪：只压人声原音轨，缺省 False
        "denoise": bool(effects.get("denoise")),
    }
    return opts, problems


@app.post("/api/render")
def api_render():
    from backend.renderer import build_keep_ranges, render_cut

    data = request.get_json(force=True, silent=True) or {}
    path = data.get("path", "")
    cut_ranges = data.get("cut_ranges") or []
    duration = float(data.get("duration") or 0)
    audio_mode = data.get("audio_mode") or "clip"
    effects = data.get("effects") or {}
    cues_in = data.get("cues") or []

    if not _is_video(path):
        return jsonify(ok=False, error="文件不存在"), 400
    if duration <= 0:
        return jsonify(ok=False, error="缺少视频时长"), 400
    if not cut_ranges:
        return jsonify(ok=False, error="没有需要剪掉的内容"), 400

    opts, problems = _render_options(effects, cues_in)
    # 报错顺序要和以前一样：字幕的毛病先报，再查 keep，最后报 BGM
    for field, msg in problems:
        if field == "subtitle":
            return jsonify(ok=False, error=msg), 400

    keep = build_keep_ranges(duration, [(float(s), float(e)) for s, e in cut_ranges])
    if not keep:
        return jsonify(ok=False, error="全部内容都会被剪掉，请调整设置"), 400

    for field, msg in problems:
        if field == "bgm":
            return jsonify(ok=False, error=msg), 400

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    stem = Path(path).stem
    out = OUTPUT_DIR / f"{stem}_剪辑_{time.strftime('%m%d_%H%M%S')}.mp4"

    jid = new_job("render")

    def cb(pct, msg=""):
        update_job(jid, progress=round(pct, 1), message=msg or "渲染中…")

    def work():
        try:
            render_notes = []
            render_cut(
                str(path), str(out), keep, progress_cb=cb,
                cancel_flag=lambda: is_cancelled(jid), audio_mode=audio_mode,
                speed=opts["speed"],
                transition=opts["transition"],
                bgm=opts["bgm"],
                subtitle=opts["subtitle"],
                frame=opts["frame"],
                denoise=opts["denoise"],
                notes=render_notes,
            )
            if is_cancelled(jid):
                update_job(jid, status="cancelled", message="已取消")
                return

            # 音量归一：只重编音频，失败也不影响成片可用
            norm_err = ""
            if opts["normalize"]:
                update_job(jid, progress=97, message="统一音量…")
                try:
                    from backend import audio_fx

                    audio_fx.set_volume(str(out), str(out), normalize=True)
                except Exception as e:
                    norm_err = f"统一音量失败（{str(e)[:60]}），成片未归一"

            result = {
                "output": str(out),
                "name": out.name,
                "size_mb": round(out.stat().st_size / 1048576, 2),
                "kept": round(sum(e - s for s, e in keep), 2),
                "removed": round(duration - sum(e - s for s, e in keep), 2),
            }
            if norm_err:
                result["warning"] = norm_err
            # 降噪 / 竖屏降级的说明（不阻断出片，但要让人看见）
            if render_notes:
                result["note"] = "；".join(render_notes)
            update_job(jid, status="done", progress=100, message="成片已生成", result=result)
        except Exception as e:
            from backend.renderer import RenderCancelled

            if isinstance(e, RenderCancelled) or is_cancelled(jid):
                update_job(jid, status="cancelled", message="已取消")
            else:
                import traceback

                update_job(jid, status="error", error=str(e), message="渲染失败",
                           result={"traceback": traceback.format_exc()})

    threading.Thread(target=work, daemon=True).start()
    return jsonify(ok=True, job=jid, preview_kept=round(sum(e - s for s, e in keep), 2))


@app.post("/api/autocut")
def api_autocut():
    """AI 一站式粗剪：分析 → 听写 → 决策 → 渲染，一次跑完

    口播一条龙：剪停顿/口头禅 + 上字幕 + 硬切 + 统一音量，都在这条链路里。
    """
    from backend.ai_analyzer import AIAnalyzer
    from backend.autocut import decide
    from backend.renderer import RenderCancelled, render_cut

    data = request.get_json(force=True, silent=True) or {}
    path = data.get("path", "")
    strength = data.get("strength") or "standard"
    effects = data.get("effects") or {}
    cues_in = data.get("cues") or []
    if not _is_video(path):
        return jsonify(ok=False, error="文件不存在"), 400
    if not isinstance(effects, dict):
        effects = {}

    sub_on = bool((effects.get("subtitle") or {}).get("on"))
    # 断句宽度和 /api/subtitle/transcribe 对齐，否则同一段视频两处出的字幕不一样长
    try:
        max_chars = int(data.get("max_chars") or 18)
    except (TypeError, ValueError):
        max_chars = 18

    _clean_old_jobs()
    jid = new_job("autocut")

    # 进度分段：分析 0–42，听写 42–56，决策 57，渲染 60–100
    def acb(pct, msg=""):
        update_job(jid, progress=round(max(0.0, min(100.0, float(pct))) * 0.42, 1),
                   message=msg or "分析中…")

    def lcb(pct, msg=""):
        update_job(jid, progress=round(42 + max(0.0, min(100.0, float(pct))) * 0.14, 1),
                   message=msg or "听写中…")

    def rcb(pct, msg=""):
        update_job(jid, progress=round(60 + max(0.0, min(100.0, float(pct))) * 0.40, 1),
                   message=msg or "渲染中…")

    speech_kinds = _speech_kinds(data.get("speech"))

    def work():
        try:
            key = _cache_key(path)
            analysis = _cache_get(key)

            if analysis:
                update_job(jid, progress=56, message="复用上次分析，直接决策…")
            else:
                analyzer = AIAnalyzer(path)
                # 0.5s 粒度：紧凑档（0.55）才有东西可判；剪多狠交给 decide 的档位
                analyzer.analyze(progress_cb=acb, cancel_flag=lambda: is_cancelled(jid),
                                 min_silence=0.5)
                if is_cancelled(jid):
                    update_job(jid, status="cancelled", message="已取消")
                    return

                analysis = {
                    "scenes": analyzer.scenes,
                    "silent": analyzer.silent_segments,
                    "redundant": analyzer.redundant_segments,
                    "static": analyzer.static_segments,
                    "blank": analyzer.blank_segments,
                    "duration": round(analyzer.duration, 2),
                    "fps": round(analyzer.fps, 2),
                    "has_audio": analyzer.has_audio,
                }
                _cache_put(key, analysis)

            # 字幕 / 口头禅共用一次词级转写：前端给了 cues 就直接用，不重复听写
            cues = [c for c in (cues_in or []) if isinstance(c, dict)]
            need_cues = sub_on and not cues
            analysis, asr_cues, note = _speech_and_cues(
                analysis, path, speech_kinds, strength, jid, lcb, need_cues,
                max_chars=max_chars)
            # 用户给的（导入的 SRT / 手改过的）永远优先，ASR 只补空缺：
            # 勾了口头禅会强制转写，不能因此把用户那份字幕顶掉
            if asr_cues and not cues:
                cues = asr_cues
            if is_cancelled(jid):
                update_job(jid, status="cancelled", message="已取消")
                return

            # 效果配置 → 渲染参数。一键粗剪不因为效果没配好就失败：
            # 能降级的降级（不出字幕 / 不用 BGM），原因写进 note 带回前端。
            opts, problems = _render_options(effects, cues)
            notes = [note] if note else []
            for field, msg in problems:
                if field == "bgm":
                    notes.append("BGM 没找到，这次没用")
                elif field == "subtitle" and not any("字幕" in n for n in notes):
                    notes.append("字幕没做出来，这次只出了画面")

            update_job(jid, progress=57, message="AI 正在决定剪哪里…")
            plan = decide(analysis, strength)
            if not plan["keep_ranges"]:
                update_job(jid, status="error", message="剪不出来",
                           error=plan.get("warning") or "全部内容都会被剪掉，请换更保守的强度")
                return

            OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
            stem = Path(path).stem
            out = OUTPUT_DIR / f"{stem}_粗剪_{time.strftime('%m%d_%H%M%S')}.mp4"

            fx_norm = bool(opts.get("normalize"))
            fx_sub = bool(opts.get("subtitle"))
            fx_bgm = bool(opts.get("bgm"))
            fx_frame = opts.get("frame") or None
            fx_denoise = bool(opts.get("denoise"))
            render_notes = []          # 渲染阶段的降级原因（降噪 / 竖屏）

            if not plan["cut_ranges"]:
                update_job(jid, progress=80, message="AI 判断不需要剪辑，直接输出…")
                duration = float(analysis.get("duration") or 0)
                wants_fx = (fx_sub or fx_norm or fx_bgm or fx_frame or fx_denoise
                            or abs(opts["speed"] - 1.0) > 1e-6
                            or bool(opts.get("transition")))
                if wants_fx and duration > 0.05:
                    # 时间上没剪点，但字幕/归一/BGM/竖屏还得落到画面上：整条走一遍渲染
                    render_cut(str(path), str(out), [(0.0, duration)],
                               progress_cb=rcb, cancel_flag=lambda: is_cancelled(jid),
                               audio_mode="clip", speed=opts["speed"],
                               transition=opts["transition"], bgm=opts["bgm"],
                               subtitle=opts["subtitle"], frame=fx_frame,
                               denoise=fx_denoise, notes=render_notes)
                else:
                    # 没效果可加（或拿不到时长）：原片直接复制，最快
                    shutil.copy2(path, out)
            else:
                render_cut(str(path), str(out), plan["keep_ranges"],
                           progress_cb=rcb, cancel_flag=lambda: is_cancelled(jid),
                           audio_mode="clip", speed=opts["speed"],
                           transition=opts["transition"], bgm=opts["bgm"],
                           subtitle=opts["subtitle"], frame=fx_frame,
                           denoise=fx_denoise, notes=render_notes)
            # 渲染里降级的地方（比如降噪失败）也要让人看见
            notes.extend(render_notes)

            if is_cancelled(jid):
                update_job(jid, status="cancelled", message="已取消")
                return

            # 音量归一：只重编音频，失败不阻断出片
            if fx_norm:
                update_job(jid, progress=96, message="统一音量…")
                try:
                    from backend import audio_fx

                    audio_fx.set_volume(str(out), str(out), normalize=True)
                except Exception as e:
                    fx_norm = False
                    notes.append(f"统一音量失败（{str(e)[:60]}），成片未归一")

            subtitle_cues = (opts.get("subtitle") or {}).get("cues") or []
            from backend import subtitle as submod
            want_highlight = bool((opts.get("subtitle") or {})
                                  .get("style", {}).get("highlight"))
            highlight_ok = bool(fx_sub and want_highlight
                                and submod.has_word_times(subtitle_cues))

            update_job(jid, status="done", progress=100, message="粗剪完成", result={
                "output": str(out),
                "name": out.name,
                "size_mb": round(out.stat().st_size / 1048576, 2),
                "kept": plan["after"],
                "removed": plan["saved"],
                "no_cut": not plan["cut_ranges"],
                "analysis": _public_analysis(analysis),
                "plan": plan,
                # 原片时间的字幕：前端直接复用，下次出片不用再听一遍
                "cues": cues,
                "cues_count": len(cues),
                "fx_applied": {
                    "subtitle": len(subtitle_cues) if fx_sub else 0,
                    "bgm": fx_bgm,
                    "speed": opts["speed"],
                    "transition": bool(opts.get("transition")),
                    "normalize": fx_norm,
                    "frame": (fx_frame or {}).get("mode") or "",
                    "denoise": fx_denoise,
                    # 逐字点亮能不能用（这次的字幕到底有没有词时间）
                    "word_highlight": highlight_ok,
                },
                "note": "；".join(notes),
            })
        except AIAnalyzer.Cancelled:
            update_job(jid, status="cancelled", message="已取消")
        except RenderCancelled:
            update_job(jid, status="cancelled", message="已取消")
        except Exception as e:
            import traceback

            if is_cancelled(jid):
                update_job(jid, status="cancelled", message="已取消")
            else:
                update_job(jid, status="error", error=str(e), message="粗剪失败",
                           result={"traceback": traceback.format_exc()})

    threading.Thread(target=work, daemon=True).start()
    return jsonify(ok=True, job=jid)


def _merge_effects(intent, effects):
    """把「效果」面板的设置合进意图：文字里说过的字段优先，没说过的用面板"""
    if not isinstance(effects, dict):
        return intent

    for key in ("subtitle", "bgm", "transition"):
        if intent.get(key) is None and isinstance(effects.get(key), dict):
            intent[key] = effects[key]

    if intent.get("speed") is None:
        try:
            sp = float(effects.get("speed") or 1.0)
        except (TypeError, ValueError):
            sp = 1.0
        if abs(sp - 1.0) > 1e-6:
            intent["speed"] = round(max(0.1, min(10.0, sp)), 3)

    if not intent.get("speed_zones") and isinstance(effects.get("speed_zones"), list):
        intent["speed_zones"] = effects["speed_zones"]

    # 面板上明确关掉的，别被文字里的默认值带回来
    tr = intent.get("transition")
    if isinstance(tr, dict) and tr.get("on") is False:
        intent["transition"] = {"on": False, "type": None}
    return intent


def _resolve_bgm(spec):
    """需求里说的音乐 → 实际文件路径（先查音乐库，再看是不是直接给了路径）"""
    from backend import audio_fx

    q = str(spec.get("query") or "").strip()
    files = audio_fx.list_music()

    if q:
        p = Path(q)
        if p.is_file() and audio_fx.is_audio(p):
            return str(p)
        low = q.lower()
        for f in files:
            if low in f["name"].lower():
                return f["path"]
        stem = Path(q).stem.lower()
        if stem:
            for f in files:
                if stem in Path(f["name"]).stem.lower() or \
                        Path(f["name"]).stem.lower() in stem:
                    return f["path"]
        return None

    return files[0]["path"] if files else None


def _sub_style(spec):
    """字幕意图 → ASS 样式参数

    新键（描边色 / 底框不透明度 / 逐字点亮）缺省就不塞进 style 字典，
    保证老请求走出来的 ASS 和以前一模一样。
    """
    spec = spec or {}
    ratio = {"small": 0.045, "large": 0.068}.get(spec.get("size"), 0.055)
    st = {"size_ratio": ratio}
    for key in ("position", "color"):
        if spec.get(key):
            st[key] = spec[key]
    if spec.get("box"):
        st["box"] = True
    if spec.get("outline"):
        st["outline"] = spec["outline"]
    if spec.get("outline_color"):
        st["outline_color"] = str(spec["outline_color"])
    if spec.get("box_alpha") is not None:
        try:
            st["box_alpha"] = max(0.0, min(100.0, float(spec["box_alpha"])))
        except (TypeError, ValueError):
            pass
    if spec.get("highlight"):
        st["highlight"] = True
    if spec.get("highlight_color"):
        st["highlight_color"] = str(spec["highlight_color"])
    return st


@app.post("/api/direct")
def api_direct():
    """按一句话需求剪片：理解需求 → 分析画面 → 排方案 →（可选）渲染"""
    from backend import director
    from backend.ai_analyzer import AIAnalyzer
    from backend.renderer import RenderCancelled, render_cut

    data = request.get_json(force=True, silent=True) or {}
    path = data.get("path", "")
    text = (data.get("text") or "").strip()
    want_render = bool(data.get("render", True))
    effects = data.get("effects") or {}
    cues_in = data.get("cues") or []
    if not isinstance(effects, dict):
        effects = {}

    if not _is_video(path):
        return jsonify(ok=False, error="文件不存在"), 400
    if not text:
        return jsonify(ok=False, error="先说一句你想怎么剪"), 400

    _clean_old_jobs()
    jid = new_job("direct")

    # 要加字幕就得先转写，进度条得给转写留一段
    prog = {"an_end": 52, "render_base": 58, "render_span": 40}

    def acb(pct, msg=""):
        update_job(jid, progress=round(3 + pct * (prog["an_end"] - 3) / 100, 1),
                   message=msg or "分析画面…")

    def pcb(pct, msg=""):
        update_job(jid, progress=prog["an_end"] + 2, message=msg or "排剪辑方案…")

    def rcb(pct, msg=""):
        update_job(jid, progress=round(prog["render_base"] + pct * prog["render_span"] / 100, 1),
                   message=msg or "渲染中…")

    def scb(pct, msg=""):
        update_job(jid, progress=round(44 + pct * 0.30, 1), message=msg or "听写字幕…")

    def work():
        try:
            # 1) 理解需求：这一步只看文字，不碰视频，几毫秒就出
            update_job(jid, progress=1, message="理解你的需求…")
            intent, source = director.parse_demand(text)
            # 面板设置只在「这句话里没提」的时候才生效，说了话就以话为准
            _merge_effects(intent, effects)
            brief = director.describe(intent).split("\n")[0]
            update_job(jid, progress=3, message=brief[:46])

            want_sub = bool((intent.get("subtitle") or {}).get("on"))
            if want_sub:
                prog.update(an_end=40, render_base=76, render_span=22)

            # 2) 分析画面（有缓存就直接复用）
            key = _cache_key(path)
            analysis = _cache_get(key)
            if analysis:
                update_job(jid, progress=55, message="复用上次的画面分析…")
            else:
                analyzer = AIAnalyzer(path)
                # 同上：粗剪这条路用 0.5s 粒度，档位差异交给 decide
                analyzer.analyze(progress_cb=acb, cancel_flag=lambda: is_cancelled(jid),
                                 min_silence=0.5)
                if is_cancelled(jid):
                    update_job(jid, status="cancelled", message="已取消")
                    return
                analysis = {
                    "scenes": analyzer.scenes,
                    "silent": analyzer.silent_segments,
                    "redundant": analyzer.redundant_segments,
                    "static": analyzer.static_segments,
                    "blank": analyzer.blank_segments,
                    "duration": round(analyzer.duration, 2),
                    "fps": round(analyzer.fps, 2),
                    "has_audio": analyzer.has_audio,
                }
                _cache_put(key, analysis)

            # 3) 排方案（目标时长 / 类型开关 / 卡点排镜都在这里）
            update_job(jid, progress=prog["an_end"] + 2, message="按需求排剪辑方案…")
            plan = director.build_plan(analysis, intent, video_path=path,
                                       duration=analysis.get("duration"), progress_cb=pcb)
            if is_cancelled(jid):
                update_job(jid, status="cancelled", message="已取消")
                return

            plan["style"] = plan.get("style") or intent.get("style") or "auto"

            # 4) 字幕：语音转写 + 断句（这一步最慢，失败就降级不烧字幕）
            cues, sub_err = None, ""
            if want_sub and plan.get("keep_ranges"):
                from backend import subtitle as submod
                if cues_in:
                    cues = cues_in
                    update_job(jid, progress=74, message=f"用已有的 {len(cues)} 条字幕")
                else:
                    try:
                        cues = submod.speech_to_cues(
                            path, progress_cb=scb,
                            cancel_flag=lambda: is_cancelled(jid), with_words=True)
                    except submod.SubtitleCancelled:
                        update_job(jid, status="cancelled", message="已取消")
                        return
                    except Exception as e:
                        sub_err = str(e)[:120]
                        cues = None
                        update_job(jid, progress=74, message="字幕没做出来，先不带字幕出片")

            # 5) BGM：把需求里说的音乐解析成实际文件
            bgm_cfg = plan.get("bgm") or {}
            bgm_path, bgm_err = "", ""
            if bgm_cfg.get("on"):
                bgm_path = _resolve_bgm(bgm_cfg) or ""
                if not bgm_path:
                    q = bgm_cfg.get("query")
                    bgm_err = (f"音乐库里没找到「{q}」，这次没用 BGM" if q
                               else "音乐库是空的，这次没用 BGM——把喜欢的音乐放进 music 文件夹再来")

            reply = director.describe(intent, plan)
            if sub_err:
                reply += f"\n字幕没做出来：{sub_err}"
            if bgm_err:
                reply += f"\n{bgm_err}。"

            intent_out = dict(intent)
            intent_out["allow"] = sorted(intent.get("allow") or [])

            base = {
                "analysis": _public_analysis(analysis),
                "plan": plan,
                "intent": intent_out,
                "reply": reply,
                "source": source,
                "cues": cues or [],
            }

            if not plan.get("keep_ranges"):
                update_job(jid, status="error", message="剪不出来",
                           error=plan.get("warning") or "全部内容都会被剪掉，需求太狠了",
                           result=base)
                return

            if not want_render:
                update_job(jid, status="done", progress=100, message="方案已排好",
                           result=dict(base, no_render=True,
                                       kept=plan["after"], removed=plan["saved"]))
                return

            OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
            stem = Path(path).stem
            out = OUTPUT_DIR / f"{stem}_按需求_{time.strftime('%m%d_%H%M%S')}.mp4"

            speed = float(plan.get("speed") or 1.0)
            dir_frame = _frame_cfg(effects.get("frame"))
            dir_denoise = bool(effects.get("denoise"))
            has_fx = bool(cues) or bool(bgm_path) or bool(plan.get("transition")) \
                or bool(dir_frame) or dir_denoise \
                or abs(speed - 1.0) > 1e-6

            if not plan.get("cut_ranges") and not has_fx:
                update_job(jid, progress=80, message="没什么可剪的，直接输出原片…")
                shutil.copy2(path, out)
            else:
                dir_notes = []
                render_cut(
                    str(path), str(out), plan["keep_ranges"],
                    progress_cb=rcb, cancel_flag=lambda: is_cancelled(jid),
                    audio_mode=plan.get("audio_mode") or "clip",
                    speed=speed,
                    transition=plan.get("transition"),
                    bgm=({"path": bgm_path,
                          "volume": bgm_cfg.get("volume", 0.24),
                          "keep_original": bgm_cfg.get("keep_original", True),
                          "duck": bool(bgm_cfg.get("duck")),
                          } if bgm_path else None),
                    subtitle=({"cues": cues, "style": _sub_style(plan.get("subtitle") or {})}
                              if cues else None),
                    frame=dir_frame,
                    denoise=dir_denoise,
                    notes=dir_notes,
                )
                if dir_notes:
                    reply += "\n" + "；".join(dir_notes)
                    base["reply"] = reply

            if is_cancelled(jid):
                update_job(jid, status="cancelled", message="已取消")
                return

            update_job(jid, status="done", progress=100, message="按需求剪完了", result=dict(
                base,
                output=str(out),
                name=out.name,
                size_mb=round(out.stat().st_size / 1048576, 2),
                kept=plan["after"],
                removed=plan["saved"],
            ))
        except AIAnalyzer.Cancelled:
            update_job(jid, status="cancelled", message="已取消")
        except RenderCancelled:
            update_job(jid, status="cancelled", message="已取消")
        except Exception as e:
            import traceback

            if is_cancelled(jid):
                update_job(jid, status="cancelled", message="已取消")
            else:
                update_job(jid, status="error", error=str(e), message="按需求剪失败",
                           result={"traceback": traceback.format_exc()})

    threading.Thread(target=work, daemon=True).start()
    return jsonify(ok=True, job=jid)


@app.get("/api/settings")
def api_settings_get():
    from backend import director

    return jsonify(ok=True, llm=director.mask_settings())


@app.post("/api/settings")
def api_settings_post():
    from backend import director

    data = request.get_json(force=True, silent=True) or {}
    try:
        if isinstance(data.get("llm"), dict):
            director.save_settings({"llm": data["llm"]})
        return jsonify(ok=True, llm=director.mask_settings())
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 400


@app.post("/api/llm-test")
def api_llm_test():
    """用一句示例需求试一下大模型通不通"""
    from backend import director

    if not director.llm_ready():
        return jsonify(ok=False, error="还没填接口地址和 API Key")
    try:
        intent, source = director.parse_demand("紧凑一点，控制在 60 秒，别剪静音")
        if source != "llm":
            note = "；".join(intent.get("notes") or []) or "请检查配置"
            return jsonify(ok=False, error="没能用上大模型：" + note)
        return jsonify(ok=True, style=intent.get("style"),
                       target=intent.get("target"), source=source)
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 400


@app.post("/api/export-kit")
def api_export_kit():
    """把成片 + 时间线 + 分段素材打包，拿到剪映 / PR 里精调"""
    from backend.exporter import export_kit

    data = request.get_json(force=True, silent=True) or {}
    path = data.get("path", "")
    output = data.get("output", "")
    analysis = data.get("analysis") or {}
    plan = data.get("plan") or {}
    want_clips = bool(data.get("want_clips", True))
    cues = data.get("cues") or []
    try:
        speed = float(data.get("speed") or 1.0)
    except (TypeError, ValueError):
        speed = 1.0
    # transition 可以是统一秒数，也可以是「每个接缝一个重叠秒数」的数组
    transition = data.get("transition") or 0.0

    if not _is_video(path):
        return jsonify(ok=False, error="源视频不存在"), 400
    if not output or not Path(output).exists():
        return jsonify(ok=False, error="成片还没生成"), 400
    if not plan.get("keep_ranges"):
        return jsonify(ok=False, error="缺少剪辑数据"), 400

    out_mp4 = Path(output)
    kit_dir = out_mp4.parent / f"{out_mp4.stem}_精调包"

    jid = new_job("kit")

    def cb(pct, msg=""):
        update_job(jid, progress=round(pct, 1), message=msg or "整理中…")

    def work():
        try:
            kit_dir.mkdir(parents=True, exist_ok=True)
            cb(3, "复制成片…")
            mp4 = kit_dir / out_mp4.name
            if not mp4.exists() or mp4.stat().st_size != out_mp4.stat().st_size:
                shutil.copy2(out_mp4, mp4)

            files = export_kit(path, analysis, plan, kit_dir, want_clips=want_clips,
                               progress_cb=cb, cues=cues, speed=speed,
                               transition=transition)
            files["mp4"] = str(mp4)
            files["dir"] = str(kit_dir)
            update_job(jid, status="done", progress=100, message="精调包已生成", result=files)
        except Exception as e:
            import traceback

            update_job(jid, status="error", error=str(e), message="打包失败",
                       result={"traceback": traceback.format_exc()})

    threading.Thread(target=work, daemon=True).start()
    return jsonify(ok=True, job=jid, dir=str(kit_dir))


@app.get("/api/job/<jid>")
def api_job(jid):
    job = job_state(jid)
    if not job:
        return jsonify(ok=False, error="任务不存在"), 404
    return jsonify(ok=True, job=job)


@app.post("/api/job/<jid>/cancel")
def api_cancel(jid):
    update_job(jid, cancelled=True, message="正在取消…")
    return jsonify(ok=True)


@app.post("/api/open")
def api_open():
    data = request.get_json(force=True, silent=True) or {}
    target = data.get("path", "")
    mode = data.get("mode", "folder")
    try:
        p = Path(target)
        if mode == "file":
            os.startfile(str(p))
        else:
            os.startfile(str(p if p.is_dir() else p.parent))
        return jsonify(ok=True)
    except Exception as e:
        return jsonify(ok=False, error=str(e))


@app.post("/api/save-json")
def api_save_json():
    data = request.get_json(force=True, silent=True) or {}
    video = data.get("path", "")
    payload = data.get("data")
    if not payload:
        return jsonify(ok=False, error="没有可保存的数据"), 400
    OUT = PROJECT_ROOT / "projects"
    OUT.mkdir(exist_ok=True)
    stem = Path(video).stem or "分析"
    out = OUT / f"{stem}_建议.json"
    try:
        with open(out, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        return jsonify(ok=True, path=str(out))
    except Exception as e:
        return jsonify(ok=False, error=str(e))


# ---------------- 字幕 ----------------

@app.get("/api/subtitle/status")
def api_subtitle_status():
    from backend import subtitle

    return jsonify(ok=True, **subtitle.model_status())


@app.post("/api/subtitle/transcribe")
def api_subtitle_transcribe():
    """单独转写一条视频（不出片），给「效果」面板预览用"""
    from backend import subtitle

    data = request.get_json(force=True, silent=True) or {}
    path = data.get("path", "")
    model = data.get("model") or subtitle.DEFAULT_MODEL
    max_chars = int(data.get("max_chars") or 18)

    if not _is_video(path):
        return jsonify(ok=False, error="文件不存在"), 400

    _clean_old_jobs()
    jid = new_job("subtitle")

    def work():
        try:
            def cb(pct, msg=""):
                update_job(jid, progress=round(pct * 0.95, 1), message=msg or "听写中…")

            cues = subtitle.speech_to_cues(
                path, model_size=model, max_chars=max_chars, progress_cb=cb,
                cancel_flag=lambda: is_cancelled(jid), with_words=True)
            if is_cancelled(jid):
                update_job(jid, status="cancelled", message="已取消")
                return
            words_ok = subtitle.has_word_times(cues)
            update_job(jid, status="done", progress=100, message=f"转写完，{len(cues)} 条",
                       result={"cues": cues, "count": len(cues), "model": model,
                               # 能不能逐字点亮，前端要能看出来（SRT 导入没有词时间）
                               "word_level": words_ok})
        except subtitle.SubtitleCancelled:
            update_job(jid, status="cancelled", message="已取消")
        except Exception as e:
            update_job(jid, status="error", error=str(e), message="转写失败",
                       result={"traceback": __import__("traceback").format_exc()})

    threading.Thread(target=work, daemon=True).start()
    return jsonify(ok=True, job=jid)


@app.post("/api/subtitle/save")
def api_subtitle_save():
    """把字幕存成 SRT / ASS，方便拿去剪映或检查"""
    from backend import subtitle

    data = request.get_json(force=True, silent=True) or {}
    cues = data.get("cues") or []
    if not cues:
        return jsonify(ok=False, error="没有字幕内容"), 400
    fmt = str(data.get("format") or "srt").lower()
    video = data.get("path", "")
    stem = Path(video).stem if video else "字幕"
    out_dir = PROJECT_ROOT / "projects"
    out_dir.mkdir(exist_ok=True)

    try:
        if fmt == "ass":
            out = out_dir / f"{stem}.ass"
            subtitle.write_ass(cues, out, int(data.get("width") or 1920),
                               int(data.get("height") or 1080), data.get("style"))
        else:
            out = out_dir / f"{stem}.srt"
            out.write_text(subtitle.to_srt(cues), encoding="utf-8")
        return jsonify(ok=True, path=str(out))
    except Exception as e:
        return jsonify(ok=False, error=str(e))


@app.post("/api/subtitle/parse")
def api_subtitle_parse():
    """读一个 SRT/VTT 文件，返回字幕条目"""
    from backend import subtitle

    data = request.get_json(force=True, silent=True) or {}
    path = data.get("path", "")
    if not path or not Path(path).is_file():
        return jsonify(ok=False, error="文件不存在"), 400
    try:
        text = Path(path).read_text(encoding="utf-8-sig", errors="ignore")
        cues = subtitle.parse_srt(text)
        return jsonify(ok=True, cues=cues, count=len(cues), name=Path(path).name)
    except Exception as e:
        return jsonify(ok=False, error=str(e))


# ---------------- BGM 音乐库 ----------------

@app.get("/api/bgm/list")
def api_bgm_list():
    from backend import audio_fx

    files = audio_fx.list_music()
    return jsonify(ok=True, files=files, dir=str(audio_fx.music_dir()),
                   presets=audio_fx.BGM_PRESETS)


@app.post("/api/bgm/add")
def api_bgm_add():
    """把外部音频复制进音乐库（单文件，老接口保留）"""
    from backend import audio_fx

    data = request.get_json(force=True, silent=True) or {}
    src = data.get("path", "")
    if not audio_fx.is_audio(src):
        return jsonify(ok=False, error="不是音频文件"), 400
    try:
        added, skipped, errors = audio_fx.import_music([src])
        if not added:
            return jsonify(ok=False, error=(errors or ["导入失败"])[0]), 400
        it = added[0]
        return jsonify(ok=True, path=it["path"], name=it["name"])
    except Exception as e:
        return jsonify(ok=False, error=str(e))


@app.post("/api/bgm/import")
def api_bgm_import():
    """批量导入：把选中的多个音频复制进音乐库"""
    from backend import audio_fx

    data = request.get_json(force=True, silent=True) or {}
    paths = data.get("paths")
    if not isinstance(paths, list):
        paths = [data.get("path")] if data.get("path") else []
    if not paths:
        return jsonify(ok=False, error="没给文件"), 400
    try:
        added, skipped, errors = audio_fx.import_music(paths)
        return jsonify(ok=True, added=added, skipped=skipped, errors=errors,
                       total=len(audio_fx.list_music()))
    except Exception as e:
        return jsonify(ok=False, error=str(e))


@app.post("/api/bgm/upload")
def api_bgm_upload():
    """浏览器拖拽 / 选文件上传音频（multipart，字段名 files）"""
    from backend import audio_fx

    files = request.files.getlist("files")
    if not files:
        return jsonify(ok=False, error="没收到文件"), 400

    added, errors = [], []
    for f in files:
        try:
            dst = audio_fx.save_upload(f, f.filename)
            added.append({"name": dst.name, "path": str(dst)})
        except Exception as e:
            errors.append(f"{f.filename}: {e}")
    return jsonify(ok=True, added=added, errors=errors,
                   total=len(audio_fx.list_music()))


@app.get("/api/bgm/audio")
def api_bgm_audio():
    """试听：只送音乐库里的文件，支持 Range（浏览器才能拖进度条）"""
    from backend import audio_fx

    p = audio_fx.music_path(request.args.get("name", ""))
    if not p:
        return jsonify(ok=False, error="找不到这首歌"), 404
    mime = {
        ".mp3": "audio/mpeg", ".wav": "audio/wav", ".m4a": "audio/mp4",
        ".aac": "audio/aac", ".flac": "audio/flac", ".ogg": "audio/ogg",
        ".opus": "audio/ogg", ".wma": "audio/x-ms-wma", ".aiff": "audio/aiff",
    }.get(p.suffix.lower(), "application/octet-stream")
    resp = send_file(str(p), mimetype=mime, conditional=True)
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.post("/api/bgm/delete")
def api_bgm_delete():
    """从音乐库移走（进 music/.trash，可恢复）"""
    from backend import audio_fx

    data = request.get_json(force=True, silent=True) or {}
    name = data.get("name", "")
    try:
        dst = audio_fx.delete_music(name)
        if not dst:
            return jsonify(ok=False, error="音乐库里没有这首歌"), 404
        return jsonify(ok=True, name=Path(name).name, trash=str(dst),
                       total=len(audio_fx.list_music()))
    except Exception as e:
        return jsonify(ok=False, error=f"移动失败：{e}")


@app.post("/api/bgm/reveal")
def api_bgm_reveal():
    """在资源管理器里打开音乐库文件夹"""
    from backend import audio_fx

    d = audio_fx.music_dir()
    try:
        if os.name == "nt":
            os.startfile(str(d))
        else:
            subprocess.Popen(["xdg-open", str(d)])
        return jsonify(ok=True, dir=str(d))
    except Exception as e:
        return jsonify(ok=False, error=str(e), dir=str(d))


# ---------------- 启动 ----------------
LAST_PING = time.time()


def serve(port=8756, open_browser=True, idle_exit=True):
    global START_TIME, LAST_PING

    # 先把 ffmpeg 准备好，后续 pydub / moviepy 才能正常调用
    try:
        from utils.ffmpeg_setup import ensure_ffmpeg

        ensure_ffmpeg()
    except Exception:
        pass

    from werkzeug.serving import make_server

    server = make_server("127.0.0.1", port, app, threaded=True)
    url = f"http://127.0.0.1:{port}/"
    START_TIME = time.time()
    LAST_PING = time.time()

    if open_browser:
        threading.Thread(target=lambda: (time.sleep(0.8), open_app_window(url)), daemon=True).start()

    if idle_exit:
        def watchdog():
            # 启动后 20 秒内不判；之后连续 12 秒没心跳就退出（窗口被关掉）
            while True:
                time.sleep(2)
                if time.time() - LAST_PING > 12 and time.time() - START_TIME > 20:
                    server.shutdown()
                    break

        threading.Thread(target=watchdog, daemon=True).start()

    _log(f"[AI 剪辑工具] 服务已启动：{url}")
    server.serve_forever()

START_TIME = time.time()


def open_app_window(url):
    """用 Edge/Chrome 的应用模式打开，看起来就是一个独立 App 窗口"""
    import ctypes

    candidates = [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    ]

    # 按屏幕大小给个合适的初始窗口（留出边距，别顶满屏）
    try:
        u = ctypes.windll.user32
        sw, sh = u.GetSystemMetrics(0), u.GetSystemMetrics(1)
        win_w = int(min(1500, max(1080, sw * 0.86)))
        win_h = int(min(980, max(680, sh * 0.86)))
        x = int(max(20, (sw - win_w) / 2 - 40))
        y = int(max(20, (sh - win_h) / 2 - 30))
    except Exception:
        win_w, win_h, x, y = 1320, 880, 80, 40

    for exe in candidates:
        if Path(exe).exists():
            try:
                subprocess.Popen([
                    exe,
                    f"--app={url}",
                    f"--window-size={win_w},{win_h}",
                    f"--window-position={x},{y}",
                    "--disable-features=Translate",
                ])
                return True
            except Exception:
                continue
    import webbrowser

    webbrowser.open(url)
    return False
