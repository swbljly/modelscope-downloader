# ModelScope 模型下载器（Windows 桌面版）

一个基于 Python 标准库 `tkinter` 的 Windows 桌面小工具：**只输入模型库名称、选择保存文件夹，就能把 ModelScope 上的大模型直接下载到指定目录**。无需命令行、无需记参数、无需额外 GUI 框架。

- 界面、日志、提示全部简体中文
- 文件**直接落在你选的文件夹**，不是 `~/.cache/modelscope` 里的软链接
- 断点续传 / 跳过已存在文件
- 支持只下指定文件、指定子目录、指定量化版本
- 支持版本 / 分支、镜像站点、代理、并发线程数
- 下载前可预览文件清单（文件名、大小、总大小、文件数），清单可不装 SDK 也能查

> **单文件版**：`dist/ModelScope下载器.exe` 已内置解释器、SDK、Tk 与根证书，
> 双击即用，不需要 Python、不需要 `pip install`。本文的「环境要求 / 安装 /
> 启动」三节针对的是**源码版**；单文件版请直接看 `dist/使用说明.txt`。

---

## 一、环境要求

| 项目 | 要求 |
| --- | --- |
| 操作系统 | Windows 10 / 11 |
| Python | 3.8 或更高版本，安装时**务必勾选 `Add Python to PATH`**（仅源码版需要） |
| 网络 | 能访问 `https://www.modelscope.cn`（或你配置的镜像 / 代理） |
| 磁盘 | 目标文件夹所在盘剩余空间 ≥ 模型总大小，建议多留 20% |

Python 自带 `tkinter`，绝大多数官方安装包已包含；若启动时提示缺少 tkinter，请重新运行 Python 安装程序并勾选 **tcl/tk and IDLE** 组件。**便携版 / 嵌入式 Python 通常不带 tkinter**，不建议用于本工具。

如果电脑上装了多个 Python，`启动.bat` 与 `安装依赖.bat` 会优先从 `where pythonw` 的候选中挑出带 `tkinter` 的解释器（找不到时再遍历 `where python`），并且**两个脚本使用同一个解释器**，避免「依赖装到了 A、程序却跑在 B」的情况。

---

## 二、安装（只需一次）

1. 确认 Python 可用：打开「命令提示符」，输入 `python --version`，能看到版本号即可。
2. 在本文件夹空白处按住 `Shift` + 右键 →「在此处打开 PowerShell 窗口」，或在地址栏输入 `cmd` 回车，然后执行：

   ```bat
   pip install -r requirements.txt
   ```

3. **更省事的方法**：直接双击本目录下的 **`安装依赖.bat`**，它会自动升级 pip 并安装依赖，完成后按任意键关闭。

> 国内网络较慢时可用镜像：
> `pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple`

---

## 三、启动

双击 **`启动.bat`** 即可。该脚本按以下顺序启动（**不使用 `py -3`**，本机无此命令）：

1. `cd /d "%~dp0"` 切到程序所在目录，保证相对路径正确；
2. `where pythonw` → 逐个校验候选项**同目录的 `python.exe` 能否 `import tkinter`**，用第一个可用的 `pythonw.exe gui_app.py` 启动（**没有黑色控制台窗口**）；
3. 找不到可用的 `pythonw` → 回退 `where python`，同样挑出带 `tkinter` 的解释器，用 `python gui_app.py` 启动（带控制台，便于查看报错）；
4. 两者都找不到 → 提示安装 Python 3.8+ 并给出 `https://www.python.org/downloads/` 下载地址。

> **本机实测环境**：`python` 为 3.13.14，但 PATH 中排第一的是便携版 Python，**不含 `tkinter`**；PATH 中另有 Python 3.14.6 带 `tkinter`。脚本会自动跳过便携版、选中 3.14.6 的 `pythonw.exe`。
> 因此请**务必双击 `启动.bat`**，不要直接执行 `python gui_app.py`（默认解释器缺 `tkinter` 会报 `ModuleNotFoundError`）。
> 同理，安装依赖请双击 `安装依赖.bat`，它使用与启动脚本**完全相同**的解释器选择逻辑，确保 SDK 装到真正运行 GUI 的那个 Python 里。

如果双击后没有任何窗口出现，请在本目录查看是否生成了 `启动错误.log`，或在命令行用带 `tkinter` 的解释器运行 `gui_app.py` 查看报错。

---

## 四、最快上手（两步）

1. 在「模型库名称」填入 `Qwen/Qwen2.5-7B-Instruct`（GGUF 例子：`qwen/Qwen2.5-7B-Instruct-GGUF`）。
2. 点「浏览...」选一个保存文件夹（例如 `D:\models`），然后点 **【开始下载】**。

