# -*- coding: utf-8 -*-
"""需求解析 / 方案生成的回归测试（不碰视频，纯逻辑）"""

import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from backend.director import build_plan, describe, parse_demand_rules  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
        print(f"  [OK]   {name}")
    else:
        FAIL.append(f"{name} :: {detail}")
        print(f"  [FAIL] {name}  {detail}")


def analysis(dur=120.0):
    """造一份典型素材：有大量停顿、少量黑场和重复，够用来压时长"""
    return {
        "duration": dur,
        "fps": 25.0,
        "has_audio": True,
        "scenes": [{"start": 20.0, "score": 0.6}, {"start": 50.0, "score": 0.7},
                   {"start": 90.0, "score": 0.5}],
        "silent": [
            {"start": 4.0, "end": 12.0},
            {"start": 20.0, "end": 28.0},
            {"start": 32.0, "end": 40.0},
            {"start": 48.0, "end": 56.0},
            {"start": 60.0, "end": 68.0},
            {"start": 72.0, "end": 80.0},
            {"start": 88.0, "end": 96.0},
            {"start": 104.0, "end": 112.0},
        ],
        "redundant": [{"start": 44.0, "end": 48.0, "similarity": 0.9, "similar_to": 20.0}],
        "static": [{"start": 84.0, "end": 88.0}],
        "blank": [{"start": 0.0, "end": 0.7, "mean": 3.0},
                  {"start": 114.0, "end": 116.0, "mean": 2.0}],
    }


def total_after(plan):
    return sum(e - s for s, e in plan["keep_ranges"])


