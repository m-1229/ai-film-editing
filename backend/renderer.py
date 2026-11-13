# -*- coding: utf-8 -*-
"""
成片渲染 - 按「保留区间」裁剪拼接，输出 mp4
"""

import os
import subprocess
import time
from pathlib import Path


class RenderCancelled(Exception):
    pass


def _merge_ranges(ranges):
    """合并重叠/相邻区间"""
    if not ranges:
        return []
    rs = sorted((float(s), float(e)) for s, e in ranges if e > s)
    out = [list(rs[0])]
    for s, e in rs[1:]:
        if s <= out[-1][1] + 0.02:
            out[-1][1] = max(out[-1][1], e)
        else:
            out.append([s, e])
    return [(s, e) for s, e in out]


def build_keep_ranges(duration, cut_ranges, min_keep=0.15):
    """从总时长里挖掉 cut_ranges，返回保留区间"""
    if duration <= 0:
        return []
    cuts = _merge_ranges([(max(0.0, s), min(duration, e)) for s, e in cut_ranges])
    keep = []
    cursor = 0.0
    for s, e in cuts:
        if s - cursor > min_keep:
            keep.append((cursor, s))
        cursor = max(cursor, e)
    if duration - cursor > min_keep:
        keep.append((cursor, duration))
    return keep


def _mux_full_audio(src_video, out_video, progress_cb=None):
    """把成片的音轨换成「原片从头开始的音轨」

    卡点混剪的画面是碎块拼起来的，如果沿用每块自带的音频，
    音乐会每切一次就断一次。这里改成画面拼接、音轨整条铺。
    """
    from utils.ffmpeg_setup import ensure_ffmpeg

    exe = ensure_ffmpeg()
    if not exe:
        raise RuntimeError("找不到 ffmpeg，无法铺设原音轨")

    tmp = str(out_video) + ".mux.mp4"
    cmd = [
        exe, "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(out_video),
        "-i", str(src_video),
        "-map", "0:v:0", "-map", "1:a:0?",
        "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
        "-shortest", tmp,
    ]
    flags = 0x08000000 if os.name == "nt" else 0
    r = subprocess.run(cmd, capture_output=True, creationflags=flags)
    if r.returncode != 0 or not Path(tmp).exists():
        err = (r.stderr or b"").decode("utf-8", "ignore")[:200]
        try:
            if Path(tmp).exists():
                os.remove(tmp)
        except Exception:
            pass
        raise RuntimeError(f"铺设原音轨失败：{err}")
    os.replace(tmp, str(out_video))


def render_cut(video_path, out_path, keep_ranges, progress_cb=None, cancel_flag=None,
               threads=4, preset="medium", audio_mode="clip",
               speed=1.0, transition=None, bgm=None, subtitle=None,
               frame=None, denoise=False, notes=None):
    """按保留区间渲染成片，progress_cb(percent, message)

    audio_mode:
      - "clip"：每个片段带自己的音频（普通粗剪）
      - "full"：画面按区间拼接，音轨用原片整条（卡点混剪，音乐不断）

    frame: None | "blur" | "crop" | {"mode": "blur"|"crop"}（竖屏 9:16）
    denoise: 轻度降噪（只压人声原音轨，失败不阻断出片）
    notes: 传一个 list 进来，能降级的地方会把原因写进去（调用方拼进 note）

    speed/transition/bgm/subtitle 是新增能力。所有渲染统一走 ffmpeg 管线：
    moviepy 在写音频时会把 24k 单声道转成 44.1k 立体声并掉 3dB 音量，
    老路径只留作 ffmpeg 缺失时的兜底。
    """
    seps = _normalize_ranges(keep_ranges, speed)

    from utils.ffmpeg_setup import ensure_ffmpeg
    if not ensure_ffmpeg():
        if frame or denoise:
            # 兜底路径做不到竖屏/降噪，说清楚，别让用户以为生效了
            _note(notes, "ffmpeg 不可用，竖屏画幅和降噪这次没做")
        return _render_moviepy(video_path, out_path, keep_ranges, progress_cb=progress_cb,
                               cancel_flag=cancel_flag, threads=threads, preset=preset,
                               audio_mode=audio_mode)
    return _render_ffmpeg(video_path, out_path, seps, progress_cb=progress_cb,
                          cancel_flag=cancel_flag, preset=preset, audio_mode=audio_mode,
                          transition=transition, bgm=bgm, subtitle=subtitle,
                          frame=frame, denoise=denoise, notes=notes)


def _note(notes, msg):
    """把一句降级原因记下来（没有收集容器就当没听见，绝不因为记日志而失败）"""
    if not msg:
        return
    try:
        if notes is not None:
            notes.append(msg)
        else:
            print(msg)
    except Exception:
        pass