建议第一次先点 **【查看文件清单】**，确认文件列表与总大小无误再下载。

---

## 五、界面字段说明

| 界面字段 | 是否必填 | 说明 | 默认值 |
| --- | --- | --- | --- |
| 模型库名称 | ✅ | `owner/name` 形式，如 `Qwen/Qwen2.5-7B-Instruct`；可直接粘贴模型主页链接 | 空 |
| 保存到文件夹 | ✅ | 模型文件直接保存到这里；目录不存在会自动创建；支持空格与中文路径 | `用户目录/ModelScopeDownloads` |
| 查看文件清单 | — | 下载前预览文件名、大小、总大小与文件数；**不依赖 modelscope SDK** | — |
| 开始下载 | — | 启动后台下载线程 | — |
| 停止 / 取消 | — | 置位取消事件；SDK 进度回调会在**每个数据块**检查并抛出 `DownloadCancelled` 真实中断下载循环，任务结束后状态机复位 | 空闲时禁用 |
| 版本 / 分支 | — | `revision`，可填分支名、标签或 commit id | `master` |
| 镜像 / API 站点 | — | 模型站点 / API 根地址，企业内网可填自建镜像 | 留空＝官方 `https://www.modelscope.cn` |
| HTTP(S) 代理 | — | 如 `http://127.0.0.1:7890` | 留空＝沿用系统代理 |
| 并发线程数 | — | `max_workers`，网络好可调大，磁盘慢或易断流建议调小 | `4` |
| 仅下载子目录 | — | 便捷输入，如 `onnx` 或 `onnx/q4`，自动展开为 `onnx/*`、`onnx/**` | 空 |
| 包含规则 | — | `allow_patterns`，多个用逗号分隔，如 `*.safetensors, *.json` | 空＝全部 |
| 排除规则 | — | `ignore_patterns`，多个用逗号分隔，如 `*.md, *.txt, *.png` | 空 |
| 仅下载文件清单中勾选的文件 | — | 勾选后只下载 Treeview 中打勾的文件 | 关闭 |
| 文件清单 Treeview | — | 单击「选择」列或选中后按空格切换勾选；可全选 / 全不选 / 反选 | 默认全勾选 |
| 进度条 + 日志区 | — | 百分比 / 字节数、带时间戳的滚动日志 | — |

所有「高级设置」里的配置都会在退出程序时自动保存到
`%APPDATA%\ModelScopeDownloader\config.json`，下次启动自动回填；配置文件损坏时会自动回退默认值，不会导致程序崩溃。

---

## 六、常见问题

### 1. 断点续传是怎么实现的？中断后要重新下载吗？

不需要。**SDK 自带断点续传**：`snapshot_download` 会检查目标目录中已存在文件的大小 / 哈希，一致则直接跳过，未完成的会继续传输。网络中断、手动点「停止 / 取消」、甚至直接关掉窗口之后，只要再次点击【开始下载】，就会从断点继续。

- 回退路径（老版本 SDK）下，程序下载到缓存后会用 `sync_cache_to_target()` 同步到目标文件夹，同样**跳过已存在且大小一致的文件**，并逐块检查取消；未完成的临时文件以 `.part` 结尾，不会被误判为已完成。
- 注意：如果中途修改了「包含规则 / 排除规则」，已下载但不在新规则内的文件不会被删除，只是不再参与下载。

### 2. 只想下 GGUF / 某个量化版本 / 某几类文件怎么做？

按需选用以下任一方式（可组合）：

| 目标 | 做法 |
| --- | --- |
| 只下 GGUF | 模型名填 `qwen/Qwen2.5-7B-Instruct-GGUF`，「包含规则」填 `*.gguf` |
| 只下某个量化 | 先点【查看文件清单】，在清单里勾选需要的那个 `.gguf`，勾上「仅下载文件清单中勾选的文件」 |
| 只下权重和配置 | 「包含规则」填 `*.safetensors, config.json, tokenizer*` |
| 只下某个子目录 | 「仅下载子目录」填 `onnx`（会自动变成 `onnx/*`） |
| 排除文档和图片 | 「排除规则」填 `*.md, *.png, *.jpg, *.txt` |

规则支持 `*` 通配，多个规则用逗号分隔，空白项会自动忽略；`onnx/*` 这类路径规则请用正斜杠 `/`。

### 3. 代理和镜像怎么配？

- **代理**：在「HTTP(S) 代理」填 `http://127.0.0.1:7890`（按你本机代理软件的实际端口）。留空时沿用系统代理环境变量。
- **镜像 / API 站点**：填站点根地址，例如 `https://www.modelscope.cn`，或内网镜像 `https://modelscope.example.com`；也支持填到 `/api/v1` 级别。程序会同时写入 `MODELSCOPE_ENDPOINT` 环境变量并尝试传给 SDK。
- 文件清单预览也会用同一套镜像与代理设置。

