# -*- coding: utf-8 -*-
"""
字幕引擎 —— 语音转写 / SRT 导入 → 断句 → 时间轴映射 → ASS 烧录

三个关键点，踩错任意一个字幕就会错位：
1) 转写拿到的是「原片时间」，成片是剪过的，必须按 keep_ranges 重新映射
2) 区间变速会改变每段长度，映射时要除以速度
3) 转场会让相邻段重叠，累计时间要减掉重叠量

所以 map_cues() 是唯一入口：给它原片字幕 + 最终渲染参数，吐出成片时间轴的字幕。
"""

import json
import os
import re
import subprocess
import threading
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
MODEL_DIR = PROJECT_ROOT / "models" / "whisper"
CACHE_DIR = PROJECT_ROOT / "projects" / "cache"

# 模型档位：中文场景 small 是性价比拐点，再往下错字明显
MODEL_SIZES = {
    "tiny": {"label": "极速", "mb": 75, "note": "最快，中文错字多，只适合试水"},
    "base": {"label": "轻量", "mb": 145, "note": "快，口播尚可，长句容易漏字"},
    "small": {"label": "推荐", "mb": 484, "note": "中文识别准，CPU 上速度可接受"},
    "medium": {"label": "高准", "mb": 1500, "note": "更准，慢 3～4 倍，长视频慎用"},
}

DEFAULT_MODEL = "small"

# 给 whisper 的上下文提示：中文输出更容易带上标点。
# 注意：没有听到人声时模型会把这个提示词原样「回读」成字幕，所以要能识别并丢掉。
_INITIAL_PROMPT = "以下是普通话视频的语音内容，请使用简体中文并加上标点。"

_model_cache = {}
_model_lock = threading.Lock()


class SubtitleCancelled(Exception):
    pass


# ---------------- 模型准备 ----------------

def _model_downloaded(size):
    """huggingface 缓存目录里是否已经有这个模型"""
    for d in MODEL_DIR.glob(f"models--*faster-whisper-{size}"):
        if d.is_dir() and any(d.rglob("model.bin")):
            return True
    return False


def model_status():
    """给界面看的模型状态"""
    out = {}
    for size, meta in MODEL_SIZES.items():
        out[size] = dict(meta, ready=_model_downloaded(size), loaded=size in _model_cache)
    return {"sizes": out, "default": DEFAULT_MODEL, "dir": str(MODEL_DIR)}


def load_model(size=None, progress_cb=None, cancel_flag=None):
    """加载（首次会下载）模型。同一档位只加载一次，常驻内存。"""
    size = size or DEFAULT_MODEL
    if size not in MODEL_SIZES:
        size = DEFAULT_MODEL

    with _model_lock:
        if size in _model_cache:
            return _model_cache[size]

        # 国内直连 huggingface 基本不通，走镜像
        os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
        try:
            from faster_whisper import WhisperModel
        except ImportError as e:
            raise RuntimeError("没装 faster-whisper，跑一下 pip install faster-whisper") from e

        MODEL_DIR.mkdir(parents=True, exist_ok=True)
        fresh = not _model_downloaded(size)
        if fresh and progress_cb:
            progress_cb(2, f"首次使用，正在下载 {size} 模型（约 {MODEL_SIZES[size]['mb']}MB）…")

        if cancel_flag and cancel_flag():
            raise SubtitleCancelled()

        # CPU int8：比 float32 快 2～3 倍，准确率掉得极少
        model = WhisperModel(size, device="cpu", compute_type="int8",
                             download_root=str(MODEL_DIR), cpu_threads=os.cpu_count() or 4)
        _model_cache[size] = model
        if progress_cb:
            progress_cb(10, "模型就绪")
        return model


# ---------------- 转写 ----------------

def _cache_key(video_path, size):
    """缓存键：文件名 + 大小 + 修改时间 + 模型档位（换片或换档位就失效）"""
    p = Path(video_path)
    try:
        st = p.stat()
        return f"{p.name}_{st.st_size}_{int(st.st_mtime)}_{size}"
    except OSError:
        return f"{p.name}_{size}"


def _safe_key(key):
    """键里可能有中文 / 空格 / 冒号，落到文件名上要洗一遍"""
    return re.sub(r"[^0-9A-Za-z_.-]", "_", key)[:120]


def _cache_path(video_path, size):
    """转写缓存文件路径（词级 / 句级共用同一份）

    词级结果是句级结果的超集（每条多带 "words"），所以只存一份就够：
    要词级就直接用，要句级把 words 忽略掉即可，不必把同一段音频听两遍。
    键里不再区分 w/s，只有文件内容和 word_level 字段说明它是哪种。
    """
    safe = _safe_key(_cache_key(video_path, size))
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return CACHE_DIR / f"asr_{safe}.json"


def _legacy_cache_paths(video_path, size):
    """老版本按 w / s 两个 tag 分开存的缓存，只用于读取兼容

    顺序固定为 [_w, _s]，「要不要词级」由调用方决定怎么排。
    """
    safe = _safe_key(_cache_key(video_path, size))
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return [CACHE_DIR / f"asr_{safe}_w.json", CACHE_DIR / f"asr_{safe}_s.json"]


# 回读判定用：去掉标点、空白，只比字
_ECHO_STRIP = re.compile(r"[\s，。、！？；：,.!?;“”\"'（）()]+")


def _is_prompt_echo(text):
    """whisper 在没听到人声时会把 initial_prompt 回读成字幕，这种条目必须丢

    判据只有一条：去掉标点空白后，这段文字是提示词的子串（或反过来包含提示词）。
    真实说话几乎不可能整句复述这段提示词，所以不会误杀正常内容。
    """
    t = _ECHO_STRIP.sub("", str(text or ""))
    p = _ECHO_STRIP.sub("", _INITIAL_PROMPT)
    if len(t) < 6:
        return False
    return t in p or p in t


def _read_cache(paths, need_words=False):
    """按顺序读缓存，命中就返回 segments，全都不合用返回 None

    - 明确标记过 empty 的（上次就没听到人声）直接返回 []，别让它反复重转
    - 老缓存里可能存着「回读提示词」的假字幕，读的时候一并滤掉；
      滤完为空说明这段本来就没声，也按 [] 处理
    need_words=True 时只认「第一条带 words」的缓存：没有词级时间戳，
    语气词定位会把整句当成一个词，剪点必然错。
    """
    for path in paths or []:
        try:
            if not path.exists():
                continue
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        if data.get("empty") is True:
            return []
        segs = data.get("segments") or []
        if not segs:
            continue
        segs = [x for x in segs if not _is_prompt_echo(x.get("text"))]
        if not segs:
            # 整份都是回读提示词的假字幕：等同于「没人声」，用空结果，别再重转
            return []
        if need_words and not segs[0].get("words"):
            continue
        return segs
    return None


