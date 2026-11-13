# -*- coding: utf-8 -*-
"""
音频特效 —— 换 BGM / 音量 / 淡入淡出 / 变速音频

全部走 ffmpeg 命令行，理由：
- 变速要保持音调（moviepy 改采样率会让声音变尖），只有 atempo 能做到
- BGM 混音要精确控制两路音量的相对关系，用滤镜图最直观
- 这些步骤都不碰画面，可以用 -c:v copy，零画质损失

所有失败都抛 RuntimeError 并带 ffmpeg 的原始报错，不吞错误。
"""

import hashlib
import json
import os
import re
import subprocess
from pathlib import Path

AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".wma", ".aiff"}

# 常见 BGM 音量档：口播类要压得更低，纯音乐可以稍高
BGM_PRESETS = {
    "soft": {"label": "很轻（口播）", "volume": 0.14},
    "normal": {"label": "适中（日常）", "volume": 0.24},
    "strong": {"label": "明显（混剪）", "volume": 0.40},
}


def _exe():
    from utils.ffmpeg_setup import ensure_ffmpeg
    exe = ensure_ffmpeg()
    if not exe:
        raise RuntimeError("找不到 ffmpeg")
    return exe


def _run(cmd, cwd=None):
    flags = 0x08000000 if os.name == "nt" else 0
    return subprocess.run(cmd, capture_output=True, creationflags=flags, cwd=cwd)


def _err(r, what):
    msg = (r.stderr or b"").decode("utf-8", "ignore").strip()
    tail = msg[-400:] if msg else "未知错误"
    raise RuntimeError(f"{what}失败：{tail}")


# ---------------- 探测 ----------------

def probe(path):
    """返回 {duration, has_audio, width, height, fps, ar, ch}"""
    exe = _exe()
    r = _run([exe, "-hide_banner", "-i", str(path)])
    text = (r.stderr or b"").decode("utf-8", "ignore")
    info = {"duration": 0.0, "has_audio": False, "width": 0, "height": 0,
            "fps": 0.0, "ar": 48000, "ch": "stereo"}

    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", text)
    if m:
        info["duration"] = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))

    if re.search(r"Stream #\d+:\d+.*: Audio:", text):
        info["has_audio"] = True
        # 采样率和声道留着后面统一用：24k 单声道被自动转 44.1k 立体声会掉 3dB
        ma = re.search(r"Audio:[^\n]*?(\d+)\s*Hz,\s*([a-z0-9.]+)", text)
        if ma:
            info["ar"] = int(ma.group(1))
            info["ch"] = ma.group(2)

    mv = re.search(r"Stream #\d+:\d+.*: Video:.*?(\d{2,5})x(\d{2,5})", text)
    if mv:
        info["width"], info["height"] = int(mv.group(1)), int(mv.group(2))
    mf = re.search(r"(\d+(?:\.\d+)?)\s*fps", text)
    if mf:
        info["fps"] = float(mf.group(1))
    return info


def atempo_chain(speed):
    """atempo 单次只能 0.5–2.0，超出范围要串联"""
    s = max(0.05, float(speed or 1.0))
    if abs(s - 1.0) < 0.005:
        return None
    parts = []
    remain = s
    while remain > 2.0:
        parts.append("atempo=2.0")
        remain /= 2.0
    while remain < 0.5:
        parts.append("atempo=0.5")
        remain /= 0.5
    parts.append(f"atempo={remain:.6f}")
    return ",".join(parts)


def _upmix(src_ch="stereo"):
    """把一个声道数不定的输入统一成 48k 立体声，且不丢电平

    坑：aformat=channel_layouts=stereo 走的是 swresample 的功率守恒声像，
    单声道升立体声时每个声道各乘 1/√2，实测整体掉 3dB，人声听着就变小了。
    单声道必须用显式 pan 复制（各路增益 1.0）才不会掉。
    """
    ch = (src_ch or "stereo").lower()
    if "mono" in ch:
        return "pan=stereo|c0=c0|c1=c0,aformat=sample_rates=48000"
    return "aformat=sample_rates=48000:channel_layouts=stereo"


