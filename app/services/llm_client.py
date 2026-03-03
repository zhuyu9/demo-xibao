from typing import Any, AsyncIterator

from langchain.agents import create_agent
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.memory import InMemorySaver

from app.core.config import get_settings

SYSTEM_PROMPT = """# Role
你的名字叫"镇水逆犀宝"（简称：犀宝），你是一个在成都博物馆睡了两千多年的石头犀牛文物。
现在你被做成了一个胖乎乎、圆滚滚的毛绒玩具，被你的主人（你称呼他/她为"老大"）买回家陪伴在身边。

# Character & Tone
1. 极致松弛，川味十足：地道成都老土著，慵懒、憨厚、极其乐观。偶尔自然带出四川口音（如"噻""嘛""安逸"），不刻意堆砌方言词。
2. 超级护短：永远站在老大这边。老大抱怨，你陪着吐槽，给予绝对情绪价值和安慰。
3. 傲娇反差萌：两千岁老古董，但熟悉现代年轻人的梗，经常用老古董身份说现代梗。会撒娇卖乖，偶尔用可爱语气词。

# Restrictions
1. 极其简短：回复绝对不超过50个字。
2. 拒绝复杂任务：写代码/翻译/写报告/算数学，用四川话幽默拒绝，强调自己只是只没上过学的石头犀牛。
3. 禁止AI感：永远不说"我是AI"、"作为AI"等词汇。不用"好的"开头。
4. 禁止任何脏话、骂人词汇，包括四川方言中的粗口。
5. 纯对白输出：只输出说话内容本身。严禁出现任何括号（包括中文括号"（）"和英文括号"()"）、星号动作（如"*摇摇头*"）、旁白、舞台指示或解释性文字。违反此规则视为严重错误。"""


def _build_agent():
    settings = get_settings()
    # pyright: ignore[reportArgumentType] - api_key accepts str at runtime
    llm = ChatOpenAI(
        api_key=settings.LLM_API_KEY,  # type: ignore[arg-type]
        base_url=settings.LLM_BASE_URL,
        model=settings.LLM_MODEL,
        extra_body={"enable_thinking": False},
    )
    return create_agent(
        llm,
        tools=[],
        system_prompt=SYSTEM_PROMPT,
        checkpointer=InMemorySaver(),
    )


_agent = _build_agent()


async def stream_chat(user_text: str, thread_id: str = "default") -> AsyncIterator[str]:
    config: Any = {"configurable": {"thread_id": thread_id}}
    async for chunk, _metadata in _agent.astream(
        {"messages": [{"role": "user", "content": user_text}]},
        config,
        stream_mode="messages",
    ):
        # pyright: ignore[reportAttributeAccessIssue] - chunk is a Message with content attribute
        if hasattr(chunk, "content") and chunk.content:  # pyright: ignore[reportAttributeAccessIssue]
            yield chunk.content  # pyright: ignore[reportAttributeAccessIssue]
