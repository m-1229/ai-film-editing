# -*- coding: utf-8 -*-
"""
视频处理核心模块 - 读取视频元信息、抽帧、检测场景切换
"""

import cv2
import numpy as np
from pathlib import Path


class VideoProcessor:
    def __init__(self, video_path):
        self.video_path = str(video_path)
        self.cap = None
        self.fps = 0.0
        self.total_frames = 0
        self.duration = 0.0
        self.width = 0
        self.height = 0

    # ---------------- 生命周期 ----------------
    def load(self):
        """打开视频并读取基础参数"""
        if not Path(self.video_path).exists():
            raise FileNotFoundError(f"文件不存在：{self.video_path}")

        self.cap = cv2.VideoCapture(self.video_path)
        if not self.cap.isOpened():
            self.cap = None
            raise RuntimeError("无法打开视频，可能是格式不支持或缺少解码器")

        fps = self.cap.get(cv2.CAP_PROP_FPS) or 0.0
        self.fps = fps if fps > 0 else 25.0
        self.total_frames = int(self.cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        self.width = int(self.cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
        self.height = int(self.cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
        self.duration = self.total_frames / self.fps if self.fps else 0.0
        return self

    def close(self):
        if self.cap is not None:
            self.cap.release()
            self.cap = None

    def __enter__(self):
        return self.load()

    def __exit__(self, exc_type, exc, tb):
        self.close()

    # ---------------- 信息 ----------------
    def get_duration(self):
        if not self.cap and self.duration == 0.0:
            self.load()
        return self.duration

    def get_resolution(self):
        if not self.cap and self.width == 0:
            self.load()
        return self.width, self.height

    def get_video_info(self):
        """读取基本信息（自动开关文件，不占用句柄）"""
        self.load()
        info = {
            "path": self.video_path,
            "duration": self.duration,
            "fps": self.fps,
            "total_frames": self.total_frames,
            "width": self.width,
            "height": self.height,
        }
        self.close()
        return info

    # ---------------- 取帧 ----------------
    def get_frame(self, timestamp):
        """获取指定时间戳（秒）的帧，BGR 格式"""
        if self.cap is None:
            self.load()
        frame_number = max(0, int(timestamp * self.fps))
        self.cap.set(cv2.CAP_PROP_POS_FRAMES, frame_number)
        ok, frame = self.cap.read()
        return frame if ok else None

    def detect_scene_cuts(self, threshold=30.0, min_gap=0.4):
        """基于帧差检测场景切换点"""
        if self.cap is None:
            self.load()

        self.cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
        cuts = []
        prev_small = None
        last_cut_time = -999.0
        index = 0

        while True:
            ok, frame = self.cap.read()
            if not ok:
                break

            small = cv2.resize(frame, (160, 90))
            small = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)

            if prev_small is not None:
                diff = float(np.mean(cv2.absdiff(prev_small, small)))
                now = index / self.fps
                if diff > threshold and (now - last_cut_time) >= min_gap:
                    cuts.append({
                        "timestamp": now,
                        "frame_number": index,
                        "diff": diff,
                        "confidence": min(diff / (threshold * 2), 1.0),
                    })
                    last_cut_time = now

            prev_small = small
            index += 1

        self.close()
        return cuts

    # 兼容旧接口
    def _calculate_frame_diff(self, frame1, frame2):
        gray1 = cv2.resize(cv2.cvtColor(frame1, cv2.COLOR_BGR2GRAY), (100, 100))
        gray2 = cv2.resize(cv2.cvtColor(frame2, cv2.COLOR_BGR2GRAY), (100, 100))
        return float(np.mean(cv2.absdiff(gray1, gray2)))
