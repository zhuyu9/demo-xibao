#include <WiFi.h>
#include <driver/i2s.h>

#include "M5Atom.h"
#include <ArduinoWebsockets.h>
#include <ArduinoJson.h>
#include <string>

const char *WifiSSID = "Z-HOME";
const char *WifiPWD  = "perfect56";

// demo-xibao 后端配置（局域网 IP + 端口）
#define BFF_SERVER_HOST "192.168.3.214"
#define BFF_SERVER_PORT 8000
#define DEVICE_WS_PATH  "/api/device/ws"

#define CONFIG_I2S_BCK_PIN     19
#define CONFIG_I2S_LRCK_PIN    33
#define CONFIG_I2S_DATA_PIN    22
#define CONFIG_I2S_DATA_IN_PIN 23

#define SPEAK_I2S_NUMBER I2S_NUM_0

#define MODE_MIC 0
#define MODE_SPK 1
#define I2S_DMA_BUF_COUNT 6
#define I2S_DMA_BUF_LEN 60

enum DeviceState {
    STATE_IDLE,
    STATE_READY,
    STATE_RECORDING,
    STATE_WAIT_RESULT,
    STATE_PLAYBACK,
    STATE_ERROR,
};

websockets::WebsocketsClient ws_client;
volatile bool isWebSocketConnected = false;
DeviceState g_state = STATE_IDLE;

bool g_wsIntentionalClose = false;
unsigned long g_lastWsActivityAtMs = 0;
unsigned long g_lastWsConnectAttemptAtMs = 0;
static const unsigned long WS_IDLE_DISCONNECT_MS = 15000;
static const unsigned long WS_RECONNECT_BACKOFF_MS = 1500;

#define I2S_READ_CHUNK_SIZE 1024
static uint8_t i2s_read_buffer[I2S_READ_CHUNK_SIZE];
int g_currentI2SMode = MODE_SPK;
bool g_i2sInstalled = false;
int g_speakerSampleRate = 24000;

// Microphone capture tuning (long-term quality baseline)
// 1) Gain boosts low-level input from PDM mic.
// 2) Soft limiter prevents hard clipping after gain.
// 3) Noise gate reduces background hiss during silence.
// Increase front-end gain because raw PDM input level is consistently low in real runs.
static const float MIC_CAPTURE_GAIN = 4.8f;
static const int16_t MIC_SOFT_LIMIT = 28000;
// Keep this low: hard gate at high threshold will chop syllables and hurt ASR.
static const int16_t MIC_NOISE_GATE = 12;
// Instead of muting to zero, attenuate low-level region to preserve continuity.
static const uint8_t MIC_GATE_ATTENUATION_SHIFT = 1;  // /2
static const unsigned long MIC_STATS_PRINT_INTERVAL_MS = 1000;
static const uint32_t MIC_FADE_IN_SAMPLES = 640;      // 40 ms @ 16 kHz
static const uint32_t MIC_FADE_OUT_SAMPLES = 320;     // 20 ms @ 16 kHz
static const size_t MIC_PRIME_DROP_BYTES = 1280;      // 40 ms @ 16 kHz, s16 mono
static const float MIC_HPF_ALPHA = 0.985f;            // remove low-frequency plosive thump

struct MicCaptureStats {
    uint32_t sampleCount;
    uint32_t gateHitCount;
    uint32_t nearLimitCount;
    uint32_t hardClipCount;
    uint64_t inAbsSum;
    uint64_t outAbsSum;
    uint16_t inPeak;
    uint16_t outPeak;
};

MicCaptureStats g_micStats = {0};
unsigned long g_lastMicStatsPrintAtMs = 0;
uint32_t g_micSessionSampleIndex = 0;
size_t g_micPrimeDropBytesRemaining = 0;
float g_micHpfPrevIn = 0.0f;
float g_micHpfPrevOut = 0.0f;

#define SPEAKER_RING_BUFFER_SIZE 65536
#define SPEAKER_PLAY_CHUNK_SIZE 2048
#define SPEAKER_PREBUFFER_BYTES 8192
static uint8_t g_speakerRing[SPEAKER_RING_BUFFER_SIZE];
volatile size_t g_speakerHead = 0;
volatile size_t g_speakerTail = 0;
volatile bool g_ttsActive = false;
volatile size_t g_ttsBytesInCurrent = 0;
volatile bool g_playbackStarted = false;
volatile bool g_ttsPcmReady = true;
bool g_pcmCarryValid = false;
uint8_t g_pcmCarryByte = 0;

