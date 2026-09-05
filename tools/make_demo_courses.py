#!/usr/bin/env python3
"""生成 LectureLite 示例课程（demo/*.lecture.zip）。

每个课程 = 一篇 markdown 讲义 + edge-tts 逐句合成的语音 + 完整操作时间轴
(states/cursor/clicks/annotations/script)，与「录制演讲」一键生成的包同一数据格式，
可直接拖进播放模式观看。运行：python tools/make_demo_courses.py
"""

import asyncio, json, re, sys, zipfile
from pathlib import Path

WEB_DIR = Path(__file__).parent.parent.resolve()
sys.path.insert(0, str(WEB_DIR / "llm"))   # 复用 edge-tts 合成
import edge_tts                             # noqa: E402

VOICE = "zh-CN-YunxiNeural"
RATE = "+8%"
GAP_MS = 420          # 句间停顿
VIEWPORT = {"w": 1280, "h": 720}


def split_sentences(text):
    """把讲稿切成句（保住引号/百分号等结尾）。"""
    parts = re.split(r"(?<=[。！？；：])\s*", text.strip())
    return [p.strip() for p in parts if p.strip()]


async def synth_sentence(text, sem):
    """合成一句，返回 (mp3_bytes, duration_ms)，时长由 mp3 帧头精确解析。"""
    async with sem:
        for attempt in range(4):
            try:
                com = edge_tts.Communicate(text, VOICE, rate=RATE)
                audio = bytearray()
                async for chunk in com.stream():
                    if chunk["type"] == "audio":
                        audio.extend(chunk["data"])
                if audio:
                    return bytes(audio), mp3_duration_ms(bytes(audio))
            except Exception as e:
                if attempt == 3:
                    raise RuntimeError(f"句子合成失败: {text[:30]!r}: {e}")
                await asyncio.sleep(1.5 * (attempt + 1))
        raise RuntimeError(f"句子无音频: {text[:30]!r}")


_BITRATES = {1: 8, 2: 16, 3: 24, 4: 32, 5: 40, 6: 48, 7: 56, 8: 64,
             9: 80, 10: 96, 11: 112, 12: 128, 13: 160, 14: 192, 15: 224}
_SRATES = {0: 44100, 1: 22050, 2: 11025}


def mp3_duration_ms(data):
    """逐帧解析 mp3，返回总时长（毫秒）；解析失败时按 48kbps 估算。"""
    dur, i, n = 0.0, 0, len(data)
    parsed = 0
    while i < n - 4:
        if data[i] == 0xFF and (data[i + 1] & 0xE0) == 0xE0:
            br = _BITRATES.get(data[i + 2] >> 4)
            sr = _SRATES.get((data[i + 1] >> 2) & 0x03)
            if br and sr:
                pad = (data[i + 2] >> 1) & 1
                dur += 1152 / sr
                parsed += 1
                i += int(144 * br * 1000 / sr) + pad
                continue
        i += 1
    if not parsed:
        return len(data) * 8 / 48 / 1000
    return dur * 1000


