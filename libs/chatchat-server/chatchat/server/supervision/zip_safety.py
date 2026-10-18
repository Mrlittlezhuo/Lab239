"""论文稿件压缩包的安全检查与安全解压（§6.6 第1条、§16.6 第1条）。

设计底线（与《AI预审功能详细设计方案》§5.5.1—§5.5.2 一致）：

- **只允许明确允许的扩展名**，不是"没禁止就接收"；
- **检查两遍**：先按 zip 中央目录的元数据做静态检查（`inspect_zip_package`），
  再在真正解压时逐个文件重新应用白名单、重新计数、重新嗅探内容
  （`safe_extract_zip`）。元数据可以撒谎，落盘字节不会；
- **解压炸弹**同时按"单文件声明大小 / 解压后总大小 / 文件数 / 压缩比"四个
  维度拦，并在流式读取时按真实字节再拦一遍，不信任 zip 头部；
- **不覆盖已有文件**：落盘用 ``O_CREAT|O_EXCL``，已存在即失败；
- **任何失败都回滚**本次已写出的文件，调用方拿到的是"完整解压"或"什么都没写"；
- 本模块**不联网、不调用任何外部程序**（不 shell out ``unzip``/``7z``），
  只用标准库 ``zipfile`` 在进程内读取。

嵌套压缩包（zip 里再套 zip/rar/7z/tar 等）一律拒绝：首版不做递归解压，
也不声称已经看过内层内容（§5.5.1）。
"""
from __future__ import annotations

import hashlib
import os
import re
import stat
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, FrozenSet, List, Optional, Sequence, Tuple

#: 解析器版本必须写进后续的业务快照，升级后不得静默改变旧结论。
ZIP_SAFETY_VERSION = "zip-safety/1.0"

# ---------------------------------------------------------------------------
# 默认上限（量级与 §5.5.2 的 20 文件 / 单文件 10 MB 对齐，按 LaTeX 工程放宽文件数）
# ---------------------------------------------------------------------------
DEFAULT_MAX_ENTRIES = 300
DEFAULT_MAX_TOTAL_BYTES = 40 * 1024 * 1024
DEFAULT_MAX_SINGLE_BYTES = 10 * 1024 * 1024
#: 压缩比阈值。正常 .tex 文本约 3—6 倍；超过该值按解压炸弹处理。
DEFAULT_MAX_COMPRESSION_RATIO = 40.0
#: 小于该展开体积的文件不做压缩比判断（小文本压缩比天然偏高，会误报）。
DEFAULT_RATIO_MIN_BYTES = 8 * 1024
DEFAULT_MAX_NAME_BYTES = 512
#: 内容嗅探最多读取的字节数（判断是否嵌套压缩包）。
SNIFF_BYTES = 512

# ---------------------------------------------------------------------------
# 白名单：LaTeX 工程场景。二进制图片只作为人工可见资源入库，本包不解析其内容。
# ---------------------------------------------------------------------------
LATEX_TEXT_EXTENSIONS: FrozenSet[str] = frozenset(
    {
        ".tex", ".bib", ".cls", ".sty", ".bst", ".bbx", ".cbx", ".dbx",
        ".cfg", ".def", ".ltx", ".txt", ".md",
    }
)
LATEX_IMAGE_EXTENSIONS: FrozenSet[str] = frozenset(
    {
        ".png", ".jpg", ".jpeg", ".pdf", ".eps", ".gif", ".bmp", ".tif",
        ".tiff", ".svg",
    }
)
LATEX_ALLOWED_EXTENSIONS: FrozenSet[str] = LATEX_TEXT_EXTENSIONS | LATEX_IMAGE_EXTENSIONS

#: 嵌套压缩包扩展名。命中即拒绝，不做递归解压。
NESTED_ARCHIVE_EXTENSIONS: FrozenSet[str] = frozenset(
    {
        ".zip", ".jar", ".apk", ".rar", ".001", ".7z", ".tar", ".gz", ".tgz",
        ".bz2", ".tbz2", ".xz", ".txz", ".lz", ".lz4", ".zst", ".z", ".cab",
        ".iso", ".war",
    }
)
#: 明确禁止的可执行/脚本类型。白名单本来已经挡住，这里只为给出更准确的中文提示。
DENIED_EXTENSIONS: FrozenSet[str] = frozenset(
    {
        ".exe", ".dll", ".so", ".dylib", ".sh", ".bash", ".bat", ".cmd",
        ".ps1", ".vbs", ".js", ".ts", ".jar", ".apk", ".msi", ".scr", ".pif",
        ".com", ".lnk", ".docm", ".xlsm", ".pptm",
    }
)

