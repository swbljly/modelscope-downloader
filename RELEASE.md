# 发布单文件 exe（GitHub Release 操作指引）

仓库**不含** `dist/ModelScope下载器.exe`（34,272,805 字节 / 32.69 MB）。二进制通过 GitHub Release 分发，
源码仓库只保留文本，clone 与 CI 都保持轻量。exe 可用 `pack/build.py` 一键重建。

## 一、网页创建 Release（约 1 分钟）

1. 打开仓库主页 → 右侧 **Releases** → **Draft a new release**（或 **Create a new release**）。
2. **Choose a tag**：输入 `v1.0.0`，选择 **Create new tag on publish**；**Target** 选 `main`。
3. **Release title**：`ModelScope 下载器 v1.0.0（单文件 exe）`。
4. 把下面「Release 说明」整段复制进描述框。
5. 在 **Attach binaries by dropping them here or selecting them** 区域，
   把本地 `dist/ModelScope下载器.exe` 拖进去（或点选），等待上传完成。
6. 点 **Publish release**。仓库为私有，Release 与附件也仅自己 / 受邀协作者可见。

> 若想让他人直接下载，需把仓库改为公开，或改用其他分发渠道。

## 二、Release 说明（可直接复制）

````markdown
## ModelScope 大模型下载器 · 单文件版 v1.0.0

双击即用，无需安装 Python、无需 pip、无需任何依赖；内置 Python 3.14.6、tkinter/Tcl-Tk 8.6、
modelscope 1.40.2 + modelscope-hub 0.6.1 与 HTTPS 根证书。

**用法**：填写「模型库名称」（如 `Qwen/Qwen2.5-0.5B-Instruct`）+「保存到文件夹」→
【查看文件清单】确认 →【开始下载】。文件直接落在所选文件夹，支持断点续传 / 跳过已存在、
`allow_patterns` / `ignore_patterns` 只下指定文件、revision / 镜像 / 代理 / 并发配置。

**v1.0.0 修复**
- 修复无黑框 exe 双击下载时 `AttributeError: 'NoneType' object has no attribute 'write'`
  （windowed 模式六个标准流为 None，SDK 的 tqdm 进度条写入失败）。
- 修复【停止 / 取消】无反应、任务停不下来（SDK 进度回调注入取消信号，正确结束并复位按钮状态）。

**校验**
| 项 | 值 |
| --- | --- |
| 文件 | `ModelScope下载器.exe` |
| 大小 | 34,272,805 字节（32.69 MB） |
| MD5 | `f90efbd0cb9479e03230014c1688b7c5` |
| SHA256 | `cb3dabbb66d63373f510fc6ebaf35895b4734690540a6bbabe4032d1cd51b944` |

**自检**（在 exe 同目录用命令行执行，会生成报告文件）
```
ModelScope下载器.exe --self-check        # 运行时自检 8 项
ModelScope下载器.exe --smoke-progress    # 启用进度回调的真实下载冒烟
ModelScope下载器.exe --smoke-cancel      # 停止 / 取消控制流
```

首次启动需解压，约 3~8 秒；exe 未做数字签名，个别安全软件可能误报。
模型文件本身仍需联网下载，本程序只是下载器。
````

## 三、本地重新构建 exe

```bash
# 需 Python 3.14+，在项目根目录执行（隔离 venv，不污染全局 Python）
python -m venv pack/.venv
pack/.venv/Scripts/python.exe -m pip install -r requirements.txt
pack/.venv/Scripts/python.exe -m pip install "pyinstaller==6.21.0"
pack/.venv/Scripts/python.exe pack/build.py
```

产物在 `dist/ModelScope下载器.exe`。`pack/app.ico` 由 `build.py` 自动生成，故未入库。

## 四、验证

```bash
python -m unittest discover -s tests -v       # 118 个用例
pack/.venv/Scripts/python.exe pack/verify_bugfix.py   # 无标准句柄（等价双击）验证 exe
```

> 注意：`tests/test_bugfix.py` 的 `test_a01` 固化了交付文件的 md5 / 行数作快照护栏，
> 修改 `README.md`、`dist/使用说明.txt` 等文件后需同步刷新 `EXPECTED_MD5` / `EXPECTED_LINES`。
