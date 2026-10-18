"""LaTeX 稿件的静态结构解析（§6.6 第2、3条，§15.5 第2、3条）。

钉死的底线（§16.6 第6条）：

- **本模块不调用 LaTeX 编译器**：不 ``import subprocess``，不 shell out
  ``pdflatex``/``xelatex``/``latexmk``/``bibtex``，也不联网核对包是否齐全。
  所有结论都来自对源文件文本的静态扫描；
- 因此解析结果只能说明"稿件里写了这些内容"（E1 文档证据），**不能**说明
  公式可解、算法可实现、实验已运行或结果成立；
- 每一条结论都必须带可定位来源（归档内相对路径 + 行号）。无法定位的
  结论一律不输出。

解析是文本级近似，不是 TeX 解释器：按行去注释后，用正则 + 环境栈扫描。
近似点与解析不了的构造见文件末尾"已知近似"注释。
"""
from __future__ import annotations

import posixpath
import re
from bisect import bisect_right
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from chatchat.server.supervision.zip_safety import (
    LATEX_ALLOWED_EXTENSIONS,
    LATEX_IMAGE_EXTENSIONS,
    LATEX_TEXT_EXTENSIONS,
    ZipLimits,
    normalize_entry_name,
)

#: 解析器版本必须写进业务快照；升级解析器不得静默改变旧稿件的结论。
PARSER_VERSION = "latex-static/1.0"

# 单文件解析预算（量级对齐《AI预审功能详细设计方案》§5.5.2 的 100,000 字符）
DEFAULT_MAX_SOURCE_BYTES = 2 * 1024 * 1024
DEFAULT_MAX_SOURCE_FILES = 300
#: 主文件打分 tie-break 的最小分差；小于它就算"不确定"，交给用户选。
MAIN_SCORE_MARGIN = 20

# ---------------------------------------------------------------------------
# 命令与环境清单
# ---------------------------------------------------------------------------
SECTION_LEVELS: Dict[str, int] = {
    "part": 0,
    "chapter": 1,
    "section": 2,
    "subsection": 3,
    "subsubsection": 4,
    "paragraph": 5,
    "subparagraph": 6,
}
#: 计入"公式"的显示数学环境（去掉尾部 ``*`` 后比较）。
DISPLAY_ENVS: Set[str] = {
    "equation", "eqnarray", "align", "alignat", "gather", "gathered", "multline",
    "flalign", "longeqnarray", "displaymath", "math", "arraymath",
}
FIGURE_ENVS: Set[str] = {"figure", "figure*", "wrapfigure", "subfigure"}
TABLE_ENVS: Set[str] = {"table", "table*", "longtable", "tabular", "tabularx",
                        "array", "sidewaystable", "threeparttable"}
ALGORITHM_ENVS: Set[str] = {"algorithm", "algorithm2e", "algorithmic",
                            "algorithmicx", "algo", "algorithmicH"}
BIB_ENVS: Set[str] = {"thebibliography", "refs", "bibliography"}

_MAIN_FILENAMES: Dict[str, int] = {
    "main.tex": 25, "paper.tex": 15, "thesis.tex": 15, "root.tex": 15,
    "manuscript.tex": 15, "ms.tex": 12, "article.tex": 10, "dissertation.tex": 15,
}

_ENV_RE = re.compile(r"\\(begin|end)\{([^{}\n]+)\}")
_SECTION_RE = re.compile(
    r"\\(subsubsection|subsection|section|subparagraph|paragraph|chapter|part)(\*?)"
)
_LABEL_RE = re.compile(r"\\label\{([^{}]*)\}")
_REF_RE = re.compile(
    r"\\(crefrange|Crefrange|nameref|vref|eqref|autoref|pageref|Cref|cref|ref)"
    r"\*?\s*(?:\[[^\]]*\])?\s*\{([^{}]*)\}"
)
_CITE_RE = re.compile(
    r"\\(nocite|autocite|footcite|textcite|parencite|citeauthor|citeyear|"
    r"citealp|citet|citep|Cite|cite)\*?\s*(?:\[[^\]]*\])*\s*\{([^{}]*)\}"
)
_GRAPHICS_RE = re.compile(r"\\includegraphics\s*(\[[^\]]*\])?\s*\{([^{}]*)\}")
_GRAPHICSPATH_RE = re.compile(r"\\graphicspath\s*\{((?:\s*\{[^{}]*\}\s*)+)\}")
_INPUT_RE = re.compile(r"\\(input|include|subfile|subfileinclude)\s*\*?\s*\{([^{}]*)\}")
_IMPORT_RE = re.compile(
    r"\\(subimport|importfrom|subimportfrom|import)\s*\*?\s*\{([^{}]*)\}\s*\{([^{}]*)\}"
)
_BIBCMD_RE = re.compile(
    r"\\(addbibresource|nobibliography|bibliography|bibliographystyle)\s*"
    r"(?:\[[^\]]*\])?\s*\{([^{}]*)\}"
)
_BIBITEM_RE = re.compile(r"\\bibitem\s*(?:\[[^\]]*\])?\s*\{([^{}]*)\}")
_DOCUMENTCLASS_RE = re.compile(
    r"\\documentclass\s*(\[[^\]]*\])?\s*(?:\{([^{}]*)\})?"
)
_USEPACKAGE_RE = re.compile(r"\\usepackage\s*(\[[^\]]*\])?\s*(?:\{([^{}]*)\})?")
_TITLE_RE = re.compile(r"\\title\s*\{([^{}]*)\}")
_AUTHOR_RE = re.compile(r"\\author\s*\{([^{}]*)\}")
_BIBENTRY_RE = re.compile(r"@\s*([A-Za-z]+)\s*\{\s*([^,\s]+)\s*,")
_BIBFIELD_RE = re.compile(
    r"^\s*([A-Za-z]+)\s*=\s*[{\"]?(.*?)[}\"]?\s*,?\s*$", re.M
)
_CONTROL_SEQ_RE = re.compile(r"\\[a-zA-Z@]+\*?")
_STRIP_CHARS_RE = re.compile(r"[{}~&%$\\]")
_NON_PROSE_COMMANDS = (
    "includegraphics", "graphicspath", "documentclass", "usepackage",
    "bibliographystyle", "addbibresource", "bibliography", "bibitem",
    "label", "pageref", "crefrange", "nameref", "autoref", "eqref",
    "Cref", "cref", "ref", "nocite", "autocite", "footcite", "textcite",
    "parencite", "citeauthor", "citeyear", "citealp", "citet", "citep", "cite",
    "input", "include", "subfile", "subfileinclude", "subimport", "import",
    "url", "href", "hyperref", "tableofcontents", "maketitle", "newpage",
    "clearpage", "pagenumbering", "setlength", "vspace", "hspace", "thanks",
)
_NON_PROSE_RE = re.compile(
    r"\\(?:%s)\s*(?:\[[^\]]*\])*\s*\{[^{}]*\}" % "|".join(_NON_PROSE_COMMANDS)
)
_CJK_RE = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af]")
_LATIN_WORD_RE = re.compile(r"[A-Za-z0-9]+(?:[-'’][A-Za-z0-9]+)*")


