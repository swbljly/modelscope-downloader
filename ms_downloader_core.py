# -*- coding: utf-8 -*-
"""ModelScope 模型下载器 —— 纯逻辑层。

本模块只依赖 Python 标准库与（可选的）``modelscope`` SDK，**不导入
tkinter**，因此可以脱离图形界面被单元测试直接调用。所有与界面 / 日志系统的
交互都通过 ``log_cb`` / ``progress_cb`` 回调完成，便于在测试中 mock。

主要能力：
    * 模型仓库文件清单查询（SDK ``HubApi`` 优先，公开 REST 接口兜底）。
    * 下载参数组装与 ``snapshot_download`` 版本兼容探测（``local_dir``）。
    * 断点续传、``cache_dir`` 回退复制、取消事件与进度上报。
    * 配置持久化（``%APPDATA%/ModelScopeDownloader/config.json``）。

异常分层::

    DownloaderError
      ├── ValidationError      输入校验失败
      ├── DependencyError      modelscope SDK 缺失
      ├── RepoQueryError       文件清单查询失败（SDK 与 REST 双路径都失败）
      ├── DownloadError        下载 / 复制失败
      └── DownloadCancelled    用户主动取消
"""
from __future__ import annotations

import dataclasses
import importlib
import importlib.util
import inspect
import json
import math
import os
import re
import threading
import time
import urllib.parse
import urllib.request

# 以下标准库模块是 urllib 发起 HTTPS 请求时的惰性依赖。刻意在模块导入阶段
# （主线程）预热：若留给后台线程在首次 REST 请求时才导入，会与启动期的 SDK
# 探测线程争用解释器导入锁，本机实测会把首次「查看文件清单」从约 0.4 秒
# 拖慢到 10 秒以上。均为标准库，无额外依赖、无副作用。
import base64  # noqa: F401
import email  # noqa: F401
import email.parser  # noqa: F401
import hashlib  # noqa: F401
import http.client  # noqa: F401
import mimetypes  # noqa: F401
import ssl  # noqa: F401

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple, Union

__all__ = [
    "AppConfig",
    "DownloadCancelled",
    "DownloadError",
    "DownloadScope",
    "DownloaderError",
    "DependencyError",
    "ModelInfo",
    "RepoFile",
    "RepoQueryError",
    "SCOPE_ALL",
    "SCOPE_EMPTY",
    "SCOPE_SELECTED",
    "ValidationError",
    "apply_environment",
    "build_download_kwargs",
    "dir_size_bytes",
    "download_model",
    "escape_glob_pattern",
    "fetch_repo_files",
    "format_size",
    "get_config_path",
    "get_snapshot_download",
    "is_sdk_available",
    "load_config",
    "normalize_model_id",
    "parse_patterns",
    "plan_download_scope",
    "probe_local_dir_support",
    "resolve_patterns",
    "save_config",
    "sdk_is_installed",
    "sdk_version",
    "selection_to_allow_patterns",
    "sync_cache_to_target",
]


# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

DEFAULT_REVISION: str = "master"
DEFAULT_ENDPOINT: str = "https://www.modelscope.cn"
DEFAULT_MAX_WORKERS: int = 4
DEFAULT_TIMEOUT: float = 20.0
CONFIG_DIR_NAME: str = "ModelScopeDownloader"
CONFIG_FILE_NAME: str = "config.json"
INSTALL_COMMAND: str = "pip install -r requirements.txt"
USER_AGENT: str = "ModelScopeDownloader/1.0 (Windows; Python)"

#: SDK 版本差异较大、可以安全剔除后重试的可选关键字参数。
_OPTIONAL_KWARGS: Tuple[str, ...] = (
    "progress_callbacks",
    "endpoint",
    "max_workers",
    "user_agent",
    "repo_type",
    "token",
    "allow_file_pattern",
    "ignore_file_pattern",
)

#: 回退复制时需要跳过的 SDK 元数据目录名。
_SKIP_DIR_NAMES = frozenset({".cache", ".git", "__pycache__", "blobs", "locks", ".locks"})
#: 回退复制时需要跳过的临时文件后缀。
_SKIP_FILE_SUFFIXES: Tuple[str, ...] = (".lock", ".part", ".tmp", "._____temp")

ProgressReporter = Callable[[int, int, str], None]
LogReporter = Callable[[str], None]


# ---------------------------------------------------------------------------
# 异常分层
# ---------------------------------------------------------------------------


class DownloaderError(Exception):
    """下载器所有自定义异常的基类。"""


class ValidationError(DownloaderError):
    """用户输入不合法（模型名格式、目标目录等）。"""


class DependencyError(DownloaderError):
    """``modelscope`` SDK 未安装或不可用。"""


class RepoQueryError(DownloaderError):
    """文件清单查询失败（SDK 与 REST 两条路径均失败）。"""


class DownloadError(DownloaderError):
    """下载或回退复制过程中发生错误。"""


class DownloadCancelled(DownloaderError):
    """用户通过取消事件主动中止了任务。"""


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


@dataclass
class RepoFile:
    """模型仓库中的单个文件。

    Attributes:
        path: 相对仓库根目录的路径，统一使用 ``/`` 分隔。
        size: 文件字节数；接口未提供时为 0。
        name: 文件名；未显式提供时由 ``path`` 推导。
        file_type: 条目类型（``blob`` / ``tree`` 等）。
        is_lfs: 是否为 LFS 大文件。
        sha256: 文件哈希，可为空。
    """

    path: str
    size: int = 0
    name: str = ""
    file_type: str = "blob"
    is_lfs: bool = False
    sha256: str = ""

    def __post_init__(self) -> None:
        """补全文件名并规范化路径分隔符。"""
        self.path = str(self.path).replace("\\", "/")
        if not self.name:
            self.name = self.path.rsplit("/", 1)[-1]
        self.size = max(0, int(self.size or 0))


@dataclass
class ModelInfo:
    """一次文件清单查询的结果。

    Attributes:
        model_id: 规范化后的模型 ID（``owner/name``）。
        revision: 查询时使用的版本 / 分支。
        files: 文件列表（已按路径排序，不含纯目录条目）。
        source: 清单来源，``SDK`` 或 ``REST``。
    """

    model_id: str
    revision: str = DEFAULT_REVISION
    files: List[RepoFile] = field(default_factory=list)
    source: str = ""

    @property
    def total_size(self) -> int:
        """所有文件字节数之和。"""
        return sum(item.size for item in self.files)

    @property
    def file_count(self) -> int:
        """文件数量。"""
        return len(self.files)

    @property
    def paths(self) -> List[str]:
        """所有文件路径列表。"""
        return [item.path for item in self.files]


@dataclass
class AppConfig:
    """界面配置的持久化载体。

    所有字段均为界面可直接回填的简单类型；``load_config`` 在配置文件损坏时
    会返回全默认值的实例。

    ``only_selected`` 的历史与现状：
        * 早期版本用它决定「文件清单的勾选是否生效」，默认 ``False``，
          导致用户只勾 1 个文件却下载整模型（Bug：清单勾选未生效）。
        * 现在「勾选即下载范围」已固化为默认行为，界面不再暴露该开关。
          字段仍保留读写（旧配置文件不会报错 / 不崩），但取值恒为 ``True``；
          旧配置里的 ``False`` 会被忽略并在下次保存时自动迁移为 ``True``。
    """

    model_id: str = ""
    target_dir: str = ""
    revision: str = DEFAULT_REVISION
    endpoint: str = ""
    proxy: str = ""
    max_workers: int = DEFAULT_MAX_WORKERS
    subdir: str = ""
    allow_patterns: str = ""
    ignore_patterns: str = ""
    only_selected: bool = True


_CONFIG_FIELDS: List[str] = [item.name for item in dataclasses.fields(AppConfig)]


# ---------------------------------------------------------------------------
# 通用工具
# ---------------------------------------------------------------------------


def _noop(_message: str) -> None:
    """空日志回调。"""
    return None


def _make_logger(callback: Optional[LogReporter]) -> LogReporter:
    """把可选日志回调包装成永不抛异常的回调。

    Args:
        callback: 用户提供的日志回调，可为 ``None``。

    Returns:
        接受单个字符串的回调。
    """
    if callback is None:
        return _noop

    def _log(message: str) -> None:
        try:
            callback(str(message))
        except Exception:  # 界面已关闭等情况下不能影响下载线程
            pass

    return _log


def _make_progress(callback: Optional[ProgressReporter]) -> ProgressReporter:
    """把可选进度回调包装成永不抛异常的回调。

    Args:
        callback: 形如 ``(done_bytes, total_bytes, message) -> None`` 的回调。

    Returns:
        安全的进度回调。
    """
    if callback is None:
        return lambda *_args: None

    def _progress(done: int, total: int, message: str = "") -> None:
        try:
            callback(int(done), int(total), str(message))
        except Exception:
            pass

    return _progress


def _raise_if_cancelled(cancel_event: Optional[threading.Event]) -> None:
    """若取消事件已置位，则抛出 :class:`DownloadCancelled`。"""
    if cancel_event is not None and cancel_event.is_set():
        raise DownloadCancelled("操作已被用户取消")


