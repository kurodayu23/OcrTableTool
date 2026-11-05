# 测试与门禁（TESTING）

> 结论先行：改完代码只跑一条命令 —— `pwsh -File scripts\verify_all.ps1`。
> 它任一失败即退出码非 0，并且**只打印首要错误行**，完整日志落盘。
> CI 加 `-Json` 拿机器可读摘要。要跑交付包校验加 `-IncludeDelivery`，要编译 C++ 加 `-IncludeCpp`，
> 要守真实翻拍照片的逐格准确率加 `-IncludePhoto`。

本文件解决的是"**流程型**缺陷"：代码没错，但流程让人测错了东西。
2026-10-28 一次就集中暴露了四类，见文末「已知坑」。

---

## 一、一键门禁

```powershell
# 日常（约 3.5 分钟，后端单测占 190~230 秒）
pwsh -File scripts\verify_all.ps1

# CI：只有一段 JSON 输出，其余全部进日志
pwsh -File scripts\verify_all.ps1 -Json

# 交付前：再加交付包校验 + 客户侧自检 + 交付说明数字校验（几分钟，需要已打好的包）
pwsh -File scripts\verify_all.ps1 -IncludeDelivery

# 要编译并跑 C++ 测试
pwsh -File scripts\verify_all.ps1 -IncludeCpp
pwsh -File scripts\verify_all.ps1 -IncludeCpp -SkipCppBuild   # 复用已有构建目录，只跑测试

# 要跑真实翻拍照片的逐格准确率门禁（需要先构建 photo-corpus，见「门禁清单」）
pwsh -File scripts\verify_all.ps1 -IncludePhoto

# 语料不在默认位置（<产品树>\photo-corpus）时，用 -PhotoCorpusDir 指定
pwsh -File scripts\verify_all.ps1 -IncludePhoto -PhotoCorpusDir D:\somewhere\photo-corpus
```

产物：`D:\AP\AP7000\OcrTableTool\_work\gate-r1\run-<时间戳>\`
（`logs\*.log` 逐项完整输出、`logs\*.json` 静态门禁明细、`summary.json` 机器摘要）。

### 门禁清单

> 2026-11-05 更新：步骤**编号与执行顺序**、真实照片门禁的判据、C++ 构建脚本
> 都已按当前实现逐条核对过（改动都标了「订正」，见下面各条）。
> 此前停在 10-28 / 11-02，此后门禁新增了 16 处判据（见「门禁判据演进」一节）。**以本表为准**。

**基础步（默认全跑，15 步）**

| # | 名称 | 默认 | 说明 | 典型耗时 |
|---|---|---|---|---|
| 1 | `bom` | 开 | `src\`、`tests\`、`interface-sdk\` 下含非 ASCII 的 `.cpp/.h` 必须带 UTF-8 BOM | 0.2s |
| 2 | `identity` | 开 | `backend\tests`、`tests\*.py` 不许写死指向本机项目树的绝对路径（**仓库树 + 产品树两棵都扫**） | 0.3s |
| 3 | `backend-unit` | 开 | `python -B -m unittest discover -s backend\tests -t backend\tests`（1538 用例） | 190–230s |
| 4 | `contract-ui` | 开 | `tests\test_ui_contract.py` | 0.3s |
| 5 | `contract-sdk` | 开 | `tests\test_interface_sdk_contract.py` | 0.3s |
| 6 | `hygiene-unit` | 开 | `tests\test_repo_hygiene.py`（覆盖 1/2/7 + 交付说明模板） | 1.5s |
| 7 | `gate-log-unit` | 开 | `tests\test_gate_error_line.py`（首要错误行提取器） | 2.5s |
| 8 | `gate-delivery-unit` | 开 | `tests\test_gate_delivery_package.py`（当前交付位解析器） | 2.5s |
| 9 | `gate-policy-unit` | 开 | `tests\test_gate_step_policy.py`（"验不到"的判定策略） | 1.5s |
| 10 | `gate-identity-unit` | 开 | `tests\test_gate_identity_scope.py`（身份门禁的扫描范围） | 0.3s |
| 11 | `gate-vacuous-unit` | 开 | `tests\test_gate_vacuous_scan.py`（静态扫描不许"扫到 0 个文件"也算过） | 0.2s |
| 12 | `gate-coverage-unit` | 开 | `tests\test_gate_step_coverage.py`（**步骤清单**核对 + 静态交叉核对） | 10s |
| 13 | `hygiene-isolation-unit` | 开 | `tests\test_hygiene_fixture_isolation.py`（门禁用例不许拿真实机器目录当夹具） | 0.2s |
| 14 | `qt-integrity-unit` | 开 | `tests\test_qt_test_integrity.py`（C++/Qt 侧测试黑洞：孤儿方法 / 没实例化的类 / 平凡断言） | 0.2s |
| 15 | `mirror` | 开 | 仓库工作树 ↔ 产品树镜像逐文件比对（LF 归一化 + SHA-256） | 0.3s |

> `gate-coverage`（**步骤清单核对**）**永远排在最后一步** —— 它得在其它步都跑完之后
> 才能核对"实际跑了哪些步 vs 必须跑的清单"（少跑 / 多跑 / 重名 ⇒ FAIL）。
> 所以结果里的编号是：基础 1~15 → 可选步按**执行顺序**接在后面 → `gate-coverage` 最后一条。

**真实照片步（`-IncludePhoto`，1 步；排在交付步之前）**

| # | 名称 | 说明 | 典型耗时 |
|---|---|---|---|
| 16 | `photo-acceptance` | `scripts\gate_photo_acceptance.py`：**真实翻拍照片**的逐格端到端准确率 + "错值不许静默"（判据见下） | 30–90s |

**交付步（`-IncludeDelivery`，3 步）**

| # | 名称 | 说明 | 典型耗时 |
|---|---|---|---|
| 17 | `delivery-notes` | `交付说明.md` 的数字能否由模板 + 实际目录重算 | 0.3s |
| 18 | `delivery-verify` | `scripts\verify_delivery_package.py`（**真的跑打包后的 exe**；脚本自己打印 `summary: N passed, N failed, N skipped`） | 1–5min |
| 19 | `delivery-selfcheck` | `scripts\customer_selfcheck.py`（客户侧视角；脚本自己打印"自检结果：通过 N 项，失败 N 项，跳过 N 项"） | 1–5min |

**C++ 步（`-IncludeCpp`，4 步）**

| # | 名称 | 说明 | 典型耗时 |
|---|---|---|---|
| 20 | `cpp-build` | `scripts\build_qt_all.ps1`（三套都建：主程序 + core tests、GUI tests、SDK tests；qmake + nmake） | 2–10min |
| 21 | `cpp-core-test` | `<build>\tests\release\tst_ocrtable_core.exe` | 秒级 |
| 22 | `cpp-gui-test` | `<build>\gui-tests\bin\tst_ocrtable_gui.exe` | 分钟级 |
| 23 | `cpp-sdk-test` | `<build>\sdk-tests\release\tst_ocrtableclient.exe` | 秒级 |

> ⚠ 2026-11-05 订正：上表原来写的是 `scripts\build_msvc2015.ps1` 与 `<build>\tstgui\bin\...`，
> **与实现不符**。门禁实际跑的是 `scripts\build_qt_all.ps1` —— 只跑 `build_msvc2015.ps1` 的话
> GUI 套件与 SDK 套件根本不会被构建，干净构建目录上 `-IncludeCpp` 必然报两项 FAIL；
> GUI 测试的产物目录是 `gui-tests\bin\`（不是 `tstgui\bin\`）。

> 2026-11-05 新增。**为什么合成语料不够**：field-corpus 的 1200 例全是渲染
> 出来的清晰表格，它测不到翻拍照片上的框线残留、背面透印、倾斜拍摄、低分辨率。
> 真实照片上"真值 `55` 被读成 `552`""真值 `2298` 被读成 `12298`"这类**静默瞎编**
> 就是这么漏到用户手上的 —— 合成语料长期全绿，真实照片一直没人守。
>
> 这一步同时守住三件事（`scripts\gate_photo_acceptance.py`）：
> ① **逐格准确率** `--min-cell-exact-rate`（默认 **0.97**）不低于阈值；
> ② **错格总数** `--max-wrong-cells`（默认 **4**）不超过上限；
> ③ **错值必须被标黄**（`needs_review=true`）—— "读错了却没标黄"的格数
>    `--max-silent-wrong-cells`（默认 **0**）必须为 0。
>    这一条是"杜绝静默瞎编"的制度性防线：即使准确率还没修到位，错值也**不许**
>    以"已确认"的姿态导出。
>
> 前置：真实照片语料（人工逐格真值）。
> ```powershell
> # 1) 构建语料（把照片 + 人工真值转成 field-corpus 同款布局）
> python scripts\build_photo_corpus.py `
>     --source "C:\Users\jss18\Pictures\excel3" --output photo-corpus
> # 2) 跑门禁
> pwsh -File scripts\verify_all.ps1 -IncludePhoto
> ```
> 人工真值在 `scripts\photo_corpus_truths.py`（**逐格放大核对后转录**，看不清的格
> 放进歧义格集合，不参与比对）。改动它等于改动门禁的判据，改前请三思。

