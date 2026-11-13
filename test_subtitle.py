# -*- coding: utf-8 -*-
"""字幕引擎回归测试 —— 纯逻辑，不依赖视频和模型"""

import sys

from backend import subtitle as st

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
    print("== 1. 断句 ==")
    r = st.split_text("大家好", 18)
    check("短句不切", r == ["大家好"], r)

    r = st.split_text("大家好，今天我想聊聊怎么用AI帮我们把长视频剪短。", 18)
    check("长句按逗号切，不切在词中间",
          all("视频简" not in s[:1] for s in r) and all(len(s) <= 26 for s in r), r)
    check("拼回去文字不丢", "".join(r) == "大家好，今天我想聊聊怎么用AI帮我们把长视频剪短。", r)

    long_text = "这是一段非常长的没有任何标点符号的连续中文内容用来测试硬切逻辑是否正常工作不会把内容丢掉"
    r = st.split_text(long_text, 12)
    check("无标点超长句能硬切", len(r) > 1, r)
    check("硬切不丢字", "".join(r) == long_text, r)

    r = st.split_text("", 18)
    check("空文本返回空", r == [], r)

    print("\n== 2. 时间映射：不剪辑 ==")
    keep = [(0.0, 20.0)]
    cues = [{"start": 1.0, "end": 3.0, "text": "第一句"}]
    out = st.map_cues(cues, keep)
    check("原样不动", out and abs(out[0]["start"] - 1.0) < 0.01
          and abs(out[0]["end"] - 3.0) < 0.01, out)

    print("\n== 3. 时间映射：剪掉中间 ==")
    keep = [(0.0, 5.0), (10.0, 15.0)]
    cues = [{"start": 4.0, "end": 6.0, "text": "横跨剪切点"}]
    out = st.map_cues(cues, keep)
    check("跨剪切点的字幕被截到可见部分", len(out) == 1 and out[0]["end"] <= 5.01, out)
    check("内容保留", out and "横跨" in out[0]["text"], out)

    cues = [{"start": 11.0, "end": 12.0, "text": "第二段里"}]
    out = st.map_cues(cues, keep)
    check("第二段字幕前移到 6s", out and abs(out[0]["start"] - 6.0) < 0.05, out)

    cues = [{"start": 6.0, "end": 8.0, "text": "被完全剪掉"}]
    out = st.map_cues(cues, keep)
    check("落在剪掉区间里的字幕被丢弃", out == [], out)

    print("\n== 4. 时间映射：变速 ==")
    keep = [{"start": 0.0, "end": 10.0, "speed": 2.0}]
    cues = [{"start": 2.0, "end": 4.0, "text": "两倍速"}]
    out = st.map_cues(cues, keep)
    check("2 倍速后时间减半", out and abs(out[0]["start"] - 1.0) < 0.01
          and abs(out[0]["end"] - 2.0) < 0.01, out)

    keep = [{"start": 0.0, "end": 10.0}]
    cues = [{"start": 2.0, "end": 4.0, "text": "半速"}]
    out = st.map_cues(cues, keep, default_speed=0.5)
    check("0.5 倍速后时间翻倍", out and abs(out[0]["start"] - 4.0) < 0.01, out)

    print("\n== 5. 时间映射：转场重叠 ==")
    keep = [(0.0, 5.0), (10.0, 15.0)]
    tl = st.render_timeline(keep, transition=1.0)
    check("第二段起点因重叠提前 1s", abs(tl[1]["out_start"] - 4.0) < 0.01, tl)
    check("总时长扣掉重叠", abs(tl[-1]["out_end"] - 9.0) < 0.01, tl)

    cues = [{"start": 10.5, "end": 11.0, "text": "第二段开头"}]
    out = st.map_cues(cues, keep, transition=1.0)
    check("字幕跟着重叠后移", out and abs(out[0]["start"] - 4.5) < 0.05, out)

    print("\n== 6. 混合场景 ==")
    keep = [{"start": 0.0, "end": 4.0, "speed": 1.0},
            {"start": 8.0, "end": 16.0, "speed": 2.0}]
    tl = st.render_timeline(keep)
    check("总长 = 4 + 4 = 8s", abs(tl[-1]["out_end"] - 8.0) < 0.01, tl)
    cues = [{"start": 12.0, "end": 14.0, "text": "变速段"}]
    out = st.map_cues(cues, keep)
    check("变速段字幕映射到 6-7s", out and abs(out[0]["start"] - 6.0) < 0.02, out)

    print("\n== 7. 重叠保护 ==")
    cues = [{"start": 0.0, "end": 3.0, "text": "甲"}, {"start": 2.5, "end": 4.0, "text": "乙"}]
    out = st.map_cues(cues, [(0.0, 5.0)])
    check("相邻字幕不重叠", len(out) == 2 and out[0]["end"] <= out[1]["start"] + 0.01, out)

    print("\n== 8. ASS 生成 ==")
    ass = st.build_ass([{"start": 1.0, "end": 2.5, "text": "测试字幕"}], 1920, 1080)
    check("有脚本头", "[Script Info]" in ass and "[Events]" in ass, ass[:60])
    check("PlayRes 正确", "PlayResX: 1920" in ass and "PlayResY: 1080" in ass)
    check("时间格式 0:00:01.00", "0:00:01.00" in ass, ass[-160:])
    check("有对话行", "Dialogue: 0,0:00:01.00,0:00:02.50" in ass, ass[-160:])

    ass = st.build_ass([{"start": 0, "end": 1, "text": "换行\n测试"}], 720, 1280,
                       {"position": "top", "color": "#FFD700", "size_ratio": 0.05})
    check("竖屏 PlayRes", "PlayResX: 720" in ass and "PlayResY: 1280" in ass)
    check("换行转义 \\N", "换行\\N测试" in ass, ass[-120:])
    check("顶部对齐", ",8,60,60," in ass, [ln for ln in ass.split("\n") if "Style:" in ln])
    check("金色转 BGR", "&H0000D7FF" in ass, [ln for ln in ass.split("\n") if "Style:" in ln])

    print("\n== 9. SRT 解析 ==")
    srt = """1
00:00:01,000 --> 00:00:03,500
第一句字幕

2
00:00:04,000 --> 00:00:06,000
第二句
跨行内容
"""
    segs = st.parse_srt(srt)
    check("解析出 2 条", len(segs) == 2, segs)
    check("时间正确", abs(segs[0]["start"] - 1.0) < 0.01
          and abs(segs[0]["end"] - 3.5) < 0.01, segs[0])
    check("多行合并", segs[1]["text"] == "第二句 跨行内容", segs[1])

    vtt = """WEBVTT

00:00:02.000 --> 00:00:04.000
VTT 内容
"""
    segs = st.parse_srt(vtt)
    check("能读 VTT", len(segs) == 1 and segs[0]["text"] == "VTT 内容", segs)

    out_srt = st.to_srt([{"start": 1.234, "end": 3.5, "text": "回写"}])
    check("SRT 回写格式", "00:00:01,234 --> 00:00:03,500" in out_srt, out_srt)

    print("\n== 10. 边界 ==")
    check("空 keep 返回空", st.map_cues([{"start": 0, "end": 1, "text": "x"}], []) == [])
    check("空 cues 返回空", st.map_cues([], [(0, 5)]) == [])
    check("超短 keep 被忽略", st.render_timeline([(0, 0.01)]) == [])
    check("速度被夹紧", st.render_timeline([{"start": 0, "end": 10, "speed": 999}])[0]["speed"] <= 10)

    print(f"\n{'='*40}\n通过 {OK} 项" + (f"，失败 {len(FAIL)} 项：{FAIL}" if FAIL else "，全部通过"))
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
