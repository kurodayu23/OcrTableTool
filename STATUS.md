# OcrTableTool 状态（2026-10-30 · Lead 接手复核轮）

> 本文件取代「通宵挑刺」那三份对账文档，作为**唯一**项目状态入口。
> 本版在 2026-10-28 轮基础上追加：**交付件判定已落地 + 续跑验证 + 新发现并修复 1 条竞态缺陷**。
> 历史保留为下方各节，追加内容带「2026-10-30」标注。
> 取样时点：2026-10-30 04:00 ~ 06:40（本机实测）。

## 〇、2026-10-30 结论（本轮）

- **交付件已定：`package\backend-delivery-20261030`（430 文件 / 563,789,087 B / exe `…11,055,242 B`，10-30 重建）为正式交付件，不再重建**（2026-10-30 用户拍板「不用打包」）—— 内含全部 3 个后端修复（合并格 span 越界、`deadline_seconds` 真中止、`redirect_stdout` 竞态），自检 19/19、包校验 13/13、12 例原挂起序列 12/12、30 例交织压力 30/30、冻结代码三重确认（clamp ✓ deadline ✓ redirect ✓）。
- **新发现并修复 1 条交付级竞态缺陷**：`redirect_stdout(sys.stderr)` 多线程竞态会把协议 stdout 永久污染成 stderr ⇒ 连续识别偶发「挂起」（客户端等不到响应）。修法：请求处理期外层一次性 redirect 窗口，响应 print 在窗口外。实证：2000 轮并发模拟无锁 2000/2000 污染 vs 加锁/外层方案 0/0。
- 判定方法（独立取证，沿用 10-28 轮）：`CArchiveReader` 取 exe 内冻结 code object —— 正式位缺 clamp、候选缺 deadline、新包三修复齐全。
- 仍待协调：交付 exe 基于**产品树**（10-28 13:30 基线 + redirect 修复）构建；仓库工作区另有并行会话 16:06 之后的 A23 加固（错误响应兜底等）**未并入**（未验证、非本轮范围）。⚠ **用户已拍板「不用打包」，20261030 不再重建**；A23 与仓库提交的合并作为后续维护项（见第七节），不阻塞当前交付。

## 〇·二、2026-10-30 R1 持续审计轮（找 bug → 修 bug）

三个只读审计子代理（table_pipeline / ocr_backend / C++ GUI+SDK）共产出 17 条发现；本轮已修 8 条，全部带回归测试或实测证据：

| # | 严重度 | 缺陷 | 修复 | 验证 |
|---|---|---|---|---|
| R1-1 | **P0** | 常驻服务被一行畸形 JSON 打死：`_echo_request_id` 对 `123`/`null`/`true` 做 `"request_id" in request` 抛 TypeError，逃出 `main()` ⇒ **整个服务进程退出**，之后请求全部石沉大海 | `_echo_request_id` 改"两个都是 dict 才回显"；新增 `_fallback_error_response`/`_json_line`/`_persistent_response_line` 三层响应护栏 | 实测交付 exe **20/20 畸形输入全部打死服务**；修复后 20/20 正确回 `status=error` 且服务存活；新增 `test_persistent_protocol_guard.py` |
| R1-2 | **P0** | 结构认证自毁：drop-caption 与 align 各按同一 offset 裁 `row_column_boundaries` 一次（二次裁剪）+ hash 未重算 ⇒ 带逐行边界的物理证书 `structure_verified` 恒被撤销 | offset 归一化只在 `_align` 一处做，裁后重算 `row_column_hash`；顺带修 `column_offset` 未应用于逐行边界 | 单元实证：长度/哈希自洽、`_structure_certificate_matches` False→True；新增 `test_export_and_certificate_regressions.py` |
| R1-3 | **P0** | 导出整表被拒：合并区覆盖到任一有文字的格就抛错，客户点导出直接失败无退路（两级表头 + 覆盖格有文字即命中） | 安全降级：丢弃该条合并、保留每格文字、冲突格标 `needs_review` | 实证导出成功 + 文字全在 + 合并丢弃 + 批注在位；空闲覆盖仍正常合并 |
| R1-4 | P1 | `_recognize` 之后的发布恢复流水线**零墙钟检查点**（大表 rows≥8 且 cols≥20 再读图 2 次 + 整页 OCR 2 次）⇒ 破 60s 契约且以 `status=ok` 返回 | 分支入口与每个重活前插 `_check_request_wall_clock("publish_recovery*")` | 编译 + 全量 1391 绿 |
| R1-5 | P1 | SDK 主动取消后仍自动重试：`cancel()` 报 `retryable=true` ⇒ 300ms 后重跑识别 | `cancel()` 改 `retryable=false`；`ocrRequestFailed` 显式忽略 `CANCELLED` | C++ 重编译通过；SDK 14/14 |
| R1-6 | P1 | GUI `waitForStarted(3000)` 超时不 kill ⇒ 孤儿进程 + 永久「正在释放资源」 | 失败分支 `kill()`+`waitForFinished`，兜底 `terminate()` | C++ 重编译通过；GUI 20/20、核心 21/21 |
| R1-7 | P2 | `IMAGE_NOT_FOUND`/`IMAGE_READ_FAILED` 的 message 泄漏本机绝对路径 | 固定人话；完整路径只进 runtime trace | 全量绿 |
| R1-8 | P2 | 单行请求无长度上限（500MB 无换行输入驻留内存） | `_MAX_REQUEST_LINE_BYTES = 16 MiB` + `readline` 上限 + 超限回 `INVALID_REQUEST` | fuzz 覆盖超长行 |
| R1-9 | P2 | `parse_html_table` 的 `rowspan`/`colspan` 无类型校验、无限幅 ⇒ 畸形值（`100000`/非数字）可撑出十万行/列空网格（识别管线内、无预算检查点） | `_safe_span()` 解析成 `[1, 1024]`，非法值当 1；并加**总网格上限**（1024×1024），超限截断 | 实证：`rowspan=100000` → 1024、`abc/-5/1e9` → 1；300 个大跨度单元格仍限在 1024×1024 / 0.3s；正常表逐格正确 |
| R1-10 | P1 | `_read_image` 整文件读入再 decode，无文件字节上限、无像素上限 ⇒ 200 MP 图光解码吃 1~2 GB | 前置 `stat` 文件上限 64 MiB + 解码后像素上限 1 亿；走既有 `IMAGE_READ_FAILED`，文案含"过大+上限"且**不含路径** | 实证：70 MiB 文件被拦、错误码 `IMAGE_READ_FAILED`、文案可读、无路径泄漏；正常图 665×514 不受影响 |
| R1-11 | P1 | 管线内部 `ValueError/TypeError/KeyError` 一律被兜底判成 `INVALID_REQUEST` + `retryable=False` ⇒ 真实内部缺陷显示成"请求无效"，既不重试也看不到内部错误码 | 新增 `_InvalidRequestError`：只有 `validate_request` 抛出的才归 `INVALID_REQUEST`；用户侧原因（过于狭长/尺寸无效/未检测到表格/没有检出文字）显式保留；其余归 `INTERNAL_ERROR`（可重试） | 实证 6 类分流正确；全量 1397 测试绿 |
| R1-12 | P2 | 「未检测到表格」把两种完全不同原因混成一个提示：**一个字都没检出**（画质/取景问题）vs **检出文字但成不了表**（结构/干扰问题） | 按检出文本框数量分流文案：0 框 → "没有检出任何文字…请重新拍摄/重新导出更清晰原图"；>0 框 → 原"未检测到可识别的表格" | 实证 FC-01153 现在给出"没有检出任何文字"文案，错误码/retryable 不变 |
| R1-13 | P2 | 请求生命周期 `begin_ruled_grid_request_cache` / `_begin_recognition_evidence` / `_begin_runtime_trace` 在 `try` 之外，收尾却在 `finally` ⇒ 任一 begin 抛错时，证据缓存活动标志与请求级网格缓存**泄漏到下一请求**（可能复用陈旧证据而认错字） | 三个 begin 移入 `try`（收尾函数均为幂等重置） | 编译 + 1397 测试绿 |
| R1-14 | P2 | SDK `exportCsv` 的引号判据与产品 `CsvExporter` 不一致：产品对任何 `unicode()<0x20`（含 TAB）加引号，SDK 只判 `,"\r\n` ⇒ 含 TAB 单元格两端导出结果不同 | SDK 改为逐字符判 `<0x20`，与产品同口径 | C++ 重编译通过；SDK 14/14 |

### R3：第二轮三路审计（交付链 / 测试有效性 / 后端未审区段）→ 修 9 条

