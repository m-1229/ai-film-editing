# -*- coding: utf-8 -*-
"""
AI 智能剪辑工具 - Tkinter 主界面
"""

import os
import sys
import json
import threading
import traceback

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

# 确保能以项目根目录为基准导入 backend / utils
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from backend.video_processor import VideoProcessor
from backend.ai_analyzer import AIAnalyzer


def _fmt_time(seconds):
    """秒 -> mm:ss.s"""
    if seconds is None:
        return "--"
    seconds = float(seconds)
    m, s = divmod(seconds, 60)
    return f"{int(m):02d}:{s:04.1f}"


class AIClipEditor:
    def __init__(self, root):
        self.root = root
        self.root.title("AI 智能剪辑工具 v1.0")
        self.root.geometry("1000x680")
        self.root.minsize(860, 560)

        self.current_video = None
        self.video_info = None
        self.analyzer = None
        self.suggestions = []
        self._analyzing = False

        self.create_widgets()

    # ---------------- 界面 ----------------
    def create_widgets(self):
        menubar = tk.Menu(self.root)
        self.root.config(menu=menubar)

        file_menu = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="文件", menu=file_menu)
        file_menu.add_command(label="打开视频...", command=self.open_video)
        file_menu.add_command(label="导出剪辑建议...", command=self.export_result)
        file_menu.add_separator()
        file_menu.add_command(label="退出", command=self.root.destroy)

        help_menu = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="帮助", menu=help_menu)
        help_menu.add_command(label="关于", command=self.show_about)

        # 顶部：视频信息
        info_frame = ttk.LabelFrame(self.root, text="视频信息", padding=10)
        info_frame.pack(fill=tk.X, padx=12, pady=(10, 6))

        self.video_label = ttk.Label(info_frame, text="未选择视频", font=("Microsoft YaHei UI", 10, "bold"))
        self.video_label.grid(row=0, column=0, columnspan=4, sticky="w", pady=(0, 6))

        self.duration_label = ttk.Label(info_frame, text="时长：--")
        self.duration_label.grid(row=1, column=0, sticky="w", padx=(0, 24))
        self.resolution_label = ttk.Label(info_frame, text="分辨率：--")
        self.resolution_label.grid(row=1, column=1, sticky="w", padx=(0, 24))
        self.fps_label = ttk.Label(info_frame, text="帧率：--")
        self.fps_label.grid(row=1, column=2, sticky="w", padx=(0, 24))

        # 操作区
        btn_frame = ttk.Frame(self.root, padding=(12, 0))
        btn_frame.pack(fill=tk.X, pady=6)

        ttk.Button(btn_frame, text="选择视频", command=self.open_video).pack(side=tk.LEFT, padx=(0, 8))
        self.analyze_btn = ttk.Button(btn_frame, text="开始 AI 分析", command=self.analyze_video)
        self.analyze_btn.pack(side=tk.LEFT, padx=(0, 8))
        self.export_btn = ttk.Button(btn_frame, text="导出建议", command=self.export_result)
        self.export_btn.pack(side=tk.LEFT, padx=(0, 8))

        # 结果区
        result_frame = ttk.LabelFrame(self.root, text="分析结果", padding=8)
        result_frame.pack(fill=tk.BOTH, expand=True, padx=12, pady=(0, 6))

        self.result_text = tk.Text(
            result_frame, height=18, wrap="word",
            font=("Consolas", 10), relief="flat",
            bg="#ffffff", fg="#1f2328", insertbackground="#1f2328",
        )
        scrollbar = ttk.Scrollbar(result_frame, command=self.result_text.yview)
        self.result_text.config(yscrollcommand=scrollbar.set)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.result_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        # 状态栏
        self.status = tk.StringVar(value="就绪")
        status_bar = ttk.Label(self.root, textvariable=self.status, anchor="w", relief="sunken", padding=(8, 4))
        status_bar.pack(fill=tk.X, side=tk.BOTTOM)

        self._log("欢迎使用 AI 智能剪辑工具。")
        self._log("第一步：点击「选择视频」，导入一个本地视频文件。")

    # ---------------- 工具方法 ----------------
    def _log(self, text=""):
        self.result_text.insert(tk.END, text + "\n")
        self.result_text.see(tk.END)

    def _reset_result(self):
        self.result_text.delete("1.0", tk.END)

    def show_about(self):
        messagebox.showinfo(
            "关于",
            "AI 智能剪辑工具 v1.0\n\n"
            "· 场景切换检测\n"
            "· 冗余片段识别\n"
            "· 剪辑建议导出\n\n"
            "后端：OpenCV + MoviePy + FFmpeg",
        )

    # ---------------- 打开视频 ----------------
    def open_video(self):
        filepath = filedialog.askopenfilename(
            title="选择视频文件",
            filetypes=[
                ("视频文件", "*.mp4 *.avi *.mov *.mkv *.flv *.wmv *.webm"),
                ("所有文件", "*.*"),
            ],
        )
        if not filepath:
            return

        try:
            info = VideoProcessor(filepath).get_video_info()
        except Exception as e:
            self._reset_result()
            self._log("加载失败：" + str(e))
            messagebox.showerror("错误", f"无法加载视频：\n{e}")
            return

        self.current_video = filepath
        self.video_info = info
        self.analyzer = None
        self.suggestions = []

        self.video_label.config(text=os.path.basename(filepath))
        self.duration_label.config(text=f"时长：{_fmt_time(info['duration'])}")
        self.resolution_label.config(text=f"分辨率：{info['width']}x{info['height']}")
        self.fps_label.config(text=f"帧率：{info['fps']:.2f}")

        self._reset_result()
        self._log(f"已载入：{filepath}")
        self._log(f"时长 {_fmt_time(info['duration'])} / {info['width']}x{info['height']} / {info['fps']:.2f}fps")
        self._log("")
        self._log("点击「开始 AI 分析」检测场景切换与冗余片段。")
        self.status.set("视频已载入")

    # ---------------- 分析 ----------------
    def analyze_video(self):
        if self._analyzing:
            return
        if not self.current_video:
            messagebox.showwarning("提示", "请先选择视频文件")
            return

        self._analyzing = True
        self.analyze_btn.config(state=tk.DISABLED)
        self._reset_result()
        self._log("正在分析视频，请稍候……（视频越长耗时越久）")
        self.status.set("分析中……")

        threading.Thread(target=self._analyze_worker, daemon=True).start()

    def _analyze_worker(self):
        try:
            analyzer = AIAnalyzer(self.current_video)
            suggestions = analyzer.analyze()
            self.root.after(0, self._on_analyze_done, analyzer, suggestions, None)
        except Exception as e:
            detail = traceback.format_exc()
            self.root.after(0, self._on_analyze_done, None, None, (e, detail))

    def _on_analyze_done(self, analyzer, suggestions, error):
        self._analyzing = False
        self.analyze_btn.config(state=tk.NORMAL)

        if error is not None:
            e, detail = error
            self._reset_result()
            self._log("分析失败：" + str(e))
            self._log("")
            self._log(detail)
            self.status.set("分析失败")
            return

        self.analyzer = analyzer
        self.suggestions = suggestions

        self._reset_result()
        scenes = analyzer.scenes
        silent = analyzer.silent_segments
        redundant = analyzer.redundant_segments

        self._log(f"【场景切换】共 {len(scenes)} 处")
        if scenes:
            for i, sc in enumerate(scenes[:50], 1):
                self._log(f"  {i:>3}. {_fmt_time(sc['start'])}  (第 {sc['frame']} 帧)")
            if len(scenes) > 50:
                self._log(f"  …… 其余 {len(scenes) - 50} 处已省略")
        else:
            self._log("  未检测到明显场景切换（视频可能本身镜头较长）")

        self._log("")
        self._log(f"【冗余片段】共 {len(redundant)} 段")
        for i, seg in enumerate(redundant[:30], 1):
            self._log(
                f"  {i:>3}. {_fmt_time(seg['start'])} → {_fmt_time(seg['end'])}"
                f"   相似度 {seg['similarity']:.2f}"
            )
        if len(redundant) > 30:
            self._log(f"  …… 其余 {len(redundant) - 30} 段已省略")

        if silent:
            self._log("")
            self._log(f"【静音片段】共 {len(silent)} 段")

        self._log("")
        self._log(f"共生成 {len(suggestions)} 条剪辑建议，可点击「导出建议」保存为 JSON。")
        self.status.set(f"分析完成：{len(scenes)} 处切点 / {len(redundant)} 段冗余")

    # ---------------- 导出 ----------------
    def export_result(self):
        if not self.analyzer:
            messagebox.showwarning("提示", "请先执行 AI 分析")
            return

        default_name = os.path.splitext(os.path.basename(self.current_video or "suggestions"))[0]
        filepath = filedialog.asksaveasfilename(
            title="保存剪辑建议",
            defaultextension=".json",
            initialfile=f"{default_name}_建议.json",
            filetypes=[("JSON 文件", "*.json"), ("所有文件", "*.*")],
        )
        if not filepath:
            return

        try:
            if hasattr(self.analyzer, "export_suggestions"):
                self.analyzer.export_suggestions(filepath)
            else:
                with open(filepath, "w", encoding="utf-8") as f:
                    json.dump(self.suggestions, f, ensure_ascii=False, indent=2)
            self.status.set("已导出：" + filepath)
            messagebox.showinfo("完成", f"已保存到：\n{filepath}")
        except Exception as e:
            messagebox.showerror("错误", f"保存失败：\n{e}")


def main():
    # 确保 ffmpeg 可用（moviepy / pydub / yt-dlp 共用）
    try:
        from utils.ffmpeg_setup import ensure_ffmpeg

        ensure_ffmpeg()
    except Exception:
        pass

    root = tk.Tk()
    try:
        # 让 ttk 使用较现代的主题
        style = ttk.Style(root)
        if "vista" in style.theme_names():
            style.theme_use("vista")
    except Exception:
        pass
    AIClipEditor(root)
    root.mainloop()


if __name__ == "__main__":
    main()