# (签名, 在文件中的偏移, 类型名)
_MAGIC_SIGNATURES: Tuple[Tuple[bytes, int, str], ...] = (
    (b"PK\x03\x04", 0, "zip"),
    (b"PK\x05\x06", 0, "zip"),
    (b"PK\x07\x08", 0, "zip"),
    (b"Rar!\x1a\x07", 0, "rar"),
    (b"7z\xbc\xaf\x27\x1c", 0, "7z"),
    (b"\x1f\x8b", 0, "gzip"),
    (b"BZh", 0, "bzip2"),
    (b"\xfd7zXZ\x00", 0, "xz"),
    (b"\x04\"m\x18", 0, "lz4"),
    (b"(\xb5/\xfd", 0, "zstd"),
)
#: tar 的魔数在偏移 257 处，需要多读一点。
_TAR_MAGIC_OFFSET = 257
_TAR_MAGIC = b"ustar"

_DRIVE_RE = re.compile(r"^[A-Za-z]:")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


class ZipSafetyError(ValueError):
    """压缩包未通过安全检查，或解压目标已存在同名文件。

    ``problems`` 是全部命中项（不只有第一条），路由层可以直接展示。
    """

    def __init__(self, problems: Sequence["ZipProblem"]) -> None:
        self.problems: List[ZipProblem] = list(problems)
        first = self.problems[0].message if self.problems else "压缩包检查未通过"
        super().__init__(first if len(self.problems) == 1 else f"{first}（共 {len(self.problems)} 项问题）")


@dataclass(frozen=True)
class ZipLimits:
    """一次解压的全部上限。全部可参数化，默认值偏保守。"""

    max_entries: int = DEFAULT_MAX_ENTRIES
    max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES
    max_single_bytes: int = DEFAULT_MAX_SINGLE_BYTES
    max_compression_ratio: float = DEFAULT_MAX_COMPRESSION_RATIO
    ratio_min_bytes: int = DEFAULT_RATIO_MIN_BYTES
    max_name_bytes: int = DEFAULT_MAX_NAME_BYTES
    allowed_extensions: FrozenSet[str] = LATEX_ALLOWED_EXTENSIONS
    text_extensions: FrozenSet[str] = LATEX_TEXT_EXTENSIONS
    denied_extensions: FrozenSet[str] = DENIED_EXTENSIONS
    nested_archive_extensions: FrozenSet[str] = NESTED_ARCHIVE_EXTENSIONS

    def as_dict(self) -> Dict[str, object]:
        return {
            "max_entries": self.max_entries,
            "max_total_bytes": self.max_total_bytes,
            "max_single_bytes": self.max_single_bytes,
            "max_compression_ratio": self.max_compression_ratio,
            "ratio_min_bytes": self.ratio_min_bytes,
            "max_name_bytes": self.max_name_bytes,
            "allowed_extensions": sorted(self.allowed_extensions),
        }


@dataclass
class ZipProblem:
    """一条安全检查命中项。``entry`` 是定位来源（归档内条目名）。"""

    code: str
    entry: str
    message: str
    detail: str = ""

    def as_dict(self) -> Dict[str, object]:
        return {
            "code": self.code,
            "entry": self.entry,
            "message": self.message,
            "detail": self.detail,
        }


@dataclass
class ZipEntryReport:
    """按 zip 元数据列出的条目（未解压）。"""

    entry: str
    extension: str
    size: int
    compressed_size: int
    is_directory: bool
    text_like: bool

    def as_dict(self) -> Dict[str, object]:
        return {
            "entry": self.entry,
            "extension": self.extension,
            "size": self.size,
            "compressed_size": self.compressed_size,
            "is_directory": self.is_directory,
            "text_like": self.text_like,
        }


@dataclass
class ZipSafetyReport:
    """`inspect_zip_package` 的结果：条目清单 + 全部命中项。"""

    archive_path: str
    ok: bool
    problems: List[ZipProblem] = field(default_factory=list)
    entries: List[ZipEntryReport] = field(default_factory=list)
    total_uncompressed_bytes: int = 0
    file_count: int = 0
    limits: ZipLimits = field(default_factory=ZipLimits)

    def as_dict(self) -> Dict[str, object]:
        return {
            "checker_version": ZIP_SAFETY_VERSION,
            "archive_path": self.archive_path,
            "ok": self.ok,
            "problems": [p.as_dict() for p in self.problems],
            "entries": [e.as_dict() for e in self.entries],
            "total_uncompressed_bytes": self.total_uncompressed_bytes,
            "file_count": self.file_count,
            "limits": self.limits.as_dict(),
        }


