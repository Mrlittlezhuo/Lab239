"""LaTeX 安全静态解析离线测试（《学生研究与导师审核功能重新设计方案》里程碑5）。

按 §16.6 的六条放行标准组织：

1. ZIP 路径穿越、链接、解压炸弹、超量文件和危险类型被拒绝；
2. 能识别主文件，多主文件时返回候选并支持用户指定；
3. 能展示章节结构、引用关系、资源缺失与版本差异；
4. 首次上传只建立基线（无差异）；
5. 差异产出的是草稿载荷，字段明确表明"未发布、必须学生确认"；
6. 全程不调用 LaTeX 编译器，也不声称论文中的结果已被运行验证。

夹具全部用 ``zipfile`` 现场生成在 ``tmp_path`` 里，不提交二进制夹具；
不联网、不调模型、不落业务数据库。
"""
import os
import re
import stat
import subprocess
import zipfile
from pathlib import Path
from typing import Dict, Tuple

import pytest

from chatchat.server.supervision import (
    LatexAnalysis,
    ProgressDraft,
    ZipLimits,
    ZipSafetyError,
    analyze_latex_package,
    check_dangerous_commands,
    check_latex_document,
    detect_main_file,
    diff_latex_documents,
    inspect_zip_package,
    list_archive_files,
    load_latex_sources,
    parse_latex_document,
    parse_latex_project,
    safe_extract_zip,
)

MODULE_DIR = Path(__file__).resolve().parents[2] / "chatchat" / "server" / "supervision"
MODULE_FILES = [
    MODULE_DIR / name
    for name in ("__init__.py", "zip_safety.py", "latex_parse.py", "latex_check.py",
                 "latex_diff.py")
]


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------


def write_zip(path: Path, entries: Dict[str, bytes], *, symlinks: Tuple[str, ...] = ()) -> Path:
    """按名字与字节内容现场生成 zip；``symlinks`` 里的条目带 unix 符号链接位。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in entries.items():
            info = zipfile.ZipInfo(name, date_time=(2026, 10, 18, 12, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            if name in symlinks:
                info.create_system = 3  # unix
                info.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(info, data)
    return path


def text(value: str) -> bytes:
    return value.encode("utf-8")


PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64


def paper_entries(version: int = 1) -> Dict[str, bytes]:
    """一个 IEEE 会议论文工程；``version=2`` 增加/删除章节、图、公式与引用。"""
    main = r"""
\documentclass[conference]{IEEEtran}
\usepackage{graphicx}
\usepackage{amsmath}
\graphicspath{{figs/}}
\title{SA2N Positioning}
\author{Zhang San}
\begin{document}
\maketitle
\begin{abstract}
本文研究毫米波定位方法。
\end{abstract}
\section{引言}
如表~\ref{tab:setup} 所示，并引用~\cite{smith2020}。
缺失的 label~\ref{fig:nope}，缺失的引用~\cite{ghost2021}。
\input{sections/model}
\input{sections/gone}
\section{仿真结果}
\begin{figure}[h]
\includegraphics[width=0.8\columnwidth]{results-1}
\caption{定位 RMSE}
\label{fig:results}
\end{figure}
\begin{figure}[h]
\includegraphics{absent-figure}
\caption{归档里没有的图}
\label{fig:absent}
\end{figure}
\begin{table}[h]
\begin{tabular}{cc}\hline 参数 & 值\\\hline 频率 & 28 GHz\\\hline\end{tabular}
\caption{实验参数}
\label{tab:setup}
\end{table}
\begin{equation}\label{eq:rmse}
E=\sqrt{\frac{1}{N}\sum_{i} r_i^{2}}
\end{equation}
\begin{algorithm}[H]
\caption{SA2N AoA 估计}
\label{alg:sa2n}
\end{algorithm}
"""
    if version == 1:
        main += r"""\includegraphics{Geometric_model}
\section{旧版讨论}
这一节在第二个版本里被删掉了，用来验证删除差异。
"""
    else:
        main += r"""\section{角偏移估计}