def build_course(title, md, ops, out_path):
    """md: 讲义全文；ops: {句子片段: op}，op ∈ highlight/underline/comment(+text)/strike。"""
    lines = md.splitlines()
    # 讲稿 = 标题 + 正文句子（跳过 markdown 标记行与代码块）
    script_src = []
    in_code = False
    for ln in lines:
        if ln.strip().startswith("```"):
            in_code = not in_code
            continue
        if in_code:
            continue
        t = re.sub(r"^#{1,6}\s*|^[-*]\s+|^\d+\.\s+|`|\*\*", "", ln).strip()
        if t and not t.startswith(("|", "!", "---")):
            script_src.extend(split_sentences(t))

    # ── 逐句合成语音，拿精确时长（限制并发，防被服务限流）──
    async def _synth_all():
        sem = asyncio.Semaphore(3)
        return await asyncio.gather(*(synth_sentence(s, sem) for s in script_src))
    pieces = asyncio.run(_synth_all())
    audio_all = b"".join(p[0] for p in pieces)

    # ── 时间轴 ──
    total_chars = len(re.sub(r"\s", "", md))
    states, cursor, clicks, annotations, popups, script = [], [], [], [], [], []
    t = 800.0
    pos = {"x": 0.30, "y": 0.18}
    for si, (sent, (ablob, dur)) in enumerate(zip(script_src, pieces)):
        start, end = t, t + dur
        # 该句在讲义里的深度 → 滚动比例与光标高度（人类式缓慢推进）
        idx = md.find(sent[:12])
        depth = min(1.0, (idx if idx >= 0 else si * 40) / max(1, total_chars))
        sr = round(min(0.96, depth * 0.95), 4)
        states.append({"t": int(start), "sr": sr, "y": 0, "s": None})
        script.append({"t": int(start), "text": sent})

        # 光标滑到本句锚点（弧线：中点向外凸），句内加心跳采样
        target = {"x": round(0.26 + 0.10 * ((si % 3) - 1) + 0.02 * (si % 2), 4),
                  "y": round(0.14 + 0.68 * depth, 4)}
        mid = {"x": round((pos["x"] + target["x"]) / 2 + (0.035 if si % 2 else -0.035), 4),
               "y": round((pos["y"] + target["y"]) / 2 - 0.02, 4)}
        for tt, p in ((start - GAP_MS * 0.6, pos), (start - GAP_MS * 0.25, mid), (start, target)):
            cursor.append({"t": int(tt), "x": p["x"], "y": p["y"]})
        cursor.append({"t": int(start + dur * 0.55), "x": target["x"], "y": target["y"]})
        clicks.append({"t": int(start), "x": target["x"], "y": target["y"]})
        pos = target

        # 标注：讲到才画上去（+400ms），批注配气泡开合事件
        for frag, op in ops.items():
            if frag in sent and not any(a.get("_s") == si for a in annotations):
                a0 = md.find(sent[:10])
                off = md.find(frag, max(0, a0 - 60))
                if off < 0:
                    continue
                ann = {
                    "id": f"ann-demo-{si}-{abs(hash(frag)) % 99999}", "t": int(start + 400),
                    "type": op["kind"], "fi": 0,
                    "quote": frag[:120], "offsets": [off, off + len(frag)], "_s": si,
                }
                if op["kind"] == "comment":
                    ann["text"] = op.get("text", "这里注意一下")
                    popups.append({"t": int(start + 1000), "id": ann["id"], "fi": 0, "open": True})
                    popups.append({"t": int(end + 300), "id": ann["id"], "fi": 0, "open": False})
                states.append({"t": int(start + 400), "sr": sr, "y": 0,
                               "s": {"a": off, "b": off + len(frag)}})
                annotations.append(ann)
        t = end + GAP_MS

    duration_s = t / 1000
    json_data = {
        "v": 3, "app": "LectureLite", "created": "demo",
        "duration": duration_s, "viewport": VIEWPORT,
        "file": {"name": "讲义.md", "mime": "text/markdown"},
        "files": [{"name": "讲义.md", "mime": "text/markdown", "type": "md"}],
        "fileSwitches": [{"t": 0, "fi": 0}],
        "states": sorted(states, key=lambda x: x["t"]),
        "cursor": sorted(cursor, key=lambda x: x["t"]),
        "clicks": sorted(clicks, key=lambda x: x["t"]),
        "annotations": [{k: v for k, v in a.items() if k != "_s"} for a in annotations],
        "annotationPopups": sorted(popups, key=lambda x: x["t"]),
        "script": script,
    }
    out_path.parent.mkdir(exist_ok=True)
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("讲义.md", md)
        z.writestr("audio.mp3", audio_all)
        z.writestr("lecture.json", json.dumps(json_data, ensure_ascii=False))
    print(f"✓ {out_path.name}  {duration_s:.0f}s  {out_path.stat().st_size/1024:.0f}KB  {len(script)} 句")