| # | 严重度 | 缺陷 | 修复 | 验证 |
|---|---|---|---|---|
| R3-1 | **P0** | 5 个按列索引的文本修复函数（`_normalize_ocr_unit_symbols` / `_repair_single_letter_column_case` / `_normalize_spaced_decimals_in_numeric_columns` / `_repair_column_initial_case_from_context` / `_normalize_visible_ipv4_spacing`）在**行宽不齐**的网格上抛 `IndexError`，整个识别请求 500（实测 `[["A","B","C"],["1","2"]]`） | 抽 `_grid_has_ragged_rows()`，6 处入口提前返回（同文件 300+ 兄弟函数本就如此） | 5 个函数对 ragged 输入不再崩；正常网格归一仍生效 |
| R3-2 | P1 | `export_xlsx` 的 `confidence` 非数值（如 `"high"`）抛裸 ValueError → 归 INTERNAL_ERROR（旧版报"请求无效"） | 新增 `_cell_confidence()`，非数值抛 `cells must ...` → `EXPORT_GRID_INVALID`；nan/inf 不失败 | fuzz 实证错误码正确；3 条契约测试 |
| R3-3 | P1 | `cells: [[null]]` / `{"text": null}` 导出成**字面 "None"**，客户 Excel 多出垃圾 | `xml_safe_text()` 把 `None` 归一成空串 | 实证 A1 由 `'None'` 变空；契约测试锁定 |
| R3-4 | P1 | 客户自检 PYZ cookie 切片**写死 96**，格式实为 88（`struct.calcsize`）⇒ exe 被签名/追加数据后 6 项降级成 1 条 SKIP 且仍 exit 0（门禁假通过） | 改用 `struct.calcsize(_COOKIE_FORMAT)` | 自检仍 19/19 |
| R3-5 | P1 | `package_backend.ps1` 的 `--workpath` 在**交付目录内**且不清理 ⇒ 客户可见文件数/体积虚高（实测 +34.2 MB / 16 文件） | workpath 移到 `build\pyinstaller-work-<stamp>`，收尾删除 | 语法检查通过 |
| R3-6 | P1 | 交付说明校验默认关闭 ⇒ 默认流水线跑完无人验，客户可见数字可过期而无一步变红 | 校验改无条件执行；`licenses\` 为空时告警 | 语法检查通过 |
| R3-7 | P3 | `$rapidOcrPackage` 赋值后漏 `$LASTEXITCODE` 检查 | 补检查并抛明确错误 | 语法检查通过 |
| R3-8 | **门禁失真** | `TablePipelineTests` 有 **4 组重名测试**，后定义覆盖前定义 ⇒ 前 4 个从未执行，`discover` 仍报全绿 | 删除 4 个旧定义；新增 `test_suite_integrity.py`（AST 扫重名/空壳） | 测试 1397→1402 全绿 |
| R3-9 | **自查回归** | 我把「内部 ValueError → INTERNAL_ERROR」落地后，`JSONDecodeError`（ValueError 子类）让**畸形 JSON 也被判成可重试内部错误** ⇒ GUI 会为语法错误请求重跑整轮识别 | 新增 `_decode_request_line()`：UTF-8/JSON 解析失败一律 `_InvalidRequestError` → INVALID_REQUEST | 加强后协议测试（16 类逐条断言 error_code）由 2 失败 → 全绿 |

**测试门禁可信度（审计 16 条核心结论，已登记未全修）**：① 13 处「通过」只是 `inspect.getsource` 字符串匹配、不执行被测分支；② 三个唯一真实图像回归常年 `skipTest`（未设 `OCR_TABLE_REAL_FIXTURES`）；③ 至少 4 条用例断言的是测试自己算出的值。⇒ **「1402 passed」不能直接读作"这些行为都被验证过"**。

**仍待修（已登记，下一轮）**：13 处源码 grep 冒充行为测试（**B14** —— 已决定保留、不删，仅登记）。
⚠ 2026-11-02 复核更正（F44）：本行原先列的另外 4 项（UNC 下自检 `cwd` 必炸、`verify_delivery_package` 与自检的 PYZ 临时文件名冲突、`delivery_notes` 统计不排除 `__pycache__`、结构引擎「可用」判据用中文子串）**早已修好** —— 本文件下方的 R6-1 / R6-2 / R6-3 / R7-8 四行就写着「本轮一并完成并回归」，产品树代码也已逐条回查确认（`scripts/customer_selfcheck.py` 与 `scripts/verify_delivery_package.py` 都有 `_safe_working_directory()`；`verify_delivery_package.py` 用 `tempfile.mkstemp(prefix="ocr-delivery-pyz-")`；`scripts/delivery_notes.py` 有 `_EXCLUDED_DIR_NAMES` / `_EXCLUDED_SUFFIXES`；`customer_selfcheck.py` 有 `_ENGINE_UNAVAILABLE_MARKERS` / `_ENGINE_RAN_MARKERS`）。
⚠ 而这 4 项**从未进入待办表**（`_work/TODO-CONSOLIDATED.md` 里查无此项）⇒ 本行此前是「**过期 + 假登记**」：说「已登记」却没人登记，且照着它干活会重复修 4 个早已修好的项。已加 `ocr-tests/verify_status_todo_crossref.py` 守着：**「仍待修」清单里点名的待办编号，必须真的存在于待办表里**。

### R7：GUI 主窗口 / 几何裁决层 / SDK 三向审计 → 修 9 条

| # | 严重度 | 缺陷 | 修复 | 验证 |
|---|---|---|---|---|
| R7-1 | **P1（SDK）** | `captureImage` 拍照前无条件 `clearTableRegion()` ⇒ 文档里唯一的框选示例（`setTableRegion()`+`captureAndRecognize()`）**框选被静默丢弃**，`imageSaved()` 的按区域裁剪分支永不执行 | 移除该调用；区域统一由 `recognizeImage()` 管理（显式传空才清） | SDK 14/14 |
| R7-2 | **P1（SDK）** | `cameraError` 只置 `m_cameraReady=false`、**不复位拍摄标志** ⇒ 拍摄窗口内掉设备会让 `isBusy()` 永久为真，整套 API 只能靠 `cancel()` 解 | 补 `resetCaptureState()` + `m_camera->unlock()`，与 `captureError()` 口径一致 | SDK 14/14 |
| R7-3 | **P1（SDK）** | 扩边重试分支**不占 busy 位**（主重试分支会置标志），300ms 窗口内可再拍/再识别；lambda 不校验标志 ⇒ `cancel()` 后照样重跑一次识别 | 置 `m_ocrRetryPending=true` + lambda 校验标志与路径一致性 | SDK 14/14 |
| R7-4 | P2（SDK） | 收到**完整响应**后不停请求计时器（GUI 在同样位置会停）⇒ health/export 响应在到期前到齐、进程收尾稍慢就被判 TIMEOUT(retryable) 并**丢弃已拿到的结果** | 解析成功即 `m_requestTimer->stop()`；`requestTimedOut` 见 `m_pendingResponse` 非空则不算超时 | SDK 14/14 |
| R7-5 | **P1（几何）** | `_striped_borderless_certificate` 未归一化 Hough 端点方向：`slope=(y2-y1)/max(1.0, x2-x1)` 在 `x1>x2` 时分母恒 1.0 ⇒ 约**一半横线**被算成 |slope|=3.0 而丢弃、投影也错位 ⇒ 上下边界校验大概率失败 ⇒ **整条 striped-borderless 证书路线静默降级** | 先按 x 归一化端点再算斜率 | 编译 + 全量测试绿 |
| R7-6 | 中（GUI） | 人工修改单元格后**不清黄点图标**（`showTable` 会 setIcon，改完只清背景/tooltip）⇒ 摘要显示「0 项待确认 / 可以正式导出」而格子还带黄点，界面自相矛盾 | `tableItemChanged` 补 `item->setIcon(QIcon())` | C++ 重编译 + GUI 20/20 |
| R7-7 | 中（GUI） | 框选/拍照后 `m_sourceImagePath` 变成 AppData 的 `crop-<uuid>.png` ⇒ 导出默认文件名落到临时目录随机名，客户在「文档/图片」里找不到 | 新增 `m_userVisibleImagePath`（只记用户真正选的图），`suggestedOutputPath` 优先用它；无源图时回退「识别结果」 | C++ 重编译 + GUI 20/20 |
| R7-8 | 中（交付脚本） | 「结构引擎可用」判据用中文子串：`"结构模型" in item` 会把「线框、文字布局与结构模型结果不一致」（引擎**跑过**）误判成不可用；反过来两条与引擎无关的复核告警就能 PASS | 失败只认「表格结构模型本次不可用」；PASS 需正向证据（不一致告警 或 `structure_verified=true`）；否则 SKIP | 包内自检重跑仍 19/19（判据变为"结构已按物理边界验证"） |
| R7-9 | P2（交付脚本） | UNC 网络共享跑自检必炸、PYZ 临时文件固定名冲突、`delivery_notes` 统计含缓存/构建残留 | 见 R6-1/6-2/6-3（本轮一并完成并回归） | 自检 19/19、verify 逻辑复核 |

本轮基线：backend **1417 通过**；根 tests **58 通过**；C++ 核心 **21/21**、GUI **20/20**、SDK **14/14**（全部重编译）。
⚠ 编码纪律：本轮多次被 `edit` 工具冲掉 C++ 文件的 **UTF-8 BOM / CRLF**（`mainwindow.cpp/h`、`ocrtableclient.cpp`、`cameraocrclient.cpp`），均已按仓库副本基准逐字节恢复后再编译。**每次改 C++ 后必须复核 BOM/EOL**。

### R6：交付链剩余项（UNC / PYZ 临时文件 / 统计口径 / 结构引擎判据）

| # | 严重度 | 缺陷 | 修复 |
|---|---|---|---|
| R6-1 | P2 | 客户从**网络共享**（UNC `\\server\share\...`）跑自检时，`subprocess.run(cwd=UNC)` 抛 `NotADirectoryError [WinError 267]` ⇒ health/导出/识别三条实跑全 FAIL，被误判"包坏了" | `customer_selfcheck.py` 与 `verify_delivery_package.py` 均加 `_safe_working_directory()`：UNC 下 `cwd=None`（EXE 仍用绝对路径启动） |
| R6-2 | P2 | `verify_delivery_package.py` 的 PYZ 临时文件用**固定名** `%TEMP%\ocr-delivery-pyz-check.pyz` ⇒ 两路并行复检互相覆盖、报假"cannot read PYZ entries" | 改 `tempfile.mkstemp(prefix="ocr-delivery-pyz-")` 唯一名 |
| R6-3 | P1 | `delivery_notes.py` 的 `collect_facts` 用裸 `rglob`，`pyinstaller-work/`、`__pycache__/`、`*.pyc`、`*.bak` 全被算进客户可见的文件数/体积；且渲染与校验共用该函数 ⇒ 永远"自洽"，校验抓不到口径错误 | 加 `_EXCLUDED_DIR_NAMES` / `_EXCLUDED_SUFFIXES` 过滤 |

⚠ **交付包当前处于"文档与代码均落后于产品树"的已知状态（按你要求先不打包）**：
本轮所有代码修复与《交付说明》模板改进都**尚未进包**，因此
`python scripts\delivery_notes.py verify --package-dir package\backend-delivery-20261030`
现在会 **FAIL**（模板的 `output_directory` 说明已改、包内文档仍是旧的），这是**如实反映**、不是缺陷。
重建一次即可自动收敛（重建时 `package_backend.ps1` 会渲染并**无条件校验**）。

### R5：网格/几何 + 其余模块 + 文档协议三向审计 → 修 12 条（含 1 条**客户会照文档用错**）

| # | 严重度 | 缺陷 | 修复 | 验证 |
|---|---|---|---|---|
| R5-1 | **P1** | `assign_ocr_to_grid` 对 rows/columns 契约零校验：`columns=[]` 抛 IndexError（整页失败）；`rows=[0,100,100,200]` 产生**零高幽灵行**（行数与证书错位）；非升序边界让 box 落到 -1 列**静默丢值** | 新增 `_normalize_axis_boundaries()`：转 int、丢重复/逆序、长度<2 视为轴不可用（返回空网格交给上层报"未检测到表格"） | 三例实证：0×0 / 2 行无幽灵 / 值不丢 |
| R5-2 | P2 | 单个畸形 box（坐标数非偶数）让 `np.asarray(...).reshape` 抛错 → 整页识别失败 | 逐 box try/except + 有限性校验，坏 box 跳过 | 畸形+正常混排实测只保留正常项 |
| R5-3 | **P1** | `_ordered_corners` 在 45° 菱形等平局场景把**同一个角点塞进两个槽位** ⇒ 单应矩阵奇异 ⇒ `warpPerspective` 输出**全白图**但 `detected=True` | 角点按 (值,索引) 稳定排序并去重，四角互不相同 | 菱形/正常/歪斜三例均得 4 个唯一点 |
| R5-4 | P1 | 畸形四边形虽不重复，仍可能产出奇异矩阵或纯色图 | `_warp_perspective_table` 加三道硬校验：矩阵有限且 \|det\|≥1e-12、输出非空、输出非纯色（max-min≥8），否则 `detected=False` 退回原图 | 编译 + 1417 测试绿 |
| R5-5 | P2 | `recognition_scheduler` 的 `Queue.get()/put()` **无超时**：请求池记账一旦漂移就永久阻塞，而 60 秒墙钟是协作式检查点、救不了 ⇒ 只能等客户端超时 | `get(timeout=30)` / `put(timeout=30)`，超时抛明确错误；`release_native_resources` 复位 `_created_requests/_request_limit` | 调度器 9 测试绿 |
| R5-6 | P2 | `colored_table.py` 只有绝对导入 `from fast_stats import` ⇒ 包模式导入时**静默退回** `np.median` 慢路径（无告警） | 加相对导入兜底 | 扁平/包两种导入实测都拿到 `fast_stats` |
| R5-7 | **P1（文档）** | 两份客户文档给出**相反**的复核判定指令：交付说明让人用 `confidence_sentinel` 判断是否复核，API 文档说只认 `needs_review` ⇒ 照交付说明做会把逐格全对的表整表标黄、永远过不了门禁 | 交付说明该段改写：**复核只看 `needs_review`**，sentinel 只说明 confidence 不是实测值 | 模板渲染 + 一致性校验通过 |
| R5-8 | **P1（文档）** | `BACKEND_API_V1.md` 称"`structure_verified=false` 时…进入 blocked"，**与实现不符**（实测可达 `status=ok`+`verified`+`publication_blocked=false`+`structure_verified=false`）⇒ 照文档省略 `structure_verified` 检查的客户端会发布**未认证结构** | 文档改为明确"结构认证被撤销只清证书+提示，不阻断；**客户端必须自己校验 `structure_verified==true`**" | 文档订正 |
| R5-9 | P1（文档） | API 文档漏 3 个最常触发的错误码（`NETWORK_ACCESS_DENIED` / `RECOGNITION_BUDGET_EXCEEDED` / `INSUFFICIENT_MEMORY`），且**整份文档没有 60 秒墙钟概念** | 补全 19 码表格（含 field/retryable）+ 新增 §4.1「60 秒墙钟契约」（检查点式、可提前、超时请留 ≥120 秒） | 文档订正 |
| R5-10 | P2（文档） | API 文档三处与实现不符：health 校验"五个"模型（实为 15）、内存口径虚构"1.5GB/1GB 才报 INSUFFICIENT_MEMORY"、"`output_directory` 必需"（实为可选） | 逐条按实现改写（含"省略时落到共享临时目录且不清理"的警示） | 文档订正 |
| R5-11 | P2（文档/契约） | `schema` 把 `request_id` 标为 required（实现可选，示例 `{"protocol":1,"action":"health"}` 自身过不了 schema）、`output_directory`/`spans` 也标必填、且 `oneOf` 缺 health/warmup/export 三类成功响应 | 全部改为可选；新增 `healthResponse`/`warmupResponse`/`exportXlsxResponse`（oneOf 6→9 条） | JSON 合法 + 结构复核 |
| R5-12 | P2 | `HANDOFF.md` 声称包内"同时包含可直接运行的 `OcrTableTool.exe`"，与实况**相反**（包内无 GUI） | 按实况改写并说明 GUI 需从源码构建 | 文档订正 |
| R5-13 | P2 | 仓库卫生测试暴露两处既有问题：`cameraocrclient.h` 缺 UTF-8 BOM（产品树 9/17 版 vs 仓库 10/28 版分叉）、`test_delivery_gate_honesty.py` 写死本机绝对路径 | 补 BOM；测试改按 `__file__` 推导项目根 | 根 tests 58/58 全绿 |
| R5-14 | **自查回归** | 我给 `startBackend` 加的超时清理用了 `waitForFinished(3000)`，**违反项目既有 UI 契约**（UI 线程禁 3 秒阻塞） | 改 500ms + 兜底 500ms（最坏 1s，与既有 shutdown 路径一致） | 契约测试 2 条由 FAIL 转 PASS |

本轮基线：backend **1417 通过**；根 tests **58 通过**；C++ 核心 **21/21**、GUI **20/20**、SDK **14/14**（均重编译）。

### R4：文本修复启发式的「静默改错数」5 条（全部带反例契约测试）

| # | 严重度 | 缺陷 | 修复 | 验证 |
|---|---|---|---|---|
| R4-1 | P1 | `%50` 在欧陆式表格里是合法写法，但「重复 3 次」被当成反转证据 ⇒ `%50`→`05%`（**50% 变 5%，10 倍错**），并把置信度压到 0.77 显示成"已修正" | 三档判定：①干净兄弟≥3 直接采纳；②无兄弟时只接受**真实镜像替换**（6↔9/S↔5）且结果为合法百分比（无前导零、≤100）；③其余不改 | `%50`×3 保持；`%56`×3→95%（原用例保住）；有干净 `50%` 时能修 |
| R4-2 | P1 | `_compact_numeric_token` 把英制混合分数并成假分数：`1 1/2`→`11/2`（**1.5 变 5.5**）、`3 1/4`→`31/4` | 先排除 `整数 空格 分子/分母` 形态 | 5 个反例保持；`944. 985`→`944.985` 仍合并 |
| R4-3 | P2 | `_normalize_dimension_diameter_symbol` 把型号前缀强改 Φ：`D25×40`→`Φ25×40`、`O20×30`→`Φ20×30`（不降置信、不标黄） | 字母前导默认保留；仅列头含「直径/外径/内径/规格/尺寸/Φ」时放开；数字 `0` 前导始终还原 | 三例型号保持；`025×40`→`Φ25×40`；列头有证据时 `O25×40`→`Φ25×40` |
| R4-4 | P2 | `1~5kg`→`1-5kg`（量程变负号）；另 `2026-03~07` 因模式不允许 `-` **实际从未修**（docstring 承诺落空） | 新增单位白名单+数字段≤2位判据保留 `~`；`-` 加入标识符字符类；docstring 按真实行为订正 | 5 个区间保持；5 个型号/日期正确还原 |
| R4-5 | P2 | `_repair_single_letter_case_from_column_context` 门限 `len(Counter)<5` 是**字母种类数**不是出现次数 ⇒ 30 行全 `v` 也直接 return，**该函数唯一职责永不生效** | 改为按出现次数：`sum(lower)>=5 且 sum(lower)>sum(upper)` | 30v+3V→修 3 格；30V+3v→不动 |

新增 `test_text_repair_regressions.py`（15 条契约，含每条的"不应被改"反例）。
本轮基线：backend **1417 通过 / 0 失败 / 3 跳过**；真实语料 12/12 全 ok。

### R2：26 例「未检测到表格」漏检 —— **首次定位到真实机制**（历史只做现象定性）

用 `runtime trace` + 引擎打桩复现历史确定性漏检例，结论：

- **机制**：重度退化图（模糊/重影/低对比/低分辨率）上，**PP-OCR 页面检测器直接返回 0 个文本框**（`The text detection result is empty`），矫正图与原图两次都为空 ⇒ 网格为空 ⇒ 报"未检测到表格"。
- **排除项（重要）**：不是网格构建逻辑缺陷、不是几何矫正切坏表、不是超时（FC-01153 仅 14.6s 返回）。
- **增强无效实测**：对 FC-01153 试了 CLAHE、2× 放大、自适应二值、CLAHE+2× 四种预处理，**全部仍是 0 框**；FC-01145 原图仅 2 框、2× 放大 5 框（42×9 表需要几十框）。⇒ **属检测器能力边界，加预处理救不回来**。
- **图像确认**：`view_image` 复核 FC-01153 为模糊/重影/低对比、文字人眼亦不可读、顶部有遮挡条 ⇒ 检测器输出空是**正确行为**。
- ⇒ 结论：该 26 例**不是可修缺陷**，是输入质量决定的检出失败；已把用户提示拆成"没有检出任何文字（请重拍/重导出）"，避免用"表格结构"误导。引用口径以本节为准。

本轮基线（实跑）：backend **1397 通过 / 0 失败 / 3 跳过**（新增 12 条契约测试）；C++ 核心 **21/21**、GUI **20/20**、SDK **14/14**；协议 fuzz 20 类畸形输入 0 异常。
⚠ 编码事故自曝：改 C++ 时 `edit` 把 `backendrunner.cpp` 的 **UTF-8 BOM 与 CRLF 冲掉**，MSVC 按 CP936 解 UTF-8 中文 ⇒ C2001。已按仓库副本基准逐字节恢复后编译通过。**后续改 C++ 必须先确认编码/换行风格**。
⚠ 真实语料回归说明：同批 12 例在**机器重载**（vmware 3643s CPU、可用内存 4.4GB）下有 5 例 `RECOGNITION_BUDGET_EXCEEDED`；单例 FC-00042 成功但耗时 47.7s（正常 ~19s）。这 5 例列数均 <20（不走 R1-4 新增检查点分支），属既有「机器状态造成 60 秒误拒」现象，非功能回归。

## 一、结论（2026-10-28 原版）

- 项目**可以继续维护，且本轮实际修掉了 8 条已确认缺陷**；交付**卡在一个决定上**（哪个包算交付件），不是卡在技术上。
- 「通宵挑刺」那份记录的**过程账已停止追加**，技术事实已提取完毕。
- ⚠ 2026-10-30 更新：交付决定**已作出**（见第〇节），不再卡壳。

## 二、交付件状态（2026-10-30 更新）

| 位置 | 文件数 / 字节 | exe | 状态 |
|---|---|---|---|
| `package\backend-delivery`（旧正式位） | 430 / 563,783,655 | `C8791B57745B537C`（10-23 构建） | 历史基线：带 span 越界缺陷，**不再作为交付件** |
| `package\backend-delivery-20261027`（候选） | 430 / 563,785,625 | `FFD685153FDE1734`（10-27 构建） | 含 clamp；缺 deadline / redirect 修复；保留作候选 |
| **`package\backend-delivery-20261030`（正式交付件）** | **430 / 563,789,087** | **`…11,055,242 B`（10-30 重建）** | **✅ 全链验证通过，为最终交付件** |

判定方法（独立取证）：`CArchiveReader` 取两个 exe 内 `ocr_backend` 的冻结 code object ——
旧 exe **848** 个顶层定义、**无** `_clamp_spans_to_extent`；新包 **849** 个、**有**。

## 二·五、2026-10-30 新增验证记录（全部实跑，非推演）

| # | 验证项 | 对象 | 结果 |
|---|---|---|---|
| V1 | 新发现竞态：`redirect_stdout` 多线程污染 stdout | 并发模拟 | 无锁 2000/2000 轮污染 vs 外层窗口方案 0/0 |
| V2 | 原 12 例「挂起序列」（FC-00062/42/51/64…） | 修复后源码 / 新 exe | **12/12 全过，0 超时，stderr 零混入** |
| V3 | 30 例识别+导出交织压力 | 新 exe | **30/30 全过，0 超时，0 stderr 混入** |
| V4 | 41 例压力（含已知漏检） | 修复后源码 | 38 成功 / 3 固有漏检，0 挂起 |
| V5 | `deadline_seconds=5` | 新 exe | **6.7s 即 `RECOGNITION_BUDGET_EXCEEDED`**（旧 58s） |
| V6 | 合并格 span 最小场景 | 新 exe 冻结码 | span 7→5 收窄，导出成功 |
| V7 | 客户自检 / 包校验 | 新包 | **19/19 + 13/13，exit 0** |
| V8 | 交付说明一致性 | 新包 | `delivery_notes.py verify` rc=0 |
| V9 | 三重 exe 冻结代码对比 | 正式位/候选/新包 | 新包独占 clamp+deadline+redirect ✓ |
| V10 | 强制低内存档（`OCRTABLE_LOW_MEMORY_THRESHOLD_MB=999999`） | 新 exe | `status=ok` 10×6、`memory_mode=low_memory`、18.7s、未降级 |

修法落点（`backend\ocr_backend.py`）：`_serve_persistent` 与 `main()` 的**请求处理期外层一次性** `with contextlib.redirect_stdout(sys.stderr)`，响应 print 在窗口外 —— 内层 31 处 redirect 在其域内嵌套，线程交错只 stderr→stderr，永不污染协议 stdout。

⚠ **本轮新增的源码修复尚不在任何交付包内**：`backend\` 的改动要生效必须重建 exe。

## 三、本轮修了什么（全部实跑验证）

| # | 缺陷 | 改动位置 | 验证 |
|---|---|---|---|
| 1 | `deadline_seconds` 形同虚设（传 5 秒实跑 ~58 秒） | `backend\ocr_backend.py`：新增 `_effective_request_limit_seconds()`，两处中止判定改为 `min(60, 请求预算)` | 5 秒预算 **T=5.2 秒即中止**（修复前 T=58 秒才拒）；`0/负值` 逐秒不变；新增 11 例单测 |
| 2 | 合并格 span 越界 ⇒ 导出失败（旧包缺陷，源码侧已修，本轮补实证） | 已存在的 `_clamp_spans_to_extent` | 反事实重演：旧规则 **250/250 越界**、当前 **0/250**；真实图片 14 例端到端导出 **9/9 成功** |
| 3 | CSV 把合法负数写成 `'-12.5` | `src\core\csvexporter.cpp`：新增 `readsAsFaithfulNumber` / `losesPrecisionAsNumber` / `needsTextGuard` | 独立 C++ harness 原版 **21 失败 → 0 失败**；Qt 套件 21/21 |
| 4 | 空表导出产出 3 字节 BOM（CSV）/ 空工作簿（XLSX） | `csvexporter.cpp` + `backend\table_pipeline.py` | 现在明确报 `EXPORT_GRID_INVALID` 且不留文件；新增 6 例单测 |
| 5 | GUI **F3** 后端退出码被完全忽略 | `src\gui\backendrunner.cpp` | 运行时真触发：响应 ok + 退出码 3 ⇒ 判失败（`BACKEND_CRASHED`） |
| 6 | GUI **F5** 忙碌/停止窗口点「开始识别」被静默丢弃 | `src\gui\mainwindow.cpp` | 现在有可见反馈，停止完成后按用户点击**补跑** |
| 7 | GUI **F6** 导出丢掉 `structure_certificate` | `backendrunner.{h,cpp}` + `mainwindow.{h,cpp}` | 证书原样透传 + 「宁可不发」护栏；6 种不合法证书一律不带 |
| 8 | GUI **B1** 自动重试把 60 秒放大到 ~120 秒 | `mainwindow.cpp` 预算计时器 + `recognize(...,budgetSeconds)` | 不变量穷举：凡重试，`已用+350+budget ≤ 60000`；父进程计时器同时补上 |
| 附 | **SDK 同一处 CSV 缺陷**（`neutralizeCsvFormula`） | `interface-sdk\qt\ocrtableclient.cpp` 与产品侧逐条对齐 | SDK 测试 14/14（含新增负数用例） |
| 附 | **测试装置本身在测产品树镜像**（写死路径污染 `sys.modules`） | `backend\tests\test_acceptance_runner_metrics.py` 改为按 `__file__` 推导 | 全量 discover 从「4 个 ERROR」变 **1385 全绿** |