size_t speakerRingUsed() {
    if (g_speakerHead >= g_speakerTail) {
        return g_speakerHead - g_speakerTail;
    }
    return SPEAKER_RING_BUFFER_SIZE - (g_speakerTail - g_speakerHead);
}

size_t speakerRingFree() {
    // Keep 1 byte gap to distinguish full vs empty.
    return SPEAKER_RING_BUFFER_SIZE - speakerRingUsed() - 1;
}

void speakerRingReset() {
    g_speakerHead = 0;
    g_speakerTail = 0;
}

void enqueueSpeakerPcm(const uint8_t *data, size_t len) {
    if (len == 0) {
        return;
    }
    size_t freeBytes = speakerRingFree();
    if (len > freeBytes) {
        size_t drop = len - freeBytes;
        // 向上取整到 2 字节边界，保证 S16LE 对齐。
        // 若 drop 为奇数，tail 会偏移到奇数位置，导致后续所有采样高低字节颠倒，声音变成噪音。
        drop = (drop + 1) & ~((size_t)1);
        g_speakerTail = (g_speakerTail + drop) % SPEAKER_RING_BUFFER_SIZE;
    }
    for (size_t i = 0; i < len; ++i) {
        g_speakerRing[g_speakerHead] = data[i];
        g_speakerHead = (g_speakerHead + 1) % SPEAKER_RING_BUFFER_SIZE;
    }
    g_ttsBytesInCurrent += len;
}

void enqueueSpeakerPcmS16(const uint8_t *data, size_t len) {
    if (len == 0) {
        return;
    }
    if (g_pcmCarryValid) {
        uint8_t pair[2] = {g_pcmCarryByte, data[0]};
        enqueueSpeakerPcm(pair, 2);
        g_pcmCarryValid = false;
        data += 1;
        len -= 1;
    }
    size_t evenLen = len & ~((size_t)1);
    if (evenLen > 0) {
        enqueueSpeakerPcm(data, evenLen);
    }
    if ((len & 1) == 1) {
        g_pcmCarryByte = data[len - 1];
        g_pcmCarryValid = true;
    }
}

bool InitI2SSpeakOrMic(int mode) {
    esp_err_t err = ESP_OK;

    if (g_i2sInstalled) {
        i2s_driver_uninstall(SPEAK_I2S_NUMBER);
        g_i2sInstalled = false;
    }
    i2s_config_t i2s_config = {
        .mode        = (i2s_mode_t)(I2S_MODE_MASTER),
        .sample_rate = (mode == MODE_MIC) ? 16000 : g_speakerSampleRate,
        .bits_per_sample = I2S_BITS_PER_SAMPLE_16BIT,
        .channel_format = I2S_CHANNEL_FMT_ALL_RIGHT,
#if ESP_IDF_VERSION > ESP_IDF_VERSION_VAL(4, 1, 0)
        .communication_format = I2S_COMM_FORMAT_STAND_I2S,
#else
        .communication_format = I2S_COMM_FORMAT_I2S,
#endif
        .intr_alloc_flags = ESP_INTR_FLAG_LEVEL1,
        .dma_buf_count    = I2S_DMA_BUF_COUNT,
        .dma_buf_len      = I2S_DMA_BUF_LEN,
        .use_apll = false,
        .tx_desc_auto_clear = true,
    };

    if (mode == MODE_MIC) {
        i2s_config.mode = (i2s_mode_t)(I2S_MODE_MASTER | I2S_MODE_RX | I2S_MODE_PDM);
    } else {
        i2s_config.mode = (i2s_mode_t)(I2S_MODE_MASTER | I2S_MODE_TX);
    }

    err = i2s_driver_install(SPEAK_I2S_NUMBER, &i2s_config, 0, NULL);
    if (err != ESP_OK) {
        Serial.printf("i2s_driver_install error: %d\n", err);
        return false;
    }
    g_i2sInstalled = true;

    i2s_pin_config_t tx_pin_config;
#if (ESP_IDF_VERSION > ESP_IDF_VERSION_VAL(4, 3, 0))
    tx_pin_config.mck_io_num = I2S_PIN_NO_CHANGE;
#endif
    tx_pin_config.bck_io_num   = CONFIG_I2S_BCK_PIN;
    tx_pin_config.ws_io_num    = CONFIG_I2S_LRCK_PIN;
    tx_pin_config.data_out_num = CONFIG_I2S_DATA_PIN;
    tx_pin_config.data_in_num  = CONFIG_I2S_DATA_IN_PIN;

    err = i2s_set_pin(SPEAK_I2S_NUMBER, &tx_pin_config);
    if (err != ESP_OK) {
        Serial.printf("i2s_set_pin error: %d\n", err);
        return false;
    }

    int sampleRate = (mode == MODE_MIC) ? 16000 : g_speakerSampleRate;
    err = i2s_set_clk(SPEAK_I2S_NUMBER, sampleRate, I2S_BITS_PER_SAMPLE_16BIT, I2S_CHANNEL_MONO);
    if (err != ESP_OK) {
        Serial.printf("i2s_set_clk error: %d\n", err);
        return false;
    }

    if (mode == MODE_MIC) {
        i2s_zero_dma_buffer(SPEAK_I2S_NUMBER);
    }

    g_currentI2SMode = mode;
    return true;
}

