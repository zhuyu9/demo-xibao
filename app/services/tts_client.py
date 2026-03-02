"""阿里云 DashScope TTS 客户端（OpenAI Realtime 协议）。

协议流程：
1. 连接 wss://dashscope.aliyuncs.com/api-ws/v1/realtime?model=<model>
2. 等待 session.created
3. 发送 session.update（voice、format、mode=commit）
4. 等待 session.updated
5. 发送 input_text_buffer.append（文本）
6. 发送 input_text_buffer.commit
7. 接收 response.audio.delta（base64 音频）→ decode → yield bytes
8. 接收 response.audio.done / response.done / session.finished → 结束
"""

import asyncio
import base64
import json
from typing import AsyncIterator

import websockets

from app.core.logger import logger


class TTSError(Exception):
    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


class TTSClient:
    """DashScope 流式 TTS 客户端（OpenAI Realtime 协议）。"""

    def __init__(self, api_key: str, ws_url: str, model: str, voice: str):
        self.api_key = api_key
        self.ws_url = ws_url
        self.model = model
        self.voice = voice

    async def synthesize(self, text: str) -> AsyncIterator[bytes]:
        """合成文字为音频流，yield 二进制 MP3 数据块。"""
        url = f"{self.ws_url}?model={self.model}"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "OpenAI-Beta": "realtime=v1",
        }
        logger.info(f"TTS synthesize: url={url}, voice={self.voice}, text={text!r}")

        async with websockets.connect(url, additional_headers=headers) as ws:
            # 等待 session.created
            raw = await asyncio.wait_for(ws.recv(), timeout=10)
            msg = json.loads(raw)
            if msg.get("type") != "session.created":
                raise TTSError(f"预期 session.created，收到: {msg.get('type')} | {msg}")
            logger.info("TTS session.created")

            # 配置会话：commit 模式，mp3 输出
            await ws.send(json.dumps({
                "type": "session.update",
                "session": {
                    "mode": "commit",
                    "voice": self.voice,
                    "response_format": "mp3",
                    "sample_rate": 24000,
                },
            }))

            # 等待 session.updated
            raw = await asyncio.wait_for(ws.recv(), timeout=10)
            msg = json.loads(raw)
            if msg.get("type") != "session.updated":
                raise TTSError(f"预期 session.updated，收到: {msg.get('type')} | {msg}")
            logger.info("TTS session.updated")

            # 发送文本并提交
            await ws.send(json.dumps({
                "type": "input_text_buffer.append",
                "text": text,
            }))
            await ws.send(json.dumps({"type": "input_text_buffer.commit"}))
            logger.info("TTS text committed")

            # 接收音频数据
            async for message in ws:
                if isinstance(message, bytes):
                    yield message
                    continue

                evt = json.loads(message)
                evt_type = evt.get("type", "")

                if evt_type == "response.audio.delta":
                    b64 = evt.get("delta", "")
                    if b64:
                        yield base64.b64decode(b64)

                elif evt_type in ("response.audio.done", "response.done", "session.finished"):
                    logger.info(f"TTS finished: {evt_type}")
                    break

                elif evt_type == "error":
                    err = evt.get("error", {})
                    raise TTSError(f"TTS 错误: {err.get('message', str(err))}")

                else:
                    logger.debug(f"TTS event: {evt_type}")