def transcribe(video_path, model_size=None, language="zh", progress_cb=None,
               cancel_flag=None, use_cache=True, word_level=False):
    """语音转写 → [{"start":0.0,"end":2.4,"text":"大家好"}]（原片时间轴）

    word_level=True 时每条多带 "words": [{"start","end","text"}]，
    语气词/重说检测要靠它定位到词，没有词级时间戳就只能整段剪。
    """
    size = model_size or DEFAULT_MODEL
    cache = _cache_path(video_path, size)
    legacy = _legacy_cache_paths(video_path, size)
    if use_cache:
        # 新单份缓存优先；老缓存要词级就先看 _w，不要词级就先看 _s
        paths = [cache] + (legacy if word_level else [legacy[1], legacy[0]])
        segs = _read_cache(paths, need_words=word_level)
        # [] 也算命中（上次就没听到人声），直接返回，别再白听一遍
        if segs is not None:
            if progress_cb:
                progress_cb(100, f"用上次的转写结果（{len(segs)} 条）")
            return segs

    model = load_model(size, progress_cb=progress_cb, cancel_flag=cancel_flag)

    total = _probe_duration(video_path) or 0.0
    if progress_cb:
        progress_cb(12, "听写中…")

    kw = dict(language=language or None, beam_size=5, vad_filter=True,
              vad_parameters={"min_silence_duration_ms": 400},
              condition_on_previous_text=False)
    if word_level:
        kw["word_timestamps"] = True
    # 给点上下文，中文输出更容易带标点
    kw["initial_prompt"] = _INITIAL_PROMPT

    segments_iter, info = model.transcribe(str(video_path), **kw)

    out = []
    for seg in segments_iter:
        if cancel_flag and cancel_flag():
            raise SubtitleCancelled()
        text = (seg.text or "").strip()
        if text:
            item = {"start": round(float(seg.start), 3),
                    "end": round(float(seg.end), 3),
                    "text": text}
            if word_level and getattr(seg, "words", None):
                item["words"] = [{"start": round(float(w.start), 3),
                                  "end": round(float(w.end), 3),
                                  "text": w.word} for w in seg.words]
            out.append(item)
        if progress_cb and total > 0:
            pct = 12 + 83 * min(1.0, float(seg.end) / total)
            progress_cb(pct, f"听写中 {int(min(1.0, seg.end / total) * 100)}%")

    if progress_cb:
        progress_cb(96, f"转写完，共 {len(out)} 条")

    # 没听到人声时 whisper 会把提示词原样回读成字幕，这种绝对不能烧进成片
    kept = [x for x in out if not _is_prompt_echo(x.get("text"))]
    dropped = len(out) - len(kept)
    out = kept
    if dropped > 0 and progress_cb:
        progress_cb(97, f"过滤掉 {dropped} 条语音识别回读提示词的假字幕")

    # 统一写回新单份缓存；但如果这次没做词级、而缓存里已经有带 words 的，
    # 就别覆盖——否则「先词级转写、再句级读缓存」会把词级结果写没了。
    try:
        has_words = any(seg.get("words") for seg in out)
        if has_words or not _read_cache([cache] + legacy, need_words=True):
            cache.write_text(json.dumps({"segments": out,
                                         "word_level": bool(word_level or has_words),
                                         "language": getattr(info, "language", language),
                                         # 没人声也要落盘：否则每次口播都白听几分钟
                                         "empty": not out},
                                        ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass
    return out


def _probe_duration(path):
    from utils.ffmpeg_setup import ensure_ffmpeg
    exe = ensure_ffmpeg()
    if not exe:
        return 0.0
    flags = 0x08000000 if os.name == "nt" else 0
    try:
        r = subprocess.run([exe, "-hide_banner", "-i", str(path)],
                           capture_output=True, creationflags=flags)
        m = re.search(rb"Duration:\s*(\d+):(\d+):(\d+\.\d+)", r.stderr or b"")
        if m:
            h, mi, s = int(m.group(1)), int(m.group(2)), float(m.group(3))
            return h * 3600 + mi * 60 + s
    except Exception:
        pass
    return 0.0


# ---------------- SRT 读写 ----------------

_SRT_TIME = re.compile(r"(\d+):(\d{2}):(\d{2})[,.](\d{1,3})\s*-->\s*(\d+):(\d{2}):(\d{2})[,.](\d{1,3})")


def parse_srt(content):
    """SRT / VTT 文本 → segments"""
    content = (content or "").replace("\r\n", "\n").replace("\r", "\n")
    content = re.sub(r"^WEBVTT.*?\n\n", "", content, flags=re.S)
    out = []
    for block in re.split(r"\n\s*\n", content):
        lines = [ln for ln in block.split("\n") if ln.strip()]
        if not lines:
            continue
        m = None
        idx = 0
        for i, ln in enumerate(lines):
            m = _SRT_TIME.search(ln)
            if m:
                idx = i
                break
        if not m:
            continue
        a = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3)) + int(m.group(4).ljust(3, "0")) / 1000
        b = int(m.group(5)) * 3600 + int(m.group(6)) * 60 + int(m.group(7)) + int(m.group(8).ljust(3, "0")) / 1000
        text = " ".join(lines[idx + 1:]).strip()
        text = re.sub(r"<[^>]+>", "", text)          # 去掉 <i> 之类标签
        if text and b > a:
            out.append({"start": round(a, 3), "end": round(b, 3), "text": text})
    out.sort(key=lambda x: x["start"])
    return out