void setState(DeviceState next) {
    g_state = next;
    switch (next) {
        case STATE_IDLE:
            M5.dis.drawpix(0, CRGB(0, 0, 64));
            break;
        case STATE_READY:
            M5.dis.drawpix(0, CRGB(0, 255, 0));
            break;
        case STATE_RECORDING:
            M5.dis.drawpix(0, CRGB(255, 255, 0));
            break;
        case STATE_WAIT_RESULT:
            M5.dis.drawpix(0, CRGB(0, 128, 255));
            break;
        case STATE_PLAYBACK:
            M5.dis.drawpix(0, CRGB(255, 0, 255));
            break;
        default:
            M5.dis.drawpix(0, CRGB(255, 0, 0));
            break;
    }
}

bool ensureI2SMode(int mode) {
    if (g_currentI2SMode == mode) {
        return true;
    }
    return InitI2SSpeakOrMic(mode);
}

bool connectWifi() {
    WiFi.mode(WIFI_STA);
    WiFi.setSleep(false);
    WiFi.begin(WifiSSID, WifiPWD);

    Serial.println("Connecting to WiFi...");
    setState(STATE_IDLE);

    unsigned long start = millis();
    while (WiFi.status() != WL_CONNECTED) {
        delay(500);
        Serial.print('.');
        if (millis() - start > 20000) {
            Serial.println("\nWiFi Connection Failed");
            setState(STATE_ERROR);
            return false;
        }
    }

    Serial.println("\nWiFi connected");
    Serial.print("IP: ");
    Serial.println(WiFi.localIP());
    return true;
}


void onWebsocketMessage(websockets::WebsocketsMessage message);
void onWebsocketEvent(websockets::WebsocketsEvent event, String data);
void resetMicCaptureStats();
void printMicCaptureStats(const char *tag);
void maybePrintMicCaptureStats();
void applyMicFadeOutTail(uint8_t *data, size_t len);
void markWsActivity();
bool isWsConnectionRequired(bool buttonPressed);
void closeDeviceWebSocketIfOpen(const char *reason);

void markWsActivity() {
    g_lastWsActivityAtMs = millis();
}

bool isWsConnectionRequired(bool buttonPressed) {
    if (buttonPressed && g_state == STATE_READY) {
        return true;
    }
    return g_state == STATE_RECORDING ||
           g_state == STATE_WAIT_RESULT ||
           g_state == STATE_PLAYBACK ||
           g_ttsActive;
}

void closeDeviceWebSocketIfOpen(const char *reason) {
    if (g_wsIntentionalClose) {
        return;
    }
    if (!isWebSocketConnected && !ws_client.available()) {
        return;
    }
    g_wsIntentionalClose = true;
    Serial.printf("[ws] close idle connection: %s\n", reason);
    ws_client.close();
    isWebSocketConnected = false;
}