class LatexParseError(ValueError):
    """解析前置条件不满足（例如归档里没有任何 .tex 文件）。"""


# ---------------------------------------------------------------------------
# 定位与源文件
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SourceLocation:
    """一条结论的可定位来源：归档内相对路径 + 行号区间 + 原文片段。"""

    path: str
    line: int
    end_line: int = 0
    snippet: str = ""

    def as_dict(self) -> Dict[str, object]:
        return {
            "path": self.path,
            "line": self.line,
            "end_line": self.end_line or self.line,
            "snippet": self.snippet,
        }


@dataclass
class LatexSourceFile:
    """一个已读入的源文件。``read_state`` 说明是否被预算截断。"""

    path: str
    text: str
    code: str  # 去注释后的文本，行结构与 text 完全一致（注释替换为空格）
    lines: List[str] = field(default_factory=list)
    offsets: List[int] = field(default_factory=list)
    read_state: str = "fully_read"  # fully_read / partially_read / not_read
    truncated_at_line: int = 0
    not_read_reason: str = ""

    def __post_init__(self) -> None:
        if not self.lines:
            self.lines = self.text.split("\n")
        if not self.offsets:
            self.offsets = _line_offsets(self.code)

    def line_of(self, offset: int) -> int:
        """字符偏移 → 1 起始行号。"""
        return bisect_right(self.offsets, offset)

    def snippet(self, line: int, limit: int = 120) -> str:
        if line < 1 or line > len(self.lines):
            return ""
        raw = self.lines[line - 1].strip()
        return raw if len(raw) <= limit else raw[: limit - 1] + "…"

    def location(self, offset: int, end_offset: int = -1) -> SourceLocation:
        line = self.line_of(offset)
        end_line = self.line_of(end_offset) if end_offset >= 0 else line
        return SourceLocation(self.path, line, end_line, self.snippet(line))


def _line_offsets(text: str) -> List[int]:
    offsets = [0]
    for index, char in enumerate(text):
        if char == "\n":
            offsets.append(index + 1)
    return offsets


def strip_comments(text: str) -> str:
    r"""去掉 ``%`` 注释，但保持行数与每行长度不变，使偏移仍对应原行号。

    ``\%`` 是转义百分号不算注释；``\\%`` 是换行 + 注释，算注释。
    """
    out: List[str] = []
    for line in text.split("\n"):
        cut = -1
        index = 0
        while index < len(line):
            if line[index] == "\\":
                index += 2
                continue
            if line[index] == "%":
                cut = index
                break
            index += 1
        out.append(line if cut < 0 else line[:cut] + " " * (len(line) - cut))
    return "\n".join(out)