def _pick(mapping: Any, names: Sequence[str]) -> Any:
    """大小写不敏感地从字典中取第一个非空值。

    Args:
        mapping: 待读取的字典（非字典时返回 ``None``）。
        names: 候选键名，按优先级排列。

    Returns:
        第一个值不为 ``None`` 的候选项，或一个都不存在时返回 ``None``。
    """
    if not isinstance(mapping, dict):
        return None
    lowered = {str(key).lower(): value for key, value in mapping.items()}
    for name in names:
        value = lowered.get(name.lower())
        if value is not None:
            return value
    return None


def _to_int(value: Any, default: int = 0) -> int:
    """宽松地把接口返回值转为非负整数。"""
    try:
        number = int(float(value))
    except (TypeError, ValueError, OverflowError):
        return default
    return number if number >= 0 else default


def format_size(num_bytes: Union[int, float, None]) -> str:
    """把字节数格式化为易读的中文单位字符串。

    Args:
        num_bytes: 字节数；``None`` / 负数 / 非法值返回 ``"未知"``。

    Returns:
        例如 ``"1.45 GB"``、``"0 B"``、``"未知"``。
    """
    try:
        size = float(num_bytes)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return "未知"
    # NaN / ±inf 不是有限字节数：int(nan) 会抛 ValueError，inf 会显示成 "inf PB"。
    if not math.isfinite(size):
        return "未知"
    if size < 0:
        return "未知"
    units = ("B", "KB", "MB", "GB", "TB", "PB")
    index = 0
    while size >= 1024.0 and index < len(units) - 1:
        size /= 1024.0
        index += 1
    if index == 0:
        return "{0} {1}".format(int(size), units[index])
    return "{0:.2f} {1}".format(size, units[index])


def normalize_model_id(raw: str) -> str:
    """规范化并校验模型库名称。

    支持用户直接粘贴 ModelScope 模型主页链接；强制要求 ``owner/name`` 两段式
    结构，并拒绝空白、路径穿越与 Windows 非法字符。

    Args:
        raw: 原始输入，例如 ``"Qwen/Qwen2.5-7B-Instruct"``。

    Returns:
        规范化后的模型 ID。

    Raises:
        ValidationError: 输入为空、段数不足或含非法字符。
    """
    if raw is None:
        raise ValidationError("请输入模型库名称，例如 Qwen/Qwen2.5-7B-Instruct")
    value = str(raw).strip().strip('"').strip("'").replace("\\", "/")
    if not value:
        raise ValidationError("请输入模型库名称，例如 Qwen/Qwen2.5-7B-Instruct")

    # 兼容粘贴 https://www.modelscope.cn/models/Qwen/Qwen2.5-7B-Instruct 之类的链接。
    value = re.sub(r"^https?://[^/]+/(?:models|model)/", "", value, flags=re.IGNORECASE)
    value = value.split("?")[0].split("#")[0].strip("/")
    if value.lower().endswith(".git"):
        value = value[:-4]

    parts = [part.strip() for part in value.split("/") if part.strip()]
    if len(parts) < 2:
        raise ValidationError(
            "模型库名称格式应为 owner/name，例如：\n"
            "  Qwen/Qwen2.5-7B-Instruct\n"
            "  qwen/Qwen2.5-7B-Instruct-GGUF\n"
            "（当前输入：{0!r}）".format(raw)
        )
    # 允许 owner/name/... 形式的深层链接，只取前两段作为模型 ID。
    parts = parts[:2]
    joined = "/".join(parts)
    illegal = (':', '*', '?', '"', '<', '>', '|')
    if ".." in joined or any(char in joined for char in illegal) or any(c.isspace() for c in joined):
        raise ValidationError(
            "模型库名称含非法字符或空白：{0!r}\n"
            "正确示例：Qwen/Qwen2.5-7B-Instruct".format(raw)
        )
    return joined


def parse_patterns(raw: Optional[str]) -> List[str]:
    """把用户在界面输入的过滤规则解析成去重后的规则列表。

    支持逗号、分号、换行三种分隔；自动去掉空白项与重复项；反斜杠统一成
    ``/``，避免 Windows 下 ``onnx\\*`` 之类的输入失效。

    Args:
        raw: 原始输入，例如 ``"*.safetensors, *.json"``。

    Returns:
        规则列表，输入为空时返回空列表。
    """
    if not raw:
        return []
    text = str(raw).replace("\n", ",").replace(";", ",")
    result: List[str] = []
    for item in text.split(","):
        cleaned = item.strip().strip('"').strip("'").replace("\\", "/").strip()
        if cleaned and cleaned not in result:
            result.append(cleaned)
    return result


def resolve_patterns(
    allow_raw: str = "",
    ignore_raw: str = "",
    subdir: str = "",
    selected_paths: Optional[Sequence[str]] = None,
) -> Tuple[List[str], List[str]]:
    """把界面上的多个过滤输入合并为 ``allow_patterns`` / ``ignore_patterns``。

    Args:
        allow_raw: 包含规则原始文本（逗号分隔）。
        ignore_raw: 排除规则原始文本（逗号分隔）。
        subdir: 「仅下载某个子目录」便捷输入，例如 ``onnx`` 或 ``onnx/q4``。
        selected_paths: 文件清单中被勾选的文件路径，作为精确包含规则追加。
            **注意**：本函数只做原样追加，不转义通配元字符；把勾选结果交给
            SDK 时应使用 :func:`selection_to_allow_patterns`（或直接用
            :func:`plan_download_scope`），否则 ``model[1].onnx`` 这类文件名
            会被当成字符类而匹配失败。

    Returns:
        ``(allow_patterns, ignore_patterns)`` 二元组。
    """
    allow = parse_patterns(allow_raw)
    ignore = parse_patterns(ignore_raw)
    sub = str(subdir or "").strip().replace("\\", "/").strip("/")
    if sub:
        for pattern in ("{0}/*".format(sub), "{0}/**".format(sub)):
            if pattern not in allow:
                allow.append(pattern)
    for path in selected_paths or []:
        normalized = str(path).replace("\\", "/").lstrip("/")
        if normalized and normalized not in allow:
            allow.append(normalized)
    return allow, ignore


# ---------------------------------------------------------------------------
# 下载范围：把「清单勾选」翻译成 SDK 的过滤规则
# ---------------------------------------------------------------------------

#: 下载范围的三种形态（:class:`DownloadScope` 的 ``mode`` 字段）。
SCOPE_ALL: str = "all"
SCOPE_SELECTED: str = "selected"
SCOPE_EMPTY: str = "empty"

#: ``fnmatch`` 的通配元字符 -> POSIX 字符类写法的转义映射。
#: ``[`` -> ``[[]``、``]`` -> ``[]]``、``*`` -> ``[*]``、``?`` -> ``[?]``，
#: 经 ``fnmatch.translate`` 后都只匹配字符本身（已实测验证）。
_GLOB_ESCAPE_MAP: Dict[str, str] = {
    "[": "[[]",
    "]": "[]]",
    "*": "[*]",
    "?": "[?]",
}


def escape_glob_pattern(path: str) -> str:
    """把仓库相对路径转成可被 ``fnmatch`` **精确**匹配的模式。

    为什么需要转义：modelscope SDK 用 ``fnmatch.fnmatch(path, pattern)`` 匹配
    ``allow_patterns``（见 ``modelscope_hub/_download.py::_matches_patterns``）。
    文件名里的 ``[``、``]``、``*``、``?`` 是通配元字符，直接当精确路径传进去
    会退化成字符类 / 通配符：既可能匹配不到文件本身（该下的没下），也可能
    误匹配同目录下的其它文件（不该下的下了）。这里统一转义成只匹配自身的
    字符类写法，保证「勾选哪个文件就精确匹配哪个文件」。

    Args:
        path: 仓库相对路径，例如 ``onnx/model.onnx`` 或 ``model[1].onnx``。

    Returns:
        转义后的模式；``path`` 为空时返回空字符串。
    """
    text = str(path or "").replace("\\", "/").strip().lstrip("/")
    if text.startswith("./"):
        text = text[2:]
    return "".join(_GLOB_ESCAPE_MAP.get(char, char) for char in text)


def selection_to_allow_patterns(
    selected_paths: Optional[Sequence[str]] = None,
) -> List[str]:
    """把清单勾选的文件路径转成 SDK 的精确包含规则。

    处理顺序：去掉首尾空白与 ``./`` 前缀、统一 ``/`` 分隔符、转义通配元字符、
    去重（保持清单顺序）。

    Args:
        selected_paths: 勾选的文件路径（``None`` / 非序列时按空处理）。

    Returns:
        可直接作为 ``snapshot_download(allow_patterns=...)`` 的列表。
    """
    result: List[str] = []
    if not isinstance(selected_paths, (list, tuple, set, frozenset)):
        return result
    for raw in selected_paths:
        pattern = escape_glob_pattern(str(raw or ""))
        if pattern and pattern not in result:
            result.append(pattern)
    return result