def to_srt(segments):
    def ts(t):
        t = max(0.0, float(t))
        h = int(t // 3600)
        m = int((t % 3600) // 60)
        s = int(t % 60)
        ms = int(round((t - int(t)) * 1000))
        if ms >= 1000:
            ms = 999
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

    lines = []
    for i, seg in enumerate(segments, 1):
        lines.append(str(i))
        lines.append(f"{ts(seg['start'])} --> {ts(seg['end'])}")
        lines.append(seg["text"])
        lines.append("")
    return "\n".join(lines)


# ---------------- 断句 ----------------

_PUNCT_END = "。！？；!?;"
_PUNCT_ALL = "，。！？；、,.!?;：:"
_CJK = re.compile(r"[\u4e00-\u9fff]")

# whisper 的中文输出常常混着半角标点，统一成全角才像字幕
_HALF2FULL = {",": "，", ";": "；", ":": "：", "?": "？", "!": "！"}


def _normalize_punct(t):
    if not t:
        return t
    for half, full in _HALF2FULL.items():
        t = re.sub(rf"(?<=[\u4e00-\u9fff])\s*{re.escape(half)}", full, t)
    # 中文之间的句点转句号；数字小数点（1.5）不动
    t = re.sub(r"(?<=[\u4e00-\u9fff])\.(?=$|[\u4e00-\u9fff])", "。", t)
    # 全角标点后面的空格没意义
    t = re.sub(r"([，。！？；：、])\s+", r"\1", t)
    return t


def _clean_text(text):
    t = (text or "").strip()
    t = re.sub(r"\s+", " ", t)
    # whisper 偶尔冒出这些标记
    t = re.sub(r"\[\d+:\d+[^\]]*\]|\([^)]*\)|（[^）]*）(?=\s*$)", "", t).strip()
    return _normalize_punct(t)


def split_text(text, max_chars=18):
    """把一句长文本切成适合单屏显示的小段

    max_chars 是「软上限」：切段之后 build_ass 会按画面宽再折行（本机 libass 不给
    中文折行，见 line_capacity），所以宁可留一条稍长的整句、交给折行处理，
    也不要在词中间硬切（中文硬切切出来读着别扭，反而更难看）。
    只有一句话明显超长（超过软上限 1.35 倍）才动手切。
    """
    text = _clean_text(text)
    if not text:
        return []
    max_chars = int(max_chars or 18)
    if max_chars <= 0:
        max_chars = 18
    hard = max(max_chars + 6, int(max_chars * 1.35))
    if len(text) <= hard:
        return [text]

    atoms = [p.strip() for p in re.findall(r"[^，。！？；、,.!?;]*[，。！？；、,.!?;]?", text)]
    atoms = [a for a in atoms if a]

    # 单个原子块自己就超长时，内部均分（这时没办法，只能切）
    blocks = []
    for a in atoms:
        if len(a) <= hard:
            blocks.append(a)
            continue
        n = -(-len(a) // max_chars)          # ceil
        step = -(-len(a) // n)
        for i in range(0, len(a), step):
            blocks.append(a[i:i + step])

    out, buf = [], ""
    for b in blocks:
        if not buf:
            buf = b
        elif len(buf) + len(b) <= hard:
            buf += b
        else:
            out.append(buf)
            buf = b
    if buf:
        out.append(buf)
    return [s.strip() for s in out if s.strip()]


def segment_to_cues(segments, max_chars=16,
                    min_dur=0.6, max_dur=6.0, gap_hold=0.35):
    """转写段落 → 一行一条的字幕，时间按字数比例分配

    min_dur: 短于此的条目和上一条合并（否则一闪而过看不清）
    max_dur: 一条字幕最长显示时间，超出就按比例拆
    gap_hold: 一句话说完后字幕多留一会儿（不超过下一条开始）
    """
    cues = []
    for seg in segments:
        text = _clean_text(seg.get("text"))
        if not text:
            continue
        s, e = float(seg["start"]), float(seg["end"])
        if e - s < 0.08:
            continue

        chunks = split_text(text, max_chars)
        if not chunks:
            continue

        # 按字数占比分配时间（中文 1 字≈1 个时长单位，英文单词算 0.5）
        weights = []
        for c in chunks:
            cjk = len(_CJK.findall(c))
            other = len(c) - cjk
            weights.append(max(1.0, cjk + other * 0.5))
        total_w = sum(weights) or 1.0
        span = e - s
        cursor = s
        for i, (chunk, w) in enumerate(zip(chunks, weights)):
            dur = span * (w / total_w)
            if i == len(chunks) - 1:
                dur = max(0.05, e - cursor)
            cues.append({"start": round(cursor, 3), "end": round(cursor + dur, 3), "text": chunk})
            cursor += dur

    # 合并过短的条目（闪一下就没了字反而干扰）
    merged = []
    for c in cues:
        if merged and c["end"] - c["start"] < min_dur and c["start"] - merged[-1]["end"] < 0.5:
            merged[-1]["text"] += c["text"]
            merged[-1]["end"] = c["end"]
        else:
            merged.append(dict(c))

    # 过长的一条按最大时长截断（保持文字不丢，只是拆成两条）
    final = []
    for c in merged:
        dur = c["end"] - c["start"]
        if dur <= max_dur:
            final.append(c)
            continue
        n = int(dur // max_dur) + 1
        chunks = split_text(c["text"], max(6, len(c["text"]) // n + 1))
        if len(chunks) < 2:
            final.append(c)
            continue
        step = dur / len(chunks)
        for i, ch in enumerate(chunks):
            final.append({"start": round(c["start"] + i * step, 3),
                          "end": round(min(c["end"], c["start"] + (i + 1) * step), 3),
                          "text": ch})

    # 相邻条目之间多一点停留，读起来不急
    for i in range(len(final) - 1):
        nxt = final[i + 1]["start"]
        if final[i]["end"] < nxt - 0.05:
            final[i]["end"] = round(min(final[i]["end"] + gap_hold, nxt - 0.02), 3)
    return final


# 句末标点：后一条字幕是不以这些结尾就说明话没说完
_SENTENCE_TAIL = "。！？；!?;…"


def aggregate_segments(segments, max_chars=18, max_gap=0.45):
    """把碎句聚合成整句（词级转写出的句子往往被切得很碎）

    合并条件：相邻两条间隔 < max_gap、前一条不以句末标点结尾、
              合并后长度 <= max_chars*1.4

    保守起见默认几乎不合并：whisper 的 segment 本来就是一整句，
    只有像「然后」/「我们」被单独切出来的明显碎片才并句。
    返回新的 [{"start","end","text"}]，丢弃 words 字段。
    """
    if not segments:
        return []

    try:
        base = int(float(max_chars or 18))
    except (TypeError, ValueError):
        base = 18
    if base <= 0:
        base = 18
    limit = max(base, int(round(base * 1.4)))
    try:
        gap_limit = float(max_gap)
    except (TypeError, ValueError):
        gap_limit = 0.45

    out = []
    for seg in segments:
        try:
            s = float(seg.get("start"))
            e = float(seg.get("end"))
        except (TypeError, ValueError):
            continue
        if e < s:
            s, e = e, s
        text = str(seg.get("text") or "").strip()
        if not text:
            continue

        if out:
            prev = out[-1]
            tail = prev["text"][-1:]
            gap = s - prev["end"]
            # 话没说完 + 挨得够近 + 并起来不过长 → 就是同一句被切碎了
            if (gap < gap_limit and tail not in _SENTENCE_TAIL
                    and len(prev["text"]) + len(text) <= limit):
                prev["text"] += text
                prev["end"] = round(max(prev["end"], e), 3)
                continue

        out.append({"start": round(s, 3), "end": round(e, 3), "text": text})
    return out


# ---------------- 词时间对齐（逐字点亮的前提） ----------------

# 对齐只看「真正读出来的字」：标点空白全去掉再去比，否则半角/全角一混就全错位
_SIG_STRIP = re.compile(r"[\s，。、！？；：,.!?;:…“”\"'‘’（）()《》〈〉【】\[\]{}—－~·]+")


def _sig(text):
    """去掉标点和空白，只留读出来的字（对齐用）"""
    return _SIG_STRIP.sub("", str(text or ""))


def _flatten_word_times(segments):
    """按时间顺序把各段的 words 摊平成一个词表 [(start, end, text)]"""
    words = []
    for seg in segments or []:
        for w in (seg.get("words") or []):
            try:
                s, e = float(w.get("start")), float(w.get("end"))
            except (TypeError, ValueError):
                continue
            if e < s:
                s, e = e, s
            words.append((s, e, str(w.get("text") or "")))
    return words


def attach_words(cues, segments, max_mismatch=0.30):
    """给每条 cue 挂上 words（原片时间），对不上的 cue 不挂

    为什么要按字符顺序「消费」：
      cue 文本是 segment 文本经 split_text 连续切出来的（拼回去就是原文），
      所以拿掉标点空白后，第 k 个字属于第几个词是确定的——顺着数就行，
      不需要算什么坐标，也不会因为断句位置变了就失效。

    宁可不挂也绝不错位（错位的逐字点亮比没有还难看）：
      - 词表总字数和字幕总字数差超过 30% → 整份都不挂
      - 逐个字比对，错得太多（>30%）→ 整份都不挂
      - 某条 cue 一个词都没摊到 → 这条不挂，退化成整句显示
      - 切点落在词中间时按字符占比插值，允许一个词被两条 cue 共用

    返回新的 cues 列表，不修改入参。
    """
    out = [dict(c) for c in (cues or [])]
    if not out:
        return out

    words = _flatten_word_times(segments)
    if not words:
        return out                      # 句级转写：功能自动降级，不报错

    # 每条 cue 的「有效字」按顺序摊平：chars[k] = (cue 序号, 那个字)
    chars = []
    for ci, cue in enumerate(out):
        for ch in _sig(cue.get("text")):
            chars.append((ci, ch))
    total_chars = len(chars)
    if not total_chars:
        return out

    total_word_chars = sum(len(_sig(t)) for _, _, t in words)
    if total_word_chars <= 0:
        return out
    if abs(total_chars - total_word_chars) / float(total_word_chars) > max_mismatch:
        # 两份文本根本不是同一段话（比如用户手改过字幕），绝不硬套
        return out

    pieces = {}          # cue 序号 → [[start, end, text], ...]
    cursor = 0
    mismatch = 0
    for ws, we, wt in words:
        sig = _sig(wt)
        n = len(sig)
        if n <= 0:                   # 纯标点词（。！ 之类）没有时长可言
            continue
        if cursor + n > total_chars:
            break
        for k, ch in enumerate(sig):
            if chars[cursor + k][1] != ch:
                mismatch += 1
        span = we - ws
        k = 0
        while k < n:
            ci = chars[cursor + k][0]
            j = k
            while j + 1 < n and chars[cursor + j + 1][0] == ci:
                j += 1
            t0 = ws + span * (k / float(n))
            t1 = ws + span * ((j + 1) / float(n))
            pieces.setdefault(ci, []).append((t0, t1, wt))
            k = j + 1
        cursor += n

    # 逐字比对下来错得太多 → 一定是错位了，整份都不挂
    if cursor <= 0 or mismatch > max(2, int(0.30 * cursor)):
        return out

    for ci, lst in pieces.items():
        cue = out[ci]
        try:
            cs, ce = float(cue.get("start")), float(cue.get("end"))
        except (TypeError, ValueError):
            continue
        if ce - cs <= 0.02:
            continue
        got = []
        prev = cs
        for t0, t1, wt in lst:
            a = max(cs, min(ce, t0))
            b = max(cs, min(ce, t1))
            a = max(a, prev)          # 单调不交叠：后一个词的起点不能压到前一个
            b = max(b, a)
            prev = b
            if b - a <= 0.004:
                continue              # 被夹没了：这一小截时间由末词延伸补上
            got.append({"start": round(a, 3), "end": round(b, 3), "text": wt})
        if got:
            cue["words"] = got
    return out


def cues_from_transcription(segments, max_chars=16, with_words=False):
    """转写结果（词级或句级都行）→ 一行一条的字幕条目（原片时间）

    词级结果是碎句，先聚合成整句再断句；句级结果本来就成句，直接用。
    with_words=True 时给能对上的 cue 挂 "words": [{"start","end","text"}]，
    对不上（句级转写 / 用户改过文字 / 词表缺一半）就自动降级成整句显示。
    """
    segs = list(segments or [])
    if not segs:
        return []
    if any(seg.get("words") for seg in segs):
        segs = aggregate_segments(segs, max_chars=max_chars)
    cues = segment_to_cues(segs, max_chars=max_chars)
    if with_words and cues:
        # 聚合会丢掉 words 字段，所以对齐用的是**没聚合**的原始 segments
        cues = attach_words(cues, segments)
    return cues


def has_word_times(cues):
    """这批字幕能不能逐字点亮（任意一条挂上 words 就算能）"""
    return any(c.get("words") for c in (cues or []))


# ---------------- 时间轴映射（核心） ----------------

def render_timeline(keep_ranges, default_speed=1.0, transition=0.0):
    """算出每段在成片里的位置

    keep_ranges 元素可以是 (start, end) 或 {"start","end","speed"}
    transition 可以是统一秒数，也可以是「每个接缝一个重叠秒数」的列表
               （自动转场时每个接缝不一样，字幕必须用同一份数字）
    返回 [{"src_start","src_end","speed","out_start","out_end","out_len","index"}]
    """
    if not keep_ranges:
        return []

    segs = []
    for r in keep_ranges:
        if isinstance(r, dict):
            s, e = float(r.get("start")), float(r.get("end"))
            sp = float(r.get("speed") or default_speed or 1.0)
        else:
            s, e = float(r[0]), float(r[1])
            sp = float(default_speed or 1.0)
        if e - s <= 0.02:
            continue
        sp = max(0.1, min(10.0, sp))
        segs.append({"src_start": s, "src_end": e, "speed": sp,
                     "out_len": (e - s) / sp})

    if not segs:
        return []

    # 转场时相邻段重叠：第二段起点 = 前一段终点 - overlap
    per_seam = isinstance(transition, (list, tuple))
    overlap = 0.0 if per_seam else max(0.0, float(transition or 0.0))
    cursor = 0.0
    for i, seg in enumerate(segs):
        if i == 0:
            seg["out_start"] = 0.0
        else:
            raw = float(transition[i]) if (per_seam and i < len(transition)) else (
                0.0 if per_seam else overlap)
            # 重叠不能超过两段中较短的一半，否则会吃掉整段
            ov = min(raw, segs[i - 1]["out_len"] * 0.5, seg["out_len"] * 0.5)
            seg["out_start"] = round(cursor - max(0.0, ov), 4)
        seg["out_end"] = round(seg["out_start"] + seg["out_len"], 4)
        cursor = seg["out_end"]
        seg["index"] = i

    return segs


def _map_time(t, timeline):
    """把一个原片时刻映射到成片时刻；落在被剪掉的空白里返回 None"""
    for seg in timeline:
        if seg["src_start"] - 1e-6 <= t <= seg["src_end"] + 1e-6:
            offset = (t - seg["src_start"]) / seg["speed"]
            return seg["out_start"] + offset
    return None


def _map_words(words, timeline, a, b, o1, o2):
    """把落在这段可见窗口里的词映射到成片时间（给逐字点亮用）

    只收**整个词都落在窗口里**的：跨剪切点的词映射出来会错位，
    少亮一个词远好过点亮位置错乱。
    """
    out = []
    for w in words or []:
        try:
            ws, we = float(w.get("start")), float(w.get("end"))
        except (TypeError, ValueError):
            continue
        if ws < a - 1e-6 or we > b + 1e-6:
            continue
        s, e = _map_time(ws, timeline), _map_time(we, timeline)
        if s is None or e is None:
            continue
        s, e = max(s, o1), min(e, o2)
        if e - s <= 0.004:
            continue
        if out and s < out[-1]["end"]:
            s = out[-1]["end"]
        if e <= s:
            continue
        out.append({"start": round(s, 3), "end": round(e, 3), "text": w.get("text")})
    return out


def map_cues(cues, keep_ranges, default_speed=1.0, transition=0.0, min_dur=0.12):
    """原片字幕 → 成片字幕（按剪辑结果裁剪、重映射、变速折算）

    一条字幕如果横跨被剪掉的区间，会被拆成多段，各自对齐到成片位置。
    cue 上带 words（逐字点亮）时一并映射；映射不干净就不带，退化成整句显示。
    """
    timeline = render_timeline(keep_ranges, default_speed, transition)
    if not timeline or not cues:
        return []

    out = []
    for cue in cues:
        cs, ce = float(cue["start"]), float(cue["end"])
        text = cue.get("text") or ""
        if not text or ce <= cs:
            continue
        pieces = []
        for seg in timeline:
            a = max(cs, seg["src_start"])
            b = min(ce, seg["src_end"])
            if b - a <= 0.01:
                continue
            o1 = seg["out_start"] + (a - seg["src_start"]) / seg["speed"]
            o2 = seg["out_start"] + (b - seg["src_start"]) / seg["speed"]
            pieces.append((o1, o2, a, b, seg))

        if not pieces:
            continue

        if len(pieces) == 1:
            o1, o2, a, b, _ = pieces[0]
            if o2 - o1 >= min_dur:
                item = {"start": round(o1, 3), "end": round(o2, 3), "text": text}
                cw = _map_words(cue.get("words"), timeline, a, b, o1, o2)
                if cw:
                    item["words"] = cw
                out.append(item)
            continue

        # 跨了剪切点：按原片可见时长占比把文字也切碎
        total_src = sum(b - a for _, _, a, b, _ in pieces) or 1.0
        cursor = 0
        for k, (o1, o2, a, b, _) in enumerate(pieces):
            ratio = (b - a) / total_src
            n = max(1, int(round(len(text) * ratio)))
            if k == len(pieces) - 1:
                chunk = text[cursor:]
            else:
                chunk = text[cursor:cursor + n]
            cursor += n
            chunk = chunk.strip()
            if chunk and o2 - o1 >= min_dur:
                item = {"start": round(o1, 3), "end": round(o2, 3), "text": chunk}
                cw = _map_words(cue.get("words"), timeline, a, b, o1, o2)
                if cw:
                    item["words"] = cw
                out.append(item)

    out.sort(key=lambda x: x["start"])
    # 防重叠：后一条不能压到前一条的结束
    for i in range(len(out) - 1):
        if out[i]["end"] > out[i + 1]["start"]:
            out[i]["end"] = round(max(out[i]["start"] + 0.05, out[i + 1]["start"] - 0.02), 3)
    return [c for c in out if c["end"] - c["start"] > 0.05]


# ---------------- ASS 生成 ----------------

def _ass_time(t):
    t = max(0.0, float(t))
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    s = t % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def _ass_color(hex_color, alpha=0):
    """#RRGGBB → &HAABBGGRR（ASS 是 BGR 顺序）"""
    c = (hex_color or "#FFFFFF").strip().lstrip("#")
    if len(c) == 3:
        c = "".join(ch * 2 for ch in c)
    if len(c) != 6:
        c = "FFFFFF"
    try:
        r, g, b = int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)
    except ValueError:
        r = g = b = 255
    return f"&H{max(0, min(255, int(alpha))):02X}{b:02X}{g:02X}{r:02X}"


def _ass_bgr(hex_color):
    """#RRGGBB → &HBBGGRR&（行内 \\c 覆盖用的格式，不带 alpha）"""
    c = (hex_color or "#FFFFFF").strip().lstrip("#")
    if len(c) == 3:
        c = "".join(ch * 2 for ch in c)
    if len(c) != 6:
        c = "FFFFFF"
    try:
        r, g, b = int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)
    except ValueError:
        r = g = b = 255
    return f"&H{b:02X}{g:02X}{r:02X}&"


def _esc(text):
    t = (text or "").replace("\r", "").replace("\n", "\\N")
    t = t.replace("{", "｛").replace("}", "｝")
    return t


# 逐字点亮（karaoke）的默认高亮色：暖黄，压在白色字幕上对比够
DEFAULT_HIGHLIGHT = "#FFD54A"
# 底框默认不透明度（62 ≈ 半透明，压得住画面又不糊字）
DEFAULT_BOX_ALPHA = 62


def _box_alpha(alpha):
    """底框不透明度 0–100 → ASS alpha（ASS 是「越小越不透明」，所以反过来）"""
    try:
        a = float(alpha)
    except (TypeError, ValueError):
        a = DEFAULT_BOX_ALPHA
    a = max(0.0, min(100.0, a))
    return max(0, min(255, int(round((100.0 - a) / 100.0 * 255))))


def _karaoke_words(cue):
    """一条 cue 的词时间 → 干净的 [(text, 秒数)]，对不上返回 None

    只认 cue 自己的 words，且必须落在 cue 区间里（build_ass 再兜一次）。
    """
    try:
        cs, ce = float(cue.get("start")), float(cue.get("end"))
    except (TypeError, ValueError):
        return None
    if ce - cs <= 0.02:
        return None
    raw = cue.get("words") or []
    ws = []
    for w in raw:
        try:
            s, e = float(w.get("start")), float(w.get("end"))
        except (TypeError, ValueError):
            continue
        text = str(w.get("text") or "")
        if not _sig(text):
            continue                     # 纯标点词不占点亮时间
        if e < s:
            s, e = e, s
        # 词时间必须和 cue 有交集，否则说明这条 cue 的词表是借来的
        s = max(cs, min(ce, s))
        e = max(cs, min(ce, e))
        if e <= s:
            continue
        if ws and s < ws[-1][1]:         # 单调不交叠
            s = ws[-1][1]
        if e <= s:
            continue
        ws.append([s, e, text])
    if not ws:
        return None
    return {"start": cs, "end": ce, "words": ws}


def _karaoke_marks(cue):
    """一条 cue → ["{\\kf12}今天", "{\\kf8}大家", …]（每个词块不可再分）

    厘秒按 cue 相对时间重算，**总和精确等于 cue 时长**（误差 0）：
      首词 = 词尾 - cue 起点，中间词 = 本词尾 - 上词尾，
      末词直接补足余量（这样舍入误差全落在最后一个词上，不会越攒越多）。

    返回列表而不是拼好的字符串，是为了让折行只切在词块之间 —— `\\N` 落进
    `{}` 里会把整段 karaoke 搞坏，拆不开的词块才是安全的切点。
    """
    info = _karaoke_words(cue)
    if not info:
        return []
    cs, ce, ws = info["start"], info["end"], info["words"]
    total = int(round((ce - cs) * 100))
    if total <= 0:
        return []

    marks = []
    acc = 0
    for i, (s, e, text) in enumerate(ws):
        if i == len(ws) - 1:
            c = total - acc
        else:
            c = int(round((e - cs) * 100)) - acc
        c = max(0, min(c, total - acc))
        acc += c
        piece = _esc(text).strip()
        if not piece:
            continue
        # 英文词之间原本靠空格分开，strip 掉以后要补回来
        if text[:1].isspace() and marks and not marks[-1].endswith(" "):
            piece = " " + piece
        marks.append("{\\kf%d}%s" % (c, piece))
    return marks


def _karaoke_text(cue):
    """一条 cue → "{\\kf12}今天{\\kf8}大家…"（不折行的老用法，保持兼容）"""
    return "".join(_karaoke_marks(cue))


# ---------------- 折行 ----------------
# 本机 ffmpeg 的 libass **没有 CJK 断行能力**（编译时没带 libunibreak）：只对带空格的
# 拉丁文折行，中文一路顶到画面外、首末字被裁。所以宽度得自己算，超了主动插 \N。
# 中文字宽实测 ≈ 0.757 * font_size（1080 宽下 8/10/12 字 = 640/803/965px），
# 这里取 0.80 略保守，保证折出来的行一定放得下。
WRAP_CHAR_RATIO = 0.80
# MarginL/R 在 Style 行里固定 60，**不许按画面宽缩放**：
# 一改横屏 1920 的可用宽度就变了，18 字的老成片会突然折行，零回归就破了。
WRAP_MARGIN = 60
# 最多折 5 行。自动转写碰不到（split_text 硬上限 24 字，1080 竖屏 3 行就够），
# 但**导入 SRT 是常规路径**，外部 SRT 一行 40–60 字很普遍：行数封死在 3 时，
# 48 字以上每行就得挤 13+ 字、重新切边（实测 48/60 字左右余量 0/0）。
# 按 per=11 算，60 字 → 5 行 × 12 字 = 960px 正好等于可用宽度，不切。
WRAP_MAX_ROWS = 5

# 半角字符相对汉字的宽度（0.55 ≈ 微软雅黑里西文平均宽度 / 汉字宽度）
_HALF_WIDTH = 0.55

# 半角字符的**实测**宽度表（单位：一个汉字 = 1.0，微软雅黑 font=106 下逐字量出来的）：
#   W 86.6px / M 81.2 / @ 81.8 / m 78.0 / w 68.6 / H 64.6 / n 51.1 / a 45.9 /
#   t 33.2 / i·l 23.0，汉字宽 80.2px。
# 以前一律按 0.55 折算是**低估**：W 实际是 0.55 的 1.96 倍（1.08 个汉字宽），
# 于是「17 个 M」被算成 9.35 个汉字宽 < per=11，连折行都不触发，直接单行切边。
# 表里的值都在实测值上**向上取整到 0.05**（宁可高估早折一行，也不能低估切字）。
_GLYPH_W = {
    # 小写
    "a": 0.60, "b": 0.65, "c": 0.55, "d": 0.65, "e": 0.60, "f": 0.40, "g": 0.65,
    "h": 0.65, "i": 0.30, "j": 0.30, "k": 0.60, "l": 0.30, "m": 1.00, "n": 0.65,
    "o": 0.65, "p": 0.65, "q": 0.65, "r": 0.45, "s": 0.55, "t": 0.45, "u": 0.65,
    "v": 0.60, "w": 0.90, "x": 0.60, "y": 0.60, "z": 0.55,
    # 大写
    "A": 0.80, "B": 0.75, "C": 0.75, "D": 0.80, "E": 0.70, "F": 0.65, "G": 0.80,
    "H": 0.85, "I": 0.40, "J": 0.50, "K": 0.75, "L": 0.65, "M": 1.05, "N": 0.85,
    "O": 0.85, "P": 0.70, "Q": 0.85, "R": 0.75, "S": 0.75, "T": 0.65, "U": 0.80,
    "V": 0.75, "W": 1.10, "X": 0.75, "Y": 0.70, "Z": 0.65,
    # 数字与常见符号
    "@": 1.05, "&": 0.85, "#": 0.80, "%": 0.95, "0": 0.75, "1": 0.75, "2": 0.75,
    "3": 0.75, "4": 0.75, "5": 0.75, "6": 0.75, "7": 0.75, "8": 0.75, "9": 0.75,
    ".": 0.45, ",": 0.45, ":": 0.45, ";": 0.45, "'": 0.40, "\"": 0.45, "!": 0.45,
    "?": 0.55, "(": 0.45, ")": 0.45, "[": 0.45, "]": 0.45, "{": 0.45, "}": 0.45,
    "/": 0.45, "\\": 0.45, "|": 0.40, "`": 0.45, "-": 0.40, "_": 0.60,
    "*": 0.75, "+": 0.75, "=": 0.75, "~": 0.75, "^": 0.65, "<": 0.75, ">": 0.75,
}

_KF_TAG = re.compile(r"\{[^{}]*\}")


def _visible(piece):
    """去掉 {...} 覆盖块，只留看得见的文字"""
    return _KF_TAG.sub("", piece or "")


def _char_width(ch):
    """单个字符的显示宽度（一个汉字 = 1.0）"""
    if ch.isspace():
        return 0.0
    if ord(ch) >= 0x2E80:            # 汉字 / 全角标点 / CJK 扩展
        return 1.0
    return _GLYPH_W.get(ch, _HALF_WIDTH)


def _unit_width(text):
    """显示宽度（以「一个汉字 = 1」为单位）：空白不占宽，半角查 _GLYPH_W"""
    n = 0.0
    for ch in text or "":
        n += _char_width(ch)
    return n


def unit_capacity(per):
    """一行最保守能放多少个**最宽字形**（W = 1.10 个汉字宽）

    只是给外部/日志用的粗估：真正的硬切已经改成按宽度算（`_split_token`），
    因为字符数这个口径对宽字形（W/M/@）根本不成立——17 个 M 按 0.55 折算
    只有 9.35 个汉字宽、看着没超，实际渲染出来有 13.8 个汉字宽，直接顶出画面。
    """
    try:
        per = float(per)
    except (TypeError, ValueError):
        return 0
    if per <= 0:
        return 0
    widest = max(_GLYPH_W.values())          # W = 1.10
    return max(1, int(per / widest))


def _split_token(tok, per):
    """把放不下的半角长串按**宽度**切成若干段（每段宽度 ≤ per，且尽量等宽）

    网址、连写的长单词都没有空格，整串是一个不可断单元；不切就只能整行溢出。
    段数取 ceil(总宽/per)，切点按「总宽/段数」找最近的位置 —— 直接贪心塞满会
    切出 10.6 + 1.8 这种尾巴，最后一行只剩三四个字符。
    """
    if per <= 0:
        return [tok]
    total = _unit_width(tok)
    if total <= per + 1e-9:
        return [tok]
    n = max(2, int(-(-total // per)))            # 需要几段（向上取整）
    goal = min(total / float(n), per)            # 每段目标宽度，绝不超过 per
    out, buf, w = [], "", 0.0
    for i, ch in enumerate(tok):
        cw = _char_width(ch)
        if buf and len(out) < n - 1 and i < len(tok) - 1 and w + cw > goal + 1e-9:
            out.append(buf)
            buf, w = "", 0.0
        buf += ch
        w += cw
    if buf:
        out.append(buf)
    return out or [tok]


def line_capacity(video_w, font_size):
    """一行最多放几个字（按可用宽度算，不含左右边距）"""
    avail = max(1, int(video_w or 1920) - 2 * WRAP_MARGIN)
    per = int(avail / max(1e-6, WRAP_CHAR_RATIO * float(font_size)))
    return max(4, per)


def _text_units(text, per=0):
    """显示文字 → 不可再分的折行单元 [(文本, 宽度)]

    中文（含全角标点）一个字一个单元 —— 中文在哪儿断都读得下去；
    半角连续段（英文词、数字）整段一个单元 —— 从词中间断开才是真的难看。

    per > 0：半角单元**宽度超过一行**就按宽度硬切。网址、长串连写没有空格，
    整串就是一个单元，`_split_rows` 只能在单元之间切，那就只能整行溢出。
    """
    units, latin = [], ""

    def flush():
        """把攒着的半角串吐成一个（或几个）单元"""
        nonlocal latin
        if not latin:
            return
        tok, latin = latin, ""
        if per > 0 and _unit_width(tok) > per + 1e-9:
            units.extend(_split_token(tok, per))
        else:
            units.append(tok)

    for ch in text or "":
        if ch.isspace():
            # 空格并进前一个单元：既保证拼回去和原文逐字一致，也让切点天然落在
            # 词尾。**不能让它单独成单元**——0 宽的纯空格单元会让续行以空格开头，
            # 居中排版下整行偏半格（`这个功能叫 auto cut` 实测偏 ~18px）。
            flush()
            if units:
                units[-1] = units[-1] + ch
            else:
                latin += ch          # 行首空格：先攒着，等后面有字再一起成单元
            continue
        if ord(ch) < 0x2E80:
            latin += ch
            continue
        flush()
        units.append(ch)
    flush()
    return [(t, _unit_width(t)) for t in units]


def _split_rows(units, rows):
    """把单元切成 rows 行：行数最少、各行尽量等宽，切点只落在单元之间"""
    n = len(units)
    rows = max(1, min(int(rows), n))
    total = sum(u[1] for u in units)
    out, start, acc = [], 0, 0.0
    for k in range(1, rows):
        target = total * k / float(rows)
        limit = n - (rows - k)          # 后面每一行至少留一个单元
        best_i, best_val, cur = start + 1, None, acc
        for i in range(start, limit):
            cur += units[i][1]
            if best_val is None or abs(cur - target) < abs(best_val - target) - 1e-9:
                best_i, best_val = i + 1, cur
        if best_val is None:            # 理论上到不了，兜底也要切走一个
            best_i, best_val = start + 1, acc + units[start][1]
        out.append(units[start:best_i])
        start, acc = best_i, best_val
    out.append(units[start:])
    return out


def _join_rows(units, per):
    """单元列表 → 折行文本；总宽没超 per 就原样拼回去（逐字不变）"""
    if not units:
        return ""
    total = sum(u[1] for u in units)
    if total <= per or len(units) < 2:
        return "".join(u[0] for u in units)
    rows = 1
    while rows < WRAP_MAX_ROWS and total > per * rows:
        rows += 1
    return "\\N".join("".join(u[0] for u in g) for g in _split_rows(units, rows))


def _wrap_plain(txt, per):
    """普通文本折行（原文里本来就有 \\N 的，按段各自折，不再跨段均衡）"""
    if "\\N" not in txt:
        return _join_rows(_text_units(txt, per), per)
    return "\\N".join(_join_rows(_text_units(s, per), per) for s in txt.split("\\N"))


_KF_MARK = re.compile(r"^\{\\kf(\d+)\}(.*)$", re.S)


def _split_kf_mark(mark, per):
    """把超长的词块按宽度切开，\\kf 厘秒按字符数比例分（**总和一分不变**）

    whisper 对着一串连写的网址/长单词只会吐一个 word，时长全压在一个词块上；
    不拆就是整行溢出。拆开以后第一段先亮、第二段接着亮，语速观感反而是对的。
    末段直接吃掉余量，保证厘秒总和精确等于原值。
    """
    m = _KF_MARK.match(mark or "")
    if not m:
        return [mark]
    total, body = int(m.group(1)), m.group(2)
    if per <= 0 or _unit_width(body) <= per + 1e-9:
        return [mark]
    pieces = _split_token(body, per)
    if len(pieces) < 2:
        return [mark]
    out, acc, seen = [], 0, 0
    for i, piece in enumerate(pieces):
        seen += len(piece)
        if i == len(pieces) - 1:
            c = total - acc                         # 末段补足余量
        else:
            c = int(round(total * seen / float(len(body)))) - acc
        c = max(0, min(c, total - acc))
        acc += c
        out.append("{\\kf%d}%s" % (c, piece))
    return out


def _wrap_karaoke(marks, per):
    """卡拉OK 文本折行：\\N 只插在两个词块之间，\\kf 的厘秒总和一个都不动"""
    units = []
    for m in marks:
        vis = _visible(m)
        if per > 0 and _unit_width(vis) > per + 1e-9:
            for piece in _split_kf_mark(m, per):
                units.append((piece, _unit_width(_visible(piece))))
        else:
            units.append((m, _unit_width(vis)))
    return _join_rows(units, per)


def build_ass(cues, video_w=1920, video_h=1080, style=None):
    """生成 ASS 字幕文件内容

    style:
      size_ratio     字号占画面高度的比例（0.045 ≈ 1080p 下 48px）
      color          文字颜色 #RRGGBB
      outline        描边粗细（像素，按画高缩放；0 就是没描边）
      outline_color  描边颜色 #RRGGBB
      position       bottom | top | center
      bold           是否加粗
      box            是否用底框代替描边（BorderStyle=3）
      box_alpha      底框不透明度 0–100（默认 62）。
                     注意：本项目 ffmpeg/libass 实测会**忽略这个 alpha**，底框仍然是不透明的；
                     ASS 里的值写对了（BackColour 的 alpha 字节），换别的播放器/剪辑软件才生效。
      margin_v       距底部（或顶部）的间距比例
      highlight      逐字点亮（libass 原生 karaoke，靠 word 时间）
      highlight_color 已读部分的颜色 #RRGGBB

    折行：本机 libass 不会给中文折行（没有 libunibreak），所以这里按
    `line_capacity()` 算出的每行容量主动插 \\N。总宽没超容量时**一个字都不动**，
    横屏 1920 下 18 字的输出与没有折行逻辑之前逐字一致。
    """
    st = {
        "size_ratio": 0.055,
        "color": "#FFFFFF",
        "outline": 2.6,
        "position": "bottom",
        "bold": True,
        "box": False,
        "margin_v": 0.06,
        "font": "微软雅黑",
    }
    if isinstance(style, dict):
        for k, v in style.items():
            if v is not None:
                st[k] = v

    w = int(video_w or 1920)
    h = int(video_h or 1080)
    font_size = max(14, int(round(h * float(st["size_ratio"]))))
    outline = max(0.0, float(st["outline"]) * h / 1080.0)
    margin_v = max(0, int(round(h * float(st["margin_v"]))))

    align = {"bottom": 2, "top": 8, "center": 5}.get(str(st["position"]), 2)
    box = bool(st.get("box"))
    border_style = 3 if box else 1
    # 底框 + 逐字点亮能不能一起开：实测（本项目 bin/ffmpeg.exe + libass）不会
    # 出现「一字一框」，相邻 \kf 段的框是连成一整条的，所以两者可以同时用。
    highlight = bool(st.get("highlight"))
    if box:
        back_alpha = _box_alpha(st.get("box_alpha", DEFAULT_BOX_ALPHA))
    else:
        back_alpha = 0x00                # 没有底框时这个值不参与渲染，保持老样子

    # 先把每条的 karaoke 文本算出来：**真有一条能点亮**才换样式色。
    # 否则（导入的 SRT / 句级转写没有词时间）整句会被渲染成高亮色，
    # 看着就像「全部都读完了」，比不亮还糟——这种情况一律保持原样。
    karaoke = {}
    if highlight:
        for idx, cue in enumerate(cues):
            k = _karaoke_marks(cue)
            if k:
                karaoke[idx] = k
    use_k = highlight and bool(karaoke)

    if use_k:
        # libass 的 karaoke 语义（本项目实测，t=0.2s 全 Secondary / t=3.6s 全 Primary）：
        #   没念到的字用 SecondaryColour，念到的字用 PrimaryColour。
        # 所以「已读 = 高亮色」必须写在 Primary 上，反过来的话点亮方向就是反的。
        primary = _ass_color(st.get("highlight_color") or DEFAULT_HIGHLIGHT)
        secondary = _ass_color(st.get("color"))
    else:
        primary = _ass_color(st.get("color"))
        secondary = "&H000000FF"         # 老文件里的值，逐字不变
    outline_col = _ass_color(st.get("outline_color") or "#000000")
    normal_bgr = _ass_bgr(st.get("color"))

    head = [
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {w}",
        f"PlayResY: {h}",
        "WrapStyle: 0",
        "ScaledBorderAndShadow: yes",
        "YCbCr Matrix: TV.709",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
        "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
        "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        "Style: Default,{font},{size},{primary},{secondary},{outline_col},{back},{bold},"
        "0,0,0,100,100,0.5,0,{border},{outline},0.8,{align},60,60,{mv},1".format(
            font=st["font"], size=font_size,
            primary=primary, secondary=secondary, outline_col=outline_col,
            back=f"&H{back_alpha:02X}000000",
            bold=-1 if st.get("bold") else 0,
            border=border_style, outline=f"{outline:.1f}",
            align=align, mv=margin_v,
        ),
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]

    lines = []
    per = line_capacity(w, font_size)
    for idx, c in enumerate(cues):
        txt = _esc(c.get("text"))
        if not txt:
            continue
        if use_k:
            ktxt = karaoke.get(idx)
            if ktxt:
                # \kf 只作用于当前这条 Dialogue，逐行独立；
                # \N 只切在词块之间，厘秒总和不受影响
                txt = _wrap_karaoke(ktxt, per)
            else:
                # 这条没词时间：用行内 \c 把它压回文字色，
                # 不然它会跟着样式里的高亮色一起变黄（等于骗人「读完了」）
                txt = "{\\c" + normal_bgr + "}" + _wrap_plain(txt, per)
        else:
            txt = _wrap_plain(txt, per)
        lines.append(f"Dialogue: 0,{_ass_time(c['start'])},{_ass_time(c['end'])},"
                     f"Default,,0,0,0,,{txt}")
    return "\n".join(head + lines) + "\n"


def write_ass(cues, out_path, video_w=1920, video_h=1080, style=None):
    content = build_ass(cues, video_w, video_h, style)
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    Path(out_path).write_text(content, encoding="utf-8-sig")   # BOM 让 libass 正确识别 UTF-8
    return str(out_path)


# ---------------- 烧录 ----------------

def burn(video_in, video_out, ass_path, progress_cb=None, cancel_flag=None,
         crf=20, preset="medium"):
    """把 ASS 字幕烧进画面（重新编码视频，音频直接复制）

    刻意把工作目录切到 ass 所在目录、滤镜里只写文件名：
    Windows 路径里的冒号和反斜杠在 ffmpeg 滤镜里是转义地狱，绕开最省事。
    """
    from utils.ffmpeg_setup import ensure_ffmpeg

    exe = ensure_ffmpeg()
    if not exe:
        raise RuntimeError("找不到 ffmpeg，无法烧录字幕")

    ass = Path(ass_path).resolve()
    work = ass.parent
    flags = 0x08000000 if os.name == "nt" else 0
    # cwd 会切到 ass 目录，所以输入输出一律转绝对路径，否则相对路径会解析错
    vout = Path(video_out).resolve()
    vout.parent.mkdir(parents=True, exist_ok=True)
    tmp = str(vout.with_suffix(".burn.mp4"))

    cmd = [
        exe, "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(Path(video_in).resolve()),
        "-vf", f"subtitles={ass.name}:fontsdir=.",
        "-c:v", "libx264", "-crf", str(crf), "-preset", preset,
        "-pix_fmt", "yuv420p",
        "-c:a", "copy",
        "-movflags", "+faststart",
        tmp,
    ]
    if progress_cb:
        progress_cb(0, "压制字幕…")
    r = subprocess.run(cmd, capture_output=True, creationflags=flags, cwd=str(work))
    if cancel_flag and cancel_flag():
        try:
            os.remove(tmp)
        except Exception:
            pass
        raise SubtitleCancelled()

    if r.returncode != 0 or not Path(tmp).exists():
        err = (r.stderr or b"").decode("utf-8", "ignore")[-400:]
        try:
            if Path(tmp).exists():
                os.remove(tmp)
        except Exception:
            pass
        raise RuntimeError(f"字幕烧录失败：{err}")

    os.replace(tmp, str(video_out))
    if progress_cb:
        progress_cb(100, "字幕已烧录")
    return str(video_out)


# ---------------- 便捷入口 ----------------

def speech_to_cues(video_path, model_size=None, max_chars=16, language="zh",
                   progress_cb=None, cancel_flag=None, with_words=False):
    """一条龙：转写 → 断句 → 字幕条目（还没映射时间）

    with_words=True 时会要求词级时间戳（逐字点亮要用），
    拿不到就是句级结果，cues 上不会挂 words，功能自动降级。
    """
    segs = transcribe(video_path, model_size=model_size, language=language,
                      progress_cb=progress_cb, cancel_flag=cancel_flag,
                      word_level=bool(with_words))
    return cues_from_transcription(segs, max_chars=max_chars, with_words=with_words)
