# -*- coding: utf-8 -*-
"""音乐库（导入窗口）回归测试 —— 全在临时目录里跑，不碰真正的 music/"""

import io
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from backend import audio_fx

OK = 0
FAIL = []


def check(name, cond, detail=""):
    global OK
    if cond:
        OK += 1
        print(f"  ✓ {name}")
    else:
        FAIL.append(name)
        print(f"  ✗ {name}   {detail}")


def make_audio(path, freq=440, dur=1.5):
    from utils.ffmpeg_setup import ensure_ffmpeg

    exe = ensure_ffmpeg()
    subprocess.run([exe, "-y", "-loglevel", "error", "-f", "lavfi",
                    "-i", f"sine=frequency={freq}:duration={dur}",
                    "-c:a", "libmp3lame", "-b:a", "64k", str(path)], check=True)
    return path


def names():
    return [f["name"] for f in audio_fx.list_music()]


def run(lib, src, a, b):
    print("== 1. 空库 ==")
    check("一开始是空的", audio_fx.list_music() == [])

    print("\n== 2. 导入 ==")
    added, skipped, errors = audio_fx.import_music([str(a), str(b), str(src / "说明.txt")])
    check("两首都进来了", [x["name"] for x in added] == ["晴天.mp3", "夜曲.mp3"], added)
    check("非音频进 skipped", skipped == ["说明.txt"], skipped)
    check("没有报错", errors == [], errors)
    check("列表里 2 首", len(names()) == 2, names())

    info = audio_fx.list_music()[0]
    check("列表带时长", 1.0 < info["duration"] < 2.5, info)
    check("列表带大小", info["size_mb"] > 0, info)

    print("\n== 3. 重复导入 ==")
    added2, _, _ = audio_fx.import_music([str(a)])
    check("同名的同大小文件不再复制", added2 and added2[0]["copied"] is False, added2)
    check("数量没变", len(names()) == 2, names())

    c = make_audio(src / "晴天.mp3", 660)   # 同名但内容不同
    added3, _, _ = audio_fx.import_music([str(c)])
    check("同名不同内容另存一份", added3 and added3[0]["name"] == "晴天 (2).mp3", added3)
    check("数量变 3", len(names()) == 3, names())

    print("\n== 4. 路径安全 ==")
    check("正常取到", audio_fx.music_path("晴天.mp3") is not None)
    check("越界被挡", audio_fx.music_path("../../backend/server.py") is None)
    check("绝对路径被挡", audio_fx.music_path("C:/Windows/win.ini") is None)
    check("不存在的返回 None", audio_fx.music_path("没这首.mp3") is None)
    check("去掉路径只留文件名", audio_fx.clean_name("C:/a/b/c.mp3") == "c.mp3",
          audio_fx.clean_name("C:/a/b/c.mp3"))
    check("非法字符被清掉", audio_fx.clean_name('a<b>c:"d?.mp3') == "abcd.mp3",
          audio_fx.clean_name('a<b>c:"d?.mp3'))

    print("\n== 5. 上传（拖拽/选文件） ==")
    from werkzeug.datastructures import FileStorage

    fs = FileStorage(stream=io.BytesIO(a.read_bytes()), filename="传进来的歌.mp3")
    dst = audio_fx.save_upload(fs)
    check("中文名保留", dst.name == "传进来的歌.mp3", dst.name)
    check("文件真的落库", dst.exists())
    check("列表变 4 首", len(names()) == 4, names())

    try:
        audio_fx.save_upload(FileStorage(stream=io.BytesIO(b"x"), filename="歌.txt"))
        check("非音频上传被拒", False)
    except RuntimeError:
        check("非音频上传被拒", True)

    print("\n== 6. 删除（进 .trash） ==")
    moved = audio_fx.delete_music("夜曲.mp3")
    check("移到 .trash", moved is not None and moved.exists(), moved)
    check("从库里消失", "夜曲.mp3" not in names(), names())
    check("回收目录里能找到", (lib / ".trash").is_dir()
          and len(list((lib / ".trash").iterdir())) == 1)
    check("删不存在的返回 None", audio_fx.delete_music("没有.mp3") is None)
    check("删不掉的越界名返回 None", audio_fx.delete_music("../server.py") is None)

    print("\n== 7. HTTP 接口 ==")
    from backend.server import app

    cli = app.test_client()

    r = cli.get("/api/bgm/list")
    d = r.get_json()
    check("list 200", r.status_code == 200 and d["ok"], d)
    check("list 带时长", all("duration" in f for f in d["files"]), d["files"])
    check("list 给了目录", bool(d.get("dir")), d.get("dir"))

    r = cli.post("/api/bgm/upload",
                 data={"files": (io.BytesIO(a.read_bytes()), "接口上传.mp3")},
                 content_type="multipart/form-data")
    d = r.get_json()
    check("upload 200", r.status_code == 200 and d["ok"], d)
    check("upload 落库", any(x["name"] == "接口上传.mp3" for x in d["added"]), d)

    r = cli.post("/api/bgm/upload",
                 data={"files": (io.BytesIO(b"x"), "坏.txt")},
                 content_type="multipart/form-data")
    d = r.get_json()
    check("坏格式有报错但接口不炸", d["ok"] and d["errors"], d)

    r = cli.get("/api/bgm/audio", query_string={"name": "晴天.mp3"})
    check("试听 200", r.status_code == 200, r.status_code)
    check("试听 mime 对", r.headers.get("Content-Type") == "audio/mpeg",
          r.headers.get("Content-Type"))
    r.close()

    r = cli.get("/api/bgm/audio", query_string={"name": "晴天.mp3"},
                headers={"Range": "bytes=0-99"})
    check("支持 Range（能拖进度条）", r.status_code == 206, r.status_code)
    r.close()

    r = cli.get("/api/bgm/audio", query_string={"name": "../../backend/server.py"})
    check("越界试听 404", r.status_code == 404, r.status_code)

    r = cli.post("/api/bgm/delete", json={"name": "接口上传.mp3"})
    d = r.get_json()
    check("delete 200", r.status_code == 200 and d["ok"], d)
    check("delete 后不在库", "接口上传.mp3" not in names(), names())

    r = cli.post("/api/bgm/delete", json={"name": "没有这首.mp3"})
    check("删不存在的 404", r.status_code == 404, r.status_code)


def main():
    tmp = Path(tempfile.mkdtemp(prefix="music_test_"))
    lib = tmp / "lib"
    src = tmp / "src"
    lib.mkdir()
    src.mkdir()
    a = make_audio(src / "晴天.mp3", 440)
    b = make_audio(src / "夜曲.mp3", 523)
    (src / "说明.txt").write_text("x", encoding="utf-8")

    orig = audio_fx.music_dir
    audio_fx.music_dir = lambda: lib
    try:
        run(lib, src, a, b)
    finally:
        audio_fx.music_dir = orig
        try:
            shutil.rmtree(tmp, ignore_errors=True)
        except Exception:
            pass

    print(f"\n{'='*40}\n通过 {OK} 项" + (f"，失败 {len(FAIL)} 项：{FAIL}" if FAIL else "，全部通过"))
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