def load_latex_sources(
    root: object,
    *,
    limits: Optional[ZipLimits] = None,
    max_source_bytes: int = DEFAULT_MAX_SOURCE_BYTES,
    max_source_files: int = DEFAULT_MAX_SOURCE_FILES,
) -> Dict[str, LatexSourceFile]:
    """把解压目录里的文本源文件读成 ``{归档内相对路径: LatexSourceFile}``。

    只读白名单内的文本扩展名；不跟随符号链接；超过 ``max_source_bytes`` 的
    文件按"部分读取"标记，绝不静默截断（§5.5.2）。
    """
    limits = limits or ZipLimits()
    base = Path(str(root))
    if not base.is_dir():
        raise LatexParseError(f"解压目录不存在：{base}")
    sources: Dict[str, LatexSourceFile] = {}
    for path in sorted(base.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        relative = path.relative_to(base).as_posix()
        name, code = normalize_entry_name(relative)
        if code or not name:
            continue
        ext = path.suffix.lower()
        if ext not in LATEX_TEXT_EXTENSIONS:
            continue
        if ext not in limits.text_extensions:
            continue
        if len(sources) >= max_source_files:
            sources[name] = LatexSourceFile(
                path=name, text="", code="", lines=[], offsets=[0],
                read_state="not_read",
                not_read_reason=f"文件数超过解析上限 {max_source_files}，未读取",
            )
            continue
        size = path.stat().st_size
        raw = path.read_bytes()
        if b"\x00" in raw:
            sources[name] = LatexSourceFile(
                path=name, text="", code="", lines=[], offsets=[0],
                read_state="not_read", not_read_reason="文件含 NUL 字节，未按文本读取",
            )
            continue
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            sources[name] = LatexSourceFile(
                path=name, text="", code="", lines=[], offsets=[0],
                read_state="not_read",
                not_read_reason=f"不是合法 UTF-8（第 {exc.start} 字节附近）",
            )
            continue
        truncated = False
        if size > max_source_bytes:
            cut = raw[:max_source_bytes]
            text = cut.decode("utf-8", "ignore")
            newline = text.rfind("\n")
            if newline > 0:
                text = text[:newline]
            truncated = True
        if text.startswith("\ufeff"):
            text = text[1:]
        code_text = strip_comments(text)
        sources[name] = LatexSourceFile(
            path=name,
            text=text,
            code=code_text,
            lines=text.split("\n"),
            offsets=_line_offsets(code_text),
            read_state="partially_read" if truncated else "fully_read",
            truncated_at_line=text.count("\n") + 1 if truncated else 0,
            not_read_reason=(
                f"文件 {size} 字节超过单文件解析预算 {max_source_bytes}，"
                "只解析了前部分" if truncated else ""
            ),
        )
    return sources


def list_archive_files(root: object) -> List[str]:
    """列出解压目录内全部文件的归档相对路径（含图片等资源，不解 symlink）。"""
    base = Path(str(root))
    out: List[str] = []
    for path in sorted(base.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        out.append(path.relative_to(base).as_posix())
    return out


# ---------------------------------------------------------------------------
# 主文件识别
# ---------------------------------------------------------------------------


@dataclass
class MainFileCandidate:
    """一个可能是主文件的 .tex，附打分与理由（不确定时给用户看）。"""

    path: str
    has_documentclass: bool
    has_begin_document: bool
    score: int
    reasons: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, object]:
        return {
            "path": self.path,
            "has_documentclass": self.has_documentclass,
            "has_begin_document": self.has_begin_document,
            "score": self.score,
            "reasons": list(self.reasons),
        }


@dataclass
class MainFileResolution:
    """主文件识别结果。``status`` 只有四种：

    - ``resolved``：按规则唯一确定；
    - ``user_specified``：用户指定并校验通过；
    - ``ambiguous``：多个同分候选，必须让用户选（不猜死）；
    - ``not_found`` / ``invalid_selection``：找不到 / 指定的文件不在归档里。
    """

    status: str
    main_path: str
    reason: str
    candidates: List[MainFileCandidate] = field(default_factory=list)

    def as_dict(self) -> Dict[str, object]:
        return {
            "status": self.status,
            "main_path": self.main_path,
            "reason": self.reason,
            "candidates": [c.as_dict() for c in self.candidates],
        }


def _score_main_file(
    path: str, source: LatexSourceFile, referenced_as_child: Set[str]
) -> MainFileCandidate:
    code = source.code
    has_documentclass = _DOCUMENTCLASS_RE.search(code) is not None
    has_begin_document = re.search(r"\\begin\{document\}", code) is not None
    reasons: List[str] = []
    score = 0
    if has_documentclass:
        score += 40
        reasons.append("含 \\documentclass")
    if has_begin_document:
        score += 100
        reasons.append("含 \\begin{document}")
    if re.search(r"\\maketitle\b", code):
        score += 15
        reasons.append("含 \\maketitle")
    if re.search(r"\\begin\{abstract\}", code):
        score += 15
        reasons.append("含摘要环境")
    if _TITLE_RE.search(code):
        score += 10
        reasons.append("含 \\title")
    if re.search(r"\\bibliography\b|\\addbibresource\b|\\begin\{thebibliography\}", code):
        score += 10
        reasons.append("含参考文献命令")
    package_count = len(_USEPACKAGE_RE.findall(code))
    if package_count:
        score += min(package_count * 2, 10)
        reasons.append(f"含 {package_count} 条 \\usepackage")
    include_count = len(_INPUT_RE.findall(code)) + len(_IMPORT_RE.findall(code))
    if include_count:
        score += min(include_count * 5, 25)
        reasons.append(f"含 {include_count} 条 \\input/\\include")
    name = posixpath.basename(path).lower()
    if name in _MAIN_FILENAMES:
        score += _MAIN_FILENAMES[name]
        reasons.append(f"文件名 {name} 常用作主文件")
    if "/" not in path:
        score += 10
        reasons.append("位于归档根目录")
    if path in referenced_as_child:
        score -= 60
        reasons.append("被其他 .tex 以 \\input/\\include 引用，更像子文件")
    return MainFileCandidate(path, has_documentclass, has_begin_document, score, reasons)


def detect_main_file(
    sources: Dict[str, LatexSourceFile], *, preferred: str = ""
) -> MainFileResolution:
    """识别主 ``.tex``；不确定时返回候选列表让用户选，不猜死。

    ``preferred`` 是用户指定的归档相对路径（或文件名）。指定后只做存在性与
    基本校验，校验通过即按用户选择解析。
    """
    tex_paths = sorted(
        path for path, source in sources.items()
        if path.lower().endswith(".tex") and source.read_state != "not_read"
    )
    if not tex_paths:
        return MainFileResolution("not_found", "", "归档里没有可读取的 .tex 文件", [])

    # 被别的 .tex 引用过的文件更可能是子文件（确定性：只看 \input/\include 边）。
    referenced: Set[str] = set()
    for path in tex_paths:
        for target, _offset in _raw_input_targets(sources[path].code):
            resolved = _resolve_relative(path, target, set(tex_paths), {".tex"})
            if resolved and resolved != path:
                referenced.add(resolved)

    candidates = [
        _score_main_file(path, sources[path], referenced) for path in tex_paths
    ]
    primary = [c for c in candidates if c.has_documentclass and c.has_begin_document]

    if preferred:
        wanted = preferred.replace("\\", "/").strip().lstrip("/")
        match = next(
            (c for c in candidates if c.path == wanted
             or posixpath.basename(c.path) == posixpath.basename(wanted)),
            None,
        )
        if match is None:
            return MainFileResolution(
                "invalid_selection", "",
                f"指定的主文件 {preferred} 不在归档的可读 .tex 文件里",
                candidates,
            )
        if not match.has_documentclass:
            return MainFileResolution(
                "invalid_selection", "",
                f"指定的 {match.path} 不含 \\documentclass，不像主文件；"
                "请从候选里另选一个",
                candidates,
            )
        return MainFileResolution(
            "user_specified", match.path,
            f"按用户指定选择 {match.path}", [match],
        )

    if not primary:
        return MainFileResolution(
            "not_found", "",
            "没有找到同时含 \\documentclass 与 \\begin{document} 的主文件；"
            "请手动指定主 .tex",
            candidates,
        )
    # 候选清单始终给全（含子文件），页面才能解释"为什么不是它"。
    ranked = sorted(candidates, key=lambda c: (-c.score, c.path))
    primary.sort(key=lambda c: (-c.score, c.path))
    if len(primary) == 1:
        return MainFileResolution(
            "resolved", primary[0].path,
            f"唯一候选：{'；'.join(primary[0].reasons)}",
            ranked,
        )
    top, second = primary[0], primary[1]
    if top.score - second.score >= MAIN_SCORE_MARGIN:
        return MainFileResolution(
            "resolved", top.path,
            f"多个候选按规则打分后唯一最高（{top.score} 对 {second.score}）："
            f"{'；'.join(top.reasons)}",
            ranked,
        )
    return MainFileResolution(
        "ambiguous", "",
        f"{len(primary)} 个文件都像主文件且得分接近"
        f"（最高 {top.score}，次高 {second.score}，分差小于 {MAIN_SCORE_MARGIN}）；"
        "请选择本次稿件的主 .tex",
        ranked,
    )


def _raw_input_targets(code: str) -> List[Tuple[str, int]]:
    """返回 ``[(原始写法, 字符偏移)]``，偏移用于把 ``\\input`` 归到所属章节。"""
    targets: List[Tuple[str, int]] = []
    for match in _INPUT_RE.finditer(code):
        target = match.group(2).strip()
        if target:
            targets.append((target, match.start()))
    for match in _IMPORT_RE.finditer(code):
        directory, name = match.group(2).strip(), match.group(3).strip()
        joined = posixpath.join(directory, name) if directory else name
        if joined:
            targets.append((joined, match.start()))
    return targets


# ---------------------------------------------------------------------------
# 路径解析
# ---------------------------------------------------------------------------


def _inside_archive(path: str) -> bool:
    return not path.startswith("../") and path != ".." and not posixpath.isabs(path)


def _resolve_relative(
    base_file: str, target: str, available: Set[str], suffixes: Iterable[str]
) -> str:
    """把 ``\\input``/``\\includegraphics`` 里的路径解析成归档内相对路径。

    依次尝试：原样、相对引用文件所在目录、归档根、补扩展名、大小写不敏感、
    归档内唯一同名文件。找不到返回空串（调用方必须报"缺失"，不能猜）。
    """
    target = (target or "").replace("\\", "/").strip()
    if not target:
        return ""
    suffixes = list(suffixes)
    base_dir = posixpath.dirname(base_file)
    attempts: List[str] = []
    for candidate in (target, posixpath.normpath(posixpath.join(base_dir, target)) if base_dir else "", target):
        if not candidate:
            continue
        normalized = posixpath.normpath(candidate)
        if not _inside_archive(normalized):
            continue
        if normalized not in attempts:
            attempts.append(normalized)
    for candidate in attempts:
        if candidate in available:
            return candidate
        for suffix in suffixes:
            if candidate + suffix in available:
                return candidate + suffix
    lowered = {name.lower(): name for name in available}
    for candidate in attempts:
        if candidate.lower() in lowered:
            return lowered[candidate.lower()]
        for suffix in suffixes:
            if (candidate + suffix).lower() in lowered:
                return lowered[(candidate + suffix).lower()]
    by_base: Dict[str, List[str]] = {}
    for name in available:
        by_base.setdefault(posixpath.basename(name).lower(), []).append(name)
    base_name = posixpath.basename(target).lower()
    for suffix in [""] + suffixes:
        matches = by_base.get(base_name + suffix, [])
        if len(matches) == 1:
            return matches[0]
    return ""


# ---------------------------------------------------------------------------
# 文件级扫描
# ---------------------------------------------------------------------------


@dataclass
class _EnvRecord:
    name: str
    start: int
    inner_start: int
    inner_end: int
    end: int


@dataclass
class _SectionEvent:
    name: str
    level: int
    starred: bool
    command_start: int
    body_start: int
    title: str


@dataclass
class _FileScan:
    path: str
    begin_document: int = -1
    end_document: int = -1
    envs: List[_EnvRecord] = field(default_factory=list)
    sections: List[_SectionEvent] = field(default_factory=list)
    labels: List[Tuple[str, int]] = field(default_factory=list)
    references: List[Tuple[str, str, int]] = field(default_factory=list)
    citations: List[Tuple[str, List[str], int]] = field(default_factory=list)
    graphics: List[Tuple[str, str, int]] = field(default_factory=list)
    graphics_paths: List[str] = field(default_factory=list)
    inputs: List[Tuple[str, int]] = field(default_factory=list)
    bib_commands: List[Tuple[str, List[str], int]] = field(default_factory=list)
    bib_items: List[Tuple[str, int]] = field(default_factory=list)
    display_math: List[Tuple[str, int, int]] = field(default_factory=list)
    inline_math: int = 0
    documentclass: str = ""
    title: str = ""
    abstract: str = ""
    code_nomath: str = ""
    warnings: List[str] = field(default_factory=list)


def _scan_envs(code: str, warnings: List[str]) -> List[_EnvRecord]:
    stack: List[Tuple[str, int, int]] = []
    records: List[_EnvRecord] = []
    for match in _ENV_RE.finditer(code):
        kind, name = match.group(1), match.group(2).strip()
        if kind == "begin":
            stack.append((name, match.start(), match.end()))
            continue
        if not stack:
            warnings.append(f"\\end{{{name}}} 没有对应的 \\begin，已忽略")
            continue
        if stack[-1][0] == name:
            begin_name, start, inner_start = stack.pop()
            records.append(_EnvRecord(begin_name, start, inner_start, match.start(), match.end()))
            continue
        matched = -1
        for index in range(len(stack) - 1, -1, -1):
            if stack[index][0] == name:
                matched = index
                break
        if matched >= 0:
            begin_name, start, inner_start = stack.pop(matched)
            records.append(_EnvRecord(begin_name, start, inner_start, match.start(), match.end()))
            warnings.append(f"环境 {name} 与内层环境交叉未闭合，已按同名配对")
        else:
            warnings.append(f"\\end{{{name}}} 没有对应的 \\begin，已忽略")
    for begin_name, start, inner_start in stack:
        records.append(_EnvRecord(begin_name, start, inner_start, len(code), len(code)))
        warnings.append(f"环境 {begin_name} 未闭合，按文件末尾近似处理")
    return records


def _scan_math(code: str, envs: Sequence[_EnvRecord]) -> Tuple[str, List[Tuple[str, int, int]], int]:
    """标出显示/行内数学，返回（数学被空格替换后的文本, 显示数学区间, 行内数学数）。"""
    display: List[Tuple[str, int, int]] = [
        (env.name, env.start, env.end)
        for env in envs
        if env.name.rstrip("*") in DISPLAY_ENVS
    ]
    chars = list(code)

    def blank(start: int, end: int) -> None:
        for index in range(max(start, 0), min(end, len(chars))):
            chars[index] = " "

    for _, start, end in display:
        blank(start, end)
    covered = sorted((start, end) for _, start, end in display)

    def in_display(pos: int) -> bool:
        for start, end in covered:
            if start <= pos < end:
                return True
            if start > pos:
                return False
        return False

    index = 0
    inline = 0
    while index < len(code):
        if in_display(index):
            index += 1
            continue
        if code.startswith("$$", index):
            close = code.find("$$", index + 2)
            if close < 0:
                break
            display.append(("$$", index, close + 2))
            blank(index, close + 2)
            index = close + 2
            continue
        if code.startswith("\\[", index) and (index == 0 or code[index - 1] != "\\"):
            close = code.find("\\]", index + 2)
            if close < 0:
                break
            display.append(("[]", index, close + 2))
            blank(index, close + 2)
            index = close + 2
            continue
        if code.startswith("\\(", index):
            close = code.find("\\)", index + 2)
            if close < 0:
                break
            display.append(("()", index, close + 2))
            blank(index, close + 2)
            index = close + 2
            continue
        if code[index] == "$" and (index == 0 or code[index - 1] != "\\"):
            close = code.find("$", index + 1)
            while close > 0 and code[close - 1] == "\\":
                close = code.find("$", close + 1)
            if close < 0:
                break
            inline += 1
            blank(index, close + 1)
            index = close + 1
            continue
        index += 1
    return "".join(chars), display, inline


def _read_group(text: str, pos: int) -> Tuple[str, int]:
    """从 ``pos`` 读取一个 ``{...}`` 组，返回（内容, 组之后的位置）。"""
    if pos >= len(text) or text[pos] != "{":
        return "", pos
    depth = 0
    index = pos
    while index < len(text):
        char = text[index]
        if char == "\\":
            index += 2
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[pos + 1:index], index + 1
        index += 1
    return text[pos + 1:], len(text)


def _skip_spaces(text: str, pos: int) -> int:
    while pos < len(text) and text[pos] in " \t\r\n":
        pos += 1
    return pos


def _read_optional(text: str, pos: int) -> int:
    if pos < len(text) and text[pos] == "[":
        close = text.find("]", pos)
        if close > pos:
            return close + 1
    return pos


def _scan_file(source: LatexSourceFile) -> _FileScan:
    code = source.code
    scan = _FileScan(path=source.path)
    scan.envs = _scan_envs(code, scan.warnings)
    for env in scan.envs:
        if env.name == "document":
            scan.begin_document = env.start
            scan.end_document = env.end
    scan.code_nomath, scan.display_math, scan.inline_math = _scan_math(code, scan.envs)

    for match in _SECTION_RE.finditer(code):
        name = match.group(1)
        if match.end() < len(code) and code[match.end()].isalpha():
            continue  # \sectioning、\partial 之类不是章节命令
        pos = _skip_spaces(code, match.end())
        title, body_start = _read_group(code, pos)
        if not title and pos < len(code) and code[pos] == "[":
            after = _read_optional(code, pos)
            title, body_start = _read_group(code, _skip_spaces(code, after))
        scan.sections.append(
            _SectionEvent(
                name=name,
                level=SECTION_LEVELS[name],
                starred=bool(match.group(2)),
                command_start=match.start(),
                body_start=body_start,
                title=_clean_text(title),
            )
        )

    for match in _LABEL_RE.finditer(code):
        scan.labels.append((match.group(1).strip(), match.start()))
    for match in _REF_RE.finditer(code):
        for target in _split_keys(match.group(2)):
            scan.references.append((match.group(1), target, match.start()))
    for match in _CITE_RE.finditer(code):
        keys = _split_keys(match.group(2))
        if keys:
            scan.citations.append((match.group(1), keys, match.start()))
    for match in _GRAPHICS_RE.finditer(code):
        scan.graphics.append(
            ((match.group(2) or "").strip(), (match.group(1) or "").strip(), match.start())
        )
    for match in _GRAPHICSPATH_RE.finditer(code):
        for item in re.finditer(r"\{([^{}]*)\}", match.group(1)):
            value = item.group(1).strip()
            if value:
                scan.graphics_paths.append(value)
    scan.inputs = _raw_input_targets(code)
    for match in _BIBCMD_RE.finditer(code):
        names = _split_keys(match.group(2)) if match.group(2) else []
        scan.bib_commands.append((match.group(1), names, match.start()))
    for env in scan.envs:
        if env.name.rstrip("*") in BIB_ENVS:
            for match in _BIBITEM_RE.finditer(code, env.inner_start, env.inner_end):
                scan.bib_items.append((match.group(1).strip(), match.start()))
    class_match = _DOCUMENTCLASS_RE.search(code)
    if class_match and class_match.group(2):
        scan.documentclass = class_match.group(2).strip()
    title_match = _TITLE_RE.search(code)
    if title_match:
        scan.title = _clean_text(title_match.group(1))
    for env in scan.envs:
        if env.name == "abstract":
            scan.abstract = _clean_text(code[env.inner_start:env.inner_end])
            break
    return scan


def _split_keys(raw: str) -> List[str]:
    return [item.strip() for item in (raw or "").split(",") if item.strip()]


def _clean_text(raw: str) -> str:
    text = re.sub(r"\\[a-zA-Z@]+\*?", " ", raw or "")
    text = re.sub(r"[{}~\\]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def count_words(text: str) -> int:
    """近似字数：中日韩单字按 1 个词，拉丁/数字串按 1 个词。

    先去掉数学、环境标记、非正文命令（引用/标签/图形路径等），保留普通
    命令的参数文本，所以 ``\\emph{method}`` 里的 method 会被计入。
    """
    plain = _plain_text(text)
    return len(_CJK_RE.findall(plain)) + len(_LATIN_WORD_RE.findall(plain))


def _plain_text(text: str) -> str:
    value = _ENV_RE.sub(" ", text)
    for _ in range(4):
        stripped = _NON_PROSE_RE.sub(" ", value)
        if stripped == value:
            break
        value = stripped
    value = _CONTROL_SEQ_RE.sub(" ", value)
    value = _STRIP_CHARS_RE.sub(" ", value)
    return value


# ---------------------------------------------------------------------------
# 结构对象
# ---------------------------------------------------------------------------


@dataclass
class Section:
    """一个章节命令。``key`` 在单版本文档内唯一（文件 + 行号）。"""

    key: str
    path: str
    line: int
    end_line: int
    name: str          # part / chapter / section / subsection …
    level: int
    starred: bool
    number: str        # 近似编号，如 "3.2"；未编号章节为空串
    title: str
    parent_key: str
    word_count: int
    included_files: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, object]:
        return {
            "key": self.key,
            "path": self.path,
            "line": self.line,
            "end_line": self.end_line,
            "name": self.name,
            "level": self.level,
            "starred": self.starred,
            "number": self.number,
            "title": self.title,
            "parent_key": self.parent_key,
            "word_count": self.word_count,
            "included_files": list(self.included_files),
        }

    def location(self) -> SourceLocation:
        return SourceLocation(self.path, self.line, self.end_line, self.title[:120])


@dataclass
class LabelRef:
    name: str
    path: str
    line: int
    section_key: str = ""

    def as_dict(self) -> Dict[str, object]:
        return {"name": self.name, "path": self.path, "line": self.line,
                "section_key": self.section_key}


@dataclass
class Reference:
    """``\\ref``/``\\cref``/``\\eqref`` 等对 label 的引用。"""

    command: str
    target: str
    path: str
    line: int
    section_key: str = ""

    def as_dict(self) -> Dict[str, object]:
        return {"command": self.command, "target": self.target, "path": self.path,
                "line": self.line, "section_key": self.section_key}


@dataclass
class Citation:
    command: str
    keys: List[str]
    path: str
    line: int
    section_key: str = ""

    def as_dict(self) -> Dict[str, object]:
        return {"command": self.command, "keys": list(self.keys), "path": self.path,
                "line": self.line, "section_key": self.section_key}


@dataclass
class Equation:
    kind: str  # equation / align / $$ / [] / () …
    label: str
    path: str
    line: int
    end_line: int
    section_key: str = ""

    def as_dict(self) -> Dict[str, object]:
        return {"kind": self.kind, "label": self.label, "path": self.path,
                "line": self.line, "end_line": self.end_line,
                "section_key": self.section_key}


@dataclass
class FloatItem:
    """图、表、算法环境。``kind`` 决定页面分组。"""

    kind: str  # figure / table / algorithm
    name: str  # 环境名，如 figure* / tabular / algorithm2e
    label: str
    caption: str
    path: str
    line: int
    end_line: int
    section_key: str = ""

    def as_dict(self) -> Dict[str, object]:
        return {"kind": self.kind, "name": self.name, "label": self.label,
                "caption": self.caption, "path": self.path, "line": self.line,
                "end_line": self.end_line, "section_key": self.section_key}

    def location(self) -> SourceLocation:
        return SourceLocation(self.path, self.line, self.end_line, self.caption[:120])


@dataclass
class GraphicsInclude:
    raw: str          # 原文写法（常无扩展名）
    options: str
    resolved: str     # 归档内实际文件；空串 = 未找到，必须报缺失
    path: str         # 所在 .tex
    line: int
    section_key: str = ""

    def as_dict(self) -> Dict[str, object]:
        return {"raw": self.raw, "options": self.options, "resolved": self.resolved,
                "path": self.path, "line": self.line, "section_key": self.section_key}


@dataclass
class InputEdge:
    """一条 ``\\input``/``\\include``/``\\import`` 边。``resolved`` 为空 = 归档里找不到。"""

    raw: str
    resolved: str
    path: str
    line: int

    def as_dict(self) -> Dict[str, object]:
        return {"raw": self.raw, "resolved": self.resolved, "path": self.path,
                "line": self.line}


@dataclass
class BibEntry:
    key: str
    entry_type: str
    path: str
    line: int
    fields: Dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, object]:
        return {"key": self.key, "entry_type": self.entry_type, "path": self.path,
                "line": self.line, "fields": dict(self.fields)}