@dataclass
class ExtractedFile:
    """一个真正落盘的文件（已重新应用白名单与内容检查）。"""

    entry: str
    relative_path: str
    extension: str
    size: int
    sha256: str
    text_like: bool

    def as_dict(self) -> Dict[str, object]:
        return {
            "entry": self.entry,
            "relative_path": self.relative_path,
            "extension": self.extension,
            "size": self.size,
            "sha256": self.sha256,
            "text_like": self.text_like,
        }


@dataclass
class ExtractResult:
    """安全解压的结果。调用方只应相信这里的清单。"""

    archive_path: str
    archive_sha256: str
    target_dir: str
    files: List[ExtractedFile] = field(default_factory=list)
    total_bytes: int = 0
    limits: ZipLimits = field(default_factory=ZipLimits)

    def as_dict(self) -> Dict[str, object]:
        return {
            "extractor_version": ZIP_SAFETY_VERSION,
            "archive_path": self.archive_path,
            "archive_sha256": self.archive_sha256,
            "target_dir": self.target_dir,
            "files": [f.as_dict() for f in self.files],
            "total_bytes": self.total_bytes,
            "limits": self.limits.as_dict(),
        }


def normalize_entry_name(name: str) -> Tuple[str, str]:
    """把 zip 条目名规范成归档内相对 posix 路径。

    返回 ``(规范化路径, 问题代码)``；问题代码为空串表示可接受。
    拒绝：绝对路径、盘符、UNC、``..``、空段、控制字符、超长名。
    """
    if name is None:
        return "", "empty_entry"
    raw = str(name)
    if "\x00" in raw:
        return "", "nul_in_name"
    # Windows 分隔符统一按目录分隔处理，避免 `\..\evil.tex` 绕过检查。
    unified = raw.replace("\\", "/")
    if len(raw) > 4096:
        return "", "name_too_long"
    if unified.startswith("//"):
        return "", "unc_path"
    if unified.startswith("/"):
        return "", "absolute_path"
    if _DRIVE_RE.match(unified):
        return "", "drive_path"
    segments = unified.split("/")
    kept: List[str] = []
    for index, segment in enumerate(segments):
        stripped = segment.strip()
        if stripped in (".", ""):
            # 中间的空段（`a//b`）与 `.` 段一律拒绝或忽略：目录条目允许尾部空段。
            if index == len(segments) - 1 and stripped == "":
                continue
            if stripped == "":
                return "", "empty_segment"
            continue
        if stripped == "..":
            return "", "path_traversal"
        if _CONTROL_RE.search(stripped):
            return "", "control_char"
        if len(stripped.encode("utf-8", "replace")) > 255:
            return "", "name_too_long"
        kept.append(stripped)
    if not kept:
        return "", "empty_entry"
    joined = "/".join(kept)
    if len(joined.encode("utf-8", "replace")) > 512:
        return "", "path_too_long"
    return joined, ""


def sniff_archive(data: bytes) -> str:
    """按文件头判断内容是否本身就是压缩包/归档，返回类型名，否则空串。

    用于抓住"改名成 ``.tex`` 的嵌套压缩包"。只读文件头，不解析内容。
    """
    for signature, offset, kind in _MAGIC_SIGNATURES:
        end = offset + len(signature)
        if len(data) >= end and data[offset:end] == signature:
            return kind
    if (
        len(data) >= _TAR_MAGIC_OFFSET + len(_TAR_MAGIC)
        and data[_TAR_MAGIC_OFFSET:_TAR_MAGIC_OFFSET + len(_TAR_MAGIC)] == _TAR_MAGIC
    ):
        return "tar"
    return ""