### 真实照片门禁的判据与参数（`scripts\gate_photo_acceptance.py`）

门禁内部实际执行的那条命令：

```powershell
python -B scripts\gate_photo_acceptance.py --corpus <语料目录> --output <报告目录>
```

- `--corpus`：语料目录，必须同时有 `images/` 与 `_truth/`。
  `verify_all.ps1` 不传 `-PhotoCorpusDir` 时默认取 **`<产品树>\photo-corpus`**。
- `--output`：报告目录；门禁传的是 `<OutDir>\photo-gate`，落盘 `photo-gate-report.json`。
- `--ids PF-00014,PF-00001`：只跑指定用例（默认全跑）。
- `--model-dir`：模型目录（默认 `<项目>\runtime\models`）。
- `--skip-models`：跳过模型指纹检查（`batch_acceptance_runner.verify_models`）。
- 这一步**不是**标准库能跑的：它 `import ocr_backend`，需要完整后端依赖
  （numpy / opencv / onnxruntime / rapidocr）与 `runtime\models\` 下的模型。

退出码：

| 码 | 含义 |
|---|---|
| 0 | 判据全过 |
| 1 | 判据未达标（准确率 / 错格数 / 静默错值） |
| 2 | 语料或环境不可用：模型门禁不过、`images/` + `_truth/` 不全、或没有可跑的用例 |

> ⚠ `gate_photo_acceptance.py` 的模块 docstring 里把 `--min-cell-exact-rate` 的示例写成 `0.98`，
> 但 `argparse` 的**实际默认值是 0.97** —— 以 0.97 为准。

### 人工真值（`scripts\photo_corpus_truths.py`）

- `CASES`：`case_id → (真值网格, 歧义格集合)`。真值网格是**逐格人工放大核对后转录**的文字。
- 歧义格（写进 `_truth\*.json` 的 `ambiguous_cells`）：人工划改 / 印刷不清 / 真值不确定
  ⇒ **不参与**逐格比对（`gate_photo_acceptance.py` 的 `load_truth` 直接跳过）。
  当前只有 `PF-00014` 的 `(6, 4)` 一格。
- `SOURCES`：`case_id → 原图相对 `PHOTO_ROOT` 的路径`；`PHOTO_ROOT` 是照片库根目录。
- ⚠ `photo_corpus_truths.py` 的注释写着"默认从环境变量 `OCR_TABLE_PHOTO_ROOT` 覆盖"，
  但代码里**没有**读这个环境变量 —— 换机器请用 `build_photo_corpus.py --source <照片库>`。
- 语料目录（`photo-corpus\images\` + `photo-corpus\_truth\`）是**本机生成物**，不入库：
  登记进版本库的是 `scripts\photo_corpus_truths.py` 里的真值源，语料由
  `scripts\build_photo_corpus.py` 重建。
  ⚠ 产品树的 `.gitignore` 目前**没有**登记 `photo-corpus/`（只登记了 `field-corpus/`、
  `field-corpus-nearfill/`、`excel-screenshot-corpus/`、`no-line-handwritten-corpus/`、
  `stress-test-corpus/`），别把重建出来的语料提交进去。

### 「没查」怎么算

**点名的检查项查不到 ⇒ FAIL，不是 SKIP**：

- 契约测试文件缺失 ⇒ FAIL；
- 点了 `-IncludeDelivery` 但交付包 / exe 不存在 ⇒ 三步全 FAIL；
- 点了 `-IncludeCpp` 但测试可执行文件没构建出来 ⇒ FAIL；
- 点了 `-IncludePhoto` 但 `scripts\gate_photo_acceptance.py` 或真实照片语料不存在 ⇒ FAIL
  （"没查"不等于"通过"）。

**只有一种情况允许 SKIP**：用户**明确**传了 `-SkipCppBuild`（= 本来就打算复用旧构建目录）。
其余没查到的情形一律 FAIL ——「没查」必须等同于「不通过」。

判定规则集中在 `scripts\gate_step_policy.ps1`（`Get-MissingStepStatus`），
可被单元测试直接点调用；不要在 `verify_all.ps1` 里裸写 `Add-Skip`。

### 步骤清单核对（`gate-coverage`）

门禁自己的"检查清单"曾经**没有任何强制力**：`PlannedGates` 只是个显示用计数器，
从执行列表里删掉一步、少跑一项，汇总照样 PASS。

现在「必须跑的步骤」写在 `scripts\gate_step_coverage.ps1` 的**独立清单**里
（`Get-ExpectedGateSteps`），跑完由 `Test-GateStepCoverage` 逐项核对：

- 少跑一步 ⇒ `missing` ⇒ FAIL；
- 多跑一步（没登记）⇒ `unexpected` ⇒ FAIL；
- 同名两步 ⇒ `duplicated` ⇒ FAIL。

清单**必须独立维护** —— 若由执行代码推导，执行代码少跑一步时清单也跟着少，
核对就变成自己跟自己比，缺陷照样漏网。改执行代码时记得同步改清单
（漏改会被 `gate-coverage-unit` 的静态交叉核对当场抓住）。

清单里现在登记的步骤（`Get-ExpectedGateSteps` 的返回）：

- **基础 15 步**（永远要跑）：`bom`、`identity`、`backend-unit`、`contract-ui`、`contract-sdk`、
  `hygiene-unit`、`gate-log-unit`、`gate-delivery-unit`、`gate-policy-unit`、`gate-identity-unit`、
  `gate-vacuous-unit`、`gate-coverage-unit`、`hygiene-isolation-unit`、`qt-integrity-unit`、`mirror`；
- `-IncludeDelivery` 时追加 3 步：`delivery-notes`、`delivery-verify`、`delivery-selfcheck`；
- `-IncludeCpp` 时追加 4 步：`cpp-build`、`cpp-core-test`、`cpp-gui-test`、`cpp-sdk-test`
  （`-SkipCppBuild` 也恒为 4 步 —— `cpp-build` 会被记成一条 SKIP 结果）；
- `-IncludePhoto` 时追加 1 步：`photo-acceptance`。

> ⚠ 2026-11-05 补：清单里**不含** `gate-coverage` 自己 —— 它是核对动作，不是被核对的那一步；
> 但它照样会作为一条结果出现在汇总里（最后一条）。新增步骤**必须先登记**，
> 否则会被判成 `unexpected` ⇒ FAIL。

### 门禁判据演进（2026-11-02）

当天一次性把 16 处"判据本身有洞"的问题挖出来并修掉，全部配了用例 + 变异证明：

| 编号 | 洞 | 修法 |
|---|---|---|
| F11–F13 | 「当前交付位」在三处被写死成旧包 | 按 exe mtime 解析当前位（`gate_delivery_package.ps1`） |
| F14 | 6 处内联守卫只有静态守卫、没有可执行行为断言 | 抽真实源码执行，20 条行为用例（后端一行未改） |
| F15 | 语料对照只比交集、永远 exit 0 | 单边样本 / 重复 ID / 条数不符都判 FAIL |
| F16 | 「验不到」只标 SKIP | `gate_step_policy.ps1` 统一判定 |
| F17 | 静态扫描恒退出 0 | 按判据给退出码 |
| F18 | 身份门禁只扫仓库树 | 两棵树都扫 |
| F19 | Python 侧测试黑洞 | `test_suite_completeness.py` 元测试 |
| F20 | 静态扫描扫到 0 个文件也算过 | `scanned == 0` ⇒ FAIL |
| F21 | 门禁的步骤清单没有强制力 | `gate_step_coverage.ps1`（本节） |
| F22 | 门禁用例拿真实机器目录当夹具 ⇒ 假红 | 改用临时目录夹具 + 隔离回归用例 |
| F26 | C++/Qt 侧测试黑洞无护栏 | `test_qt_test_integrity.py` |

### 退出码与 JSON

- 退出码：`0` 全通过（含 SKIP）；`1` 有 FAIL。
- `-Json` 输出结构：

```json
{
  "gate": "verify_all", "ok": false,
  "repo": "...", "product": "...", "branch": "...", "head": "c03703a",
  "mirror_baseline": "worktree",
  "started_at": "...", "finished_at": "...", "elapsed_seconds": 56.9,
  "passed": 5, "failed": 2, "skipped": 0,
  "out_dir": "...", "gates": [
    {"name": "bom", "status": "PASS", "seconds": 0.22, "summary": "...", "first_error": "", "log": "..."}
  ]
}
```

`first_error` 就是"首要错误行"：静态门禁取第一条明细（`backend/tests/x.py:26: ...`），
单测取第一条 `FAIL:`/`AssertionError`，编译取第一条 `error C####`。要更多看 `log`。