def build_bgm_filter(bgm_volume=0.24, duration=0.0, fade_in=1.5, fade_out=2.0,
                     keep_original=True, original_volume=1.0, duck=False,
                     src_ch="stereo"):
    """拼 BGM 那一路的滤镜（输入 1 已用 -stream_loop 循环）

    src_ch 是原片的声道布局，用来决定怎么上混（见 _upmix 的坑）。
    BGM 那边本来就是独立文件，按立体声 aformat 不会掉电平。
    """
    bgm_fmt = "aformat=sample_rates=48000:channel_layouts=stereo"
    ofmt = _upmix(src_ch)
    chain = [bgm_fmt, f"volume={max(0.0, min(4.0, float(bgm_volume))):.4f}"]
    if fade_in > 0.05:
        chain.append(f"afade=t=in:st=0:d={fade_in:.2f}")
    if duration > 0 and fade_out > 0.05:
        st = max(0.0, duration - fade_out)
        chain.append(f"afade=t=out:st={st:.2f}:d={fade_out:.2f}")
    if duration > 0:
        chain.append(f"atrim=0:{duration:.3f}")
    bgm_chain = ",".join(chain)

    if not keep_original:
        return f"[1:a]{bgm_chain}[aout]"

    ov = max(0.0, min(4.0, float(original_volume)))
    if duck:
        # 人声侧链压缩：有说话时自动把 BGM 压下去，说完自己回来
        return (
            f"[0:a]{ofmt},volume={ov:.4f},asplit=2[vo][sc];"
            f"[1:a]{bgm_chain}[bg];"
            f"[bg][sc]sidechaincompress=threshold=0.08:ratio=6:attack=20:release=380[bgd];"
            f"[vo][bgd]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[aout]"
        )
    return (
        f"[0:a]{ofmt},volume={ov:.4f}[vo];"
        f"[1:a]{bgm_chain}[bg];"
        f"[vo][bg]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[aout]"
    )


def mix_bgm(video_in, video_out, bgm_path, bgm_volume=0.24, keep_original=True,
            original_volume=1.0, fade_in=1.5, fade_out=2.0, duck=False,
            duration=None, progress_cb=None):
    """把 BGM 铺到成片上

    keep_original=True  原声 + BGM 混合（口播/教程）
    keep_original=False 丢掉原声，只留 BGM（纯混剪）
    duck=True           有说话时自动压低 BGM
    """
    bgm = Path(bgm_path)
    if not bgm.exists():
        raise RuntimeError(f"找不到 BGM 文件：{bgm_path}")

    exe = _exe()
    info = probe(video_in)
    dur = float(duration or info["duration"] or 0)

    if not info["has_audio"]:
        keep_original = False   # 原片没声音，没什么可混的

    if progress_cb:
        progress_cb(10, "铺 BGM…")

    tmp = str(Path(video_out).with_suffix(".bgm.mp4"))
    Path(video_out).parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        exe, "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(video_in),
        "-stream_loop", "-1", "-i", str(bgm),
        "-filter_complex", build_bgm_filter(
            bgm_volume=bgm_volume, duration=dur, fade_in=fade_in, fade_out=fade_out,
            keep_original=keep_original, original_volume=original_volume, duck=duck,
            src_ch=info.get("ch") or "stereo"),
        "-map", "0:v:0", "-map", "[aout]",
        "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
        "-movflags", "+faststart",
    ]
    if dur > 0:
        cmd += ["-t", f"{dur:.3f}"]
    cmd.append(tmp)

    r = _run(cmd)
    if r.returncode != 0 or not Path(tmp).exists():
        try:
            if Path(tmp).exists():
                os.remove(tmp)
        except Exception:
            pass
        _err(r, "铺 BGM ")

    os.replace(tmp, str(video_out))
    if progress_cb:
        progress_cb(100, "BGM 已铺好")
    return str(video_out)


# 口腔杂音（咂嘴 / 呼吸声 / 口水音）属于「音频缺陷」，不该靠剪点解决：
# 剪点一旦为了躲它缩小，人声就会被切碎。所以放在后期，只压这一遍。
DENOISE_FILTER = "highpass=f=80,afftdn=nr=10:nf=-30"