bool connectDeviceWebSocket() {
    g_lastWsConnectAttemptAtMs = millis();
    ws_client.onMessage(onWebsocketMessage);
    ws_client.onEvent(onWebsocketEvent);

    Serial.printf("Connect ws://%s:%d%s\n", BFF_SERVER_HOST, BFF_SERVER_PORT, DEVICE_WS_PATH);

    bool ok = ws_client.connect(BFF_SERVER_HOST, BFF_SERVER_PORT, DEVICE_WS_PATH);
    if (!ok) {
        isWebSocketConnected = false;
        Serial.println("[ws] connect failed");
        return false;
    }

    isWebSocketConnected = true;
    g_wsIntentionalClose = false;
    markWsActivity();
    return true;
}

void startVoiceSession() {
    if (!isWebSocketConnected || !ws_client.available()) {
        Serial.println("ws not connected");
        setState(STATE_ERROR);
        return;
    }

    if (!ensureI2SMode(MODE_MIC)) {
        setState(STATE_ERROR);
        return;
    }

    resetMicCaptureStats();
    Serial.printf("[mic] gain=%.2f gate=%d soft_limit=%d\n", MIC_CAPTURE_GAIN, MIC_NOISE_GATE, MIC_SOFT_LIMIT);
    Serial.println("voice session started");
    setState(STATE_RECORDING);
}

void finishVoiceSession() {
    if (!isWebSocketConnected || !ws_client.available()) {
        return;
    }

    ws_client.send("finish");
    markWsActivity();

    Serial.println("session finish sent");
    printMicCaptureStats("session");
    setState(STATE_WAIT_RESULT);
}

void writeSpeakerPcm(const uint8_t *data, size_t len) {
    if (!ensureI2SMode(MODE_SPK)) {
        setState(STATE_ERROR);
        return;
    }

    size_t offset = 0;
    while (offset < len) {
        size_t written = 0;
        esp_err_t err = i2s_write(
            SPEAK_I2S_NUMBER,
            data + offset,
            len - offset,
            &written,
            120 / portTICK_PERIOD_MS
        );
        if (err != ESP_OK) {
            Serial.printf("i2s_write error: %d\n", err);
            setState(STATE_ERROR);
            return;
        }
        if (written == 0) {
            break;
        }
        offset += written;
    }
}

static inline int16_t clampS16(int32_t v) {
    if (v > 32767) return 32767;
    if (v < -32768) return -32768;
    return (int16_t)v;
}

static inline uint16_t absS16(int16_t v) {
    if (v >= 0) return (uint16_t)v;
    if (v == -32768) return 32768;
    return (uint16_t)(-v);
}

void resetMicCaptureStats() {
    g_micStats = {0};
    g_lastMicStatsPrintAtMs = millis();
    g_micSessionSampleIndex = 0;
    g_micPrimeDropBytesRemaining = MIC_PRIME_DROP_BYTES;
    g_micHpfPrevIn = 0.0f;
    g_micHpfPrevOut = 0.0f;
}

void printMicCaptureStats(const char *tag) {
    if (g_micStats.sampleCount == 0) {
        return;
    }

    float inAvg = (float)g_micStats.inAbsSum / (float)g_micStats.sampleCount;
    float outAvg = (float)g_micStats.outAbsSum / (float)g_micStats.sampleCount;
    float gatePct = ((float)g_micStats.gateHitCount * 100.0f) / (float)g_micStats.sampleCount;
    float nearLimitPct = ((float)g_micStats.nearLimitCount * 100.0f) / (float)g_micStats.sampleCount;
    float clipPct = ((float)g_micStats.hardClipCount * 100.0f) / (float)g_micStats.sampleCount;

    Serial.printf(
        "[mic][%s] n=%u in_peak=%u out_peak=%u in_avg=%.1f out_avg=%.1f gate=%.2f%% near_limit=%.2f%% clip=%.3f%%\n",
        tag,
        (unsigned int)g_micStats.sampleCount,
        (unsigned int)g_micStats.inPeak,
        (unsigned int)g_micStats.outPeak,
        inAvg,
        outAvg,
        gatePct,
        nearLimitPct,
        clipPct
    );

    if (g_micStats.inPeak < 7000 && nearLimitPct < 1.0f) {
        Serial.println("[mic][hint] raw input level is low; front-end mic gain is likely insufficient.");
    } else if (g_micStats.inPeak > 22000 || nearLimitPct > 8.0f || clipPct > 0.2f) {
        Serial.println("[mic][hint] front-end level is high; reduce gain to avoid distortion risk.");
    } else {
        Serial.println("[mic][hint] front-end input level is in a usable range.");
    }
}

