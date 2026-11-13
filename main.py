#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
AI 智能剪辑工具 - 主程序入口

用法：
    python main.py                # 启动网页版界面（默认，浏览器 App 模式）
    python main.py web [端口]     # 同上，可指定端口
    python main.py gui            # 启动旧版 Tkinter 界面
    python main.py analyze <视频路径>   # 命令行分析
"""

import socket
import sys
import traceback
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

DEFAULT_PORT = 8756


def _port_busy(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.4)
        return s.connect_ex(("127.0.0.1", port)) == 0


def run_web(port=DEFAULT_PORT):
    """启动本地服务 + App 窗口"""
    from backend.server import open_app_window, serve

    if _port_busy(port):
        # 已经在跑了，直接把窗口拉到前面
        print("服务已在运行，打开窗口…")
        open_app_window(f"http://127.0.0.1:{port}/")
        return 0

    serve(port=port, open_browser=True)
    return 0


def run_gui():
    from gui.app import main as gui_main

    gui_main()
    return 0


def run_analyze(video_path):
    from backend.ai_analyzer import AIAnalyzer

    if not Path(video_path).exists():
        print(f"文件不存在：{video_path}")
        return 1

    analyzer = AIAnalyzer(video_path)
    suggestions = analyzer.analyze()
    print()
    print(f"场景切换：{len(analyzer.scenes)} 处")
    print(f"静音片段：{len(analyzer.silent_segments)} 段")
    print(f"冗余片段：{len(analyzer.redundant_segments)} 段")
    print(f"剪辑建议：{len(suggestions)} 条")
    for i, sug in enumerate(suggestions[:20], 1):
        end = sug.get("end")
        end_text = f"{end:.1f}s" if isinstance(end, (int, float)) else "结尾"
        print(f"  {i}. [{sug['type']}] {sug['start']:.1f}s -> {end_text}  {sug.get('reason', '')}")
    return 0


def main():
    try:
        from utils.ffmpeg_setup import ensure_ffmpeg

        ensure_ffmpeg()
    except Exception:
        pass

    args = sys.argv[1:]
    command = args[0].lower() if args else "web"

    if command in ("web", ""):
        port = DEFAULT_PORT
        if len(args) > 1:
            try:
                port = int(args[1])
            except ValueError:
                pass
        return run_web(port)

    if command == "gui":
        return run_gui()

    if command == "analyze":
        if len(args) < 2:
            print("用法：python main.py analyze <视频路径>")
            return 1
        return run_analyze(args[1])

    if command in ("-h", "--help", "help"):
        print(__doc__)
        return 0

    print(f"未知命令：{command}")
    print(__doc__)
    return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
    except Exception:
        traceback.print_exc()
        sys.exit(1)
