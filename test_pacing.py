# -*- coding: utf-8 -*-
"""转场编排回归测试 —— 判定规则纯逻辑，最后一段才碰真视频"""

import subprocess
import sys
import tempfile
from pathlib import Path

from backend import pacing

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


def main():
    print("== 1. 转场时长收敛 ==")
    check("长镜头给到期望值", pacing.fit_duration(5, 5, 0.5) == 0.5,
          pacing.fit_duration(5, 5, 0.5))
    check("短镜头压到一半以内", pacing.fit_duration(0.8, 5, 0.5) <= 0.36,
          pacing.fit_duration(0.8, 5, 0.5))
    check("太短直接不给", pacing.fit_duration(0.1, 0.1, 0.5) == 0.0)
    check("零长度不崩", pacing.fit_duration(0, 3, 0.5) == 0.0)

    print("\n== 2. 同一镜头内剪停顿 → 硬切 ==")
    s = pacing.decide_seam(0.05, 0.8, 3.0, 3.0)
    check("画面几乎没变就硬切", s["type"] == "hard" and s["duration"] == 0.0, s)
    s = pacing.decide_seam(0.28, 0.8, 3.0, 3.0)
    check("刚好在阈值内也硬切", s["type"] == "hard", s)

    print("\n== 3. 换了镜头 ==")
    s = pacing.decide_seam(0.6, 0.5, 3.0, 3.0)
    check("换镜头但剪得少 → 短叠化", s["type"] == "fade" and s["duration"] <= 0.35, s)
    s = pacing.decide_seam(0.6, 2.5, 3.0, 3.5)
    check("剪掉一大段 → 转场更明显", s["type"] == "fade" and s["duration"] >= 0.4, s)
    s = pacing.decide_seam(0.9, 4.0, 5.0, 5.0)
    check("段落级切换给最长", s["duration"] >= 0.5, s)
    s = pacing.decide_seam(0.6, 0.5, 0.8, 0.9)
    check("快剪段落 → 硬切更利落", s["type"] == "hard", s)

    print("\n== 4. 拿不到画面时按剪掉的量猜 ==")
    s = pacing.decide_seam(None, 3.0, 3.0, 3.0)
    check("剪掉 3 秒 → 给转场", s["type"] == "fade" and s["duration"] > 0, s)
    s = pacing.decide_seam(None, 0.4, 3.0, 3.0)
    check("只剪了口气 → 硬切", s["type"] == "hard", s)

    print("\n== 5. 手动指定类型 ==")
    s = pacing.decide_seam(0.05, 0.5, 3.0, 3.0, base_type="wipe")
    check("手动指定就按指定的来", s["type"] == "wipe" and s["duration"] > 0, s)
    s = pacing.decide_seam(0.9, 0.5, 3.0, 3.0, base_type="black", base_dur=0.9)
    check("手动指定时长生效", s["type"] == "black" and s["duration"] > 0.4, s)

    print("\n== 6. 分组 ==")
    ov = [0.0, 0.0, 0.3, 0.0, 0.4]
    groups = pacing.group_segments(5, ov)
    check("按接缝切块", groups == [[0, 1], [2, 3], [4]], groups)
    check("全硬切只有一块", pacing.group_segments(4, [0, 0, 0]) == [[0, 1, 2, 3]])
    check("空输入返回空", pacing.group_segments(0, []) == [])

    print("\n== 7. 重叠计算 ==")
    seams = [{"type": "hard", "duration": 0.0},
             {"type": "fade", "duration": 0.5},
             {"type": "fade", "duration": 0.9}]
    lens = [4.0, 4.0, 3.0, 1.0]
    ov = pacing.overlaps_of(4, seams, lens)
    check("硬切没有重叠", ov[1] == 0.0, ov)
    check("普通转场按计划", abs(ov[2] - 0.5) < 0.01, ov)
    check("长转场被后一段长度夹住", abs(ov[3] - 0.45) < 0.01, ov)
    check("列表长度等于段数", len(ov) == 4, ov)

    print("\n== 8. 总结文案 ==")
    check("全硬切", "硬切" in pacing.summarize(
        [{"type": "hard"}, {"type": "hard"}]))
    check("部分转场", "1" in pacing.summarize([{"type": "hard"}, {"type": "fade"}]))
    check("空列表不报错", pacing.summarize([]) == "")
    check("总结带上类型", "叠化" in pacing.summarize(
        [{"type": "fade"}, {"type": "black"}]), pacing.summarize(
        [{"type": "fade"}, {"type": "black"}]))

    print("\n== 8.1 换的是什么，就用哪种转场 ==")
    check("明暗突变 → 黑场",
          pacing.pick_type({"dist": 0.6, "dy": 70, "ds": 3})[0] == "black")
    check("明暗突变（反向也算）→ 黑场",
          pacing.pick_type({"dist": 0.6, "dy": -70, "ds": 3})[0] == "black")
    check("颜色换了一大套 → 溶解",
          pacing.pick_type({"dist": 0.6, "dy": 5, "ds": 40})[0] == "dissolve")
    check("只是换镜头 → 叠化",
          pacing.pick_type({"dist": 0.4, "dy": 5, "ds": 3})[0] == "fade")
    check("没画面信息 → 叠化兜底", pacing.pick_type(None)[0] == "fade")

    s = pacing.decide_seam(0.7, 3.0, 4.0, 4.0, info={"dist": 0.7, "dy": 80, "ds": 2})
    check("过段落 + 明暗突变 → 黑场", s["type"] == "black" and s["duration"] > 0, s)
    check("reason 写明原因", "明暗" in s["reason"], s)
    s = pacing.decide_seam(0.7, 0.5, 3.0, 3.0, info={"dist": 0.7, "dy": 80, "ds": 2})
    check("短接缝黑场太短会闪 → 退回叠化", s["type"] == "fade", s)
    s = pacing.decide_seam(0.7, 0.5, 0.9, 0.9, info={"dist": 0.7, "dy": 80, "ds": 2})
    check("快剪段落类型让位于节奏", s["type"] == "hard", s)
    s = pacing.decide_seam(0.05, 0.5, 3.0, 3.0, info={"dist": 0.05, "dy": 80, "ds": 2})
    check("同镜头剪停顿仍然硬切（明暗差不参与）", s["type"] == "hard", s)

    s = pacing.decide_seam(0.7, 4.0, 4.0, 4.0, info={"dist": 0.7, "dy": 5, "ds": 3}, cap=0.2)
    check("卡点上限生效（≤0.2s）", 0 < s["duration"] <= 0.2, s)
    s = pacing.decide_seam(0.7, 4.0, 4.0, 4.0, info={"dist": 0.7, "dy": 5, "ds": 3})
    check("不传上限时按正常 0.5s", s["duration"] >= 0.5, s)

    print("\n== 8.2 首尾淡入淡出 ==")
    check("auto 默认给 0.4/0.5", pacing.fades_of({"type": "auto"}) == (0.4, 0.5),
          pacing.fades_of({"type": "auto"}))
    check("显式 0 就是不淡",
          pacing.fades_of({"type": "auto", "fade_in": 0, "fade_out": 0}) == (0.0, 0.0))
    check("手动类型默认不淡", pacing.fades_of({"type": "fade"}) == (0.0, 0.0),
          pacing.fades_of({"type": "fade"}))
    check("none 只听显式设置",
          pacing.fades_of({"type": "none", "fade_in": 1.0}) == (1.0, 0.0),
          pacing.fades_of({"type": "none", "fade_in": 1.0}))
    check("不加转场就没淡入淡出", pacing.fades_of(None) == (0.0, 0.0))
    check("淡出不超过成片 1/3", pacing.fades_of({"type": "auto", "fade_out": 2}, 3.0)[1] <= 1.05,
          pacing.fades_of({"type": "auto", "fade_out": 2}, 3.0))
    check("负值当 0", pacing.fades_of({"type": "auto", "fade_in": -1})[0] == 0.0)

    print("\n== 9. 真视频取指纹 ==")
    tmp = Path(tempfile.mkdtemp(prefix="pacing_"))
    try:
        from utils.ffmpeg_setup import ensure_ffmpeg

        exe = ensure_ffmpeg()
        if not exe:
            check("找不到 ffmpeg，跳过", True)
        else:
            a = tmp / "a.mp4"
            b = tmp / "b.mp4"
            subprocess.run([exe, "-y", "-loglevel", "error", "-f", "lavfi",
                            "-i", "testsrc2=duration=1.5:size=320x180:rate=25",
                            "-pix_fmt", "yuv420p", str(a)], check=True)
            subprocess.run([exe, "-y", "-loglevel", "error", "-f", "lavfi",
                            "-i", "smptebars=duration=1.5:size=320x180:rate=25",
                            "-pix_fmt", "yuv420p", str(b)], check=True)

            sa, sb = pacing.part_signature(a), pacing.part_signature(b)
            check("能取到首尾帧指纹", sa is not None and sb is not None)
            if sa and sb:
                same = pacing.distance(sa[0], sa[1])
                diff = pacing.distance(sa[1], sb[0])
                check("同一片段首尾差异小", same < pacing.SCENE_DIST, same)
                check("不同画面差异大", diff > pacing.SCENE_DIST, diff)

            segs = [{"start": 0, "end": 1.5, "speed": 1.0},
                    {"start": 1.5, "end": 3.0, "speed": 1.0}]
            seams = pacing.plan_seams([a, b], segs)
            check("plan_seams 出 1 个接缝", len(seams) == 1, seams)
            check("两个不同画面 → 不是硬切", seams and seams[0]["type"] != "hard", seams)

            check("文件不存在时返回 None", pacing.part_signature(tmp / "nope.mp4") is None)

            # ---- 画面画像 + 类型挑选（真素材） ----
            print("\n== 10. 画面画像与转场类型 ==")
            prof = pacing.part_profile(a)
            check("画像带亮度/饱和度",
                  prof and {"first_y", "last_y", "first_s", "last_s"} <= set(prof), prof)

            dark = tmp / "dark.mp4"
            subprocess.run([exe, "-y", "-loglevel", "error", "-f", "lavfi",
                            "-i", "color=c=black:duration=1.5:size=320x180:rate=25",
                            "-pix_fmt", "yuv420p", str(dark)], check=True)
            pd, pa_ = pacing.part_profile(dark), pacing.part_profile(a)
            info = pacing.seam_info(pd, pa_)
            check("黑场→亮画面：明暗差够大", info and info["dy"] > pacing.LIGHT_JUMP, info)
            check("黑场→亮画面：判黑场过渡",
                  pacing.pick_type(info)[0] == "black", pacing.pick_type(info))
            back = pacing.seam_info(pa_, pd)
            check("亮画面→黑场也判黑场", pacing.pick_type(back)[0] == "black", back)

            seams = pacing.plan_seams([dark, a], [{"start": 0, "end": 1.5, "speed": 1.0},
                                                  {"start": 4.0, "end": 5.5, "speed": 1.0}])
            check("真素材接缝给出黑场", seams and seams[0]["type"] == "black", seams)
            check("reason 说清了为什么", seams and "明暗" in seams[0]["reason"], seams)

            # ---- 硬切路径也要有首尾淡入淡出 ----
            print("\n== 11. 全硬切时的首尾淡入淡出 ==")
            from backend.renderer import _concat_plain

            out = tmp / "faded.mp4"
            _concat_plain(exe, [a, b], out, False, fade_in=0.5, fade_out=0.5, total=3.0)
            check("成片出来了", out.exists())
            if out.exists():
                import cv2

                cap = cv2.VideoCapture(str(out))
                ok1, f1 = cap.read()
                cap.set(cv2.CAP_PROP_POS_FRAMES, 40)
                ok2, f2 = cap.read()
                cap.release()
                y1 = float(cv2.cvtColor(f1, cv2.COLOR_BGR2GRAY).mean()) if ok1 else 0
                y2 = float(cv2.cvtColor(f2, cv2.COLOR_BGR2GRAY).mean()) if ok2 else 0
                check("第一帧明显更暗（淡入生效）", y1 < y2 * 0.6, (y1, y2))
    finally:
        try:
            for p in tmp.rglob("*"):
                if p.is_file():
                    p.unlink()
            tmp.rmdir()
        except Exception:
            pass

    print(f"\n{'='*40}\n通过 {OK} 项" + (f"，失败 {len(FAIL)} 项：{FAIL}" if FAIL else "，全部通过"))
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
