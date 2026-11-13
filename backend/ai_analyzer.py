# -*- coding: utf-8 -*-
"""
AI 分析模块 - 粗剪分析
1) 场景切换检测（颜色直方图 + 帧差）
2) 静音片段检测（pydub / ffmpeg）
3) 冗余片段检测（抽样帧相似度）
"""

import json
import os
import sys
from pathlib import Path

import cv2
import numpy as np


def _log(*args):
    """pythonw 下 sys.stdout 为 None，print 会抛异常，这里做保护"""
    if sys.stdout is None:
        return
    try:
        print(*args)
    except Exception:
        pass


class AIAnalyzer:
    """AI 分析模块 - 用于粗剪分析"""

    def __init__(self, video_path, scene_threshold=0.45, sample_step=15):
        self.video_path = str(video_path)
        self.scene_threshold = scene_threshold   # 直方图距离阈值，越大越不敏感
        self.sample_step = sample_step           # 冗余检测的抽样间隔（帧）
        self.scenes = []                         # 场景切换点
        self.silent_segments = []                # 静音片段
        self.redundant_segments = []             # 冗余片段
        self.static_segments = []                # 静止画面（画面几乎不动）
        self.blank_segments = []                 # 黑场 / 白场 / 纯色帧
        self.has_audio = True                    # 是否真的有音轨
        self.fps = 25.0
        self.duration = 0.0
        self.total_frames = 0
        self.progress_cb = None
        self.cancel_flag = None

    class Cancelled(Exception):
        pass

    def _report(self, pct, msg=""):
        if self.progress_cb:
            try:
                self.progress_cb(max(0.0, min(100.0, pct)), msg)
            except Exception:
                pass

    def _check_cancel(self):
        if self.cancel_flag and self.cancel_flag():
            raise AIAnalyzer.Cancelled()

    # ---------------- 主流程 ----------------
    def analyze(self, progress_cb=None, cancel_flag=None, min_silence=None):
        """min_silence: 静音检测的粒度（秒）。

        分析本身只负责「把所有停顿都找出来」，剪多狠由 autocut.decide 的
        strength 决定。所以粗剪那条路传 0.5（紧凑档 0.55 才有东西可判），
        建议页仍用 1.0（短停顿不值得在时间轴上标出来）。
        """
        self.progress_cb = progress_cb
        self.cancel_flag = cancel_flag

        self._report(1, "读取视频信息…")
        self._probe()
        self.detect_scenes()
        self._check_cancel()
        self.detect_silent_segments(
            min_silence=1.0 if min_silence is None else float(min_silence))
        self._check_cancel()
        self.detect_redundant_segments()
        self._drop_static_false_duplicates()
        self._check_cancel()
        self._report(98, "整理建议…")
        suggestions = self.get_suggestions()
        self._report(100, "完成")
        return suggestions

    def _probe(self):
        cap = cv2.VideoCapture(self.video_path)
        if not cap.isOpened():
            raise RuntimeError("无法打开视频")
        fps = cap.get(cv2.CAP_PROP_FPS) or 0.0
        self.fps = fps if fps > 0 else 25.0
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        self.total_frames = total
        self.duration = total / self.fps if total else 0.0
        cap.release()

    # ---------------- 场景 / 静止 / 空白 一次遍历 ----------------
    def detect_scenes(self):
        """一次遍历同时产出三类线索：

        1) 场景切换：HSV 直方图距离，抗运镜抖动
        2) 静止画面：相邻帧平均差极小，说明画面卡住/空镜（粗剪可压掉）
        3) 黑场 / 纯色：整帧灰度方差极低（转场黑屏、淡入淡出、漏帧）
        """
        cap = cv2.VideoCapture(self.video_path)
        prev_hist = None
        prev_tiny = None
        index = 0
        last_cut = -999.0

        # 静止 / 空白片段累计器
        static_run = None      # [start_sec, end_sec]
        blank_run = None       # [start_sec, end_sec, 亮度累计, 帧数]

        def close_static():
            nonlocal static_run
            if static_run and static_run[1] - static_run[0] >= 2.0:
                self.static_segments.append({
                    "start": round(static_run[0], 2),
                    "end": round(static_run[1], 2),
                })
            static_run = None

        def close_blank():
            nonlocal blank_run
            if blank_run and blank_run[1] - blank_run[0] >= 0.25:
                n = max(1, blank_run[3])
                self.blank_segments.append({
                    "start": round(blank_run[0], 2),
                    "end": round(blank_run[1], 2),
                    "mean": round(blank_run[2] / n, 1),   # 用来区分黑场 / 白场 / 彩色纯色
                })
            blank_run = None

        try:
            while True:
                ok, frame = cap.read()
                if not ok:
                    break

                small = cv2.resize(frame, (160, 90))
                now = index / self.fps

                # --- 1. 场景切换 ---
                hsv = cv2.cvtColor(small, cv2.COLOR_BGR2HSV)
                hist = cv2.calcHist([hsv], [0, 1], None, [30, 32], [0, 180, 0, 256])
                cv2.normalize(hist, hist, 0, 1, cv2.NORM_MINMAX)

                if prev_hist is not None:
                    dist = float(cv2.compareHist(prev_hist, hist, cv2.HISTCMP_BHATTACHARYYA))
                    if dist > self.scene_threshold and (now - last_cut) > 0.5:
                        self.scenes.append({
                            "start": round(now, 2),
                            "frame": index,
                            "score": round(dist, 3),
                        })
                        last_cut = now
                prev_hist = hist

                # --- 2 / 3. 静止与纯色（64×36 灰度就够，成本极低）---
                tiny = cv2.resize(
                    cv2.cvtColor(small, cv2.COLOR_BGR2GRAY), (64, 36)
                ).astype(np.float32)
                std = float(tiny.std())

                # 纯色（含黑场白场）：方差接近 0，用 9 容忍压缩噪点
                if std < 9.0:
                    if blank_run is None:
                        blank_run = [now, now, float(tiny.mean()), 1]
                    else:
                        blank_run[1] = now
                        blank_run[2] += float(tiny.mean())
                        blank_run[3] += 1
                else:
                    close_blank()

                # 静止：整帧平均变化极小（1.6 是经验值，能容忍轻微噪点/呼吸）
                if prev_tiny is not None:
                    if float(np.abs(tiny - prev_tiny).mean()) < 1.6:
                        if static_run is None:
                            static_run = [now, now]
                        else:
                            static_run[1] = now
                    else:
                        close_static()
                prev_tiny = tiny

                index += 1
                if index % 10 == 0:
                    self._check_cancel()
                    if self.total_frames:
                        self._report(2 + 53 * min(1.0, index / self.total_frames), "检测画面与镜头…")

            close_blank()
            close_static()
        finally:
            cap.release()

        self._report(55, f"镜头 {len(self.scenes)} 处 · 静止 {len(self.static_segments)} 段")
        _log(f"场景 {len(self.scenes)} / 静止 {len(self.static_segments)} / 空白 {len(self.blank_segments)}")

    def _auto_silence_threshold(self, audio, fallback=-40.0, step_ms=100):
        """按音频电平分布自适应定静音阈值。

        固定 -40dB 在两种真实素材上都会失灵：
        - 手机录音底噪就有 -38dB → 检测不到任何静音
        - 有背景音乐时整片都在 -25dB → 反而会被全判成静音

        做法：取电平的 10 分位当作"安静基线"，阈值抬到它上面 6dB，
        再把结果夹在 -50 ~ -30 之间，两头都不会走极端。
        """
        try:
            levels = []
            n = len(audio)
            for i in range(0, max(1, n - step_ms), step_ms * 5):
                d = audio[i:i + step_ms].dBFS
                if d != d or d < -100:      # NaN / -inf 用 -90 占位，保住它在百分位里的权重
                    d = -90.0
                levels.append(d)
            if len(levels) < 20:
                return fallback

            levels.sort()
            p10 = levels[max(0, int(len(levels) * 0.10) - 1)]
            thresh = p10 + 6.0
            return float(min(-30.0, max(-50.0, thresh)))
        except Exception:
            return fallback

    # ---------------- 静音检测 ----------------
    def detect_silent_segments(self, silence_dbfs=-40.0, min_silence=1.0, chunk_ms=100):
        """先用 ffmpeg 把音轨抽成 wav（避免依赖 ffprobe），再做静音检测"""
        tmp_wav = None
        try:
            import subprocess
            import tempfile

            from utils.ffmpeg_setup import ensure_ffmpeg

            # 必须在导入 pydub 之前把 ffmpeg 加进 PATH，
            # 否则 pydub 导入时找不到 ffmpeg 会打印告警
            exe = ensure_ffmpeg()
            if not exe:
                _log("静音检测跳过：未找到 ffmpeg")
                return

            from pydub import AudioSegment
            from pydub.silence import detect_silence

            self._report(58, "提取音轨…")
            fd, tmp_wav = tempfile.mkstemp(suffix=".wav")
            os.close(fd)

            cmd = [
                exe, "-y", "-hide_banner", "-loglevel", "error",
                "-i", self.video_path,
                "-vn", "-ac", "1", "-ar", "16000",
                tmp_wav,
            ]
            flags = 0x08000000 if os.name == "nt" else 0  # 不弹黑窗口
            subprocess.run(cmd, check=True, creationflags=flags,
                           stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

            audio = AudioSegment.from_wav(tmp_wav)
            self._report(62, "检测静音…")
            silence_dbfs = self._auto_silence_threshold(audio, fallback=silence_dbfs)

            # 全片电平中位数：用来判断"这段是真的安静，还是全片都这个音量"
            try:
                levels = []
                for i in range(0, max(1, len(audio) - 100), 500):
                    d = audio[i:i + 100].dBFS
                    levels.append(-90.0 if (d != d or d < -100) else d)
                levels.sort()
                median_dbfs = levels[len(levels) // 2] if levels else -20.0
            except Exception:
                median_dbfs = -20.0

            ranges = detect_silence(
                audio,
                min_silence_len=int(min_silence * 1000),
                silence_thresh=silence_dbfs,
                seek_step=chunk_ms,
            )
            for start_ms, end_ms in ranges:
                # 只按绝对电平判断会把「全程有背景音乐」的片子在音乐轻的地方剪出水口。
                # 真正的静音必须显著低于全片常态（低 12dB 以上）。
                try:
                    seg_dbfs = audio[start_ms:end_ms].dBFS
                    if seg_dbfs != seg_dbfs or seg_dbfs > median_dbfs - 12:
                        continue
                except Exception:
                    pass

                self.silent_segments.append({
                    "start": round(start_ms / 1000.0, 2),
                    "end": round(end_ms / 1000.0, 2),
                })

            # 全片几乎都是静音 = 大概率根本没有音轨，别拿它当剪辑依据
            silent_total = sum(s["end"] - s["start"] for s in self.silent_segments)
            if self.duration > 1 and silent_total / self.duration > 0.75:
                self.has_audio = False
                _log("音轨几乎全静音，判定为无音轨，跳过静音剪裁")
            self._report(70, f"静音 {len(self.silent_segments)} 段")
            _log(f"检测到 {len(self.silent_segments)} 段静音")
        except Exception as e:
            _log(f"静音检测跳过：{e}")
        finally:
            if tmp_wav and os.path.exists(tmp_wav):
                os.remove(tmp_wav)

    # ---------------- 冗余检测 ----------------
    def detect_redundant_segments(self, similarity_threshold=0.985, min_gap_sec=3.0):
        """抽样帧两两比较，找出内容高度重复但不连续的位置（口误重录、重复镜头）"""
        cap = cv2.VideoCapture(self.video_path)
        samples = []   # (frame_index, 32x32 gray flatten)
        index = 0

        while True:
            ok, frame = cap.read()
            if not ok:
                break
            if index % self.sample_step == 0:
                small = cv2.resize(frame, (32, 32))
                gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY).astype(np.float32)
                samples.append((index, gray))
            index += 1
            if index % 50 == 0:
                self._check_cancel()
                if self.total_frames:
                    self._report(70 + 20 * min(1.0, index / self.total_frames), "抽样比对…")
        cap.release()

        if len(samples) < 2:
            _log("视频过短，跳过冗余检测")
            self._report(92, "跳过多余片段检测")
            return

        arr = np.array([s[1] for s in samples])
        idxs = np.array([s[0] for s in samples])
        # 归一化后计算相关系数矩阵
        flat = arr.reshape(len(arr), -1)
        flat = flat - flat.mean(axis=1, keepdims=True)
        norm = np.linalg.norm(flat, axis=1, keepdims=True)
        norm[norm == 0] = 1e-6
        flat = flat / norm
        corr = flat @ flat.T

        step_sec = self.sample_step / self.fps if self.fps else 0.0
        min_gap_samples = max(1, int(min_gap_sec / step_sec)) if step_sec else 1

        for i in range(len(samples)):
            best_j = -1
            for j in range(i + min_gap_samples, len(samples)):
                if corr[i, j] >= similarity_threshold:
                    best_j = j
                    break
            if best_j > 0:
                seg = {
                    "start": round(idxs[i] / self.fps, 2),
                    "end": round((idxs[i] + self.sample_step) / self.fps, 2),
                    "similar_to": round(idxs[best_j] / self.fps, 2),
                    "similarity": round(float(corr[i, best_j]), 3),
                }
                # 合并相邻的重复结果
                if self.redundant_segments and seg["start"] - self.redundant_segments[-1]["end"] <= step_sec * 1.5:
                    self.redundant_segments[-1]["end"] = seg["end"]
                else:
                    self.redundant_segments.append(seg)

        self._report(92, f"冗余片段 {len(self.redundant_segments)} 段")
        _log(f"检测到 {len(self.redundant_segments)} 个冗余片段")

    def _drop_static_false_duplicates(self, ratio=0.8):
        """静态画面（彩条、PPT、固定机位）相邻帧本来就完全相同，
        不剔除的话会被当成"重复镜头"成片剪掉。"""
        if not self.static_segments or not self.redundant_segments:
            return

        def covered(seg):
            s, e = seg["start"], seg["end"]
            span = max(0.001, e - s)
            ov = 0.0
            for st in self.static_segments:
                ov += max(0.0, min(e, st["end"]) - max(s, st["start"]))
            return ov / span

        before = len(self.redundant_segments)
        self.redundant_segments = [x for x in self.redundant_segments if covered(x) < ratio]
        dropped = before - len(self.redundant_segments)
        if dropped:
            _log(f"剔除 {dropped} 个「静止画面」误判的重复片段")

    # ---------------- 生成建议 ----------------
    def get_suggestions(self):
        suggestions = []

        for i, scene in enumerate(self.scenes):
            if i + 1 < len(self.scenes):
                end = self.scenes[i + 1]["start"]
            else:
                end = round(self.duration, 2) if self.duration else None
            suggestions.append({
                "type": "scene",
                "start": scene["start"],
                "end": end,
                "reason": f"场景 {i + 1} 起点",
            })

        for seg in self.redundant_segments:
            suggestions.append({
                "type": "redundant",
                "start": seg["start"],
                "end": seg["end"],
                "reason": f"与 {seg['similar_to']:.1f}s 处内容重复（相似度 {seg['similarity']:.2f}），可考虑删除",
            })

        for seg in self.silent_segments:
            suggestions.append({
                "type": "silence",
                "start": seg["start"],
                "end": seg["end"],
                "reason": f"静音 {seg['end'] - seg['start']:.1f}s，可加速或删除",
            })

        suggestions.sort(key=lambda s: s["start"])
        return suggestions

    # ---------------- 导出 ----------------
    def export_suggestions(self, output_path):
        data = {
            "video": self.video_path,
            "duration": round(self.duration, 2),
            "scenes": self.scenes,
            "redundant": self.redundant_segments,
            "silent": self.silent_segments,
            "suggestions": self.get_suggestions(),
        }
        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        with open(out, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return str(out)