@dataclass
class BibliographyCommand:
    command: str
    names: List[str]
    resolved: List[str]
    path: str
    line: int

    def as_dict(self) -> Dict[str, object]:
        return {"command": self.command, "names": list(self.names),
                "resolved": list(self.resolved), "path": self.path, "line": self.line}


@dataclass
class LatexDocument:
    """一次静态解析的全部结构。所有条目都带文件 + 行号。"""

    main_path: str
    parser_version: str = PARSER_VERSION
    sources: Dict[str, LatexSourceFile] = field(default_factory=dict)
    reachable: List[str] = field(default_factory=list)
    included_by: Dict[str, str] = field(default_factory=dict)
    unreferenced_tex: List[str] = field(default_factory=list)
    title: str = ""
    author: str = ""
    abstract: str = ""
    documentclass: str = ""
    sections: List[Section] = field(default_factory=list)
    labels: List[LabelRef] = field(default_factory=list)
    references: List[Reference] = field(default_factory=list)
    citations: List[Citation] = field(default_factory=list)
    equations: List[Equation] = field(default_factory=list)
    floats: List[FloatItem] = field(default_factory=list)
    graphics: List[GraphicsInclude] = field(default_factory=list)
    input_edges: List[InputEdge] = field(default_factory=list)
    bib_files: List[str] = field(default_factory=list)
    bib_entries: List[BibEntry] = field(default_factory=list)
    bibliography_commands: List[BibliographyCommand] = field(default_factory=list)
    word_count: int = 0
    inline_math_count: int = 0
    parse_warnings: List[str] = field(default_factory=list)

    def counts(self) -> Dict[str, int]:
        return {
            "sections": len(self.sections),
            "labels": len(self.labels),
            "references": len(self.references),
            "citations": len(self.citations),
            "equations": len(self.equations),
            "figures": sum(1 for f in self.floats if f.kind == "figure"),
            "tables": sum(1 for f in self.floats if f.kind == "table"),
            "algorithms": sum(1 for f in self.floats if f.kind == "algorithm"),
            "graphics": len(self.graphics),
            "bib_entries": len(self.bib_entries),
            "word_count": self.word_count,
        }

    def section_by_key(self, key: str) -> Optional[Section]:
        return next((s for s in self.sections if s.key == key), None)

    def outline(self) -> List[str]:
        """给页面用的缩进大纲（带编号、标题、字数与来源）。"""
        return [
            f"{'  ' * s.level}{(s.number + ' ') if s.number else ''}{s.title or '（无标题）'}"
            f"　[{s.word_count} 字，{s.path}:{s.line}]"
            for s in self.sections
        ]

    def as_dict(self) -> Dict[str, object]:
        return {
            "parser_version": self.parser_version,
            "main_path": self.main_path,
            "title": self.title,
            "author": self.author,
            "abstract": self.abstract,
            "documentclass": self.documentclass,
            "reachable": list(self.reachable),
            "included_by": dict(self.included_by),
            "unreferenced_tex": list(self.unreferenced_tex),
            "counts": self.counts(),
            "word_count": self.word_count,
            "inline_math_count": self.inline_math_count,
            "sections": [s.as_dict() for s in self.sections],
            "labels": [x.as_dict() for x in self.labels],
            "references": [x.as_dict() for x in self.references],
            "citations": [x.as_dict() for x in self.citations],
            "equations": [x.as_dict() for x in self.equations],
            "floats": [x.as_dict() for x in self.floats],
            "graphics": [x.as_dict() for x in self.graphics],
            "input_edges": [x.as_dict() for x in self.input_edges],
            "bib_files": list(self.bib_files),
            "bib_entries": [x.as_dict() for x in self.bib_entries],
            "bibliography_commands": [x.as_dict() for x in self.bibliography_commands],
            "parse_warnings": list(self.parse_warnings),
            "read_states": {
                path: {
                    "read_state": src.read_state,
                    "truncated_at_line": src.truncated_at_line,
                    "not_read_reason": src.not_read_reason,
                }
                for path, src in self.sources.items()
            },
        }


