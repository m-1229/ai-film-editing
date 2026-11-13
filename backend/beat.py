# -*- coding: utf-8 -*-
"""
节拍检测 - 给「卡点混剪」提供音乐节拍

不依赖 librosa，纯 numpy 实现：
1) ffmpeg 抽 22.05k 单声道 wav
2) STFT 幅度谱 → 频谱通量（spectral flux）得到起音强度包络
3) 包络自相关估 BPM（60–200），偏好 90–160
4) 相位搜索：找让包络能量最大的网格偏移，生成等间隔节拍点

注意：这里只探测「拍点」，不区分强拍弱拍。对卡点够用。
"""

import os
import subprocess
import tempfile
import wave
from pathlib import Path

import numpy as np

SR = 22050
HOP = 512
N_FFT = 1024
MAX_SECONDS = 1800          # 超过 30 分钟只分析前 30 分钟，避免抽音频太久


def _extract_wav(video_path, sr=SR, max_seconds=MAX_SECONDS):
    """抽成单声道 wav，返回临时文件路径（调用方负责删除）"""
    from utils.ffmpeg_setup import ensure_ffmpeg

    exe = ensure_ffmpeg()
    if not exe:
        raise RuntimeError("找不到 ffmpeg")

    fd, path = tempfile.mkstemp(suffix=".wav", prefix="beat_")
    os.close(fd)
    cmd = [
        exe, "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(video_path),
        "-vn", "-ac", "1", "-ar", str(sr),
        "-t", str(int(max_seconds)),
        path,
    ]
    flags = 0x08000000 if os.name == "nt" else 0
    subprocess.run(cmd, check=True, creationflags=flags,
                   stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    return path


def _read_wav(path):
    with wave.open(path, "rb") as w:
        n = w.getnframes()
        sr = w.getframerate()
        ch = w.getnchannels()
        sw = w.getsampwidth()
        raw = w.readframes(n)
    if sw != 2:
        raise RuntimeError("音频位深不支持（只认 16bit）")
    data = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    if ch > 1:
        data = data.reshape(-1, ch).mean(axis=1)
    return data, sr


def _onset_envelope(y, sr):
    """起音强度包络：每帧一个数，越大越像「一下」"""
    if y.size < N_FFT * 2:
        return np.zeros(0)

    n_frames = 1 + (len(y) - N_FFT) // HOP
    idx = np.arange(N_FFT)[None, :] + HOP * np.arange(n_frames)[:, None]
    frames = y[idx] * np.hanning(N_FFT)[None, :]
    spec = np.abs(np.fft.rfft(frames, axis=1))
    spec = np.log1p(spec * 10.0)                       # 对数压缩，弱起音也能留下
    flux = np.maximum(0.0, np.diff(spec, axis=0)).sum(axis=1)
    env = np.concatenate([[0.0], flux])

    if env.size > 16:                                  # 去掉缓慢起伏，只留突变
        k = 16
        local = np.convolve(env, np.ones(k) / k, mode="same")
        env = np.maximum(0.0, env - local)

    top = float(env.max())
    if top > 0:
        env = env / top
    return env


def _estimate_tempo(env, fps):
    """自相关估 BPM"""
    if env.size < fps * 4:
        return 120.0

    e = env - env.mean()
    min_lag = max(1, int(round(fps * 60.0 / 200.0)))
    max_lag = int(round(fps * 60.0 / 60.0))
    max_lag = min(max_lag, max(1, e.size // 2))
    if max_lag <= min_lag:
        return 120.0

    ac = np.correlate(e, e, mode="full")[e.size - 1:]

    best_score, best_bpm = -1.0, 120.0
    for lag in range(min_lag, max_lag + 1):
        v = float(ac[lag])
        if v <= 0:
            continue
        bpm = 60.0 * fps / lag
        weight = 1.15 if 90.0 <= bpm <= 160.0 else 1.0
        if v * weight > best_score:
            best_score, best_bpm = v * weight, bpm

    while best_bpm < 75.0:
        best_bpm *= 2
    while best_bpm > 180.0:
        best_bpm /= 2
    return float(best_bpm)


def _beat_times(env, fps, bpm, duration):
    """按 BPM 生成节拍网格，相位取包络能量最大处"""
    if bpm <= 0:
        return []
    step = 60.0 / bpm
    if step <= 0 or duration <= 0:
        return []

    n = env.size
    period_frames = step * fps
    if n > 0 and period_frames > 1:
        best_off, best_score = 0, -1.0
        for off in range(int(round(period_frames))):
            idx = np.arange(off, n, period_frames).astype(int)
            if idx.size == 0:
                continue
            score = float(env[idx].mean())
            if score > best_score:
                best_score, best_off = score, off
        start = best_off / fps
    else:
        start = 0.0

    times = []
    t = start
    limit = duration + 1e-6
    while t < limit:
        times.append(round(t, 3))
        t += step
    return times


def detect_beats(video_path, duration=0.0):
    """返回 {ok, bpm, beats[], reason}

    没有音轨 / 几乎静音 / 检测失败都返回 ok=False，调用方要用兜底节奏。
    """
    result = {"ok": False, "bpm": 0.0, "beats": [], "reason": ""}
    tmp = None
    try:
        tmp = _extract_wav(video_path)
        y, sr = _read_wav(tmp)
        if y.size == 0:
            result["reason"] = "音轨是空的"
            return result

        rms = float(np.sqrt(np.mean(y ** 2)))
        if rms < 0.002:
            result["reason"] = "音轨几乎是静音"
            return result

        env = _onset_envelope(y, sr)
        if env.size < 8:
            result["reason"] = "音频太短"
            return result

        fps = sr / float(HOP)
        bpm = _estimate_tempo(env, fps)
        dur = float(duration) or (y.size / float(sr))
        beats = _beat_times(env, fps, bpm, dur)

        if len(beats) < 4:
            result["reason"] = "节拍点太少，判断不出节奏"
            return result

        result.update(ok=True, bpm=round(bpm, 1), beats=beats, reason="")
        return result
    except Exception as e:
        result["reason"] = str(e)
        return result
    finally:
        if tmp:
            try:
                if Path(tmp).exists():
                    os.remove(tmp)
            except Exception:
                pass


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1:
        info = detect_beats(sys.argv[1])
        print(f"ok={info['ok']} bpm={info['bpm']} beats={len(info['beats'])} reason={info['reason']}")
        print(info["beats"][:12])
    else:
        print("用法: python -m backend.beat <视频路径>")
