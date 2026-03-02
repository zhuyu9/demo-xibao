"""实时语音识别 WebSocket 端点。"""

import asyncio

from fastapi import WebSocket, WebSocketDisconnect, status
from starlette.websockets import WebSocketState
from fastapi.routing import APIRouter

from app.core.config import get_settings
from app.core.logger import logger
from app.services.speech_client import (
    DashScopeSpeechClient,
    SpeechRecognitionConfig,
    SpeechRecognitionError,
)

router = APIRouter(prefix="/api/speech", tags=["语音识别"])


@router.websocket("/ws")
async def speech_recognition_websocket(websocket: WebSocket) -> None:
    """实时语音识别 WebSocket 端点。

    协议流程:
    1. 客户端连接 WebSocket
    2. 服务端使用预设配置启动识别任务
    3. 客户端发送二进制音频数据（PCM 格式）
    4. 服务端转发识别结果到客户端
    5. 客户端发送 "finish" 文本消息结束识别

    识别结果格式:
    {
        "type": "result",
        "text": "识别的文本",
        "is_final": false,
        "sentence_id": "xxx",
        "begin_time": 0,
        "end_time": 1000
    }
    """
    logger.info(f"WebSocket connection attempt from {websocket.client}")
    await websocket.accept()
    logger.info(f"WebSocket accepted from {websocket.client}")

    client: DashScopeSpeechClient | None = None
    finished_event = asyncio.Event()

    try:
        # 1. 连接阿里云 DashScope
        client = DashScopeSpeechClient(
            api_key=get_settings().DASHSCOPE_API_KEY,
            ws_url=f"{get_settings().DASHSCOPE_WS_URL}?model={get_settings().DASHSCOPE_MODEL}",
        )
        await client.connect()

        # 2. 定义结果回调
        async def send_result(result: dict) -> None:
            if result.get("type") == "finished":
                finished_event.set()
            if websocket.client_state != WebSocketState.CONNECTED:
                return
            try:
                await websocket.send_json(result)
            except (WebSocketDisconnect, RuntimeError):
                # Client closed or close message already sent
                return

        # 3. 启动识别任务（使用服务器预设配置）
        recognition_config = SpeechRecognitionConfig(
            model=get_settings().DASHSCOPE_MODEL,
            format=get_settings().DASHSCOPE_FORMAT,
            sample_rate=get_settings().DASHSCOPE_SAMPLE_RATE,
            vocabulary_id=get_settings().DASHSCOPE_VOCABULARY_ID,
            heartbeat=True,
        )
        await client.start_task(
            recognition_config,
            lambda r: asyncio.create_task(send_result(r)),  # type: ignore[arg-type]
        )

        logger.info(
            "Speech recognition connection established",
            extra={
                "model": get_settings().DASHSCOPE_MODEL,
                "format": get_settings().DASHSCOPE_FORMAT,
                "sample_rate": get_settings().DASHSCOPE_SAMPLE_RATE,
            },
        )

        # 5. 开始转发音频流
        while True:
            try:
                if websocket.client_state != WebSocketState.CONNECTED:
                    break
                message = await websocket.receive()

                if "bytes" in message:
                    # 二进制音频数据，转发到阿里云
                    audio = message["bytes"] or b""
                    if audio:
                        await client.send_audio(audio)
                elif "text" in message:
                    # 文本消息，可能是控制命令
                    text = message["text"]
                    if text == "finish":
                        await client.finish_task()
                        try:
                            await asyncio.wait_for(finished_event.wait(), timeout=3)
                        except asyncio.TimeoutError:
                            try:
                                await websocket.send_json(
                                    {"type": "finished", "task_id": client._task_id}
                                )
                            except Exception:
                                pass
                        try:
                            await websocket.close(code=status.WS_1000_NORMAL_CLOSURE)
                        except Exception:
                            pass
                        break

            except WebSocketDisconnect:
                logger.info("Client disconnected")
                break
            except RuntimeError as e:
                if "disconnect message has been received" in str(e):
                    logger.info("Client disconnected")
                    break
                raise

    except WebSocketDisconnect:
        logger.info("WebSocket disconnected during handshake")
    except SpeechRecognitionError as e:
        logger.error(f"Speech recognition error: {e.message} (status={e.status_code})")
        try:
            if websocket.client_state == WebSocketState.CONNECTED:
                await websocket.send_json({"type": "error", "message": e.message})
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        except Exception:
            pass
    except Exception as e:
        logger.error("WebSocket processing error", exc_info=e)
        try:
            if websocket.client_state == WebSocketState.CONNECTED:
                await websocket.send_json(
                    {"type": "error", "message": "服务端内部错误"}
                )
            await websocket.close(code=status.WS_1011_INTERNAL_ERROR)
        except Exception:
            pass
    finally:
        try:
            await websocket.close(code=status.WS_1000_NORMAL_CLOSURE)
        except Exception:
            pass
        if client:
            await client.close()
