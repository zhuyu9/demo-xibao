# ESP32 犀宝固件（XBBtn）

M5Atom（ESP32）硬件端固件，实现按钮触发的语音对话：按下录音 → ASR → LLM → TTS → 播放。

## 硬件要求

- **主控**：M5Atom Matrix 或 M5Atom Lite（ESP32 PICO-D4）
- **音频**：M5Atom 内置 PDM 麦克风（GPIO23）+ 外接 I2S 扬声器

| I2S 引脚 | GPIO |
|---|---|
| BCK（位时钟）| 19 |
| LRCK（左右声道） | 33 |
| DATA OUT（扬声器）| 22 |
| DATA IN（麦克风）| 23 |

## 依赖库

Arduino IDE 库管理器中安装：

| 库 | 版本 |
|---|---|
| M5Atom | 最新 |
| ArduinoWebsockets | 最新 |
| ArduinoJson | ≥ 6.x |

Board：`M5Stack-ATOM`（需先安装 M5Stack Arduino 板包）

## 配置

烧录前修改 `XBBtn.ino` 顶部：

```cpp
// WiFi
const char *WifiSSID = "your-ssid";
const char *WifiPWD  = "your-password";

// Toy Cloud backend（局域网 IP，需与 ESP32 在同一网段）
#define TOY_CLOUD_HOST "192.168.x.x"
#define TOY_CLOUD_PORT 8080
#define TOY_CLOUD_WS_PATH "/v1/toy/audio-stream"

#define DEVICE_ID "toy_000123"
#define CHARACTER_ID "shixi"
#define FIRMWARE_VERSION "esp32-xbbtn-0.2.0"
```

后端启动命令：

```bash
cd /Volumes/x5/projects/cdm/xi/toy-cloud
TOY_CLOUD_AUTH_MODE=disabled TOY_CLOUD_LLM_PROVIDER=fake go run ./cmd/toy-cloud
```

## 使用方法

1. 上电后 LED **蓝色**（IDLE）→ 连接 WiFi → **绿色**（READY）
2. **按住**按钮开始录音，LED **黄色**（RECORDING）
3. **松开**按钮结束录音，LED **青色**（WAIT\_RESULT）
4. 后端完成 ASR→LLM→TTS，LED **品红**（PLAYBACK），扬声器播放回复
5. 播放完毕自动回到**绿色**（READY）

| LED 颜色 | 状态 |
|---|---|
| 蓝色 | IDLE（启动/WiFi 连接中）|
| 绿色 | READY（待机）|
| 黄色 | RECORDING（录音中）|
| 青色 | WAIT\_RESULT（等待后端）|
| 品红 | PLAYBACK（播放中）|
| 红色 | ERROR |

## 会话与认证

1. 固件将 `session_id` 存入 ESP32 NVS `Preferences`，下次 `session.start` 时携带，重启后也会保留。
2. 开发环境使用 `TOY_CLOUD_AUTH_MODE=disabled`。
3. 试点版 HMAC 认证使用 `TOY_AUTH_MODE=AUTH_MODE_HMAC` 和 `TOY_DEVICE_SECRET`。
4. 完成 NTP 同步后，HMAC 请求携带 `X-Toy-Device-Id`、`X-Toy-Timestamp`、`X-Toy-Signature`。

## 后端协议（WebSocket `/v1/toy/audio-stream`）

```
ESP32 → Toy Cloud：
  {"type":"session.start",...}   开始 push-to-talk 语音会话
  {"type":"audio.append",...}    base64 PCM 16kHz Int16 mono 音频块，seq 递增
  {"type":"audio.finish",...}    录音结束

Toy Cloud → ESP32：
  {"type":"asr.partial",...}     ASR 中间结果
  {"type":"asr.final",...}       ASR 最终结果
  {"type":"assistant.reply_text","reply_text":"...","session_id":"..."}  回复文本和新 session_id
  {"type":"assistant.audio_start",...}  PCM 16kHz Int16 mono 播放格式
  {"type":"assistant.audio_delta",...}  base64 PCM 音频块
  {"type":"assistant.audio_done"}       音频结束
  {"type":"error","code":"...","message":"...","retryable":bool}  错误
```

## 麦克风调参

`XBBtn.ino` 顶部常量：

| 常量 | 默认值 | 说明 |
|---|---|---|
| `MIC_CAPTURE_GAIN` | 4.8 | 前端增益（PDM 原始信号偏低）|
| `MIC_SOFT_LIMIT` | 28000 | 软限幅阈值（防削波）|
| `MIC_NOISE_GATE` | 12 | 噪声门阈值（抑制背景噪音）|
| `MIC_FADE_IN_SAMPLES` | 640 | 淡入样本数（40ms @ 16kHz）|
| `MIC_FADE_OUT_SAMPLES` | 320 | 淡出样本数（20ms @ 16kHz）|

串口（115200 baud）会实时打印每秒麦克风统计，包含 `in_peak`、`out_peak`、`gate%` 等指标，可用于调参参考。
