from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.services.llm_client import stream_chat

router = APIRouter(prefix="/api/chat")


class ChatRequest(BaseModel):
    text: str


async def _sse_generator(text: str):
    async for token in stream_chat(text):
        yield f"data: {token}\n\n"
    yield "data: [DONE]\n\n"


@router.post("/stream")
async def chat_stream(req: ChatRequest):
    return StreamingResponse(
        _sse_generator(req.text),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
