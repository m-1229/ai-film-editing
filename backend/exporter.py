# -*- coding: utf-8 -*-
"""
粗剪结果导出

除了成片 mp4，还产出一份「精调包」：

    <名字>_精调包_时间/
        <名字>_粗剪.mp4        成片，直接能发
        timeline.edl           剪辑时间线，PR / DaVinci / FCP 可直接导入
        剪辑清单.csv            每段从哪到哪、AI 为什么这么剪
        clips/clip_01.mp4 ...   仅保留片段，逐段拖进剪映精调

clips 用 ffmpeg stream copy 切，不重编码，快且无损。
"""

import csv
import os
import subprocess
from pathlib import Path


def _flags():
    return 0x08000000 if os.name == "nt" else 0


def _ffmpeg():
    from utils.ffmpeg_setup import ensure_ffmpeg

    return ensure_ffmpeg()


def frames_to_tc(sec, fps):
    """秒 → HH:MM:SS:FF"""
    fps = max(1, int(round(fps or 25)))
    total = int(round(max(0.0, sec) * fps))
    f = total % fps
    s = total // fps
    return f"{s // 3600:02d}:{(s % 3600) // 60:02d}:{s % 60:02d}:{f:02d}"


def write_edl(out_path, video_path, keep_ranges, fps=25.0):
    """CMX3600 EDL，多数专业剪辑软件都能导"""
    name = Path(video_path).name
    lines = ["TITLE: AI_ROUGH_CUT", "FCM: NON-DROP FRAME", ""]
    record_start = 0.0
    for i, (s, e) in enumerate(keep_ranges, 1):
        dur = e - s
        lines.append(
            f"{i:03d}  AX       V     C        "
            f"{frames_to_tc(s, fps)} {frames_to_tc(e, fps)} "
            f"{frames_to_tc(record_start, fps)} {frames_to_tc(record_start + dur, fps)}"
        )
        lines.append(f"* FROM CLIP NAME: {name}")
        record_start += dur
    Path(out_path).write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_csv(out_path, keep_ranges, decisions, fps=25.0):
    with open(out_path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["序号", "源起点", "源终点", "时长(秒)", "起点时间码", "终点时间码"])
        for i, (s, e) in enumerate(keep_ranges, 1):
            w.writerow([i, f"{s:.2f}", f"{e:.2f}", f"{e - s:.2f}",
                        frames_to_tc(s, fps), frames_to_tc(e, fps)])
        w.writerow([])
        w.writerow(["AI 剪辑决策", "起点(秒)", "终点(秒)", "剪掉(秒)", "原因"])
        for d in decisions:
            w.writerow([d["type"], d["start"], d["end"], d["saved"], d["reason"]])


def _media_duration(path, exe):
    """用 ffmpeg 自己解析时长（cv2 对 stream-copy 出来的文件常读不准）"""
    import re

    try:
        r = subprocess.run([exe, "-hide_banner", "-i", str(path)],
                           capture_output=True, text=True, errors="ignore",
                           creationflags=_flags(), timeout=60)
        m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", r.stderr or "")
        if m:
            h, mi, s = m.groups()
            return int(h) * 3600 + int(mi) * 60 + float(s)
    except Exception:
        pass
    return 0.0


def export_clips(video_path, clips_dir, keep_ranges, progress_cb=None):
    """逐段快速切出素材（stream copy，不重编码）

    注意两个坑：
    - 必须用 -ss(前) + -t(后)；-to 在快速 seek 下会被当成相对时长
    - 不能加 -avoid_negative_ts，否则 copy 出来的片子时间戳会乱、时长翻几倍
    """
    exe = _ffmpeg()
    if not exe:
        return []
    clips_dir = Path(clips_dir)
    clips_dir.mkdir(parents=True, exist_ok=True)
    made = []
    total = max(1, len(keep_ranges))

    def encode(src, out, s, span):
        subprocess.run([
            exe, "-y", "-hide_banner", "-loglevel", "error",
            "-ss", f"{s:.3f}", "-i", str(src),
            "-t", f"{span:.3f}",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-c:a", "aac", str(out),
        ], capture_output=True, creationflags=_flags(), timeout=900)

    for i, (s, e) in enumerate(keep_ranges, 1):
        out = clips_dir / f"clip_{i:02d}.mp4"
        span = max(0.05, e - s)
        try:
            subprocess.run([
                exe, "-y", "-hide_banner", "-loglevel", "error",
                "-ss", f"{s:.3f}", "-i", str(video_path),
                "-t", f"{span:.3f}", "-c", "copy", str(out),
            ], capture_output=True, creationflags=_flags(), timeout=600)

            ok = out.exists() and out.stat().st_size > 1024
            if ok:
                # copy 会退到前一个关键帧，GOP 长的时候偏差可能很大
                actual = _media_duration(out, exe)
                if actual and abs(actual - span) > 0.6:
                    encode(video_path, out, s, span)
            else:
                encode(video_path, out, s, span)

            if out.exists() and out.stat().st_size > 1024:
                made.append(str(out))
        except Exception:
            continue
        if progress_cb:
            progress_cb(i / total)
    return made


def export_kit(video_path, analysis, autocut_result, out_dir, want_clips=True,
               progress_cb=None, cues=None, speed=1.0, transition=0.0):
    """产出完整精调包，返回清单

    cues 是「原片时间」的字幕条目；给了就按这次的剪辑结果映射成成片时间，
    额外落一份 字幕.srt，拿到剪映里能直接当字幕轨道用。
    """

    def report(p, msg):
        if progress_cb:
            progress_cb(p, msg)

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    fps = float(analysis.get("fps") or 25.0)
    keep = [tuple(x) for x in autocut_result["keep_ranges"]]
    decisions = autocut_result.get("decisions") or []

    report(5, "写入时间线…")
    edl = out_dir / "timeline.edl"
    write_edl(edl, video_path, keep, fps)

    csv_path = out_dir / "剪辑清单.csv"
    write_csv(csv_path, keep, decisions, fps)

    files = {"edl": str(edl), "csv": str(csv_path), "clips": []}

    if want_clips and keep:
        report(12, f"切出 {len(keep)} 段素材…")

        def cp(ratio):
            report(12 + 60 * ratio, f"切素材 {int(ratio * 100)}%")

        files["clips"] = export_clips(video_path, out_dir / "clips", keep, progress_cb=cp)

    # 字幕：原片时间 → 成片时间（变速要折算、转场重叠要扣掉，全交给 subtitle.map_cues）
    # 放在切素材之后：进度是单向递增的，先报 74% 再回头报切素材会让进度条卡住
    if cues:
        report(74, "写入字幕…")
        try:
            from backend import subtitle

            try:
                default_speed = float(speed or 1.0)
            except (TypeError, ValueError):
                default_speed = 1.0
            keep_sp = [{"start": float(s), "end": float(e), "speed": default_speed}
                       for s, e in keep]
            mapped = subtitle.map_cues(cues, keep_sp,
                                       default_speed=default_speed,
                                       transition=transition)
        except Exception:
            mapped = []      # 字幕是加分项，做不出来不能拖累整包
        if mapped:
            srt = out_dir / "字幕.srt"
            srt.write_text(subtitle.to_srt(mapped), encoding="utf-8")
            files["srt"] = str(srt)

    report(75, "精调包已就绪")
    return files
