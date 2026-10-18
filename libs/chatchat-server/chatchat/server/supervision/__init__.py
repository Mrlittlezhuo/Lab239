"""师生协作监督侧的稿件静态分析包（里程碑5：LaTeX 安全静态解析）。

四个模块各自独立、全是纯函数 + dataclass，不碰数据库、不碰路由、不调模型：

- ``zip_safety``：压缩包安全检查与安全解压（路径穿越、符号链接、嵌套压缩包、
  解压炸弹、文件数、单文件/总大小、压缩比、扩展名白名单、非 UTF-8 文本）；
- ``latex_parse``：主文件识别（含"多候选让用户选"）与结构解析——章节层级、
  label/ref/cite 引用关系、公式、表格、算法、图片、参考文献与字数统计；
- ``latex_check``：缺失资源与危险命令提示（缺失图片/label/cite key/\\input
  目标；``\\write18``、``\\openout``、``\\catcode``、``\\csname`` 拼接、
  ``\\input`` 绝对路径或 ``|`` 管道等）；
- ``latex_diff``：两个已解析版本的结构差异 → **进度草稿载荷**。

钉死的底线（§6.6、§15.5、§16.6）：

1. **系统绝不调用 LaTeX 编译器**：本包任何模块都不 ``import subprocess``，
   不 shell out ``pdflatex``/``xelatex``/``latexmk``/``bibtex``；
2. **不联网**：不核对宏包是否存在于 TeX 发行版，不抓取任何外部资源；
3. **所有结论必须可定位**：一律带归档内相对路径 + 行号，无法定位的结论不输出；
4. **草稿不等于进展**：差异只生成 ``ProgressDraft``（``published=False``、
   ``requires_student_confirmation=True``），必须学生确认后由业务层发布；
5. **首次上传只建立基线**：没有上一版本时不生成差异，也不推断"完成了什么"；
6. **静态证据不得表述为运行成功**：解析结果属于 E1 文档证据，只说明稿件
   写了这些内容，不证明公式可解、算法已实现或实验已运行。
"""
from chatchat.server.supervision.latex_check import (
    CHECKER_VERSION,
    DANGEROUS_PATTERNS,
    KNOWN_DOCUMENT_CLASSES,
    LatexAnalysis,
    LatexCheckError,
    LatexCheckReport,
    LatexFinding,
    analyze_latex_archive,
    analyze_latex_package,
    check_dangerous_commands,
    check_missing_resources,
    check_unused_resources,
    check_latex_document,
)
from chatchat.server.supervision.latex_diff import (
    BASELINE_NOTE,
    DRAFT_CAVEAT,
    DRAFT_VERSION,
    MAX_DRAFT_ITEMS,
    LatexDiff,
    ProgressDraft,
    ProgressDraftItem,
    ResourceChange,
    SectionChange,
    diff_latex_documents,
)
from chatchat.server.supervision.latex_parse import (
    DEFAULT_MAX_SOURCE_BYTES,
    DEFAULT_MAX_SOURCE_FILES,
    PARSER_VERSION,
    BibEntry,
    BibliographyCommand,
    Citation,
    Equation,
    FloatItem,
    GraphicsInclude,
    InputEdge,
    LabelRef,
    LatexDocument,
    LatexParseError,
    LatexParseResult,
    LatexSourceFile,
    MainFileCandidate,
    MainFileResolution,
    Reference,
    Section,
    SourceLocation,
    count_words,
    detect_main_file,
    list_archive_files,
    load_latex_sources,
    parse_latex_document,
    parse_latex_project,
)
from chatchat.server.supervision.zip_safety import (
    DEFAULT_MAX_COMPRESSION_RATIO,
    DEFAULT_MAX_ENTRIES,
    DEFAULT_MAX_SINGLE_BYTES,
    DEFAULT_MAX_TOTAL_BYTES,
    LATEX_ALLOWED_EXTENSIONS,
    LATEX_IMAGE_EXTENSIONS,
    LATEX_TEXT_EXTENSIONS,
    NESTED_ARCHIVE_EXTENSIONS,
    ZIP_SAFETY_VERSION,
    ExtractResult,
    ExtractedFile,
    ZipEntryReport,
    ZipLimits,
    ZipProblem,
    ZipSafetyError,
    ZipSafetyReport,
    inspect_zip_package,
    normalize_entry_name,
    safe_extract_zip,
    sniff_archive,
)

__all__ = [
    # zip_safety
    "ZIP_SAFETY_VERSION",
    "DEFAULT_MAX_ENTRIES",
    "DEFAULT_MAX_TOTAL_BYTES",
    "DEFAULT_MAX_SINGLE_BYTES",
    "DEFAULT_MAX_COMPRESSION_RATIO",
    "LATEX_ALLOWED_EXTENSIONS",
    "LATEX_TEXT_EXTENSIONS",
    "LATEX_IMAGE_EXTENSIONS",
    "NESTED_ARCHIVE_EXTENSIONS",
    "ZipLimits",
    "ZipProblem",
    "ZipEntryReport",
    "ZipSafetyReport",
    "ExtractedFile",
    "ExtractResult",
    "ZipSafetyError",
    "normalize_entry_name",
    "sniff_archive",
    "inspect_zip_package",
    "safe_extract_zip",
    # latex_parse
    "PARSER_VERSION",
    "DEFAULT_MAX_SOURCE_BYTES",
    "DEFAULT_MAX_SOURCE_FILES",
    "SourceLocation",
    "LatexSourceFile",
    "MainFileCandidate",
    "MainFileResolution",
    "Section",
    "LabelRef",
    "Reference",
    "Citation",
    "Equation",
    "FloatItem",
    "GraphicsInclude",
    "InputEdge",
    "BibEntry",
    "BibliographyCommand",
    "LatexDocument",
    "LatexParseResult",
    "LatexParseError",
    "load_latex_sources",
    "list_archive_files",
    "detect_main_file",
    "parse_latex_document",
    "parse_latex_project",
    "count_words",
    # latex_check
    "CHECKER_VERSION",
    "DANGEROUS_PATTERNS",
    "KNOWN_DOCUMENT_CLASSES",
    "LatexFinding",
    "LatexCheckReport",
    "LatexCheckError",
    "LatexAnalysis",
    "check_missing_resources",
    "check_dangerous_commands",
    "check_unused_resources",
    "check_latex_document",
    "analyze_latex_package",
    "analyze_latex_archive",
    # latex_diff
    "DRAFT_VERSION",
    "MAX_DRAFT_ITEMS",
    "DRAFT_CAVEAT",
    "BASELINE_NOTE",
    "SectionChange",
    "ResourceChange",
    "ProgressDraftItem",
    "ProgressDraft",
    "LatexDiff",
    "diff_latex_documents",
]
