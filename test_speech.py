# -*- coding: utf-8 -*-
"""语音级剪辑回归测试 —— 纯逻辑，不需要视频、不需要模型

重点不是「能不能检出」，而是「会不会剪错」：
把「好啊」剪成「好」、把「然后我们开始」的「然后」剪掉、
把「第一点 / 第二点」当成重说剪掉一条 —— 这些都是不可接受的，
测试里专门占了几条守着。
"""

import sys

from backend import speech_cut as sc

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


def W(s, e, t):
    return {"start": s, "end": e, "text": t}


def seg(s, e, text, words=None):
    d = {"start": s, "end": e, "text": text}
    if words is not None:
        d["words"] = words
    return d


# 一句话里每个字的等分时间，省得每条都手写
def line(s, e, text, unit=0.2):
    chars = [c for c in text if c.strip()]
    return seg(s, e, text, [W(s + i * unit, s + (i + 1) * unit, c)
                            for i, c in enumerate(chars)])


def main():
    print("== 1. 单字语气词：孤立就剪 ==")
    s = [line(0.0, 0.5, "嗯"), line(1.0, 3.0, "我们今天讲这个产品")]
    r = sc.detect_fillers(s)
    check("孤立的「嗯」被剪", len(r) == 1 and r[0]["text"] == "嗯", r)
    check("剪点落在词边界内（没吃掉后面）", r and r[0]["end"] <= 0.6, r)
    check("原因说得清楚", r and "语气词" in r[0]["reason"], r)

    print("\n== 2. 句尾语气词：两侧都要停顿才剪 ==")
    r = sc.detect_fillers([line(0.0, 0.6, "好啊")])
    check("「好啊」不能剪成「好」", not r, r)
    r = sc.detect_fillers([line(0.0, 0.4, "啊"), line(0.9, 2.0, "原来是这样")])
    check("单独一句的「啊」可以剪", len(r) == 1, r)

    print("\n== 3. 词组口头禅：不能乱剪连接词 ==")
    r = sc.detect_fillers([line(0.0, 1.2, "然后我们开始")])
    check("「然后我们开始」里的然后不剪", not r, r)
    r = sc.detect_fillers([line(0.0, 0.6, "那个"), line(1.1, 3.0, "我们开始")])
    check("孤立成一句的「那个」剪掉", len(r) == 1, r)

    print("\n== 4. 连说两遍：一定剪 ==")
    r = sc.detect_fillers([line(0.0, 1.6, "那个那个我们开始", 0.2)])
    check("「那个那个」剪掉", len(r) == 1, r)
    check("两遍一起剪，不是只剪一遍", r and r[0]["end"] >= 0.7, r)
    r = sc.detect_fillers([line(0.0, 0.8, "嗯嗯"), line(1.2, 3.0, "我知道了")])
    check("「嗯嗯」剪掉", len(r) == 1, r)

    print("\n== 5. 强度分档 ==")
    s = [line(0.0, 0.5, "那个"), line(0.75, 3.0, "我们开始")]
    tight = sc.detect_fillers(s, "tight")
    cons = sc.detect_fillers(s, "conservative")
    check("紧凑档一侧小停顿也剪", len(tight) == 1, tight)
    check("保守档不乱剪", len(cons) == 0, cons)

    print("\n== 6. 没有词级时间戳时不能剪语气词 ==")
    s = [seg(0.0, 3.0, "嗯我们今天讲这个产品")]
    check("整段没有 words → 检测不到", not sc.detect_fillers(s))
    a = sc.analyze(s, kinds=("filler", "retake"))
    check("analyze 如实说明没剪", "词级" in a["note"], a["note"])
    check("word_level 标记为 False", a["word_level"] is False)

    print("\n== 7. 句内重复词组 ==")
    s = [line(0.0, 3.0, "我们今天讲这个我们今天讲这个产品")]
    r = sc.detect_retakes(s)
    check("识别出句内重复", len(r) == 1, r)
    check("只剪前一遍（到第二遍开头为止）",
          r and r[0]["start"] == 0.0 and 1.2 <= r[0]["end"] <= 1.5, r)
    check("原因里带上重复的词", r and "我们今天讲这个" in r[0]["reason"], r)

    print("\n== 8. 跨句重说 ==")
    r = sc.detect_retakes([seg(0, 2.0, "这个方案我同意。"), seg(2.2, 4.2, "这个方案我同意。")])
    check("同一句说两遍 → 剪前一遍", len(r) == 1 and r[0]["end"] <= 2.0, r)

    r = sc.detect_retakes([seg(0, 1.1, "我们今天讲"), seg(1.3, 3.5, "我们今天讲这个产品的三个卖点。")])
    check("说半句停下重来 → 剪掉半句", len(r) == 1, r)

    print("\n== 9. 不能把并列内容当重说 ==")
    r = sc.detect_retakes([seg(0, 2.0, "我们来讲第一点。"), seg(2.3, 4.3, "我们来讲第二点。")])
    check("「第一点 / 第二点」不算重说", not r, r)
    r = sc.detect_retakes([seg(0, 2.0, "今天天气不错。"), seg(2.3, 4.3, "我们去外面走走。")])
    check("两句不同的话不算重说", not r, r)
    r = sc.detect_retakes([seg(0, 2.0, "我们来讲第一点。"),
                           seg(2.3, 4.3, "我们来讲第一点和第二点。")])
    check("说完一句再补一句不算重说", not r, r)

    print("\n== 10. 隔太久的相同句不算重说 ==")
    r = sc.detect_retakes([seg(0, 2.0, "这个方案我同意。"), seg(5.0, 7.0, "这个方案我同意。")])
    check("隔了 3 秒以上不当重说", not r, r)

    print("\n== 11. 区间合并 ==")
    merged = sc.merge_spans([
        {"start": 1.0, "end": 1.3, "kind": "filler", "reason": "a"},
        {"start": 1.35, "end": 1.6, "kind": "filler", "reason": "b"},
        {"start": 5.0, "end": 5.4, "kind": "filler", "reason": "c"},
    ])
    check("挨着的合成一条", len(merged) == 2, merged)
    check("合并后终点取更远的", merged[0]["end"] == 1.6, merged)

    print("\n== 12. 总量保护 ==")
    many = [line(i * 1.0, i * 1.0 + 0.4, "嗯") for i in range(8)]
    many += [line(i * 1.0 + 0.6, i * 1.0 + 1.0, "好") for i in range(8)]
    a = sc.analyze(many, duration=8.0)
    check("剪掉超过三成就整个不动", not a["filler"], a["filler"][:2])
    check("并给出提示", "没剪" in a["note"], a["note"])
    a = sc.analyze(many[:4], duration=60.0)
    check("正常比例照常剪", len(a["filler"]) > 0, a["filler"])

    print("\n== 13. 回执文案 ==")
    txt = sc.summarize({"filler": [{}] * 3, "retake": [{}] * 1})
    check("报出几处口头禅和重说", "3 处口头禅" in txt and "1 处重说" in txt, txt)
    check("没东西剪时不说话", sc.summarize({"filler": [], "retake": []}) == "")

    print("\n== 14. 接进决策引擎 ==")
    from backend import autocut

    base = {"duration": 30.0, "has_audio": True, "silent": [], "redundant": [],
            "static": [], "blank": []}
    plan = autocut.decide(dict(base), "standard")
    check("没给语音结果时不产生 filler 决策",
          not any(d["type"] == "filler" for d in plan["decisions"]))

    with_sp = dict(base, speech={
        "filler": [{"start": 5.0, "end": 5.35, "reason": "语气词「嗯」"}],
        "retake": [{"start": 10.0, "end": 13.0, "reason": "这句说了两遍"}],
        "word_level": True,
    })
    plan = autocut.decide(with_sp, "standard")
    kinds = [d["type"] for d in plan["decisions"]]
    check("0.35s 的语气词没被 min_cut 吃掉", "filler" in kinds, kinds)
    check("重说也进了决策", "retake" in kinds, kinds)
    check("统计数透出来", plan["speech"]["filler"] == 1 and plan["speech"]["retake"] == 1,
          plan["speech"])
    check("剪掉的总量算对了", abs(plan["saved"] - 3.35) <= 0.05, plan["saved"])
    check("保留区间不覆盖被剪的部分",
          all(not (s <= 5.1 <= e) for s, e in plan["keep_ranges"]), plan["keep_ranges"])

    print(f"\n{'=' * 40}\n通过 {OK} 项"
          + (f"，失败 {len(FAIL)} 项：{FAIL}" if FAIL else "，全部通过"))
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
