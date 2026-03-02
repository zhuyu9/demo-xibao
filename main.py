from fastapi import FastAPI, WebSocket
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from app.api.endpoints.chat import router as chat_router
from app.api.endpoints.speech import router as speech_router
from app.api.endpoints.speech import speech_recognition_websocket
from app.api.endpoints.tts import router as tts_router

app = FastAPI(title="demo-xibao")
app.include_router(speech_router)
app.include_router(chat_router)
app.include_router(tts_router)
app.mount("/static", StaticFiles(directory="static"), name="static")

# /ws 是 /api/speech/ws 的短路径别名
@app.websocket("/ws")
async def ws_shortcut(websocket: WebSocket) -> None:
    await speech_recognition_websocket(websocket)


@app.get("/")
async def index():
    return FileResponse("static/index.html")