# ---------------------------------------------------------------------------
# 解析主流程
# ---------------------------------------------------------------------------


def parse_latex_document(
    sources: Dict[str, LatexSourceFile],
    main_path: str,
    *,
    available_files: Optional[Sequence[str]] = None,
) -> LatexDocument:
    """从主文件出发做可达性分析并解析结构。

    只解析从主文件沿 ``\\input``/``\\include`` 可达的 .tex；其余 .tex 记入
    ``unreferenced_tex``（仍然会被危险命令扫描，但不计入结构统计）。
    """
    if main_path not in sources:
        raise LatexParseError(f"主文件不在已读入的源文件里：{main_path}")
    tex_paths = {
        path for path, src in sources.items()
        if path.lower().endswith(".tex") and src.read_state != "not_read"
    }
    if not tex_paths:
        raise LatexParseError("归档里没有可读取的 .tex 文件")
    available = set(available_files) if available_files is not None else set(sources)
    tex_available = {p for p in available if p.lower().endswith(".tex")}

    scans: Dict[str, _FileScan] = {}
    for path in sorted(tex_paths):
        scans[path] = _scan_file(sources[path])

    # 可达性 + 文档顺序：沿 \input/\include 图递归，在 \input 处下钻，
    # 使被包含文件的章节按真实出现位置插在父文件章节之间（不是简单拼接）。
    sequence, reachable, included_by, walk_warnings = _document_walk(
        main_path, scans, tex_available
    )

    warnings: List[str] = list(walk_warnings)
    for path in sorted(tex_paths):
        for note in scans[path].warnings:
            warnings.append(f"{sources[path].path}: {note}")

    document = LatexDocument(main_path=main_path)
    document.sources = {path: sources[path] for path in sorted(sources)}
    document.reachable = reachable
    document.included_by = included_by
    document.unreferenced_tex = sorted(tex_paths - set(reachable))
    document.parse_warnings = warnings
    main_scan = scans[main_path]
    document.documentclass = main_scan.documentclass
    document.title = main_scan.title
    document.abstract = main_scan.abstract
    author_match = _AUTHOR_RE.search(sources[main_path].code)
    if author_match:
        document.author = _clean_text(author_match.group(1))

    document.sections = _build_sections(sources, scans, sequence, tex_available)
    section_index = _section_index(document.sections)

    for path in reachable:
        source = sources[path]
        scan = scans.get(path)
        if scan is None:
            # 文件存在但没能按文本读取（编码不合法规整）：只登记，不猜结构。
            continue
        for name, offset in scan.labels:
            if name:
                line = source.line_of(offset)
                document.labels.append(
                    LabelRef(name, path, line, _owner_section(section_index, path, line))
                )
        for raw, input_offset in scan.inputs:
            document.input_edges.append(
                InputEdge(raw, _resolve_relative(path, raw, tex_available, [".tex"]),
                          path, source.line_of(input_offset))
            )
        for command, target, offset in scan.references:
            line = source.line_of(offset)
            document.references.append(
                Reference(command, target, path, line,
                          _owner_section(section_index, path, line))
            )
        for command, keys, offset in scan.citations:
            line = source.line_of(offset)
            document.citations.append(
                Citation(command, keys, path, line,
                         _owner_section(section_index, path, line))
            )
        for kind, start, end in scan.display_math:
            label = _first_label(scan, start, end)
            line = source.line_of(start)
            document.equations.append(
                Equation(kind, label, path, line, source.line_of(end),
                         _owner_section(section_index, path, line))
            )
        document.inline_math_count += scan.inline_math
        for env in scan.envs:
            kind = _float_kind(env.name)
            if not kind:
                continue
            if kind == "table" and _inside_figure(scan, env):
                continue
            caption, label = _caption_and_label(scan, sources[path], env)
            line = source.line_of(env.start)
            document.floats.append(
                FloatItem(kind, env.name, label, caption, path, line,
                          source.line_of(env.end),
                          _owner_section(section_index, path, line))
            )

    graphics_available = {p for p in available if _is_image(p)}
    image_suffixes = sorted(LATEX_IMAGE_EXTENSIONS)
    for path in reachable:
        source = sources.get(path)
        scan = scans.get(path)
        if source is None or scan is None:
            continue
        extra_dirs = _graphics_dirs(path, scan)
        for raw, options, offset in scan.graphics:
            cleaned = raw.strip()
            resolved = ""
            # \graphicspath 里的目录先试，再退回引用文件所在目录与归档根。
            for directory in ["", *extra_dirs]:
                candidate = posixpath.join(directory, cleaned) if directory else cleaned
                resolved = _resolve_relative(path, candidate, graphics_available, image_suffixes)
                if resolved:
                    break
            line = source.line_of(offset)
            document.graphics.append(
                GraphicsInclude(cleaned, options.strip("[]"), resolved, path, line,
                                _owner_section(section_index, path, line))
            )

    _collect_bibliography(document, sources, scans, reachable, available)
    document.word_count = _document_word_count(sources, scans, reachable)
    return document


