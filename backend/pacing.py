# -*- coding: utf-8 -*-
"""镜头节奏与转场编排

问题：面板上选一个转场类型，全片每个接缝都用它，看起来像套模板——
尤其口播里剪掉一个停顿也叠化，几秒钟糊一次，很假。

这里做三件事：

1. 接缝要不要转场，看它「换没换镜头」
   - 同一镜头里剪掉停顿 → 硬切（正确做法，加了反而闪）
   - 换了镜头 → 才考虑转场，剪掉的内容越多、镜头越长，转场越明显
   判断方式不用重新分析全片：切片时每段本身就是个小文件，
   比较「前一段的尾帧」和「后一段的首帧」画面差异就够，几十毫秒一段。

2. 用哪种转场，看「换的是什么」
   - 明暗突变（灯一关/从室内到室外）→ 黑场过渡，中间压一段黑最不跳
   - 颜色换了一大套（色温、主色调都变了）→ 溶解
   - 普通换镜头 / 同场景换机位 → 叠化
   擦除、滑动这类有方向感的转场不自动用：直方图看不出「该往哪个方向擦」，
   硬猜会显得随机，留给手动指定。

3. 转场时长跟着镜头长短走
   - 长镜头 0.5s，一秒钟的快剪 0.25s，太短直接硬切
   - 首尾淡入淡出由调用方决定开不开、开多长（`fades_of`）

画面判断依赖 cv2，取不到就退回「按剪掉的内容量猜」，不会因此渲染失败。
"""

import numpy as np

# 两张直方图的巴氏距离超过这个值，认为是换了镜头
# （实测：同镜头内剪停顿多在 0.05–0.15，硬切换镜头多在 0.45–0.85）
SCENE_DIST = 0.30

# 剪掉超过这么多秒，说明是段落级切换而不是剪口气
SEGMENT_GAP = 1.6

# 前后亮度（0–255 的灰度均值）差这么多，算「明暗突变」→ 黑场过渡
LIGHT_JUMP = 45.0
# 饱和度均值差这么多，算「颜色换了一大套」→ 溶解
SAT_JUMP = 22.0
# 直方图差异大到这个程度，整幅画面已经换掉了
BIG_DIST = 0.55
# 黑场过渡短于这个数会像闪一下黑，宁可不给（0.3s 档正好落在这条线下）
MIN_BLACK = 0.32

# 卡点成片的上限：转场一叠就吃时长，节拍会跟着整体提前，所以压到 0.2s 以内
BEAT_CAP = 0.20

# 自动模式下默认的片头淡入 / 片尾淡出
DEFAULT_FADE_IN = 0.4
DEFAULT_FADE_OUT = 0.5


# ---------------- 画面指纹 ----------------

def _hist(frame):
    """HSV 二维直方图，归一化到和为 1（画面内容的指纹）"""
    import cv2

    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    h = cv2.calcHist([hsv], [0, 1], None, [30, 32], [0, 180, 0, 256])
    h = h.astype(np.float32).ravel()
    s = float(h.sum())
    return h / s if s > 0 else h


def _luma(frame):
    """平均亮度 0–255"""
    import cv2

    return float(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY).mean())


def _sat(frame):
    """平均饱和度 0–255（只影响颜色浓度，跟明暗无关）"""
    import cv2

    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    return float(hsv[:, :, 1].mean())


def distance(h1, h2):
    """巴氏距离，0 = 完全一样，1 = 毫无关系"""
    a = np.asarray(h1, dtype=np.float32).ravel()
    b = np.asarray(h2, dtype=np.float32).ravel()
    if a.size != b.size or a.size == 0:
        return 0.0
    sa, sb = float(a.sum()), float(b.sum())
    if sa > 0:
        a = a / sa
    if sb > 0:
        b = b / sb
    bc = float(np.sum(np.sqrt(a * b)))
    return float(np.sqrt(max(0.0, 1.0 - bc)))


def _frames(path):
    """读一小段视频的首帧和尾帧，失败返回 None"""
    try:
        import cv2

        cap = cv2.VideoCapture(str(path))
        if not cap.isOpened():
            return None
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        if total <= 0:
            cap.release()
            return None
        ok, first = cap.read()
        if not ok or first is None:
            cap.release()
            return None
        last = first
        if total > 2:
            cap.set(cv2.CAP_PROP_POS_FRAMES, total - 2)
            ok2, f2 = cap.read()
            if ok2 and f2 is not None:
                last = f2
        cap.release()
        return first, last
    except Exception:
        return None


def part_profile(path):
    """一段小视频的首尾画面画像，失败返回 None

    比 `part_signature` 多带了亮度/饱和度：光靠直方图距离只知道「变了多少」，
    加上这两项才知道「变的是什么」，才能挑对转场类型。
    """
    fr = _frames(path)
    if not fr:
        return None
    first, last = fr
    try:
        return {
            "first_hist": _hist(first),
            "last_hist": _hist(last),
            "first_y": _luma(first),
            "last_y": _luma(last),
            "first_s": _sat(first),
            "last_s": _sat(last),
        }
    except Exception:
        return None