def inspect_zip_package(archive_path: object, *, limits: Optional[ZipLimits] = None) -> ZipSafetyReport:
    """按 zip 元数据做静态安全检查（不解压落盘）。

    覆盖：条目名合法性、符号链接位、嵌套压缩包扩展名与内容魔数、
    扩展名白名单、单文件大小、总大小、文件数、压缩比、条目重名。
    """
    limits = limits or ZipLimits()
    archive = Path(str(archive_path))
    report = ZipSafetyReport(archive_path=str(archive), ok=True, limits=limits)
    if not archive.is_file():
        report.ok = False
        report.problems.append(ZipProblem("missing_archive", "", "压缩包文件不存在或不可读"))
        return report
    try:
        zf = zipfile.ZipFile(archive)
    except (zipfile.BadZipFile, OSError) as exc:
        report.ok = False
        report.problems.append(
            ZipProblem("not_a_zip", "", f"无法按 zip 读取：{exc.__class__.__name__}")
        )
        return report

    seen: Dict[str, str] = {}
    with zf:
        for info in zf.infolist():
            name, code = normalize_entry_name(info.filename)
            if code:
                report.problems.append(
                    ZipProblem(code, str(info.filename), _name_problem_message(code))
                )
                continue
            if info.is_dir():
                report.entries.append(
                    ZipEntryReport(name, "", 0, info.compress_size, True, False)
                )
                continue
            if name.lower() in seen:
                report.problems.append(
                    ZipProblem(
                        "duplicate_entry", name,
                        f"归档内出现同名条目（与 {seen[name.lower()]} 重复）",
                    )
                )
                continue
            seen[name.lower()] = name

            # unix 模式下常见的是"只存权限位、不存文件类型位"（S_IFMT 为 0），
            # 那按普通文件接受；真正带类型的非普通文件（链接/设备/套接字）才拒绝。
            mode = info.external_attr >> 16
            if stat.S_ISLNK(mode):
                report.problems.append(
                    ZipProblem("symlink", name, "条目是符号链接，已拒绝",
                               "符号链接可能指向归档外部的文件")
                )
                continue
            if stat.S_IFMT(mode) not in (0, stat.S_IFREG):
                report.problems.append(
                    ZipProblem("irregular_entry", name, "条目不是普通文件，已拒绝")
                )
                continue

            ext = Path(name).suffix.lower()
            if ext in limits.denied_extensions:
                report.problems.append(
                    ZipProblem("denied_extension", name, f"不允许的文件类型：{ext or '（无扩展名）'}")
                )
                continue
            if ext in limits.nested_archive_extensions:
                report.problems.append(
                    ZipProblem("nested_archive", name,
                               f"不允许嵌套压缩包（{ext}），首版不做递归解压")
                )
                continue
            if ext not in limits.allowed_extensions:
                report.problems.append(
                    ZipProblem("not_allowed_extension", name,
                               f"不在允许的类型清单内：{ext or '（无扩展名）'}")
                )
                continue

            if info.file_size > limits.max_single_bytes:
                report.problems.append(
                    ZipProblem("single_file_too_large", name,
                               f"单文件解压后 {info.file_size} 字节，超过上限 "
                               f"{limits.max_single_bytes} 字节")
                )
            if info.file_size >= limits.ratio_min_bytes:
                compressed = max(info.compress_size, 1)
                ratio = info.file_size / compressed
                if ratio > limits.max_compression_ratio:
                    report.problems.append(
                        ZipProblem("compression_ratio", name,
                                   f"压缩比 {ratio:.1f} 倍，超过上限 "
                                   f"{limits.max_compression_ratio:.0f} 倍（疑似解压炸弹）")
                    )

            # 内容嗅探：改名成 .tex 的 zip/rar 也在这一层被抓住。
            try:
                with zf.open(info) as handle:
                    head = handle.read(SNIFF_BYTES)
            except (zipfile.BadZipFile, OSError, RuntimeError) as exc:
                report.problems.append(
                    ZipProblem("unreadable_entry", name,
                               f"条目无法读取：{exc.__class__.__name__}")
                )
                continue
            kind = sniff_archive(head)
            if kind:
                report.problems.append(
                    ZipProblem("nested_archive_content", name,
                               f"条目内容本身是 {kind} 归档，已拒绝（不按文本接收）")
                )
                continue

            report.total_uncompressed_bytes += info.file_size
            report.file_count += 1
            report.entries.append(
                ZipEntryReport(name, ext, info.file_size, info.compress_size,
                               False, ext in limits.text_extensions)
            )

        if report.file_count > limits.max_entries:
            report.problems.append(
                ZipProblem("too_many_entries", "",
                           f"归档内文件数 {report.file_count}，超过上限 {limits.max_entries}")
            )
        if report.total_uncompressed_bytes > limits.max_total_bytes:
            report.problems.append(
                ZipProblem("total_too_large", "",
                           f"解压后总大小 {report.total_uncompressed_bytes} 字节，超过上限 "
                           f"{limits.max_total_bytes} 字节")
            )

    report.ok = not report.problems
    return report