### 4. 大文件下载要注意什么？

- 先点【查看文件清单】看总大小，确保目标盘剩余空间足够；建议多留 20% 余量（SDK 下载时可能产生临时文件）。
- 下载过程中进度条若显示「总大小未知」并左右滚动，属于正常的降级显示：可先查一次清单再下载，即可得到精确百分比。
- 进度数值来自「目标目录已落盘字节数」的定时统计，可能比网络层进度略有延迟，但不会让界面卡死。
- 尽量避免在下载中移动 / 删除目标文件夹。

### 5. 提示 modelscope 版本过低 / 不支持 `local_dir` 怎么办？

程序会用 `inspect.signature()` 静态探测 + 运行时 `try/except TypeError` 双重判断：

- 支持 `local_dir`（推荐）：文件**直接**写入你选的文件夹；
- 不支持：自动回退到 `cache_dir` 下载，完成后把文件**复制 / 同步**到目标文件夹（保留目录结构，跳过已存在且大小一致的文件），日志里会明确写出「下载策略：cache_dir」。

功能不会因此失效，但会多占一份缓存空间、多一次复制。建议升级：

```bat
pip install -U modelscope
```

升级后重启程序，日志会显示「下载策略：local_dir」。

### 6. 没装 modelscope 时还能做什么？

- **文件清单预览完全可用**：走纯标准库 `urllib` 调用 ModelScope 公开 REST 接口
  `https://www.modelscope.cn/api/v1/models/{model_id}/repo/files?Revision=master&Recursive=True`，
  解析 `Data.Files` 中的 `Path` / `Size` / `Type` / `IsLFS`，并对字段名做了大小写兼容。
- 点击【开始下载】会弹出清晰的安装指引（`pip install -r requirements.txt`）而不执行下载，不会抛出 traceback；状态栏与日志区也会同时给出提示。

### 7. 点「停止 / 取消」后为什么不是立刻停？

取消事件会在下载循环、进度轮询和回退复制的**每一个检查点**生效。此外（BugFix 之后）`ms_downloader_core` 构造的 SDK 进度回调会在**每个数据块**检查取消事件并抛出 `DownloadCancelled`，从而**真实中断** `modelscope_hub` 的下载循环 —— SDK 本身不接受外部取消事件，进度回调是唯一能触及 SDK 内部的注入点。SDK 抛出的异常在取消已置位时会被统一转译为 `DownloadCancelled`，界面因此显示「已停止（可续传）」并复位按钮状态机，而不是误报「任务失败」。

限制：若 SDK 正卡在**长时间没有数据块到达**的网络等待上，取消要等下一个数据块或网络超时才生效（无法强杀阻塞中的 socket 读）。界面点击后会立即写出「已请求停止」日志、更新状态并禁用停止按钮，任务结束时自动恢复；**不会伪造「已停止」状态**。已完成内容保留，下次点击【开始下载】继续，不会重复传输。

### 8. 单文件 exe 双击后下载立刻报 `'NoneType' object has no attribute 'write'`？

这是旧版 exe 的已知缺陷：PyInstaller `--windowed` 由资源管理器双击启动时，`sys.stdout` / `sys.stderr`（含 `sys.__stderr__` 等）全为 `None`，`tqdm` 首次刷新进度条时会写 `None.write` 而崩溃。`pack/entry.py` 现在会在导入 modelscope / tkinter **之前**把六个标准流统一换成指向 `os.devnull` 的安全 `TextIOWrapper`。请改用新构建的 `dist/ModelScope下载器.exe` 覆盖旧文件。

---

## 七、文件清单

| 文件 | 职责 |
| --- | --- |
| `gui_app.py` | tkinter 界面与程序入口 `main()`：输入区、高级设置、文件清单 Treeview、进度条、日志区、后台线程与按钮状态机 |
| `ms_downloader_core.py` | 纯逻辑层（**不 import tkinter**，可独立单测）：数据结构、解析与校验、SDK/REST 双路径清单查询、下载参数兼容探测、断点续传与回退复制、进度上报与取消注入、配置持久化 |
| `启动.bat` | 双击启动：检查 Python / tkinter，优先 `pythonw` 无黑框启动，失败回退 `python` |
| `安装依赖.bat` | 一键安装 / 升级 `requirements.txt` 中的依赖 |
| `requirements.txt` | 依赖声明（`modelscope>=1.18.0`、`requests>=2.25.0`） |
| `README.md` | 本文档 |

单文件版（免 Python 环境）相关工程文件：

