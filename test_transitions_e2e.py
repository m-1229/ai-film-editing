# -*- coding: utf-8 -*-
"""转场的端到端测试 —— 真渲染，走 HTTP 接口，不碰浏览器

`test_pacing.py` 验的是判定规则；这里验的是「设置真的落到了成片上」：
时长对不对、淡入淡出到底有没有烧进去、关掉转场是不是真的回到纯硬切。
`py_compile` 抓不到「参数没接上」这类错，所以新增渲染参数后必须跑这个。

素材是现场用 lavfi 合成的 3 个镜头（彩条 / 黑场 / 彩条），跑完自己清理。
"""

import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from backend import audio_fx, server  # noqa: E402
from utils.ffmpeg_setup import ensure_ffmpeg  # noqa: E402

OK, FAIL = 0, []


def check(name, cond, detail=""):
    global OK
    if cond:
        OK += 1
        print(f"  ✓ {name}")
    else:
        FAIL.append(name)
        print(f"  ✗ {name}   {detail}")


SHOTS = ["testsrc2=duration=4:size=640x360:rate=25",
         "color=c=black:duration=4:size=640x360:rate=25",
         "smptebars=duration=4:size=640x360:rate=25"]


def build(tmp):
    """3 个 4 秒镜头拼成 12 秒素材，带正弦音轨"""
    exe = ensure_ffmpeg()
    parts = []
    for i, src in enumerate(SHOTS):
        p = tmp / f"shot{i}.mp4"
        subprocess.run([exe, "-y", "-loglevel", "error", "-f", "lavfi", "-i", src,
                        "-f", "lavfi", "-i", "sine=frequency=440:duration=4",
                        "-c:v", "libx264", "-crf", "20", "-pix_fmt", "yuv420p",
                        "-c:a", "aac", "-ar", "48000", "-ac", "2", str(p)], check=True)
        parts.append(p)
    lst = tmp / "list.txt"
    lst.write_text("".join(f"file '{p.name}'\n" for p in parts), encoding="utf-8")
    out = tmp / "shots.mp4"
    subprocess.run([exe, "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
                    "-i", str(lst), "-c", "copy", str(out)], check=True)
    return out


def render(client, path, cut_ranges, effects, duration=12.0):
    r = client.post("/api/render", json={
        "path": str(path), "cut_ranges": cut_ranges, "duration": duration,
        "effects": effects,
    })
    j = r.get_json() or {}
    if not j.get("ok"):
        raise RuntimeError(f"接口拒绝：{j}")
    jid = j["job"]
    job = {}
    for _ in range(1200):
        job = (client.get(f"/api/job/{jid}").get_json() or {}).get("job") or {}
        if job.get("status") in ("done", "error", "cancelled"):
            break
        time.sleep(0.25)
    if job.get("status") != "done":
        raise RuntimeError(f"渲染失败：{job.get('error') or job.get('status')}")
    out = job["result"]["output"]
    return out, float(audio_fx.probe(out)["duration"])


def luma_at(path, t):
    import cv2
    cap = cv2.VideoCapture(str(path))
    fps = cap.get(cv2.CAP_PROP_FPS) or 25
    cap.set(cv2.CAP_PROP_POS_FRAMES, max(0, int(t * fps)))
    ok, fr = cap.read()
    cap.release()
    return float(cv2.cvtColor(fr, cv2.COLOR_BGR2GRAY).mean()) if ok else -1


def brightest(path, dur):
    """全片几个采样点里最亮的那一档，用来判断「有没有被淡压暗」

    素材里本来就有黑场镜头，不能拿「成片中间那一帧」当参照。
    """
    pts = [dur * (i + 1) / 8 for i in range(8)]
    return max(luma_at(path, t) for t in pts)


def dimmed(path, t, ref):
    return 0 <= luma_at(path, t) < ref * 0.5


def main():
    global OK
    client = server.app.test_client()
    tmp = Path(tempfile.mkdtemp(prefix="trans_e2e_"))
    made = []
    try:
        src = build(tmp)
        total = float(audio_fx.probe(src)["duration"])
        print(f"素材 {total:.2f}s：彩条 / 黑场 / 彩条，各 4s\n")

        print("== 1. 自动转场 + 首尾淡入淡出（跨镜头，剪掉 4s）==")
        out, d = render(client, src, [[2.5, 6.5]], {"transition": {
            "on": True, "type": "auto", "duration": 0, "fade_in": 0.6, "fade_out": 1.0}})
        made.append(out)
        raw = 2.5 + (total - 6.5)
        check("成片比段总长短（转场吃了重叠）", d < raw - 0.15, (raw, d))
        check("重叠不超过 0.6s", raw - d <= 0.6, raw - d)
        bright = brightest(out, d)
        check("首帧被淡入压暗", dimmed(out, 0.0, bright), (luma_at(out, 0.0), bright))
        check("末帧被淡出压暗", dimmed(out, d - 0.15, bright), (luma_at(out, d - 0.15), bright))

        print("\n== 2. 关掉转场，只要首尾淡入淡出 ==")
        out, d = render(client, src, [[3.0, 3.5]], {"transition": {
            "on": False, "type": "auto", "fade_in": 0.7, "fade_out": 0.9}})
        made.append(out)
        raw = 3.0 + (total - 3.5)
        check("硬切不吃时长", abs(d - raw) <= 0.1, (raw, d))
        bright = brightest(out, d)
        check("淡入仍然生效", dimmed(out, 0.0, bright), (luma_at(out, 0.0), bright))
        check("淡出仍然生效", dimmed(out, d - 0.15, bright), (luma_at(out, d - 0.15), bright))

        print("\n== 3. 面板全关（老行为：纯硬切、不淡）==")
        out, d = render(client, src, [[3.0, 3.5]], {"transition": {
            "on": False, "type": "auto", "fade_in": 0, "fade_out": 0}})
        made.append(out)
        check("时长等于段总长", abs(d - raw) <= 0.1, (raw, d))
        check("首帧没被压暗", luma_at(out, 0.0) > 100, luma_at(out, 0.0))

        print("\n== 4. 手动全片叠化（默认不加首尾淡入淡出）==")
        out, d = render(client, src, [[2.5, 6.5]], {"transition": {
            "on": True, "type": "fade", "duration": 0.5}})
        made.append(out)
        raw = 2.5 + (total - 6.5)
        check("叠化按指定 0.5s 吃时长", abs(raw - d - 0.5) <= 0.15, (raw, d))
        check("首帧没被压暗（手动类型不自动淡入）", luma_at(out, 0.0) > 100,
              luma_at(out, 0.0))

        print("\n== 5. 只有一段时也要能淡入淡出 ==")
        out, d = render(client, src, [[0.0, total - 3.2]], {"transition": {
            "on": False, "type": "auto", "fade_in": 0.8, "fade_out": 0.8}})
        made.append(out)
        bright = brightest(out, d)
        check("单段成片仍加淡入", dimmed(out, 0.0, bright), (luma_at(out, 0.0), bright))
    finally:
        for p in made:
            try:
                Path(p).unlink()
            except Exception:
                pass
        try:
            for p in tmp.rglob("*"):
                if p.is_file():
                    p.unlink()
            tmp.rmdir()
        except Exception:
            pass

    print(f"\n{'=' * 40}\n通过 {OK} 项" + (f"，失败 {len(FAIL)} 项：{FAIL}" if FAIL else "，全部通过"))
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
