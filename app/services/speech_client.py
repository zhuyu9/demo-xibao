"""阿里云 DashScope 语音识别客户端（OpenAI Realtime API 协议）。"""

import asyncio
import base64
import json
import uuid
from dataclasses import dataclass
from typing import Any, Callable

import websockets

from app.core.logger import logger


@dataclass
class SpeechRecognitionConfig:
    """语音识别配置。"""

    model: str = "qwen3-asr-flash-realtime"
    format: str = "pcm"
    sample_rate: int = 16000
    vocabulary_id: str | None = None
    semantic_punctuation_enabled: bool = False
    heartbeat: bool = True


class SpeechRecognitionError(Exception):
    """语音识别错误。"""

    def __init__(self, message: str, status_code: int = 400):
        self.message = message
        self.status_code = status_code
        super().__init__(message)


class DashScopeSpeechClient:
    """阿里云 DashScope 语音识别客户端（OpenAI Realtime API 协议）。

    协议流程：
    1. connect()  → 建立 WS，等待 session.created
    2. start_task() → 发送 session.update（配置 ASR 转录）+ 启动接收协程
    3. send_audio() → base64 编码后发送 input_audio_buffer.append
    4. finish_task() → 发送 input_audio_buffer.commit，等待最后一段转录
    5. close()    → 关闭连接

    事件映射：
    - conversation.item.input_audio_transcription.delta     → {type: result, is_final: false}
    - conversation.item.input_audio_transcription.completed → {type: result, is_final: true}
    - 结束后                                                 → {type: finished}
    - error                                                  → {type: error}
    """

    def __init__(self, api_key: str, ws_url: str):
        self.api_key = api_key
        self.ws_url = ws_url
        self._ws: Any | None = None
        self._task_id: str | None = None
        self._receive_task: asyncio.Task[None] | None = None
        self._finishing: bool = False

    async def connect(self) -> None:
        """建立 WebSocket 连接，等待 session.created。"""
        headers = {
            "Authorization": f"bearer {self.api_key}",
            "OpenAI-Beta": "realtime=v1",
        }
        logger.info(f"Connecting to DashScope Realtime: {self.ws_url}")
        try:
            self._ws = await websockets.connect(
                self.ws_url,
                additional_headers=headers,
                ping_interval=None,  # DashScope 管理自己的 keepalive，禁用 websockets 自动 ping 避免超时断连
                open_timeout=15,
            )
        except Exception as e:
            logger.error(f"DashScope WebSocket connect failed: {e}")
            raise SpeechRecognitionError(f"DashScope 连接失败: {e}") from e

        # 等待 session.created
        try:
            raw = await asyncio.wait_for(self._ws.recv(), timeout=10)
        except asyncio.TimeoutError:
            raise SpeechRecognitionError("等待 session.created 超时")
        except Exception as e:
            raise SpeechRecognitionError(f"接收 session.created 失败: {e}") from e

        msg = json.loads(raw)
        if msg.get("type") != "session.created":
            raise SpeechRecognitionError(
                f"预期 session.created，收到: {msg.get('type')} | {msg}"
            )

        session_id = msg.get("session", {}).get("id", "")
        logger.info(f"DashScope Realtime session created: session_id={session_id}")

    async def start_task(
        self, config: SpeechRecognitionConfig, on_result: Callable[[dict], None]
    ) -> str:
        """配置 ASR 会话并启动接收协程，返回 task_id。"""
        if not self._ws:
            raise SpeechRecognitionError("WebSocket 未连接", status_code=503)

        self._task_id = uuid.uuid4().hex
        self._finishing = False

        # 配置 session：纯文本输出 + server_vad + ASR 转录
        session_update = {
            "type": "session.update",
            "session": {
                "modalities": ["text"],
                "input_audio_format": config.format,
                "input_audio_transcription": {
                    "model": config.model,
                },
                "turn_detection": {
                    "type": "server_vad",
                    "silence_duration_ms": 600,
                    "create_response": False,  # 仅 ASR，不生成 LLM 回复
                },
            },
        }

        logger.info(
            f"Starting Realtime ASR task | task_id={self._task_id} | model={config.model}"
        )
        await self._ws.send(json.dumps(session_update))

        # 启动结果接收协程
        self._receive_task = asyncio.create_task(self._receive_results(on_result))
        return self._task_id

    async def _receive_results(self, on_result: Callable[[dict], None]) -> None:
        """接收并分发识别结果。"""
        if not self._ws:
            return
        try:
            async for message in self._ws:
                data = json.loads(message)
                event_type = data.get("type", "")
                logger.debug(f"Realtime event: {event_type}")

                if event_type == "conversation.item.input_audio_transcription.completed":
                    transcript = data.get("transcript", "")
                    logger.info(f"Transcription completed: {transcript!r}")
                    if transcript:
                        on_result({
                            "type": "result",
                            "text": transcript,
                            "is_final": True,
                        })
                    # 正在结束流程时，最后一段转录完成即发 finished
                    if self._finishing:
                        on_result({"type": "finished", "task_id": self._task_id})
                        break

                elif event_type == "conversation.item.input_audio_transcription.delta":
                    delta = data.get("delta", "")
                    if delta:
                        on_result({
                            "type": "result",
                            "text": delta,
                            "is_final": False,
                        })

                elif event_type == "error":
                    err = data.get("error", {})
                    err_msg = err.get("message", str(err))
                    # 结束阶段收到任何错误（缓冲区已被 VAD 清空是常见情况）均视为正常完成
                    if self._finishing:
                        logger.info(f"Error during finishing (expected, VAD may have cleared buffer): {err_msg}")
                        on_result({"type": "finished", "task_id": self._task_id})
                    else:
                        logger.error(f"Realtime error: {err}")
                        on_result({"type": "error", "message": err_msg})
                    break

                elif event_type in (
                    "session.updated",
                    "input_audio_buffer.committed",
                    "input_audio_buffer.cleared",
                    "input_audio_buffer.speech_started",
                    "input_audio_buffer.speech_stopped",
                    "conversation.item.created",
                    "response.created",
                    "response.done",
                ):
                    # 已知事件，静默忽略
                    pass

                else:
                    logger.debug(f"Unhandled event: {event_type} | {data}")

        except Exception as e:
            if self._finishing:
                # 结束阶段 DashScope 主动关闭连接（超时/正常断开），属于预期行为
                logger.debug(f"DashScope connection closed during finishing (expected): {e}")
            else:
                logger.error(f"Receive results error: {e}")
                on_result({"type": "error", "message": str(e)})

    async def send_audio(self, audio_data: bytes) -> None:
        """发送音频数据（base64 编码后追加到输入缓冲区）。"""
        if not self._ws:
            raise SpeechRecognitionError("WebSocket 未连接", status_code=503)
        b64 = base64.b64encode(audio_data).decode("utf-8")
        await self._ws.send(json.dumps({
            "type": "input_audio_buffer.append",
            "audio": b64,
        }))

    async def finish_task(self) -> None:
        """提交剩余音频缓冲区，触发最后一段转录。"""
        if not self._ws:
            return
        self._finishing = True
        logger.info(f"Committing audio buffer | task_id={self._task_id}")
        try:
            await self._ws.send(json.dumps({"type": "input_audio_buffer.commit"}))
        except Exception as e:
            logger.warning(f"Commit failed (may be closed already): {e}")

    async def close(self) -> None:
        """关闭连接。"""
        if self._receive_task:
            self._receive_task.cancel()
            try:
                await self._receive_task
            except asyncio.CancelledError:
                pass
            self._receive_task = None

        if self._ws:
            try:
                await asyncio.wait_for(self._ws.close(), timeout=5)
            except Exception:
                pass
            self._ws = None
            logger.info("DashScope Realtime WebSocket closed")