def denoise(video_in, video_out):
    """轻度降噪：只处理人声原音轨，画面直接 copy

    - highpass=f=80 去掉 80Hz 以下的低频轰鸣/桌面震动
    - afftdn=nr=10:nf=-30 轻档频域降噪，-30dB 噪声底，够压呼吸声又不糊字

    刻意不写 -ar / -ac：源是 24k 单声道时强行转 48k 立体声会掉 3dB（以前踩过），
    保持源采样率和声道数，音量才不会被这一步偷偷改掉。
    """
    exe = _exe()
    info = probe(video_in)
    if not info.get("has_audio"):
        raise RuntimeError("这段视频没有音轨，不需要降噪")

    tmp = str(Path(video_out).with_suffix(".dn.mp4"))
    Path(video_out).parent.mkdir(parents=True, exist_ok=True)
    cmd = [exe, "-y", "-hide_banner", "-loglevel", "error",
           "-i", str(video_in), "-af", DENOISE_FILTER,
           "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
           "-movflags", "+faststart", tmp]
    r = _run(cmd)
    if r.returncode != 0 or not Path(tmp).exists():
        try:
            if Path(tmp).exists():
                os.remove(tmp)
        except Exception:
            pass
        _err(r, "降噪")
    os.replace(tmp, str(video_out))
    return str(video_out)


def set_volume(video_in, video_out, volume=1.0, normalize=False):
    """单纯调整整条音轨音量（不动画面）"""
    exe = _exe()
    tmp = str(Path(video_out).with_suffix(".vol.mp4"))
    vf = f"volume={max(0.0, min(8.0, float(volume))):.4f}"
    if normalize:
        vf += ",loudnorm=I=-16:TP=-1.5:LRA=11"
    cmd = [exe, "-y", "-hide_banner", "-loglevel", "error",
           "-i", str(video_in), "-af", vf,
           "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", tmp]
    r = _run(cmd)
    if r.returncode != 0 or not Path(tmp).exists():
        try:
            if Path(tmp).exists():
                os.remove(tmp)
        except Exception:
            pass
        _err(r, "调整音量")
    os.replace(tmp, str(video_out))
    return str(video_out)


# ---------------- BGM 音乐库 ----------------

def music_dir():
    """默认音乐库目录：项目下的 music/，不存在就建"""
    from pathlib import Path as _P
    d = _P(__file__).resolve().parent.parent / "music"
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return d


# 文件名里 Windows 不允许的字符（中文要留着，所以不能用 werkzeug 的 secure_filename）
_BAD_CHARS = '<>:"/\\|?*\0'


def clean_name(name):
    """只留文件名本身，去掉路径和非法字符；结果可能是空串"""
    n = Path(str(name or "")).name
    n = "".join(c for c in n if c not in _BAD_CHARS).strip().strip(".")
    return n


def _unique_path(d):
    """同名时自动加 (2)(3)…，不覆盖库里已有的歌"""
    if not d.exists():
        return d
    stem, suf = d.stem, d.suffix
    i = 2
    while True:
        c = d.with_name(f"{stem} ({i}){suf}")
        if not c.exists():
            return c
        i += 1


def _file_md5(p):
    m = hashlib.md5()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            m.update(chunk)
    return m.digest()


def _same_file(a, b):
    """同名文件是不是同一首：大小 + 内容都比

    只比大小不可靠——CBR 编码的两首完全不同的歌，大小可能刚好一样。
    """
    try:
        if a.stat().st_size != b.stat().st_size:
            return False
        return _file_md5(a) == _file_md5(b)
    except OSError:
        return False


# 时长探测要跑一次 ffmpeg，按 (大小, 修改时间) 缓存，列表才不会卡
_META_CACHE = {}


def _audio_meta(p):
    try:
        st = p.stat()
    except OSError:
        return {"duration": 0.0, "size_mb": 0.0}
    key = (str(p), st.st_size, int(st.st_mtime))
    hit = _META_CACHE.get(key)
    if hit is not None:
        return hit
    dur = 0.0
    try:
        dur = float(probe(p).get("duration") or 0.0)
    except Exception:
        dur = 0.0        # 探测失败不影响导入，列表里显示 --:--
    info = {"duration": round(dur, 1), "size_mb": round(st.st_size / 1048576, 2)}
    if len(_META_CACHE) > 500:
        _META_CACHE.clear()
    _META_CACHE[key] = info
    return info