def _matching_listing(
    listing: Optional[ModelInfo],
    model_id: str = "",
    revision: str = "",
) -> Optional[ModelInfo]:
    """判断文件清单是否仍然匹配当前输入。

    清单可能是上一次查询的结果：用户若改了模型名 / 版本，旧清单的勾选结果
    不能拿来当新模型的过滤条件（否则会把新模型下成旧清单的子集）。不匹配时
    返回 ``None``，等价于「未获取清单」。

    Args:
        listing: 清单对象，可为 ``None``。
        model_id: 当前输入的模型 ID。
        revision: 当前输入的版本 / 分支。

    Returns:
        仍然可用的清单对象，或 ``None``。
    """
    if listing is None or not getattr(listing, "files", None):
        return None
    want_revision = str(revision or "").strip() or DEFAULT_REVISION
    have_revision = str(getattr(listing, "revision", "") or "").strip() or DEFAULT_REVISION
    if have_revision != want_revision:
        return None
    want_id = str(model_id or "").strip()
    if want_id:
        try:
            want_id = normalize_model_id(want_id)
        except ValidationError:
            pass
        have_id = str(getattr(listing, "model_id", "") or "").strip()
        if have_id != want_id:
            return None
    return listing


def _align_selection(
    selected_paths: Optional[Sequence[str]],
    listing_paths: Sequence[str],
) -> List[str]:
    """把界面勾选结果对齐到清单路径：丢弃未知项、去重、按清单顺序返回。"""
    allowed = set(listing_paths)
    picked = set()
    if isinstance(selected_paths, (list, tuple, set, frozenset)):
        for raw in selected_paths:
            text = str(raw or "").strip().replace("\\", "/").lstrip("/")
            if text.startswith("./"):
                text = text[2:]
            if text in allowed:
                picked.add(text)
    return [path for path in listing_paths if path in picked]


@dataclass
class DownloadScope:
    """一次下载的最终范围，以及范围的来源说明。

    Attributes:
        mode: ``SCOPE_ALL`` / ``SCOPE_SELECTED`` / ``SCOPE_EMPTY``。
        allow_patterns: 传给 SDK 的包含规则；空列表表示不加限制（下载全部）。
        ignore_patterns: 传给 SDK 的排除规则（排除优先，始终生效）。
        total_bytes: 进度条分母：勾选文件字节数之和；未知时为 0。
        selected_count: 清单中勾选的文件数。
        total_count: 清单中的文件总数；未获取清单时为 0。
        notes: 需要写进日志的解释性说明（规则优先级、被忽略的输入等）。
    """

    mode: str = SCOPE_ALL
    allow_patterns: List[str] = field(default_factory=list)
    ignore_patterns: List[str] = field(default_factory=list)
    total_bytes: int = 0
    selected_count: int = 0
    total_count: int = 0
    notes: List[str] = field(default_factory=list)

    @property
    def is_blocked(self) -> bool:
        """是否必须中止下载（清单存在但一个文件都没勾选）。"""
        return self.mode == SCOPE_EMPTY

    def describe(self) -> str:
        """返回一句「本次下载范围」的可读描述，供日志与界面直接展示。"""
        if self.mode == SCOPE_EMPTY:
            return "未勾选任何文件，已中止（清单共 {0} 个文件）".format(
                self.total_count)
        if self.mode == SCOPE_SELECTED:
            return "本次下载 {0}/{1} 个文件，合计 {2}".format(
                self.selected_count, self.total_count,
                format_size(self.total_bytes))
        if self.total_count:
            return "本次下载 {0}/{1} 个文件，合计 {2}".format(
                self.selected_count, self.total_count,
                format_size(self.total_bytes))
        return "未获取清单，本次下载全部文件（文件数量与大小未知）"


def plan_download_scope(
    allow_raw: str = "",
    ignore_raw: str = "",
    subdir: str = "",
    listing: Optional[ModelInfo] = None,
    selected_paths: Optional[Sequence[str]] = None,
    model_id: str = "",
    revision: str = "",
) -> DownloadScope:
    """把「清单勾选 + 手填规则」合并成确定的下载范围。

    优先级（写进 README，便于用户预期）：

    1. **有清单且部分勾选** —— 勾选即范围，是最强约束：
       ``allow_patterns`` 完全由勾选文件生成（逐条转义成精确匹配），手填的
       「包含规则 / 仅下载子目录」被忽略（日志会明说），避免用户勾了 A 却
       因为一条 ``*.safetensors`` 规则把同类文件一起拉下来。
    2. **排除规则永远优先** —— 即使文件被勾选，只要命中「排除规则」就不会
       下载（与旧版一致的语义），日志会提示该规则仍在生效。
    3. **有清单且全部勾选** —— 等价于「下载全部」，退回旧行为：手填的包含 /
       排除 / 子目录规则照常生效（保证老用户「填规则下载」的习惯不变）。
    4. **未获取清单（或清单与当前模型 / 版本不匹配）** —— 无法按勾选过滤，
       下载全部文件，但日志必须明确写出「未获取清单，本次下载全部文件」。
    5. **有清单但一个都没勾选** —— 返回 ``SCOPE_EMPTY``，界面必须中止下载并
       提示，绝不静默下载整模型。

    Args:
        allow_raw: 「包含规则」原始文本。
        ignore_raw: 「排除规则」原始文本。
        subdir: 「仅下载子目录」原始文本。
        listing: 当前的文件清单，可为 ``None``。
        selected_paths: 清单中被勾选的文件路径（Treeview 的 iid，即仓库相对路径）。
        model_id: 当前输入的模型 ID，用于判断清单是否过期。
        revision: 当前输入的版本，用于判断清单是否过期。

    Returns:
        :class:`DownloadScope` 实例（纯函数，无副作用）。
    """
    scope = DownloadScope(mode=SCOPE_ALL)
    rules_allow, rules_ignore = resolve_patterns(allow_raw, ignore_raw, subdir)
    scope.ignore_patterns = list(rules_ignore)

    usable = _matching_listing(listing, model_id, revision)
    if usable is None:
        scope.allow_patterns = list(rules_allow)
        scope.total_bytes = 0
        if listing is not None and getattr(listing, "files", None):
            scope.notes.append(
                "文件清单属于其它模型 / 版本，已忽略：请重新点【查看文件清单】后再按勾选下载。")
        scope.notes.append(
            "未获取清单，本次下载全部文件：勾选过滤只有在【查看文件清单】成功后才生效。")
        return scope

    listing_paths = [item.path for item in usable.files]
    sizes = {item.path: item.size for item in usable.files}
    chosen = _align_selection(selected_paths, listing_paths)
    scope.total_count = len(listing_paths)
    scope.selected_count = len(chosen)

    if not chosen:
        scope.mode = SCOPE_EMPTY
        scope.allow_patterns = []
        scope.total_bytes = 0
        scope.notes.append(
            "文件清单共 {0} 个文件，当前一个都没勾选。".format(len(listing_paths)))
        return scope

    if len(chosen) >= len(listing_paths):
        scope.mode = SCOPE_ALL
        scope.allow_patterns = list(rules_allow)
        scope.total_bytes = int(usable.total_size)
        if rules_allow or rules_ignore:
            scope.notes.append(
                "清单已全部勾选：按「下载全部」处理，手填的包含 / 排除 / 子目录规则照常生效。")
        return scope

    scope.mode = SCOPE_SELECTED
    scope.allow_patterns = selection_to_allow_patterns(chosen)
    scope.total_bytes = sum(int(sizes.get(path, 0)) for path in chosen)
    comma_names = [path for path in chosen if "," in path]
    if comma_names:
        # SDK 会把每条规则按逗号切分（normalize_patterns），逗号文件名无法精确
        # 表达：与其静默漏下，不如在日志里明确告知用户改用其它方式。
        scope.notes.append(
            "以下文件名含英文逗号，SDK 的规则语法无法精确表达，可能不会被下载：{0}；"
            "请改用「包含规则」或勾选全部。".format(", ".join(comma_names)))
    if rules_allow:
        scope.notes.append(
            "勾选优先：本次忽略手填的包含规则 / 子目录（{0}），范围以清单勾选为准。".format(
                ", ".join(rules_allow)))
    if rules_ignore:
        scope.notes.append(
            "排除规则仍然生效（排除优先）：{0}；命中这些规则的文件即使勾选也不会下载。".format(
                ", ".join(rules_ignore)))
    return scope


def dir_size_bytes(root: Union[str, Path]) -> int:
    """统计目录内所有普通文件的字节数总和（用于降级进度）。

    Args:
        root: 目录路径；不存在或无权限时返回 0。

    Returns:
        字节数。
    """
    path = Path(root)
    if not path.exists():
        return 0
    total = 0
    try:
        for entry in path.rglob("*"):
            try:
                if entry.is_file():
                    total += entry.stat().st_size
            except (OSError, ValueError):
                continue
    except (OSError, ValueError):
        return total
    return total


def apply_environment(endpoint: str = "", proxy: str = "") -> Dict[str, str]:
    """把镜像站点与代理写入进程环境变量，供 SDK 与底层 HTTP 库读取。

    仅写入非空值；不会清除系统原有的代理设置，便于用户清空输入框后沿用
    系统代理。桌面工具为单进程，写入全局环境变量是可接受的。

    Args:
        endpoint: 镜像 / API 站点，例如 ``https://www.modelscope.cn``。
        proxy: HTTP(S) 代理，例如 ``http://127.0.0.1:7890``。

    Returns:
        实际生效的键值对（键为 ``endpoint`` / ``proxy``）。
    """
    applied: Dict[str, str] = {}
    site = str(endpoint or "").strip().rstrip("/")
    if site:
        for key in ("MODELSCOPE_ENDPOINT", "MODELSCOPE_API_ENDPOINT"):
            os.environ[key] = site
        applied["endpoint"] = site
    address = str(proxy or "").strip()
    if address:
        for key in (
            "HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
            "ALL_PROXY", "all_proxy",
        ):
            os.environ[key] = address
        applied["proxy"] = address
    return applied


