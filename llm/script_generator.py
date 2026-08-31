#!/usr/bin/env python3
"""文稿自动生成工具 - 调用本地大模型生成讲解稿

使用 OpenAI Python SDK，严格遵循 OpenAI 兼容接口。
"""

from openai import OpenAI

# 大模型配置
client = OpenAI(
    base_url="http://10.133.72.161:20133/v1",
    api_key="callmemaybe",
)

MODEL_NAME = "Qwen3.8-27B-BF16"

LENGTH_MAP = {
    "short": "5分钟",
    "medium": "10-15分钟",
    "long": "20-30分钟",
}


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
    clipped = content[:6000]

    system = (
        "你是一位专业的演讲教练和内容策划专家。"
        "请根据提供的文档内容生成一份自然流畅、口语化的讲解稿。\n"
        "要求：\n"
        "1. 纯口语讲解口吻，适合录制讲课视频\n"
        "2. 结构清晰：开场引入 → 主体要点 → 总结升华\n"
        "3. 适当加入过渡语，衔接自然\n"
        "4. 重点突出，避免复述原文，用讲解的方式转述\n"
        "5. 直接输出讲稿正文，不要 Markdown 标题，纯文本分段落即可\n"
        "6. 每段不超过 150 字，方便朗读"
    )

    user = (
        f"请为以下文档内容生成一份约 {duration} 的讲解稿。\n\n"
        f"讲解主题：{topic if topic else '（请根据内容确定合适的主题）'}\n\n"
        f"===== 文档内容 =====\n{clipped}\n===== 结束 =====\n\n"
        "请直接输出讲解稿正文。"
    )

    response = client.chat.completions.create(
        model=MODEL_NAME,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        extra_body={"chat_template_kwargs": {"enable_thinking": False}},
        temperature=0.7,
        max_tokens=2000,
    )

    return response.choices[0].message.content.strip()


def generate_script_stream(content, topic="", length="medium"):
    """根据文档内容流式生成讲解稿。

    Yields:
        逐块生成的文本片段
    """
    duration = LENGTH_MAP.get(length, "10-15分钟")
    clipped = content[:6000]

    system = (
        "你是一位专业的演讲教练和内容策划专家。"
        "请根据提供的文档内容生成一份自然流畅、口语化的讲解稿。\n"
        "要求：\n"
        "1. 纯口语讲解口吻，适合录制讲课视频\n"
        "2. 结构清晰：开场引入 → 主体要点 → 总结升华\n"
        "3. 适当加入过渡语，衔接自然\n"
        "4. 重点突出，避免复述原文，用讲解的方式转述\n"
        "5. 直接输出讲稿正文，不要 Markdown 标题，纯文本分段落即可\n"
        "6. 每段不超过 150 字，方便朗读"
    )

    user = (
        f"请为以下文档内容生成一份约 {duration} 的讲解稿。\n\n"
        f"讲解主题：{topic if topic else '（请根据内容确定合适的主题）'}\n\n"
        f"===== 文档内容 =====\n{clipped}\n===== 结束 =====\n\n"
        "请直接输出讲解稿正文。"
    )

    stream = client.chat.completions.create(
        model=MODEL_NAME,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        extra_body={"chat_template_kwargs": {"enable_thinking": False}},
        temperature=0.7,
        max_tokens=2000,
        stream=True,
    )

    for chunk in stream:
        delta = chunk.choices[0].delta.content
        if delta:
            yield delta


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