新增的一节，正文内容明显变多了 with quite a few more words added here.
\begin{figure}[h]
\includegraphics{results-2}
\caption{角偏移 RMSE}
\label{fig:offset}
\end{figure}
\begin{equation}\label{eq:offset}
\Delta\theta = \arctan\frac{y}{x}
\end{equation}
再引用~\cite{li2019}。
"""
    main += r"""\bibliographystyle{IEEEtran}
\bibliography{ref}
\end{document}
"""
    model = r"""\section{系统模型}
如图~\ref{fig:results} 所示，这里写一些正文内容 content words for counting.
"""
    if version >= 2:
        model += r"""\subsection{信道模型}
新增的小节，补充了更多正文内容 with more prose added here.
"""
    bib = (
        "@article{smith2020,\n"
        "  title = {Millimeter wave positioning},\n"
        "  author = {Smith, A.},\n"
        "  year = {2020},\n"
        "}\n"
    )
    if version >= 2:
        bib += (
            "@inproceedings{li2019,\n"
            "  title = {AoA estimation},\n"
            "  author = {Li, B.},\n"
            "  year = {2019},\n"
            "}\n"
        )
    return {
        "main.tex": text(main),
        "sections/model.tex": text(model),
        "figs/results-1.png": PNG,
        "figs/results-2.png": PNG,
        "figs/Geometric_model.png": PNG,
        "ref.bib": text(bib),
        "sample.bib": text(
            "@article{unused2018,\n  title = {Never cited},\n  author = {Wang, C.},\n"
            "  year = {2018},\n}\n"
        ),
    }


def extract_paper(tmp_path: Path, version: int = 1):
    archive = write_zip(tmp_path / f"paper-v{version}.zip", paper_entries(version))
    target = tmp_path / f"extract-v{version}"
    return safe_extract_zip(archive, target)


def problem_codes(error: ZipSafetyError) -> set:
    return {problem.code for problem in error.problems}


def finding_kinds(findings) -> set:
    return {finding.kind for finding in findings}


# ---------------------------------------------------------------------------
# 1. ZIP 安全检查
# ---------------------------------------------------------------------------


def test_zip_rejects_path_traversal_and_absolute_paths(tmp_path: Path) -> None:
    for name in ("../evil.tex", "a/../../evil.tex", "/etc/passwd.tex",
                 "C:\\evil.tex", "\\\\server\\share\\evil.tex"):
        archive = write_zip(tmp_path / f"trav-{abs(hash(name))}.zip",
                            {name: text("\\documentclass{article}")})
        with pytest.raises(ZipSafetyError) as caught:
            safe_extract_zip(archive, tmp_path / f"out-{abs(hash(name))}")
        assert problem_codes(caught.value) & {
            "path_traversal", "absolute_path", "drive_path", "unc_path"
        }, caught.value.problems


def test_zip_rejects_symlink_entries(tmp_path: Path) -> None:
    archive = write_zip(
        tmp_path / "symlink.zip",
        {"main.tex": text("\\documentclass{article}\n\\begin{document}\n\\end{document}\n"),
         "link.tex": text("/etc/passwd")},
        symlinks=("link.tex",),
    )
    with pytest.raises(ZipSafetyError) as caught:
        safe_extract_zip(archive, tmp_path / "out")
    assert "symlink" in problem_codes(caught.value)


def test_zip_rejects_decompression_bomb(tmp_path: Path) -> None:
    # 5 MB 的同字节内容：单文件没超 10 MB 上限，但压缩比远超阈值。
    archive = write_zip(tmp_path / "bomb.zip", {"huge.tex": b"A" * 5_000_000})
    with pytest.raises(ZipSafetyError) as caught:
        safe_extract_zip(archive, tmp_path / "out")
    assert "compression_ratio" in problem_codes(caught.value)

    # 展开总体积超上限：按部署参数另设更小的预算也必须被拦住。
    big = write_zip(tmp_path / "wide.zip",
                    {f"p{i}.tex": text("x" * 4000) for i in range(5)})
    with pytest.raises(ZipSafetyError) as caught:
        safe_extract_zip(big, tmp_path / "out2",
                         limits=ZipLimits(max_total_bytes=8 * 1024))
    assert "total_too_large" in problem_codes(caught.value)

    # 单文件超上限。
    single = write_zip(tmp_path / "single.zip", {"p.tex": text("x" * 5000)})
    with pytest.raises(ZipSafetyError) as caught:
        safe_extract_zip(single, tmp_path / "out3",
                         limits=ZipLimits(max_single_bytes=1024))
    assert "single_file_too_large" in problem_codes(caught.value)


def test_zip_rejects_too_many_files(tmp_path: Path) -> None:
    archive = write_zip(tmp_path / "many.zip",
                        {f"p{i}.tex": text("\\documentclass{article}") for i in range(12)})
    report = inspect_zip_package(archive, limits=ZipLimits(max_entries=10))
    assert not report.ok
    assert "too_many_entries" in {p.code for p in report.problems}
    with pytest.raises(ZipSafetyError) as caught:
        safe_extract_zip(archive, tmp_path / "out", limits=ZipLimits(max_entries=10))
    assert "too_many_entries" in problem_codes(caught.value)


def test_zip_rejects_dangerous_and_unlisted_types(tmp_path: Path) -> None:
    archive = write_zip(
        tmp_path / "types.zip",
        {
            "main.tex": text("\\documentclass{article}\n\\begin{document}\n\\end{document}\n"),
            "evil.exe": b"MZ\x90\x00",
            "run.sh": text("#!/bin/sh\nid\n"),
            "inner.zip": b"PK\x03\x04" + b"0" * 40,
            "disguised.tex": b"PK\x03\x04" + b"0" * 60,   # 改名成 .tex 的 zip
            "plot.bin": b"\x00\x01\x02\x03",
        },
    )
    with pytest.raises(ZipSafetyError) as caught:
        safe_extract_zip(archive, tmp_path / "out")
    codes = problem_codes(caught.value)
    assert "denied_extension" in codes        # .exe
    assert "not_allowed_extension" in codes   # .sh / .bin
    assert "nested_archive" in codes          # inner.zip
    assert "nested_archive_content" in codes  # disguised.tex 内容其实是 zip


def test_zip_rejects_non_utf8_or_nul_text(tmp_path: Path) -> None:
    archive = write_zip(
        tmp_path / "encoding.zip",
        {"main.tex": text("\\documentclass{article}"),
         "broken.tex": b"\\section{\xff\xfe not utf8}",
         "nul.tex": b"\\section{a\x00b}"},
    )
    with pytest.raises(ZipSafetyError) as caught:
        safe_extract_zip(archive, tmp_path / "out")
    assert {"not_utf8_text", "nul_in_text"} <= problem_codes(caught.value)


def test_safe_extract_never_overwrites_and_rolls_back(tmp_path: Path) -> None:
    first = write_zip(tmp_path / "a.zip", {"main.tex": text("第一版")})
    target = tmp_path / "out"
    safe_extract_zip(first, target)
    original = (target / "main.tex").read_text(encoding="utf-8")

    second = write_zip(tmp_path / "b.zip",
                       {"main.tex": text("覆盖内容"), "extra.tex": text("新文件")})
    with pytest.raises(ZipSafetyError) as caught:
        safe_extract_zip(second, target)
    assert "target_exists" in problem_codes(caught.value)
    # 已有文件不被覆盖，本次写出的文件被回滚。
    assert (target / "main.tex").read_text(encoding="utf-8") == original
    assert not (target / "extra.tex").exists()


def test_safe_extract_reports_sha_and_reapplies_whitelist(tmp_path: Path) -> None:
    result = extract_paper(tmp_path, 1)
    assert result.files and result.total_bytes > 0
    assert all(item.sha256 for item in result.files)
    names = {item.relative_path for item in result.files}
    assert "sections/model.tex" in names and "figs/results-1.png" in names
    # 解压目录里不出现任何被拒绝的类型。
    assert not any(Path(name).suffix.lower() in {".exe", ".sh", ".zip"} for name in names)


# ---------------------------------------------------------------------------
# 2. 主文件识别
# ---------------------------------------------------------------------------


def test_main_file_is_detected_and_children_penalised(tmp_path: Path) -> None:
    extract_paper(tmp_path, 1)
    root = tmp_path / "extract-v1"
    sources = load_latex_sources(root)
    resolution = detect_main_file(sources)
    assert resolution.status == "resolved"
    assert resolution.main_path == "main.tex"
    assert resolution.candidates
    child = next(c for c in resolution.candidates if c.path == "sections/model.tex")
    assert not child.has_begin_document


def test_multiple_main_files_return_candidates_and_user_can_choose(tmp_path: Path) -> None:
    root = tmp_path / "two-mains"
    root.mkdir()
    body = "\\documentclass{article}\n\\begin{document}\n正文\n\\end{document}\n"
    (root / "main.tex").write_text(body, encoding="utf-8")
    (root / "paper.tex").write_text(body, encoding="utf-8")

    ambiguous = parse_latex_project(root)
    assert ambiguous.main.status == "ambiguous"
    assert ambiguous.document is None
    paths = [c.path for c in ambiguous.main.candidates]
    assert paths == ["main.tex", "paper.tex"]

    chosen = parse_latex_project(root, main_file="paper.tex")
    assert chosen.main.status == "user_specified"
    assert chosen.main.main_path == "paper.tex"
    assert chosen.document is not None

    invalid = parse_latex_project(root, main_file="nope.tex")
    assert invalid.main.status == "invalid_selection"
    assert invalid.document is None
    assert invalid.main.candidates


def test_no_main_file_is_reported_not_guessed(tmp_path: Path) -> None:
    root = tmp_path / "no-main"
    root.mkdir()
    (root / "chapter.tex").write_text("\\section{只有章节}\n", encoding="utf-8")
    result = parse_latex_project(root)
    assert result.main.status == "not_found"
    assert result.document is None


# ---------------------------------------------------------------------------
# 3. 结构、引用关系、资源缺失与版本差异
# ---------------------------------------------------------------------------


def test_structure_and_reference_graph(tmp_path: Path) -> None:
    extract_paper(tmp_path, 1)
    root = tmp_path / "extract-v1"
    parsed = parse_latex_project(root)
    document = parsed.document
    assert document is not None

    titles = [s.title for s in document.sections]
    assert "引言" in titles and "系统模型" in titles and "仿真结果" in titles
    # \input 进来的章节按文档顺序插在中间，编号连续可解释。
    numbers = {s.title: s.number for s in document.sections}
    assert numbers["系统模型"] == "2" and numbers["仿真结果"] == "3"
    for section in document.sections:
        assert section.path and section.line >= 1 and section.end_line >= section.line
    assert any(s.word_count > 0 for s in document.sections)

    # 章节层级：subsection 挂在所属 section 之下。
    model_doc = parse_latex_document(
        load_latex_sources(root), "main.tex", available_files=list_archive_files(root)
    )
    assert model_doc.included_by.get("sections/model.tex") == "main.tex"

    labels = {label.name for label in document.labels}
    assert {"fig:results", "tab:setup", "eq:rmse", "alg:sa2n"} <= labels
    refs = {ref.target for ref in document.references}
    assert "fig:nope" in refs
    cited = {key for citation in document.citations for key in citation.keys}
    assert {"smith2020", "ghost2021"} <= cited

    counts = document.counts()
    assert counts["figures"] == 2 and counts["tables"] == 1
    assert counts["algorithms"] == 1 and counts["equations"] == 1
    # ref.bib 与 sample.bib 都被解析出来，才能报"未使用的参考文献条目"。
    assert {"smith2020", "unused2018"} <= {e.key for e in document.bib_entries}
    assert document.word_count > 0

    resolved = {g.resolved for g in document.graphics if g.resolved}
    assert "figs/results-1.png" in resolved and "figs/Geometric_model.png" in resolved
    assert document.outline() and all("main.tex" in line or "sections/" in line
                                      for line in document.outline())


def test_missing_resources_are_reported_with_locations(tmp_path: Path) -> None:
    extract_paper(tmp_path, 1)
    analysis = analyze_latex_package(tmp_path / "extract-v1")
    assert isinstance(analysis, LatexAnalysis)
    assert analysis.check is not None and analysis.check.compiled is False
    findings = analysis.check.findings
    kinds = finding_kinds(findings)
    assert "missing_graphics" in kinds      # absent-figure
    assert "missing_label" in kinds         # fig:nope
    assert "missing_citation" in kinds      # ghost2021
    assert "missing_input" in kinds         # sections/gone
    assert "unused_bib_entry" in kinds      # sample.bib 里的 unused2018
    assert "unused_resource" in kinds       # sample.bib 未被 \bibliography 使用

    for finding in findings:
        assert finding.path, finding
        assert finding.line >= 1, finding
    missing_graphic = next(f for f in findings if f.kind == "missing_graphics")
    assert "absent-figure" in missing_graphic.target
    assert missing_graphic.snippet and "includegraphics" in missing_graphic.snippet
    assert missing_graphic.location().path == "main.tex"


def test_dangerous_commands_are_reported_not_executed(tmp_path: Path) -> None:
    root = tmp_path / "danger"
    (root / "sections").mkdir(parents=True)
    (root / "main.tex").write_text(
        "\\documentclass{article}\n\\begin{document}\n"
        "\\input{sections/danger}\n\\end{document}\n", encoding="utf-8")
    (root / "sections" / "danger.tex").write_text(
        "\\immediate\\write18{rm -rf /}\n"
        "\\openout15=foo.txt\n"
        "\\catcode`\\@=11\n"
        "\\csname write18\\endcsname\n"
        "\\input{/etc/passwd}\n"
        "\\input{|ls}\n"
        "\\directlua{os.execute('id')}\n", encoding="utf-8")
    analysis = analyze_latex_package(root)
    assert analysis.document is not None
    findings = check_dangerous_commands(analysis.document)
    assert findings
    targets = {f.target for f in findings}
    assert {"write18", "openout", "catcode", "csname_exec", "input_absolute",
            "input_pipe", "directlua"} <= targets
    for finding in findings:
        assert finding.path == "sections/danger.tex"
        assert finding.line >= 1
        assert finding.snippet
    # 提示不等于编译：报告里 compiled 恒 False。
    assert check_latex_document(analysis.document).compiled is False


def test_unreachable_tex_is_still_scanned_and_listed(tmp_path: Path) -> None:
    root = tmp_path / "unused-tex"
    root.mkdir()
    (root / "main.tex").write_text(
        "\\documentclass{article}\n\\begin{document}\n正文\n\\end{document}\n",
        encoding="utf-8")
    (root / "draft.tex").write_text("\\section{没被用到}\n", encoding="utf-8")
    analysis = analyze_latex_package(root)
    assert analysis.document is not None
    assert analysis.document.unreferenced_tex == ["draft.tex"]
    assert "unused_tex" in finding_kinds(analysis.check.findings)


# ---------------------------------------------------------------------------
# 4. 首次上传只建立基线
# ---------------------------------------------------------------------------


def test_first_upload_is_baseline_only(tmp_path: Path) -> None:
    extract_paper(tmp_path, 1)
    analysis = analyze_latex_package(tmp_path / "extract-v1")
    assert analysis.document is not None
    diff = diff_latex_documents(None, analysis.document, version_ref="MANUSCRIPT-1")
    assert diff.first_version is True
    assert diff.baseline_only is True
    assert diff.sections == []
    assert diff.resources == []
    assert diff.word_count_delta == 0
    assert diff.added_sections == 0 and diff.removed_sections == 0
    assert diff.draft is not None
    assert diff.draft.status == "baseline"
    assert diff.draft.items == []
    assert diff.draft.published is False
    assert "基线" in diff.note


# ---------------------------------------------------------------------------
# 5. 版本差异 → 未发布的进度草稿
# ---------------------------------------------------------------------------


def test_version_diff_produces_unpublished_draft(tmp_path: Path) -> None:
    extract_paper(tmp_path, 1)
    extract_paper(tmp_path, 2)
    old = analyze_latex_package(tmp_path / "extract-v1").document
    new = analyze_latex_package(tmp_path / "extract-v2").document
    assert old is not None and new is not None

    diff = diff_latex_documents(old, new, version_ref="MANUSCRIPT-2",
                                base_version_ref="MANUSCRIPT-1")
    assert diff.first_version is False
    changes = {s.title: s.change for s in diff.sections}
    assert changes.get("角偏移估计") == "added"
    assert changes.get("旧版讨论") == "removed"
    assert changes.get("系统模型") == "unchanged"
    # \input 进来的新小节被识别为改动过的父章节之外，单独算新增。
    assert any(s.title == "信道模型" and s.change == "added" for s in diff.sections)
    assert diff.word_count_delta != 0

    resource_kinds = {(r.kind, r.change) for r in diff.resources}
    assert ("figure", "added") in resource_kinds
    assert ("equation", "added") in resource_kinds
    assert ("citation", "added") in resource_kinds
    assert ("bib_entry", "added") in resource_kinds
    assert ("image_file", "added") in resource_kinds
    assert ("image_file", "removed") in resource_kinds  # Geometric_model 不再被引用
    for change in diff.sections + diff.resources:
        assert change.locations(), change

    draft = diff.draft
    assert isinstance(draft, ProgressDraft)
    assert draft.published is False
    assert draft.requires_student_confirmation is True
    assert draft.status == "draft"
    assert draft.items
    assert "未编译" in draft.caveat and "不能证明实验已执行" in draft.caveat
    for item in draft.items:
        assert item.statement
        assert item.locations, item
    # 草稿可整体序列化成快照载荷，字段名稳定。
    payload = diff.as_dict()
    assert payload["draft"]["published"] is False
    assert payload["draft"]["requires_student_confirmation"] is True
    assert payload["draft"]["items"]


def test_draft_items_are_bounded_and_flagged(tmp_path: Path) -> None:
    extract_paper(tmp_path, 1)
    extract_paper(tmp_path, 2)
    old = analyze_latex_package(tmp_path / "extract-v1").document
    new = analyze_latex_package(tmp_path / "extract-v2").document
    diff = diff_latex_documents(old, new, version_ref="MANUSCRIPT-2",
                                base_version_ref="MANUSCRIPT-1")
    assert len(diff.draft.items) <= 200


# ---------------------------------------------------------------------------
# 6. 不调用编译器、不联网
# ---------------------------------------------------------------------------


@pytest.fixture
def external_calls(monkeypatch: pytest.MonkeyPatch) -> list:
    """把一切外部进程入口换成记录器：被调用即失败。"""
    calls: list = []

    def record(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("静态解析不得调用外部程序或编译器")

    for name in ("run", "Popen", "call", "check_call", "check_output", "getoutput",
                 "getstatusoutput"):
        monkeypatch.setattr(subprocess, name, record, raising=False)
    monkeypatch.setattr(os, "system", record)
    monkeypatch.setattr(os, "popen", record)
    return calls


def test_full_pipeline_never_invokes_a_compiler(tmp_path: Path, external_calls: list) -> None:
    extract_paper(tmp_path, 1)
    extract_paper(tmp_path, 2)
    first = analyze_latex_package(tmp_path / "extract-v1")
    second = analyze_latex_package(tmp_path / "extract-v2")
    diff = diff_latex_documents(first.document, second.document, version_ref="MANUSCRIPT-2",
                                base_version_ref="MANUSCRIPT-1")
    assert diff.sections
    assert external_calls == []


def test_modules_never_import_subprocess_or_network() -> None:
    for path in MODULE_FILES:
        assert path.is_file(), f"缺少模块 {path}"
        source = path.read_text(encoding="utf-8")
        assert re.search(r"^\s*(import|from)\s+subprocess\b", source, re.M) is None, path
        for forbidden in ("subprocess.", "os.system(", "os.popen(", "Popen(",
                          "check_output(", "pdflatex ", "xelatex ", "latexmk "):
            assert forbidden not in source, (path, forbidden)
        assert re.search(r"^\s*(import|from)\s+(requests|httpx|urllib\.request)\b",
                         source, re.M) is None, path


def test_reports_never_claim_verified_results(tmp_path: Path) -> None:
    extract_paper(tmp_path, 1)
    extract_paper(tmp_path, 2)
    first = analyze_latex_package(tmp_path / "extract-v1")
    second = analyze_latex_package(tmp_path / "extract-v2")
    diff = diff_latex_documents(first.document, second.document, version_ref="MANUSCRIPT-2",
                                base_version_ref="MANUSCRIPT-1")
    payload = diff.as_dict()
    assert payload["draft"]["caveat"]
    assert first.check.as_dict()["compiled"] is False
    assert second.check.as_dict()["compiled"] is False
    assert "不能证明实验已执行" in payload["draft"]["caveat"]