# ---------------------------------------------------------------------------
# SDK 探测
# ---------------------------------------------------------------------------

_SDK_CACHE: Dict[str, Any] = {
    "checked": False,
    "available": False,
    "func": None,
    "version": "",
    "error": "",
}
_SDK_LOCK = threading.Lock()


def _load_sdk() -> Tuple[bool, Optional[Callable[..., Any]], str]:
    """惰性导入 ``snapshot_download`` 并缓存结果。

    真实导入 ``modelscope`` 可能耗时数秒；调用方应放在后台线程执行。

    Returns:
        ``(是否可用, snapshot_download 函数或 None, 版本字符串)``。
    """
    with _SDK_LOCK:
        if _SDK_CACHE["checked"]:
            return bool(_SDK_CACHE["available"]), _SDK_CACHE["func"], str(_SDK_CACHE["version"])
        try:
            module = importlib.import_module("modelscope")
            hub_module = importlib.import_module("modelscope.hub.snapshot_download")
            func = getattr(hub_module, "snapshot_download", None)
            if not callable(func):
                raise ImportError("modelscope.hub.snapshot_download 中未找到 snapshot_download")
            version = str(getattr(module, "__version__", "未知"))
            _SDK_CACHE.update(
                checked=True, available=True, func=func, version=version, error=""
            )
        except Exception as exc:  # 导入失败不应让程序崩溃
            _SDK_CACHE.update(
                checked=True, available=False, func=None, version="", error=str(exc)
            )
        return bool(_SDK_CACHE["available"]), _SDK_CACHE["func"], str(_SDK_CACHE["version"])


def sdk_is_installed() -> bool:
    """快速判断 ``modelscope`` 是否已安装（不真正导入，界面启动时不阻塞）。"""
    try:
        return importlib.util.find_spec("modelscope") is not None
    except (ImportError, ValueError, AttributeError):
        return False


def is_sdk_available() -> bool:
    """真正导入 SDK 并返回是否可用（首次调用可能较慢）。"""
    return _load_sdk()[0]


def sdk_version() -> str:
    """返回已加载的 ``modelscope`` 版本，不可用或未知时返回空字符串 / ``未知``。"""
    return _load_sdk()[2]


def get_snapshot_download() -> Optional[Callable[..., Any]]:
    """返回缓存的 ``snapshot_download`` 函数，SDK 不可用时返回 ``None``。"""
    return _load_sdk()[1]


def probe_local_dir_support(snapshot_download: Optional[Callable[..., Any]] = None) -> bool:
    """探测当前 SDK 是否支持 ``local_dir`` 参数。

    优先使用 ``inspect.signature`` 静态判断；若签名只有 ``**kwargs`` 无法
    确定，则乐观返回 ``True``，由 :func:`download_model` 中的运行时
    ``try/except TypeError`` 兜底切换到 ``cache_dir`` 回退路径。

    Args:
        snapshot_download: 待探测的函数；``None`` 时使用缓存的 SDK 函数。

    Returns:
        是否（可能）支持 ``local_dir``。
    """
    func = snapshot_download or _load_sdk()[1]
    if func is None:
        return False
    try:
        signature = inspect.signature(func)
    except (TypeError, ValueError):
        return False
    parameters = signature.parameters
    if "local_dir" in parameters:
        return True
    has_var_kw = any(
        param.kind is inspect.Parameter.VAR_KEYWORD
        for param in parameters.values()
    )
    if not has_var_kw:
        return False
    # 带 **kwargs 却显式声明了 cache_dir 而没有 local_dir，基本可判定为旧版 SDK；
    # 若此时乐观返回 True，旧版会用 **kwargs 静默吞掉 local_dir 而不报错。
    if "cache_dir" in parameters:
        return False
    # 纯 (*args, **kwargs) 代理层静态无法判断，交给 download_model 的运行时校验。
    return True


# ---------------------------------------------------------------------------
# 配置持久化
# ---------------------------------------------------------------------------


def get_config_path() -> Path:
    """返回配置文件路径。

    Windows 上为 ``%APPDATA%/ModelScopeDownloader/config.json``；取不到
    ``APPDATA`` 时回退到用户主目录。
    """
    appdata = os.environ.get("APPDATA", "").strip()
    base = Path(appdata) if appdata else Path.home()
    return base / CONFIG_DIR_NAME / CONFIG_FILE_NAME


def load_config(path: Optional[Union[str, Path]] = None) -> AppConfig:
    """读取配置；文件缺失、损坏或字段类型错误时回退默认值。

    Args:
        path: 自定义配置路径，主要用于单元测试；``None`` 时使用默认路径。

    Returns:
        永远不会抛异常的 :class:`AppConfig` 实例。
    """
    config_path = Path(path) if path else get_config_path()
    config = AppConfig()
    try:
        if not config_path.is_file():
            return config
        with open(str(config_path), "r", encoding="utf-8") as handle:
            data = json.load(handle)
        if not isinstance(data, dict):
            return config
        for name in _CONFIG_FIELDS:
            if name not in data:
                continue
            value = data[name]
            expected = getattr(config, name)
            if isinstance(expected, bool):
                if isinstance(value, bool):
                    parsed: Any = value
                else:
                    parsed = str(value).strip().lower() in ("1", "true", "yes", "on")
            elif isinstance(expected, int):
                try:
                    parsed = int(value)
                except (TypeError, ValueError):
                    continue
            elif isinstance(expected, str):
                parsed = "" if value is None else str(value)
            else:
                continue
            setattr(config, name, parsed)
        if config.max_workers <= 0:
            config.max_workers = DEFAULT_MAX_WORKERS
        if not config.revision.strip():
            config.revision = DEFAULT_REVISION
    except Exception:
        return AppConfig()
    return config


def save_config(config: AppConfig, path: Optional[Union[str, Path]] = None) -> bool:
    """原子写入配置文件。

    Args:
        config: 待保存的配置。
        path: 自定义路径，主要用于单元测试。

    Returns:
        是否写入成功；任何异常都被吞掉并返回 ``False``。
    """
    config_path = Path(path) if path else get_config_path()
    try:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = config_path.with_name(config_path.name + ".tmp")
        with open(str(temp_path), "w", encoding="utf-8") as handle:
            json.dump(asdict(config), handle, ensure_ascii=False, indent=2)
        os.replace(str(temp_path), str(config_path))
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# 文件清单查询：SDK 优先 + REST 兜底
# ---------------------------------------------------------------------------


def _http_get_json(url: str, proxy: str, timeout: float) -> Any:
    """用标准库 urllib 发起 GET 并解析 JSON。

    Args:
        url: 完整 URL。
        proxy: 显式代理地址；为空时沿用 urllib 默认（含系统环境变量）。
        timeout: 超时秒数。

    Returns:
        解析后的 JSON 对象。

    Raises:
        Exception: 网络错误、HTTP 错误或 JSON 解析错误，由调用方统一处理。
    """
    handlers: List[Any] = []
    address = str(proxy or "").strip()
    if address:
        handlers.append(
            urllib.request.ProxyHandler({"http": address, "https": address, "ftp": address})
        )
    opener = urllib.request.build_opener(*handlers)
    request = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )
    with opener.open(request, timeout=timeout) as response:
        raw = response.read()
    return json.loads(raw.decode("utf-8", errors="replace"))


def _build_files_api_urls(model_id: str, revision: str, endpoint: str) -> List[str]:
    """构造 ModelScope 文件清单 REST 接口候选 URL。

    兼容用户填写 ``https://www.modelscope.cn`` 与
    ``https://www.modelscope.cn/api/v1`` 两种镜像站点写法。
    """
    base = str(endpoint or "").strip().rstrip("/") or DEFAULT_ENDPOINT
    prefix = base if base.lower().endswith("/api/v1") else base + "/api/v1"
    quoted_id = urllib.parse.quote(model_id, safe="/")
    query = urllib.parse.urlencode(
        {"Revision": revision or DEFAULT_REVISION, "Recursive": "True"}
    )
    return [
        "{0}/models/{1}/repo/files?{2}".format(prefix, quoted_id, query),
        "{0}/models/{1}/repo/tree?{2}".format(prefix, quoted_id, query),
    ]


def _parse_file_entry(entry: Any) -> Optional[RepoFile]:
    """把一条接口记录解析为 :class:`RepoFile`，兼容多种字段命名。

    目录条目（``Type=tree`` / 路径以 ``/`` 结尾）会被丢弃。
    """
    if isinstance(entry, str):
        path = entry.replace("\\", "/")
        return RepoFile(path=path, size=0) if path else None
    if not isinstance(entry, dict):
        return None

    raw_path = _pick(
        entry,
        ["Path", "path", "Name", "name", "FilePath", "file_path",
         "RelativePath", "relative_path", "Key", "key"],
    )
    if raw_path is None:
        return None
    path = str(raw_path).replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    path = path.lstrip("/")
    if not path:
        return None

    file_type = str(_pick(entry, ["Type", "type", "Kind", "kind"]) or "blob").lower()
    if file_type in ("tree", "dir", "directory", "folder") or path.endswith("/"):
        return None

    size = _to_int(
        _pick(entry, ["Size", "size", "Length", "length", "Bytes", "bytes",
                      "FileSize", "file_size"]),
        0,
    )
    is_lfs = bool(_pick(entry, ["IsLFS", "is_lfs", "IsLfs", "LFS", "lfs"]) or False)
    sha256 = str(_pick(entry, ["Sha256", "sha256", "SHA256", "Sha", "sha"]) or "")
    return RepoFile(
        path=path,
        size=size,
        name=path.rsplit("/", 1)[-1],
        file_type=file_type,
        is_lfs=is_lfs,
        sha256=sha256,
    )


