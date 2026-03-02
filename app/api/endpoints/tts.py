from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.core.config import get_settings
from app.core.logger import logger
from app.services.tts_client import TTSClient, TTSError

router = APIRouter(prefix="/api/tts")


class TTSRequest(BaseModel):
    text: str


async def _audio_generator(text: str):
    settings = get_settings()
    client = TTSClient(
        api_key=settings.DASHSCOPE_API_KEY,
        ws_url=settings.TTS_WS_URL,
        model=settings.TTS_MODEL,
        voice=settings.TTS_VOICE,
    )
    try:
        async for chunk in client.synthesize(text):
            yield chunk
    except TTSError as e:
        logger.error(f"TTS error: {e.message}")


@router.post("/stream")
async def tts_stream(req: TTSRequest):
    if not req.text.strip():
        raise HTTPException(status_code=400, detail="text 不能为空")
    return StreamingResponse(
        _audio_generator(req.text),
        media_type="audio/mpeg",
        headers={"Cache-Control": "no-cache"},
    )