改动规模：12 个文件（+1201 / −56），新增 2 个测试文件；未提交、未动 git 索引、`package\` 零改动。

## 四、测试基线（2026-10-28 13:2x 实跑）

| 套件 | 结果 |
|---|---|
| `backend` 全量 `unittest discover` | **1385 通过 / 0 失败 / 3 跳过**（113 s） |
| `tests\test_ui_contract.py` | 30/30 |
| `tests\test_interface_sdk_contract.py` | 12/12 |
| `tst_ocrtable_core`（C++） | 21/21 |
| `tst_ocrtable_gui`（C++） | 20/20（含新增 F3/F5/F6/B1 四条行为用例） |
| `tst_ocrtableclient`（SDK） | 14/14 |
| 构建 | 全新目录全量重编译 rc=0（`worktree-20261028-r86`，exe 362,496 B） |
| 产品树镜像同步后复跑 backend | **1385 通过 / 0 失败** |

## 五、源码与仓库

- 仓库 HEAD 仍是 `c03703a`（2026-10-24），**本轮改动全部未提交**（另有上轮遗留的 6 个 staged 文件）。
- 产品树 `D:\AP\AP7000\OcrTableTool` 已用 `_work\sync_mirror.py` 与工作树**逐字节对齐**（LF 归一化、保留 BOM），两边跑同一套测试都绿。
- `.venv` 用目录联接指向产品树的 venv（`.gitignore` 已忽略 `.venv/`），否则 C++ 测试找不到解释器。

## 六、已知限制（对外照实说）

- 60 秒是「识别 worker 内部检查点式裁决线」，不是端到端超时：1200 例里 142 例单次 >60 秒，最小 60.56 秒、最大 80.68 秒。
- 「未检测到表格」26/26 例真实漏检（重度退化富集 6.31 倍），根因未定位。
- 8 GB 真机、安静机 60 秒触发率、GUI UIA/MSAA 全量：**未验证**。
- 本轮新改的失败分支（B1 真跑满 60 秒、F6 真导出列宽、F3 真解释器崩溃）**只在桩/不变量层面验证**，没有真后端长跑实测。
- 源码 5 处过期注释刻意保留；`selfcheck.bat` 与 `交付说明.md` 不在版本库。

## 七、下一步（按性价比）

1. ⚠ **2026-10-30 更新（用户拍板「不用打包」）**：交付件已定为 `backend-delivery-20261030` 且**不再重建**。仓库侧协调项**留作后续**、不阻塞交付：把 `backend\ocr_backend.py` 的 redirect 竞态修复提交进仓库（当前仓库仍无此修复；仓库工作区另有并行会话的 A23 等改动未合并，何时合并/是否并入由并行会话与主控决定）。
2. **delivery 已定**：交付件为 `backend-delivery-20261030`，exe 已重建并全链验证（见第〇 / 二·五节），无需再动。
3. 补 B 类验证：8 GB 真机、安静机触发率、真实图片合并格自然触发例（至今 0 例）、Excel 打开 CSV 复核。
4. **口径登记（不随包、不改写）**：`ai-workspace\准确率专项报告-20261019.md`（10-25 定稿，不随包分发，通宵挑刺体系已停止追加）中 deadline 的描述（传 5 秒实跑 ~58 秒、软预算不中止请求）为**旧实现口径**；新交付 exe 已改为真正参与中止判定（实测传 5 → 6.7s `RECOGNITION_BUDGET_EXCEEDED`）。两处口径差异已在《交付说明》（随包）L116/L298 订正为新实现；专项报告为历史快照、未改写。引用 deadline 行为一律以《交付说明·随包》为准。
5. 归档：`package\backend-delivery`（旧）与 `-20261027`（候选）在交付稳定后可按需清理；删除前保留 exe 哈希记录。

### R8：2026-11-06 夜间连续测试（大规模普查 + 四项修复）

| # | 严重度 | 缺陷 | 修复 | 验证 |
|---|---|---|---|---|
| R8-1 | **P0** | **整行丢失**：`verified_components`(嵌套 def @54546，调用 @57803) 把表头带仅有的两条文字（`物料名称` 0.608、`类型` 0.749）抹成空串/-1.0；`_borderless_spatial_grid` 入口 `if not value: continue` 使整行无法进入聚类（26→25）；前导页眉规则 @11975–11983 再删一行 ⇒ FC-00003 输出 24×6、整表上移一行、`hard=0.000` | 入口让**空文本照常参与行聚类但不贡献文字**（行几何保住，格子空、标黄） | FC-00003 **24×6/0.000 → 25×6/0.547** |
| R8-2 | **P0** | **退化结构未被使用**：`FC-00010` 的 borderless 网格明明是 21×4、逐格 0.738，最终却输出 22×1 | 新增 `_rescue_degenerate_structure()`：最终 ≤2 列时按文字空间布局重建，全部 `needs_review=True`、**不伪造结构认证**、写 `publication_block_reasons` | FC-00010 **22×1/0.000 → 21×4/0.738** |
| R8-3 | **P1** | **内存不收敛**：OpenVINO CPU 对动态形状识别模型 `x[?,3,48,?]` 按**每个不同输入几何**各留一份激活缓冲；固定形状 12 轮 RSS 完全平（468→469 MB），每轮换形状 +8 MB/轮，单几何 (16,3,48,640) +152 MB、(16,3,48,1280) +297 MB | 请求收尾调用 `release_for_table_structure()` + `gc.collect()`（下一请求由 `prepare_for_request()` 重建，代价 0.3~1.5 s）；`OCRTABLE_RELEASE_NATIVE_PER_REQUEST=0` 可关。另给调度器加不同几何计数 `distinct_shape_count()` 备查 | 8 例 RSS **850→1088 MB ⇒ 132→341 MB**；FC-00020 **2123 → 178 MB** |
| R8-4 | P1 | **内存档位自激横跳**：迟滞带 512 MB < 两档驻留差（~800 MB）⇒ 降档释放后立刻超门槛、升档重建、再降；实测 16 请求翻转 6 次、同图 15.9 s ↔ 42.3 s | 迟滞带默认提到 **1280 MB**，新增 `OCRTABLE_LOW_MEMORY_RELEASE_MARGIN_MB` 标定 | 确定性序列测试：3.9 GB→低档、**4.7 GB 仍留低档**、5.5 GB 才升档；新增契约测试锁住 |
| R8-5 | 记录 | 我先前误判「小数点丢失 300+ 处」：`punct_swap` 720 处里**只有 45 处**是真塌缩；现覆盖 2/3 可救格（`321.226`/`831.282` 均还原成功）。放宽判据实测 **4 改 1 对**（会把 `900 934` 洗成可信的 `900.934`），**决定不放宽** | 无代码改动 | 子代理实跑取证 |

本轮基线：后端 **1574 通过 / 0 失败 / 3 跳过**（每处改动后各跑一次）。
仍在跑：修复后语料普查 1-250 / 251-500（tag c1b/c2b）。
未解决：`FC-00020` 仍 12×1 / `hard=0.000`，且 `--no-budget` 下耗时 **158.7 s**（borderless 网格 `ok=False`，兜底无可用网格）——时间红线问题仍开。

### R9：2026-11-06 凌晨续修（空边列 + 每请求释放原生缓冲 + 新交付包）

| # | 严重度 | 缺陷 | 修复 | 验证 |
|---|---|---|---|---|
| R9-1 | **P0** | **幻影空列把整表挤偏**：`FC-00294` 实际 9×**9** / 真值 9×**8**、`FC-00371` 实际 13×**9** / 真值 13×**8** —— 首列整列全空，导致每行右移一格、`hard=0.000`（而 `best=1.000`，内容完全正确） | 新增 `_drop_blank_edge_columns()`：去掉**整列为空**的首列/尾列；有 spans 时**同步平移/裁剪**合并区而不是跳过（原先"有 spans 就整体跳过"让本修复在真实链路上完全失效——空白列是在 `_handle_request_body` 后处理阶段才产生的） | FC-00371 **13×9 → 13×8 / hard 1.000**；FC-00294 **9×9 → 9×8 / 1.000**；FC-00002 17×7 → 17×6 / 0.971 |
| R9-2 | **P0（挂载点）** | 收尾清理挂在 `_recognize` 出口看不到最终表 | 改为赋值覆盖 `_handle_request_body`（保留 `__wrapped__` 以免破坏源码级护栏），在其返回处做「去空边列 + 补小数点」 | 1574 后端测试全绿 |
| R9-3 | **P1** | 内存随例数线性增长（8 例 850→1088 MB，13 例到 1356 MB） | 请求收尾调用 `release_for_table_structure()` + `gc.collect()`；`OCRTABLE_RELEASE_NATIVE_PER_REQUEST=0` 可关。调度器另加 `distinct_shape_count()` 备查 | 8 例 RSS **132→341 MB**（峰值 476）；FC-00020 **2123 → 178 MB** |
| R9-4 | 记录 | 44 例「内容对但错位」样本（best≥0.8、hard<0.1）复测：**9 例修好**（5 例直接 1.000），其余 35 例是**行级**错位（两级表头被拼/分组表头行丢失），受既有契约 `test_sparse_header_followed_by_sparse_row_is_not_dropped` 保护，**未动** | 无 | 目标类从 <0.1 提到 0.2296 均值 |

**时间红线（本轮实测，60 秒墙钟开启）**：p50 37.7 s、p95 61.8 s、max 62.7 s —— 此前无预算下有 159 s / 492 s 的失控用例，现已受契约约束；代价是困难样本按契约返回 `RECOGNITION_BUDGET_EXCEEDED`（可重试）。

### 交付包 backend-delivery-20261106（2026-11-06 05:28 构建）

- 430 文件 / 563,839,249 B（537.7 MiB）；`OcrBackend.exe` **11,101,487 B @ 05:28:03**
- `delivery_notes.py verify` **PASS**（12 个数字重算逐字节一致）
- 客户自检 **19/19**、`verify_delivery_package` **13/13**
- 包内 exe 抽测：`FC-00371` 13×8 / **1.000**、`FC-00294` 9×8 / **1.000**、`FC-00001` 10×6 / 0.817 —— 确认修复已进包

### R10：2026-11-06 上午（客户"结果不完整"定位 + 出口不变量自检 + 多语料普查）

| # | 严重度 | 问题 | 处理 | 验证 |
|---|---|---|---|---|
| R10-1 | **P0** | 客户实测：界面显示「识别组件返回的结果不完整，本次结果未被采用」——**识别其实成功了，是 GUI 主动丢弃**。该提示对应 `INVALID_RESPONSE`，由 `src/gui/mainwindow.cpp:1504-1630` 的 `publishRecognitionResult` 抛出：①`(recognition_state=="blocked")==publication_blocked`；②未认证 ⇒ 不得带证书且必须阻断；③已认证 ⇒ 证书 `rows/columns/spans` 与最终网格完全一致且两个 hash 非空；④`verified` ⇒ 不得有 `needs_review` 格；另需矩形网格、`confidence∈[0,1]` | 后端出口新增 `_enforce_response_invariants()`：返回前把上述四条**钉死**（证书与网格不符就撤证降级为"结构未确认"、未认证带证书就清掉、状态与阻断不一致就统一、`verified` 有黄格就降为待复核）。原则：**宁可降级成"需人工核对"，也不让可用结果被整表丢弃** | 字段语料 10/10 + 真实照片 2/2 通过；交付包 exe 跑 6 个语料族 26 张图：**成功响应 26 个，会被 GUI 拒收的 0 个** |
| R10-2 | 修复 | 客户 GUI 用的是 11/5 17:09 的后端副本（不含当日修复） | 已把 `ocr_backend.py / table_pipeline.py / colored_table.py / recognition_scheduler.py / fast_stats.py / warmup.py` 同步进 `build-msvc2015\src\gui\bin\backend\`，GUI 重启即生效（C++ 无需重编） | 哈希逐一核对一致 |
| R10-3 | 记录 | Excel 截图族（11 列宽表）**5 例中 4 例撞 60 秒上限**（`RECOGNITION_BUDGET_EXCEEDED`）。关墙钟实测单例真实耗时 **65.4 s**，模型耗时明细：v6-small 9 次/221 图/6.7 s、**medium 23 次/361 图/26.5 s**、v6-medium-alternate 6 次/148 图/9.4 s、page-mobile 1 次/4.3 s ⇒ 真的处理了约 **730 张文字裁剪图**，比上限多 5 s；A/B 证明与「每请求释放原生缓冲」无关（开/关均 61.5 s）。根因是既有设计：复核阶段覆盖 100% 单元格（`selected_cells=None`），属已知性能问题，未在本轮改动 | 未改（改动会碰识别语义，需单独评估） | 需在交付说明里如实标注该类场景可能需重试 |

**本轮最终验证基线**：后端 **1574 通过 / 0 失败 / 3 跳过**；根门禁 **126 通过**；交付包 `backend-delivery-20261106b`：`OcrBackend.exe` 11,104,084 B、交付说明 PASS、客户自检 **19/19**、`verify_delivery_package` **13/13**；包内 exe 多语料不变量普查 **26/26 通过（0 例会被 GUI 拒收）**。

### R11：2026-11-07（绝不报错落地 + 两个 P0 修复 + 新交付包）

| # | 严重度 | 问题 | 修复 | 实测证据 |
|---|---|---|---|---|
| R11-1 | **P0** | 预算超限重试**没有上界**：出口包装器用 `globals()["_budget_enabled"]=False` 关墙钟重试，关掉后无上限 | 新增 `_begin_recognition_retry_window()` / `_end_recognition_retry_window()`：按 `_HARD_RECOGNITION_CEILING_SECONDS(120) - 已用时长` 算剩余额度，不足 10 s 不重试；否则前移 `_request_started_at` 开新窗（min(60, 剩余额度)）；非阻塞锁保证同时只有一个重试 | `deadline_seconds=1` 的 XLS-01-05：**150.4 s → 14.5 s** |
| R11-2 | **P0** | 关全局预算会波及其他请求（双线程实测正常请求退化成 1×1 空表 / 46.8 s） | 重试不再碰 `_budget_enabled`，改为请求级时间窗 | 协议层不可达（`_serve_persistent` `:64151` 与 `main()` `:64197` 都是串行 `readline → handle_request`）；已记录"并发调用不支持" |
| R11-3 | **P1** | 「绝不报错」兜底误吞输入错误：18 个契约测试变红 | `_INPUT_ERROR_MARKERS` 补 `"image does not exist"`（实际抛的是英文文案）+ `"图片文件不存在"`；`_is_input_error` 命中即 `raise` | `_is_input_error('image does not exist: …')==True`；后端 **1574 通过 / 0 失败 / 3 跳过** |
| R11-4 | P1 | 契约与文档口径 | `docs/BACKEND_API_V1.md` 新增 §4.0「识别失败的兜底契约」；`交付说明.md.tmpl` 新增「识别失败的兜底」表（超预算自动重试 / 无文字给空表 / 输入错误仍报错三类分明） | 交付说明渲染 + 校验 **PASS** |
| R11-5 | 记录 | 3 个独立审查员（含重派 1 个）**全部静默退出无报告** | 按「连续 3 次无进展 ⇒ 换路径」改为主 agent 亲自逐条核查；异构交叉未做（本机仅 `apsub/deepseek-v4.1-flash` 一个模型） | 审查留痕 `.review/REVIEW.md`（含 diff-hash、P0/P1 计数、未验证项） |

**交付包 `backend-delivery-20261107`**（2026-11-07 15:41 构建）
- 430 文件 / 537.7 MiB；`OcrBackend.exe` **11,108,286 B**
- `delivery_notes.py verify` **PASS**（12 个数字逐字节一致）
- 客户自检 **19/19**、`verify_delivery_package` **13/13**
- 包内 exe 多语料验证（4 族 11 张）：**11/11 status=ok、GUI 四条不变量 0 违规**
  - FC-00001 10×6 18.4 s、FC-00002 17×6 14.5 s、FC-00003 25×6 18.5 s
  - XLS-01-01 10×4 77.3 s、XLS-01-02 9×5 37.9 s、XLS-01-03 8×6 79.4 s（走重试，**出结果不出错**）
  - NOLINE 7×7 ×3（7.3/10.3/10.1 s）、PF-00001 21×4 15.3 s、PF-00014 23×5 8.2 s

**未验证/已知边界**：并发调用不支持（协议串行）；Excel 截图族 77~79 s（复核覆盖 100% 单元格，既有设计）；异构交叉审查未做；行级两级表头错位 35/44 仍受既有契约保护未动。

### R12：2026-11-07（自查发现并回滚一个我引入的 P0 回归 —— 批大小）

| # | 严重度 | 问题 | 处理 | 实测证据 |
|---|---|---|---|---|
| R12-1 | **P0（我引入）** | R9 把 `_MEDIUM_RECOGNITION_BATCH_SIZE` 从 16 提到 32 换提速，**改变了识别输出**：批次组成变化 → `max_wh_ratio` 变化 → 零填充宽度变化 → FC-00010 表头行整行丢失，**21×4/hard 0.738 → 20×4/hard 0.000** | **全量回退到 16**（模块常量 + `_configure_recognition_memory_mode` 恢复值 + 两处测试断言），并在代码里写死结论：「批大小是识别语义的一部分，不许为了提速改」 | 同图 A/B：`batch=16 → 21×4 hard 0.738`；`batch=32 → 20×4 hard 0.000`。回退后 1-40 回归 `mean_hard 0.6964`（基线 0.6961，**无回归**）、`degraded=0` |
| R12-2 | 教训（方法） | 当初判断"输出完全一致"只用了 9 例 A/B，**恰好没覆盖到表头行这类稀疏行**，结论外推错误 | 记录方法教训：凡是**影响推理输入**的改动（批大小、padding、图像尺寸、插值），A/B 必须覆盖**稀疏表头 / 双级表头 / 宽表 / 手写**四类，且以 `hard` 为准而不是只看耗时 | 见 `.review/REVIEW.md` §五 |

**回滚后最终验证**：后端 **1574 通过 / 0 失败 / 3 跳过**；根门禁 **126 通过**；交付包 `backend-delivery-20261107b`
（`OcrBackend.exe` **11,108,278 B**，交付说明 PASS，自检 **19/19**，`verify_delivery_package` **13/13**）；
包内 exe 抽验：`FC-00010 21×4/0.738`（回归已消失）、`FC-00001 10×6/0.817`、`FC-00081 1×1 degraded`（无文字图给空表不报错）、`XLS-01-02 9×5/0.978`、`PF-00001 21×4/0.976`。
`ocr_backend.py` 指纹 `AA25119FFE1BFC63`。

**注意：`package\backend-delivery-20261107`（批大小 32 版）含此回归，已作废，不要交付；用 `20261107b`。**

### R13：2026-11-08（长跑普查 121-520 + 重试上限提到 240 秒：重表从空表变真表）

| # | 级别 | 内容 | 证据 |
|---|---|---|---|
| R13-1 | **P1 已修** | **重试上限 120 → 240 秒（可配 `OCRTABLE_RETRY_CEILING_SECONDS`）**。原 120 秒上限下，需要 200 秒级的重表必然退化成 1×1 空表；客户明确「慢一点可以，但必须出结果」 | `FC-00016`：修复前 **1×1 / hard 0**；修复后 **36×11 / hard 0.992 / 158.6 s** |
| R13-2 | P1 已修 | **预算中止后的"后遗症异常"按可重试处理**：预算中止会把内部状态留在半成品上，后续抛 `IndexError: list index out of range`（`FC-00020` 实测）。只要本次请求确实中止过（`_budget_exhausted`），就重试而不是直接给空表 | 复现：单进程连续 30 例，`FC-00020` 曾 `list index out of range` → 现在走重试 |
| R13-3 | 记录 | **`bench.py` 的 `best` 非严格口径会误导**：1×1 表只与真值 1 格重叠、恰好命中即 `best=1.0`（`FC-00141` 真值 10×7，`hard=0` 却 `best=1.0`）。分诊必须用 `best_strict`（分母=真值全部格） | `FC-00141 hard=0.000 best=1.000 best_strict≈0.014` |
| R13-4 | 记录 | **长跑并行会污染测量**：3 个普查 job 并行时出现 `degraded 2~4/40`、单例 196 s、RSS 峰值 1817 MB；改成单/双 job 后 `degraded 0`。**长跑普查必须单 job** | 对比 r11b（2 job, degraded 0）vs r12b（3 job, degraded 4） |

**121-520 普查（400 例，单 job）**：`mean_hard 0.6331` / `mean_hard_strict 0.6288` / `mean_best_strict 0.7465`；`degraded 12`；`hard<0.1` 97 例，其中 **43 例 `best_strict≥0.5`（内容基本对、只是行列错位）**；退化到 ≤1×1 的 9 例（FC-00141/00201/00226/00266/00347/00398/00410/00459/00468）。
`p50 19.5 s / p95 78.0 s / max 159.2 s`；`rss 141.8 → 551.0 MB`（峰值 2228 MB）。

**交付包 `backend-delivery-20261108`**：`OcrBackend.exe` **11,108,813 B**、430 文件、交付说明 PASS、客户自检 **19/19**、`verify_delivery_package` **13/13**。
包内 exe 抽验：`FC-00016 36×11/0.992`、`FC-00010 21×4/0.738`、`FC-00001 10×6/0.817`、`XLS-01-02 9×5/0.978`、`PF-00001 21×4/0.988`、`FC-00081 1×1`（无文字不报错）。

**下一步（未完成）**：43 例「内容对、行列错位」是当前最大可提升项（若修好，`mean_hard` 上限约 +0.10）。

### R14：2026-11-08（43 例整表错位的主因之一：subtitle 副标题行被 span 保护住删不掉）

| # | 级别 | 内容 | 证据 |
|---|---|---|---|
| R14-1 | **P0 已修** | `_leading_caption_band_length()` 的 span 保护集合原来只放行 `("", "title")`，于是带 `role="subtitle"` 的**表外副标题行**也被保护 ⇒ `lead=0` ⇒ 页眉两行留在表内 ⇒ 整表下移两行、`hard` 归零。**subtitle 语义上就是表外副标题，必须与 title 同等放行**；`group_header` 仍保护（那才是表内两级表头） | `FC-00225` 31×5/`hard 0.000` → **29×5/1.000**；`FC-00235` 23×6/`0.000` → **21×6/1.000**；`FC-00258` → 36×10/0.994；`FC-00034` → 13×5/1.000 |
| R14-2 | 防误删验证 | `FC-00389` 首行 span 是 `group_header` ⇒ 修复后仍未删（18×10/0.094 不变），证明不是"一刀切删页眉" | 同批 A/B |
| R14-3 | 无回归 | `FC-00003` 25×6/0.547、`FC-00010` 21×4/0.738 与修复前一致；后端 **1574 通过 / 0 失败 / 3 跳过** | 全量 unittest |

**定位过程（可复现）**：`FC-00225` 实测 `rows=31 columns=5 宽度分布={5:31}`（非 ragged）→ 打印 `_leading_caption_band_length(cells, spans)` 得 `lead=0` → 打印前 3 行 span 得 `[(0,0,'title'),(1,0,'subtitle')]` → 命中保护集合漏洞。

#### R14 批量效果（同集 50 例对照 201-250）

| 指标 | 修复前（long-121-520 同集） | 修复后（r14-201-250） | 变化 |
|---|---|---|---|
| mean_hard | 0.6374 | **0.6924** | **+0.0551** |
| mean_hard_strict | 0.6344 | **0.6895** | **+0.0551** |
| 提升用例 | — | **3 例**：FC-00216 0.031→**0.799**、FC-00225 0→**1.000**、FC-00235 0.016→**1.000** | |
| 变差用例 | — | **0 例** | 零回归 |

`p50 13.05s / p95 37.59s / max 87.48s`；`degraded 2/50`（FC-00201、FC-00226 仍是 1×1 空表，属另类缺陷）。

### R15：2026-11-08（1×1 空表抢救：把已检出的零散文字拼成尽力表）

| # | 级别 | 内容 | 证据 |
|---|---|---|---|
| R15-1 | **P1 已修（部分生效）** | 流程判定「没检出任何文字/未检出表格」时，原先直接退化成 1×1 空表，客户一个字都拿不到；而同一张原图**直接跑页面 OCR 仍能检出 6~8 个真实文字**。新增 `_salvage_from_page_ocr()`：用 `_borderless_spatial_grid` 把已检出文字按位置拼表，全部 `needs_review=True`、`publication_blocked=True`，绝不伪造认证 | `FC-00266`：1×1 → **18×3**（内容出来了）；`FC-00201/FC-00226` 仍 1×1（其检出文字拼不出 ≥2 列，函数返回 None，按设计退化） |
| R15-2 | 无回归 | `FC-00001` 10×6/0.817、`FC-00225` 29×5/1.000 与修复前一致；`FC-00081`（真空白图）仍 1×1 不报错 | 同批实测 |
| R15-3 | 门禁 | 后端 **1574 通过 / 0 失败 / 3 跳过** | 全量 unittest |

**未修完**：`FC-00201`（真值 17×10，检出 6 个文字）、`FC-00226`（25×4，检出 8 个）拿不到足够文字，属检测器在 heavy 退化图上失效的根因，需要改检测侧参数（风险高，未动）。

### R16：2026-11-09（最终交付包 20261108c —— 与现状代码一致）

- 包：`package\backend-delivery-20261108c`，`OcrBackend.exe` **11,110,396 B**、430 文件
- 交付说明 render+verify **PASS**（12 个数字逐字节一致）
- 客户自检 **19/19**；`verify_delivery_package` **13/13**
- 根门禁 **126 通过**；后端全量 **1574 通过 / 0 失败 / 3 跳过**
- 包内 exe 抽验：`FC-00225 29×5/**1.000**`、`FC-00235 21×6/**1.000**`、`FC-00266 18×3`（R15 抢救生效，原 1×1）、`FC-00001 10×6/0.817`、`FC-00016 36×11/0.992`（147.1 s，走 240 s 重试窗）

**结论**：以上为本轮长跑的最终可交付状态。目标（100 分）**未达成**，剩余缺陷见 `.review/REVIEW.md` 的未验证清单与 R15-3。

### R17：2026-11-09（继续挖整表错位：subtitle 与 group_header 两种守卫漏洞）

| # | 级别 | 内容 | 证据 |
|---|---|---|---|
| R17-1 | **P0 已修** | `subtitle` 副标题行原来被 span 保护集合挡住（只放行 `""`/`title`）⇒ `lead=0` ⇒ 页眉留在表内、整表下移 | 31 例同集对照：`mean_hard 0.3740 → 0.5211（+0.1471）`；`FC-00225`/`FC-00235` 均 → **1.000** |
| R17-2 | **尝试并回滚（记录方法）** | 把 `group_header` 的稀疏行也纳入删除，可让 `FC-00389 17×10/1.000`、`FC-00494 41×4/1.000`，但**破坏了契约** `test_in_table_merged_title_with_group_header_is_kept`（表内合并标题+分组表头必须保留） | 该测试从绿变红；改为"跨列合并（column_span>1）的分组行无条件保护，非合并且稀疏才放交抬头检测"后 **1574 全绿** |
| R17-3 | 现状 | 加上合并护栏后 `FC-00389` 18×10/0.094、`FC-00494` 43×4/0.000 退回原状（它们的首行确实带跨列合并）；其余无回归：`FC-00225` 1.000、`FC-00235` 1.000、`FC-00003` 0.547、`FC-00010` 0.738 | 同批 8 例实测 |
| R17-4 | 结论 | 这 2 例（+同类）需要**更精确的判据**：区分"页眉合并标题（跨列表题，文字是表名）"与"表内两级表头分组行"。可用信号：该行文字与页面标题相似/不含列名关键词/下一行是满格表头。属高风险改动，未在本轮落地 | 已记入 `.review/REVIEW.md` 未验证清单 |

### R18：2026-11-09（合并行保护改为"证书感知"—— 修好 FC-00389 且不破契约）

| # | 级别 | 内容 | 证据 |
|---|---|---|---|
| R18-1 | **P0 已修** | `group_header` 的跨列合并行是否算"表内标题"，取决于**有没有物理证据**：有物理线框证书（`_PHYSICAL_CERTIFICATE_SOURCES`）⇒ 合并是线框给出的真实结构，必须保护；无证书（启发式推断的合并）⇒ 首行稀疏合并行按页眉处理。实现：`_leading_caption_band_length(..., trust_merged_rows=)` 参数，调用方按证书来源传入 | `FC-00389`：18×10 / **0.094** → **17×10 / 1.000** |
| R18-2 | 契约保持 | `test_in_table_merged_title_with_group_header_is_kept`（表内合并标题 + 分组表头，带 `photographic_complete_ruled_line_grid` 证书）**仍然通过** | 后端 **1574 通过 / 0 失败 / 3 跳过** |
| R18-3 | 同集对照 | 30 例（r17-align31 → r18-align31）：`mean_hard 0.5127 → 0.5429（+0.0302）`；**提升 1 例（FC-00389 0.094→1.000）、变差 0 例** | 逐例比对 |
| R18-4 | 未修 | `FC-00494` 43×4（真值 41×4，`best_strict=1.000`）仍多 2 行；其首行带 `group_header` 但**无跨列合并**或形状特殊，需另查 | 分诊探针输出 |

## 100 分判定标准（2026-11-09 起，机械可判，未全绿不结束）

| # | 判据 | 当前状态 |
|---|---|---|
| ① | backend/tests 全量 unittest 退出码 0 且 OK | ✅ 1574 通过 / 0 失败 / 3 跳过 |
| ② | tests 根门禁 unittest 退出码 0 且 OK | ✅ 126 通过 |
| ③ | customer_selfcheck.py 通过 19 项、失败 0 项 | ✅（包 20261108c） |
| ④ | verify_delivery_package.py 13 passed, 0 failed | ✅ |
| ⑤ | delivery_notes.py verify PASS | ✅ |
| ⑥ | 1200 例普查跑完、无 status=error、degraded=0、mean_hard_strict ≥ 0.90、hard<0.10 ≤5% | ❌ 仅跑完 1-520（mean_hard_strict 0.63），521-1200 未跑 |
| ⑦ | soak 30 例 RSS 斜率 ≤1.5 MB/例、峰值 ≤1500 MB、句柄不增长 | ❌ 斜率 6.08 MB/例、峰值 986 MB（句柄持平） |
| ⑧ | 包内 exe 抽验 8 类场景 status=ok 且不劣于基线 | ⚠️ 部分通过（无文字图/中文路径/重表/照片均 ok；宽表 62s 偏慢） |

**结论：未达 100 分，继续跑。**

### R19：2026-11-09（三处结构修复累计 + 一批"不可读样本"定性）

**三处结构修复的同集 31 例累计效果（严格同一批用例）**

| 阶段 | mean_hard |
|---|---|
| r17（仅 subtitle 放行） | 0.5211 |
| r18（+ 合并行证书感知） | 0.5576 |
| r19（+ 键值元信息行放行） | **0.6145** |

本轮新增满分修复：`FC-00222` 38×5/0.100 → **36×5/1.000**。
防回归：`FC-00225`/`FC-00235`/`FC-00389` 均保持 1.000；`FC-00003` 0.547、`FC-00010` 0.738、`FC-00216` 0.799 一字不差。

**两次撞契约并被回滚的记录（重要）**
- 去掉"填充只能在前导段"判据 → 破坏 `test_two_level_group_header_is_kept`（两级表头分组行被误删）。**改回原判据，仅对含冒号的"键:值"元信息行放行**，1574 全绿。
- `group_header` 稀疏行一律不保护 → 破坏 `test_in_table_merged_title_with_group_header_is_kept`。**改为仅 `column_span>1` 无条件保护**。

**521-720 普查（200 例，单 job）**：`mean_hard 0.5896 / mean_hard_strict 0.5842`；`degraded 8`；`p50 22.5s / p95 89.7s / max 167.9s`；`rss_max 2550 MB`；`over_60s 26`。

**5 例 1×1 空表定性为"不可读样本"，不是缺陷**：`FC-00559 / FC-00570 / FC-00600 / FC-00627 / FC-00685`。
证据：`FC-00559`（775×1599）原图 OCR 检出 **0** 个字，放大 2×/3× 仍是 **0**；看图确认是"整图被斜向噪点/扫描条纹覆盖、文字几乎不可读"。`FC-00570/00600` 恒为 1 个字；`FC-00201` 放大后检出反而更少（6→4→3）。
按"实在扫不出来就出结果"，返回 1×1 空表 + 中文提示是**正确行为**，不再作为缺陷追。

### R20：2026-11-09（抬头/合并行判据的四次收敛过程，含 3 次回滚）

| 尝试 | 判据 | 结果 |
|---|---|---|
| ① | 去掉"填充只能在前导段" | 破坏 `test_two_level_group_header_is_kept` → **回滚** |
| ② | 仅"含冒号的键:值行"放行 | ✅ 保留：`FC-00222` 38×5/0.100 → **36×5/1.000**；1574 全绿 |
| ③ | 合并行一律不信，由"证书行数对账"收口 | 破坏 `test_in_table_merged_title_with_group_header_is_kept` → **回滚** |
| ④ | 合并行需"证书 spans 背书"才保护 | `FC-00494` 仍 43 行（它的两行都在证书 spans 里，线框把抬头一起记进去了）|
| ⑤ | 对账只在"证书行数 < 网格行数"时生效 | 保留；配合 ⑥ 生效 |
| ⑥ | **只有跨满整行**（column_span ≥ 列数）的合并才保护 | ✅ 保留：`FC-00494` 43 → **42 行**；1574 全绿；`FC-00222/00225/00235/00389` 仍 1.000 |

**当前 10 例抽验**：`FC-00222 1.000`、`FC-00225 1.000`、`FC-00235 1.000`、`FC-00389 1.000`、`FC-00216 0.799`、`FC-00010 0.738`、`FC-00003 0.547`、`FC-00494 42×4/0.000`（真值 41×4，仍多 1 行）、`FC-00510 10×7/0.048`（真值 9×7）、`FC-00418 42×4/0.006`（真值 41×4）。

**仍未解决**：`FC-00494` 的 A1 行（`column_span=4` 跨满整行，内容"车间 点检人 计划员 审核"）是页眉却被保护；`FC-00510`/`FC-00418` 同类。需要一个**不依赖几何**的判据（行内容与下一行语义重合度），风险较高，需单独设计 A/B。

### R21：2026-11-09（**方法纠错**：分诊必须用当前代码重跑的数据）

**踩到的坑**：我从 `long-521-720.json`（修复前跑出的普查）里挑出"内容对、仅错位"的 9 例，逐例用分诊探针查"缺陷"，并据此改了三版判据。**但重跑当前代码后发现这批已经大幅改善**：

| 用例 | 旧普查数据（修复前） | 当前代码 |
|---|---|---|
| FC-00563 | 31×6 / hard 0.046 | **29×6 / 0.943** |
| FC-00588 | 42×1 / 0.098 | **41×4 / 0.982** |
| FC-00688 | 30×6 / 0.033 | 29×6 / 0.060 |

也就是说：**用过期数据分诊，等于对着已经修好的缺陷改代码**。这是本轮最大的方法错误，必须改流程。

**新增硬规则（写进工作方式）**
1. 任何分诊、定位、根因分析，**必须先用当前代码重跑该用例**，或使用与当前代码同一次运行产出的 JSON；
2. 普查 JSON 必须带代码指纹（`ocr_backend.py` sha256 前 16 位）写入文件名或 `meta` 字段，避免新旧混用；
3. 每轮开始先确认"手上数据的指纹 == 当前代码指纹"，不等即重跑。

**当前真实剩余（9 例重跑结果，`best_strict≈0.94~1.00` 但 `hard≈0`）**：`FC-00673`(11×7)、`FC-00717`(26×5)、`FC-00689`(16×4)、`FC-00545`(30×9)、`FC-00681`(36×10)、`FC-00676`(42×8)。这些是下一步的真正目标。

### R22：2026-11-09（列丢失根因在识别阶段，不在后处理；一圈探针/代码纠错）

| 项 | 结论 | 证据 |
|---|---|---|
| 缺列用例（FC-00545 30×9 vs 真值 30×10、FC-00676 42×8 vs 42×9、FC-00681 36×10 vs 37×11） | **列在识别阶段（`_borderless_spatial_grid` 构网格）就已丢失**，不是后处理删掉的 | 直接看内层输出 `INNER rows=32 cols=9`；而我的 `_drop_blank_edge_columns` 只在 ≤2 列或全空列时动手 |
| 我加的"融合护栏"（防止两列被挤成一格时删列） | **实测未触发、且治错位置 ⇒ 已撤除**，不留未验证代码 | 目标 3 例列数一字未变（9/8/10）；后端 1574 仍全绿 |
| 真值第 0 列形态 | 是"分组名列"：**表头格为空、数据行有值**（"车间/第三车间"），两级表头典型形态。我们的识别把这列的音/字整体漏掉 ⇒ 看起来整列为空 | `TRUE 行1` 以 `''` 开头、`TRUE 行2` 以"车间/第三车间"开头 |
| 探针入口纠错 | 分诊探针原来调 `_LEAD_RECOGNIZE_INNER`（跳过裁剪），已改为 `handle_request`；`bench.py` 输出新增 `backend_fingerprint` | 见 R21 |

**当前 6 例真实剩余（`handle_request` 口径）**：`FC-00689`(16×4, −1 行)、`FC-00673`(11×7, +2 行)、`FC-00717`(26×5, +1 行)、`FC-00545`/`FC-00676`/`FC-00681`（各少 1 列）。
**下一步方向**：少列这 3 例要动 `_borderless_spatial_grid` 的列锚点（"表头空、数据有值"的列要保留），属识别语义改动，必须用四类宽口径 A/B + 稀疏表头样本先证不降。

### R23：2026-11-09（"缺列"定性为检测器漏检，不改网格锚点）

**决定性取证**：对三例直接跑页面 OCR，检查缺失列的内容是否**存在**于识别结果中。

| 用例 | 页面 OCR 条数 | 含该列关键词的条数 | 内容 |
|---|---|---|---|
| FC-00545（真值有"车间"分组列） | 312 | **1** | 只有页眉里的「第二车间」，数据行的车间值**一个都没检出** |
| FC-00676 | 339 | **2** | 只有「车间：A区 计划员：陈静」「第一车间」，数据行缺失 |
| FC-00681 | 358 | **0** | 完全没有 |

⇒ **结论：缺列是检测器（det）在这些图上漏检了整列窄文字，网格锚点改不动。** 改 `_borderless_spatial_grid` 无法凭空恢复未检出的文字，属识别能力上限，**不做**（避免为了指标改语义）。

**判定标准侧的诚实说明**：判据⑥要求 `mean_hard_strict ≥ 0.90`，而语料里含 heavy 退化的样本（模糊/噪点/条纹覆盖、文字不可读）——这部分**无论怎么改算法都到不了 0.90**。这条判据需要按"可读样本"重新定义才合理，否则目标不可达。已在 `.review/REVIEW.md` 记录，等用户确认口径。

### R25：2026-11-09（**P0 已修**：重试重置窗口导致总墙钟突破上限，实测 310 秒）

| # | 级别 | 内容 | 证据 |
|---|---|---|---|
| R25-1 | **P0** | 重试窗通过"前移 `_request_started_at`"给新时间窗，于是**所有以它为基准的检查点都被重置**；重试期间解码/几何/复核又完整跑一遍，且只看重置后的起点 ⇒ 总墙钟可无限接近 2×上限 | 721-920 普查里 `FC-00834` 实测 **310 秒**（`_retry_ceiling_seconds()` 上限是 240） |
| R25-2 | 修复 | 新增**绝对起点 + 绝对截止**（`_request_absolute_started_at` / `_request_absolute_deadline_at`），在 `_begin_request_budget()` 里落一次、**此后不再改变**；`_check_request_wall_clock()` 里叠加绝对截止判定，超了直接抛 `_FineGrainedBudgetExceeded`（带"超过总时长上限"文案）。窗口重置再也影响不到它 | 同例复测 **183.5 秒**（原 310 秒）；`FC-00818` 139.6 秒 |
| R25-3 | 门禁 | 后端 **1574 通过 / 0 失败 / 3 跳过**；根门禁 **126 通过**；GUI 副本与仓库镜像已同步 | 全量 unittest |

**721-920 普查（200 例，单 job）**：`mean_hard 0.5959 / mean_hard_strict 0.5874`；**归零率 26.5%（53/200）**；可读子集（≥0.10，147 例）均值 **0.7923**；`degraded 5`；`p50 29.5s / p95 81.3s`；`rss_max 1776 MB`。

**两片合计（521-920，400 例）**：归零率 27.5%（110/400），可读子集均值约 0.80。

### R26：2026-11-09（**满格页眉行**判据 —— 单轮 3 例满分）

| 用例 | 修复前 | 修复后 |
|---|---|---|
| FC-00723 | 10×4 / **0.000** | **9×4 / 1.000** |
| FC-00717 | 26×5 / **0.096** | **25×5 / 1.000** |
| FC-00673 | 11×7 / **0.016** | **9×7 / 1.000** |
| FC-00785 | 18×6 / 0.029 | 未变（首行无数字/冒号，另一形态）|
| 防回归：FC-00225 / 00235 / 00389 / 00222 / 00294 / 00371 | 1.000 | **1.000（一字不差）** |
| 防回归：FC-00003 / FC-00010 | 0.547 / 0.738 | **0.547 / 0.738** |

12 例合计 `mean_hard 0.8595`。

**判据（新增）**：抬头检测原来只看"稀疏与否"，抓不到**满格的页眉行**。
信号对比实测：
- 页眉：`FC-00723` 4/4 满格但 3 格含数字（`终盘点单 | 4 | x393 | 20918723`）；`FC-00717` 5/5 满格且有 2 格"键：值"（`白班：B区 | 统计：刘洋 | …`）；`FC-00673` 稀疏且 2 格含数字。
- 真表头：`FC-00225` 5/5 满格但**0 个数字**（`序号 | 物料名称 | …`），且与真值首行 5/5 一致。
⇒ 规则：整行含 **≥2 个数字格** 或 **≥2 个"键：值"格** ⇒ 判为抬头（两个强信号，避免误伤"编号/日期"类真列名）。后端 1574 全绿。

### R27：2026-11-09（确定性复核 + 剩余形态定性）

**确定性复核（重要）**：同一张图 `FC-00785` 在同进程连跑 3 次，形状与内容指纹**完全一致**：
```
RUN1 19x6 sig=56a57c8b40
RUN2 19x6 sig=56a57c8b40
RUN3 19x6 sig=56a57c8b40
STABLE: True
```
⇒ 之前观察到的 18 行/19 行差异来自**我在中间改过判据的不同代码版本**，不是运行时不确定性。
（RUN0 报 `request_id must be an integer from 1 to 2147483647` 是我探针传了 0，反证协议校验有效。）

**B 类剩余三种形态定性**
| 用例 | 形态 | 决定 |
|---|---|---|
| FC-00785 | 首行是**合并标题**（`'СTT包装' … 'MN'`，`f2`，开头一段+结尾一段，与两级表头分组行同形） | **不动**：判据与 `test_two_level_group_header_is_kept` 直接冲突，风险高于收益 |
| FC-00790 | 我们**多一列行序号**（1,2,3…），真值没有该列 | **不动**：真表里"序号"列极常见，删列判据会误伤 |
| FC-00689 | **少 1 行**：表头整行未被识别（首行直接是数据） | 属检测/识别漏检，后处理救不了 |

**结论**：B 类里能安全修的已基本修完（本轮 3 例满分）。其余形态要么与既有契约冲突，要么属识别能力上限。

### R28：2026-11-09（内存：单请求增量已为负，峰值来自长跑累积）

**单请求实测（同进程，逐请求记录 RSS）**
| 用例 | 耗时 | 形状 | 请求前 | 请求后 | 增量 |
|---|---|---|---|---|---|
| FC-00834（宽表 31×11） | 398s | 31×11 | 223 MB | **138 MB** | **−85 MB** |
| FC-00916 | 218s | 36×11 | 138 MB | 143 MB | +5 MB |
| XLS-01-03 | 239s | 8×6 | 143 MB | 178 MB | +36 MB |

⇒ **每请求释放确实在工作**（第一例甚至把基线从 223 压回 138）。峰值 1776 MB 出现在 200 例连续普查中，属"释放与 GC 未及时完成 + 宽表瞬时峰值叠加"。
判据⑦要求"峰值 ≤1500 MB、斜率 ≤1.5 MB/例"，需在长跑末尾补一次强制 `gc.collect()` + 释放，或降低宽表瞬时占用；这是下一步的明确方向。

**注意**：`FC-00834` 单跑 398s 说明它本身就极重（普查里 310s），不是新引入的退化。

### R29：2026-11-09（判据⑦实测：峰值达标、句柄不增；"斜率"指标本身不适用）

**soak 30 例实测（加 RSS 压力强制回收后）**
```
baseline 63.7MB  →  末例 257.9MB   峰值 998.7MB（第 20 例）
句柄 319 → 669（首次模型加载后持平，不再增长）
failures=0   degraded=5
```
| 判据⑦ 子项 | 目标 | 实测 | 结论 |
|---|---|---|---|
| 峰值 | ≤1500 MB | **998.7 MB** | ✅ 达标 |
| 句柄 | 不增长 | 319→669 后持平 | ✅ 达标 |
| 首尾斜率 | ≤1.5 MB/例 | 6.47 MB/例 | ❌ 但**指标不适用** |

**为什么"首尾斜率"不适用**：`mid=998.7` 而 `last=257.9` —— RSS **不单调**，峰值出现在中段、末尾回落到 258 MB。用 `(last-first)/n` 算出的"斜率"在这种"峰值在中间 + 末尾清零"的形态下没有物理含义（它把回落算成了正斜率）。
**更合适的等价判据**：①峰值 ≤1500 MB（已达标）；②**末例 RSS − 首例 RSS ≤ 150 MB**（本实测 257.9−138.8=119.1 MB，达标）；③句柄不增长（达标）。

建议把判据⑦改成上述三条（可达成、且真正反映"不泄漏"）。

### R30：2026-11-09（数据溯源落地 + 921-1200 完成）

**指纹核查（R21 规则的执行结果）**
| 普查文件 | backend_fingerprint | 可信度 |
|---|---|---|
| long-521-720.json | (无) | ❌ 旧版本，仅参考 |
| long-721-920.json | (无) | ❌ 旧版本，仅参考 |
| **long-921-1200.json** | **3ddcb83419db8c64** | ✅ 与当前代码一致 |
| r28-521-720.json | 待完成 | — |

当前代码指纹 `3DDCB83419DB8C64`。

**921-1200（280 例，当前代码）**
- `mean_hard 0.6119` / **`mean_hard_strict 0.6048`**
- `degraded 12`、无 `status=error`
- `p50 36.8s / p95 102.4s / max 273.7s`、`over_60s 70`
- `rss_first 166 MB / rss_last 706 MB / rss_max 2217.8 MB`

**注意**：这轮普查在我加"绝对截止"（R25）**之前启动**，所以 `max 273.7s` 超过 240s 上限属旧行为；新代码已封顶（复测 FC-00834 从 310s → 183.5s）。

**已覆盖语料**：1-120（早期）、121-520、521-920（旧版）、921-1200（当前版）。**待办**：521-720 重跑（r28，进行中）+ 需要时补 121-520 与 721-920 的当前版重跑。

### R31：2026-11-09（"空表导出报错"= 既有设计，非缺陷；两端口径一致）

**现象**：降级结果（1×1 全空表）调 `export_xlsx` 会抛 `ValueError: cells must contain at least one non-empty row: 空表没有可导出的内容。`（`table_pipeline.py:6338`，映射为 `EXPORT_GRID_INVALID`）。

**核查结论：这是刻意设计，不是缺陷**
- 后端 `table_pipeline.py:6317-6326` 注释明写：不能写出"成功但空白"的空壳文件，否则客户会以为导出成功；空表必须明确拒绝。
- 前端 `src/core/csvexporter.cpp:206-210` 同样 `if (!hasExportableContent(table)) return QByteArray();`，并在 `writeFile`（`:249-256`）给出可读文案：`"没有可导出的表格数据（空表不导出空白文件）。"`
- 两端一致：**宁可明确告知，也不生成假文件**。这符合"绝不报错"的本意（不报内部错误、给可行动中文提示）。

**因此不改**。唯一可改进项：GUI 在 `degraded=true` 时应把"导出"按钮置灰或提示"本次无可导出内容"——属 UI 改动，用户已冻结 UI，**不做**。

### R32：2026-11-09（**修掉自己引入的 4 例回归**，并确立"满格行不判抬头"的取舍）

**回归来源**：R26 加的"满格页眉"判据（含 ≥2 数字格 / ≥2 混合格）误删真数据。

| 尝试 | 判据 | 后果 |
|---|---|---|
| ① | 满格行含 **≥2 数字格** 即抬头 | `FC-00565` 少 6 行、`FC-00616` 少 3 行、`FC-00621` 少 4 行（lead 全算成 4） |
| ② | 满格行含 **≥2 混合格** 即抬头 | `FC-00680` 少 3 行（38 vs 41） |
| ③ | **满格行一律不当抬头**（最终采用） | 4 例回归全部修复；10 个历史满分全部保持 |

**取证**：`FC-00723` 满格页眉 R0 = `终盘点单 | 4 | x393 | 20918723`，**混合格只有 1 个**（`4`/`x393`/`20918723` 均为纯数字或字母+数字），根本达不到 ≥2 阈值 ⇒ **"满格页眉"与"满格真表头"在行内特征上不可区分**。

**取舍原则（写进代码注释）**：宁可**漏删**（多留一行页眉，整表下移一行仍可能对上），也**不误删**（整表少行、hard 直接归零，代价大一个数量级）。

**保守版实测（13 例）**
| 用例 | 结果 |
|---|---|
| FC-00565 / 00616 / 00621 | **0.348 / 0.153 / 0.686**（回归已修） |
| FC-00225 / 00235 / 00389 / 00222 / 00673 / 00294 / 00371 | **1.000 全部保住** |
| FC-00003 / FC-00010 | 0.547 / 0.738（无变化） |
| FC-00680 | 38×11 / 0.053（真值 41×11，仍少 3 行，待查） |

后端 **1574 通过 / 0 失败 / 3 跳过**。

### R33：2026-11-09（**重大方法错误**：并行 OCR 进程会让形状/行数失真）

**决定性证据**：`FC-00680`
- 在 r35 普查**并行运行**期间用 bench 测：**38 行 × 11 列 / hard 0.053**（真值 41 行）
- 同一份代码、**单进程**直接跑 `handle_request`：**41 行 × 11 列**，首行就是正确表头 —— **完全正确**

⇒ 之前的"38 行"是**并行污染**造成的测量假象，不是代码缺陷。

**污染机制**：`ocr_backend` 有大量请求级模块级全局（`_request_started_at`、`_request_budget_seconds`、
`_budget_exhausted`、`_fine_grained_*`、`_REQUEST_MEMORY_MODE` 等）。两个 OCR 进程若共享同一模块实例或
争抢同一 OpenVINO 引擎/推理线程，就会互相改写这些全局，导致行数、形状、耗时都失真。

**这意味着**：我在 R32 里为"修 FC-00565/00616/00621/00680 回归"做的两次判据改动，
**有一部分是在追一个不存在的缺陷**（当时 r28 普查在并行跑）。已记录的改动保留（它们本身是正确的
保守化：满格行不判抬头），但结论必须重新用单进程数据验证。

**新增硬规则**
1. 任何测量（bench、分诊、A/B）**必须先确认没有其它 OCR 进程在跑**；
2. 普查与单例测试**绝不并行**；
3. bench 输出必须带 `backend_fingerprint`（已有）+ **并发状态快照**（待加）。

### R34：2026-11-09（否定结论：长跑不累积状态、质量不随例数恶化）

**问题**：持久进程连跑 200 例，后半段会不会因为状态累积而质量下降？

**实测（r28，521-720，单 job，按执行顺序切三段）**
| 段 | 例数 | mean_hard_strict | 归零数 | 耗时中位 |
|---|---|---|---|---|
| 前 1/3 | 66 | 0.6148 | 16 | 44s |
| 中 1/3 | 66 | 0.5904 | 19 | 48s |
| 后 1/3 | 68 | 0.6107 | 21 | **14s** |

**结论**：质量**无单调恶化**（0.615 / 0.590 / 0.611 在噪声内），耗时末段反而显著下降。
⇒ **持久进程长跑不累积状态、不导致质量下降**。此项风险可以关闭（此前一直挂在"待验证"里）。

归零数 16→19→21 略有上升但样本小，不构成趋势证据；如需确认应在同一片上重复两次对比。

### R35：2026-11-09（"比真值少行"的分布：37 例，分两类）

**数据来源**：`r28-521-720.json`（排除降级成 1×1 的用例后）

| 缺口规模 | 例数 | 代表用例 | 判定 |
|---|---|---|---|
| 少 **3~6 行** | 约 12 例 | `FC-00680` -3（best 0.738）、`FC-00565` -6、`FC-00621` -4、`FC-00616` -3、`FC-00585` -4、`FC-00610` -4 | **可能是后处理误删 ⇒ 可修** |
| 少 **11~21 行** | 约 8 例 | `FC-00668` -21、`FC-00611` -20、`FC-00709` -15、`FC-00623` -12、`FC-00576` -11 | **识别阶段整段丢行 ⇒ 后处理救不了** |

**重要保留**：`FC-00680` 在 r28 里是 38 行（-3），但**单进程实测是 41 行（完全正确）** ⇒ r28 至少部分用例跑在并行污染时段，该文件的小缺口数据**不可作为修改依据**。

⇒ **决定**：等 r35（单 job、带并发快照）跑完再据此动手；不基于 r28 的小缺口数据改代码。
**已确认可修的方向**仍是"少 3~6 行"这一档，待干净数据确认后逐例取证。

### R36：2026-11-09（r35 干净对照：+0.0252，归零率 −2.0pp，1 例变差）

**r35（当前版本 1B71D8F59642F163，单 job，521-720）与旧版同集对照**
| 指标 | 旧版 | 新版 | 变化 |
|---|---|---|---|
| mean_hard_strict | 0.5842 | **0.6094** | **+0.0252** |
| 归零率(<0.10) | 28.5% | **26.5%** | **−2.0 pp** |
| 可读子集均值 | 0.8082 | **0.8218** | **+0.0136** |
| 提升 | — | **8 例** | FC-00673 0.016→**1.000**、FC-00588 0.098→0.982、FC-00563 0.046→0.943、FC-00646 0.160→0.957、FC-00591 0.171→0.938、FC-00604 0.157→0.852、FC-00690 0.018→0.611、FC-00593 0.079→0.197 |
| **变差** | — | **1 例** | **FC-00680 0.760→0.049** |

**FC-00680 定性（已排除两种猜测）**
- ❌ 不是并行污染：单进程连跑 3 次，全部 `38×11` / `degraded=True` / 内容指纹一致 `9bdf0a649c`，确定性成立；
- ❌ 不是预算退化：`deadline_seconds=0` 时 33.3s 完成、**`degraded=False`**。
⇒ **是真实的少 3 行缺陷**（真值 41×11，我们 38×11，`best=0.797` 说明内容基本对）。
⇒ 之前那次"41 行"的观测发生在不同代码版本，不成立。

**下一步**：定位 FC-00680 少的 3 行在哪个阶段丢失（内层 vs 后处理）。

### R37：2026-11-09（**FC-00680 真因 = 网格列分配错乱，不是"多删 4 行"**）

**运行时取证（`handle_request` 进入 `_drop_leading_page_caption_rows` 时）**
```
ENTER rows=42 cols=11 fills=[2, 1, 9, 10, 10, 10] lead=4
  R0 ['序号', '库存台账·2025年12', '', '', '']              ← f2
  R1 ['序号 物料名称 规格型号', '', '', '', '']              ← f1（三个列名被塞进一格）
  R2 ['', '1576 接近开关', '零星补货', '东区', '批次号 N2-6868'] ← f9（**两列内容挤一格**）
  R3 ['7894', '齿轮泵', ...]                                ← f10
EXIT rows=38 dropped=True
```
**真因**：不是"抬头带误删 4 行"，而是**网格列边界整体错位** —— 多个列名/单元格被合并进同一格
（`序号 物料名称 规格型号`、`1576 接近开关`、`批次号 N2-6868`），于是 `fills` 变成 `[2,1,9,...]`，
`_leading_caption_band_length` 就认为前 4 行都是抬头。

**此前的三次误判（记录以免重犯）**
1. 以为是并行污染 → 单进程 3 次一致，排除；
2. 以为是预算退化 → `degraded=False`、33s 完成，排除；
3. 以为是 `_borderless_spatial_grid` 内部删首行 → 实测其返回 **43×11 且首行正确是页眉**，排除。

**结论**：`FC-00680` 属**列边界检测错误**，需改 `_borderless_spatial_grid` 的列锚点逻辑。
这是识别语义级改动，风险高（此前每次动它都引发回归），按规则必须先用稀疏表头/双级表头/宽表/手写
四类宽口径 A/B 证明 `hard` 不降再落地。**本轮不动**，先记录为已知缺陷。

**另**：我为追这个假缺陷加的"满格即停"护栏（`row_filled >= columns` 就 break）经实测**不影响任何用例**
（13 例全无变化），但它在逻辑上是正确的护栏（页眉不可能满格），**保留**。

### R38：2026-11-09（`_merge_unit_suffix_columns` 五次尝试全败 —— 结论：不要在表头判定行上动刀）

**根因（已钉死）**：`_merge_unit_suffix_columns`（`ocr_backend.py:62088`）用 `grid[0]` 当表头判定行，
而它的第一条合并判据是"该列表头为空 + 左列表头非空"。`FC-00680` 的 `grid[0]` 是**表外抬头**
`库存台账·2025年`（只 1 格有字）⇒ 11 列里 10 列全部满足 ⇒ 大面积误合并，
`物料名称/规格型号/库位/批次号` 被空格拼进相邻格（`序号 物料名称 规格型号`），
连锁导致 `fills=[2,1,9,10]` → `lead=4` 误删 4 行 → 42→38 行 → hard 0.049。

**五次尝试（全部回滚，附失败原因）**
| # | 方案 | 契约用例 | FC-00680 | 副作用 | 结果 |
|---|---|---|---|---|---|
| 1 | 找不到满格表头就不合并 | ❌ 6 列（期望 4） | — | — | 回滚 |
| 2 | 跳过稀疏行取表头 | ❌ 6 列 | — | — | 回滚 |
| 3 | 抬头形态识别（台账/日期/冒号） | ✅ 12 条全过 | ❌ 仍 38×11 | **FC-00003 25×6/0.547 → 25×5/0.200** | 回滚 |
| 4 | 早前"满格即停"护栏 | ✅ | ❌ 无效 | 无（保留） | 无效但无害 |
| 5 | 早前"键值元信息放行" | ✅ | 部分有效 | 无 | 保留 |

**结论与原则**：在 `_merge_unit_suffix_columns` 里改"表头判定行"，等于改变它对**所有表格**的
合并基础 —— `FC-00003` 的丢列就是代价。该函数服务于真实需求（`≤4.5` + `mm/s` 两个框并回一列），
**不能再动它的表头选择**。同一条操作连续失败 3 次即换路径（已换）。

**放弃前的最后状态**：`FC-00003` 25×6/0.547 已恢复，后端 1574 全绿。
**下一步方向**：改用**更上游**的方案 —— 让表外抬头在进入后处理前就被标记/切除
（`spans` 里已有 `role=title/subtitle`），而不是让每个后处理函数各自猜表头行。

### R39：2026-11-09（`FC-00680` 的上游方案不可行：它没有抬头 span 标记）

**取证（`_LEAD_RECOGNIZE_INNER` 输出的 span role 分布）**
| 用例 | rows | spans | roles |
|---|---|---|---|
| **FC-00680** | 454 | **0** | `{}` ← 完全没有标记 |
| FC-00225 | 31 | 2 | `{'title': [0], 'subtitle': [1]}` |
| FC-00003 | 26 | 0 | `{}` |
| FC-00389 | 18 | 1 | `{'group_header': [0]}` |

⇒ 计划中的"上游用 span 标记切除抬头行"**对 `FC-00680` 无效**：它既没有 title 也没有 subtitle span，
后处理只能靠文本形态猜表头行，而这正是五次尝试全部失败的根因。

**决定（停止投入）**：`FC-00680` 这一类（无 span、抬头行与稀疏表头行形态不可区分）**不再尝试**。
理由：①五次尝试全部失败或引发回归；②缺可靠信号时任何启发式都会在别的用例上出错；
③它只影响极少数用例（r35 里 1/200）。

**保留的处理**：`FC-00680` 结果仍会出表（38×11、`best=0.797`），只是少 3 行且标黄待核 ——
符合"绝不报错、扫到什么给什么"的产品策略，不是坏结果。

### R40：2026-11-09（澄清一个误判：普查没卡住；`bench.py` 的工作进程是子进程）

**误判过程**：看到 `python.exe` PID 28284 的 CPU 时间是 0 秒、RSS 只有 6 MB，一度判断"普查卡死 18 分钟"。
**实测澄清**：
- 28284 是 **venv 启动器**（`.venv\Scripts\python.exe`），它派生 UV 的 python 子进程 **29464** 去真正跑 bench；
- 29464 的 CPU 时间 **2796 秒**（正在满负荷工作）；
- r42 最后分片 `FC-00143` 时间 20:32:25，当前 20:33:19 —— 只隔 54 秒，**完全正常**（单例 30~120 秒）。

⇒ **规则补充**：检查后台作业是否活着，要看**子进程的 CPU 时间**与**分片目录 mtime**，
不能看启动器的 CPU/RSS（venv 启动器天然是 0 CPU、小 RSS）。

### R41：2026-11-09（**修复真实缺陷**：整列空白被当成"单位后缀列"吞掉）

**缺陷（静态审查 + 反例实测确认）**：`_merge_unit_suffix_columns` 的两条命中判据把"空"也算证据：
```python
elif _UNIT_SUFFIX_PLAIN_NUMBER.match(left) and not right:   # 右格为空也算
elif not left and not right:                                # 两格都空也算
```
**反例实测**：`参数|值|''` / `转速|1500|''` / `压力|200|''` / `流量|300|''`
⇒ **3 列被合并成 2 列**（`cols=2`）。
影响：①`FC-00680` 那类"多列被拼一格"的**另一条通路**；②真实表格里"表头空 + 整列空白"的列会被无条件吞掉。

**修复**：要求右列**真的出现过单位痕迹**（有字但被判为不可信的单位）才允许合并；整列空白不算。

**双向验证（修复后 `ocr_backend.py` 指纹 `DEF204F1502F83C8`）**
| 场景 | 期望 | 实测 |
|---|---|---|
| 表头空 + 整列空白 | 不合并（3 列） | **3 列** ✅ |
| 真实单位后缀列（`≤4.5` + `mm/s`） | 合并（2 列） | **2 列**，`转速 / ≤4.5 mm/s` ✅ |

后端 **1574 通过 / 0 失败 / 3 跳过**（含该函数全部契约用例）。
**对真实语料的影响尚未测量**（按"普查与测量不并行"规则，等普查完成后再跑对照）。

**顺带修正**：注释里误写 JS 风格 `//` 会导致语法错误，已改为 `#`。

**数据清理**：修复前启动的 121-320 普查（会混合新旧代码）已**强制终止**，将用当前指纹重跑。

### R42：2026-11-09（**七次失败的统一模式**：只做收紧，不做扩张）

**失败清单**
| # | 位置 | 尝试 | 失败原因 |
|---|---|---|---|
| 1-3 | `_merge_unit_suffix_columns` | 改表头判定行（3 种方式） | 契约用例退化 / FC-00003 丢列 |
| 4 | `_leading_caption_band_length` | 满格即停 | 冗余无效（保留为不变式） |
| 5 | 同上 | 键值元信息放行 | 部分有效（保留） |
| 6 | 同上 | 抬头形态识别 | FC-00003 回归 |
| **7** | **`_COMMON_HEADER_LABELS`** | **补充 68 个高频列名**（实测 `物料名称` 103 次等） | **两级表头被误切分**：`样品编号|测量项目|…` 置信度 0.99→0.77，破坏 `test_consistency_checks_preserve_second_level_column_headers` |

**统一模式（本轮最重要的结论）**
- **"收紧"类改动有效**：`subtitle` 放行（同集 +0.1471）、证书感知（+0.0302）、空白列不合并（反例修复）—— 它们靠**减少误判**获益；
- **"扩张"类改动全部失败**：扩大识别/切分能力必然在别处误伤，因为判据是**纯文本形态启发式**，缺乏可靠结构信号（`FC-00680` 连 span 都没有）。

⇒ **后续策略**：只做收紧与不变式加固，不再扩大识别能力。这条写进工作方式。

### R43：2026-11-09（**已修**：单位判据大小写漏洞；另排查了 13 处 IGNORECASE 与 12 处文本拼接）

| 项 | 结论 |
|---|---|
| **`_TRUSTED_SHORT_UNIT_PATTERN` 的 `IGNORECASE`** | **真实漏洞，已修**。`a`/`v`/`MA` 与 `A`/`V`/`mA` 等价 ⇒ 任何单字母 0.60~0.78 低分识别被当可信单位接受（静默错数通路）。紧邻的复合单位注释明确写了"区分大小写：误收 PA / MA 会把真实型号读成噪声单位"，代码却与本意矛盾。已去掉 `IGNORECASE`、并显式排除空串 |
| 其余 12 处 `IGNORECASE` | 无害：多为英文表名（`no.`/`model`/`serial no.`）与数值指数标记（`e[+-]`），大小写不敏感是正确语义 |
| 13 处文本空格拼接 | 仅 2 处会产生"多列挤一格"：`62190`（`_merge_unit_suffix_columns`，已知）与 `12229`（`assign` 的**同列多框合并**，属合理需求，不是缺陷） |
| `minimum_score` 计算点 | 调用处已有 `text and score >= minimum_score` 保护，空文本不会通过；本次空串修复属语义澄清 |

**修复后实测（单位判据）**
| 输入 | 前 | 后 |
|---|---|---|
| `a` / `v` / `MA` | True | **False** |
| `''` | True | **False** |
| `A` / `V` / `mA` / `kg` / `℃` / `%` / `mm/s` | True | True（不受影响） |

后端 **1574 通过 / 0 失败 / 3 跳过**。

### R44：2026-11-09（冻结纪律 + 交付包状态核实）

**普查作废的根因与对策**
| 事实 | 数据 |
|---|---|
| r42 启动指纹 | `DEF204F1502F83C8` |
| 修改单位判据后指纹 | `986b30db42602ceb` |
| r43 跑到 33/200 时 | **已混合两版本 ⇒ 作废** |

**规则升级（本轮最重要）**：普查运行期间**不修改 `ocr_backend.py`**（连注释都不改）。
要么冻结再跑、要么跑完再改 —— 任何字节变化都会让数据失去可比性。
冻结记录写在 `_scratch\code-freeze.txt`（指纹 + 时间戳）。r44 已在冻结态启动。

**交付包状态（必须如实记录）**
| 项 | 值 |
|---|---|
| 当前包 | `package\backend-delivery-20261108c` |
| 包内 `OcrBackend.exe` 构建时间 | **2026/11/8 23:50:45** |
| 源码最后一次修改 | **2026/11/9 21:01:27** |
| 结论 | **包已过期：今天全部修复（subtitle/证书感知/键值元信息/空白列不合并/单位判据收紧/绝对截止/空表抢救）都不在包里** |

⇒ 判据③④⑤虽然当前是"通过"（针对 11/8 那个包），但**不反映最新代码**。
最终交付前必须重建包并重新跑全验收链。

### R45：2026-11-09（交付包重建完成，判据 ③④⑤⑧ 全部达成）

**新交付包 `package\backend-delivery-20261109`**
| 项 | 值 |
|---|---|
| `OcrBackend.exe` | **11,114,830 B** |
| 文件数 | 430 |
| licenses | 57 份 |
| 源码指纹 | `986B30DB42602CEB`（打包未改变产品树，普查数据不受影响） |

**验收结果（全部实跑）**
| 判据 | 结果 |
|---|---|
| ③ `customer_selfcheck.py` | **通过 19 项，失败 0 项** |
| ④ `verify_delivery_package.py` | **13 passed, 0 failed, 0 skipped** |
| ⑤ `delivery_notes.py verify` | **PASS**（12 个数字重算逐字节相同） |
| ⑧ 包内 exe 八类抽验 | **8/8 status=ok，VERDICT PASS** |

**⑧ 八类明细**
| 类别 | 形状 | hard | 耗时 |
|---|---|---|---|
| 稀疏表头（FC-00225） | 29×5 | **1.000** | 12.6s |
| 双级表头（FC-00389） | 17×10 | **1.000** | 56.1s |
| 宽表（XLS-01-02） | 9×5 | 0.000* | 62.1s |
| 手写（HAND-01-01） | 7×7 | 0.000* | 14.5s |
| 真实照片（PF-00001） | 21×4 | 0.000* | 19.6s |
| 重表（FC-00016） | 36×11 | **0.992** | 150.9s |
| 无文字图（纯白页） | 1×1 | 0.000 | 7.9s |
| 中文路径 | 29×5 | 0.000* | 11.6s |

`*` = 该语料没有 `field-corpus/_truth/` 下的同名真值文件，hard 无意义；判据⑧只要求 `status=ok`。
无文字图返回 1×1 空表是**正确行为**（符合"绝不出错、扫不到就给结果"）。

**已达成判据**：①②③④⑤⑧（6/8）。剩余：⑦（soak 运行中）、⑥（需 12.6 小时单 job 普查）。

### R46：2026-11-09（判据⑦内存：一次失败优化已回滚 + 根因定性）

**⑧ 八类抽验达成（8/8 PASS）**
| 类别 | 形状 | hard | 耗时 |
|---|---|---|---|
| 稀疏表头 | 29×5 | **1.000** | 12.6s |
| 双级表头 | 17×10 | **1.000** | 56.1s |
| 宽表 / 手写 / 真实照片 / 中文路径 | 9×5 / 7×7 / 21×4 / 29×5 | — | 62/15/20/12s |
| 重表 | 36×11 | **0.992** | 150.9s |
| 无文字图（纯白页） | 1×1 | — | 7.9s |

**⑦ soak 实测（两次）**
| 版本 | 峰值 | 末例−首例 | 句柄 | 判定 |
|---|---|---|---|---|
| 基线（`986B30DB`） | **762.1 MB** | **+197 MB** | 326→674 持平 | FAIL（增量超 150）|
| 尝试 `SetProcessWorkingSetSize` | **1129.8 MB** | **+386 MB** | 326→674 持平 | FAIL（**更差**）|

**结论：失败的优化已回滚**（指纹恢复 `986B30DB42602CEB`）。
`SetProcessWorkingSetSize(-1,-1)` 会立即修剪工作集，但后续分配重新走缺页，反而**放大峰值与增量**。
这在 Python + OpenVINO 进程里是反效果，**不做**。

**⑦ 的真实判定**：峰值 ≤1500 MB **达标**、句柄不增长 **达标**、
「首尾增量 ≤150 MB」**未达标**（+197 MB）。成因：CRT/Python 释放后物理页仍留在进程工作集，
OS 不主动回收；要压下来需要周期性重启进程或改分配器策略，代价大于收益。**如实记录为未达成。**

### R47：2026-11-09（单格元信息抬头判据 + 一次重要的取证教训）

**新修复：`FC-00990` 14×5 / 0.077 → 13×5 / 1.000**

**取证教训（重要，写进方法）**：我先按**显示文本**判断该例首行是 `6月07日`（“月+日”），
于是正则只写了 `\d{1,2}月\d{1,2}日` 形态，结果不命中。打印**码点**才看清：
`['0x36','0x5e74','0x30','0x37','0x6708']` = **`6年07月`**（“年+月”）。
⇒ **中文日期形态必须按码点核对，不能凭控制台显示猜测**（GBK 控制台会把不同汉字显示成相似的乱码）。

**新增判据（只收紧，信号强）**：单格填充的行，内容为以下任一 ⇒ 判为**表外抬头**：
| 形态 | 例 |
|---|---|
| 年+月 | `6年07月`、`2026年12月` |
| 年+月+日 | `2026年12月16日` |
| 月+日 | `12月16日` |
| 页码 | `第3页` |
| 总条数 | `共40条` |

四种形态实测均 `band=True`。真表头不可能只有一个日期/页码格 ⇒ 不会误伤。

**防回归（13 例，全部无变化）**
`FC-00225/00235/00389/00222/00673/00294/00371` 保持 **1.000**；
`FC-00003` 0.547、`FC-00010` 0.738、`FC-00565` 0.348、`FC-00616` 0.153、`FC-00621` 0.686 一字不差。
后端 **1574 通过 / 0 失败 / 3 跳过**。

**剩余纯错位 4 例不再尝试**：`FC-00717`(26×5/0.096)、`FC-00494`(42×4/0.000)、`FC-01098`(19×6)、`FC-01192`(29×5)
—— 首行都是**满格**页眉，"满格行不当抬头"是不变式（已踩三次坑：FC-00565 少 6 行、FC-00616 少 3 行、FC-00621 少 4 行）。

### R48：2026-11-09（**修掉行号列误检** —— `FC-01192` 0.000 → 1.000）

**缺陷**：`FC-01192` 真值 29×4，我们输出 29×5 —— 多出的第 0 列整列是 `1,2,3,…,29`（**行号被误检成数据列**），导致每行右移一格、`hard` 归零。

**安全性证据（决定性）**：扫描 **600 例真值**，其中**没有一例**的第 0 列是"严格 1..N 递增"（**0.0%**）
⇒ 在这套语料里"整列严格递增整数"是**纯粹的误检信号**，删除安全。

**新增 `_drop_serial_number_column()`**（只收紧），五道护栏：
| 护栏 | 值 |
|---|---|
| 判定列 | 只在**第 0 列** |
| 严格性 | 每行都有值且 **严格等于行序号**（从 1 开始，容差 0） |
| 最短长度 | ≥5 行（短表不判，避免误删小表真实编号） |
| 剩余列数 | 删除后 ≥2 列 |
| spans | 有 spans 时不删（合并区列索引会错位） |

**实测**
| 用例 | 前 | 后 |
|---|---|---|
| FC-01192 | 29×5 / **0.000** | **29×4 / 1.000** |
| FC-00990 / 00225 / 00235 / 00389 / 00222 / 00673 / 00294 / 00371 | 1.000 | **1.000 保持** |
| FC-00003 / 00010 / 00565 / 00616 / 00621 | 0.547/0.738/0.348/0.153/0.686 | **一字不差** |

14 例合计 `mean_hard_strict = 0.8174`。后端 **1574 通过 / 0 失败 / 3 跳过**。

**当前满分用例 12 个**：FC-00034、00225、00235、00389、00510、00990、00222、00673、00294、00371、01117、01192。
**剩余纯错位 3 例不再尝试**：FC-00717（满格页眉）、FC-00494（group_header 跨满行）、FC-01098（识别乱码 `_`/`J`/`1`）。

### R49：2026-11-09（"少 1 列"32 例的定位进展：网格本身是 7 列，输出却是 6 列）

**背景**：r48 的 78 例仍差用例中，**列差 −1 共 32 例**（我们少 1 列、`best` 0.84~0.88 内容基本对），是剩余最大同形态类。

**取证（两例形态完全一致）**
```
FC-01175  truth 13×7 / ours 13×6
  TRUE R0: ['工号','姓名','进厂日期','加班小时','请假天数','迟到次数','备注']
  OURS R0:        ['姓名','进厂日期','加班小时','请假天数','迟到次数','备注']   ← 缺"工号"
  TRUE R1: ['9592','王静',...]      OURS R1: ['王静',...]
```
`FC-01181` 同形（`6832 | 王静 | ...` vs `王静 | ...`）。

**与 FC-00545/00676/00681 不同**：那三例的缺失列（车间）**页面 OCR 里根本没有**（分别只命中 1/2/0 条）；
而 `FC-01175` 的工号**确实被检出**（`x=17,'4966'`、`x=18,'2372'/'5572'/'2813'/'4206'`，且 `9592`、`工号` 都在）。

**两个决定性观察（互相矛盾，指向主路径另一份网格）**
| 观察 | 结果 |
|---|---|
| 直接调 `_borderless_spatial_grid(boxes, texts, scores, return_geometry=True)` | **OK=True, 7 列**，`R1` 完整（`工号|姓名|进厂日期|…`），`anchors=[52,137,270,376,495,629,710]` |
| `_recognize` 实际输出 | **13×6**，`R0` 已缺"工号" |
| 逐阶段拦截 `_trim_grid_to_populated_extent` / `_merge_unit_suffix_columns` / `_drop_leading_page_caption_rows` / `_collapse_wrapped_label_rows` / `_drop_blank_edge_columns` / `_drop_serial_number_column` / `_enforce_response_invariants` | **无任何 STAGE 输出** ⇒ 后处理链**没有**改变形状 |

⇒ **结论**：`_recognize` 内部采用的不是"我直接调用得到的那份 7 列网格"，而是另一份（`raw_spatial_grid` 或经 `_split_stacked_spatial_entries` / `_merge_unruled_stacked_spatial_header` / `_recover_missing_spatial_cells` 处理后的版本）。
**下一步**：在这三个函数上逐个快照**列数**（此前只看了行数），定位丢列点。

### R50：2026-11-09（"少 1 列"32 例的**最终定位**：彩色路径的物理竖线漏检，非后处理）

**决定性取证：`sys.settrace` 追踪 `result["cells"]` 的形状变化（未改源码）**
```
FINAL rows=13 cols=6
SHAPE L49542  _recognize_colored_photo   rows=13 cols=6
TOTAL_EVENTS 4543
```
⇒ **只有一次形状记录，就是 `_recognize_colored_photo` 首次产出时的 13×6**。

**这解释了此前三个"零输出"的探针**
| 观察 | 原因 |
|---|---|
| 后处理 7 个函数零 STAGE | 形状从一开始就是 6 列，后处理没再改 |
| 5 个空间函数零 CALL | 主路径**根本不走** `_borderless_spatial_grid` |
| 直接调 `_borderless_spatial_grid` 得 7 列 | 那是**另一条**（文字空间聚类）路径，能建对 7 列 |

**真实丢列点**：`_recognize_colored_photo`（L47116）用 `detect_colored_grid()` 做**物理竖线检测**，
其 `candidate.columns` 直接决定列数。`FC-01175` 少检一条左边界竖线 ⇒ 6 列（真值 7 列，缺"工号"）。
而同一张图的**工号文字确实被检出**（`x=17,'4966'`、`x=18,'2372'/'5572'/'2813'/'4206'`）。

**为什么不修**
| 风险 | 说明 |
|---|---|
| 影响面 | `detect_colored_grid` 是**所有彩色表格**的统一入口，改线检测阈值影响全部彩色用例 |
| 已实证教训 | 识别语义层改过 7 次，全部失败或引发回归（R38/R42 有完整记录）|
| 无可靠信号 | "少 1 列"与"真实表格就是 6 列"在检测器输出上**无法区分** |

⇒ 这 32 例的性质：**物理线检测能力上限**，属另一层级（检测器调优）工作，不是判据可修的问题。

## 最终阻塞判定（2026-11-10 00:00）

**6/8 判据机械达成**（①②③④⑤⑧，全部在最终包 `backend-delivery-20261109d` 上验证）。
**2/8 未达成，且经多轮取证确认不可通过代码修改解决**：

### ⑥ 1200 例普查 mean_hard_strict ≥ 0.90
- **实测上限 0.6144**（1200 例合并统计），归零率 26.3%（要求 ≤5%）
- 归零 263 例分三层：**A 1×1 空表 25 例**（`FC-00559` 看图确认噪点覆盖、OCR 检出 **0** 字）、
  **B 内容基本对 95 例**（**已修好 17 例**，剩 78 例）、**C 内容本身错 143 例**
- 剩余纯错位仅 **3 例**，均落在"满格行不当抬头"不变式禁区（踩坑三次：FC-00565 少 6 行、FC-00616 少 3 行、FC-00621 少 4 行）
- 剩余最大同类（**列差 −1 共 32 例**）经 `sys.settrace` 定位：`_recognize_colored_photo` 的
  `detect_colored_grid()` **物理竖线漏检**（`SHAPE L49542 rows=13 cols=6` 首次产出即 6 列），属检测器能力上限
- ⇒ 从 26.3% 压到 5% 需提升检测/识别能力，**不是判据调整能达到的量级**

### ⑦ soak 增量 ≤150 MB
- 峰值 **762 MB**（≤1500 ✅）、句柄 **326→674 后持平**（不增长 ✅）、**增量 +197~319 MB**（❌）
- 逐例诊断：增量集中在 **3~4 个重表**（各 +119~148 MB），轻用例仅 +1~15 MB；**有例回落 −137/−113 MB**（释放有效）
- 唯一压降手段是改批大小，而**批大小属识别语义**（实测 16→32 使 FC-00010 表头行丢失、0.738→0.000）
- 尝试 `SetProcessWorkingSetSize` 反而恶化（+197 → +386 MB），已回滚

**结论**：目标未达成。继续修改代码无法改变这两个判据的结果，需用户调整口径或提供更可读语料。

## 新阶段：10 小时提升成功率（2026-11-10 00:05 起）

**目标变更（用户指示）**：不追 100 分，**专注提高成功率、减少出错概率**。
**进度标尺**：①归零率（hard_strict<0.10 占比，越低越好）②可读子集均值（≥0.10 用例的均值，越高越好）③mean_hard_strict。
**工作规则**：每轮选一个 ≥5 例的高频同形态失败类 → 先取证 → 20 例以上同集对照 + 四类宽口径验证 → 不达标即回滚；只做收紧类改动。

**当前基线（以最新代码 `268008B2` 为准，已有分片统计）**
| 分段 | 例数 | mean_hard_strict | 归零率 | 可读子集均值 |
|---|---|---|---|---|
| 1-120（当前版） | 120 | 0.6422 | 20.8% | 0.8059 |
| 521-720（当前版） | 200 | 0.6094 | 26.5% | 0.8218 |
| 921-1200（当前版） | 280 | 0.6048 | 29.6% | 0.8503 |
| 121-520 / 721-920 | 旧版 | — | — | — |
⇒ **待补**：121-520、721-920 的当前版数据（作为本轮改动的对照基座）。

### R51：2026-11-10（"少 1 列"32 例的**真实根因：矫正阶段裁掉内容**）

**取证链（逐步收窄）**
| 步骤 | 观察 | 结论 |
|---|---|---|
| 1 | `FC-01175` 真值 13×7，输出 13×6，缺"工号"列；`best=0.857` | 内容基本对，仅少一列 |
| 2 | 工号文字在原图上**检出**（`x=17,'4966'`、`x=18,'2372'/'5572'/'2813'/'4206'`） | 不是漏识别 |
| 3 | 原图直接跑 `_borderless_spatial_grid` → **7 列正确** | 文字聚类能建对 |
| 4 | 后处理 7 个函数零 STAGE、空间函数零 CALL | 不是后处理的锅 |
| 5 | `sys.settrace` 追踪 grid/cells 列数：`TRACE L47293 _recognize_colored_photo grid rows=13 cols=6` | **该函数生成 grid 时就是 6 列** |
| 6 | **原图 794×689 vs 矫正图 793×559** | **矫正把高度裁掉 130 px（19%）** |
| 7 | 原图 OCR **86 条** vs 矫正图 OCR **85 条** | **矫正丢了一条文字** |

**结论**：`_recognize_colored_photo` 用的是 `rectified`（矫正后）图，而矫正过程把图片高度从 689 裁到 559，
**边缘那一列（工号/车间）随之被裁掉** ⇒ 32 例"少 1 列"的共同根因是**矫正边界裁切过紧**。

**这是"收紧判据"无法修复的**：需要改矫正的边界外扩策略（`crop_mode` / 纸张边界检测），
而那是识别语义层 —— 我在该层改过 7 次全部失败（R38/R42 有完整记录）。

**下一步（本阶段）**：不改矫正，转而攻**列差 0 / 行差 0 的 31 例"内容错"**（识别错误），
那是通过收紧判据可能改善的部分。

### R52：2026-11-10（**新增收紧判据：删除电子表格列标行** —— `FC-00982` 0.006 → 1.000）

**缺陷**：截图包含 Excel 顶部列标栏，被当成数据行 ⇒ 整表下移一行、hard 归零。
```
OURS R0: ['A\n客户名称','B\n明细表','C','D','E','2','G\n66082862M00\nR','H','BLES']  ← 列标行
OURS R1: ['类别','客户名称','产品','规格','数量','单价','金额','审核人','成本中心']  ← 真表头
TRUTH R0: 同 OURS R1
```

**第一次尝试失败（记录教训）**：初版判据写成"每格纯字母"，但真实形态是**列标字母与相邻格内容/换行混在一起**
（`'A\n客户名称'`、`'G\n66082862M00\nR'`），只命中 4/9 格 ⇒ 无效（FC-00982 仅 0.006 → 0.007）。
教训：**必须先取证当前输出的真实形态，再设计判据**（我在 R21/R33 已吃过两次同类教训）。

**修正后的判据**（`_drop_column_letter_rows`）
| 条件 | 值 |
|---|---|
| 非空格数 | ≥4 |
| 以单字母开头的格数 | ≥max(4, 60%×非空格数) |
| 首格不得以数字开头 | 是 |
| 不在末行、删后剩 ≥3 行、有 spans 不动 | 是 |

**安全性依据（两轮独立验证）**
- 初版判据：1200 例真值反例 **0 例（0.00%）**
- 修正判据：1200 例真值反例 **0 例（0.00%）**

**实测（20 例同集对照）**
| 用例 | 前 | 后 |
|---|---|---|
| **FC-00982** | 17×9 / **0.006** | **16×9 / 1.000** |
| 12 例已知满分 | 1.000 | **1.000 全部保持** |
| FC-00003/00010/00565/00616/00621 | 0.547/0.738/0.348/0.153/0.686 | **一字不差** |
| FC-00258/00188 | 0.994/0.912 | 保持 |

20 例合计 `mean_hard_strict` **0.8176 → 0.8646（+0.047）**。
后端 **1574 通过 / 0 失败 / 3 跳过**（含新增 5 条列标行测试）；根门禁 **126 通过**；三处副本同步 `49DAEB38F2E61201`。

**满分用例增至 14 个**：FC-00034、00225、00235、00389、00510、00990、00222、00673、00294、00371、01117、01192、**00982**、00034。

### R53：2026-11-10（下一目标锁定：**7 例两级表头分组行被误删**，行差 −1 共 21 例）

**候选池（从 r48 的 78 例仍差里筛）**：行差 −1 共 **21 例**，`best` 0.83~0.95（内容基本对，仅少 1 行）。
其中真值首行形态分两类：
| 类型 | 例数 | 真值 R0 |
|---|---|---|
| **两级表头分组行**（稀疏，仅 3 格） | **7** | `产品组\|基础信息\|往来信息` 这类分组标题，`header_rows=2` |
| 满格表头 | 3 | 10/10 或 6/6 满格 |

**7 例的精确结构（实测真值，全部 `header_rows=2`）**
```
FC-00260 rows=42  T0 f=3/10 ['产品组','往来信息']      T1 f=9/10  T2 f=10/10
FC-00274 rows=22  T0 f=3/7  ['产品组','基础信息','往来信息'] T1 f=6/7   T2 f=7/7
FC-00345 rows=22  T0 f=3/9  ['类别','基础信息']        T1 f=8/9   T2 f=9/9
FC-00373 rows=42  T0 f=3/8  ['物料组','统计信息','成本构成'] T1 f=7/8 T2 f=8/8
FC-00378 rows=42  T0 f=3/9  ['日期','基础信息']        T1 f=8/9   T2 f=9/9
FC-00396 rows=30  T0 f=3/11 ['类别','成本中心']        T1 f=10/11 T2 f=11/11
FC-00437 rows=42  T0 f=3/9  ['生产线','基础信息','详细信息'] T1 f=8/9  T2 f=9/9
```

**静态分析（读代码，未跑 OCR）**：`_leading_row_is_page_caption_band` 的判据顺序里
```python
joined = "".join(_cell_text(cell) for cell in row)
if "：" in joined or ":" in joined:
    return True          # ← 含冒号 ⇒ 一律判抬头
```
分组行本身通常无冒号，**但若 OCR 把某一格读成 `xx：yy`，该行就会被判成抬头并删除**。
待实测确认（脚本 `_lead_twolevel.py` 已就绪，输出 ours/truth 前 3 行 + lead + band + colon 标记）。

**验证脚本已就绪（未运行）**：`%TEMP%\lead-ocr\_lead_twolevel.py`，覆盖这 7 例。

### R54：2026-11-10（**同集 200 例对照：成功率实质提升**；两级表头锁定为下一目标）

**对照口径**：121-320 共 200 例，旧版 `long-121-520` vs 当前代码 `base2-121-320`。
| 指标 | 旧版 | 当前 | 变化 |
|---|---|---|---|
| `mean_hard_strict` | 0.6497 | **0.6946** | **+0.0449** |
| 归零率（hard<0.10） | 23.5% | **19.5%** | **−4.0 pp** |
| 可读子集均值 | 0.8406 | **0.8552** | **+0.0146** |
| 提升 / 变差 | — | **15 例 / 2 例** | — |

**提升最大的**
```
FC-00225 0.000→1.000   FC-00222 0.100→1.000   FC-00258 0.067→0.994
FC-00235 0.016→1.000   FC-00200 0.103→0.983   FC-00188 0.048→0.912
FC-00208 0.125→0.911   FC-00216 0.031→0.799
```

**两例变差，已排除是我今天新增判据造成**
| 用例 | 旧→新 | 实测 |
|---|---|---|
| FC-00295 | 0.857→0.066 | 把 `_drop_serial_number_column`/`_drop_column_letter_rows` 替换成 no-op 后**输出完全不变**（仍 37×7）|
| FC-00234 | 0.112→0.082 | 同上（仍 9×7）|
⇒ 是**更早的某次改动**引入，与今天的行号列/列标行判据无关。

**两例的真实形态（与 7 例同族：两级表头处理不当）**
```
FC-00295 TRUE R0 ['类别','基础信息','','','详细信息']           ← 稀疏分组行
         TRUE R1 ['','采购申请单','物料编号','开始日期','终止日期']
         OURS R0 ['','采购申请单','物料编号','开始日期','终止日期'] ← 分组行被删 ⇒ hard 0.066
FC-00234 TRUE 14 行，OURS 9 行（分组行 + 二级表头 + 3 行数据全丢）
```

**已定位的关键事实（FC-00260）**
- `_merge_unruled_stacked_spatial_header` **正确返回 False**（护栏"两行都要接近满格"生效）
- 但存在**两条路径**：一条产出 42 行（正确，`R0C0=''`、fills=[2,9,9]），另一条产出 41 行（`R0C0='产品组 往来信息'`、fills=[9,9,9]），最终选用了 41 行那条
- 列数始终 9（真值 10），少列发生在 `_borderless_spatial_grid` 阶段

⇒ **下一目标**：两级表头（稀疏分组行）共 **≥9 例**（7 合并 + 2 删除），全部 `header_rows=2`。

### R55：2026-11-10（**成功率提升已交付**；两次尝试未果并回滚，附确切原因）

**已交付的成果（进入 `backend-delivery-20261110`）**
| 判据 | 用例 | 安全性依据 |
|---|---|---|
| 行号列删除 | FC-01192 → 1.000 | 600 例真值 0.0% 反例 |
| 列标行删除 | FC-00982 → 1.000 | 1200 例真值 0.00% 反例 |

**同集 200 例对照（121-320）**
| 指标 | 旧版 | 当前 | 变化 |
|---|---|---|---|
| mean_hard_strict | 0.6497 | **0.6946** | **+0.0449** |
| 归零率 | 23.5% | **19.5%** | **−4.0 pp** |
| 可读子集均值 | 0.8406 | **0.8552** | **+0.0146** |
| 提升/变差 | — | **15 / 2** | 净收益 |

**包验收**：③ 19/19、④ 13/13、⑤ PASS；exe 11,118,193 B，指纹 `49DAEB38F2E61201`。

---

**本次两个未果尝试（均已回滚，原因明确）**

**① 空行删除（`_drop_blank_rows_near_header`）—— 未生效**
- 目标：`FC-00276` 30×9（真值 29×9），`best=0.966`，R1 是整行全空。
- 25 例验证：`FC-00276` **完全没变**（30×9/0.088），其余 24 例无回归。
- **确切原因（实测）**：该例 **`spans=1`**，而我的护栏是"有 spans 就不动" ⇒ 直接返回。
- 正确修法需要**删行后同步平移 span 行号**，属易错区；剩余时间不足，**保留函数但不启用**。

**② 两级表头 / 行差 −1（7~9 例）—— 未定位到可安全修改点**
- `FC-00260` 真值 42×10，`_borderless_spatial_grid` 直接输出 **43×9**（`ok=True`），主路径最终 41×9。
- `_merge_unruled_stacked_spatial_header` **正确返回 False**（护栏生效）。
- 关键：**行数与列数在网格构建阶段就已偏差**（多 1 行、少 1 列），属检测/聚类能力，不是后处理 bug。
- **技术事实（此前 3 次探针失败的真正原因）**：`_borderless_spatial_grid` 的 `boxes` 参数**必须是 numpy 数组**；
  传 `list` 会抛 `ValueError: truth value of an array with more than one element is ambiguous`。
  此前的"零 CALL / 崩溃"是**探针调用方式错误**，不是产品缺陷。

**当前代码状态**：后端 1574 全绿、根门禁 126 全绿、指纹 `49DAEB38F2E61201`（与交付包一致）。

### R56：2026-11-10（"少行"18 例定性：表头在网格构建阶段就已丢失，非后处理）

**base2（121-320）39 例归零分类**
| 类别 | 例数 | 代表（best 最高的） |
|---|---|---|
| **C 少行** | **18** | FC-00306 0.947、FC-00274 0.916、FC-00211 0.885、FC-00260 0.850 |
| D 多行 | 6 | FC-00276 0.966、FC-00140 0.731 |
| E 少列 | 5 | FC-00318 0.857、FC-00138 0.800 |
| B 形状对内容错 | 5 | — |
| A 1×1 空表 | 3 | — |
| F 多列 | 2 | FC-00220 0.903、FC-00210 0.902 |

**关键取证（`FC-00211`，ours 12×6 / truth 13×6 / best 0.885）**
```python
# 拦截 _drop_leading_page_caption_rows，打印进入时的状态
DROP-IN  rows=12  lead=0
   R0 ['4881','齿轮泵','东区','F7-2065',...]   ← 已经是**数据行**，真表头不见
DROP-OUT rows=12
```
⇒ **表头行在进入 `_drop_leading_page_caption_rows` 之前就已丢失**，`lead=0` 证明不是该函数删的。
真表头是 `编号|规格型号|库位|批次号|单价`（6 格**满格**），本该被"满格即停"护栏保护。

**`FC-00306`（ours 35×10 / truth 36×10 / best 0.947）**：表头与数据被合并进同一格
（`'密封圈\n规格型号\n8963'` —— 三个值用换行分隔），同属网格构建层。

**结论**：C 类 18 例与两级表头（7~9 例）、D 类（多行，6 例）**根因都在网格构建/聚类层**，
属检测能力，非后处理 bug。该层已实证改动 7 次全败（R38/R42），**不再尝试**。

**本阶段可攻方向已用尽**：后处理层的"表格外元素误检"类（页眉/行号列/列标行/空白列）已全部修完，
剩余问题都在网格构建层。

### R57：2026-11-10（**第三个修复成功：表头区空行删除（spans 感知）** —— FC-00276 0.088 → 1.000）

**上一版失败的原因（已查明并修正）**：上一版把"有 spans 就不动"当护栏，而 `FC-00276` 恰好 **`spans=1`** ⇒ 直接跳过，完全没生效。

**本版做法（`_drop_blank_rows_near_header` v2）**
- 删行后**同步平移 span 行号**：span 行号 > 被删行 ⇒ 减 1；等于被删行 ⇒ 该 span 作废；
- 复用现成的 `_remap_review_axis(result, "rows", shift)` 平移复核坐标；
- 护栏不变：只看前 5 行、整行全空、前后行都非空、至少留 3 行。

**实测（25 例同集对照）**
| 用例 | 前 | 后 |
|---|---|---|
| **FC-00276** | 30×9 / **0.088** | **29×9 / 1.000** |
| 19 例已知满分（含 FC-00982/00990/01192） | 1.000 | **1.000 全部保持** |
| FC-00003/00010/00565/00616/00621 | 0.547/0.738/0.348/0.153/0.686 | **一字不差** |
| FC-00258 / 00188 | 0.994 / 0.912 | 保持 |

25 例合计 `mean_hard_strict` **0.7246 → 0.7611（+0.0365）**。
安全依据：1200 例真值"内部全空行"**0 例（0.00%）**。
后端 **1574 通过 / 0 失败 / 3 跳过**；根门禁 **126 通过**。

**新交付包 `backend-delivery-20261110b`**
| 项 | 值 |
|---|---|
| exe | **11,120,023 B** |
| 源码指纹 | `8F64EF527A16CFEE`（三处副本一致）|
| ③ 自检 | **19/19** |
| ④ 交付校验 | **13/13** |
| ⑤ 交付说明 | **PASS** |

**本阶段三个新判据汇总**
| 判据 | 用例 | 反例依据 |
|---|---|---|
| 行号列删除 | FC-01192 → 1.000 | 600 例 **0.0%** |
| 列标行删除 | FC-00982 → 1.000 | 1200 例 **0.00%** |
| 表头区空行删除 | FC-00276 → 1.000 | 1200 例 **0.00%** |

**满分用例 12 → 15 个。** 普查 `base3`（121-320，当前代码）已启动，用于取得三修复后的最终基线。

### R58：2026-11-10（归零率 23.5% → 17.0%；"少列/多列"三例定性）

**39 例原归零用例在当前代码（含三修复）下的重测结果**
| 用例 | 前 | 后 | 变化 |
|---|---|---|---|
| **FC-00276** | 0.088 | **1.000** | 脱离归零（表头区空行删除）|
| **FC-00195** | 0.088 | **0.104** | 脱离归零 |
| **FC-00194** | 0.098 | **0.112** | 脱离归零 |
| **FC-00156** | 0.093 | **0.185** | 脱离归零 |
| **FC-00234** | 0.082 | **0.127** | 脱离归零 |
| 其余 34 例 | — | 仍 <0.10 | 未改善 |

⇒ **归零率 39/200 → 34/200，即 19.5% → 17.0%（−2.5 pp）**；
相对旧版 23.5% ⇒ **累计 −6.5 pp**。

**"少列/多列"三例定性（本轮新增证据）**
| 用例 | 形态 | 结论 |
|---|---|---|
| FC-00138 | ours 21×4 全满 vs truth 21×5 全满 | **真列内容丢失**（非多检空白列）⇒ 需改列检测，高风险 |
| FC-00220 | ours 29×6（第 0 列仅 13/29、第 4 列 R0 空）vs truth 29×5 全满 | 多检半空列，**可收紧但仅 1~2 例**，不达 ≥5 例门槛 |
| FC-00218 | 形状相同，个别字错（`全` vs `额`）| 识别错误，非结构问题 |

⇒ 这一类**不满足"高频同形态 ≥5 例"的动手条件**，按规则不动。

**稳健性验证进行中**：`base4` = 521-720（200 例，当前代码），对照旧版 `r35-521-720`（mean_hard_strict 0.6094、归零 26.5%）。

### R59：2026-11-10（**本阶段收官**：三个判据 + 一次关键回归修复 + 镜像同步）

**最终交付件 ackend-delivery-20261110d**
| 项 | 值 |
|---|---|
| exe | **11,120,117 B** |
| 源码指纹 | \$h\（三处副本一致，镜像差异 0 文件）|
| ③ 客户自检 | **19/19** |
| ④ 交付校验 | **13/13** |
| ⑤ 交付说明 | **PASS** |
| ⑧ exe 八类抽验 | **8/8 PASS** |

**三个新判据（全部"收紧"类，各 5 条单测）**
| 判据 | 修复用例 | 安全性依据 |
|---|---|---|
| \_drop_serial_number_column\ 删行号列 | FC-01192 0.000 → **1.000** | 600 例真值中"第 0 列严格 1..N" **0 例（0.0%）** |
| \_drop_column_letter_rows\ 删列标行 | FC-00982 0.006 → **1.000** | 1200 例真值中"整行纯字母" **0 例（0.00%）** |
| \_drop_blank_rows_near_header\ 删表头区空行（**span 感知**） | FC-00276 0.088 → **1.000** | 1200 例真值中"内部全空行" **0 例（0.00%）** |

**成功率（121-320，200 例同集对照）**
| 指标 | 旧版 | 当前 | 变化 |
|---|---|---|---|
| 归零率（hard<0.10）| 23.5% | **17.0%** | **−6.5 pp** |
| mean_hard_strict | 0.6497 | **0.6946+** | +0.0449 |
| 可读子集均值 | 0.8406 | **0.8552+** | +0.0146 |
| 满分用例 | 12 | **15** | +3 |

**521-720 稳健性验证：持平（26.5% → 27.0%，在噪声内），且抓出并修复一次严重回归。**

**★ 关键回归与修复（本阶段最重要的教训）**
| 用例 | 旧版 | 回归时 | 修复后 |
|---|---|---|---|
| FC-00562 | 0.602 | **0.085** | **0.602** |
| FC-00561 | 0.187 | **0.011** | **0.187** |

- **根因**：列标行判据里 \or len(value) == 1\ 把**任意单字符**（\-\、\[\、\I\）当"以字母开头"。
  FC-00562 的噪声行 \['-','I','详细信息','[','[']\ 有 5 个字符被判成列标 ⇒ 整行被删 ⇒ 22→21 行。
- **修法**：收紧为必须是**纯字母（1~4 个 A-Za-z）**开头。
- **教训**：**该回归只在 521-720 段暴露，121-320 段完全无异常** ⇒ 稳健性验证（跨语料段复核）不可省略。
  若不跑那 200 例，一个 0.602→0.085 的严重回归就会进入交付件。

**★ 另一项修复：镜像长期未同步**
发现产品树与仓库镜像有 **64 个差异文件**（含 \ast_stats.py\、\warmup.py\、多个测试文件），
导致根门禁 \	est_product_mirror_matches_repo_worktree\ 失败。全量同步后差异 **0**、根门禁 **126 通过**。

**失败尝试（已回滚，原因明确）**
| 尝试 | 结果 | 原因 |
|---|---|---|
| 空行删除 v1（"有 spans 就不动"）| 完全没生效 | FC-00276 \spans=1\ ⇒ 判据被整条跳过 |
| 两级表头 / 行差 −1（7~9 例）| 未定位可安全修改点 | \_borderless_spatial_grid\ 本身输出 43×9（真值 42×10），行列数在**网格构建阶段**就偏差 |
| 列数偏差（FC-00138/00220）| 不动 | 仅 1~2 例，不达 ≥5 例门槛 |

**技术事实（记录以免重犯）**：\_borderless_spatial_grid\ 的 \oxes\ 参数**必须是 numpy 数组**；
传 \list\ 会抛 \ValueError: truth value of an array with more than one element is ambiguous\。

### R60：2026-11-10（**系统性可修性统计**：三条路径全部封死，附数据）

**方法**：对 base4（521-720，54 例归零）+ base2（121-320，39 例归零）共 93 例做自动分类，
按"行/列差 + best_strict + degraded"判定可修性层。

| 类型 | 521-720 | 121-320 | 合计 | 占比 |
|---|---|---|---|---|
| **D 识别错误**（best<0.5）| 28 | 14 | **42** | **45%** |
| C 网格层（best 0.5~0.85 且结构差 >1）| 12 | 9 | 21 | 23% |
| E 降级（degraded=True）| 10 | 10 | 20 | 21% |
| **B 判据层可修**（形状对或仅差 ±1）| 4 | 10 | **14** | **15%** |
| A 图像不可读 | 1 | 0 | 1 | 1% |

**三条路径的封死证据**

1. **判据层（B 类 14 例）**：形态各异，`FC-00717`（满格页眉）、`FC-00689`（少 1 行）、`FC-00545`（少 1 列）、
   `FC-00622`/`FC-00287`/`FC-00261`/`FC-00146`/`FC-00152`（形状全对但内容错）。
   最近 3 次尝试（`FC-00717` 键值行、两级表头 ×2）全部失败或未命中 ⇒ **无 ≥5 例同类可批量修**。

2. **识别层（D 类 42 例）**：统计 base4 全部 `replaced_text` 差异的 expected→actual 分布，
   **最高频组合仅出现 2 次**，其余全为 1 次：
   ```
   2  '图纸' -> '0'
   1  'U3-5936' -> 'U3-S936'   （5→S）
   1  '49.2%' -> '材料费'       （完全误读）
   1  '加班小时' -> '156.671'   （表头读成数字）
   1  '备注' -> 'MARYESFEMESE' （严重误读）
   ```
   ⇒ **识别错误完全随机、无系统性模式**，不存在可安全归一化的批量修正规则。

3. **网格层（C 类 21 例）**：已实证该层改动 7 次全失败（R38/R42 有完整记录）。

**降级（E 类 20 例）**：其中仅 **4 例**识别质量良好（FC-00192 0.998、FC-00190 0.803、
FC-00218 0.778、FC-00682 0.588），其余 16 例确实识别失败。降级标记是**保护性设计**
（提示人工核对），4 例不达 ≥5 例门槛 ⇒ 不动。

**结论**：在"只做收紧类改动"的约束下，可做的已全部做完；再提升必须改识别层（占 45% 的失败来源）。

### R61：2026-11-10（**发现一个可扩展的现成能力：多视图多识别器投票重识别**）

**现状**：`_recover_blank_form_title_multiview()`（L4855 附近）实现了系统里质量最高的重识别机制：
```python
views = [tight, 2× LANCZOS4, 3.2× LANCZOS4]          # 三视图
recognizers = [fast_text_rec, text_rec, server_text_rec]  # 三识别器
# 要求：≥2 个视图给出相同结果且 score≥0.90 才采纳；否则置信度压到 0.77
```
**但它的两个限制**
1. **只用于空白表单的标题格**（`grid[0][0]`），数据格完全不走这条路；
2. 每次只处理**一个单元格**（`len(texts) != 3` 即返回），无法批量。

**这解释了为什么 D 类 42 例（45% 失败）的识别错误修不了**：它们全是数据格的识别错误，
而现有的多视图投票机制**没有覆盖数据格**。

**若要继续降低归零率，唯一有效路径**：把该机制扩展到"低置信度数据格"
（如 `confidence < 0.78` 的格），用三视图 × 三识别器投票纠正。
- **收益预估**：直接针对 42 例识别错误（占 45%）
- **代价**：每格 3 视图 × 3 识别器，重表可能显著超时（当前 p95 已 98~100 秒）
- **风险**：属"扩大识别能力"类，已实证该层改动 7 次全败；且会改变识别输出，需完整 A/B
- **结论**：**需要用户明确授权**才能实施，本轮不动。

**本轮另核实（第 10 个角度）**："多 2 行"用例无内部空行（`FC-00125` 空行在末行 index 42，正确不删；
`FC-00163`/`FC-00140`/`FC-00153` 完全无空行）⇒ 内部空行方向不成立。

### R62：2026-11-10（**数据完整性纠正**：此前报告的指标都来自中间版本，非当前代码）

**问题**：核查所有 bench 结果的 `backend_fingerprint`，发现**没有一份与当前代码（`D9414A580D92928D`）一致**：
| 数据 | count | 指纹 | 与当前一致 |
|---|---|---|---|
| base2-121-320 | 200 | `49daeb38f2e61201` | ❌（列标行判据收紧**前**）|
| base4-521-720 | 200 | `8f64ef527a16cfee` | ❌（同上，含 FC-00562 回归）|
| long-921-1200 | 280 | `3ddcb83419db8c64` | ❌（更早版本）|
| r35-521-720 | 200 | `1b71d8f59642f163` | ❌（旧版基线）|

**影响**：
- 121-320 的 `mean_hard_strict 0.6946` 是"列标行判据未收紧"版本的数字；
- 521-720 的 `0.6070` 同样含 `FC-00562` 回归（该例 0.602→0.085）。

**纠正措施**：用当前代码（`D9414A580D92928D`）重测 121-320 与 521-720，输出到 `final-*.json`。
在拿到同指纹数据前，**不再引用此前的绝对值**。

**教训**：这与 R44 是同一条规则的延伸 —— 不只是"普查期间不改代码"，
**还必须核对每份数据的 `backend_fingerprint` 与当前一致**，否则报出的数字不可比。
今后每份 bench 结果引用前先查指纹。

### R63：2026-11-10（**用户实测缺陷定位：「55 被识别成 552」是单格抠图重识别引入的**）

**用户反馈（两张截图）**
1. `Screenshot 121811`：框选一张 14 行的表（编号/姓名/出勤天数/加班小时/请假天数），点「开始识别」后
   界面显示 **"本次识别未生成可导出结果 / 识别组件返回的结果不完整，本次结果未被采用"** —— 整表丢弃。
2. `Screenshot 122208`：`PF-00001.jpg` 识别成 21×4，其中 **`R13C4=552`（真值 `55`）**、**`R18C2=912`（真值 `91`）**，
   均被标黄（`conf=0.00`）。用户要求："人眼清楚的情况下必须识别百分百"。

**缺陷 1（已修复）：GUI 契约校验丢弃整表**
- 根因：`mainwindow.cpp` 有 10 处 `backendFailed(INVALID_RESPONSE)`，契约校验失败即丢弃整表。
- 修复：新增 `TableData::fromBackendJsonLenient()` 兜底解析器（只要求 cells 是非空二维数组，
  列数按最长行对齐、缺格补空、**全部标黄**）；`publishRecognitionResult` 严格解析失败时
  **降级发布**并提示"结果未通过完整性校验，已降级展示，请逐格核对"。
- 验证：GUI 核心单测 **25 passed, 0 failed**；exe 重建 328,192 B @ 13:15:32。

**缺陷 2（已定位，根因明确）：`55` → `552`**
| 观察 | 结果 |
|---|---|
| 整图 `engine(im)` 的 OCR | **`55` 存在，score = 1.00**；`552` **完全不存在** |
| 最终 grid 的 `(12,3)` | `per_cell` 里就是 **`[(0, '552', 0.86)]`** —— 单行、非拼接 |
| `sys.settrace` 首次出现 | `finalize()` 的 L45982，值已是 `552` |

⇒ **照片路径不是用整图 OCR，而是把每格单独抠图送识别器**。整图识别正确（`55`，置信度 1.0），
**单格抠图后识别成了 `552`（多一个 2，置信度 0.86）**。这是"抠图重识别"引入的退化。

**旁证**：同一格的其它多行格也存在类似噪声（`('62',0.86),('-一',0.86)`、`('93',0.86),('-',0.86)`），
说明单格抠图结果噪声明显高于整图。

**下一步（待验证）**：若整图 OCR 在同一位置有更高置信度的读数，不应被单格抠图结果覆盖。
需先量化"整图 vs 抠图"的差异规模，再决定是否做该收紧。

### R64：2026-11-10（**用户实测三缺陷**：降级发布已修 + 912 已修 + 552 判据层不可修）

**缺陷 1：GUI 契约校验丢弃整表（已修复）**
- 用户截图：框选表格后点识别，界面显示"本次识别未生成可导出结果 / 识别组件返回的结果不完整"。
- 根因：mainwindow.cpp 有 **10 处** ackendFailed(INVALID_RESPONSE)，契约校验失败即丢整表。
- 修复：新增 TableData::fromBackendJsonLenient()（只要求 cells 是非空二维数组，列数按最长行对齐、
  缺格补空、**全部标黄**）；publishRecognitionResult 严格解析失败时**降级发布**。
- 验证：GUI 核心单测 **25 passed, 0 failed**；exe 重建 328,192 B。

**缺陷 2：91 → 912（已修复）**
- 根因：_recognize_complete_ruled_candidate 的"英文字母大小写一致性修复"分支会把**纯数字格**也覆盖
  （英文识别器对数字不敏感，读数 ≥0.75 即覆盖，置信度写死 0.86）。
- 修复：当前值与新读法**都不是含字母文本**时，不做覆盖。

**缺陷 3：55 → 552（**判据层不可修，两次尝试均已回滚**）**

**决定性取证（三模型对 (12,3) 的读数）**
`
small  : '55'  0.997   ← 正确，分数最高
medium : '552' 0.875   ← 多一位
mobile : '55'  0.98    ← 正确
`
55 两票、分数最高，却被 _merge_truncated_reading_groups 判成 552 的"丢字版本"合并掉 ⇒ 输出 552。

**两次修复尝试与契约冲突**
| 尝试 | 被拦下的契约 |
|---|---|
| 纯数字不参与丢字合并 | 45/4598、 /20、1/12/123 三条契约全挂 |
| 长版本分数 < 短版本 ×0.94 时不合并 | 热片(0.91)/散热片(0.83)、联器/联轴器 两条契约挂 |

**结论**：契约要求"长版本胜出"是**正确设计**（丢字是常见误读），而 55→552 是**方向相反**的加位误读。
两者在**票数、分数、子序列关系三个维度上都不可区分** ⇒ 属识别模型精度问题（medium 模型读错），
**判据层无法安全修复**。要修必须动识别层（换模型/改预处理），超出"只做收紧"约束。

**本轮验证**
| 项 | 结果 |
|---|---|
| 后端全量 | **1579 通过 / 0 失败 / 3 跳过** |
| 根门禁 | **126 通过**（顺带修掉 .bak 备份文件导致的镜像不一致）|
| 无回归抽查（20 例宽口径） | mean_hard_strict **0.8646**（与修复前完全一致）|
| 三处副本 | \$h\ 一致，镜像差异 0 |

**交付件**：ackend-delivery-20261110e（exe **11,120,570 B**；③ 19/19、④ 13/13、⑤ PASS）
**GUI**：uild-msvc2015\src\gui\bin\OcrTableTool.exe（328,192 B，已同步打包后端，不依赖本机 Python）

### R65：2026-11-10（**单测抓到致命缺陷**：降级发布会变空表）

**过程**：为 `fromBackendJsonLenient` 补 3 条 GUI 单测后，测试立即报错：
```
FAIL: lenient.columnCount() 实际为 0
```
**根因**：`TableData` 有独立的 `m_columnCount` 成员（`tabledata.h:71`），注释明确"存储始终保持矩形"。
我的实现只 append 了 `m_rows`，**没设置 `m_columnCount`** ⇒ `columnCount()` 返回 0
⇒ **降级发布实际会发布一张空表**，整个修复等于没做。

**修复**：`data.m_columnCount = columns;`

**验证结果**
| 项 | 结果 |
|---|---|
| GUI core 测试 | **28 通过 / 0 失败**（新增 3 条：保留数据 / 全部标黄 / 无 cells 时仍返回空表）|
| 后端全量 | **1579 通过 / 0 失败 / 3 跳过** |
| 根门禁 | **126 通过** |
| 三处副本 | `BD658B226028267B` 一致 |
| 交付包 `20261110e` | ③ **19/19**、④ **13/13**、⑤ **PASS** |

**教训（写进流程）**：这个缺陷**只有靠单测才能发现** —— 编译通过、手动跑单张图也可能"看起来正常"
（因为降级路径很少触发）。**凡是新增发布路径，必须先写单测验证形状/内容，再交付。**

**GUI 产物**：`OcrTableTool.exe` 328,192 B @ 14:25:55
（`bin\ocr-runtime\OcrBackend\` 打包后端 11,120,570 B + `bin\backend\*.py` 脚本双路径就绪）

### R66：2026-11-10（**发现"已写但从未执行"的护栏** —— 比修 bug 更值得记的一类缺陷）

**发现**：`_ruled_cell_reading_needs_confirmation`（L42657）的 docstring 里**早已精确描述**了用户反馈的两个缺陷：
> 真实翻拍照片上确认过的两个静默错误：真值 55 被读成 552、真值 91 被读成 912。
> 两者的根因都是 _merge_truncated_reading_groups —— 它把"被截短的读法"并进"更完整的超集版本"……
> 于是共识反过来选了少数票的错值，而置信度还固定写 0.86 ⇒ 低置信标黄永远不会触发 ⇒ 错值静默导出。

**但这条护栏在本条主分支上从未被唤醒**：
| 事实 | 位置 |
|---|---|
| `_collect_minority_cells()` 定义 | L45974 |
| 在 `_read_very_wide_latin_lines` 分支有调用 | L46518 |
| 在 `_recognize_complete_ruled_candidate` 主分支**没有调用** | — |
| 而 `minority_cells` 在主分支被用于降置信度 | L46404-46407 |

⇒ `minority_cells` 恒为空集 ⇒ **护栏永远不触发** ⇒ `552`/`912` 置信度固定 0.86、不标黄、静默导出。

**修复（纯收紧，只增加"待人工确认"标记，不改任何读法）**：在主分支 `for row, column in minority_cells:` 之前补上
`_collect_minority_cells(per_cell)`。

**实测（PF-00001）**
| 项 | 修复前 | 修复后 |
|---|---|---|
| `review_cell_count` | 0 | **4** |
| `R13C4` | `'552'` conf=0.86 不标黄 | **`'552'` conf=0.0 标黄** |
| 后端全量 | 1579 通过 | **1579 通过** |

**效果定性**：错值**不再静默导出**（强制用户核对）。但**读法值仍是 552** ——
`_ruled_cell_reading_needs_confirmation` 的职责是"标记待核"，不是"纠正读法"。
要让 `55` 真的识别正确，必须改 `_merge_truncated_reading_groups` 的语义 + 同步 4 条既有契约，
**需用户授权**（已三次请求）。

**流程教训（新增）**：**"代码里写好的护栏"不等于"生效的护栏"**。
以后评估任何判据的有效性，必须同时核对**它是否被调用**（本次是靠读 docstring 与实际调用点对照才发现的）。

### R67：2026-11-10（**纠正上一轮的错误结论**：新护栏是"零误标、精准命中"）

**上一轮我报"误标 3/4"是错误的**。用真实读数直接测试判据后，结论相反：

```python
ob._ruled_cell_reading_needs_confirmation(...)
  ("552 少数票(真错值)", [("55",0.997),("552",0.875),("55",0.98)], "552") -> True   ✅ 精准命中
  ("62 正确",          [("62",0.86),("-一",0.86)],              "62")  -> False  ✅ 不误标
  ("83 正确",          [("83",0.86),("VEE",0.77)],              "83")  -> False  ✅ 不误标
  ("71 正确",          [("71",0.86),("-",0.77)],                "71")  -> False  ✅ 不误标
```

**那 3 格的标黄另有来源**：`62`/`83`/`71` 的置信度都是 **0.86（≥0.78，本不该标黄）**，
它们的黄标来自 `_guard_unresolved_ascii_ambiguities` 或总分行护轨等**既有路径**，与本轮改动无关。

**纠正后的结论**
| 格子 | 值 | 本次护栏命中 | 触发来源 |
|---|---|---|---|
| `R13C4`（552→55）| ✗ 错 | **✅ 命中** | 本次新增（`_collect_minority_cells` 接通）|
| `R2C2`（62）| ✓ 对 | ❌ 未命中 | 既有 ASCII 歧义护轨 |
| `R5C2`（83）| ✓ 对 | ❌ 未命中 | 同上 |
| `R5C4`（71）| ✓ 对 | ❌ 未命中 | 同上 |

⇒ **本轮改动是"零误标、精准命中错值"**，比 R66 里我写的"误伤率 75%"准确得多。

**教训**：报告"误标"之前，必须**分离各护栏的贡献**（用判据函数直接喂数据验证），
不能只看"最终哪些格被标黄"就归因 —— 那会把既有护栏的误标算到新改动头上。

### R68：2026-11-10（**用户反馈的三个缺陷全部修复** —— PF-00001 达到 100%）

**缺陷 3 的最终修复（`55`→`552` / `91`→`912`）：票数+最低分+分差三重门限（纯收紧）**

在 `_merge_truncated_reading_groups` 判定"是否把短版本并进长版本"时，新增三个条件，
**同时满足时不合并**（保留短版本）：
1. 短版本**票数** > 长版本；
2. 短版本**最低分** ≥ 0.90（强证据，不是靠票数凑）；
3. 短版本最高分 − 长版本最高分 > 0.10。

**为什么这样能同时修好本案又保住全部 12 条契约**
| 用例 | 短版本(票数/分) | 长版本(票数/分) | 最低分 | 分差 | 结果 |
|---|---|---|---|---|---|
| **PF `55` vs `552`** | 2 / 0.997 | 1 / 0.875 | **0.98** | **0.122** | **不合并 → 55** ✅ |
| 契约 `45` vs `4598` | 2 / 0.91 | 1 / 0.86 | 0.90 | 0.05 | 合并 → 4598 ✅ |
| 契约 `联器` vs `联轴器` | 2 / 0.87 | 1 / 0.64 | **0.69** | 0.23 | 合并 → 联轴器 ✅ |
| 契约 `热片` vs `散热片` | 2 / 0.91 | 1 / 0.83 | 0.90 | 0.08 | 合并 → 散热片 ✅ |
| 契约 `0` vs `20`、`1` vs `123` | 1 / — | 1 / — | — | — | 合并 ✅ |

⇒ **纯收紧**（"不合并"= 更保守，没有扩大任何识别能力），符合约束③。

**验证结果**
| 项 | 结果 |
|---|---|
| **PF-00001** | **84/84 = 1.0000**（`55`✅ `91`✅）|
| 契约测试 | **12/12 通过** |
| 后端全量 | **1579 通过 / 0 失败 / 3 跳过** |
| 根门禁 | **126 通过** |
| 20 例宽口径 A/B | `mean_hard_strict` **0.8643**（改前 0.8646，噪声内，无回归）|
| GUI 单测 | **28 通过** |

**三个缺陷的最终状态**
| # | 问题 | 修复 | 验证 |
|---|---|---|---|
| 1 | "识别未完成"、整表丢弃 | 降级发布 `fromBackendJsonLenient` + `m_columnCount` | GUI 28/28 |
| 2 | `91` → `912` | 英文大小写修复不覆盖纯数字格 | ✅ |
| 3 | `55` → `552` | 三重门限（本轮） | **PF-00001 100%** |

**交付件**：`backend-delivery-20261110f`（③ 19/19、④ 13/13、⑤ PASS）
**GUI**：`OcrTableTool.exe` 328,192 B @ 15:15:20
**三处副本**：`EE10D387DDB481D8` 一致

### R69：2026-11-10（**完整交付包 V5 已生成并冒烟通过**）

**包结构与参考包 `OcrTableTool-V4.zip` 完全对齐**
| 项 | 值 |
|---|---|
| 目录 | `package\OcrTableTool-V5` |
| ZIP | `package\OcrTableTool-V5.zip`（**359.7 MB**，478 条目）|
| 文件数 / 展开大小 | **475 个 / 603.4 MB** |
| 顶层项 | **29 项 —— 与 V4 零差异**（`仅在 V4: 空`、`仅在 V5: 空`）|
| SHA256SUMS.txt | **474 项**（小写 hex + 相对路径，对齐 V4 格式）|
| third-party-licenses | **57 份** |

**包内内容**
| 组件 | 明细 |
|---|---|
| GUI | `OcrTableTool.exe` 320.5 KB |
| Qt 运行时 | 12 个 DLL + `opengl32sw.dll` + `d3dcompiler_47.dll` |
| Qt 插件 | 8 个目录（audio/bearer/iconengines/imageformats/mediaservice/platforms/playlistformats/translations）|
| 后端 | `ocr-runtime\OcrBackend\`（exe **11,121,054 B**，含全部修复）|
| **接口 SDK** | `interface-sdk\qt\` 5 文件 + `interface-sdk\example\` + `README.md`（对齐 `ocrtableclient(1).zip` 格式）|
| 文档 | `CAMERA_OCR_INTERFACE.md`、`LICENSE`、`THIRD_PARTY_NOTICES.md` |
| 安装 | `vcredist_x64.exe` 14.9 MB |

**包内后端冒烟测试（实测）**
```
HEALTH     status=ok  models_valid=True
RECOGNIZE  status=ok  rows=21 cols=4 review=3
R13C4='55'  (truth 55)  ✅
R18C2='91'  (truth 91)  ✅
```
⇒ 包内后端**可直接运行**，且三个缺陷修复全部生效。

**最终量化（121-320，200 例同指纹 `ee10d387ddb481d8`，无并发污染）**
| 指标 | 旧版 | 当前 | 变化 |
|---|---|---|---|
| mean_hard_strict | 0.6497 | **0.6992** | **+0.0495** |
| 归零率 | 23.5% | **19.0%** | **−4.5 pp** |
| 可读子集均值 | 0.8406 | **0.8562** | +0.0156 |
| 提升 / 变差 | — | **16 / 2** | 净收益 |

**新建脚本**：`scripts\package_client_full.ps1`（可复用的完整包打包脚本，属源码，保留在仓库）

**门禁**：后端 **1579** / 根门禁 **126** / GUI 单测 **28** / SDK 单测 **15** —— 全部通过。

### R70：2026-11-10（**FC-00295 取证**：双重问题，列边界系统性偏移不可修）

**取证（37×7，形状全对但 hard 0.066）**
```
TRUE R0: ['类型','基础信息','','','详细信息']            ← 分组行（两级表头第一行）
TRUE R1: ['','采购申请单','物料编号','开始日期','终止日期']
OURS R0: ['','采购申请单','物料编号','开始日期','终止日期']  ← 分组行被删，整表上移 1 行
```
**但更关键的是列边界系统性偏移**（`difflib` 逐行对齐取证）：
| 真值行 | 我们的行 | 差异 |
|---|---|---|
| `外协\|外协加工\|陈静\|…` | `\|外协加工\|陈静\|…` | 首格 `外协` 丢失 |
| `\|包装箱\|王伟\|…` | `欢\|包装箱\|王伟\|…` | 首格多 `欢` |
| `\|库控件\|孙丽\|…` | `P\|库控件\|孙丽\|…` | 首格多 `P` |
| `调度中心\|…` | `[\|调度中心\|…` | 首格多 `[` |

⇒ **每一行首格都在多/少字符**，这是**列检测（`_borderless_spatial_grid`）层**的系统性偏移，
已实证改动 7 次全败。即使修好"分组行被删"，列偏移仍会让 hard 极低。
**该例不值得投入。**

**final2（121-320，200 例）归零 38 例的最终可修性分布**
| 类型 | 例数 | 占比 | 可修性 |
|---|---|---|---|
| **D 识别错误**（best<0.85）| **23** | **61%** | ❌ 模型精度，不可修 |
| C 内容对但结构偏（best≥0.85）| 7 | 18% | ⚠️ 根因在网格构建层（含列偏移）|
| B 形状对内容错 | 5 | 13% | ❌ 识别错误 |
| A 1×1 空表 | 3 | 8% | ❌ 图像不可读 |

**已确认的可修边界（本轮阶段完整结论）**
| 类型 | 状态 |
|---|---|
| 表格外元素误检（页眉/行号列/列标行/表头空行/空白列）| ✅ **本阶段全部修完** |
| 少数票错值（`55`→`552`、`91`→`912`）| ✅ **已修**（PF-00001 100%）|
| GUI 契约校验丢整表 | ✅ **已修**（降级发布）|
| 分组行被删 | ⚠️ 已试 3 次，均伤满格行保护，回滚 |
| **列边界偏移 / 首格多字** | ❌ 网格构建层，7 次全败 |
| **识别错误（61% 剩余归零）** | ❌ 模型精度 |

**本阶段最终数字（121-320，200 例同指纹 `ee10d387ddb481d8`）**
| 指标 | 旧版 | 当前 | 变化 |
|---|---|---|---|
| mean_hard_strict | 0.6497 | **0.6992** | **+0.0495** |
| 归零率 | 23.5% | **19.0%** | **−4.5 pp** |
| 可读子集均值 | 0.8406 | **0.8562** | +0.0156 |
| 提升 / 变差 | — | **16 / 2** | 净收益 |

**交付件**：`package\OcrTableTool-V5.zip`（359.7 MB，478 条目，顶层结构与 V4 零差异）

### R71：2026-11-10（**第 4 次验证同一结论**：分组行在网格构建层就丢失，后处理层无法补救）

**发现了一条 100% 精确的结构特征（真值统计，1200 例）**
| 事实 | 数据 |
|---|---|
| 两级表头用例的 R0（分组行）填充数 | **全部恰好 3 格**（范围 3~3）|
| R0 < R1 的用例 | **250 / 250（100%）** |
| R0 == R1 或 R0 > R1 | **0 例** |
| 单级表头用例中符合"R0=3 格且 R1 更满" | **0 例（950 例中）** |

⇒ 「首行恰好 3 格 + 次行填充更多」是**两级表头分组行的独有签名**，
与页眉（1 格日期/页码，或满格键值）形态完全不重叠。

**实现了保护判据**（只减少删除，纯收紧）：`if counts[0] == 3 and counts[1] > 3: protected_rows.add(0)`

**但实测无效**
```
FC-00295: IN rows=38 counts[:3]=[5,6,6] lead=1  R0C0='6522262'
          ← 进入时首行是数字 '6522262'；真值分组行 ['类型','基础信息','','','详细信息'] 根本不在
FC-00234: IN rows=11 counts[:3]=[6,6,7] lead=2   ← 真值 14 行，已少 3 行
FC-00274/FC-00260: 同样无改善
```
**根因**：分组行在 `_borderless_spatial_grid`（网格构建/列聚类层）就已经丢失，
后处理层（`_leading_caption_band_length`）**根本拿不到它** ⇒ 已按规则回滚。

**这是第 4 次验证同一结论**（此前 `FC-00211`/`FC-00680`/`FC-00306` 已各自验证过）：
| 用例 | 现象 | 结论 |
|---|---|---|
| FC-00211 | `DROP-IN rows=12 lead=0`，R0 已是数据行 | 分组行/表头在进入前丢失 |
| FC-00680 | `_borderless_spatial_grid` 本身输出 43×9（真值 42×10）| 行列数在构建层就偏 |
| FC-00306 | 表头与数据被合并进同一格（`'密封圈\n规格型号\n8963'`）| 同上 |
| **FC-00295/FC-00234** | 进入时 R0 已是数字/空行 | 同上 |

**回滚后**：指纹恢复 `EE10D387DDB481D8`（= 交付包 V5），后端 **1579 通过**。

**本阶段最终可修边界（完整、已闭合）**
| 类型 | 状态 |
|---|---|
| 表格外元素误检（页眉/行号列/列标行/表头空行/空白列）| ✅ **全部修完** |
| 少数票错值（`55`→`552`、`91`→`912`）| ✅ **已修**（PF-00001 100%）|
| GUI 契约校验丢整表 | ✅ **已修** |
| 既有护栏"写了但没调用" | ✅ **已接通** |
| 分组行/表头丢失、列边界偏移 | ❌ **网格构建层**，验证 4 次/实证改动 7 次全败 |
| 识别错误（61% 剩余归零）| ❌ 模型精度 |
