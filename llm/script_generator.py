#!/usr/bin/env python3
"""文稿自动生成工具 - 调用大模型生成讲解稿

使用 OpenAI Python SDK，严格遵循 OpenAI 兼容接口。
模型服务通过环境变量配置（也可用命令行覆盖）：

    LLM_BASE_URL   OpenAI 兼容接口地址（默认 http://10.133.72.161:20133/v1）
    LLM_API_KEY    API Key（默认 callmemaybe）
    LLM_MODEL      模型名（默认 Qwen3.8-27B-BF16）
"""

import os
import re

from openai import OpenAI

# 大模型配置（环境变量可覆盖）
client = OpenAI(
    base_url=os.environ.get("LLM_BASE_URL", "http://10.133.72.161:20133/v1"),
    api_key=os.environ.get("LLM_API_KEY", "callmemaybe"),
)

MODEL_NAME = os.environ.get("LLM_MODEL", "Qwen3.8-27B-BF16")

LENGTH_MAP = {
    "short": "5分钟",
    "medium": "10-15分钟",
    "long": "20-30分钟",
}

# 单次请求的文档上限（字符）。超过时按标题分块，逐块生成再拼接（见 generate_script_timeline_stream）。
MAX_CONTENT_CHARS = 12000

# ── 通用讲解要求（纯文本版与 NDJSON 版提示词共用，保持口径一致）──
COVERAGE_RULES = (
    "【覆盖：讲全讲透，不许跳讲】\n"
    "1. 按文档顺序逐节讲完：文档里每个承载信息的小节（标题、列表项、表格、公式、代码示例、图注、"
    "补充说明）都必须有对应的讲解句；只有纯排版或与前文完全重复的内容可以略过。\n"
    "2. 文档中列出的 N 个问题、N 个步骤、N 种方法或方案，必须逐个展开讲：讲完一个再讲下一个，"
    "每个都要给出实质内容（是什么、为什么、怎么用/什么条件），禁止"
    "“只点名一句就过去”或“后面再说”。\n"
    "3. 一句话只讲一个信息点：先给结论或现象，再给原因、例子或数字；"
    "文档中的关键数字、对比、前提条件、例外情况都要说到位，"
    "禁止用“等等”“诸如此类”含糊带过。\n"
    "4. 用讲师的口吻转述，不要逐字复述原文；可以加简短的过渡衔接，但过渡句不超过全文 10%。\n"
    "5. 篇幅以“讲全”为准：讲稿总字数约为文档字数的 40%~60%；宁可时长略超目标档位，"
    "也不要为了压时长而砍掉重要内容。\n"
)


def _clip(content, with_note=False):
    """截取文档；with_note 时在超长时附一句说明（NDJSON 版用）。"""
    if len(content) > MAX_CONTENT_CHARS:
        clipped = content[:MAX_CONTENT_CHARS]
        if with_note:
            clipped += f"\n（注意：原文较长，这里只提供了前 {MAX_CONTENT_CHARS} 字，请完整覆盖所给内容，结尾不要总结未出现过的部分）"
        return clipped
    return content


def _split_into_chunks(content, limit=MAX_CONTENT_CHARS):
    """按 markdown 标题把文档切成若干不超过 limit 字的块（贪心合并小节）。

    单节超过 limit 时按段落硬切。返回块列表；短文档返回 [content] 原样。
    """
    if len(content) <= limit:
        return [content]
    lines = content.split("\n")
    sections = []
    cur = []
    for ln in lines:
        if re.match(r"^#{1,6}\s", ln) and cur:
            sections.append("\n".join(cur))
            cur = []
        cur.append(ln)
    if cur:
        sections.append("\n".join(cur))
    if len(sections) == 1:
        # 没有标题可切：按硬长度切
        return [content[i:i + limit] for i in range(0, len(content), limit)]

    chunks = []
    buf = ""
    for sec in sections:
        # 单节超长：先把 buf 封块，再硬切该节
        if len(sec) > limit:
            if buf:
                chunks.append(buf)
                buf = ""
            for i in range(0, len(sec), limit):
                chunks.append(sec[i:i + limit])
            continue
        if buf and len(buf) + len(sec) + 1 > limit:
            chunks.append(buf)
            buf = sec
        else:
            buf = (buf + "\n" + sec) if buf else sec
    if buf:
        chunks.append(buf)
    return chunks


def _plain_system_prompt():
    return (
        "你是一位经验丰富的大学讲师和演讲教练。"
        "请根据文档内容生成一份口语化、颗粒度细、真实自然的讲解稿。\n"
        + COVERAGE_RULES +
        "【输出格式】\n"
        "1. 直接输出讲稿正文，口语讲解口吻，每句不超过 120 字，适合朗读\n"
        "2. 不要 Markdown 标题，纯文本分段落即可（一段一个讲解单元，不超过 3 句）\n"
    )


