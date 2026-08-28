# Qt 5.9.6 / QProcess 接入要点

项目 GUI 已包含经过实际使用的客户端实现：

- [`BackendRunner` 声明](../../../src/gui/backendrunner.h)
- [`BackendRunner` 实现](../../../src/gui/backendrunner.cpp)

同事集成时应复用这两个文件的协议处理方式，而不是重新用同步 `waitForFinished()` 包装后端。重点：

1. `QProcess::SeparateChannels` 启动 `OcrBackend.exe --persistent`；
2. stdout 缓存到成员变量，循环查找 `\n` 后解析一条 JSON；
3. 同时只允许一个 `m_activeRequestId`，响应 ID 不匹配就丢弃；
4. stderr 只记日志，绝不参与 JSON 解析；
5. 取消/超时要杀掉整个后端进程，下一次重新预热；
6. 收到 recognize 响应后严格执行 `BACKEND_API_V1.md` 的发布门禁。

最小请求：

```cpp
QJsonObject options;
options.insert(QStringLiteral("crop_mode"), QStringLiteral("auto"));
options.insert(QStringLiteral("accuracy_mode"), QStringLiteral("maximum"));
options.insert(QStringLiteral("deadline_seconds"), 0);

QJsonObject request;
request.insert(QStringLiteral("protocol"), 1);
request.insert(QStringLiteral("action"), QStringLiteral("recognize"));
request.insert(QStringLiteral("image_path"), imagePath);
request.insert(QStringLiteral("output_directory"), requestDirectory);
request.insert(QStringLiteral("options"), options);
```

自动发布判断：

```cpp
const bool canPublish =
    response.value(QStringLiteral("status")).toString() == QStringLiteral("ok")
    && response.value(QStringLiteral("recognition_state")).toString() == QStringLiteral("verified")
    && !response.value(QStringLiteral("publication_blocked")).toBool(true)
    && response.value(QStringLiteral("structure_verified")).toBool(false);
```

此外还必须遍历 `cells`，确认不存在 `needs_review=true`。