void maybePrintMicCaptureStats() {
    unsigned long now = millis();
    if (now - g_lastMicStatsPrintAtMs >= MIC_STATS_PRINT_INTERVAL_MS) {
        printMicCaptureStats("live");
        g_lastMicStatsPrintAtMs = now;
    }
}

void processMicPcmInplace(uint8_t *data, size_t len) {
    if (!data || len < 2) {
        return;
    }
    size_t samples = len / 2;
    for (size_t i = 0; i < samples; ++i) {
        int16_t sample = (int16_t)((uint16_t)data[i * 2] | ((uint16_t)data[i * 2 + 1] << 8));
        uint16_t inAbs = absS16(sample);
        if (inAbs > g_micStats.inPeak) {
            g_micStats.inPeak = inAbs;
        }
        g_micStats.inAbsSum += inAbs;

        // 1st-order high-pass filter: y[n] = x[n]-x[n-1] + a*y[n-1]
        // This attenuates low-frequency bursts from plosives / handling.
        float x = (float)sample;
        float y = x - g_micHpfPrevIn + MIC_HPF_ALPHA * g_micHpfPrevOut;
        g_micHpfPrevIn = x;
        g_micHpfPrevOut = y;

        int32_t amplified = (int32_t)(y * MIC_CAPTURE_GAIN);
        int16_t s = clampS16(amplified);

        // Fade-in avoids click caused by abrupt non-zero waveform start.
        if (g_micSessionSampleIndex < MIC_FADE_IN_SAMPLES) {
            int32_t scaled = ((int32_t)s * (int32_t)g_micSessionSampleIndex) / (int32_t)MIC_FADE_IN_SAMPLES;
            s = (int16_t)scaled;
        }

        // Low-level attenuation (soft gate): reduce hiss without cutting speech frames.
        if (s < MIC_NOISE_GATE && s > -MIC_NOISE_GATE) {
            s = (int16_t)(s >> MIC_GATE_ATTENUATION_SHIFT);
            g_micStats.gateHitCount += 1;
        } else {
            // Soft-knee limiter near full scale to avoid clipping artifacts.
            if (s > MIC_SOFT_LIMIT) {
                int32_t over = s - MIC_SOFT_LIMIT;
                s = (int16_t)(MIC_SOFT_LIMIT + over / 4);
                if (s > 32767) s = 32767;
            } else if (s < -MIC_SOFT_LIMIT) {
                int32_t over = -MIC_SOFT_LIMIT - s;
                s = (int16_t)(-MIC_SOFT_LIMIT - over / 4);
                if (s < -32768) s = -32768;
            }
        }

        uint16_t outAbs = absS16(s);
        if (outAbs > g_micStats.outPeak) {
            g_micStats.outPeak = outAbs;
        }
        g_micStats.outAbsSum += outAbs;
        if (outAbs >= (uint16_t)(MIC_SOFT_LIMIT - 1000)) {
            g_micStats.nearLimitCount += 1;
        }
        if (s == 32767 || s == -32768) {
            g_micStats.hardClipCount += 1;
        }
        g_micStats.sampleCount += 1;
        g_micSessionSampleIndex += 1;

        data[i * 2] = (uint8_t)(s & 0xFF);
        data[i * 2 + 1] = (uint8_t)((s >> 8) & 0xFF);
    }
}

void applyMicFadeOutTail(uint8_t *data, size_t len) {
    if (!data || len < 2) {
        return;
    }
    size_t samples = len / 2;
    size_t fadeSamples = (samples < MIC_FADE_OUT_SAMPLES) ? samples : MIC_FADE_OUT_SAMPLES;
    if (fadeSamples == 0) {
        return;
    }

    size_t fadeStart = samples - fadeSamples;
    for (size_t i = 0; i < fadeSamples; ++i) {
        size_t idx = fadeStart + i;
        int16_t s = (int16_t)((uint16_t)data[idx * 2] | ((uint16_t)data[idx * 2 + 1] << 8));
        uint32_t remain = (uint32_t)(fadeSamples - i);
        int32_t scaled = ((int32_t)s * (int32_t)remain) / (int32_t)fadeSamples;
        int16_t out = (int16_t)scaled;
        data[idx * 2] = (uint8_t)(out & 0xFF);
        data[idx * 2 + 1] = (uint8_t)((out >> 8) & 0xFF);
    }
}