def _parse_file_entries(
    raw: Any,
    source: str,
    model_id: str,
    revision: str,
) -> ModelInfo:
    """把 SDK / REST 返回的原始数据统一解析成 :class:`ModelInfo`。"""
    list_keys = ["Files", "files", "FileList", "file_list",
                 "Tree", "tree", "List", "list"]
    payload: Any = raw
    if isinstance(payload, dict):
        data = _pick(payload, ["Data", "data"])
        if isinstance(data, dict):
            payload = _pick(data, list_keys)
        elif isinstance(data, list):
            payload = data
        else:
            # 没有 Data/data 包装时，清单直接挂在顶层（Files/FileList/Tree/List）。
            payload = _pick(payload, list_keys)
        if payload is None:
            payload = _pick(raw, list_keys)
    if payload is None:
        payload = []
    if isinstance(payload, dict):
        values = list(payload.values())
        # 兼容 {"Files": [...]} 之类的包裹：展开成条目列表，避免产生嵌套 list
        # 导致 _parse_file_entry 收到 list 而返回 None（空清单）。
        if len(values) == 1 and isinstance(values[0], (list, tuple)):
            payload = list(values[0])
        else:
            payload = values
    if not isinstance(payload, (list, tuple)):
        raise RepoQueryError("接口返回的文件清单格式无法识别")

    files: List[RepoFile] = []
    for entry in payload:
        parsed = _parse_file_entry(entry)
        if parsed is not None:
            files.append(parsed)
    files.sort(key=lambda item: item.path)
    return ModelInfo(model_id=model_id, revision=revision, files=files, source=source)


def _call_list_method(
    method: Callable[..., Any],
    model_id: str,
    revision: str,
    endpoint: str,
    timeout: float,
) -> Any:
    """以兼容多种 SDK 版本的方式调用 ``HubApi`` 文件清单方法。

    先用 ``inspect.signature`` 挑选该方法真正接受的关键字参数；若静态探测
    不可用，则依次尝试若干常见调用形式，仅对 ``TypeError`` 降级，避免把
    网络异常误判为签名不匹配。
    """
    attempts: List[Callable[[], Any]] = []
    try:
        signature = inspect.signature(method)
        names = set(signature.parameters)
        has_var_kw = any(
            param.kind is inspect.Parameter.VAR_KEYWORD
            for param in signature.parameters.values()
        )
        if has_var_kw or "model_id" in names:
            kwargs: Dict[str, Any] = {"model_id": model_id}
            if has_var_kw or "revision" in names:
                kwargs["revision"] = revision
            if has_var_kw or "recursive" in names:
                kwargs["recursive"] = True
            if has_var_kw or "is_recursive" in names:
                kwargs["is_recursive"] = True
            if endpoint and (has_var_kw or "endpoint" in names):
                kwargs["endpoint"] = endpoint
            if has_var_kw or "timeout" in names:
                kwargs["timeout"] = timeout
            attempts.append(lambda bound=kwargs: method(**bound))
    except (TypeError, ValueError):
        pass

    attempts.extend([
        lambda: method(model_id=model_id, revision=revision, recursive=True),
        lambda: method(model_id, revision, True),
        lambda: method(model_id=model_id, revision=revision),
        lambda: method(model_id, revision),
        lambda: method(model_id),
    ])

    last_error: Optional[TypeError] = None
    for attempt in attempts:
        try:
            return attempt()
        except TypeError as exc:
            last_error = exc
            continue
    if last_error is not None:
        raise last_error
    raise RepoQueryError("HubApi 文件清单方法调用失败")


def _fetch_via_sdk(
    model_id: str,
    revision: str,
    endpoint: str,
    timeout: float,
) -> ModelInfo:
    """通过 ``modelscope.hub.api.HubApi`` 获取文件清单。"""
    module = importlib.import_module("modelscope.hub.api")
    hub_api_class = getattr(module, "HubApi", None)
    if not callable(hub_api_class):
        raise RepoQueryError("当前 modelscope 版本未提供 HubApi")
    try:
        api = hub_api_class(endpoint=endpoint) if endpoint else hub_api_class()
    except TypeError:
        api = hub_api_class()

    method: Optional[Callable[..., Any]] = None
    for name in (
        "get_model_files", "list_model_files", "get_model_file_tree",
        "list_repo_files", "list_model_tree",
    ):
        candidate = getattr(api, name, None)
        if callable(candidate):
            method = candidate
            break
    if method is None:
        raise RepoQueryError("当前 modelscope 版本的 HubApi 未提供文件清单接口")
    raw = _call_list_method(method, model_id, revision, endpoint, timeout)
    return _parse_file_entries(raw, "SDK", model_id, revision)


def _fetch_via_rest(
    model_id: str,
    revision: str,
    endpoint: str,
    proxy: str,
    timeout: float,
) -> ModelInfo:
    """通过 ModelScope 公开 REST 接口获取文件清单（纯标准库实现）。

    实际返回结构为 ``{"Code": 200, "Data": {"Files": [
    {"Path": "...", "Size": 123, "Type": "blob", "IsLFS": true, ...}]}}``；
    本函数对字段名与大小写做了宽松兼容。
    """
    urls = _build_files_api_urls(model_id, revision, endpoint)
    last_error: Optional[Exception] = None
    empty_info: Optional[ModelInfo] = None
    for url in urls:
        try:
            payload = _http_get_json(url, proxy, timeout)
            code = _pick(payload, ["Code", "code"])
            if code is not None and str(code) not in ("200", "0"):
                raise RepoQueryError(
                    "接口返回 Code={0}，Message={1}".format(
                        code, _pick(payload, ["Message", "message", "msg"])
                    )
                )
            success = _pick(payload, ["Success", "success"])
            if success is False:
                raise RepoQueryError("接口返回 Success=false")
            info = _parse_file_entries(payload, "REST", model_id, revision)
            if info.files:
                return info
            empty_info = info
        except Exception as exc:  # 换下一个候选 URL 再试
            last_error = exc
            continue
    if empty_info is not None:
        # 两个候选 URL 都返回了合法的空清单：不能静默宣告「共 0 个文件成功」，
        # 否则用户会误以为模型仓库是空的。给出可判定的错误信息。
        detail = ("公开接口返回了空文件清单（{0} @ {1}）。可能是模型名 / revision "
                  "有误、仓库为私有或需要登录，或接口结构与当前版本不兼容；"
                  "请核对后重试，或安装 modelscope SDK 后由 HubApi 查询。").format(
                      model_id, revision)
        if last_error is not None:
            detail += "（最后一次请求提示：{0}）".format(last_error)
        raise RepoQueryError(detail)
    raise RepoQueryError(
        "公开接口请求失败：{0}".format(
            "{0}: {1}".format(type(last_error).__name__, last_error)
            if last_error else "无可用响应"
        )
    )


def fetch_repo_files(
    model_id: str,
    revision: str = DEFAULT_REVISION,
    endpoint: str = "",
    proxy: str = "",
    timeout: float = DEFAULT_TIMEOUT,
    prefer_sdk: bool = True,
    log_cb: Optional[LogReporter] = None,
    cancel_event: Optional[threading.Event] = None,
) -> ModelInfo:
    """查询模型仓库文件清单：SDK ``HubApi`` 优先，公开 REST 接口兜底。

    两条路径都失败时抛出 :class:`RepoQueryError`，并把两边的错误原因拼接成
    可读信息。即使 SDK 完全未安装，REST 路径也能正常工作。

    Args:
        model_id: ``owner/name`` 形式的模型 ID。
        revision: 版本 / 分支，默认 ``master``。
        endpoint: 镜像 / API 站点，留空使用官方站点。
        proxy: HTTP(S) 代理，留空沿用系统设置。
        timeout: 单次 HTTP 超时秒数。
        prefer_sdk: 是否优先尝试 SDK（测试时可置 ``False`` 强制走 REST）。
        log_cb: 日志回调。
        cancel_event: 取消事件。

    Returns:
        :class:`ModelInfo` 实例。

    Raises:
        ValidationError: 模型 ID 不合法。
        DownloadCancelled: 用户取消。
        RepoQueryError: SDK 与 REST 均未取得清单。
    """
    log = _make_logger(log_cb)
    normalized_id = normalize_model_id(model_id)
    effective_revision = str(revision or "").strip() or DEFAULT_REVISION
    _raise_if_cancelled(cancel_event)

    sdk_errors: List[str] = []
    if prefer_sdk and sdk_is_installed() and is_sdk_available():
        try:
            log("正在通过 ModelScope SDK（HubApi）获取文件清单 ...")
            _raise_if_cancelled(cancel_event)
            info = _fetch_via_sdk(normalized_id, effective_revision, endpoint, timeout)
            if info.files:
                log("SDK 返回 {0} 个文件，合计 {1}".format(
                    info.file_count, format_size(info.total_size)))
                return info
            log("SDK 返回空清单，继续尝试公开接口 ...")
        except DownloadCancelled:
            raise
        except Exception as exc:
            sdk_errors.append("{0}: {1}".format(type(exc).__name__, exc))
            log("SDK 获取清单失败（{0}），改用公开接口 ...".format(exc))
    else:
        log("未检测到可用的 modelscope SDK，直接使用公开 REST 接口查询文件清单。")

    _raise_if_cancelled(cancel_event)
    log("正在通过公开接口获取文件清单：{0} @ {1} ...".format(
        normalized_id, effective_revision))
    try:
        info = _fetch_via_rest(normalized_id, effective_revision, endpoint, proxy, timeout)
        log("公开接口返回 {0} 个文件，合计 {1}".format(
            info.file_count, format_size(info.total_size)))
        return info
    except DownloadCancelled:
        raise
    except Exception as exc:
        detail = "公开接口失败：{0}: {1}".format(type(exc).__name__, exc)
        if sdk_errors:
            detail += "；SDK 失败：" + "；".join(sdk_errors)
        raise RepoQueryError(detail) from exc