COURSES = [
    ("Python入门：列表与字典", "Python入门-列表与字典.lecture.zip", """# Python 入门：列表与字典

大家好，今天我们用十分钟认识 Python 里最常用的两种数据结构：列表和字典。

## 列表：一串有序的值

列表用方括号表示，元素之间用逗号隔开。

```python
scores = [92, 85, 78]
scores.append(66)
```

列表是有序的，所以可以用下标访问，`scores[0]` 就是第一个元素 92。

切片是列表最优雅的用法之一，`scores[1:3]` 会取出下标 1 到 2 的元素，注意左闭右开。

## 字典：键值对的映射

字典用花括号表示，每个键对应一个值。

```python
student = {"name": "小明", "age": 18}
```

访问不存在的键会抛出 KeyError，安全的方式是用 `get` 方法，键不存在时返回默认值。

## 什么时候用哪个

记住一句话：关心顺序用列表，关心查找用字典。

字典的查询是常数时间，数据量大时，用字典替代列表查找，性能差距会非常明显。

课后把这两个例子亲手敲一遍，下节课我们讲循环和推导式。
""", {
    "scores[0]": {"kind": "highlight"},
    "左闭右开": {"kind": "underline"},
    "get": {"kind": "comment", "text": "get 的第二参数就是默认值，找不到键时返回它，不会报错"},
    "常数时间": {"kind": "highlight"},
    "关心查找用字典": {"kind": "comment", "text": "这是本节最重要的一句话，面试也常考"},
}),

    ("机器学习初识：梯度下降", "机器学习初识-梯度下降.lecture.zip", """# 机器学习初识：什么是梯度下降

这节课我们用一个下山的故事，搞清楚机器学习最核心的优化算法：梯度下降。

## 一个下山的故事

想象你在浓雾中下山，看不见路，只能感受到脚下的坡度。

最聪明的策略是：每次都朝最陡的下坡方向迈一小步，然后重复。

## 损失函数与梯度

机器学习里，山坡的高度就是损失函数，它衡量模型预测得有多差。

梯度指向损失上升最快的方向，所以我们朝梯度的反方向走。

更新公式是：`w = w - lr * grad`，其中 lr 叫学习率。

## 学习率：步子大小

学习率太小，下山要走很久；学习率太大，可能直接跨过谷底，越走越高。

这就是为什么训练模型时，学习率往往是最先需要调试的超参数。

## 小结

梯度下降 = 沿着梯度的反方向，一小步一小步地降低损失。

深度学习里的所有训练，本质上都是在重复这个简单动作。
""", {
    "最陡的下坡方向": {"kind": "highlight"},
    "w = w - lr * grad": {"kind": "comment", "text": "三个符号分别是：参数、学习率、梯度，后面每节课都会见到"},
    "越走越高": {"kind": "underline"},
    "梯度的反方向": {"kind": "highlight"},
}),

    ("统计学思维：条件概率与贝叶斯", "统计学思维-条件概率与贝叶斯.lecture.zip", """# 统计学思维：条件概率与贝叶斯

今天我们聊一个改变思维方式的知识点：条件概率，以及它最著名的应用，贝叶斯定理。

## 条件概率：信息的价值

条件概率回答的是：在已知 B 发生的前提下，A 发生的概率有多大。

记作 P(A|B)。知道条件之后，概率会被更新，这就是信息的价值。

## 贝叶斯定理

贝叶斯定理把这件事写成了一个公式：

`P(A|B) = P(B|A) * P(A) / P(B)`

它的含义是：后验概率 = 似然 × 先验概率 ÷ 证据。

## 经典例子：疾病检测

某病发病率是千分之一，检测准确率高达 99%。你的检测结果是阳性，你真的得病的概率有多大？

直觉说是 99%，但贝叶斯告诉我们：只有大约 9%。

因为健康人群基数太大，假阳性的绝对数量反而更多。这就是基率谬误。

## 为什么要学它

医生读片、垃圾邮件过滤、搜索引擎排序，背后都是贝叶斯更新。

学会它，你就拥有了在证据面前理性更新信念的能力。
""", {
    "P(A|B)": {"kind": "highlight"},
    "后验概率 = 似然 × 先验概率 ÷ 证据": {"kind": "comment", "text": "建议把这条公式抄一遍，后面医学统计课会反复用到"},
    "只有大约 9%": {"kind": "underline"},
    "基率谬误": {"kind": "highlight"},
}),
]


def main():
    for title, fname, md, ops in COURSES:
        build_course(title, md, ops, WEB_DIR / "demo" / fname)


if __name__ == "__main__":
    main()
