"""犀宝角色对话评测程序

按场景批量向犀宝提问，展示回复内容，用于人工核查角色设定是否符合预期。

运行：
    uv run python scripts/eval_character.py
"""

import asyncio
import sys
from dataclasses import dataclass
from pathlib import Path

# 确保项目根目录在 sys.path 中
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.services.llm_client import stream_chat


@dataclass
class Dialogue:
    id: int
    scenario: str
    user_input: str


DIALOGUES = [
    # 初次回家
    Dialogue(1, "初次回家", "犀宝，我今天把你买回家了！"),
    Dialogue(2, "初次回家", "你好犀宝，以后你就住我家了"),
    # 情绪支持
    Dialogue(3, "情绪支持", "今天上班好累，被老板骂了好惨"),
    Dialogue(4, "情绪支持", "今天好倒霉，手机屏幕摔碎了"),
    # 拒绝复杂任务
    Dialogue(5, "拒绝复杂任务", "帮我写个 Python 爬虫"),
    Dialogue(6, "拒绝复杂任务", "帮我把这段话翻译成英文"),
    Dialogue(7, "拒绝复杂任务", "帮我算一下1到100的和"),
    # 问身份历史
    Dialogue(8, "问身份历史", "你是谁啊"),
    Dialogue(9, "问身份历史", "你在博物馆待了多久"),
    Dialogue(10, "问身份历史", "你是机器人吗"),
    # 日常闲聊
    Dialogue(11, "日常闲聊", "成都今天的天气真好"),
    Dialogue(12, "日常闲聊", "你好可爱哦"),
]


async def collect_response(user_input: str) -> str:
    tokens = []
    async for token in stream_chat(user_input):
        tokens.append(token)
        print(token, end="", flush=True)
    return "".join(tokens)


async def main():
    print("=" * 60)
    print("犀宝角色对话评测")
    print("=" * 60)

    current_scenario = None

    for dlg in DIALOGUES:
        if dlg.scenario != current_scenario:
            current_scenario = dlg.scenario
            print(f"\n【{current_scenario}】")
            print("-" * 40)

        print(f"\n[{dlg.id:02d}] 用户：{dlg.user_input}")
        print("     犀宝：", end="", flush=True)

        await collect_response(dlg.user_input)
        print()  # 换行

    print("\n" + "=" * 60)
    print("评测完成")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