def _normalize_ranges(keep_ranges, default_speed=1.0):
    """(s,e) 或 {start,end,speed} → 统一字典列表"""
    out = []
    for r in keep_ranges or []:
        if isinstance(r, dict):
            s, e = float(r.get("start")), float(r.get("end"))
            sp = float(r.get("speed") or default_speed or 1.0)
        else:
            s, e = float(r[0]), float(r[1])
            sp = float(default_speed or 1.0)
        if e - s <= 0.05:
            continue
        out.append({"start": s, "end": e, "speed": max(0.1, min(10.0, sp))})
    return out


def _render_moviepy(video_path, out_path, keep_ranges, progress_cb=None, cancel_flag=None,
                    threads=4, preset="medium", audio_mode="clip"):
    from moviepy import VideoFileClip, concatenate_videoclips

    def report(p, msg=""):
        if progress_cb:
            progress_cb(max(0, min(100, p)), msg)

    keep = _merge_ranges(keep_ranges)
    if not keep:
        raise ValueError("没有可保留的片段，请检查剪辑设置")

    keep_audio = (audio_mode != "full")

    report(1, "读取视频…")
    clip = VideoFileClip(video_path)
    parts = []
    final = None
    try:
        total_keep = sum(e - s for s, e in keep)
        report(5, f"准备 {len(keep)} 个片段（共 {total_keep:.1f}s）")

        for i, (s, e) in enumerate(keep):
            if cancel_flag and cancel_flag():
                raise RenderCancelled()
            s = max(0.0, min(s, clip.duration))
            e = max(0.0, min(e, clip.duration))
            if e - s <= 0.05:
                continue
            part = clip.subclipped(s, e)
            if not keep_audio:
                part = part.without_audio()
            parts.append(part)
            report(5 + 20 * (i + 1) / len(keep), f"截取片段 {i + 1}/{len(keep)}")

        if not parts:
            raise ValueError("片段过短，无法生成")

        final = parts[0] if len(parts) == 1 else concatenate_videoclips(parts, method="compose")
        report(28, "开始渲染…")

        # 用 proglog 回调把渲染进度转成百分比
        logger = None
        try:
            from proglog import ProgressBarLogger

            class _Logger(ProgressBarLogger):
                def bars_callback(self, bar, attr, value, old_value=None):
                    if attr != "index":
                        return
                    state = self.bars.get(bar, {})
                    total = state.get("total") or 0
                    if total:
                        pct = 28 + 68 * min(1.0, value / total)
                        report(pct, f"渲染中 {int(min(1.0, value / total) * 100)}%")
                    if cancel_flag and cancel_flag():
                        raise RenderCancelled()

            logger = _Logger()
        except Exception:
            logger = None

        out_path = str(out_path)
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        final.write_videofile(
            out_path,
            codec="libx264",
            audio_codec="aac",
            audio=keep_audio,
            preset=preset,
            threads=threads,
            logger=logger,
            temp_audiofile=None,
        )
        if not keep_audio:
            report(96, "铺上原音轨…")
            if cancel_flag and cancel_flag():
                raise RenderCancelled()
            _mux_full_audio(video_path, out_path)
        report(99, "收尾…")
        report(100, "完成")
        return out_path
    finally:
        try:
            if final is not None and final is not parts[0]:
                final.close()
        except Exception:
            pass
        for p in parts:
            try:
                p.close()
            except Exception:
                pass
        try:
            clip.close()
        except Exception:
            pass


# ---------------- ffmpeg 管线（变速 / 转场 / BGM / 字幕） ----------------

# 转场类型：用户词 → xfade 的 transition
TRANSITIONS = {
    "fade": "fade",
    "dissolve": "dissolve",
    "black": "fadeblack",
    "white": "fadewhite",
    "wipe": "wipeleft",
    "slide": "slideleft",
    "circle": "circleopen",
    "pixel": "pixelize",
    "smooth": "smoothleft",
}
TRANSITION_LABEL = {
    "fade": "叠化", "dissolve": "溶解", "black": "黑场过渡", "white": "白场过渡",
    "wipe": "擦除", "slide": "滑动", "circle": "圆形展开", "pixel": "像素化", "smooth": "平滑推移",
}


def _xff(exe, args, what, cancel_flag=None, cwd=None):
    """跑一条 ffmpeg，失败时带原始报错"""
    import subprocess as sp
    flags = 0x08000000 if os.name == "nt" else 0
    r = sp.run([exe] + args, capture_output=True, creationflags=flags, cwd=cwd)
    if cancel_flag and cancel_flag():
        raise RenderCancelled()
    if r.returncode != 0:
        err = (r.stderr or b"").decode("utf-8", "ignore").strip()[-500:]
        raise RuntimeError(f"{what}失败：{err}")
    return r


