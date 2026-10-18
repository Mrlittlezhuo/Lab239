"""缺失资源与危险命令的静态提示（§6.6 第4、5条，§16.6 第3、6条）。

钉死的底线（§16.6 第6条）：

- **只提示，不编译、不执行**：本模块不 ``import subprocess``，不调用
  ``pdflatex``/``xelatex``/``latexmk``/``bibtex``，也不联网核对宏包；
  发现 ``\\write18``、``\\directlua``、``\\input{|...}`` 这类命令时，
  系统**只记录并展示**，绝不"顺手编译一下看看"；
- 提示不等于论文有错：``notice`` 级只是"归档里看不到证据"，
  可能由 TeX 发行版或后续版本补齐，必须如实写成"未找到"，不能写成"错误"；
- 每条发现都必须带来源：归档内相对路径 + 行号 + 原文片段。

``severity`` 三档：

- ``error``：正文引用了归档里确实不存在的资源，或出现试图执行外部动作的命令；
- ``warning``：静态上对不上（引用了没有的 label / cite key），或可疑构造；
- ``notice``：材料存在但没被使用、类别无法本地核对等信息性提示。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Set, Tuple

from chatchat.server.supervision.latex_parse import (
    PARSER_VERSION,
    LatexDocument,
    MainFileResolution,
    SourceLocation,
    parse_latex_project,
)
from chatchat.server.supervision.zip_safety import ZipLimits

CHECKER_VERSION = "latex-check/1.0"

#: 常见文档类/参考文献样式：由 TeX 发行版提供，归档里没有也不算缺失。
KNOWN_DOCUMENT_CLASSES: Set[str] = {
    "article", "report", "book", "letter", "slides", "beamer", "IEEEtran",
    "IEEEcompsocconf", "ACM", "acmart", "elsarticle", "svjour3", "svmult",
    "ijcai", "aaai", "neurips", "nips", "icml", "cvpr", "eccv", "jmlr",
    "geojournal", "moderncv", "preview", "standalone", "scrartcl", "scrreprt",
    "ltjarticle", "jos", "cjhe", "thest", "gbt7714", "plainnat", "natbib",
    "abbrv", "alpha", "ieeetr", "unsrtnat", "apalike", "siam", "plantj",
}

# ---------------------------------------------------------------------------
# 危险命令规则表：只匹配、只提示，永不执行
# ---------------------------------------------------------------------------
# (代码, 正则, 严重级, 中文说明)
DANGEROUS_PATTERNS: Tuple[Tuple[str, str, str, str], ...] = (
    ("write18", r"\\write18\b", "error",
     "出现 \\write18：这是 shell escape，会调用外部程序。本系统不编译，也不会代跑"),
    ("write_stream", r"\\(?:immediate\\)?write\s*(?!18)-?\d*\s*(?:\{|\(|\\)", "warning",
     "出现 \\write：向外部文件流写入内容"),
    ("newwrite", r"\\(?:newwrite|newread)\b", "notice",
     "出现 \\newwrite/\\newread：声明了外部文件读写通道"),
    ("openout", r"\\(?:immediate\\)?openout\s*-?\d*\s*=", "warning",
     "出现 \\openout：会打开输出文件"),
    ("openin", r"\\openin\s*-?\d*\s*=", "warning",
     "出现 \\openin：会打开归档外的输入文件"),
    ("read_stream", r"\\(?:immediate\\)?read\s*-?\d*\s+\\[a-zA-Z@]+", "warning",
     "出现 \\read：把外部文件内容读进宏，内容无法静态确定"),
    ("close_stream", r"\\(?:closein|closeout)\b", "notice",
     "出现 \\closein/\\closeout：外部文件通道操作"),
    ("catcode", r"\\catcode\b", "warning",
     "出现 \\catcode：改动字符分类表，会改变后续解析规则"),
    ("csname_exec", r"\\csname[^\n]*\\endcsname", "warning",
     "出现 \\csname…\\endcsname：运行时拼接控制序列，实际执行的命令无法静态确定"),
    ("directlua", r"\\(?:directlua|luaexec|luadirect|luastring)\b", "error",
     "出现 LuaTeX 原语：可直接执行任意代码"),
    ("shell_escape_cmd", r"\\(?:ShellEscape|shellEscape|bash|bashc)\b", "warning",
     "出现外部 shell 命令接口"),
    ("special_primitive", r"\\(?:special|pdfliteral|pdfstream|pdfcolor)\b", "warning",
     "出现 PDF/PostScript 原语：可向文档注入外部内容"),
    ("embed_file", r"\\(?:pdfobj\s+file|pdfembedfile|attachfile|attachfile2)\b", "warning",
     "出现外部文件嵌入命令"),
    ("input_pipe", r"\\(?:input|include|subfile|includegraphics|includegraphics)\s*"
                   r"(?:\[[^\]]*\])?\s*\{[^{}]*\|", "error",
     "引用目标里含 `|`：这是 write18 管道写法，会执行管道右侧的命令"),
    ("input_absolute", r"\\(?:input|include|subfile|import|subimport)\s*"
                       r"(?:\[[^\]]*\])?\s*\{\s*(?:/|[A-Za-z]:[\\/]|\\\\)", "error",
     "以绝对路径或盘符 \\input/\\include：会读取归档之外的文件"),
    ("input_traversal", r"\\(?:input|include|subfile|import|subimport)\s*"
                        r"(?:\[[^\]]*\])?\s*\{[^{}]*\.\.[\\/]", "warning",
     "引用路径含 ..：可能指向归档之外的文件"),
    ("href_external", r"\\(?:href|url|hyperref|Acrobatmenu)\s*(?:\[[^\]]*\])?\s*"
                      r"\{\s*(?:run:|file:|javascript:)", "warning",
     "链接指向本地程序、本地文件或脚本"),
    ("package_write18_option", r"\\usepackage\s*\[[^\]]*\bwrite18\b[^\]]*\]", "error",
     "\\usepackage 选项里请求 write18（shell escape）"),
)

_COMPILED_DANGEROUS: List[Tuple[str, "re.Pattern[str]", str, str]] = [
    (code, re.compile(pattern), severity, message)
    for code, pattern, severity, message in DANGEROUS_PATTERNS
]


class LatexCheckError(ValueError):
    """检查前置条件不满足。"""


@dataclass
class LatexFinding:
    """一条静态检查发现。``path`` + ``line`` 是强制的可定位来源。"""

    kind: str            # missing_graphics / missing_label / missing_citation /
                         # missing_input / missing_bib_resource / missing_class /
                         # unused_resource / unused_bib_entry / unused_tex /
                         # bib_not_found / dangerous_command
    severity: str        # error / warning / notice
    message: str         # 中文说明（用户可见）
    path: str            # 来源文件（归档内相对路径）
    line: int            # 来源行号（1 起始）
    snippet: str = ""    # 原文片段
    target: str = ""     # 缺失的资源/label/cite key，或危险命令代码
    detail: str = ""     # 补充口径，例如"未编译核对"
    end_line: int = 0

    def location(self) -> SourceLocation:
        return SourceLocation(self.path, self.line, self.end_line or self.line, self.snippet)

    def as_dict(self) -> Dict[str, object]:
        return {
            "kind": self.kind,
            "severity": self.severity,
            "message": self.message,
            "path": self.path,
            "line": self.line,
            "end_line": self.end_line or self.line,
            "snippet": self.snippet,
            "target": self.target,
            "detail": self.detail,
        }


@dataclass
class LatexCheckReport:
    """一次静态检查的全部发现。``compiled`` 恒为 ``False``（钉住不编译）。"""

    parser_version: str = PARSER_VERSION
    checker_version: str = CHECKER_VERSION
    ok: bool = True
    compiled: bool = False
    findings: List[LatexFinding] = field(default_factory=list)
    checked_files: List[str] = field(default_factory=list)
    counts: Dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, object]:
        return {
            "parser_version": self.parser_version,
            "checker_version": self.checker_version,
            "ok": self.ok,
            "compiled": self.compiled,          # 恒 False：系统从不编译 LaTeX
            "counts": dict(self.counts),
            "findings": [f.as_dict() for f in self.findings],
            "checked_files": list(self.checked_files),
        }

    def by_severity(self, severity: str) -> List[LatexFinding]:
        return [f for f in self.findings if f.severity == severity]


def _snippet(document: LatexDocument, path: str, line: int) -> str:
    source = document.sources.get(path)
    return source.snippet(line) if source else ""


def check_missing_resources(document: LatexDocument) -> List[LatexFinding]:
    """正文引用了归档里不存在的图片、label、cite key、\\input 目标。"""
    findings: List[LatexFinding] = []
    labels = {label.name for label in document.labels}
    bib_keys = {entry.key for entry in document.bib_entries}

    for item in document.graphics:
        if item.resolved:
            continue
        findings.append(
            LatexFinding(
                kind="missing_graphics",
                severity="error",
                message=f"\\includegraphics 引用的图片在归档里找不到：{item.raw}",
                path=item.path,
                line=item.line,
                snippet=_snippet(document, item.path, item.line),
                target=item.raw,
                detail="按归档内文件名与常见图片扩展名逐个尝试后仍未找到；未编译核对",
            )
        )

    for ref in document.references:
        if ref.target in labels:
            continue
        findings.append(
            LatexFinding(
                kind="missing_label",
                severity="warning",
                message=f"{ref.command} 指向的 label 不存在：{ref.target}",
                path=ref.path,
                line=ref.line,
                snippet=_snippet(document, ref.path, ref.line),
                target=ref.target,
                detail="只在主文件可达的 .tex 里查过 \\label；宏生成的 label 静态看不到",
            )
        )

    cited_keys = {key for citation in document.citations for key in citation.keys}
    if cited_keys and not bib_keys:
        first = document.citations[0]
        findings.append(
            LatexFinding(
                kind="bib_not_found",
                severity="warning",
                message="正文有引用命令，但归档里没找到任何 .bib 条目或 \\bibitem，"
                        "无法核对引用是否齐全",
                path=first.path,
                line=first.line,
                snippet=_snippet(document, first.path, first.line),
                target="",
                detail=f"共 {len(cited_keys)} 个 cite key 未核对",
            )
        )
    else:
        for citation in document.citations:
            for key in citation.keys:
                if key in bib_keys:
                    continue
                findings.append(
                    LatexFinding(
                        kind="missing_citation",
                        severity="warning",
                        message=f"{citation.command} 引用的参考文献条目不存在：{key}",
                        path=citation.path,
                        line=citation.line,
                        snippet=_snippet(document, citation.path, citation.line),
                        target=key,
                        detail="只在归档内的 .bib 与 \\bibitem 里查过；未联网核对",
                    )
                )

    for edge in document.input_edges:
        if edge.resolved:
            continue
        findings.append(
            LatexFinding(
                kind="missing_input",
                severity="error",
                message=f"\\input/\\include 的目标在归档里找不到：{edge.raw}",
                path=edge.path,
                line=edge.line,
                snippet=_snippet(document, edge.path, edge.line),
                target=edge.raw,
                detail="按同目录、归档根与补 .tex 扩展名尝试后仍未找到",
            )
        )

    for command in document.bibliography_commands:
        if command.command not in ("bibliography", "addbibresource", "nobibliography"):
            continue
        resolved = set(command.resolved)
        for name in command.names:
            if any(posix_basename(found) == posix_basename(name) for found in resolved):
                continue
            findings.append(
                LatexFinding(
                    kind="missing_bib_resource",
                    severity="error",
                    message=f"\\{command.command} 声明的参考文献文件在归档里找不到：{name}",
                    path=command.path,
                    line=command.line,
                    snippet=_snippet(document, command.path, command.line),
                    target=name,
                )
            )

    if document.documentclass and document.documentclass not in KNOWN_DOCUMENT_CLASSES:
        cls_name = f"{document.documentclass}.cls"
        if cls_name not in document.sources:
            main = document.sources.get(document.main_path)
            line = 1
            if main:
                for index, text_line in enumerate(main.lines):
                    if "\\documentclass" in text_line:
                        line = index + 1
                        break
            findings.append(
                LatexFinding(
                    kind="missing_class",
                    severity="notice",
                    message=f"文档类 {document.documentclass} 不在归档里",
                    path=document.main_path,
                    line=line,
                    snippet=_snippet(document, document.main_path, line),
                    target=document.documentclass,
                    detail="可能由 TeX 发行版提供；本系统不联网、不编译，无法核对",
                )
            )
    return findings


def posix_basename(path: str) -> str:
    return path.replace("\\", "/").rstrip("/").split("/")[-1].lower()


def check_dangerous_commands(document: LatexDocument) -> List[LatexFinding]:
    """扫描所有 .tex（含主文件不可达的文件）里的危险构造。

    只登记与展示：本系统不编译 LaTeX，也不会替用户执行任何命令。
    """
    findings: List[LatexFinding] = []
    for path in sorted(document.sources):
        if not path.lower().endswith(".tex"):
            continue
        source = document.sources[path]
        if source.read_state == "not_read" or not source.code:
            continue
        code = source.code
        for code_id, pattern, severity, message in _COMPILED_DANGEROUS:
            for match in pattern.finditer(code):
                line = source.line_of(match.start())
                findings.append(
                    LatexFinding(
                        kind="dangerous_command",
                        severity=severity,
                        message=message,
                        path=path,
                        line=line,
                        end_line=source.line_of(match.end()),
                        snippet=source.snippet(line),
                        target=code_id,
                        detail="静态文本匹配；未编译、未执行，也不判断该命令是否真的生效",
                    )
                )
    # 同一行同一规则只报一次，避免刷屏。
    deduped: Dict[Tuple[str, str, int], LatexFinding] = {}
    for finding in findings:
        deduped.setdefault((finding.path, finding.target, finding.line), finding)
    return list(deduped.values())


def check_unused_resources(document: LatexDocument) -> List[LatexFinding]:
    """归档里存在、但本次成稿没用到的材料（§6.6 示例里的 sample.bib / 未引用图）。"""
    findings: List[LatexFinding] = []
    used_images = {item.resolved for item in document.graphics if item.resolved}
    for path in sorted(document.sources):
        if not path.lower().endswith((".png", ".jpg", ".jpeg", ".pdf", ".eps",
                                      ".gif", ".bmp", ".tif", ".tiff", ".svg")):
            continue
        if path in used_images:
            continue
        findings.append(
            LatexFinding(
                kind="unused_resource",
                severity="notice",
                message=f"归档里有这张图片，但当前正文没有引用它：{path}",
                path=path,
                line=1,
                target=path,
                detail="也可能被注释掉的插图或被其它未纳入本次主文件的内容使用",
            )
        )

    cited = {key for citation in document.citations for key in citation.keys}
    for entry in document.bib_entries:
        if entry.key in cited:
            continue
        findings.append(
            LatexFinding(
                kind="unused_bib_entry",
                severity="notice",
                message=f"参考文献条目未被正文引用：{entry.key}",
                path=entry.path,
                line=entry.line,
                snippet=_snippet(document, entry.path, entry.line),
                target=entry.key,
                detail="使用 \\nocite 或宏生成的引用时静态看不到，故只作提示",
            )
        )

    declared = {found for command in document.bibliography_commands for found in command.resolved}
    for path in sorted(document.bib_files):
        if path in declared:
            continue
        findings.append(
            LatexFinding(
                kind="unused_resource",
                severity="notice",
                message=f"归档里有这个 .bib，但没有被 \\bibliography/\\addbibresource 使用：{path}",
                path=path,
                line=1,
                target=path,
            )
        )

    for path in sorted(document.unreferenced_tex):
        findings.append(
            LatexFinding(
                kind="unused_tex",
                severity="notice",
                message=f"这个 .tex 没有被主文件沿 \\input/\\include 用到：{path}",
                path=path,
                line=1,
                target=path,
                detail="可能是备用章节、旧版本或需要用户另选主文件",
            )
        )
    return findings


def check_latex_document(document: LatexDocument) -> LatexCheckReport:
    """缺失资源 + 危险命令 + 未使用材料的完整静态检查（不编译）。"""
    findings = (
        check_missing_resources(document)
        + check_dangerous_commands(document)
        + check_unused_resources(document)
    )
    order = {"error": 0, "warning": 1, "notice": 2}
    findings.sort(key=lambda f: (order.get(f.severity, 3), f.path, f.line, f.kind))
    counts: Dict[str, int] = {}
    for finding in findings:
        counts[finding.severity] = counts.get(finding.severity, 0) + 1
        counts[finding.kind] = counts.get(finding.kind, 0) + 1
    checked = sorted(
        {path for path in document.sources if path.lower().endswith(".tex")}
    )
    return LatexCheckReport(
        ok=not any(f.severity == "error" for f in findings),
        compiled=False,  # 恒 False：系统绝不调用 LaTeX 编译器
        findings=findings,
        checked_files=checked,
        counts=counts,
    )


@dataclass
class LatexAnalysis:
    """一个稿件版本的完整静态分析结果（主文件判定 + 结构 + 检查）。"""

    root: str
    main: MainFileResolution
    document: Optional[LatexDocument] = None
    check: Optional[LatexCheckReport] = None
    available_files: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, object]:
        return {
            "root": self.root,
            "parser_version": PARSER_VERSION,
            "checker_version": CHECKER_VERSION,
            "main": self.main.as_dict(),
            "document": self.document.as_dict() if self.document else None,
            "check": self.check.as_dict() if self.check else None,
            "available_files": list(self.available_files),
        }


def analyze_latex_archive(
    archive_path: object,
    target_dir: object,
    *,
    main_file: str = "",
    limits: Optional[ZipLimits] = None,
) -> Tuple[object, LatexAnalysis]:
    """安全解压 + 静态分析。解压未通过时抛 ``ZipSafetyError``，不落半套文件。

    返回 ``(ExtractResult, LatexAnalysis)``：前者供快照记录文件清单与哈希，
    后者供结构展示与进度草稿。
    """
    from chatchat.server.supervision.zip_safety import safe_extract_zip

    extracted = safe_extract_zip(archive_path, target_dir, limits=limits)
    analysis = analyze_latex_package(
        extracted.target_dir, main_file=main_file, limits=limits
    )
    return extracted, analysis


def analyze_latex_package(
    root: object, *, main_file: str = "", limits: Optional[ZipLimits] = None
) -> LatexAnalysis:
    """安全解压后的目录 → 主文件判定 + 结构解析 + 静态检查。

    主文件不确定时 ``document``/``check`` 均为 ``None``，调用方必须把
    ``main.candidates`` 交给学生选，不能替学生猜（§16.6 第2条）。
    """
    parsed = parse_latex_project(root, main_file=main_file, limits=limits)
    check = check_latex_document(parsed.document) if parsed.document else None
    return LatexAnalysis(
        root=parsed.root,
        main=parsed.main,
        document=parsed.document,
        check=check,
        available_files=parsed.available_files,
    )
