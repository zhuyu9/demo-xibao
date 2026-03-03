from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.services.llm_client import stream_chat

router = APIRouter(prefix="/api/chat")


class ChatRequest(BaseModel):
    text: str
    session_id: str = "default"


async def _sse_generator(text: str, session_id: str):
    async for token in stream_chat(text, thread_id=session_id):
        yield f"data: {token}\n\n"
    yield "data: [DONE]\n\n"


@router.post("/stream")
async def chat_stream(req: ChatRequest):
    return StreamingResponse(
        _sse_generator(req.text, req.session_id),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