def _plain_user_prompt(duration, topic, doc_text):
    return (
        f"请为以下文档生成一份约 {duration} 的讲解稿（覆盖优先，时长可略超）。\n\n"
        f"讲解主题：{topic if topic else '（请根据内容确定合适的主题）'}\n\n"
        f"===== 文档内容 =====\n{doc_text}\n===== 结束 =====\n\n"
        "请直接输出讲解稿正文。"
    )


def generate_script(content, topic="", length="medium"):
    """根据文档内容生成讲解稿（非流式）。

    Args:
        content: 文档原始文本（Markdown / 纯文本）
        topic: 讲解主题（可选，空则自动提炼）
        length: 篇幅档位 short/medium/long

    Returns:
        生成的讲解稿纯文本
    """
    duration = LENGTH_MAP.get(length, "10-15分钟")
    clipped = _clip(content)

    response = client.chat.completions.create(
        model=MODEL_NAME,
        messages=[
            {"role": "system", "content": _plain_system_prompt()},
            {"role": "user", "content": _plain_user_prompt(duration, topic, clipped)},
        ],
        extra_body={"chat_template_kwargs": {"enable_thinking": False}},
        temperature=0.7,
        max_tokens=16000,
    )

    return response.choices[0].message.content.strip()


def generate_script_stream(content, topic="", length="medium"):
    """根据文档内容流式生成讲解稿。

    Yields:
        逐块生成的文本片段
    """
    duration = LENGTH_MAP.get(length, "10-15分钟")
    clipped = _clip(content)

    stream = client.chat.completions.create(
        model=MODEL_NAME,
        messages=[
            {"role": "system", "content": _plain_system_prompt()},
            {"role": "user", "content": _plain_user_prompt(duration, topic, clipped)},
        ],
        extra_body={"chat_template_kwargs": {"enable_thinking": False}},
        temperature=0.7,
        max_tokens=16000,
        stream=True,
    )

    for chunk in stream:
        delta = chunk.choices[0].delta.content
        if delta:
            yield delta


def _timeline_system_prompt():
    return (
        "你是一位经验丰富的大学讲师和演讲教练。"
        "请把文档内容讲成一份口语化、颗粒度细、真实自然的讲解稿，"
        "以句子为单位逐行输出，并为每句规划画面操作与节奏标记。\n"
        + COVERAGE_RULES +
        "【节奏：思考停顿与重难点标记】\n"
        "6. pause（整数毫秒，可省略）：这句讲完后留给听众思考的停顿时长。"
        "适用场景：抛出问题或反问之后、给出关键结论之后、切换到大节之前；"
        "取值 1200~2500；全篇带 pause 的句子不超过三分之一。\n"
        "7. key（布尔，可省略）：真正的重难点（核心概念、容易误解的点、关键结论或数字）。"
        "全篇 key 句不超过 25%；key 句的 text 里必须自然带出口语提示词，"
        "例如“注意，这里是重点”“这是难点，大家听仔细了”“这句话一定要记住”，"
        "提示要口语化、不突兀，禁止出现“重点标记”之类的元语言。\n"
        "8. 抛问题的句子要像真的在跟学生说话，例如“大家想一想，为什么……？”，让停顿有落点。\n"
        "9. 章节过渡：每次切换到文档的新大节（新标题）时，该节第一句必须是自然的口语过渡句，"
        "明确告诉听众“接下来要讲哪部分”，例如“好，现在我们来看正文”“三个问题摆出来了，下面挨个解决”。"
        "过渡句的 anchor 取新节的标题（或标题下的第一句），并且必须带 pause 1500~2500，给听众一个注意力切换的缓冲。\n"
        "【输出格式】每行一个 JSON 对象（NDJSON），不要 markdown 代码块，"
        "不要 ```json 包裹，不要输出多余文字：\n"
        '{"text":"口语化的讲解句","anchor":"文档中对应的原文片段","pause":1800,"key":true,"op":{"kind":"highlight","target":"文档原文片段"}}\n'
        "字段说明：\n"
        "1. text: 口语讲解口吻，单句不超过 120 字，适合朗读\n"
        "2. anchor: 该句讲解时对应的文档原文片段，必须是文档中出现的连续原文"
        "（用于定位滚动与光标），尽量取完整短语或句子，不要改写\n"
        "3. pause / key: 见节奏要求，均可省略\n"
        "4. op（可省略）：该句朗读时画面上自动执行的标注操作，字段：\n"
        '   - kind: "highlight"（高亮，强调重点句）| "underline"（下划线，标出关键术语或次重点）| "strike"（删除线，标记文档中错误/过时/被否定的内容）| "comment"（批注，最高级别，全文最多 2 处，并在 op.text 里写一句不超过 20 字的批注）\n'
        '   - target: 被标注的文档原文片段，必须逐字复制文档中的连续文字（建议 4~20 字），不要改写、不要跨段落\n'
        '   - text: 仅 kind 为 "comment" 时需要，批注内容\n'
        "5. op 使用要克制：全篇至少三分之二的句子不写 op（没必要的 op 是错误）；"
        "highlight 每篇不超过 8 处；underline 每篇不超过 6 处；"
        "strike 仅当文档确实存在错误或过时表述，每篇不超过 2 处；"
        "comment 全文最多 2 处，只给最重要的知识点。key 句是 highlight/comment 的优先候选。\n"
        "【输出纪律】按讲解顺序逐行输出，每行一个完整 JSON，"
        "不要在 JSON 内部换行，不要输出 JSON 以外的任何内容"
    )