---

## 二、前置条件

### Python 侧

| 项 | 值 |
|---|---|
| 解释器 | `D:\AP\AP7000\OcrTableTool\.venv\Scripts\python.exe`（3.11.15） |
| 依赖 | **只用标准库**。后端单测用 `unittest`，契约/卫生测试也用 `unittest` |
| pytest | **venv 里没装**（`README.md` 第 44 行仍写着 `python -m pytest`，那条命令现在跑不通 —— 用下面的 unittest 写法） |
| 备注 | 仓库根的 `.venv` 是指向产品树 `.venv` 的符号链接，两个路径等价 |

> ⚠ `tests\` 目录**不是 Python 包**（没有 `__init__.py`，也不该加）。
> 所以不能用 `discover -s tests -t .`（会报 `Start directory is not importable`），
> 要么 `-s tests`（不带 `-t`），要么直接给文件路径。

### C++ 侧

| 项 | 值 |
|---|---|
| 编译器 | MSVC2015（VS14.0）：`C:\Program Files (x86)\Microsoft Visual Studio 14.0\VC\vcvarsall.bat` |
| Qt | Qt 5.9.6 msvc2015_64，`qmake` 需在 `PATH`，或用 `-QMake` / `QTDIR` 指定 |
| 测试运行环境 | `PATH` 必须含 **Qt bin**（`qttest` / `platforms\qwindows.dll`）与 **venv `Scripts`**（GUI 测试会拉后端 Python） |
| 平台插件 | `QT_QPA_PLATFORM=offscreen`（无显示器 / CI 必须；否则 GUI 测试直接 abort） |
| 临时目录 | `TMPDIR` / `TEMP` / `TMP` 指向**可写**且**不在仓库/产品树里**的目录 |
| 数据 | `tst_ocrtable_gui` 依赖 `QFINDTESTDATA`（`QT_TESTCASE_BUILDDIR` 宏），必须在构建目录里启动 |

`verify_all.ps1 -IncludeCpp` 会自动拼好上面这些（PATH、`QT_QPA_PLATFORM`、临时目录）。

---

## 三、单项手工命令

```powershell
$py = "D:\AP\AP7000\OcrTableTool\.venv\Scripts\python.exe"
$env:PYTHONIOENCODING = "utf-8"   # 中文日志在 pwsh 里不乱码

