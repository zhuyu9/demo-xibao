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

// demo-xibao 后端（局域网 IP，需与 ESP32 在同一网段）
#define BFF_SERVER_HOST "192.168.x.x"
#define BFF_SERVER_PORT 8000
```

后端启动命令：

```bash
uvicorn main:app --reload --port 8000 --host 0.0.0.0
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

## 后端协议（WebSocket `/api/device/ws`）

```
ESP32 → 后端：
  <二进制>      PCM 16kHz Int16 mono 音频块
  "finish"      录音结束信号

后端 → ESP32：
  {"type":"asr","text":"...","is_final":bool}        ASR 实时结果
  {"type":"tts.start","sample_rate":24000,
   "format":"pcm_s16le","channels":1,"voice":"..."}  TTS 开始
  <二进制>                                            PCM 24kHz Int16 mono 音频
  {"type":"tts.end"}                                 TTS 结束
  {"type":"error","message":"..."}                   错误
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