def _timeline_user_prompt(duration_label, topic, doc_text, part_idx=None, part_total=None):
    """NDJSON 版 user 提示词；part_idx/part_total 用于长文档分块（1 起计）。"""
    part_note = ""
    if part_total and part_total > 1:
        if part_idx == 1:
            part_note = (f"【分块说明】原文较长，这里只是第 1/{part_total} 部分，后面还有 {part_total - 1} 部分。"
                         "请完整覆盖本部分并自然结束，不要在结尾做全文总结。\n")
        elif part_idx < part_total:
            part_note = (f"【分块说明】这是原文的第 {part_idx}/{part_total} 部分，前面部分已讲完。"
                         "请直接从本部分的第一节开始讲（第一句仍要是自然的口语过渡句），不要开场白，"
                         "不要复述或总结前面部分的内容，结尾不要全文总结。\n")
        else:
            part_note = (f"【分块说明】这是原文的最后第 {part_idx}/{part_total} 部分，前面部分已讲完。"
                         "请直接从本部分的第一节开始讲，不要开场白，不要复述前面内容；"
                         "本部分讲完后可以用一两句自然的口语收尾。\n")

    return (
        f"请为以下文档生成约 {duration_label} 的讲解稿（覆盖优先，时长可略超）。\n\n"
        f"讲解主题：{topic if topic else '（请根据内容确定合适的主题）'}\n\n"
        f"{part_note}"
        f"===== 文档内容 =====\n{doc_text}\n===== 结束 =====\n\n"
        "请按 NDJSON 格式逐行输出，每行一个 JSON。"
    )


def _openai_stream(messages):
    stream = client.chat.completions.create(
        model=MODEL_NAME,
        messages=messages,
        extra_body={"chat_template_kwargs": {"enable_thinking": False}},
        temperature=0.7,
        max_tokens=16000,
        stream=True,
    )
    for chunk in stream:
        delta = chunk.choices[0].delta.content
        if delta:
            yield delta


def generate_script_timeline_stream(content, topic="", length="medium"):
    """流式生成带时间轴的讲解稿（NDJSON：每行 {text, anchor, pause?, key?, op?}）。

    时间戳由前端按音频时长计算，模型只输出句子、文档锚点、节奏标记（pause/key）与视觉操作。
    op 决定该句朗读时画面上的动作：无操作 / 高亮 / 下划线 / 删除线 / 批注，
    批注级别最高，只给最重要的知识点。target 必须是文档原文的连续子串。

    超过 MAX_CONTENT_CHARS 的文档按标题分块逐块生成再拼接，保证全文覆盖而不是截断。
    Yields: 模型输出的原始文本片段
    """
    duration_label = LENGTH_MAP.get(length, "10-15分钟")
    system = _timeline_system_prompt()

    chunks = _split_into_chunks(content)
    total = len(chunks)
    for i, chunk in enumerate(chunks, start=1):
        user = _timeline_user_prompt(
            duration_label if total == 1 else "对应本部分篇幅",
            topic,
            chunk,
            part_idx=i,
            part_total=total,
        )
        last_char = "\n"
        for piece in _openai_stream([
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]):
            if piece:
                last_char = piece[-1]
                yield piece
        # 每块都是独立的一次模型调用。模型若漏掉末尾换行，直接拼接会把
        # 两个 JSON 对象粘成一行，令前端 NDJSON 解析丢失该句。
        if i < total and last_char not in "\r\n":
            yield "\n"


if __name__ == "__main__":
    # 命令行自测
    test = """# Python 入门
## 变量
Python 变量无需声明类型。
## 控制流
if、for、while 控制程序流程。
## 函数
用 def 定义函数。"""
    print("=== 非流式 ===")
    print(generate_script(test, "Python 入门", "short"))
    print("\n=== 流式 ===")
    for piece in generate_script_stream(test, "Python 入门", "short"):
        print(piece, end="", flush=True)
    print()