# 后端单测（1538 用例，约 190~230 秒）
& $py -B -m unittest discover -s backend\tests -t backend\tests

# Python 契约
& $py -B -m unittest tests/test_ui_contract.py
& $py -B -m unittest tests/test_interface_sdk_contract.py

# 仓库卫生（BOM / 测试树身份 / 镜像 / 交付说明模板）
& $py -B -m unittest tests/test_repo_hygiene.py

# 静态门禁单项（可 --json、可 --json-out）
& $py -B scripts\gate_checks.py bom
& $py -B scripts\gate_checks.py identity --product D:\AP\AP7000\OcrTableTool
& $py -B scripts\gate_checks.py mirror  --product D:\AP\AP7000\OcrTableTool --baseline worktree
& $py -B scripts\gate_checks.py all --product D:\AP\AP7000\OcrTableTool --json

# 交付说明模板
& $py -B scripts\delivery_notes.py render --package-dir D:\...\package\backend-delivery
& $py -B scripts\delivery_notes.py verify --package-dir D:\...\package\backend-delivery

# 交付包
& $py -B scripts\verify_delivery_package.py --package-dir D:\...\package\backend-delivery\ocr-runtime\OcrBackend --backend-source .\backend
& $py -B scripts\customer_selfcheck.py  --package-dir D:\...\package\backend-delivery

# 真实照片门禁（先有语料，再跑门禁）
& $py -B scripts\build_photo_corpus.py --source "C:\Users\jss18\Pictures\excel3" --output photo-corpus
& $py -B scripts\gate_photo_acceptance.py --corpus photo-corpus --output _work\photo-gate

# C++
powershell -File scripts\build_msvc2015.ps1            # 只建主程序 + core tests
pwsh -File scripts\build_qt_all.ps1 -BuildDirectory <dir>   # 门禁用的那个：三套都建
$env:QT_QPA_PLATFORM = "offscreen"
& build\windows-msvc2015\tests\release\tst_ocrtable_core.exe
```

### 每日自检（总入口）

一键门禁之外，日常维护跑的是**自检总入口**（在 `ai-workspace`，不进仓库）：

```powershell
# 全套：后端单测 + 三套 Qt + 交付三步 + 静态门禁 + 语料抽样与逐例对照（约 20 分钟）
& 'D:\AP\AP7000\OcrTableTool\.venv\Scripts\python.exe' `
    'D:\AP\AP7000\ai-workspace\selfcheck\self_check.py' --sample 20

# 深跑（语料 60 例，约 40 分钟）
... self_check.py --sample 60

# 换一批样本（覆盖扩展）：历史基线只到 FC-00665，语料有 1200 例
#   --corpus-shard I/K  在 sorted 索引上取 ids[I::K]（稳定可复现）
#   --corpus-offset N   从第 N 个开始连续取（优先于 shard）—— 取基线之外的新样本
... self_check.py --sample 60 --corpus-offset 666
```