def part_signature(path):
    """(首帧指纹, 尾帧指纹)，失败返回 None —— 老接口，测试和外部调用仍在用"""
    prof = part_profile(path)
    if not prof:
        return None
    return (prof["first_hist"], prof["last_hist"])


def seam_info(prev_prof, next_prof):
    """前后两段的「差异画像」：画面差多少、明暗差多少、颜色浓度差多少"""
    if not prev_prof or not next_prof:
        return None
    return {
        "dist": distance(prev_prof["last_hist"], next_prof["first_hist"]),
        "dy": next_prof["first_y"] - prev_prof["last_y"],
        "ds": next_prof["first_s"] - prev_prof["last_s"],
    }


# ---------------- 判定 ----------------

def pick_type(info=None):
    """换了镜头之后，用哪种转场 → (类型, 为什么)

    只在自动模式下用。没有画面信息时退回叠化（最不容易出错的过渡）。
    """
    if not info:
        return "fade", ""
    dist = float(info.get("dist") or 0)
    dy = abs(float(info.get("dy") or 0))
    ds = abs(float(info.get("ds") or 0))

    if dy >= LIGHT_JUMP:
        return "black", f"前后明暗差 {dy:.0f}，中间压一段黑最不跳"
    if ds >= SAT_JUMP and dist >= 0.45:
        return "dissolve", f"颜色换了一大套（饱和度差 {ds:.0f}），溶解过去更柔"
    if dist >= BIG_DIST:
        return "dissolve", "整幅画面都换掉了，用溶解"
    return "fade", ""


def fit_duration(prev_len, next_len, want=0.5):
    """转场时长不能超过任一镜头的一半，否则会把整段吃掉"""
    shortest = min(float(prev_len or 0), float(next_len or 0))
    if shortest <= 0:
        return 0.0
    d = min(float(want), shortest * 0.45)
    if d < 0.12:
        return 0.0
    return round(d, 2)


def decide_seam(dist, gap, prev_len, next_len, base_type="auto", base_dur=None,
                info=None, cap=None):
    """单个接缝怎么处理

    dist      前后画面差异（None = 没拿到画面）
    gap       这里剪掉了多少秒
    prev/next 两段各自的成片时长
    info      可选，seam_info() 的结果，带了它才能挑类型（dist 以它为准）
    cap       可选，转场时长的硬上限（卡点成片用，别吃掉节拍）
    """
    if info and info.get("dist") is not None:
        dist = float(info["dist"])
    hard_cap = float(cap) if cap else None

    manual = base_type not in ("auto", "", None)
    if manual:
        # 手动指定类型：全片统一（老行为），时长仍按镜头长短收敛
        kind = base_type
        want = base_dur or 0.5
        if hard_cap:
            want = min(want, hard_cap)
        d = fit_duration(prev_len, next_len, want)
        if d <= 0:
            return {"type": "hard", "duration": 0.0, "reason": "镜头太短，转了会看不出来"}
        return {"type": kind, "duration": d, "reason": "手动指定"}

    kind, why = pick_type(info)

    if dist is None:
        # 画面拿不到，只能按剪掉多少猜：剪得多多半是过了段落
        if gap >= SEGMENT_GAP:
            d = fit_duration(prev_len, next_len, hard_cap or 0.45)
            if d > 0:
                return {"type": "fade", "duration": d, "reason": "剪掉了整段（没读到画面，用叠化）"}
        return {"type": "hard", "duration": 0.0, "reason": "同一镜头内剪停顿"}

    if dist < SCENE_DIST:
        return {"type": "hard", "duration": 0.0, "reason": "同一镜头内剪停顿"}

    # 换了镜头：只有「过得比较重」的接缝才值得加转场
    if gap >= SEGMENT_GAP and min(prev_len, next_len) >= 1.2:
        want = 0.55 if (gap >= 3.0 and min(prev_len, next_len) >= 3.0) else 0.4
        d = fit_duration(prev_len, next_len, min(want, hard_cap) if hard_cap else want)
        if d > 0:
            kind, d, why = _settle(kind, d, why)
            return {"type": kind, "duration": d,
                    "reason": f"换镜头 + 剪掉 {gap:.1f}s，算是过段落" + (f"；{why}" if why else "")}
        return {"type": "hard", "duration": 0.0, "reason": "镜头太短，转了看不出来"}

    if min(prev_len, next_len) < 1.5:
        # 快剪段落：硬切更利落，加转场会拖泥带水
        return {"type": "hard", "duration": 0.0, "reason": "快剪段落，硬切更利落"}

    d = fit_duration(prev_len, next_len, min(0.3, hard_cap) if hard_cap else 0.3)
    if d <= 0:
        return {"type": "hard", "duration": 0.0, "reason": "镜头太短"}
    kind, d, why = _settle(kind, d, why)
    return {"type": kind, "duration": d, "reason": "换镜头" + (f"；{why}" if why else "")}


