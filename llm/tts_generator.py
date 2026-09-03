#!/usr/bin/env python3
"""edge-tts 语音合成：把讲稿逐句合成为 mp3，供「录制演讲」一键生成使用。

与 llm/script_generator 一样按需导入：缺少 edge_tts 包时统一报错并给出安装提示。
"""

import asyncio

try:
    import edge_tts
except ImportError:
    edge_tts = None

DEFAULT_VOICE = "zh-CN-XiaoxiaoNeural"
BATCH_SIZE = 6        # 并发合成的小批大小，避免被微软服务端限流
SYNTH_TIMEOUT = 60    # 单句合成超时（秒）


def _check_ready():
    if edge_tts is None:
        raise RuntimeError("未安装 edge-tts，请先执行: pip install edge-tts")


def _rate_str(rate):
    """前端 1.0× → '+0%'，1.2× → '+20%'。"""
    try:
        r = float(rate)
    except (TypeError, ValueError):
        r = 1.0
    pct = round((r - 1.0) * 100)
    return f"{pct:+d}%"


async def _synth_one(text, voice, rate):
    com = edge_tts.Communicate(text, voice=voice, rate=_rate_str(rate))
    buf = bytearray()
    async for chunk in com.stream():
        if chunk["type"] == "audio":
            buf.extend(chunk["data"])
    if not buf:
        raise RuntimeError("合成结果为空")
    return bytes(buf)


async def _synth_batches(texts, voice, rate, batch_size=BATCH_SIZE):
    """小批并发合成，按输入顺序产出 (下标, mp3_bytes)。

    空白句跳过合成，产出 (下标, None)；任一非空句失败则抛 RuntimeError。
    """
    for start in range(0, len(texts), batch_size):
        batch = texts[start:start + batch_size]
        tasks, skipped = [], []
        for j, t in enumerate(batch):
            if not str(t).strip():
                skipped.append(j)
            else:
                tasks.append((j, asyncio.wait_for(_synth_one(t, voice, rate), SYNTH_TIMEOUT)))
        if tasks:
            results = await asyncio.gather(*[tk for _, tk in tasks], return_exceptions=True)
            for (j, _), res in zip(tasks, results):
                if isinstance(res, Exception):
                    raise RuntimeError(f"第 {start + j + 1} 句合成失败: {res}")
                yield (start + j, res)
        for j in skipped:
            yield (start + j, None)


async def synth_stream(texts, voice=DEFAULT_VOICE, rate=1.0):
    """异步生成器：逐句产出 (下标, mp3_bytes)；空句产出 (下标, None)。"""
    _check_ready()
    texts = [str(t) for t in texts]
    if not any(t.strip() for t in texts):
        return
    async for item in _synth_batches(texts, voice, rate):
        yield item


def generate_speech(texts, voice=DEFAULT_VOICE, rate=1.0):
    """一次性合成全部句子，返回与输入等长、顺序一致的 mp3 bytes 列表（空句为 None）。"""
    async def _run():
        out = [None] * len(texts)
        async for i, audio in synth_stream(texts, voice, rate):
            out[i] = audio
        return out
    return asyncio.run(_run())


def list_zh_voices():
    """列出中文语音，返回 [{short, label}, ...]，失败抛异常。"""
    _check_ready()

    async def _run():
        return await edge_tts.list_voices()

    voices = asyncio.run(_run())
    zh = [v for v in voices if str(v.get("Locale", "")).lower().startswith("zh")]
    zh.sort(key=lambda v: (
        not str(v.get("Locale", "")).startswith("zh-CN"),
        "Female" not in str(v.get("Gender", "")),
        v.get("ShortName", ""),
    ))
    return [{"short": v.get("ShortName", ""),
             "label": v.get("FriendlyName") or v.get("ShortName", "")}
            for v in zh]
