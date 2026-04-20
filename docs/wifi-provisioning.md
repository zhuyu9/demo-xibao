# ESP32 配网方案

## 背景

当前固件 `esp32/XBBtn/XBBtn.ino` 中 WiFi 凭据硬编码（第 9-10 行），缺乏配网机制。

## 实现方案：AP Mode + Web 配网

### 流程

```
上电
  ├── 检查 Flash (Preferences) 中是否有 WiFi 凭据
  │     ├── 有 → 尝试连接 WiFi
  │     │         ├── 成功 → 进入主循环
  │     │         └── 失败 → 进入配网模式
  │     └── 无 → 进入配网模式
  │
  └── 配网模式：
        ├── 启动 AP 热点（SSID: XBBtn_XXXXXX，密码: 12345678）
        ├── 开启 HTTP Server（80端口）
        ├── LED 蓝色闪烁
        └── 用户连接热点 → 浏览器打开 192.168.4.1
              └── 填写 SSID/密码 → 提交
                    ├── 保存到 Preferences
                    ├── 重启 WiFi 尝试连接
                    └── 成功 → 进入主循环
```

### 修改文件

**`esp32/XBBtn/XBBtn.ino`**
- 替换硬编码 WiFi 凭据为 `Preferences` 读写
- 新增配网状态 `STATE_PROVISIONING`
- 新增配网 HTTP Server 处理 `/` 和 `/save`
- `setup()` 中增加配网逻辑入口
- `loop()` 中增加配网页面响应和五连按检测

### 按钮触发配网

**五连按**：3 秒内快速按 5 次 → 进入配网模式

```cpp
unsigned long g_btnLastPressAt = 0;
int g_btnPressCount = 0;

void loop() {
    M5.update();
    bool btnPressed = M5.Btn.isPressed();
    unsigned long now = millis();

    if (btnPressed) {
        if (now - g_btnLastPressAt < 3000) {
            g_btnPressCount++;
        } else {
            g_btnPressCount = 1;
        }
        g_btnLastPressAt = now;

        if (g_btnPressCount >= 5) {
            g_btnPressCount = 0;
            enterProvisioningMode();  // 五连按 → 配网
        }
    }
    // ... 现有逻辑不变 ...
}
```

**注意**：此检测放在现有按钮逻辑之前，当五连按时先拦截，不进入录音逻辑。

### 实现细节

1. **Preferences key**: `wifi_ssid`, `wifi_password`
2. **AP 模式**: `WiFi.softAP("XBBtn_XXXXXX")`，密码 `12345678`
3. **HTTP Server**: 简单单文件 HTML，内联在代码中
4. **LED 指示**:
   - 蓝色快闪 → 配网模式等待配置
   - 蓝色慢闪 → 配网页面已打开，等待用户输入
   - 绿色常亮 → 已连接/就绪
   - 红色 → 错误

### 验证步骤

1. 烧录新固件（或清除 Flash 中的 WiFi 配置）
2. 设备发出热点 `XBBtn_XXXXXX`
3. 连接热点 → 浏览器打开 `192.168.4.1`
4. 输入 WiFi 信息 → 提交
5. 观察串口日志确认连接成功
