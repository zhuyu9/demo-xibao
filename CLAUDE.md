# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目简介

**犀宝（demo-xibao）** 是成都博物馆镇水逆犀石像的 AI 灵魂验证 Demo。模拟一个两千岁成都犀牛的毛绒玩具：用户按住按钮对着麦克风说话，ASR 识别后由 LLM 生成川味回复并流式展示。

## 常用命令

```bash
# 启动开发服务器
uvicorn main:app --reload --port 8000

# 安装依赖
uv sync

# 添加依赖
uv add <package>
```

访问 http://localhost:8000 查看 Demo 页面。

## 环境配置

项目需要 `.env` 文件，包含：

```
DASHSCOPE_API_KEY=sk-...   # 阿里云 DashScope ASR 的 API Key
llm_api_key=sk-...          # LLM 的 API Key（与 DASHSCOPE_API_KEY 相同）
```

其余配置有默认值，在 `app/core/config.py` 中定义：ASR 默认用 `qwen3-asr-flash-realtime`，LLM 默认用 `qwen-plus`（OpenAI 兼容模式，base_url 指向 DashScope）。

## 架构

### 请求流程

```
浏览器
  │
  ├─[WebSocket /api/speech/ws]──→ speech.py ──→ DashScopeSpeechClient ──→ 阿里云 Realtime ASR
  │   二进制 PCM 音频流（16kHz）                    OpenAI Realtime 协议
  │   ← JSON 识别结果 {type, text, is_final}
  │
  └─[POST /api/chat/stream]──→ chat.py ──→ llm_client.py ──→ 阿里云 DashScope LLM
      {text: "..."}            SSE 流式响应            OpenAI 兼容 API
      ← data: token\n\n ... data: [DONE]\n\n
```

### 目录结构

```
main.py                        # FastAPI 入口，注册路由，挂载 /static
app/
  core/
    config.py                  # Settings（pydantic-settings，读 .env）
    logger.py                  # loguru logger
  api/endpoints/
    speech.py                  # WebSocket 端点 /api/speech/ws（也有 /ws 别名）
    chat.py                    # SSE 端点 POST /api/chat/stream
  services/
    speech_client.py           # DashScopeSpeechClient（OpenAI Realtime 协议封装）
    llm_client.py              # stream_chat()（openai SDK，流式输出）
static/
  index.html                   # 前端 Demo 页（黑色主题，无框架）
  worklets/
    audio-processor.worklet.js # AudioWorklet：采集麦克风 PCM，重采样到 16kHz
```

### 关键设计细节

**ASR 协议**（`speech_client.py`）：DashScope 使用 OpenAI Realtime API 协议（WebSocket），需要在 header 加 `OpenAI-Beta: realtime=v1`。流程：
1. 连接，等待 `session.created`
2. 发送 `session.update`（配置 ASR 模式，关闭 LLM 回复：`create_response: false`）
3. 循环发送 `input_audio_buffer.append`（base64 编码的 PCM）
4. 结束时发 `input_audio_buffer.commit`，等待 `conversation.item.input_audio_transcription.completed`

**前端音频**：AudioWorklet 采集 Float32 PCM，重采样到 16kHz，转换为 Int16 后以二进制 WebSocket 帧发送。

**LLM 人设**（`llm_client.py`中 `SYSTEM_PROMPT`）：犀宝的全部人设约束写在这里，包括角色、语气、字数限制（不超过50字）、拒绝类型等。

**SSE 结束标记**：`data: [DONE]\n\n`，前端据此停止等待。

**`/ws` 别名**：`main.py` 将 `/ws` 直接路由到同一个 `speech_recognition_websocket` handler，方便前端使用短路径。