产出：`D:\AP\AP7000\ai-workspace\selfcheck\run-<时间戳>\`（`summary.json` / `summary.txt` + 每步日志）。

判据要点：

- **语料步判"批次完整性"**（跑够了 / 记录齐了 / 有成功例 / 模型门禁通过 ⇒ 硬失败）；
  批次完整但有失败例 ⇒ WARN，并把失败例 ID 如实列进明细。
- **准确率与回退由 `corpus-head-to-head` 步逐例判**：基线成功的例本次失败 ⇒ FAIL；
  `cell_exact_rate` 回退超过容差（默认 0.01，可用 `--max-rate-drop` 调）⇒ FAIL。
  项目契约是「结果等价」，所以默认容差刻意很小。
- 交付包漂移、镜像漂移这两项是**长期红**（分别对应"交付件落后源码"与"仓库↔产品树
  需人工合并"），属于已登记、待人工处置的项，不是当天新引入的回归。

#### 自检侧的判据演进（F23–F56）

> 这些缺陷全在**自检总入口**（`ai-workspace\selfcheck\self_check.py`），
> 不在 `verify_all.ps1` 里，所以不在上面的「门禁判据演进」表中。
> 每一条都配了用例 + 变异证明（改坏源码后用例必须如期报红）。

| 编号 | 洞 | 修法 |
|---|---|---|
| F23 | 找不到候选包时兜底指向**还在磁盘上的旧包** ⇒ 验的不是当前位 | 兜底改为**不存在的占位路径**，下游必然判 FAIL |
| F24 | 语料步拿 runner 退出码当判据 ⇒ 恒红淹没真回退 | 改判**批次完整性** + 对照工具新增**逐例回退判定** |
| F25 | 每次留下 ~87 MB 构建目录、从不回收 | `prune_build_dirs.ps1` 保留最近 N 个、只删够老的 |
| F28 | `ocr-tests` 下工具自身的用例**没有任何一步会跑** | 新增 `tool-selftest` 步逐个跑并列出失败项 |
| F29 | **自检总入口自己也没有步骤清单核对** ⇒ 少跑一步照样整体 PASS | 独立维护的 `EXPECTED_STEPS`（21 步）+ `self-coverage` 步；静态交叉核对见下 |
| F30 | `corpus-analyze` 的退出码被丢弃、这一步**根本不在汇总里** | 变成正式的一步（失败 ⇒ FAIL） |
| F31 | 对照工具早退时不写 `head-to-head.txt` ⇒ 无保护 `copyfile` 把**整轮自检**崩掉 | 归档前判存在性，缺文件只如实说明、不抛异常 |
| F32 | `--limit` 是**确定性前缀** ⇒ 每轮「深跑」跑的都是同一批样本 | 自检侧改用**显式 ID 列表**：新增 `--corpus-shard I/K` 与 `--corpus-offset N` |
| F33 | 对照工具分不清「样本丢了」与「样本是新的」 | 新增 `--allow-no-baseline`（默认**不开**）；范围重叠时**仍然 FAIL** |
| F34 | 门禁工具在 **GBK 控制台**下 `print` 崩（`⚠` / `…` 等非 ASCII） | 全目录统一加 UTF-8 输出保护 + 通用静态护栏（新增工具自动纳入） |
| F35 | 两处「当前交付位」判定**各实现一遍**、注释声称一致、实际无交叉测试 | 统一排序键 `(exe mtime, 目录 mtime, 包名降序)` + 交叉一致性用例 |
| F36 | 历史基线是**旧后端** ⇒ 逐例回退判定把系统性差异误当成产品回退 | 回退**分类标注** + **分布级**预算失败率指标（只报不判）；根因用重建基线解决 |
| F37 | 文档里声明的门禁 / 自检**步数**无人校验 ⇒ 必然过期且已自相矛盾 | 新增 `verify_doc_gate_counts.py`，从代码解析真实步数逐处对照；接为自检 `doc-gate-counts` 步 |
| F38 | 门禁的 Python 契约测试是**逐个点名**跑的 ⇒ 漏登记一个文件，那个文件的测试**永远不会被门禁跑到** | 新增 `verify_test_file_coverage.py` 做**文件级**对照；接为自检 `test-file-coverage` 步 |
| F39 | `backend/fast_stats.py` 零单元测试 ⇒ 它「与 `np.median` 逐位相同」的承诺无人守 | 新增 `backend/tests/test_fast_stats.py`（16 条，含「uint8 路径根本没调用 `np.median`」的反向护栏） |
| F41 | `backend/colored_table.py` 的三级 import 兜底**最内层完全无告警** ⇒ 打包漏掉 `fast_stats` 会静默丢掉 12.9 秒加速，而**所有功能测试照样绿** | 运行时绑定用例（`backend/tests/test_colored_table_fast_path.py`）+ 打包侧静态护栏（`verify_backend_hidden_imports.py`，接为自检 `backend-hidden-imports` 步） |
| F42 | **待办表自己的条数声明**与实际条目不一致，且无人校验（§0 写「C 类 22 条」、标题写「40 条」、实际 40 行） | 新增 `verify_todo_table_counts.py` 逐类点数 + 编号唯一性 + 三处声明对照；接为自检 `todo-table-counts` 步 |
| F43 | **自检侧**的用例文件级覆盖无护栏 ⇒ `ocr-tests/test_test_file_coverage.py`（F38 自己那 7 条）**从来没被任何步骤跑过** | 新增 `verify_tool_test_coverage.py`（AST 解析，不认注释）双向核对；接为自检 `tool-test-coverage` 步，并把漏掉的那个文件补进 `tool_tests` |
| F44 | `STATUS.md` 的「仍待修（已登记，下一轮）」清单**已过期且是假登记** —— 点名的 4 项早已修好（同文件 R6/R7 节就写着"已修"），且**从未进入待办表** | 修正该行并显式点名 `B14`；新增 `verify_status_todo_crossref.py`（清单点名的编号必须真的存在于待办表）；接为自检 `status-todo-crossref` 步 |
| F45 | 对照工具的「**不可比**」只被**标注**、从没被**判定** ⇒ 不可比的两批照样能产出「产品回退」结论 | 可比性问题升级进 FAIL 判定（`comparability_problems`） |
| F46 | 缺**稳定身份**的环境指纹 ⇒ 「是不是同一台机器、同一份代码」无法判定 | 批次落 `run-environment.json` 指纹；对照前逐项核对身份键 / 条件键 |
| F47 | 指纹里的**系统版本字段被产品自己的离线钩子污染**（`platform.win32_ver()` 拿不到子进程结果）⇒ 指纹不可信 | 改用 `sys.getwindowsversion()[2]` 取真实构建号 |
| F48 | F45 的护栏用例在 F46 落地后**悄悄失去承重能力**（exit 1 既可能来自回退、也可能来自缺指纹） | 用例改用「两边指纹齐全」场景，并**额外断言**输出里的回退标记 |
| F49 | 「[可比]」在**一项都没比**的时候也会出现 ⇒ 假绿表述 | 可比性判定返回 `compared`；为 0 时改打印「[未验证]」 |
| F50 | 产品契约「**低内存只降级、不拒绝**」**没有任何自动判据** | 新增 `verify_low_memory_degrade_only.py`（**AST** 静态判据：不得 `raise MemoryError`、低档分支只准改白名单里的量且不得调模块内函数）；接为自检 `low-memory-contract` 步 |
| F51 | 低内存档跑批把**墙钟预算耗尽**误判成「拒绝」⇒ 必然输出假结论 | 分桶只认**内存拒绝签名**；预算耗尽归「更慢」不归「拒绝」 |
| F52 | 语料失败分桶**看不见**「内存原因拒绝」⇒ 淹没在没人盯的桶里 | `bucket()` 新增该类（判在预算类之前）；新增分桶必配「不得落进『其他失败』」反向用例 |
| F53 | `verify_all.ps1 -IncludeCpp` 的 C++ 步在干净构建目录上**必然失败两项** —— GUI / SDK 两套测试**根本没被构建** | 新增 `scripts/build_qt_all.ps1`（三套都建）+ 修正二进制路径模板；门禁步数不变 |
| F54 | 墙钟边界的**抖动被当成产品回退**；直接判抖动又会「复测一失效就假绿」 | 临界预算翻转**必须复测**：复测仍失败 ⇒ 照样 FAIL；复测成功 ⇒ 判抖动并**如实标注**；**没复测 = 没查 = 不通过**；默认阻断 |
| F55 | 自检脚本里 F54 的编排调用了一个**根本没定义**的函数 ⇒ 真踩到就 `NameError` **崩掉整轮自检**，且那条分支**一次都没被执行过** | 补上该函数（含自洽校验）+ 新增 `test_self_check_names.py`：用 `symtable` 做**名字绑定分析**，抓「引用了但没定义」 |
| F56 | 复测记录的**前提条件**没被校验 ⇒ 缺指纹 / 跨内存档位 / 换机器的复测**照样被采信** | 改成**缺值即问题**（缺指纹 / 档位不同 / 机器不同 ⇒ 没查 = 不通过）；档位不同的 id 不判抖动、仍算阻断项 |
| F58 | 一轮自检**运行期间**改了它要检查的文件 ⇒ 该轮报**假红**；更危险的方向是**假绿**（改了判据本身，那轮照样绿、汇总里看不出来） | 开跑前给「本轮输入」拍 sha256 指纹快照、收尾时再算一遍；对不上 ⇒ 本轮结论不可信 ⇒ **判 FAIL**（并进 `self-coverage` 步，步数不变） |

> ⚠ **F32 的要害**：`--limit N` 取的是 `cases[:N]`，没有任何轮换机制 ⇒ 第 6 轮那 20 例
> ⊆ 第 7 轮的 60 例，逐例 delta 全是 `0.0000` —— 那**不是**"识别稳定"，
> 那是**同一批样本**被反复跑。历史基线只覆盖到 FC-00665，语料有 1200 例 ⇒
> 后面那 535 例从没跑过。runner 自带的 `--shard-index/--shard-count` 也救不了
> （`load_cases()` 先按 `--limit` 截断、**之后**才分片）。

**F41 的要害**（打包环节的静默降级，与上面几条性质不同）：

`backend/colored_table.py` 顶上是一段三级兜底 import —— 绝对导入 → 相对导入 →
**纯 `np.median` 兜底**。第 3 级功能完全正确（与直方图中位数逐位相等），
但会**白丢 12.9 秒**（单个请求里 22 552 次中位数调用，`ndarray.partition` 的代价）。

源码树里永远走第 1 级 ⇒ **本机自检永远绿**，只有 PyInstaller 出来的 exe 才会退化。
所以这条要**两侧**都设防：

1. **运行时侧**：`backend/tests/test_colored_table_fast_path.py` 断言
   `colored_table.median_u8 is fast_stats.median_u8`（对象身份，不是"数值相等"）；
   ⚠ 并用「把 `numpy.median` 打成会抛异常的桩」证明 uint8 路径**根本没走** `np.median`。
   降级后**对拍类断言照样全绿** ⇒ 只有绑定判定与反向护栏能抓住它。
2. **打包侧**：`ocr-tests/verify_backend_hidden_imports.py` 扫出所有
   「在 `except ImportError` 兜底分支里导入的同级后端模块」，
   要求它们既出现在 `scripts/package_backend.ps1` 的 `--hidden-import` 里，
   也出现在**当前交付位** `OcrBackend.spec` 的 `hiddenimports` 里。

> ⚠ 判据"没查"一律当"不通过"：spec / 打包脚本读不到、后端源码语法错 ⇒ 报红；
> 但"本树确实没有同级兜底导入"是**合法状态**，不报红。

**F29 的两道防线**（与 `gate-coverage` 同构，但对象是自检总入口）：

1. **运行期**：独立维护的 `EXPECTED_STEPS` 清单 vs 汇总里实际出现的步骤名 ——
   少跑 / 多跑 / 重名 ⇒ FAIL（`delivery-drift` 每个候选包跑一次，允许多次）。
   清单**不能**由执行代码推导，否则执行代码少跑一步时清单也跟着少 ⇒ 核对形同虚设。
2. **静态期**：`ocr-tests\test_self_check_step_coverage.py` 从 `self_check.py` 的
   AST 里提取实际步骤名，与清单交叉核对 —— 清单改了代码没改、或代码改了清单没改，
   两边都会被抓。

还有一条**通用静态护栏**（`test_self_check_corpus_analyze.py`）：自检脚本里不得出现
「返回值被丢弃」的 `run_step(...)` 调用。F15 与 F30 都是这个模式，这一族缺陷不会再复发。

> ⚠ 自检脚本**每一步**都必须出现在汇总里 —— 包括"因为前置条件不满足而跳过"的情形
> （无候选包、无历史基线、`--skip-corpus`）。以前这些情形会让该步**整段消失**，
> 汇总上"少一步"和"跑了但失败"长得一模一样。现在一律显式记 PASS / FAIL / SKIP。

`mirror` 的两种基线：

- `--baseline worktree`（默认）：镜像应等于**当前工作树**。日常用这个 —— 它回答
  "我要打的包是不是当前代码"。
- `--baseline head`：镜像应等于 **HEAD**。它回答"交付的东西有没有进版本库"，
  顺带把"工作树有未提交改动、还没同步到镜像"的文件单独列出来（不算失败）。

---

## 四、已知坑

### 1. BOM：改 `.cpp/.h` 时最容易踩

**现象**：MSVC2015 报 `C2001: 常量中有换行符` / `C1057: 宏展开中缺少右括号` /
一堆莫名其妙的语法错误，而且**报的行号是错的**，很难联想到编码。

**原因**：源码里有中文字符串字面量/中文注释，但文件没有 UTF-8 BOM。MSVC2015 在没有
BOM 时按系统 ANSI（GBK）解释源文件，UTF-8 字节被拆成非法 GBK 序列，字符串提前结束。

**规则**：`src\`、`tests\`、`interface-sdk\` 下**只要含非 ASCII 字符**的 `.cpp/.h`
就必须带 UTF-8 BOM（纯 ASCII 文件带不带都行）。

**特别注意工具链**：不少编辑器 / 补丁工具 / `git apply` / 某些 AI 改写流程会把 BOM
**悄悄剥掉**（"UTF-8 无 BOM"是很多工具的默认输出）。改完 C++ 一定跑一次：

```powershell
& $py -B scripts\gate_checks.py bom
```

补 BOM：

```powershell
$p = "src\core\csvexporter.cpp"
$bytes = [IO.File]::ReadAllBytes($p)
if (-not ($bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF)) {
    [IO.File]::WriteAllBytes($p, [byte[]](0xEF,0xBB,0xBF) + $bytes)
}
```

### 2. 测试树身份：测试在"测镜像"而不是被测树

**现象**：本地单跑某个测试模块全绿，全量 `discover` 却多出几个 ERROR；
或者改了仓库代码但测试行为不变（因为测的是 `D:\AP\AP7000\OcrTableTool` 那份镜像）。

**原因**：`unittest discover` 按**文件名排序**加载模块，第一个模块在 `import` 阶段就把
产品树镜像路径塞进 `sys.path` / `sys.modules`，后续 `import ocr_backend` 命中的是镜像。

**规则**：`backend\tests`、`tests\*.py` 里**不许**出现指向本机项目树的绝对路径。
要定位仓库根，用：

```python
ROOT = Path(__file__).resolve().parents[1]        # tests/x.py     -> 仓库根
ROOT = Path(__file__).resolve().parents[2]        # backend/tests/x.py -> 仓库根
```

确实必须写死时（例如要在交付位自检里指路），在**同一行**加豁免注释：

```python
REAL = Path(r"D:\AP\AP7000\OcrTableTool")  # gate:allow-machine-path 交付位自检需要
```

`scripts\gate_checks.py` 里 `DEFAULT_PRODUCT_MIRROR` 是**唯一**允许写死该路径的地方
（它不被 identity 门禁扫描），其它地方一律通过 `OCRTABLE_PRODUCT_MIRROR` 环境变量覆盖。

### 3. 临时目录：位置比权限更重要

**现象**：单独跑测试全绿，`verify_all.ps1` 里跑却红，报
`AssertionError: ... is not None` / `Wind...7000/OcrTableTool/backend != Wind.../worktree.../backend`。

**原因**：`TMPDIR` 指到了产品树内部时，测试里"沿包路径向上找 `backend\ocr_backend.py`"
的逻辑会**沿途命中真实源码树**（`D:\AP\AP7000\OcrTableTool\backend`），
把"找不到源码"的用例测成"找到了"。

**规则**：`TMPDIR` / `TEMP` / `TMP` 必须是**可写**且**不在仓库 / 产品树内部**的目录。
`verify_all.ps1` 默认落到系统临时目录 `%TEMP%\ocrtable-gate-<时间戳>`，一次运行一个子目录。
权限问题（只读目录、被杀软锁住）表现为 `PermissionError` / `OSError: [WinError 5]`。

### 4. 镜像一致性：CRLF 是常态，别逐字节比

仓库工作树是 **CRLF**（`core.autocrlf=true`），产品树镜像的协议是 **LF**。
`git status` 里那些"改了行尾"的文件和"真改了内容"的文件长得一样。
门禁的做法是先把 `CRLF`/`CR` 归一化成 `LF` 再比 SHA-256 —— **只比真实内容差异**。

另外镜像里有 22 个只在产品树存在的产物（`runtime\models\*` 模型权重、
`docs\superpowers\plans\*` 历史计划），它们已被 `.gitignore` 排除，比对时跳过并报出跳过条数。

**同步是人工动作**：门禁只报告差异，不会自动拷贝（自动覆盖产品树会毁掉运行时状态）。
差异清单就是同步清单，逐条 `Copy-Item` 即可。

### 5. 中文日志乱码

`PYTHONIOENCODING=utf-8`（`verify_all.ps1` 已自动设置）与 `chcp 65001`。
控制台代码页是 936 时，Python 的 `stdout` 按 GBK 编码输出，中文断言消息会变
`ȱ UTF-8 BOM` 这种乱码 —— 不是测试失败，是显示问题。

### 6. 交付说明里的数字会过期

`交付说明.md` 的文件数 / 体积 / 最新修改时间是按目录遍历算出来的。包重建后不重算，
客户拿到的就是"上一版包的规模"。规则见 `packaging\README.md`：
**必须重算的 12 个数字**自动算；**历史测量记录 / 构建指纹 / 许可分类明细**必须人工改。
`licenses\` 补入后要重跑一次 `delivery_notes.py render`。

### 7. 构建目录只增不减（磁盘被自检吃满）

**现象**：`D:\AP\AP7000\AP7000-Build-Directories\OcrTableTool\` 越来越大。
2026-11-02 实测 26 个目录共 **3.88 GB**，其中 `build-selfcheck-*` **11 份 ≈ 955 MB**。

**原因**：每日自检的 `ocr-tests\build-qt-tests.ps1` 每次用**带时间戳的新目录**做全量构建
（`build-selfcheck-<yyyyMMdd-HHmmss>`，避免复用旧二进制），但**从不清理旧的**。
按 ~40 分钟一轮、87 MB/轮计，约 **3 GB/天**。

**修法**：`ocr-tests\prune_build_dirs.ps1` 的 `Remove-StaleBuildDirs` 在**开建之前**回收：
保留最近 3 个、只删比 6 小时还老的、只碰匹配到的**目录**、默认 `-WhatIf` 不真删。
`build-qt-tests.ps1` 提供 `-KeepBuildDirs N` 与 `-NoPruneBuildDirs` 开关。

### 8. C++/Qt 侧的测试黑洞

`QTest` 只跑 `private slots:` 里声明过的函数。测试类里新增了
`void Foo::bar()` 却忘了在 `private slots:` 声明 `void bar();` ⇒ 这个用例
**一次都不会跑**，而套件汇总照样显示 `Totals: N passed, 0 failed`。

同理：新增测试类却没改 `QTEST_MAIN(X)`（或手写 `main()` 里的 `X tests;`）⇒ 整类永不执行；
测试函数里只有 `QVERIFY(true)` ⇒ 什么都不断言。

Python 侧的黑洞由 `backend\tests\test_suite_completeness.py` 覆盖；
C++/Qt 侧由 `tests\test_qt_test_integrity.py` 覆盖（门禁步 `qt-integrity-unit`）。

### 9. 跑批脚本写固定目录 ⇒ 一次误启动就毁掉已固化的基线

**现象**：2026-11-02 10:21:58，想看低内存档跑批脚本的参数，跑了个 `--help` ——
而那个脚本是**手写 argv 解析、根本不认 `--help`** ⇒ `--help` 被忽略，
脚本**真的开跑**了一个重复批次。它做两件事：

1. 把已固化的 `c3\lowmem` 基线**覆盖成 3 条** —— runner 是**逐例增量写**
   `records.json` 的，跑几秒就把 40 条截断了；
2. 与正在跑的语料批次**并发** ⇒ 污染 60 秒墙钟的测量。

**修法**（`selfcheck\_tmp-notes\lowmem_batch.py`）：

- 改用 `argparse` ⇒ `--help` 正常，**不会**误触发执行；
- **输出目录里已有批次结果（`records.json` / `acceptance-summary.csv`）⇒ 拒绝写入**，
  确需覆盖必须显式 `--force` ⇒ 让"覆盖"变成一个**有意的动作**。

**通用规则**：可测量产物的默认输出目录要么是**一次性**的（带时间戳），
要么**自带"拒绝覆盖"护栏**；手写 argv 解析的脚本**必须自己实现 `--help`**。
另见「跑批窗口内不得并行跑其它重活」—— 这条纪律是有代价的，不是形式主义。

### 10. 一轮自检运行期间，不要改它要检查的文件

**现象**：2026-11-02 第 12 轮的 `tool-test-coverage` 步报了 FAIL
（"`ocr-tests\test_compare_corpus_retest.py` 没有被自检脚本引用"）——
那是**假红**：那一步读的是**磁盘上**的 `ocr-tests/`（已有新文件）与
`self_check.py`（还没登记），而编辑落盘比那一步的执行晚了几分钟。

**更危险的方向是假绿**：如果当时改的是**判据本身**（放宽阈值、删掉某个 FAIL 条件、
把「没查」变成「不报」），那一轮会**照样绿**，而汇总里**完全看不出来**。

**修法**（F58）：自检**开跑前**给「本轮要读到的输入」拍一张 sha256 指纹快照
（自检脚本自身 + `ocr-tests/**` + 产品树与仓库树的 `backend/scripts/tests/interface-sdk/docs`
+ `TODO-CONSOLIDATED.md` / `STATUS.md`），**收尾时**再算一遍；
对不上 ⇒ 本轮结论不可信 ⇒ **判 FAIL**，并逐个列出变动文件。

**操作纪律**：一轮跑着的时候，**只做分析与写报告**，别改输入树里的文件；
要改就在两轮之间改（或改完等下一轮验证）。

---

## 五、CI 接法

### 通用（任何 CI）

```powershell
pwsh -NoProfile -File scripts\verify_all.ps1 -Json > gate-summary.json
if ($LASTEXITCODE -ne 0) { Get-Content gate-summary.json; exit 1 }
```

- 只有 `verify_all.ps1` 的**退出码**决定成败，摘要里的 `gates[].status` 用于发通知；
- 日志与摘要落在 `_work\gate-r1\run-*`，**建议作为构建产物归档**（出问题第一时间要看 `logs\*.log`）；
- 需要交付包校验的流水线：先 `scripts\package_backend.ps1`，再
  `verify_all.ps1 -IncludeDelivery -DeliveryPackageDir <刚打出来的包>`；
- 编译流水线：`verify_all.ps1 -IncludeCpp -QMake <qmake 路径>`（构建目录用 `-BuildDirectory` 指定，
  建议指向 `D:\AP\AP7000\AP7000-Build-Directories\OcrTableTool\<分支>-<日期>` 而不是仓库内部）。

### TeamCity 要点

| 项 | 设置 |
|---|---|
| Build step | `pwsh`，脚本文件 `scripts\verify_all.ps1`，参数 `-Json -OutDir %teamcity.build.checkoutDir%\..\_work` |
| 退出码 | 由 PowerShell 返回（0/1），不用再解析日志 |
| 环境 | `QT_QPA_PLATFORM=offscreen`；`PATH` 含 Qt bin 与 venv `Scripts`；`TMPDIR=%TEMP%`（**不要**指向检出目录） |
| 产物 | `_work\**\logs\*.log`、`_work\**\summary.json` |
| 快速失败 | 先用 `-Json` 只跑默认那 15 步 + 步骤清单核对（共 16 条结果，约 3.5~4 分钟，含后端单测），交付/编译放独立 Build step |

### 推荐的分层

1. **提交前（3.5~4 分钟）**：`verify_all.ps1`（15 步：静态门禁 + 单测 + 契约，外加一条 `gate-coverage` 核对）；
2. **合并前（含 C++）**：`-IncludeCpp`；
3. **交付前（含包）**：`-IncludeDelivery`（要求包已打出）。

---

## 六、故障排查对照

| 现象 | 最可能原因 | 处置 |
|---|---|---|
| `error C2001` / `C1057` / 中文变乱码 | 缺 UTF-8 BOM | `gate_checks.py bom`，补 BOM |
| 全量 discover 多出 4 个假 ERROR，单跑却绿 | 测试硬编码了产品树路径 | `gate_checks.py identity`，改 `parents[1]/[2]` 定位 |
| `mirror` 报一堆"内容不同"但 `git diff` 是空的 | 只差行尾（CRLF vs LF） | 正常情况下归一化后应为一致；若仍报，是真实内容差异 |
| `mirror` 报"镜像缺文件"含刚新增的脚本 | 新增文件没同步到镜像 | 人工 `Copy-Item` 到产品树对应路径 |
| 后端单测在门禁里红、手工跑绿 | `TMPDIR` 落在产品树内 | 指到 `%TEMP%` 下的独立目录 |
| 中文断言消息乱码 | 控制台代码页 / 编码 | `PYTHONIOENCODING=utf-8`、`chcp 65001` |
| GUI 测试 abort / `could not find or load the Qt platform plugin` | 缺 `QT_QPA_PLATFORM=offscreen` 或 PATH 缺 Qt bin | 按第二节配环境 |
| `permission denied` 写 `_work\` / `__pycache__` | 目录权限 / 只读挂载 | 把 `-OutDir` 指到可写目录；用 `-B` 禁止写 `__pycache__` |
| `delivery-notes` FAIL「已过期」 | 包重建后没重算交付说明 | `delivery_notes.py render --package-dir <包>` |
| `delivery-verify` FAIL「backend sources not found」 | 没给 `--backend-source` 且包里也没有源码树 | 传 `--backend-source .\backend`（门禁已默认传仓库 backend） |
| `photo-acceptance` FAIL「真实照片语料不存在」 | 没建语料，或语料不在默认位置 | 先跑 `build_photo_corpus.py`，或用 `-PhotoCorpusDir` 指到语料目录 |
| `photo-acceptance` FAIL「逐格准确率低于阈值」 | 真实照片上的读法还没修到位 | 看 `<OutDir>\photo-gate\photo-gate-report.json` 的 `records[].details`（逐格列了真值 / 识别值 / 有没有标黄） |
| `photo-acceptance` FAIL「有 N 个错值格没有被标黄」 | 静默瞎编 —— 读错了却没标 `needs_review` | 查 `ocr_backend._ruled_cell_reading_needs_confirmation`（多模型分歧兜底）有没有被摘掉 |
