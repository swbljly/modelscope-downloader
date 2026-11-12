# 发布单文件 exe（GitHub Release 操作指引）

仓库**不含** `dist/ModelScope下载器.exe`（34,288,975 字节 / 32.70 MB）。二进制通过 GitHub Release 分发，
源码仓库只保留文本，clone 与 CI 都保持轻量。exe 可用 `pack/build.py` 一键重建。

> 当前分发版本：**v1.0.1**（修复文件清单勾选未生效）。若仓库尚未发布过任何 Release，也可直接用 `v1.0.0`。

## 一、网页创建 Release（约 1 分钟）

1. 打开仓库主页 → 右侧 **Releases** → **Draft a new release**（或 **Create a new release**）。
2. **Choose a tag**：输入 `v1.0.1`，选择 **Create new tag on publish**；**Target** 选 `main`。
3. **Release title**：`ModelScope 下载器 v1.0.1（单文件 exe）`。
4. 把下面「Release 说明」整段复制进描述框。
5. 在 **Attach binaries by dropping them here or selecting them** 区域，
   把本地 `dist/ModelScope下载器.exe` 拖进去（或点选），等待上传完成。
6. 点 **Publish release**。仓库为私有，Release 与附件也仅自己 / 受邀协作者可见。

> 若想让他人直接下载，需把仓库改为公开，或改用其他分发渠道。

## 二、Release 说明（可直接复制）

````markdown
## ModelScope 大模型下载器 · 单文件版 v1.0.1

双击即用，无需安装 Python、无需 pip、无需任何依赖；内置 Python 3.14.6、tkinter/Tcl-Tk 8.6、
modelscope 1.40.2 + modelscope-hub 0.6.1 与 HTTPS 根证书。

**用法**：填写「模型库名称」（如 `Qwen/Qwen2.5-0.5B-Instruct`）+「保存到文件夹」→
【查看文件清单】→ 单击要下载的行（任意列即可切换 ☑/☐，下方实时显示「勾选的 N/M 个文件 · 合计 X」）→
【开始下载】。文件直接落在所选文件夹，支持断点续传 / 跳过已存在、
`allow_patterns` / `ignore_patterns` 规则、revision / 镜像 / 代理 / 并发配置。

**v1.0.1 修复**
- 修复「文件清单里只勾选 1 个文件，却把该模型全部文件都下载回来」。
  原因：勾选只有在额外开关「仅下载勾选文件」打开时才生效，而该开关默认关闭，
  传给 SDK 的 `allow_patterns` 为空（空列表 = 不过滤 = 下载全部）。
- 现在勾选**直接决定下载范围**：部分勾选只下勾选项，全选回退到规则模式，0 勾选直接拦下并提示。
- 单击行的任意列即可切换勾选，不再出现「只是高亮选中、实际仍是全选」的误解。
- 进度分母改为勾选文件字节数之和（此前恒为整模型总大小，只下小文件时进度几乎不动）。
- 对 `[ ] * ?` 等 `fnmatch` 元字符做字符类转义，避免勾选 `model[1].onnx` 时误伤 `model1.onnx`。

**v1.0.0 修复（已包含）**
- 修复无黑框 exe 双击下载时 `AttributeError: 'NoneType' object has no attribute 'write'`
  （windowed 模式六个标准流为 None，SDK 的 tqdm 进度条写入失败）。
- 修复【停止 / 取消】无反应、任务停不下来（SDK 进度回调注入取消信号，正确结束并复位按钮状态）。

**校验**
| 项 | 值 |
| --- | --- |
| 文件 | `ModelScope下载器.exe` |
| 大小 | 34,288,975 字节（32.70 MB） |
| MD5 | `8295e555c92cce8a5becdd25529c5e37` |
| SHA256 | `97bcb53c50ac2d3aa1740c22d4f896b6dab716d30d9130ddb5610eb925d9431a` |

**自检**（在 exe 同目录用命令行执行，会生成报告文件）
```
ModelScope下载器.exe --self-check        # 运行时自检
ModelScope下载器.exe --smoke-select      # 勾选下载范围 + 真实下载 1 个小文件
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
pack/.venv/Scripts/python.exe -m unittest discover -s tests   # 173 个用例
pack/.venv/Scripts/python.exe pack/verify_bugfix.py   # 无标准句柄（等价双击）验证 exe
pack/.venv/Scripts/python.exe pack/verify_select.py   # 勾选下载范围端到端验收
```

> 注意：`tests/test_bugfix.py` 的 `test_a01` 固化了交付文件的 md5 / 行数作快照护栏，
> 修改 `README.md`、`dist/使用说明.txt` 等文件后需同步刷新 `EXPECTED_MD5` / `EXPECTED_LINES`；
> `test_a03` 固定了 exe 体积。