void processSpeakerPlayback() {
    size_t used = speakerRingUsed();
    if (!g_playbackStarted) {
        if (used >= SPEAKER_PREBUFFER_BYTES || (!g_ttsActive && used > 0)) {
            g_playbackStarted = true;
        } else {
            return;
        }
    }

    if (used == 0) {
        if (!g_ttsActive && g_state == STATE_PLAYBACK) {
            setState(STATE_READY);
        }
        return;
    }

    if (!ensureI2SMode(MODE_SPK)) {
        setState(STATE_ERROR);
        return;
    }

    uint8_t chunk[SPEAKER_PLAY_CHUNK_SIZE];
    size_t take = used;
    if (take > sizeof(chunk)) {
        take = sizeof(chunk);
    }

    for (size_t i = 0; i < take; ++i) {
        chunk[i] = g_speakerRing[g_speakerTail];
        g_speakerTail = (g_speakerTail + 1) % SPEAKER_RING_BUFFER_SIZE;
    }
    writeSpeakerPcm(chunk, take);
}

void setup() {
    M5.begin(true, false, true);
    M5.dis.clear();
    Serial.begin(115200);

    delay(200);
    Serial.println("XBBtn Booting...");

    InitI2SSpeakOrMic(MODE_SPK);
    setState(STATE_IDLE);

    if (!connectWifi()) {
        return;
    }

    setState(STATE_READY);
}

void onWebsocketMessage(websockets::WebsocketsMessage message) {
    markWsActivity();
    if (message.isText()) {
        String text = message.c_str();
        Serial.printf("[ws] %s\n", text.c_str());

        StaticJsonDocument<512> doc;
        if (deserializeJson(doc, text)) {
            return;
        }

        String type = doc["type"].as<String>();
        if (type == "asr") {
            String asrText = doc["text"].as<String>();
            bool isFinal = doc["is_final"] | false;
            Serial.printf("[asr]%s %s\n", isFinal ? " [FINAL]" : "", asrText.c_str());
        } else if (type == "error") {
            Serial.printf("[ws] error: %s\n", doc["message"].as<String>().c_str());
            setState(STATE_ERROR);
        } else if (type == "tts.start") {
            g_ttsActive = true;
            g_ttsBytesInCurrent = 0;
            g_playbackStarted = false;
            g_pcmCarryValid = false;
            speakerRingReset();
            int sr = doc["sample_rate"] | 24000;
            if (sr < 8000 || sr > 48000) {
                sr = 24000;
            }
            g_speakerSampleRate = sr;
            String fmt = doc["format"].as<String>();
            int channels = doc["channels"] | 1;
            g_ttsPcmReady = (fmt == "pcm_s16le" && channels == 1);
            if (!g_ttsPcmReady) {
                Serial.printf("[tts] unsupported stream fmt=%s ch=%d\n", fmt.c_str(), channels);
            }
            Serial.printf("[tts] start sr=%d ch=%d fmt=%s voice=%s\n",
                          g_speakerSampleRate,
                          channels,
                          fmt.c_str(),
                          doc["voice"].as<String>().c_str());
            if (g_state != STATE_RECORDING) {
                setState(STATE_PLAYBACK);
            }
        } else if (type == "tts.end") {
            g_ttsActive = false;
            g_ttsPcmReady = true;
            g_pcmCarryValid = false;
            Serial.printf("[tts] end bytes=%u buffered=%u\n",
                          (unsigned int)g_ttsBytesInCurrent,
                          (unsigned int)speakerRingUsed());
            if (g_state != STATE_RECORDING && speakerRingUsed() == 0) {
                g_playbackStarted = false;
                setState(STATE_READY);
            }
        }
        return;
    }

    const std::string &raw = message.rawData();
    if (!raw.empty()) {
        if (g_state != STATE_RECORDING) {
            if (!g_ttsPcmReady) {
                return;
            }
            if (raw.length() > 4096) {
                Serial.printf("[ws] drop large binary frame: %u\n", (unsigned int)raw.length());
                return;
            }
            setState(STATE_PLAYBACK);
            enqueueSpeakerPcmS16((const uint8_t *)raw.data(), raw.length());
        }
    }
}