def _settle(kind, dur, why):
    """黑场过渡太短会变成「闪一下黑」，这种时候退回叠化"""
    if kind == "black" and dur < MIN_BLACK:
        return "fade", dur, "黑场太短会闪，改用叠化"
    return kind, dur, why


def plan_seams(parts, segs, base_type="auto", base_dur=None, cap=None):
    """给每个接缝定方案

    parts 已切好的小片段路径（用来取首尾帧）
    segs  每段的 {start,end,speed}，长度要和 parts 一致
    cap   可选，转场时长上限（卡点成片传 BEAT_CAP，别把节拍吃掉）
    """
    n = len(parts or [])
    if n < 2:
        return []

    profs = [part_profile(p) for p in parts]
    seams = []
    for i in range(1, n):
        prev_seg, next_seg = segs[i - 1], segs[i]
        prev_len = (float(prev_seg["end"]) - float(prev_seg["start"])) / max(0.1, float(prev_seg["speed"]))
        next_len = (float(next_seg["end"]) - float(next_seg["start"])) / max(0.1, float(next_seg["speed"]))
        gap = max(0.0, float(next_seg["start"]) - float(prev_seg["end"]))

        info = seam_info(profs[i - 1], profs[i])
        dist = info["dist"] if info else None
        seams.append(decide_seam(dist, gap, prev_len, next_len, base_type, base_dur,
                                 info, cap))
    return seams


def group_segments(n, overlaps):
    """按接缝把段分组：组内硬切，组间转场

    overlaps 是每个接缝实际的重叠秒数（下标 = 后一段的序号）。
    返回 [[0],[1,2,3],[4]] 这种形状，渲染器据此先 concat 再 xfade。
    """
    if n <= 0:
        return []
    groups = [[0]]
    for i in range(1, n):
        ov = float(overlaps[i]) if i < len(overlaps) else 0.0
        if ov > 0:
            groups.append([i])
        else:
            groups[-1].append(i)
    return groups


def overlaps_of(n, seams, lengths):
    """每个接缝实际的重叠秒数（下标 = 后一段的序号，第一段固定 0）

    和渲染器的 clamp 规则保持一致，字幕映射要用同一份数字。
    """
    out = [0.0] * max(0, n)
    for i in range(1, n):
        seam = seams[i - 1] if i - 1 < len(seams) else None
        if not seam or seam.get("type") in ("hard", None):
            continue
        d = float(seam.get("duration") or 0)
        d = min(d, float(lengths[i - 1]) * 0.45, float(lengths[i]) * 0.45)
        out[i] = round(max(0.0, d), 3)
    return out


def fades_of(transition, total=None):
    """首尾淡入淡出 → (淡入秒, 淡出秒)

    transition 可以是 None（不加转场）或渲染参数 dict。
    默认值只在 `type=auto`（自动编排）时给：手动选一种固定转场时，
    用户要的是「全片统一那一种」，不该自作主张再加头尾淡入。
    """
    if not transition:
        return 0.0, 0.0
    kind = str(transition.get("type") or "auto").strip().lower()
    auto = kind in ("auto", "")
    # type="none"：接缝硬切、只要首尾淡入淡出 —— 不给默认值，全听显式设置

    def _pick(key, default):
        v = transition.get(key)
        if v is None:
            return default
        try:
            return max(0.0, min(2.0, float(v)))
        except (TypeError, ValueError):
            return default

    # 显式写 0 表示「不要」；没写才用默认
    fin = _pick("fade_in", DEFAULT_FADE_IN if auto else 0.0)
    fout = _pick("fade_out", DEFAULT_FADE_OUT if auto else 0.0)

    if fin > 0 and total and fin > float(total) * 0.35:
        fin = round(float(total) * 0.35, 2)
    if fout > 0 and total and fout > float(total) * 0.35:
        fout = round(float(total) * 0.35, 2)
    return round(fin, 2), round(fout, 2)


def summarize(seams):
    """给回执用的一句话总结"""
    if not seams:
        return ""
    used = [s for s in seams if s.get("type") not in ("hard", None)]
    if not used:
        return "所有接缝都硬切"
    kinds = {}
    for s in used:
        kinds[s["type"]] = kinds.get(s["type"], 0) + 1
    label = {"fade": "叠化", "dissolve": "溶解", "black": "黑场", "white": "白场",
             "wipe": "擦除", "slide": "滑动", "circle": "圆形展开",
             "pixel": "像素化", "smooth": "平滑推移"}
    detail = "、".join(f"{label.get(k, k)} {v}" for k, v in kinds.items())
    if len(used) == len(seams):
        return f"{len(seams)} 个接缝都加了转场（{detail}）"
    return f"{len(seams)} 个接缝里给 {len(used)} 个加了转场（{detail}），其余硬切"
