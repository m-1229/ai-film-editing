#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
一键环境安装器 —— 建虚拟环境、装依赖、校验 FFmpeg、可选预下载语音模型。

用法：
    python setup_env.py                  安装或修复（可重复执行）
    python setup_env.py --download-model 顺带预下载语音模型（约 464MB）
    python setup_env.py --check          启动前自检
    python setup_env.py --force          强制重建虚拟环境

本文件只用标准库，系统自带的任何 Python 3.8+ 都能跑起来。
"""

import argparse
import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV_DIR = ROOT / ".venv"
REQUIREMENTS = ROOT / "requirements.txt"
MODEL_DIR = ROOT / "models" / "whisper"
MODEL_SIZE = "small"
MODEL_MB = 464

MIN_VERSION = (3, 10)
PREFERRED = [(3, 11), (3, 12), (3, 10), (3, 13)]
MIRROR = "https://pypi.tuna.tsinghua.edu.cn/simple"

IS_WINDOWS = os.name == "nt"

# 启动自检要看的包（import 名）
REQUIRED_MODULES = ["flask", "numpy", "cv2", "pydub", "requests", "faster_whisper", "imageio_ffmpeg"]


def say(msg=""):
    try:
        print(msg, flush=True)
    except UnicodeEncodeError:  # 极端控制台编码下退化成 ASCII，不让安装本身挂掉
        print(msg.encode("ascii", "replace").decode("ascii"), flush=True)


def title(idx, text):
    say("")
    say("[%d/5] %s" % (idx, text))


# ------------------------------------------------------------------
#  进程工具
# ------------------------------------------------------------------

def run_stream(cmd, timeout=None):
    """直接继承控制台输出，用于 pip 这类需要看进度的长任务"""
    try:
        return subprocess.run([str(c) for c in cmd], timeout=timeout).returncode
    except subprocess.TimeoutExpired:
        return 124
    except Exception as exc:
        say("  执行失败：%s" % exc)
        return -1


def capture(cmd, timeout=60):
    """静默执行，返回 (returncode, 合并后的输出)"""
    try:
        proc = subprocess.run(
            [str(c) for c in cmd],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
        )
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, "timeout"
    except Exception as exc:
        return -1, str(exc)


def venv_python():
    if IS_WINDOWS:
        return VENV_DIR / "Scripts" / "python.exe"
    return VENV_DIR / "bin" / "python"


def python_version(cmd):
    """返回 (major, minor)，跑不起来返回 None"""
    code, out = capture(list(cmd) + ["-c", "import sys; print('%d.%d' % sys.version_info[:2])"], timeout=30)
    if code != 0:
        return None
    for line in reversed(out.strip().splitlines()):
        text = line.strip()
        parts = text.split(".")
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            return int(parts[0]), int(parts[1])
    return None


# ------------------------------------------------------------------
#  挑选一个合适的 Python 解释器
# ------------------------------------------------------------------

def find_base_python():
    cands = []
    if sys.version_info[:2] >= MIN_VERSION:
        cands.append([sys.executable])

    if IS_WINDOWS:
        launcher = shutil.which("py")
        if launcher:
            for minor in (11, 12, 10, 13):
                cands.append([launcher, "-3.%d" % minor])
        # 官方安装包的默认位置：刚装完 PATH 还没刷新生效时也能命中
        local = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Python"
        for folder in ("Python311", "Python312", "Python310", "Python313"):
            exe = local / folder / "python.exe"
            if exe.exists():
                cands.append([str(exe)])
        for exe in ("python.exe", "python3.exe"):
            found = shutil.which(exe)
            if found:
                cands.append([found])
    else:
        for name in ("python3.11", "python3.12", "python3.10", "python3", "python"):
            exe = shutil.which(name)
            if exe:
                cands.append([exe])

    best = None
    best_score = 99
    seen = set()
    for cmd in cands:
        key = tuple(cmd)
        if key in seen:
            continue
        seen.add(key)
        ver = python_version(cmd)
        if not ver or ver < MIN_VERSION:
            continue
        score = PREFERRED.index(ver) if ver in PREFERRED else 50
        if score < best_score:
            best, best_score, best_ver = cmd, score, ver
    return best


# ------------------------------------------------------------------
#  安装步骤
# ------------------------------------------------------------------

def ensure_venv(force):
    vpy = venv_python()
    if vpy.exists() and not force:
        ver = python_version([vpy])
        if ver and ver >= MIN_VERSION:
            say("  复用已有虚拟环境：Python %d.%d" % ver)
            return vpy
        say("  已有虚拟环境无法运行，准备重建。")
        shutil.rmtree(VENV_DIR, ignore_errors=True)
    elif force and VENV_DIR.exists():
        say("  按 --force 要求重建虚拟环境。")
        shutil.rmtree(VENV_DIR, ignore_errors=True)

    base = find_base_python()
    if not base:
        say("  没找到 Python 3.10 或更高版本。")
        return None

    ver = python_version(base)
    say("  使用 Python %d.%d 创建虚拟环境 .venv" % ver)
    if ver >= (3, 13):
        say("  注意：3.13 尚未充分验证，若依赖装不上请改用 3.11。")

    code, out = capture(list(base) + ["-m", "venv", str(VENV_DIR)], timeout=180)
    if code != 0:
        say("  创建虚拟环境失败：")
        say(out.strip()[-800:])
        return None
    return venv_python()


def install_deps(vpy):
    say("  升级 pip（首次可能要等一会儿）")
    run_stream([vpy, "-m", "pip", "install", "--upgrade", "pip", "setuptools", "wheel", "-q"], timeout=600)

    say("  安装依赖，约 500MB，视网速需要几分钟")
    say("")
    code = run_stream([vpy, "-m", "pip", "install", "-r", str(REQUIREMENTS)], timeout=3600)

    if code != 0:
        say("")
        say("  默认源装不下来，改用清华镜像重试一次。")
        code = run_stream(
            [vpy, "-m", "pip", "install", "-r", str(REQUIREMENTS), "-i", MIRROR, "--timeout", "60"],
            timeout=3600,
        )
    if code != 0:
        return False

    # 装完真导一遍，确认 moviepy 是 2.x（代码用的是 2.x 的 API）
    check = (
        "import sys\n"
        "bad = []\n"
        "for name in %r:\n"
        "    try:\n"
        "        __import__(name)\n"
        "    except Exception as exc:\n"
        "        bad.append('%%s: %%s' %% (name, exc))\n"
        "try:\n"
        "    from moviepy import VideoFileClip\n"
        "except Exception as exc:\n"
        "    bad.append('moviepy: %%s' %% exc)\n"
        "if bad:\n"
        "    print('FAIL ' + ' | '.join(bad))\n"
        "    sys.exit(1)\n"
        "print('OK')\n" % (REQUIRED_MODULES,)
    )
    code, out = capture([vpy, "-c", check], timeout=180)
    if code != 0:
        say("  依赖装完了但导入失败：")
        say(out.strip()[-800:])
        return False
    return True


def check_ffmpeg(vpy):
    probe = (
        "import sys\n"
        "sys.path.insert(0, %r)\n"
        "from utils.ffmpeg_setup import find_ffmpeg\n"
        "exe = find_ffmpeg()\n"
        "print(exe or '')\n" % (str(ROOT),)
    )
    code, out = capture([vpy, "-c", probe], timeout=60)
    exe = out.strip().splitlines()[-1].strip() if code == 0 and out.strip() else ""
    if not exe or not Path(exe).exists():
        return None

    code, ver = capture([exe, "-version"], timeout=30)
    if code != 0:
        return None
    first = ver.strip().splitlines()[0] if ver.strip() else ""
    return exe, first


def download_model(vpy):
    script = (
        "import os, sys\n"
        "if not os.environ.get('HF_ENDPOINT'):\n"
        "    os.environ['HF_ENDPOINT'] = 'https://hf-mirror.com'\n"
        "root, size = sys.argv[1], sys.argv[2]\n"
        "from faster_whisper import WhisperModel\n"
        "print('[setup] downloading %s model ...' % size, flush=True)\n"
        "WhisperModel(size, device='cpu', compute_type='int8',\n"
        "             download_root=os.path.join(root, 'models', 'whisper'))\n"
        "print('[setup] model ready')\n"
    )
    code = run_stream([vpy, "-c", script, str(ROOT), MODEL_SIZE], timeout=7200)
    return code == 0


def model_ready():
    if not MODEL_DIR.exists():
        return False
    for folder in MODEL_DIR.glob("models--*whisper-%s" % MODEL_SIZE):
        if folder.is_dir() and any(folder.rglob("model.bin")):
            return True
    return False


# ------------------------------------------------------------------
#  子命令
# ------------------------------------------------------------------

def cmd_install(args):
    say("=" * 60)
    say("  AI 剪辑工具 —— 环境安装")
    say("=" * 60)
    say("  项目目录：%s" % ROOT)

    if not REQUIREMENTS.exists():
        say("")
        say("  缺少 requirements.txt，请在项目根目录运行本脚本。")
        return 1

    title(1, "准备 Python 解释器")
    vpy = ensure_venv(args.force)
    if not vpy:
        say("")
        say("  安装中止。请先安装 Python 3.11：https://www.python.org/downloads/")
        return 1

    title(2, "安装 Python 依赖")
    if not install_deps(vpy):
        say("")
        say("  依赖安装失败，安装中止。")
        return 1
    say("")
    say("  依赖安装完成。")

    title(3, "校验 FFmpeg")
    result = check_ffmpeg(vpy)
    if not result:
        say("  没找到可用的 FFmpeg，渲染会失败。")
        say("  可把 ffmpeg.exe 放进项目 bin 目录，或安装到系统 PATH。")
        return 1
    exe, version = result
    say("  %s" % exe)
    if version:
        say("  %s" % version)

    title(4, "准备语音模型")
    if model_ready():
        say("  已下载，无需重复下载。")
    elif args.download_model:
        if download_model(vpy):
            say("  模型下载完成，字幕功能可离线使用。")
        else:
            say("  模型下载失败，不影响启动；首次使用字幕时程序会再试一次。")
    else:
        say("  未预下载。字幕、口头禅功能首次使用时会自动下载（约 %dMB），" % MODEL_MB)
        say("  也可以之后单独执行：python setup_env.py --download-model")

    title(5, "启动自检")
    # 必须在虚拟环境里跑：否则查到的是系统 Python 的包，结果不作数
    code = run_stream([vpy, str(ROOT / "setup_env.py"), "--check"])
    if code != 0:
        return 1

    say("")
    say("=" * 60)
    say("  安装完成")
    say("=" * 60)
    say("  Windows：双击 start.bat 启动")
    say("  macOS / Linux：bash start.sh")
    say("")
    say("  首次剪片时若用到字幕，模型会自动下载，属正常现象。")
    return 0


def cmd_check(_args):
    ok = True
    vpy = venv_python()

    if not vpy.exists():
        say("  [缺少] 虚拟环境，请先运行 install.bat")
        return 2

    ver = python_version([vpy])
    if not ver:
        say("  [损坏] 虚拟环境无法运行，请运行 install.bat 修复")
        return 2
    say("  [正常] Python %d.%d" % ver)

    missing = []
    for name in REQUIRED_MODULES:
        try:
            if importlib.util.find_spec(name) is None:
                missing.append(name)
        except Exception:
            missing.append(name)

    try:
        from importlib.metadata import version as pkg_version

        moviepy_ver = pkg_version("moviepy")
        if int(moviepy_ver.split(".")[0]) < 2:
            missing.append("moviepy>=2（当前 %s）" % moviepy_ver)
    except Exception:
        missing.append("moviepy")

    if missing:
        ok = False
        say("  [缺少] 依赖：%s" % "、".join(missing))
        say("         请运行 install.bat 修复")
    else:
        say("  [正常] Python 依赖齐全")

    sys.path.insert(0, str(ROOT))
    try:
        from utils.ffmpeg_setup import find_ffmpeg

        ffmpeg = find_ffmpeg()
    except Exception:
        ffmpeg = None

    if ffmpeg:
        say("  [正常] FFmpeg：%s" % ffmpeg)
    else:
        ok = False
        say("  [缺少] FFmpeg 不可用，请运行 install.bat 修复")

    if model_ready():
        say("  [正常] 语音模型已就绪")
    else:
        say("  [提示] 语音模型未下载，首次用字幕时会自动下载 %dMB" % MODEL_MB)

    return 0 if ok else 3


# ------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="AI 剪辑工具环境安装器",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--check", action="store_true", help="启动前自检")
    parser.add_argument("--force", action="store_true", help="强制重建虚拟环境")
    parser.add_argument("--download-model", action="store_true", help="预下载语音模型")
    args = parser.parse_args()

    try:
        if args.check:
            return cmd_check(args)
        return cmd_install(args)
    except KeyboardInterrupt:
        say("")
        say("已取消。")
        return 130


if __name__ == "__main__":
    sys.exit(main())