def main():
    an = analysis()

    print("\n== 1. 力度 + 类型开关 ==")
    it = parse_demand_rules("紧凑一点，别剪静音")
    check("力度=紧凑", it["strength"] == "tight", it["strength"])
    check("排除静音", "silence" not in it["allow"], it["allow"])
    plan = build_plan(an, it)
    check("方案里没有静音决策", all(d["type"] != "silence" for d in plan["decisions"]),
          [d["type"] for d in plan["decisions"]])
    check("仍然剪了别的东西", plan["saved"] > 0, plan["saved"])

    print("\n== 2. 目标时长 ==")
    it = parse_demand_rules("控制在1分30秒")
    check("目标=90s", it["target"] == 90.0, it["target"])
    plan = build_plan(an, it)
    check("剪太狠会放回内容（≥85s）", plan["after"] >= 85, plan["after"])
    check("没超目标", plan["after"] <= 120, plan["after"])

    it = parse_demand_rules("压到30秒")
    check("目标=30s", it["target"] == 30.0, it["target"])
    plan = build_plan(an, it)
    check("压不到就明确告知", "没达到" in (plan.get("target_note") or "")
          or plan["after"] <= 30.5, (plan["after"], plan.get("target_note")))
    check("给了说明", bool(plan.get("target_note")), plan.get("target_note"))

    print("\n== 3. 精确时间 ==")
    it = parse_demand_rules("开头3秒不要")
    check("开头剪掉", any(c.get("start") == 0 and c.get("end") == 3 for c in it["cuts"]), it["cuts"])
    plan = build_plan(an, it)
    check("0-3s 落在剪切区间", any(s <= 0.01 and e >= 2.9 for s, e in plan["cut_ranges"]),
          plan["cut_ranges"][:3])

    it = parse_demand_rules("最后5秒去掉")
    check("结尾锚点", any(c.get("anchor") == "tail" and c.get("len") == 5 for c in it["cuts"]),
          it["cuts"])
    plan = build_plan(an, it)
    check("结尾 115-120 被剪", any(s <= 115.5 and e >= 119.9 for s, e in plan["cut_ranges"]),
          plan["cut_ranges"][-3:])

    it = parse_demand_rules("只保留10秒到40秒")
    check("保留区间解析", bool(it["keep"]), it["keep"])
    plan = build_plan(an, it)
    check("只留 10-40s（约 30s）", 28 <= total_after(plan) <= 31, total_after(plan))

    print("\n== 4. 卡点混剪 ==")
    it = parse_demand_rules("剪成60秒的卡点混剪，开头3秒不要")
    check("风格=卡点", it["style"] == "beat", it["style"])
    check("目标=60", it["target"] == 60.0, it["target"])
    check("开头剪掉", any(c.get("end") == 3 for c in it["cuts"]), it["cuts"])
    plan = build_plan(an, it)
    check("卡点方案用原音轨", plan.get("audio_mode") == "full", plan.get("audio_mode"))
    check("卡点方案有节拍信息", "beat" in plan, list(plan.keys()))
    check("卡点排出了镜头", len(plan["keep_ranges"]) >= 4, len(plan["keep_ranges"]))
    check("卡点成片接近目标", abs(total_after(plan) - 60) <= 8, total_after(plan))

    it = parse_demand_rules("每4拍换一个镜头，卡点")
    check("每4拍", it["shot_beats"] == 4, it["shot_beats"])

    print("\n== 5. 说不出具体要求时 ==")
    it = parse_demand_rules("帮我剪一下")
    check("风格=智能", it["style"] == "auto", it["style"])
    check("目标是空", it["target"] is None, it["target"])
    plan = build_plan(an, it)
    check("照样给出方案", plan["saved"] > 0, plan["saved"])
    txt = describe(it, plan)
    check("回执里有引导语", "没抓到" in txt, txt[:60])

    print("\n== 6. 做不到的要如实说 ==")
    it = parse_demand_rules("加个字幕，顺便换个背景音乐")
    check("字幕现在能做了（不再报不支持）", not any("字幕" in n for n in it["notes"]), it["notes"])
    check("字幕意图被识别", bool(it.get("subtitle", {}).get("on")), it.get("subtitle"))
    check("BGM 意图被识别", bool(it.get("bgm", {}).get("on")), it.get("bgm"))

    it = parse_demand_rules("帮我调个色，再裁成竖屏")
    check("调色仍然如实说不支持", any("调色" in n for n in it["notes"]), it["notes"])
    txt = describe(it)
    check("回执里带上做不到的", "做不到" in txt, txt[:80])

    print("\n== 7. 边界 ==")
    it = parse_demand_rules("")
    plan = build_plan(an, it)
    check("空需求不崩", isinstance(plan["after"], (int, float)) and plan["keep_ranges"],
          plan["after"])
    it = parse_demand_rules("只保留1秒到2秒")
    plan = build_plan(an, it)
    check("几乎全剪时保留一小段、不报空", plan["after"] > 0, plan["after"])

    print("\n== 8. 回执文案 ==")
    it = parse_demand_rules("卡点混剪45秒，紧凑一点，别剪静音")
    plan = build_plan(an, it)
    txt = describe(it, plan)
    check("回执含风格", "卡点" in txt, txt)
    check("回执含时长", "45" in txt, txt)
    print("\n  ---- 示例回执 ----")
    print("  " + txt.replace("\n", "\n  "))
    print("  ------------------")

    print("\n== 9. 字幕意图 ==")
    it = parse_demand_rules("加字幕")
    check("加字幕被识别", it["subtitle"] and it["subtitle"]["on"] is True, it["subtitle"])
    it = parse_demand_rules("不要字幕")
    check("不要字幕被识别", it["subtitle"] and it["subtitle"]["on"] is False, it["subtitle"])
    it = parse_demand_rules("加字幕，字大一点，放顶部")
    sub = it["subtitle"] or {}
    check("大字", sub.get("size") == "large", sub)
    check("顶部", sub.get("position") == "top", sub)
    it = parse_demand_rules("加黄色字幕")
    check("颜色", (it["subtitle"] or {}).get("color") == "#FFD700", it["subtitle"])
    it = parse_demand_rules("紧凑一点")
    check("不提字幕就不动字幕开关", it["subtitle"] is None, it["subtitle"])

    print("\n== 10. BGM 意图 ==")
    it = parse_demand_rules("换个背景音乐")
    check("换 BGM 被识别", it["bgm"] and it["bgm"]["on"] is True, it["bgm"])
    it = parse_demand_rules("加个 BGM，音乐小一点")
    check("音量压到 0.14", (it["bgm"] or {}).get("volume") == 0.14, it["bgm"])
    it = parse_demand_rules("配上 music.mp3 这首")
    check("点名了文件", (it["bgm"] or {}).get("query") == "music.mp3", it["bgm"])
    it = parse_demand_rules("不要原声，只要音乐")
    check("去原声", (it["bgm"] or {}).get("keep_original") is False, it["bgm"])
    it = parse_demand_rules("不要背景音乐")
    check("不要 BGM", it["bgm"]["on"] is False, it["bgm"])

    print("\n== 11. 变速意图 ==")
    it = parse_demand_rules("1.2倍速")
    check("1.2 倍速", it["speed"] == 1.2, it["speed"])
    it = parse_demand_rules("两倍速播放")
    check("两倍速", it["speed"] == 2.0, it["speed"])
    it = parse_demand_rules("整体放慢一点")
    check("放慢默认 0.8", it["speed"] == 0.8, it["speed"])
    it = parse_demand_rules("10到20秒放慢到0.5倍")
    z = it["speed_zones"]
    check("区间变速被识别", len(z) == 1 and z[0]["start"] == 10 and z[0]["end"] == 20
          and z[0]["speed"] == 0.5, z)
    it = parse_demand_rules("紧凑一点")
    check("「紧凑」不误判成变速", it["speed"] is None, it["speed"])
    it = parse_demand_rules("剪短一点，节奏快些")
    check("「剪短」不误判成变速", it["speed"] is None, it["speed"])

    print("\n== 12. 转场意图 ==")
    it = parse_demand_rules("加转场")
    check("只说加转场 → 交给自动判断", (it["transition"] or {}).get("type") == "auto",
          it["transition"])
    it = parse_demand_rules("加个叠化")
    check("明确说了类型 → 全片叠化", (it["transition"] or {}).get("type") == "fade",
          it["transition"])
    it = parse_demand_rules("用黑场过渡")
    check("黑场过渡", (it["transition"] or {}).get("type") == "black", it["transition"])
    it = parse_demand_rules("不要转场，直接切")
    check("硬切", it["transition"] and it["transition"]["type"] is None, it["transition"])
    it = parse_demand_rules("加转场，每个0.8秒")
    check("转场时长", abs((it["transition"] or {}).get("duration", 0) - 0.8) < 0.01,
          it["transition"])

    print("\n== 13. 效果落进方案 ==")
    an2 = analysis(60.0)
    it = parse_demand_rules("紧凑一点")
    base = build_plan(an2, it)
    it = parse_demand_rules("紧凑一点，1.5倍速")
    fast = build_plan(an2, it)
    check("变速后成片变短", fast["after"] < base["after"] - 1,
          (round(base["after"], 1), round(fast["after"], 1)))
    check("变速比例大致对", abs(fast["after"] - base["after"] / 1.5) < 2.0,
          (round(base["after"], 1), round(fast["after"], 1)))
    check("keep_ranges 带上速度",
          all(isinstance(k, dict) and abs(k.get("speed", 1) - 1.5) < 0.01
              for k in fast["keep_ranges"]), fast["keep_ranges"][:2])

    it = parse_demand_rules("紧凑一点，控制在30秒，1.5倍速")
    fast = build_plan(an2, it)
    check("目标时长会为变速让路（剪到 45s 再变速）", fast["after"] <= 32, fast["after"])

    it = parse_demand_rules("紧凑一点，加转场")
    tp = build_plan(an2, it)
    check("转场吃掉了时长", tp["after"] < base["after"], (base["after"], tp["after"]))
    check("方案里带上转场设置", tp.get("transition", {}).get("type") == "auto", tp.get("transition"))

    it = parse_demand_rules("紧凑一点，10到20秒放慢到0.5倍，加转场")
    sp = build_plan(an2, it)
    check("区间变速时转场被放弃并说明", sp.get("transition") is None and bool(sp.get("warning")),
          (sp.get("transition"), sp.get("warning")))

    print("\n== 14. 新能力回执 ==")
    it = parse_demand_rules("剪掉停顿，加字幕，换个轻一点的BGM，1.2倍速，加转场")
    plan = build_plan(analysis(120.0), it)
    txt = describe(it, plan)
    for word in ("字幕", "BGM", "倍速", "转场"):
        check(f"回执提到「{word}」", word in txt, txt[:160])
    check("回执不再说做不到", "做不到" not in txt, txt[:160])

    print("\n== 15. 首尾淡入淡出 ==")
    it = parse_demand_rules("片头淡入1秒，片尾淡出1.5秒")
    check("解析出淡入时长", (it["fades"] or {}).get("fade_in") == 1.0, it["fades"])
    check("解析出淡出时长", (it["fades"] or {}).get("fade_out") == 1.5, it["fades"])
    check("淡入的秒数没被当成目标时长", it["target"] is None, it["target"])
    check("淡入的秒数没被当成要剪的区间", not it["cuts"], it["cuts"])

    it = parse_demand_rules("片头不要淡入")
    check("「不要淡入」给 0", (it["fades"] or {}).get("fade_in") == 0.0, it["fades"])

    it = parse_demand_rules("片尾淡出2秒，剪成60秒")
    check("淡出时长和成片目标时长分得开",
          (it["fades"] or {}).get("fade_out") == 2.0 and it["target"] == 60, (it["fades"], it["target"]))

    it = parse_demand_rules("片头淡入1秒")
    plan = build_plan(analysis(120.0), it)
    check("只淡入不给接缝转场", (plan.get("transition") or {}).get("type") == "none",
          plan.get("transition"))
    check("淡入值落进方案", (plan.get("transition") or {}).get("fade_in") == 1.0,
          plan.get("transition"))
    txt = describe(it, plan)
    check("回执说清片头淡入", "淡入" in txt, txt[:160])

    it = parse_demand_rules("加转场，片尾淡出1秒")
    plan = build_plan(analysis(120.0), it)
    check("又加转场又要淡出：两个都在",
          (plan.get("transition") or {}).get("type") == "auto"
          and (plan.get("transition") or {}).get("fade_out") == 1.0, plan.get("transition"))

    it = parse_demand_rules("加淡入淡出")
    check("「加淡入淡出」仍按转场处理",
          (it["transition"] or {}).get("on") and (it["fades"] or {}).get("fade_in", 0) > 0,
          (it["transition"], it["fades"]))

    print("\n== 16. 语音级剪辑（口头禅 / 重说）==")
    it = parse_demand_rules("剪掉口头禅")
    check("识别「口头禅」", (it["speech"] or {}).get("kinds") == ["filler"], it["speech"])
    check("只提口头禅时不顺带剪重说",
          "retake" not in ((it["speech"] or {}).get("kinds") or []), it["speech"])

    it = parse_demand_rules("嗯啊这些语气词别留")
    check("识别「语气词」", "filler" in ((it["speech"] or {}).get("kinds") or []), it["speech"])

    it = parse_demand_rules("说错重来的剪掉")
    check("识别「重说」", (it["speech"] or {}).get("kinds") == ["retake"], it["speech"])

    it = parse_demand_rules("口头禅和重说都剪掉")
    check("两种都要",
          set((it["speech"] or {}).get("kinds") or []) == {"filler", "retake"}, it["speech"])

    it = parse_demand_rules("别剪口头禅")
    check("「别剪口头禅」= 关掉", (it["speech"] or {}).get("on") is False, it["speech"])

    it = parse_demand_rules("加字幕")
    check("没提就不开语音级（要转写，很慢）", it["speech"] is None, it["speech"])

    it = parse_demand_rules("卡点混剪 30 秒，剪掉口头禅")
    plan = build_plan(analysis(120.0), it)
    check("卡点风格不跑语音级剪辑",
          (plan.get("speech") or {}) == {}, plan.get("speech"))

    print(f"\n通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    for f in FAIL:
        print("  ✗ " + f)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        traceback.print_exc()
        sys.exit(2)
