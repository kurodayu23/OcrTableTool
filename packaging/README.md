# packaging —— 交付包内容的版本库源头

这个目录放的是**会原样发给客户的文件**，不是构建工具。构建工具在 `scripts\`。

## 为什么放 `packaging\` 而不是 `scripts\assets\`

1. **性质不同**：`scripts\` 里的东西是"给维护者跑的工具"（构建、打包、验收），
   这里的两个文件是"客户拿到手会双击/阅读的交付物"。混在 `scripts\` 里，
   评审时看不出哪个文件会离开本机、进入交付包。
2. **归属清晰**：`selfcheck.bat` 和 `交付说明.md` 的改动要按**对外口径**审（措辞、
   客户可见的路径、许可披露），和改一个内部脚本不是同一类改动。
3. **打包脚本直接消费**：`scripts\package_backend.ps1` 从这里取文件落到交付包根目录，
   路径一眼能对上：`packaging\delivery\* → <交付包>\*`。

## 目录内容

| 文件 | 说明 | 交付包落点 |
|---|---|---|
| `delivery\selfcheck.bat` | 客户双击的一键自检（`chcp 65001` + 找 Python + 调 `customer_selfcheck.py`） | `<包根>\selfcheck.bat` |
| `delivery\交付说明.md.tmpl` | 交付说明模板，随包数字用占位符 | `<包根>\交付说明.md`（渲染后） |

`customer_selfcheck.py` **不在这里**——它本来就是版本库里的
`scripts\customer_selfcheck.py`，打包时直接复制，不再另存一份，避免两处漂移。

## 哪些数字必须重算，哪些不许自动改

渲染器：`scripts\delivery_notes.py`（纯标准库）。

**必须每次重算**（全部按交付目录实际遍历得出）：

```
EXE_BYTES / EXE_MIB          OcrBackend.exe 字节数 / MiB
PACKAGE_FILES / PACKAGE_MIB  整包文件数 / 体积
RUNTIME_FILES / RUNTIME_BYTES / RUNTIME_MIB
                             ocr-runtime\OcrBackend 子树（"运行时本体"口径）
DELTA_KB                     整包 - 运行时本体（KB）
EXTRA_FILES                  包内非运行时文件数（= PACKAGE_FILES - RUNTIME_FILES）
ROOT_DELIVERY_FILES          包根目录下的文件数
LICENSE_FILES                licenses\ 下的文件数
RUNTIME_MAX_MTIME            运行时子树里最新的修改时间
MEASURED_AT                  清点时间（渲染时写入；校验时从文件里回读，不参与比对）
```

**不许自动重算**（脚本故意不碰，改它们必须人工判断并在提交信息里说明）：

- 准确率 / 耗时 / 内存等**历史测量记录**：它们绑定某一次特定语料运行和口径，
  跟着包重建自动变化就等于伪造记录；
- 构建指纹（例如 `DCF04B22A473…` 这种"本交付件是哪个后端构建"的标记）；
- `licenses\` 内部分类明细（57 份里 45 份对应实体 / 2 份署名声明 / 10 份保守冗余）；
- 目录树示意、运行环境要求、已知缺陷披露等叙述性内容。

⚠ **`licenses\` 的分类明细不会自动更新**：如果第三方许可文件增减，
`LICENSE_FILES` 会自动跟着变，但"57 份里 45/2/10"这类明细需要人工核对。

## 命令

```powershell
# 渲染（打包脚本已自动调用；licenses\ 补入后必须重跑一次）
python -B scripts\delivery_notes.py render --package-dir D:\...\package\backend-delivery

# 校验：交付位里的 交付说明.md 是否由模板 + 实际目录算出来（CI 用）
python -B scripts\delivery_notes.py verify --package-dir D:\...\package\backend-delivery

# 排查：只打印重算出来的 12 个数字
python -B scripts\delivery_notes.py facts --package-dir D:\...\package\backend-delivery
```

渲染是**幂等**的：`交付说明.md` 自己也计入"整包文件数 / 体积"，写出后自身大小变化
可能让某个数字跨过 0.1 MiB 的取整边界，所以渲染器会反复算到稳定（通常 2 轮），
`render` 后紧跟 `verify` 不会抖成"永远过期"。