# ---------------------------------------------------------------------------
# 下载参数组装
# ---------------------------------------------------------------------------


def _filter_kwargs(func: Callable[..., Any], kwargs: Dict[str, Any]) -> Dict[str, Any]:
    """按目标函数真实签名过滤 / 改名关键字参数。

    * 新旧参数名（``allow_patterns`` / ``allow_file_pattern``）二选一；
    * 函数明确声明了新版名或带 ``**kwargs`` 时优先使用新版名；
    * 无 ``**kwargs`` 时丢弃任何函数不接受的参数，避免旧 SDK 抛 TypeError。
    """
    result = dict(kwargs)
    try:
        signature = inspect.signature(func)
    except (TypeError, ValueError):
        return result
    parameters = signature.parameters
    explicit = set(parameters)
    has_var_kw = any(
        param.kind is inspect.Parameter.VAR_KEYWORD for param in parameters.values()
    )
    for new_name, old_name in (
        ("allow_patterns", "allow_file_pattern"),
        ("ignore_patterns", "ignore_file_pattern"),
    ):
        if new_name in result and old_name in result:
            if old_name in explicit and new_name not in explicit:
                # 函数明确认识旧名（带 **kwargs 的旧版 SDK），必须用旧名，
                # 否则规则会被 **kwargs 静默吞掉而不生效。
                result[old_name] = result.pop(new_name)
            else:
                result.pop(old_name, None)
    if not has_var_kw:
        result = {key: value for key, value in result.items() if key in explicit}
    return result


def build_download_kwargs(
    model_id: str,
    revision: str = DEFAULT_REVISION,
    target_dir: Union[str, Path, None] = None,
    allow_patterns: Optional[Sequence[str]] = None,
    ignore_patterns: Optional[Sequence[str]] = None,
    max_workers: int = DEFAULT_MAX_WORKERS,
    endpoint: str = "",
    supports_local_dir: bool = True,
    cache_dir: Union[str, Path, None] = None,
    snapshot_download: Optional[Callable[..., Any]] = None,
    progress_callbacks: Optional[List[Any]] = None,
) -> Dict[str, Any]:
    """组装 ``snapshot_download`` 的关键字参数（纯函数，便于单元测试）。

    兼容策略：
        * ``supports_local_dir=True`` 时写入 ``local_dir``，让文件直接落到用户
          选择的文件夹；否则写入 ``cache_dir``，由 :func:`sync_cache_to_target`
          在下载后同步。
        * 同时提供新旧两套过滤参数名，再由 :func:`_filter_kwargs` 依据 SDK
          真实签名二选一。
        * 传入 ``snapshot_download`` 时自动过滤该函数不接受的参数。

    Args:
        model_id: 模型 ID。
        revision: 版本 / 分支。
        target_dir: 用户选择的保存文件夹。
        allow_patterns: 包含规则列表。
        ignore_patterns: 排除规则列表。
        max_workers: 并发线程数。
        endpoint: 镜像 / API 站点。
        supports_local_dir: 当前 SDK 是否支持 ``local_dir``。
        cache_dir: 回退策略使用的缓存目录。
        snapshot_download: SDK 函数，用于按签名过滤参数。
        progress_callbacks: SDK 进度回调类列表（可选）。

    Returns:
        可直接 ``snapshot_download(**kwargs)`` 的字典。
    """
    kwargs: Dict[str, Any] = {
        "model_id": normalize_model_id(model_id),
        "revision": str(revision or "").strip() or DEFAULT_REVISION,
    }
    target = str(target_dir).strip() if target_dir else ""
    if supports_local_dir:
        if target:
            kwargs["local_dir"] = target
    else:
        cache = str(cache_dir).strip() if cache_dir else target
        if cache:
            kwargs["cache_dir"] = cache

    allow = [str(item).strip() for item in (allow_patterns or []) if str(item).strip()]
    ignore = [str(item).strip() for item in (ignore_patterns or []) if str(item).strip()]
    if allow:
        kwargs["allow_patterns"] = allow
        kwargs["allow_file_pattern"] = allow
    if ignore:
        kwargs["ignore_patterns"] = ignore
        kwargs["ignore_file_pattern"] = ignore
    if int(max_workers or 0) > 0:
        kwargs["max_workers"] = int(max_workers)
    site = str(endpoint or "").strip().rstrip("/")
    if site:
        kwargs["endpoint"] = site
    if progress_callbacks:
        kwargs["progress_callbacks"] = list(progress_callbacks)

    if snapshot_download is not None:
        return _filter_kwargs(snapshot_download, kwargs)
    return kwargs


def _strip_unexpected_kwarg(
    exc: TypeError,
    kwargs: Dict[str, Any],
    optional: Sequence[str],
) -> Optional[str]:
    """从 ``unexpected keyword argument`` 异常中剔除一个可选参数。

    Returns:
        被剔除的参数名；无需剔除时返回 ``None``。
    """
    message = str(exc)
    match = re.search(r"unexpected keyword argument ['\"]([^'\"]+)['\"]", message)
    name = match.group(1) if match else ""
    if not name:
        for key in optional:
            if key in kwargs and key in message:
                name = key
                break
    if name in optional and name in kwargs:
        kwargs.pop(name)
        return name
    return None


def _call_snapshot_download(
    func: Callable[..., Any],
    kwargs: Dict[str, Any],
    log: LogReporter,
) -> Any:
    """调用 SDK 并在遇到可选参数不兼容时自动剔除后重试。

    ``local_dir`` 不在可剔除列表中，其 TypeError 会向上抛给
    :func:`download_model` 以切换到 ``cache_dir`` 策略。
    """
    trial = dict(kwargs)
    for _ in range(len(_OPTIONAL_KWARGS) + 2):
        try:
            return func(**trial)
        except TypeError as exc:
            removed = _strip_unexpected_kwarg(exc, trial, _OPTIONAL_KWARGS)
            if removed:
                log("SDK 不支持参数 {0}，已自动移除后重试".format(removed))
                continue
            raise
    raise DownloadError("SDK 参数兼容重试次数过多，请升级 modelscope 后重试")


def _try_build_sdk_progress_callback(
    func: Callable[..., Any],
    shared_ref: List[Any],
    log: LogReporter,
    cancel_event: Optional[threading.Event] = None,
) -> Optional[List[Any]]:
    """尝试构造 SDK 进度回调类；任何不兼容都安全返回 ``None``。

    SDK 的字节级进度在不同版本间差异极大，这里把回调降级为「下载仍在进行」
    的心跳信号，真实数值仍由 :class:`_ProgressPoller` 扫描目标目录获得，既
    满足「优先尝试 SDK 进度回调」，又不会因为回调签名不匹配而中断下载。

    这里同时也是「停止 / 取消」唯一能触及 SDK 内部的注入点：
    ``modelscope_hub`` 的 ``snapshot_download`` 不接受外部 cancel_event，
    但会为每个文件实例化 ``progress_callbacks`` 并在每个数据块调用
    ``update``。在回调里检查取消事件并抛出 :class:`DownloadCancelled`，即可
    真实中断 SDK 的下载循环，而不是只置一个没人检查的标志（Bug B 修复）。

    Args:
        func: ``snapshot_download`` 函数，用于按签名判断是否支持回调。
        shared_ref: 单元素列表，存放最后一次心跳时间戳。
        log: 日志回调。
        cancel_event: GUI 的停止事件；``None`` 时不做取消检查。

    Returns:
        可传给 ``snapshot_download(progress_callbacks=...)`` 的回调类列表，
        或 ``None``（当前 SDK 不兼容）。
    """
    try:
        signature = inspect.signature(func)
        has_var_kw = any(
            param.kind is inspect.Parameter.VAR_KEYWORD
            for param in signature.parameters.values()
        )
        if "progress_callbacks" not in signature.parameters and not has_var_kw:
            return None
    except (TypeError, ValueError):
        return None

    base_class: Any = None
    for module_name in (
        "modelscope.hub.callback",
        "modelscope.hub.snapshot_download",
        "modelscope.hub.utils.utils",
        "modelscope.hub.file_download",
        "modelscope_hub.callback",
    ):
        try:
            candidate = getattr(importlib.import_module(module_name), "ProgressCallback", None)
            if isinstance(candidate, type):
                base_class = candidate
                break
        except Exception:
            continue
    if base_class is None:
        return None

    def _heartbeat() -> None:
        """记录一次心跳；用户已请求停止时抛异常以中断 SDK 下载。"""
        shared_ref[0] = time.time()
        if cancel_event is not None and cancel_event.is_set():
            raise DownloadCancelled("用户请求停止，SDK 下载回调已中断")

    try:
        class _SdkProgressReporter(base_class):  # type: ignore[valid-type, misc]
            """把任意 SDK 进度事件转换为心跳 / 取消检查；未知方法安全忽略。"""

            def __init__(self, *args: Any, **kwargs: Any) -> None:
                try:
                    super().__init__(*args, **kwargs)
                except Exception:
                    try:
                        super().__init__()
                    except Exception:
                        pass

            def __call__(self, *args: Any, **kwargs: Any) -> None:
                _heartbeat()

            def update(self, *args: Any, **kwargs: Any) -> None:
                _heartbeat()

            def on_progress(self, *args: Any, **kwargs: Any) -> None:
                _heartbeat()

            def progress(self, *args: Any, **kwargs: Any) -> None:
                _heartbeat()

            def end(self, *args: Any, **kwargs: Any) -> None:
                _heartbeat()

            def __getattr__(self, name: str) -> Any:
                if name.startswith("__"):
                    raise AttributeError(name)

                def _ignored(*_args: Any, **_kwargs: Any) -> None:
                    _heartbeat()

                return _ignored

        _SdkProgressReporter()  # 试实例化：抽象类 / 需要强制参数的基类会被放弃
    except Exception:
        return None

    log("已启用 SDK 进度回调（字节级数值仍由目标目录实时统计提供）")
    return [_SdkProgressReporter]