def _float_kind(env_name: str) -> str:
    if env_name in FIGURE_ENVS or env_name.rstrip("*") in {e.rstrip("*") for e in FIGURE_ENVS}:
        return "figure"
    if env_name in ALGORITHM_ENVS or env_name.rstrip("*") in {e.rstrip("*") for e in ALGORITHM_ENVS}:
        return "algorithm"
    if env_name in TABLE_ENVS or env_name.rstrip("*") in {e.rstrip("*") for e in TABLE_ENVS}:
        return "table"
    return ""


def _inside_figure(scan: _FileScan, env: _EnvRecord) -> bool:
    """``tabular`` 出现在 ``table`` 环境里时不重复计数。"""
    if env.name.rstrip("*") not in {"tabular", "tabularx", "array"}:
        return False
    return any(
        other.name.rstrip("*") in {"table", "table*", "longtable", "sidewaystable",
                                   "threeparttable"}
        and other.start <= env.start and env.end <= other.end
        for other in scan.envs
    )


def _first_label(scan: _FileScan, start: int, end: int) -> str:
    for name, offset in scan.labels:
        if start <= offset < end:
            return name
    return ""


def _caption_and_label(scan: _FileScan, source: LatexSourceFile, env: _EnvRecord) -> Tuple[str, str]:
    inner = source.code[env.inner_start:env.inner_end]
    caption_match = re.search(r"\\caption\s*(?:\[[^\]]*\])?\s*\{", inner)
    caption = ""
    if caption_match:
        caption, _ = _read_group(inner, caption_match.end() - 1)
        caption = _clean_text(caption)
    label = ""
    label_match = re.search(r"\\label\{([^{}]*)\}", inner)
    if label_match:
        label = label_match.group(1).strip()
    return caption, label