# ---------------- 竖屏画幅（口播 9:16） ----------------

# 画幅这一步专用的 x264 preset：
# 20s/1080p 竖屏实测（同一条 testsrc2+noise 素材，只跑画幅这一步）
#   medium   crop 16.3s / blur 10.1s
#   veryfast crop  6.5s / blur  4.0s
# 输出分辨率已经定死 1080x1920，-crf 20 下两者肉眼无差别，所以这里选快的。
# 注意：只有「换画布」这一步用它，主链路的切段/拼接/字幕还是走调用方给的 preset。
#
# ⚠️ 画质余量只有 2dB：验收判据是「整帧 SSIM ≥0.99 且字幕区 PSNR ≥38dB」，
# veryfast 实测 SSIM 0.9970 / PSNR 40.02dB。**改 crf、改 preset、或把字幕字号调小
# （笔画更细）都必须重跑这组指标**——最先崩的一定是字幕边缘，不是整帧。
FRAME_PRESET = "veryfast"
FRAME_W, FRAME_H = 1080, 1920
FRAME_MODES = ("blur", "crop")
# 已经是竖屏的源：宽高比和 9:16 差不到 2%，且高度够，就别再重编一遍
VERTICAL_RATIO = 0.5625


def normalize_frame(frame):
    """画幅配置 → None | "blur" | "crop"（不认识的当没传，老调用方零影响）"""
    if not frame:
        return None
    if isinstance(frame, str):
        mode = frame.strip().lower()
    elif isinstance(frame, dict):
        mode = str(frame.get("mode") or "").strip().lower()
    else:
        return None
    if mode in ("", "origin", "none", "off", "source"):
        return None
    return mode if mode in FRAME_MODES else None


def is_vertical(info):
    """源本身已经是竖屏？（宽高比 ≈ 9:16 且高 ≥ 1920）"""
    try:
        w = float((info or {}).get("width") or 0)
        h = float((info or {}).get("height") or 0)
    except (TypeError, ValueError):
        return False
    if w <= 0 or h <= 0:
        return False
    return abs(w / h - VERTICAL_RATIO) < 0.02 and h >= 1920


def frame_filter(mode, ass_name=None):
    """竖屏画幅的滤镜图 → (filter_complex, 输出标签)

    输入固定是 [0:v]（单输入）。
      blur：背景放大填满 + 高斯模糊，前景等比放进中间，叠起来
      crop：先居中裁成 9:16（1920×1080 → 607×1080），再等比放大到 1080×1920
            （顺序是「先裁再缩」：反过来的写法要先放出 3413×1920 的中间帧，
             峰值内存大 5.6 倍，可见区还多挨一次插值。实测纯滤镜耗时几乎一样
             （2.44s vs 2.40s/20s），真正的耗时大头是 x264 编码，见 FRAME_PRESET）

    ass_name 给了就顺手把字幕烧进**同一次编码**（少一次重编、少一次画质损失）。
    """
    label = "[vfit]"
    if mode == "blur":
        fc = (
            "[0:v]split=2[bg][fg];"
            f"[bg]scale={FRAME_W}:{FRAME_H}:force_original_aspect_ratio=increase,"
            f"crop={FRAME_W}:{FRAME_H},gblur=sigma=28[bgz];"
            f"[fg]scale={FRAME_W}:{FRAME_H}:force_original_aspect_ratio=decrease[fgz];"
            f"[bgz][fgz]overlay=(W-w)/2:(H-h)/2{label}"
        )
    elif mode == "crop":
        # 先把源居中裁到 9:16（横的裁宽、竖的裁高），再等比放大到 1080×1920。
        # 后面的 scale+crop 是兜底：源宽高比不是精确 16:9 时差的那几个像素在这里对齐。
        fc = ("[0:v]crop=w='min(iw,ih*9/16)':h='min(ih,iw*16/9)':"
              "x='(iw-ow)/2':y='(ih-oh)/2',"
              f"scale={FRAME_W}:{FRAME_H}:force_original_aspect_ratio=increase,"
              f"crop={FRAME_W}:{FRAME_H}{label}")
    else:
        return "", "[0:v]"

    if ass_name:
        return fc + f";{label}subtitles={ass_name}:fontsdir=.[vo]", "[vo]"
    return fc, label


