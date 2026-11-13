# -*- coding: utf-8 -*-
"""
FFmpeg 定位工具

系统里没有单独安装 ffmpeg，但 imageio-ffmpeg 自带了一份可执行文件。
这里把它找出来并注入 PATH，同时告诉 pydub / yt-dlp 用哪一份。
"""

import os
import shutil
from pathlib import Path

_ffmpeg_exe = None


def find_ffmpeg():
    """返回可用的 ffmpeg 可执行文件路径，找不到返回 None"""
    global _ffmpeg_exe
    if _ffmpeg_exe and Path(_ffmpeg_exe).exists():
        return _ffmpeg_exe

    # 1. 项目自带 bin 目录（优先，名字是标准 ffmpeg.exe）
    local = Path(__file__).resolve().parent.parent / "bin" / "ffmpeg.exe"
    if local.exists():
        _ffmpeg_exe = str(local)
        return _ffmpeg_exe

    # 2. 系统 PATH
    exe = shutil.which("ffmpeg")
    if exe:
        _ffmpeg_exe = exe
        return exe

    # 3. imageio-ffmpeg 自带的二进制
    try:
        import imageio_ffmpeg

        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if exe and Path(exe).exists():
            _ffmpeg_exe = exe
            return exe
    except Exception:
        pass

    return None


def ensure_ffmpeg():
    """把 ffmpeg 目录加入 PATH，并配置 pydub / yt-dlp。返回路径或 None"""
    exe = find_ffmpeg()
    if not exe:
        return None

    bin_dir = str(Path(exe).parent)
    path_parts = os.environ.get("PATH", "").split(os.pathsep)
    if bin_dir not in path_parts:
        os.environ["PATH"] = bin_dir + os.pathsep + os.environ.get("PATH", "")

    # pydub 需要显式的 converter 路径
    try:
        from pydub import AudioSegment

        AudioSegment.converter = exe
    except Exception:
        pass

    return exe


def ffmpeg_dir():
    """返回 ffmpeg 所在目录，供 yt-dlp 的 ffmpeg_location 使用"""
    exe = ensure_ffmpeg()
    return str(Path(exe).parent) if exe else None