def _is_image(path: str) -> bool:
    return posixpath.splitext(path)[1].lower() in LATEX_IMAGE_EXTENSIONS


def _graphics_dirs(path: str, scan: _FileScan) -> List[str]:
    r"""``\graphicspath{{fig/}{img/}}`` 里的目录，相对引用文件所在目录。"""
    base_dir = posixpath.dirname(path)
    out: List[str] = []
    for value in scan.graphics_paths:
        value = value.strip().rstrip("/")
        if not value:
            continue
        joined = posixpath.normpath(posixpath.join(base_dir, value)) if base_dir else value
        if _inside_archive(joined):
            out.append(joined)
    return out


def _document_walk(
    main_path: str,
    scans: Dict[str, _FileScan],
    tex_available: Set[str],
    *,
    max_depth: int = 40,
) -> Tuple[List[Tuple[str, _SectionEvent]], List[str], Dict[str, str], List[str]]:
    """沿 ``\\input``/``\\include`` 图递归，产出文档顺序的章节事件。

    返回 ``(章节事件序列[(path, event)], 可达文件(按访问顺序), included_by, 提示)``。
    在 ``\\input`` 处下钻，所以被包含文件的章节会插在父文件章节之间，编号
    才接近成稿顺序；循环引用与超过 ``max_depth`` 的嵌套只提示不展开。
    """
    sequence: List[Tuple[str, _SectionEvent]] = []
    reachable: List[str] = []
    included_by: Dict[str, str] = {}
    warnings: List[str] = []
    visited: Set[str] = set()

    def walk(path: str, depth: int) -> None:
        if path in visited:
            return
        if depth > max_depth:
            warnings.append(f"{path}: \\input 嵌套超过 {max_depth} 层，已停止展开")
            return
        visited.add(path)
        reachable.append(path)
        scan = scans.get(path)
        if scan is None:
            return
        items: List[Tuple[int, str, object]] = [
            (event.command_start, "section", event) for event in scan.sections
        ]
        items.extend((offset, "input", raw) for raw, offset in scan.inputs)
        items.sort(key=lambda item: item[0])
        for _offset, kind, payload in items:
            if kind == "section":
                sequence.append((path, payload))  # type: ignore[arg-type]
                continue
            resolved = _resolve_relative(path, str(payload), tex_available, [".tex"])
            if not resolved or resolved == path:
                continue
            if resolved not in visited:
                included_by[resolved] = path
                walk(resolved, depth + 1)

    walk(main_path, 0)
    return sequence, reachable, included_by, warnings


def _build_sections(
    sources: Dict[str, LatexSourceFile],
    scans: Dict[str, _FileScan],
    sequence: Sequence[Tuple[str, _SectionEvent]],
    tex_available: Set[str],
) -> List[Section]:
    counters: Dict[int, int] = {}
    used_levels: Set[int] = set()
    sections: List[Section] = []
    parents: Dict[int, str] = {}
    for path, event in sequence:
        counters[event.level] = counters.get(event.level, 0) + 1
        for level in list(counters):
            if level > event.level:
                counters[level] = 0
        used_levels.add(event.level)
        number = ""
        if not event.starred:
            parts = [str(counters[level]) for level in sorted(used_levels)
                     if level <= event.level and counters.get(level, 0) > 0]
            number = ".".join(parts)
        parent_key = ""
        for level in sorted(used_levels, reverse=True):
            if level < event.level and parents.get(level):
                parent_key = parents[level]
                break
        source = sources[path]
        scan = scans[path]
        same_file = [e for e in scan.sections if e.command_start > event.command_start]
        next_start = min((e.command_start for e in same_file), default=-1)
        body_end = next_start if next_start >= 0 else _body_end_offset(scan, source)
        line = source.line_of(event.command_start)
        end_line = source.line_of(max(body_end - 1, event.command_start))
        key = f"{path}:{line}"
        body = scan.code_nomath[event.body_start:max(body_end, event.body_start)]
        included: List[str] = []
        for raw, input_offset in scan.inputs:
            if event.command_start <= input_offset < max(body_end, event.body_start):
                resolved = _resolve_relative(path, raw, tex_available, [".tex"])
                if resolved:
                    included.append(resolved)
        sections.append(
            Section(
                key=key,
                path=path,
                line=line,
                end_line=end_line,
                name=event.name,
                level=event.level,
                starred=event.starred,
                number=number,
                title=event.title,
                parent_key=parent_key,
                word_count=count_words(body),
                included_files=sorted(set(included)),
            )
        )
        parents[event.level] = key
        for level in list(parents):
            if level > event.level:
                parents.pop(level, None)
    return sections