void onWebsocketEvent(websockets::WebsocketsEvent event, String data) {
    if (event == websockets::WebsocketsEvent::ConnectionOpened) {
        isWebSocketConnected = true;
        g_wsIntentionalClose = false;
        markWsActivity();
        Serial.println("[ws] opened");
        if (g_state == STATE_IDLE || g_state == STATE_ERROR) {
            setState(STATE_READY);
        }
    } else if (event == websockets::WebsocketsEvent::ConnectionClosed) {
        bool wasIntentional = g_wsIntentionalClose;
        g_wsIntentionalClose = false;
        isWebSocketConnected = false;
        if (wasIntentional) {
            Serial.println("[ws] closed (intentional)");
            if (g_state == STATE_ERROR || g_state == STATE_IDLE) {
                setState(STATE_READY);
            }
            return;
        }

        Serial.println("[ws] closed (unexpected)");
        if (g_state == STATE_RECORDING || g_state == STATE_WAIT_RESULT || g_state == STATE_PLAYBACK || g_ttsActive) {
            setState(STATE_ERROR);
        } else {
            setState(STATE_READY);
        }
    } else if (event == websockets::WebsocketsEvent::GotPing) {
        // ignore
    } else if (event == websockets::WebsocketsEvent::GotPong) {
        markWsActivity();
    } else {
        Serial.printf("[ws] event %d\n", event);
    }
}

void loop() {
    M5.update();

    if (WiFi.status() != WL_CONNECTED) {
        Serial.println("WiFi disconnected, reconnecting...");
        setState(STATE_ERROR);
        WiFi.reconnect();
        delay(500);
        return;
    }

    ws_client.poll();
    bool buttonPressed = M5.Btn.isPressed();
    unsigned long now = millis();

    bool needWs = isWsConnectionRequired(buttonPressed);
    if (needWs) {
        if (!ws_client.available() && (now - g_lastWsConnectAttemptAtMs >= WS_RECONNECT_BACKOFF_MS)) {
            if (!connectDeviceWebSocket() && g_state != STATE_READY) {
                setState(STATE_ERROR);
            }
        }
    } else if (ws_client.available() && (now - g_lastWsActivityAtMs >= WS_IDLE_DISCONNECT_MS)) {
        closeDeviceWebSocketIfOpen("idle timeout");
    }

    if (g_state == STATE_READY && buttonPressed) {
        if (!ws_client.available()) {
            if (now - g_lastWsConnectAttemptAtMs >= WS_RECONNECT_BACKOFF_MS) {
                if (!connectDeviceWebSocket()) {
                    setState(STATE_ERROR);
                }
            }
        } else {
            startVoiceSession();
        }
    }

    if (g_state == STATE_RECORDING) {
        size_t bytesRead = 0;
        esp_err_t readErr = i2s_read(
            SPEAK_I2S_NUMBER,
            (char *)i2s_read_buffer,
            I2S_READ_CHUNK_SIZE,
            &bytesRead,
            (60 / portTICK_PERIOD_MS)
        );

        bool shouldFinish = M5.Btn.isReleased();

        if (readErr == ESP_OK && bytesRead > 0 && isWebSocketConnected && ws_client.available()) {
            uint8_t *sendPtr = i2s_read_buffer;
            size_t sendLen = bytesRead;

            // Drop very first samples after switching to MIC mode to absorb startup transients.
            if (g_micPrimeDropBytesRemaining > 0) {
                size_t drop = (sendLen < g_micPrimeDropBytesRemaining) ? sendLen : g_micPrimeDropBytesRemaining;
                sendPtr += drop;
                sendLen -= drop;
                g_micPrimeDropBytesRemaining -= drop;
            }

            if (sendLen > 0) {
                processMicPcmInplace(sendPtr, sendLen);
                if (shouldFinish) {
                    applyMicFadeOutTail(sendPtr, sendLen);
                }
                maybePrintMicCaptureStats();
                ws_client.sendBinary((const char *)sendPtr, sendLen);
                markWsActivity();
            }
        }

        if (shouldFinish) {
            finishVoiceSession();
        }
    }

    if (g_state == STATE_PLAYBACK || g_ttsActive) {
        processSpeakerPlayback();
    }

    delay(2);
}
