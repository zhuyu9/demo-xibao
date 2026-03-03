"""ESP32 设备 WebSocket 端点：ASR → LLM → TTS（PCM）全流程。

协议：
  ESP32 → 后端：二进制 PCM（16kHz Int16）| 文本 "finish"
  后端 → ESP32：
    {"type": "asr", "text": "...", "is_final": bool}   — ASR 实时结果
    {"type": "tts.start", "sample_rate": 24000, "format": "pcm_s16le", "channels": 1}
    <二进制 PCM 帧>                                      — TTS 音频
    {"type": "tts.end"}                                 — TTS 结束，后端关闭连接
"""

import array
import asyncio

from fastapi import WebSocket, WebSocketDisconnect, status
from fastapi.routing import APIRouter
from starlette.websockets import WebSocketState

from app.core.config import get_settings
from app.core.logger import logger
from app.services.llm_client import stream_chat
from app.services.speech_client import (
    DashScopeSpeechClient,
    SpeechRecognitionConfig,
    SpeechRecognitionError,
)
from app.services.tts_client import TTSClient, TTSError

router = APIRouter(prefix="/api/device", tags=["设备"])


@router.websocket("/ws")
async def device_websocket(websocket: WebSocket) -> None:
    """ESP32 设备对话 WebSocket 端点（ASR→LLM→TTS 全流程，PCM 输出）。"""
    await websocket.accept()
    logger.info(f"Device WS connected from {websocket.client}")

    settings = get_settings()
    asr_client: DashScopeSpeechClient | None = None

    try:
        # === 1. ASR 阶段 ===
        asr_client = DashScopeSpeechClient(
            api_key=settings.DASHSCOPE_API_KEY,
            ws_url=f"{settings.DASHSCOPE_WS_URL}?model={settings.DASHSCOPE_MODEL}",
        )
        await asr_client.connect()

        final_text = ""
        finished_event = asyncio.Event()

        async def on_asr_result(result: dict) -> None:
            nonlocal final_text
            rtype = result.get("type")
            if rtype == "finished":
                finished_event.set()
                return
            if rtype == "result":
                if websocket.client_state == WebSocketState.CONNECTED:
                    try:
                        await websocket.send_json(
                            {
                                "type": "asr",
                                "text": result.get("text", ""),
                                "is_final": result.get("is_final", False),
                            }
                        )
                    except Exception:
                        pass
                if result.get("is_final"):
                    final_text = result.get("text", "")

        await asr_client.start_task(
            SpeechRecognitionConfig(
                model=settings.DASHSCOPE_MODEL,
                format=settings.DASHSCOPE_FORMAT,
                sample_rate=settings.DASHSCOPE_SAMPLE_RATE,
                vocabulary_id=settings.DASHSCOPE_VOCABULARY_ID,
                heartbeat=True,
            ),
            lambda r: asyncio.create_task(on_asr_result(r)),  # pyright: ignore[reportArgumentType]
        )

        # 接收来自 ESP32 的 PCM 音频流
        while True:
            if websocket.client_state != WebSocketState.CONNECTED:
                break
            message = await websocket.receive()
            if "bytes" in message:
                audio = message["bytes"] or b""
                if audio:
                    await asr_client.send_audio(audio)
            elif "text" in message:
                if message["text"] == "finish":
                    logger.info("Device WS: finish received, committing ASR")
                    await asr_client.finish_task()
                    try:
                        await asyncio.wait_for(finished_event.wait(), timeout=5)
                    except asyncio.TimeoutError:
                        logger.warning("Device WS: ASR finish timeout")
                    break

        if not final_text:
            # ASR 无结果（录音太短或静音）：仍需发 tts.start+tts.end，
            # 让 ESP32 从 STATE_WAIT_RESULT 走完状态机回到 STATE_READY，
            # 否则 ESP32 在 STATE_WAIT_RESULT 下收到非预期 WS 关闭会进入 STATE_ERROR。
            logger.info("Device WS: no ASR result, signalling ESP32 to reset")
            try:
                await websocket.send_json(
                    {
                        "type": "tts.start",
                        "sample_rate": 24000,
                        "format": "pcm_s16le",
                        "channels": 1,
                        "voice": settings.TTS_VOICE,
                    }
                )
                await websocket.send_json({"type": "tts.end"})
            except Exception:
                pass
            try:
                await websocket.close(code=status.WS_1000_NORMAL_CLOSURE)
            except Exception:
                pass
            return

        logger.info(f"Device WS: ASR result={final_text!r}")

        # === 2. LLM 阶段（收集完整回复再 TTS）===
        tokens: list[str] = []
        async for token in stream_chat(final_text, thread_id="device"):
            tokens.append(token)
        llm_response = "".join(tokens)
        logger.info(f"Device WS: LLM response={llm_response!r}")

        # === 3. TTS 阶段（PCM 输出）===
        tts_client = TTSClient(
            api_key=settings.DASHSCOPE_API_KEY,
            ws_url=settings.TTS_WS_URL,
            model=settings.TTS_MODEL,
            voice=settings.TTS_VOICE,
            response_format="pcm",
        )

        await websocket.send_json(
            {
                "type": "tts.start",
                "sample_rate": 24000,
                "format": "pcm_s16le",
                "channels": 1,
                "voice": settings.TTS_VOICE,
            }
        )

        # 时钟修正流控：以播放速率逐帧发送，防止 ESP32 环形缓冲区溢出/下溢
        # 问题：asyncio.sleep 每次比预期多几 ms（事件循环调度开销），
        #       累积漂移导致发送速率慢于播放速率 → 缓冲区缓慢下溢 → 后半段杂音。
        # 修复：记录发送开始时钟，每帧只在"超前于计划时间"时才 sleep，
        #       落后时立即发下一帧，自动补偿漂移，维持正确平均速率。
        PCM_FRAME_SIZE = 1024  # ~21ms / 帧，匹配 ESP32 播放块大小
        BYTES_PER_SEC = 24000 * 2  # S16LE mono @ 24kHz = 48000 B/s
        # 发完最后一帧后等待 ESP32 预充缓冲区（8192B）排空，再发 tts.end
        PREBUFFER_DRAIN_SEC = 8192 / BYTES_PER_SEC + 0.15  # ~320ms
        # DashScope TTS PCM 振幅偏低（约 10-20% 满量程），放大至接近前端归一化效果
        TTS_GAIN = 3.0

        loop = asyncio.get_running_loop()
        clock_start = loop.time()
        total_bytes = 0
        async for chunk in tts_client.synthesize(llm_response):
            if chunk:
                for i in range(0, len(chunk), PCM_FRAME_SIZE):
                    piece = chunk[i : i + PCM_FRAME_SIZE]
                    piece = _amplify_pcm_s16le(piece, TTS_GAIN)
                    await websocket.send_bytes(piece)
                    total_bytes += len(piece)
                    # 只在超前于计划时刻时 sleep，落后时立即发下一帧
                    target_t = clock_start + total_bytes / BYTES_PER_SEC
                    delay = target_t - loop.time()
                    if delay > 0:
                        await asyncio.sleep(delay)

        # 等缓冲区排空，再发 tts.end，避免 ESP32 在还有数据时收到 WS 关闭
        await asyncio.sleep(PREBUFFER_DRAIN_SEC)
        await websocket.send_json({"type": "tts.end"})
        logger.info(f"Device WS: TTS done, total_bytes={total_bytes}")

        try:
            await websocket.close(code=status.WS_1000_NORMAL_CLOSURE)
        except Exception:
            pass

    except WebSocketDisconnect:
        logger.info("Device WS: client disconnected")
    except SpeechRecognitionError as e:
        logger.error(f"Device WS ASR error: {e.message}")
        try:
            if websocket.client_state == WebSocketState.CONNECTED:
                await websocket.send_json({"type": "error", "message": e.message})
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        except Exception:
            pass
    except TTSError as e:
        logger.error(f"Device WS TTS error: {e.message}")
        try:
            if websocket.client_state == WebSocketState.CONNECTED:
                await websocket.send_json({"type": "error", "message": e.message})
            await websocket.close(code=status.WS_1011_INTERNAL_ERROR)
        except Exception:
            pass
    except Exception as e:
        logger.error("Device WS unexpected error", exc_info=e)
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
        if asr_client:
            await asr_client.close()


def _amplify_pcm_s16le(data: bytes, gain: float) -> bytes:
    """对 S16LE PCM 数据应用线性增益，软限幅防止削波。"""
    if gain == 1.0 or not data:
        return data
    n = len(data) // 2
    arr = array.array("h", data[: n * 2])
    for i, s in enumerate(arr):
        v = int(s * gain)
        if v > 32767:
            v = 32767
        elif v < -32768:
            v = -32768
        arr[i] = v
    return bytes(arr)