def _body_end_offset(scan: _FileScan, source: LatexSourceFile) -> int:
    if scan.end_document >= 0:
        return scan.end_document
    return len(source.code)


def _section_index(sections: Sequence[Section]) -> Dict[str, List[Tuple[int, int, str]]]:
    """path → 按起始行排序的 ``[(起始行, 结束行, section key)]``。"""
    index: Dict[str, List[Tuple[int, int, str]]] = {}
    for section in sections:
        index.setdefault(section.path, []).append(
            (section.line, section.end_line, section.key)
        )
    for path in index:
        index[path].sort(key=lambda item: item[0])
    return index


def _owner_section(
    index: Dict[str, List[Tuple[int, int, str]]], path: str, line: int
) -> str:
    """行号落在哪个章节里就归给哪个章节；取最后一个命中的（即最内层）。

    章节边界按"同一文件内的行区间"近似，跨 ``\\input`` 的章节边界不合并；
    落在任何章节之前的正文返回空串，调用方必须保持"无所属章节"。
    """
    entries = index.get(path)
    if not entries:
        return ""
    owner = ""
    for start, end, key in entries:
        if start > line:
            break
        if start <= line <= max(end, start):
            owner = key
    return owner


def _document_word_count(
    sources: Dict[str, LatexSourceFile],
    scans: Dict[str, _FileScan],
    reachable: Sequence[str],
) -> int:
    total = 0
    for path in reachable:
        source = sources.get(path)
        scan = scans.get(path)
        if source is None or scan is None:
            continue
        start = scan.begin_document if scan.begin_document >= 0 else 0
        end = scan.end_document if scan.end_document >= 0 else len(source.code)
        total += count_words(scan.code_nomath[start:end])
    return total


def _collect_bibliography(
    document: LatexDocument,
    sources: Dict[str, LatexSourceFile],
    scans: Dict[str, _FileScan],
    reachable: Sequence[str],
    available: Set[str],
) -> None:
    tex_paths = {p for p in document.sources if p.lower().endswith(".tex")}
    bib_available = {p for p in available if p.lower().endswith(".bib")}
    resolved_bibs: List[str] = []
    for path in reachable:
        source = sources.get(path)
        scan = scans.get(path)
        if source is None or scan is None:
            continue
        for command, names, offset in scan.bib_commands:
            resolved: List[str] = []
            if command in ("bibliography", "addbibresource", "nobibliography"):
                for name in names:
                    found = _resolve_relative(path, name, bib_available | tex_paths, [".bib"])
                    if found and found.lower().endswith(".bib"):
                        resolved.append(found)
            document.bibliography_commands.append(
                BibliographyCommand(command, names, sorted(set(resolved)), path,
                                    source.line_of(offset))
            )
            resolved_bibs.extend(resolved)
        for key, offset in scans[path].bib_items:
            if key:
                document.bib_entries.append(
                    BibEntry(key, "bibitem", path, source.line_of(offset), {})
                )
    # 被 \bibliography 声明的与归档里本来就有的 .bib 都解析：前者用于核对
    # \cite，后者用于"未被使用的材料"提示（§6.6 示例里的 sample.bib）。
    declared = sorted(set(resolved_bibs) | bib_available)
    for path in declared:
        source = sources.get(path)
        if source is None or source.read_state == "not_read":
            continue
        document.bib_entries.extend(_parse_bib_file(source))
    document.bib_files = declared


def _parse_bib_file(source: LatexSourceFile) -> List[BibEntry]:
    """按 ``@type{key,`` 切分 .bib；字段只取展示用的几个，近似解析。"""
    entries: List[BibEntry] = []
    code = source.code
    for match in _BIBENTRY_RE.finditer(code):
        key = match.group(2).strip()
        if not key:
            continue
        brace = code.find("{", match.start())
        if brace < 0:
            continue
        body, _ = _read_group(code, brace)
        fields: Dict[str, str] = {}
        for field_match in _BIBFIELD_RE.finditer(body):
            name = field_match.group(1).strip().lower()
            value = _clean_text(field_match.group(2))
            if name in ("title", "author", "year", "journal", "booktitle",
                        "publisher", "doi", "eprint") and value:
                fields[name] = value[:300]
        entries.append(
            BibEntry(key, match.group(1).strip().lower(), source.path,
                     source.line_of(match.start()), fields)
        )
    return entries


# ---------------------------------------------------------------------------
# 一站式入口（只做解析，不做检查；检查在 latex_check）
# ---------------------------------------------------------------------------


@dataclass
class LatexParseResult:
    """``parse_latex_project`` 的结果：主文件判定 + 结构（可能为 None）。"""

    root: str
    main: MainFileResolution
    document: Optional[LatexDocument]
    available_files: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, object]:
        return {
            "root": self.root,
            "main": self.main.as_dict(),
            "document": self.document.as_dict() if self.document else None,
            "available_files": list(self.available_files),
        }


def parse_latex_project(
    root: object, *, main_file: str = "", limits: Optional[ZipLimits] = None
) -> LatexParseResult:
    """解析一个已安全解压的稿件目录。

    ``main_file`` 为空时自动识别；识别不确定时 ``document`` 为 ``None``，
    调用方必须把 ``main.candidates`` 交给用户选，不能替用户猜。
    """
    base = Path(str(root))
    sources = load_latex_sources(base, limits=limits)
    available = list_archive_files(base)
    main = detect_main_file(sources, preferred=main_file)
    if not main.main_path:
        return LatexParseResult(str(base), main, None, available)
    document = parse_latex_document(sources, main.main_path, available_files=available)
    return LatexParseResult(str(base), main, document, available)


# ---------------------------------------------------------------------------
# 已知近似（写在这里，页面/文档必须如实说明，不得当成"编译通过"）
# ---------------------------------------------------------------------------
# 1. 不做宏展开：自定义 \newcommand 生成的章节、\input 的宏拼接路径看不到；
# 2. 不处理条件编译：\ifdefined/\newif 分支两侧都会被扫到；
# 3. verbatim/listings 区域里的 % 与 \begin 会被当普通文本（可能多计命令）；
# 4. 章节编号是按出现顺序推算的近似值，与 LaTeX 计数器规则不完全一致，
#    多文件 \input 时按文档顺序线性化；
# 5. 章节边界按同文件行区间近似，跨 \input 的正文不会并入父章节字数；
# 6. 字数是"中日韩单字 + 拉丁词"的近似统计，去掉了数学与非正文命令参数；
# 7. 公式只按显示数学块计数，不解析公式内容，也不判断是否可解；
# 8. 参考文献只解析 .bib 条目与 \bibitem，不做 BibTeX 样式渲染与排序；
# 9. 不核对 \usepackage/\documentclass 是否真实存在于 TeX 发行版（不联网）。