def _apply_frame(exe, src, dst, mode, preset=None, cancel_flag=None,
                 ass_name=None, work=None):
    """把一段片子转成竖屏（可选同一次编码里烧字幕）

    音轨 -c:a copy：时长/帧率/音轨一个字节都不许变。
    ass_name 只给文件名，靠 cwd=work 解析（Windows 路径里的冒号在滤镜里是转义地狱）。

    默认用 FRAME_PRESET（veryfast）而不是跟主链路走 medium：这一步只是「换画布」，
    分辨率已经定死，慢一档压缩率换不回来多少体积，但耗时差 2.5 倍（见 FRAME_PRESET）。
    """
    fc, vlabel = frame_filter(mode, ass_name)
    args = ["-y", "-hide_banner", "-loglevel", "error",
            "-i", str(src),
            "-filter_complex", fc,
            "-map", vlabel, "-map", "0:a?",
            "-c:v", "libx264", "-crf", "20", "-preset", preset or FRAME_PRESET,
            "-pix_fmt", "yuv420p",
            "-c:a", "copy",
            "-movflags", "+faststart", str(dst)]
    _xff(exe, args, "转竖屏", cancel_flag, cwd=(str(work) if work else None))


def _slice_segment(exe, src, dst, start, dur, speed, has_audio, fps, preset, cancel_flag=None):
    """切一段出来，顺带变速（画面 setpts + 音频 atempo，音调不变）

    注意 -t 必须放在 -i 之后：放在前面是「限制读取时长」，遇到关键帧稀疏的
    素材（比如拼接出来的、长 GOP 的录屏），-ss 只能落到前一个关键帧上，
    读不满就凑出一段超长画面——实测取 0.9s 能切出 1.84s。
    """
    args = ["-y", "-hide_banner", "-loglevel", "error",
            "-ss", f"{start:.3f}", "-i", str(src), "-t", f"{dur:.3f}"]

    vf = []
    if abs(speed - 1.0) > 1e-6:
        vf.append(f"setpts=PTS/{speed:.6f}")
    # 统一帧率，xfade / concat 都要求参数一致
    if fps:
        vf.append(f"fps={fps:g}")
    if vf:
        args += ["-vf", ",".join(vf)]

    if has_audio:
        from backend.audio_fx import atempo_chain
        af = atempo_chain(speed)
        if af:
            args += ["-af", af]
        # 不指定 -ar/-ac：源是 24kHz 单声道时强行转 48k 立体声会掉 3dB
        args += ["-c:a", "aac", "-b:a", "192k"]
    else:
        args += ["-an"]

    args += ["-c:v", "libx264", "-crf", "16", "-preset", "veryfast",
             "-pix_fmt", "yuv420p", "-video_track_timescale", "90000",
             str(dst)]
    _xff(exe, args, "切片段", cancel_flag)


def _audio_fmt(info):
    """把源音频格式固定下来，防止 concat/xfade 偷偷重采样掉音量"""
    return f"aformat=sample_rates={info.get('ar') or 48000}:" \
           f"channel_layouts={info.get('ch') or 'stereo'}"


def _fade_filter(fin, fout, total):
    """首尾淡入淡出的滤镜串（只做画面，不动音轨）"""
    vf = []
    if fin and fin > 0.05:
        vf.append(f"fade=t=in:st=0:d={fin:.2f}")
    if fout and fout > 0.05 and total > fout * 1.5:
        vf.append(f"fade=t=out:st={total - fout:.2f}:d={fout:.2f}")
    return ",".join(vf)


# 硬切剪口的音频微淡：每段头尾各 6ms。
# 剪辑点直接切断波形会留一个「啵」声（人声尤其明显），6ms 淡入淡出听不出来，
# 但足以把断点抹平。转场接缝本来就有 acrossfade，不重复加。
CUT_AFADE = 0.006


def _cut_afade(dur, fade=None):
    """一段素材进拼接前的音频微淡滤镜串（接在 aformat 后面）

    fade 默认取模块级 CUT_AFADE（不要写成默认参数——那会在定义时就绑死，
    测试和临时关闭微淡都改不动）。
    """
    fade = CUT_AFADE if fade is None else float(fade)
    if fade <= 0:
        return ""
    try:
        d = float(dur or 0)
    except (TypeError, ValueError):
        return ""
    if d <= fade * 2 + 0.01:
        # 段太短（比两倍淡入还短），硬加会把整段静音掉
        return ""
    return (f",afade=t=in:st=0:d={fade:.3f}"
            f",afade=t=out:st={d - fade:.3f}:d={fade:.3f}")


