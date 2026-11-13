# -*- coding: utf-8 -*-
"""语音级剪辑的端到端测试 —— 真出片，但转写结果打桩

为什么打桩：本机没有现成的人声素材，系统 TTS 又被安全策略挡住（Add-Type / COM 全禁）。
转写引擎本身有 `test_subtitle.py` 36 项守者、也做过真实中文语音验证，
这里要验的是另一半——**转写结果能不能真的变成剪辑决策并烧进成片**：

1. 勾了口头禅 → 决策里出现 filler，且区间落在该剪的地方
2. 不勾 → 压根不调用转写（老流程一点没变慢）
3. 剪完的成片真的短了，且剪掉的正好是语气词那一段

桩数据是照着真实 whisper 输出结构造的（每段带 words）。
"""

import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from backend import audio_fx, server, subtitle  # noqa: E402

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


# 0.3–0.9s 一个孤立的「嗯」，后面是正常说话 —— 照着本工具 9s 素材的长度造
CANNED = [
    {"start": 0.3, "end": 1.0, "text": "嗯。", "words": [W(0.3, 0.85, "嗯"), W(0.85, 1.0, "。")]},
    {"start": 1.4, "end": 3.4, "text": "今天我们讲这个产品。",
     "words": [W(1.4, 1.7, "今天"), W(1.7, 1.9, "我们"), W(1.9, 2.1, "讲"),
               W(2.1, 2.4, "这个"), W(2.4, 2.8, "产品"), W(2.8, 3.4, "。")]},
    {"start": 3.8, "end": 6.0, "text": "它的第一个卖点是便宜。",
     "words": [W(3.8, 4.0, "它"), W(4.0, 4.2, "的"), W(4.2, 4.5, "第一个"),
               W(4.5, 4.8, "卖点"), W(4.8, 5.2, "是"), W(5.2, 5.7, "便宜"), W(5.7, 6.0, "。")]},
    {"start": 6.4, "end": 8.6, "text": "第二个卖点是耐用。",
     "words": [W(6.4, 6.6, "第二个"), W(6.6, 6.9, "卖点"), W(6.9, 7.2, "是"),
               W(7.2, 7.8, "耐用"), W(7.8, 8.6, "。")]},
]


def run_job(client, payload, limit=900):
    r = client.post("/api/autocut", json=payload)
    j = r.get_json()
    if not j.get("ok"):
        return None, j.get("error") or "启动失败"
    jid = j["job"]
    s = {}
    for _ in range(limit):
        s = (client.get(f"/api/job/{jid}").get_json() or {}).get("job") or {}
        if s.get("status") in ("done", "error", "cancelled"):
            break
        time.sleep(0.3)
    return s, ""


def main():
    src = ROOT / "projects" / "test_demo.mp4"
    if not src.exists():
        print(f"找不到素材 {src}，跳过")
        return 0

    real = subtitle.transcribe
    calls = {"n": 0}

    def fake(*a, **kw):
        calls["n"] += 1
        if kw.get("progress_cb"):
            kw["progress_cb"](100, "打桩：假装转写完了")
        return [dict(x) for x in CANNED]

    subtitle.transcribe = fake
    client = server.app.test_client()
    try:
        print("== 1. 不勾语音级：不该碰转写 ==")
        calls["n"] = 0
        st, err = run_job(client, {"path": str(src), "strength": "standard"})
        check("任务跑完", st and st.get("status") == "done", (st or {}).get("error") or err)
        check("没调用转写", calls["n"] == 0, calls["n"])
        plan = ((st or {}).get("result") or {}).get("plan") or {}
        kinds = [d["type"] for d in plan.get("decisions") or []]
        check("没有 filler 决策", "filler" not in kinds, kinds)

        print("\n== 2. 勾了口头禅 ==")
        calls["n"] = 0
        st, err = run_job(client, {"path": str(src), "strength": "standard",
                                   "speech": {"filler": True}})
        check("任务跑完", st and st.get("status") == "done", (st or {}).get("error") or err)
        check("调用了一次转写", calls["n"] == 1, calls["n"])

        plan = ((st or {}).get("result") or {}).get("plan") or {}
        fillers = [d for d in plan.get("decisions") or [] if d["type"] == "filler"]
        check("决策里出现口头禅", len(fillers) == 1, fillers)
        if fillers:
            f = fillers[0]
            check("剪的正是那个「嗯」", 0.2 <= f["start"] <= 0.4 and 0.8 <= f["end"] <= 1.0, f)
            check("原因可读", "语气词" in (f.get("reason") or ""), f.get("reason"))

        stats = plan.get("speech") or {}
        check("统计透出 1 处", stats.get("filler") == 1, stats)
        check("标记为词级", stats.get("word_level") is True, stats)

        out = ((st or {}).get("result") or {}).get("output")
        check("出片了", bool(out) and Path(out).exists(), out)
        if out and Path(out).exists():
            src_d = audio_fx.probe(str(src))["duration"]
            out_d = audio_fx.probe(out)["duration"]
            check("成片比原片短", out_d < src_d - 0.3, (src_d, out_d))

        print("\n== 3. 两种都勾 ==")
        calls["n"] = 0
        st, err = run_job(client, {"path": str(src), "strength": "standard",
                                   "speech": {"filler": True, "retake": True}})
        check("任务跑完", st and st.get("status") == "done", (st or {}).get("error") or err)
        plan = ((st or {}).get("result") or {}).get("plan") or {}
        stats = plan.get("speech") or {}
        check("口头禅仍然检出", stats.get("filler") == 1, stats)
        check("这段素材没有重说，重说为 0", stats.get("retake") == 0, stats)

        print("\n== 4. 转写炸了不能拦住出片 ==")
        def boom(*a, **kw):
            raise RuntimeError("模型没装好")

        subtitle.transcribe = boom
        st, err = run_job(client, {"path": str(src), "strength": "standard",
                                   "speech": {"filler": True}})
        check("照样出片", st and st.get("status") == "done", (st or {}).get("error") or err)
        plan = ((st or {}).get("result") or {}).get("plan") or {}
        note = (plan.get("speech") or {}).get("note") or ""
        check("回执里说明没剪成", "转写没成功" in note, note)
        check("没有 filler 决策",
              not any(d["type"] == "filler" for d in plan.get("decisions") or []))
    finally:
        subtitle.transcribe = real

    print(f"\n{'=' * 40}\n通过 {OK} 项"
          + (f"，失败 {len(FAIL)} 项：{FAIL}" if FAIL else "，全部通过"))
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