class _ProgressPoller:
    """后台定时统计目录已落盘字节数并上报进度的守护线程。"""

    def __init__(
        self,
        roots: Sequence[Union[str, Path]],
        total_bytes: int,
        cancel_event: Optional[threading.Event],
        progress: ProgressReporter,
        interval: float = 1.0,
        sdk_ref: Optional[List[Any]] = None,
    ) -> None:
        self.roots: List[Path] = [Path(root) for root in roots]
        self.total_bytes = int(total_bytes or 0)
        self.cancel_event = cancel_event
        self.progress = progress
        self.interval = max(0.2, float(interval or 1.0))
        self.sdk_ref = sdk_ref
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        """启动轮询线程（daemon）。"""
        self._thread = threading.Thread(
            target=self._run, name="ms-progress-poller", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        """请求轮询线程退出。"""
        self._stop_event.set()

    def _run(self) -> None:
        last_value = -1
        while not self._stop_event.wait(self.interval):
            if self.cancel_event is not None and self.cancel_event.is_set():
                continue
            if self.sdk_ref is not None and time.time() - float(self.sdk_ref[0]) < self.interval * 3:
                # SDK 回调刚触发过，说明下载仍在进行，跳过本次目录扫描。
                continue
            done = 0
            for root in self.roots:
                done += dir_size_bytes(root)
            if done != last_value:
                last_value = done
                self.progress(done, self.total_bytes, "")


# ---------------------------------------------------------------------------
# 回退复制（cache_dir -> 目标文件夹）
# ---------------------------------------------------------------------------


def _should_skip_dir(name: str) -> bool:
    """判断目录是否为 SDK 元数据目录。"""
    return name in _SKIP_DIR_NAMES or name.startswith(".")


def _copy_file_with_cancel(
    src: Path,
    dst: Path,
    cancel_event: Optional[threading.Event],
    chunk_size: int = 1024 * 1024,
) -> None:
    """分块复制单个文件，逐块检查取消事件，并用 ``.part`` 临时文件保证原子性。

    取消时删除未完成的临时文件，避免目标目录出现半截文件被误判为已完成。
    """
    temp = dst.with_name(dst.name + ".part")
    try:
        with open(str(src), "rb") as source, open(str(temp), "wb") as target:
            while True:
                if cancel_event is not None and cancel_event.is_set():
                    raise DownloadCancelled("复制流程已被用户取消")
                chunk = source.read(chunk_size)
                if not chunk:
                    break
                target.write(chunk)
        os.replace(str(temp), str(dst))
    finally:
        if temp.exists():
            try:
                temp.unlink()
            except OSError:
                pass


def sync_cache_to_target(
    cache_root: Union[str, Path],
    target_dir: Union[str, Path],
    cancel_event: Optional[threading.Event] = None,
    log_cb: Optional[LogReporter] = None,
    progress_cb: Optional[ProgressReporter] = None,
    total_bytes: int = 0,
) -> int:
    """把 SDK 缓存目录中的文件同步到用户指定的文件夹（回退路径）。

    保留完整目录结构；**目标文件已存在且大小一致时直接跳过**，从而保证
    「断点续传 / 跳过已存在」。复制过程逐块检查取消事件，可随时中断。

    Args:
        cache_root: ``snapshot_download`` 返回的快照目录或缓存根目录。
        target_dir: 用户选择的保存文件夹。
        cancel_event: 取消事件。
        log_cb: 日志回调。
        progress_cb: 进度回调。
        total_bytes: 预览得到的总字节数，用于进度显示。

    Returns:
        本次实际复制的文件数量。

    Raises:
        DownloadCancelled: 用户取消。
        DownloadError: 复制失败。
    """
    log = _make_logger(log_cb)
    progress = _make_progress(progress_cb)
    source_root = Path(str(cache_root))
    destination_root = Path(str(target_dir))
    if not source_root.is_dir():
        log("缓存目录不存在，无需同步：{0}".format(source_root))
        return 0
    try:
        destination_root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise DownloadError("无法创建目标文件夹 {0}：{1}".format(destination_root, exc)) from exc

    candidates: List[Tuple[Path, Any]] = []
    for entry in source_root.rglob("*"):
        try:
            relative = entry.relative_to(source_root)
        except ValueError:
            continue
        if any(_should_skip_dir(part) for part in relative.parts[:-1]):
            continue
        if entry.is_dir() or not entry.is_file():
            continue
        if any(entry.name.endswith(suffix) for suffix in _SKIP_FILE_SUFFIXES):
            continue
        candidates.append((entry, relative))

    copied = 0
    skipped = 0
    for source, relative in candidates:
        _raise_if_cancelled(cancel_event)
        try:
            source_size = source.stat().st_size
        except OSError:
            continue
        destination = destination_root / Path(str(relative))
        if destination.exists():
            try:
                if destination.stat().st_size == source_size:
                    skipped += 1
                    continue
            except OSError:
                pass
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            log("复制 {0}（{1}）".format(
                str(relative).replace("\\", "/"), format_size(source_size)))
            _copy_file_with_cancel(source, destination, cancel_event)
            copied += 1
            progress(dir_size_bytes(destination_root), int(total_bytes or 0), "")
        except DownloadCancelled:
            raise
        except OSError as exc:
            raise DownloadError(
                "复制文件失败 {0}：{1}".format(str(relative), exc)
            ) from exc

    log("同步统计：复制 {0} 个文件，跳过已存在 {1} 个文件".format(copied, skipped))
    return copied


# ---------------------------------------------------------------------------
# 下载主流程
# ---------------------------------------------------------------------------


def download_model(
    model_id: str,
    target_dir: Union[str, Path],
    revision: str = DEFAULT_REVISION,
    allow_patterns: Optional[Sequence[str]] = None,
    ignore_patterns: Optional[Sequence[str]] = None,
    max_workers: int = DEFAULT_MAX_WORKERS,
    endpoint: str = "",
    proxy: str = "",
    total_bytes: int = 0,
    cancel_event: Optional[threading.Event] = None,
    log_cb: Optional[LogReporter] = None,
    progress_cb: Optional[ProgressReporter] = None,
    progress_interval: float = 1.0,
    apply_env: bool = True,
) -> str:
    """下载模型到用户指定的文件夹。

    流程要点：
        1. 校验输入并自动创建目标文件夹（支持空格 / 中文路径）。
        2. 探测 SDK；缺失时抛 :class:`DependencyError`（界面负责弹安装指引）。
        3. 探测 ``local_dir`` 支持情况：
           * 支持：直接写入目标文件夹，SDK 自带断点续传；
           * 不支持 / 运行时拒绝：改用 ``cache_dir``，下载完成后调用
             :func:`sync_cache_to_target` 复制（跳过已存在且大小一致的文件）。
        4. 启动进度轮询守护线程；优先尝试 SDK 进度回调（心跳），字节级进度
           始终由目标目录实时统计提供，总大小未知时 ``total=0`` 由界面切换
           为 indeterminate 滚动模式。
        5. 全程检查取消事件：SDK 进度回调在每个数据块检查取消并抛
           ``DownloadCancelled`` 真实中断下载循环；SDK 抛出的任何异常在取消
           已置位时统一转译为 ``DownloadCancelled``，保证界面状态机复位。若
           SDK 正卡在无数据到达的网络等待，取消会在下一个数据块 / 网络超时
           后生效（如实降级，不假装已停止）。

    Args:
        model_id: ``owner/name`` 形式的模型 ID。
        target_dir: 保存文件夹（不存在会自动创建）。
        revision: 版本 / 分支，默认 ``master``。
        allow_patterns: 包含规则列表。
        ignore_patterns: 排除规则列表。
        max_workers: 并发线程数。
        endpoint: 镜像 / API 站点。
        proxy: HTTP(S) 代理。
        total_bytes: 预览得到的总字节数；``<=0`` 表示未知。
        cancel_event: 取消事件。
        log_cb: 日志回调 ``(str) -> None``。
        progress_cb: 进度回调 ``(done, total, message) -> None``。
        progress_interval: 进度轮询间隔秒数。
        apply_env: 是否写入镜像 / 代理环境变量（单元测试可置 ``False``）。

    Returns:
        最终文件所在目录的字符串路径。

    Raises:
        ValidationError: 输入不合法。
        DependencyError: SDK 缺失。
        DownloadCancelled: 用户取消。
        DownloadError: 下载或复制失败。
    """
    log = _make_logger(log_cb)
    progress = _make_progress(progress_cb)
    normalized_id = normalize_model_id(model_id)

    raw_target = str(target_dir or "").strip().strip('"').strip("'")
    if not raw_target:
        raise ValidationError("请先选择保存文件夹")
    target = Path(raw_target).expanduser()
    # 取消检查必须早于 mkdir，避免用户取消后仍在磁盘上留下空的目标文件夹。
    _raise_if_cancelled(cancel_event)
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise DownloadError("无法创建目标文件夹 {0}：{1}".format(target, exc)) from exc
    if not os.access(str(target), os.W_OK):
        raise DownloadError("目标文件夹不可写：{0}".format(target))

    available, func, version = _load_sdk()
    if not available or func is None:
        raise DependencyError(
            "未检测到可用的 modelscope SDK，请先执行：{0}".format(INSTALL_COMMAND)
        )
    log("已加载 modelscope SDK（版本 {0}）".format(version or "未知"))

    if apply_env:
        applied = apply_environment(endpoint, proxy)
        if applied.get("endpoint"):
            log("已设置镜像 / API 站点：{0}".format(applied["endpoint"]))
        if applied.get("proxy"):
            log("已设置 HTTP(S) 代理：{0}".format(applied["proxy"]))

    allow = [str(item) for item in (allow_patterns or []) if str(item).strip()]
    ignore = [str(item) for item in (ignore_patterns or []) if str(item).strip()]
    if allow:
        log("包含规则：{0}".format(", ".join(allow)))
    if ignore:
        log("排除规则：{0}".format(", ".join(ignore)))
    log("断点续传：SDK 会校验已存在文件并跳过 / 续传；中断后再次点击即可继续。")

    cache_root = get_config_path().parent / "cache" / re.sub(
        r"[^0-9A-Za-z_.\-]", "_", normalized_id
    )
    supports_local = probe_local_dir_support(func)
    shared_ref: List[Any] = [0.0, 0]
    callbacks = _try_build_sdk_progress_callback(
        func, shared_ref, log, cancel_event=cancel_event)
    if callbacks is None:
        log("当前 SDK 未提供可用的进度回调，改用「目标目录字节数」定时轮询作为进度来源。")

    def _make_kwargs(use_local_dir: bool) -> Dict[str, Any]:
        """按当前策略组装 SDK 关键字参数。"""
        return build_download_kwargs(
            model_id=normalized_id,
            revision=revision,
            target_dir=str(target),
            allow_patterns=allow,
            ignore_patterns=ignore,
            max_workers=max_workers,
            endpoint=endpoint,
            supports_local_dir=use_local_dir,
            cache_dir=str(cache_root),
            snapshot_download=func,
            progress_callbacks=callbacks,
        )

    def _cancel_if_requested(exc: BaseException) -> None:
        """取消已置位时，把 SDK 抛出的任何异常转译为 ``DownloadCancelled``。

        SDK 会把回调里的异常收集成 ``FileDownloadError`` 等类型；这里统一
        按「用户已停止」处理，界面才会写「已停止（可续传）」并复位状态机，
        而不是误报「任务失败」，也不会让后台线程永远卡在 SDK 内部。
        """
        if cancel_event is not None and cancel_event.is_set():
            log("已响应停止请求：SDK 下载已被进度回调中断，已完成文件保留。")
            raise DownloadCancelled(
                "下载已取消（已完成文件已保留，可续传）") from exc

    poller: Optional[_ProgressPoller] = None
    try:
        progress(0, int(total_bytes or 0), "准备下载")
        poller = _ProgressPoller(
            roots=[target] if supports_local else [target, cache_root],
            total_bytes=int(total_bytes or 0),
            cancel_event=cancel_event,
            progress=progress,
            interval=progress_interval,
            sdk_ref=shared_ref if callbacks else None,
        )
        poller.start()

        if supports_local:
            log("下载策略：local_dir —— 文件将直接写入所选文件夹（推荐）")
            kwargs = _make_kwargs(True)
            try:
                result = _call_snapshot_download(func, kwargs, log)
                used_local = True
            except TypeError as exc:
                if "local_dir" not in str(exc):
                    raise
                log("当前 SDK 运行时拒绝 local_dir，自动切换到 cache_dir 回退路径 ...")
                poller.roots = [target, cache_root]
                result = _call_snapshot_download(func, _make_kwargs(False), log)
                used_local = False
        else:
            log("下载策略：cache_dir —— 本机 modelscope 不支持 local_dir，"
                "将先下载到缓存，再同步到所选文件夹（保留目录结构）")
            result = _call_snapshot_download(func, _make_kwargs(False), log)
            used_local = False

        if cancel_event is not None and cancel_event.is_set():
            log("已收到停止请求；SDK 正在传输的单个文件无法强制中断，"
                "已完成内容已保留，下次点击【开始下载】可续传。")
            raise DownloadCancelled("下载已取消（已完成文件已保留，可续传）")

        sync_source: Optional[Path] = None
        if not used_local:
            if result and str(result).strip():
                candidate = Path(str(result))
                if candidate.is_dir():
                    sync_source = candidate
            if sync_source is None and cache_root.is_dir():
                sync_source = cache_root
            if sync_source is None:
                raise DownloadError("SDK 未返回有效的下载目录，无法同步到目标文件夹")
        else:
            # 运行时兜底：某些带 **kwargs 的旧版 SDK 会静默吞掉 local_dir
            # （既不生效也不抛 TypeError），此时返回值是缓存目录而非目标目录。
            # 通过比对返回目录，可确保「下载到指定文件夹内」这一硬要求。
            returned = Path(str(result)) if result and str(result).strip() else None
            if returned is not None and returned.is_dir():
                try:
                    target_resolved = target.resolve()
                    returned_resolved = returned.resolve()
                    in_target = (
                        returned_resolved == target_resolved
                        or target_resolved in returned_resolved.parents
                    )
                except OSError:
                    in_target = False
                if not in_target:
                    log("检测到 SDK 返回目录不是所选文件夹（local_dir 未生效）：{0}".format(
                        returned))
                    log("自动切换为回退路径，把已下载文件同步到所选文件夹 ...")
                    sync_source = returned
            if sync_source is None and returned is None and dir_size_bytes(target) == 0:
                # SDK 既没返回下载目录、目标文件夹也没有任何文件：绝不能显示
                # 「下载完成」。PRD 要求文件必须落到指定文件夹内。
                raise DownloadError(
                    "SDK 未返回下载目录，且所选文件夹内没有检测到任何文件；"
                    "无法确认模型已下载到 {0}。请检查网络、模型名与过滤规则，"
                    "或执行 pip install -U modelscope 升级 SDK 后重试。".format(target)
                )
            if sync_source is None and dir_size_bytes(target) == 0:
                log("提示：所选文件夹暂未检测到文件；若过滤规则无误，"
                    "建议执行 pip install -U modelscope 升级 SDK 后重试。")

        if sync_source is not None:
            log("开始同步缓存到目标文件夹：{0} -> {1}".format(sync_source, target))
            copied = sync_cache_to_target(
                sync_source,
                target,
                cancel_event=cancel_event,
                log_cb=log_cb,
                progress_cb=progress_cb,
                total_bytes=int(total_bytes or 0),
            )
            log("同步完成，本次复制 {0} 个文件（已存在且大小一致的文件自动跳过）".format(copied))
        else:
            log("文件已直接下载到目标文件夹：{0}".format(target))

        progress(dir_size_bytes(target), int(total_bytes or 0), "下载完成")
        log("=" * 56)
        log("下载完成：{0} @ {1}".format(normalized_id, revision or DEFAULT_REVISION))
        log("保存位置：{0}".format(target))
        log("=" * 56)
        return str(target)

    except DownloadCancelled:
        log("下载已取消；已完成的文件不会删除，下次点击【开始下载】自动续传。")
        raise
    except DependencyError:
        raise
    except DownloaderError as exc:
        _cancel_if_requested(exc)
        log("任务失败：{0}".format(exc))
        raise
    except TypeError as exc:
        _cancel_if_requested(exc)
        log("SDK 参数不兼容：{0}".format(exc))
        raise DownloadError(
            "当前 modelscope 版本与下载参数不兼容：{0}\n请执行 pip install -U modelscope 升级后重试。".format(exc)
        ) from exc
    except Exception as exc:
        _cancel_if_requested(exc)
        log("下载失败：{0}: {1}".format(type(exc).__name__, exc))
        raise DownloadError("下载失败：{0}".format(exc)) from exc
    finally:
        if poller is not None:
            poller.stop()