def _concat_plain(exe, parts, out, has_audio, crf=18, preset="medium",
                  cancel_flag=None, afmt=None, fade_in=0.0, fade_out=0.0, total=0.0,
                  lengths=None):
    """硬切拼接（可带首尾淡入淡出）

    淡入淡出必须在这一步做掉：以前只在「有转场」那条路上加，
    结果自动模式判断成「这次全是硬切」时，片头片尾的淡入淡出就丢了。
    """
    args = ["-y", "-hide_banner", "-loglevel", "error"]
    for p in parts:
        args += ["-i", str(p)]
    n = len(parts)
    vlab = "[vraw]"
    if has_audio:
        def _ain(i):
            base = afmt or "anull"
            dur = lengths[i] if (lengths and i < len(lengths)) else 0.0
            return f"[{i}:a]{base}{_cut_afade(dur)}[a{i}]"

        pre = ";".join(_ain(i) for i in range(n))
        fc = pre + ";" + "".join(f"[{i}:v][a{i}]" for i in range(n)) + \
            f"concat=n={n}:v=1:a=1[vraw][a]"
    else:
        fc = "".join(f"[{i}:v]" for i in range(n)) + f"concat=n={n}:v=1:a=0[vraw]"

    vf = _fade_filter(fade_in, fade_out, total)
    if vf:
        fc += f";{vlab}{vf}[vfaded]"
        vlab = "[vfaded]"
    args += ["-filter_complex", fc, "-map", vlab]
    if has_audio:
        args += ["-map", "[a]"]
    args += ["-c:v", "libx264", "-crf", str(crf), "-preset", preset,
             "-pix_fmt", "yuv420p", "-video_track_timescale", "90000",
             "-movflags", "+faststart", str(out)]
    _xff(exe, args, "拼接", cancel_flag)


def _fade_only(exe, src, dst, fade_in, fade_out, total, crf=18, preset="medium",
               cancel_flag=None):
    """单段成片只加首尾淡入淡出（音轨直接 copy，不重编码）"""
    import shutil as _sh

    vf = _fade_filter(fade_in, fade_out, total)
    if not vf:
        _sh.copy2(src, dst)
        return
    args = ["-y", "-hide_banner", "-loglevel", "error", "-i", str(src),
            "-vf", vf, "-c:v", "libx264", "-crf", str(crf), "-preset", preset,
            "-pix_fmt", "yuv420p", "-c:a", "copy",
            "-movflags", "+faststart", str(dst)]
    _xff(exe, args, "淡入淡出", cancel_flag)


def _concat_xfade(exe, parts, lengths, out, has_audio, kind="fade", dur=0.5,
                  crf=18, preset="medium", cancel_flag=None, afmt=None):
    """带转场拼接：画面 xfade，音频 acrossfade

    offset 的算法和 subtitle.render_timeline 完全一致，
    否则字幕会和画面错位——这两个地方必须同步改。
    """
    n = len(parts)
    d = max(0.05, float(dur))
    # 每段重叠不能超过较短那段的一半
    d = min(d, min(lengths) * 0.5)
    trans = TRANSITIONS.get(kind, "fade")

    args = ["-y", "-hide_banner", "-loglevel", "error"]
    for p in parts:
        args += ["-i", str(p)]

    fc = []
    if has_audio and afmt:
        for i in range(n):
            fc.append(f"[{i}:a]{afmt}[af{i}]")

    prev = "[0:v]"
    acc = 0.0
    for i in range(1, n):
        acc += lengths[i - 1] - d
        lbl = f"[v{i}]" if i < n - 1 else "[vout]"
        fc.append(f"{prev}[{i}:v]xfade=transition={trans}:duration={d:.3f}:"
                  f"offset={acc:.3f}{lbl}")
        prev = lbl

    maps = ["-map", "[vout]"]
    if has_audio:
        aprev = "[af0]" if afmt else "[0:a]"
        for i in range(1, n):
            lbl = f"[a{i}]" if i < n - 1 else "[aout]"
            src = f"[af{i}]" if afmt else f"[{i}:a]"
            fc.append(f"{aprev}{src}acrossfade=d={d:.3f}:c1=tri:c2=tri{lbl}")
            aprev = lbl
        maps += ["-map", "[aout]"]

    args += ["-filter_complex", ";".join(fc)] + maps
    args += ["-c:v", "libx264", "-crf", str(crf), "-preset", preset,
             "-pix_fmt", "yuv420p", "-video_track_timescale", "90000",
             "-movflags", "+faststart", str(out)]
    _xff(exe, args, "转场拼接", cancel_flag)