def safe_extract_zip(
    archive_path: object, target_dir: object, *, limits: Optional[ZipLimits] = None
) -> ExtractResult:
    """安全检查通过后逐文件解压到 ``target_dir``。

    任何一项检查失败都抛 :class:`ZipSafetyError`，并删除本次已经写出的文件；
    已存在的同名文件永不覆盖（``O_EXCL``）。
    """
    limits = limits or ZipLimits()
    archive = Path(str(archive_path))
    report = inspect_zip_package(archive, limits=limits)
    if not report.ok:
        raise ZipSafetyError(report.problems)

    target = Path(str(target_dir))
    target.mkdir(parents=True, exist_ok=True)
    target_resolved = target.resolve()

    result = ExtractResult(
        archive_path=str(archive),
        archive_sha256=_sha256_of(archive),
        target_dir=str(target_resolved),
        limits=limits,
    )
    problems: List[ZipProblem] = []
    written: List[str] = []
    total = 0

    with zipfile.ZipFile(archive) as zf:
        for info in zf.infolist():
            name, code = normalize_entry_name(info.filename)
            if code or info.is_dir():
                continue
            ext = Path(name).suffix.lower()
            # 逐文件重新应用白名单：元数据检查与落盘之间不共享任何缓存结论。
            if ext not in limits.allowed_extensions or ext in limits.nested_archive_extensions:
                problems.append(
                    ZipProblem("not_allowed_extension", name,
                               f"不在允许的类型清单内：{ext or '（无扩展名）'}")
                )
                continue

            try:
                with zf.open(info) as handle:
                    data = handle.read(limits.max_single_bytes + 1)
            except (zipfile.BadZipFile, OSError, RuntimeError) as exc:
                problems.append(
                    ZipProblem("unreadable_entry", name,
                               f"条目无法读取：{exc.__class__.__name__}")
                )
                continue
            # 真实字节再判一遍：zip 头部声明的大小可以撒谎。
            if len(data) > limits.max_single_bytes:
                problems.append(
                    ZipProblem("single_file_too_large", name,
                               f"单文件实际解压超过 {limits.max_single_bytes} 字节")
                )
                continue
            total += len(data)
            if total > limits.max_total_bytes:
                problems.append(
                    ZipProblem("total_too_large", "",
                               f"解压后总大小超过上限 {limits.max_total_bytes} 字节")
                )
                break
            if sniff_archive(data[:SNIFF_BYTES]):
                problems.append(
                    ZipProblem("nested_archive_content", name, "条目内容本身是归档，已拒绝")
                )
                continue
            if ext in limits.text_extensions:
                if b"\x00" in data:
                    problems.append(
                        ZipProblem("nul_in_text", name, "文本文件中含 NUL 字节，已拒绝")
                    )
                    continue
                try:
                    data.decode("utf-8")
                except UnicodeDecodeError as exc:
                    problems.append(
                        ZipProblem("not_utf8_text", name,
                                   f"文本文件不是合法 UTF-8（第 {exc.start} 字节附近）",
                                   "首版只接收 UTF-8 文本，不做编码猜测或转码")
                    )
                    continue

            dest = target_resolved / name
            resolved = dest.resolve()
            if not str(resolved).startswith(str(target_resolved) + os.sep):
                problems.append(
                    ZipProblem("path_escape", name, "落盘路径越出解压目录，已拒绝")
                )
                continue
            try:
                dest.parent.mkdir(parents=True, exist_ok=True)
                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
                descriptor = os.open(str(dest), flags, 0o600)
            except FileExistsError:
                problems.append(
                    ZipProblem("target_exists", name,
                               f"解压目录中已存在 {name}，不覆盖已有文件")
                )
                continue
            except OSError as exc:
                problems.append(
                    ZipProblem("write_failed", name, f"写入失败：{exc.__class__.__name__}")
                )
                continue
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(data)
            written.append(name)
            result.files.append(
                ExtractedFile(
                    entry=name,
                    relative_path=name,
                    extension=ext,
                    size=len(data),
                    sha256=hashlib.sha256(data).hexdigest(),
                    text_like=ext in limits.text_extensions,
                )
            )

    if problems:
        # 失败回滚：调用方不会看到半套稿件。已存在的同名文件不属于本次，不删。
        for name in written:
            try:
                (target_resolved / name).unlink()
            except OSError:
                pass
        raise ZipSafetyError(problems)

    result.total_bytes = total
    return result


def _sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(64 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _name_problem_message(code: str) -> str:
    return {
        "empty_entry": "归档内有条目名为空，已拒绝",
        "nul_in_name": "条目名含 NUL 字节，已拒绝",
        "unc_path": "条目名是 UNC 路径，已拒绝",
        "absolute_path": "条目名是绝对路径，已拒绝",
        "drive_path": "条目名含盘符，已拒绝",
        "empty_segment": "条目名含空路径段，已拒绝",
        "path_traversal": "条目名含 .. 路径穿越，已拒绝",
        "control_char": "条目名含控制字符，已拒绝",
        "name_too_long": "条目名过长，已拒绝",
        "path_too_long": "条目完整路径过长，已拒绝",
    }.get(code, f"条目名不合法（{code}）")