def list_music(folder=None):
    """列出音乐库里的音频文件（带时长，用于试听列表）"""
    d = Path(folder) if folder else music_dir()
    if not d.is_dir():
        return []
    out = []
    for p in sorted(d.iterdir(), key=lambda x: x.name.lower()):
        if p.is_file() and p.suffix.lower() in AUDIO_EXTS:
            try:
                m = _audio_meta(p)
                out.append({
                    "name": p.name,
                    "path": str(p),
                    "size_mb": m["size_mb"],
                    "duration": m["duration"],
                })
            except OSError:
                continue
    return out


def is_audio(path):
    p = Path(str(path))
    return p.is_file() and p.suffix.lower() in AUDIO_EXTS


def music_path(name):
    """把「文件名」解析成音乐库里的真实路径；越界或不存在返回 None

    只接受纯文件名，杜绝 ../../ 这类越界访问。
    """
    n = clean_name(name)
    if not n:
        return None
    root = music_dir().resolve()
    p = (root / n).resolve()
    if p.parent != root or not p.is_file() or p.suffix.lower() not in AUDIO_EXTS:
        return None
    return p


def import_music(paths):
    """把外部音频复制进音乐库

    返回 (added, skipped, errors)：
      added   [{name, path, copied}]  copied=False 表示库里已经有了
      skipped [原文件名]              不是音频
      errors  ["文件: 原因"]
    """
    import shutil as _sh

    d = music_dir()
    added, skipped, errors = [], [], []
    for src in (paths or []):
        p = Path(str(src))
        if not is_audio(p):
            skipped.append(p.name or str(src))
            continue
        try:
            name = clean_name(p.name) or ("music" + p.suffix.lower())
            dst = d / name
            if dst.exists():
                # 同一首再导一次就别存副本了
                if _same_file(dst, p):
                    added.append({"name": dst.name, "path": str(dst), "copied": False})
                    continue
                dst = _unique_path(dst)
            _sh.copy2(str(p), str(dst))
            added.append({"name": dst.name, "path": str(dst), "copied": True})
        except Exception as e:
            errors.append(f"{p.name}: {e}")
    return added, skipped, errors


def save_upload(file_storage, filename=None):
    """保存浏览器上传的音频（拖拽 / 选文件都走这里）"""
    name = clean_name(filename or getattr(file_storage, "filename", ""))
    if not name:
        raise RuntimeError("文件名无效")
    if Path(name).suffix.lower() not in AUDIO_EXTS:
        raise RuntimeError(f"不支持的格式：{Path(name).suffix or '无扩展名'}")

    dst = music_dir() / name
    if dst.exists():
        # 同一个文件又拖了一次：内容一样就复用，别堆一串 (2)(3)
        cur = getattr(file_storage, "stream", None)
        same = False
        if cur is not None:
            try:
                pos = cur.tell()
                cur.seek(0)
                h = hashlib.md5()
                total = 0
                for chunk in iter(lambda: cur.read(1 << 20), b""):
                    h.update(chunk)
                    total += len(chunk)
                cur.seek(pos)
                same = total == dst.stat().st_size and h.digest() == _file_md5(dst)
            except Exception:
                same = False
        if same:
            return dst
        dst = _unique_path(dst)

    file_storage.save(str(dst))
    return dst


def delete_music(name):
    """从音乐库移走（放进 music/.trash，不真删，反悔能找到）"""
    p = music_path(name)
    if not p:
        return None
    trash = music_dir() / ".trash"
    trash.mkdir(parents=True, exist_ok=True)
    dst = _unique_path(trash / p.name)
    try:
        os.replace(str(p), str(dst))      # 同盘改名，最快也最安全
    except OSError:
        try:
            import shutil as _sh

            _sh.move(str(p), str(dst))    # 跨盘/被占用的兜底
        except OSError:
            # Windows 上文件被占用就动不了：最常见是正在试听这首
            raise RuntimeError("这首正在被占用（多半还在试听），先停掉再移走")
    return dst