def _concat_mixed(exe, parts, lengths, seams, out, has_audio, crf=18, preset="medium",
                  cancel_flag=None, afmt=None, fade_in=0.0, fade_out=0.0, work=None):
    """混合拼接：组内硬切、组间转场

    为什么分两遍：concat 滤镜出来的流帧率标记是 0/1，直接喂给 xfade 会报
    "inputs needs to be a constant frame rate"。所以先把「连续硬切的组」各自
    concat 成一个文件（帧率信息正常落进容器），再用 xfade 把组串起来。
    全硬切或全转场时只有一遍，不会多编码。

    返回 (成片时长, 每个接缝的实际重叠秒数)。
    """
    from backend import pacing

    n = len(parts)
    ov = pacing.overlaps_of(n, seams, lengths)
    groups = pacing.group_segments(n, ov)
    if work is None:
        work = Path(out).parent / "_mix_tmp"
        work.mkdir(parents=True, exist_ok=True)

    # ---- 第一遍：组内硬切合成一块 ----
    group_files, group_lens = [], []
    for gi, g in enumerate(groups):
        if len(g) == 1:
            group_files.append(parts[g[0]])
            group_lens.append(lengths[g[0]])
            continue
        piece = work / f"grp{gi}.mp4"
        _concat_plain(exe, [parts[k] for k in g], piece, has_audio, crf=crf,
                      preset=preset, cancel_flag=cancel_flag, afmt=afmt,
                      lengths=[lengths[k] for k in g])
        if not piece.exists():
            raise RuntimeError(f"第 {gi + 1} 组拼接失败")
        group_files.append(piece)
        group_lens.append(sum(lengths[k] for k in g))

    # 拼出来的块长度会有几十毫秒的帧对齐误差，xfade 的 offset 必须用真实长度，
    # 否则 offset+duration 超过前一块时长，ffmpeg 直接报 Invalid argument
    from backend.audio_fx import probe as _probe
    real_lens = []
    for i, f in enumerate(group_files):
        try:
            d = float(_probe(f).get("duration") or 0)
        except Exception:
            d = 0.0
        real_lens.append(d if d > 0.05 else group_lens[i])

    # 接缝 → 组间重叠，按实际长度重新收敛
    durs = []
    for gi in range(1, len(groups)):
        seg_i = groups[gi][0]
        d = ov[seg_i]
        d = min(d, real_lens[gi - 1] * 0.45, real_lens[gi] * 0.45)
        d = max(0.0, round(d, 3))
        ov[seg_i] = d
        durs.append(d)

    # ---- 第二遍：组间转场 ----
    m = len(group_files)
    total = sum(real_lens) - sum(durs)

    if m == 1:
        _fade_only(exe, group_files[0], out, fade_in, fade_out, total,
                   crf=crf, preset=preset, cancel_flag=cancel_flag)
        return round(total, 3), ov

    args = ["-y", "-hide_banner", "-loglevel", "error"]
    for p in group_files:
        args += ["-i", str(p)]

    fc = []
    if has_audio and afmt:
        for i in range(m):
            fc.append(f"[{i}:a]{afmt}[af{i}]")

    prev = "[0:v]"
    cur_end = real_lens[0]
    for i in range(1, m):
        d = durs[i - 1]
        # 余量只留 1ms：留多了每个转场都少叠一点，字幕会跟着一路偏
        offset = max(0.0, cur_end - d - 0.001)
        kind = seams[groups[i][0] - 1].get("type") or "fade"
        trans = TRANSITIONS.get(kind, "fade")
        last = i == m - 1
        lbl = "[vout]" if last else f"[vx{i}]"
        fc.append(f"{prev}[{i}:v]xfade=transition={trans}:duration={d:.3f}:"
                  f"offset={offset:.3f}{lbl}")
        cur_end = offset + real_lens[i]
        prev = lbl

    if has_audio:
        aprev = "[af0]" if afmt else "[0:a]"
        for i in range(1, m):
            d = float(durs[i - 1])
            last = i == m - 1
            lbl = "[aout]" if last else f"[ax{i}]"
            src = f"[af{i}]" if afmt else f"[{i}:a]"
            fc.append(f"{aprev}{src}acrossfade=d={d:.3f}:c1=tri:c2=tri{lbl}")
            aprev = lbl

    # 首尾淡入淡出（只做画面）
    final_v = prev
    vf = _fade_filter(fade_in, fade_out, total)
    if vf:
        fc.append(f"{final_v}{vf}[vfin]")
        final_v = "[vfin]"

    args += ["-filter_complex", ";".join(fc), "-map", final_v]
    if has_audio:
        args += ["-map", "[aout]"]
    args += ["-c:v", "libx264", "-crf", str(crf), "-preset", preset,
             "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(out)]
    _xff(exe, args, "转场拼接", cancel_flag)
    return round(total, 3), ov


def _render_ffmpeg(video_path, out_path, segs, progress_cb=None, cancel_flag=None,
                   preset="medium", audio_mode="clip", transition=None, bgm=None,
                   subtitle=None, frame=None, denoise=False, notes=None):
    """新管线：切片 → 拼接（可带转场）→ 降噪 → 换 BGM → 转竖屏 → 烧字幕

    每一步都独立可测，出错能定位到具体环节，不会糊成一句「渲染失败」。
    """
    import shutil
    import tempfile

    from backend import audio_fx
    from backend import pacing
    from backend import subtitle as sub

    def report(p, msg=""):
        if progress_cb:
            progress_cb(max(0, min(100, p)), msg or "")

    def bail():
        if cancel_flag and cancel_flag():
            raise RenderCancelled()

    if not segs:
        raise ValueError("没有可保留的片段，请检查剪辑设置")

    from utils.ffmpeg_setup import ensure_ffmpeg
    exe = ensure_ffmpeg()
    if not exe:
        raise RuntimeError("找不到 ffmpeg")

    info = audio_fx.probe(video_path)
    src_audio = bool(info["has_audio"])
    fps = info["fps"] or 25.0
    # 卡点那种「整条音轨铺」的模式跟变速冲突，变速时退回每段自带音频
    any_speed = any(abs(s["speed"] - 1.0) > 1e-6 for s in segs)
    keep_seg_audio = src_audio and (audio_mode != "full" or any_speed)
    mux_full = src_audio and audio_mode == "full" and not any_speed
    afmt = _audio_fmt(info) if keep_seg_audio else None

    out_path = Path(out_path).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="cut_", dir=str(out_path.parent)))
    parts, lengths = [], []

    try:
        # ---- 1. 切片 ----
        n = len(segs)
        report(2, f"准备 {n} 个片段…")
        for i, s in enumerate(segs):
            bail()
            dst = work / f"p{i:04d}.mp4"
            dur = s["end"] - s["start"]
            _slice_segment(exe, video_path, dst, s["start"], dur, s["speed"],
                           keep_seg_audio, fps, preset, cancel_flag)
            if not dst.exists():
                raise RuntimeError(f"第 {i + 1} 段没切出来")
            parts.append(dst)
            lengths.append(dur / s["speed"])
            report(2 + 44 * (i + 1) / n, f"切片段 {i + 1}/{n}")

        bail()
        # ---- 2. 拼接 ----
        merged = work / "merged.mp4"
        t = transition or {}
        seams, ovs, total_out = [], [0.0] * n, sum(lengths)
        # 首尾淡入淡出：auto 默认给，手动类型默认不给；显式传了 0 就是不要
        fade_in, fade_out = pacing.fades_of(t, total_out)

        if n == 1:
            if fade_in > 0.05 or fade_out > 0.05:
                _fade_only(exe, parts[0], merged, fade_in, fade_out, total_out,
                           preset=preset, cancel_flag=cancel_flag)
                report(56, "只有一段，加了首尾淡入淡出")
            else:
                shutil.copy2(parts[0], merged)
                report(56, "只有一段，不用拼接")
        else:
            kind = str(t.get("type") or "auto") if t else ""
            auto = bool(t) and kind in ("auto", "")
            hard_only = bool(t) and kind == "none"
            if not t:
                seams = [{"type": "hard", "duration": 0.0}] * (n - 1)
            elif auto:
                report(46, "看一眼前后画面，安排转场…")
                # 卡点成片叠转场会吃掉时长，音乐跟画面越走越偏，压到 0.2s 以内
                seams = pacing.plan_seams(
                    parts, segs, cap=(pacing.BEAT_CAP if audio_mode == "full" else None))
            elif hard_only:
                # 只要首尾淡入淡出：接缝全硬切
                seams = [{"type": "hard", "duration": 0.0}] * (n - 1)
            else:
                seams = [{"type": kind, "duration": float(t.get("duration") or 0.5)}] * (n - 1)

            ovs = pacing.overlaps_of(n, seams, lengths)
            if any(o > 0 for o in ovs):
                n_fade = sum(1 for o in ovs if o > 0)
                report(52, f"拼接片段（{n_fade} 处转场，其余硬切）…")
                total_out, ovs = _concat_mixed(
                    exe, parts, lengths, seams, merged, keep_seg_audio,
                    preset=preset, cancel_flag=cancel_flag, afmt=afmt,
                    fade_in=fade_in, fade_out=fade_out, work=work)
            else:
                if auto:
                    report(52, "这次都是硬切，不加转场…")
                else:
                    report(52, "拼接片段…")
                _concat_plain(exe, parts, merged, keep_seg_audio, preset=preset,
                              cancel_flag=cancel_flag, afmt=afmt,
                              fade_in=fade_in, fade_out=fade_out, total=total_out,
                              lengths=lengths)
                seams = [{"type": "hard", "duration": 0.0}] * (n - 1)
        report(72, "画面拼接完成")

        # ---- 3. 整条原音轨（卡点模式） ----
        cur = merged
        if mux_full:
            bail()
            report(74, "铺原音轨…")
            _mux_full_audio(video_path, merged)
            cur = merged

        # ---- 3.5 轻度降噪（只压人声原音轨，必须在混 BGM 之前）----
        # 呼吸声 / 咂嘴走后期，绝不靠剪点躲：剪点一缩小人声就碎了。
        if denoise:
            bail()
            if not src_audio:
                _note(notes, "这段视频没有音轨，降噪跳过了")
            else:
                report(75, "压制口腔杂音…")
                dn_out = work / "denoised.mp4"
                try:
                    audio_fx.denoise(str(cur), str(dn_out))
                    cur = dn_out
                    report(75, "口腔杂音已压制")
                except Exception as e:
                    # 降噪失败不能拦着出片，写清楚原因让用户知道这一版没降
                    _note(notes, f"降噪没成功（{str(e)[:60]}），这一版没降噪")

        # ---- 4. 换 BGM ----
        if bgm and bgm.get("path"):
            bail()
            report(76, "混入 BGM…")
            bg_out = work / "bgm.mp4"
            audio_fx.mix_bgm(
                str(cur), str(bg_out), bgm["path"],
                bgm_volume=bgm.get("volume", 0.24),
                keep_original=bgm.get("keep_original", True),
                original_volume=bgm.get("original_volume", 1.0),
                fade_in=bgm.get("fade_in", 1.5),
                fade_out=bgm.get("fade_out", 2.0),
                duck=bgm.get("duck", False),
                duration=total_out,
            )
            cur = bg_out
            report(84, "BGM 已铺好")

        # ---- 5. 字幕：先映射时间、写 ASS（画幅和字幕要一起决定 PlayRes）----
        cues_in = []
        if subtitle and (subtitle.get("cues") or subtitle.get("srt_path")):
            cues_in = subtitle.get("cues") or []
            if not cues_in and subtitle.get("srt_path"):
                cues_in = sub.parse_srt(
                    Path(subtitle["srt_path"]).read_text(encoding="utf-8-sig",
                                                         errors="ignore"))
        mapped = []
        if cues_in:
            bail()
            report(86, "准备字幕…")
            eff_trans = ovs if any(o > 0 for o in ovs) else 0.0
            mapped = sub.map_cues(cues_in, segs, default_speed=1.0, transition=eff_trans)

        # ---- 6. 竖屏画幅 ----
        frame_mode = normalize_frame(frame)
        need_frame = bool(frame_mode)
        if need_frame and is_vertical(info):
            # 源本身就是 9:16：跳过变换，绝不为了「统一尺寸」白重编一遍
            need_frame = False
            report(85, "源片已经是竖屏，跳过画幅变换")
        out_w = FRAME_W if need_frame else (info["width"] or 1920)
        out_h = FRAME_H if need_frame else (info["height"] or 1080)

        ass_path = None
        if mapped:
            ass_path = work / "sub.ass"
            # PlayRes 必须是**输出**尺寸：竖屏写成源尺寸的话字会缩成一小坨
            sub.write_ass(mapped, ass_path, out_w, out_h, subtitle.get("style"))

        burned = False
        if need_frame:
            bail()
            report(87, "转竖屏…")
            framed = work / "framed.mp4"
            if ass_path is not None:
                # 优先：画幅 + 字幕合进同一次编码
                try:
                    _apply_frame(exe, cur, framed, frame_mode, cancel_flag=cancel_flag,
                                 ass_name=ass_path.name, work=work)
                    burned = True
                except RuntimeError as e:
                    # filtergraph 标签踩坑时退回两遍编码：正确性优先
                    _note(notes, f"竖屏和字幕合并编码失败（{str(e)[:80]}），改成分两次编码")
            if not burned:
                _apply_frame(exe, cur, framed, frame_mode, cancel_flag=cancel_flag)
            cur = framed
            report(98 if burned else 90,
                   f"竖屏+字幕已压制（{len(mapped)} 条）" if burned else "竖屏画幅已就绪")

        # ---- 7. 烧字幕 ----
        if ass_path is not None and not burned:
            bail()
            report(92, "压制字幕…")
            sub_out = work / "subbed.mp4"
            sub.burn(str(cur), str(sub_out), ass_path, progress_cb=None,
                     cancel_flag=cancel_flag, preset=preset)
            cur = sub_out
            report(98, f"字幕已烧录（{len(mapped)} 条）")
        elif cues_in and not mapped:
            report(98, "字幕都落在剪掉的片段里了，跳过")

        bail()
        report(99, "收尾…")
        if cur == merged:
            shutil.copy2(cur, out_path)
        else:
            shutil.move(str(cur), str(out_path))
        report(100, "完成")
        return str(out_path)
    finally:
        shutil.rmtree(work, ignore_errors=True)