| 文件 | 职责 |
| --- | --- |
| `dist/ModelScope下载器.exe` | 交付成品：PyInstaller 6.21.0，onefile + windowed |
| `dist/使用说明.txt` | 单文件版使用说明 |
| `pack/entry.py` | exe 入口：windowed 标准流兜底、`--self-check` / `--smoke-download` / `--smoke-progress` / `--smoke-cancel` |
| `pack/build.py` / `pack/downloader.spec` | 一键构建脚本与 PyInstaller 配置 |
| `pack/verify_exe.py` | 基础验证（**注意**：它给子进程传了有效标准句柄，测不到双击场景） |
| `pack/verify_bugfix.py` | BugFix 验收：以「无标准句柄（等价用户双击）」方式实测新 exe |
| `pack/repro_bug_a.py` | Bug A 复现脚本：把标准流置 `None` 后走完整 GUI 下载链，支持 `--with-guard` 修复前后 A/B 对照 |
| `pack/repro_smoke_exact.py` | 严格参数对照：完全复刻 `--smoke-download` 的调用参数，在标准流为 `None` 时同样复现崩溃，排除「小文件 / 单线程 / apply_env」等参数差异 |
| `pack/probe_streams.py` / `pack/probe_stdio.py` | 标准流状态诊断（解释上一轮 smoke 为何漏测） |

---

## 八、技术实现要点

- **线程模型**：清单查询与下载均运行在 daemon 后台线程；后台线程**绝不直接触碰 Tcl**，只把界面任务放入线程安全 `queue.Queue`，由主线程的 `root.after` 定时泵（40ms）统一排空执行。这样避免了 tkinter 跨线程 `root.after` 抛 `RuntimeError: main thread is not in main loop` 导致日志 / 进度 / 文件清单被静默吞掉；`threading.Event` 负责取消；按钮状态机防止重复启动多个任务。点击停止时主线程立即排空一次队列，保证反馈即时可见。
- **进度降级与取消注入**：优先尝试 SDK 的 `progress_callbacks`（兼容失败时静默放弃），字节级进度始终由目标目录定时扫描提供；该回调同时承担取消检查，是每个数据块中断 SDK 下载的唯一入口。总大小未知时切换 `indeterminate` 滚动模式，并配「已用时」计时器保证界面有活动反馈。
- **单文件 exe 标准流兜底**：`pack/entry.py` 在模块级最早处调用 `_install_stdio_guard()`，把 windowed 下为 `None` 的六个标准流换成 `os.devnull` 上的 `TextIOWrapper`（具备 `write` / `flush` / `isatty` / `fileno` 等完整接口）。源码模式（有真实控制台）三个流都有效时直接返回、不改动任何 `sys` 属性。
- **配置持久化**：`%APPDATA%/ModelScopeDownloader/config.json`，原子写入（临时文件 + `os.replace`），读取异常一律回退默认值。
- **异常分层**：`DownloaderError` → `ValidationError` / `DependencyError` / `RepoQueryError` / `DownloadError` / `DownloadCancelled`，界面按类型给出不同提示。
- **路径处理**：统一使用 `pathlib.Path`，支持空格、中文与相对 / 绝对路径；不写死任何绝对路径。

---

## 九、已知限制

- 源码版首次使用必须手动安装一次依赖（双击 `安装依赖.bat`）；单文件 exe 版不需要。
- SDK 长时间无数据块到达时（网络等待），取消需等下一个数据块 / 网络超时才生效；正常传输中由进度回调在下一个数据块中断，通常是秒级。不会为了「看起来已停止」而伪造状态。
- 「仅下载勾选文件」需要先点一次【查看文件清单】才有可勾选条目。
- 公开 REST 接口在极少数私有 / 需登录的仓库上可能返回空清单；这类仓库建议安装 SDK 后由 `HubApi` 查询。
- 程序不会删除目标目录中已存在但已不在本次下载规则内的旧文件。
- 首次查询文件清单时，本机 DNS / 系统代理解析偶尔会额外耗时数秒（实测 0.4~10 秒波动）；查询在后台线程进行，界面保持可响应，状态栏与「已用时」计时器会持续反馈，不会卡死。
- 粘贴带 `?revision=xxx` 或 `#锚点` 的 ModelScope 模型主页链接时，`normalize_model_id` 会**静默截断** `?` / `#` 及其后内容，只保留 `owner/name`（这是有意为之：避免把查询参数当成模型名的一部分）。如需指定版本 / 分支，请在「版本 / 分支」输入框中填写 `revision`，而不是拼在链接里。
- 单文件 exe 未做数字签名，部分安全软件可能误报；放在 OneDrive 等云同步目录下时，个别文件可能受同步锁影响（与本次两个 Bug 无关）。
