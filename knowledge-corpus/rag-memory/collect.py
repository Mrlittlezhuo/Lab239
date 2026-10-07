#!/usr/bin/env python
"""RAG 与长期记忆公开论文语料采集脚本（任务 009/010）。

用法：
    python collect.py                # 执行采集（跳过已存在且校验正确的文件）
    python collect.py --validate     # 只校验已有清单与本地文件
    python collect.py --limit N      # 本次最多处理 N 个候选（调试用）

原则：
- 论文元数据一律来自 arXiv export API（HTTPS），不信任本地记忆，
  不伪造标题/作者/摘要/许可；
- 每个种子带 expected_title（标题关键片段），API 返回标题必须包含它，
  否则记为失败（防止 ID 记错导致采到无关论文）；
- PDF 下载到 documents/，校验 %PDF 魔数与非零大小；HTML 伪装不纳入成功清单；
- 按 arXiv ID（公开编号）与 SHA-256 去重；
- 失败项写入 failures.jsonl（每个 arXiv ID 只保留最新一条，重复运行不无限追加），
  且失败项在重新运行时会被再次尝试（不伪装为成功）；
- 清单为空或成功论文少于 20 篇时 --validate 必须失败；
- 不连接项目真实数据库、模型服务或知识库 API。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DOCS_DIR = os.path.join(BASE_DIR, "documents")
CATALOG_PATH = os.path.join(BASE_DIR, "catalog.jsonl")
FAILURES_PATH = os.path.join(BASE_DIR, "failures.jsonl")
SEEDS_PATH = os.path.join(BASE_DIR, "seeds.jsonl")

API_URL = "https://export.arxiv.org/api/query"
PDF_URL_TEMPLATE = "https://arxiv.org/pdf/{}"
ABS_URL_TEMPLATE = "https://arxiv.org/abs/{}"
USER_AGENT = "langchain-chatchat-corpus-collector/0.1 (offline research use)"
RETRY = 2
RETRY_WAIT = 5  # 秒（网络错误退避）
SLEEP_BETWEEN = 5  # 秒，礼貌限速
NON_PDF_BACKOFF = 15  # 秒（收到非 PDF 响应时的限流退避）
PDF_MAGIC = b"%PDF"
MIN_OK = 20
MAX_OK = 30
MAX_TOTAL_BYTES_WARN = 300 * 1024 * 1024  # 建议上限，仅警告


def log(msg: str) -> None:
    print(msg, flush=True)


def http_get(url: str, timeout: int = 40) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def read_jsonl(path: str) -> list:
    rows = []
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
    return rows


def write_jsonl(path: str, rows: list) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def update_catalog(row: dict) -> None:
    """按 arXiv ID 原位更新/追加 catalog.jsonl（保持每 ID 一条）。"""
    rows = read_jsonl(CATALOG_PATH)
    for i, r in enumerate(rows):
        if r.get("arxiv_id") == row.get("arxiv_id"):
            rows[i] = row
            break
    else:
        rows.append(row)
    write_jsonl(CATALOG_PATH, rows)


def record_failure(arxiv_id: str, stage: str, reason: str) -> None:
    """失败记录：每个 arXiv ID 只保留最新一条，重复运行不会无限追加。"""
    rows = read_jsonl(FAILURES_PATH)
    row = {
        "arxiv_id": arxiv_id,
        "stage": stage,
        "reason": reason,
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    for i, r in enumerate(rows):
        if r.get("arxiv_id") == arxiv_id:
            rows[i] = row
            break
    else:
        rows.append(row)
    write_jsonl(FAILURES_PATH, rows)


def clear_failure(arxiv_id: str) -> None:
    """成功后清除该 ID 的历史失败记录（失败后重试成功不应仍记为失败）。"""
    rows = read_jsonl(FAILURES_PATH)
    new = [r for r in rows if r.get("arxiv_id") != arxiv_id]
    if len(new) != len(rows):
        write_jsonl(FAILURES_PATH, new)


def fetch_arxiv_meta(arxiv_id: str) -> dict:
    """从 arXiv API（HTTPS）拉取单篇论文元数据。失败抛异常。"""
    params = urllib.parse.urlencode({"id_list": arxiv_id, "max_results": 1})
    url = f"{API_URL}?{params}"
    data = http_get(url, timeout=60)
    root = ET.fromstring(data)
    ns = {"atom": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}
    entry = root.find("atom:entry", ns)
    if entry is None:
        raise RuntimeError("no <entry> in arXiv response")

    def text(tag: str) -> str:
        el = entry.find(f"atom:{tag}", ns)
        return (el.text or "").strip() if el is not None else ""

    authors = []
    for a in entry.findall("atom:author", ns):
        name_el = a.find("atom:name", ns)
        if name_el is not None and name_el.text:
            authors.append(re.sub(r"\s+", " ", name_el.text).strip())
    if not authors:
        raise RuntimeError("no authors parsed from arXiv response")

    published = text("published")  # ISO8601
    year = published[:4] if published else ""
    license_url = ""
    lic_el = entry.find("arxiv:license", ns)
    if lic_el is not None:
        license_url = lic_el.get("href", "") or ""

    pdf_url = PDF_URL_TEMPLATE.format(arxiv_id)
    for l in entry.findall("atom:link", ns):
        if l.get("title") == "pdf" and l.get("href"):
            pdf_url = l.get("href")
            break

    title = re.sub(r"\s+", " ", text("title"))
    if not title:
        raise RuntimeError("no title parsed from arXiv response")

    return {
        "arxiv_id": arxiv_id,
        "title": title,
        "authors": authors,
        "year": year,
        "abstract": re.sub(r"\s+", " ", text("summary")),
        "license_url": license_url,
        "pdf_url": pdf_url,
        "abs_url": ABS_URL_TEMPLATE.format(arxiv_id),
    }


def is_pdf(path: str) -> bool:
    try:
        with open(path, "rb") as f:
            return f.read(4) == PDF_MAGIC
    except OSError:
        return False


def license_note(meta: dict) -> str:
    lic = meta.get("license_url") or ""
    m = re.search(r"license/(cc/by|cc/by-nc|cc/by-nd|cc/by-nc-nd|nc)/\d+\.\d+", lic)
    short = m.group(0).replace("license/", "") if m else "unknown"
    return (
        f"arXiv 开放获取（公开可读）；版权许可见 {lic or 'abs 页'}"
        f"（arXiv 标注: {short}）。下载用于本地研究性知识库测试，不用于再分发。"
    )


def collect(seed: dict) -> None:
    arxiv_id = seed["arxiv_id"]
    expected = seed.get("expected_title", "")
    local_path = os.path.join(DOCS_DIR, f"{arxiv_id}.pdf")
    tmp_path = local_path + ".part"

    # 断点：本地文件已存在且为合法 PDF 且与清单校验值一致 → 跳过（不重复下载）
    existing = next((r for r in read_jsonl(CATALOG_PATH) if r.get("arxiv_id") == arxiv_id), None)
    if existing is not None and existing.get("status") == "success":
        if (
            os.path.exists(local_path)
            and os.path.getsize(local_path) > 0
            and is_pdf(local_path)
            and sha256_file(local_path) == existing.get("sha256")
        ):
            clear_failure(arxiv_id)
            log(f"[skip] {arxiv_id} 已存在且校验一致")
            return
        log(f"[redownload] {arxiv_id} 清单记录成功但本地文件缺失/损坏/校验不符")

    meta = None
    last_err = None
    for attempt in range(RETRY + 1):
        try:
            meta = fetch_arxiv_meta(arxiv_id)
            last_err = None
            break
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
            time.sleep(RETRY_WAIT)
    if meta is None:
        record_failure(arxiv_id, "api", last_err)
        log(f"[FAIL api] {arxiv_id}: {last_err}")
        return

    if expected and expected.lower() not in meta["title"].lower():
        reason = f"title_mismatch: expected '{expected}' not in '{meta['title']}'"
        record_failure(arxiv_id, "title_check", reason)
        log(f"[FAIL title] {arxiv_id}: {reason}")
        return

    # 下载 PDF（到临时名，成功后改名）
    # arXiv 限流时可能返回 200 + HTML 拦截页：非 PDF 响应视为瞬时失败，退避重试。
    last_err = None
    for attempt in range(RETRY + 1):
        try:
            data = http_get(meta["pdf_url"], timeout=180)
            if data[:4] != PDF_MAGIC:
                last_err = f"non_pdf_response ({len(data)} bytes, 可能为限流拦截页)"
                time.sleep(NON_PDF_BACKOFF)
                continue
            with open(tmp_path, "wb") as f:
                f.write(data)
            last_err = None
            break
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
            time.sleep(RETRY_WAIT)
    if last_err is not None or not os.path.exists(tmp_path) or os.path.getsize(tmp_path) == 0:
        record_failure(arxiv_id, "download", last_err or "empty response")
        log(f"[FAIL download] {arxiv_id}: {last_err}")
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        return

    if not is_pdf(tmp_path):
        reason = "not_pdf (可能为 HTML 伪装或损坏)"
        record_failure(arxiv_id, "pdf_check", reason)
        log(f"[FAIL pdf_check] {arxiv_id}: {reason}")
        os.remove(tmp_path)
        return

    sha = sha256_file(tmp_path)
    # SHA 去重：与已成功项相同则视为重复（不同 arXiv 版本视为同一篇）
    dup = [
        r
        for r in read_jsonl(CATALOG_PATH)
        if r.get("status") == "success"
        and r.get("arxiv_id") != arxiv_id
        and r.get("sha256") == sha
    ]
    if dup:
        os.remove(tmp_path)
        reason = f"duplicate_sha256_of:{dup[0]['arxiv_id']}"
        record_failure(arxiv_id, "dedupe", reason)
        log(f"[FAIL dedupe] {arxiv_id}: {reason}")
        return

    os.replace(tmp_path, local_path)
    clear_failure(arxiv_id)
    row = {
        "id": f"arxiv-{arxiv_id}",
        "arxiv_id": arxiv_id,
        "title": meta["title"],
        "authors": meta["authors"],
        "year": meta["year"],
        "topic": seed.get("topic", ""),
        "topic_en": seed.get("topic_en", ""),
        "keywords": seed.get("keywords", []),
        "abs_url": meta["abs_url"],
        "pdf_url": meta["pdf_url"],
        "license_note": license_note(meta),
        "local_path": os.path.relpath(local_path, BASE_DIR),
        "sha256": sha,
        "size": os.path.getsize(local_path),
        "language": "en",
        "zh_summary": seed.get("zh_summary", ""),
        "abstract": meta["abstract"],
        "status": "success",
    }
    update_catalog(row)
    log(f"[OK] {arxiv_id} ({row['size'] / 1048576:.1f} MB) {row['title'][:70]}")
    time.sleep(SLEEP_BETWEEN)


def validate() -> int:
    rows = read_jsonl(CATALOG_PATH)
    errors = []
    required = ["id", "arxiv_id", "title", "authors", "year", "topic", "abs_url",
                "pdf_url", "license_note", "local_path", "sha256", "language",
                "keywords", "zh_summary", "status"]
    ids, shas = set(), set()
    ok = 0
    total_bytes = 0
    if not rows:
        errors.append("清单为空（catalog.jsonl 无记录）")
    for i, r in enumerate(rows):
        for k in required:
            if k not in r or r[k] in ("", None, []):
                errors.append(f"row {i}: 缺字段或为空 {k}")
        if r.get("status") != "success":
            errors.append(f"row {i}: 清单中不允许非 success 记录（失败项应在 failures.jsonl）")
            continue
        ok += 1
        total_bytes += r.get("size", 0)
        aid = r.get("arxiv_id")
        if aid in ids:
            errors.append(f"row {i}: arxiv_id 重复 {aid}")
        ids.add(aid)
        sha = r.get("sha256")
        if sha in shas:
            errors.append(f"row {i}: sha256 重复 {sha}")
        shas.add(sha)
        p = os.path.join(BASE_DIR, r.get("local_path", ""))
        if not os.path.exists(p):
            errors.append(f"row {i}: 文件不存在 {r.get('local_path')}")
        else:
            if os.path.getsize(p) == 0:
                errors.append(f"row {i}: 文件为空 {r.get('local_path')}")
            elif not is_pdf(p):
                errors.append(f"row {i}: 非 PDF（魔数不符） {r.get('local_path')}")
            else:
                actual = sha256_file(p)
                if actual != sha:
                    errors.append(f"row {i}: sha256 与文件实际值不一致 {r.get('local_path')}")
                elif r.get("size") and r["size"] != os.path.getsize(p):
                    errors.append(f"row {i}: size 字段与文件实际大小不一致 {r.get('local_path')}")
    if ok < MIN_OK:
        errors.append(f"成功论文 {ok} 篇，少于 {MIN_OK} 篇下限")
    if ok > MAX_OK:
        errors.append(f"成功论文 {ok} 篇，超过 {MAX_OK} 篇上限")

    # 失败记录结构检查（不校验原因内容，只保证可解析且字段完整）
    fails = read_jsonl(FAILURES_PATH)
    for i, r in enumerate(fails):
        for k in ("arxiv_id", "stage", "reason"):
            if k not in r or r[k] in ("", None):
                errors.append(f"failures row {i}: 缺字段 {k}")
    fail_ids = {r.get("arxiv_id") for r in fails}
    dup_fail = fail_ids & ids
    if dup_fail:
        errors.append(f"同一 arXiv ID 同时出现在成功清单与失败记录: {sorted(dup_fail)}")

    log(f"validate: 清单 {len(rows)} 条（成功 {ok}），失败记录 {len(fails)} 条，成功总大小 {total_bytes / 1048576:.1f} MB")
    if total_bytes > MAX_TOTAL_BYTES_WARN:
        log(f"  WARN: 总大小超过 {MAX_TOTAL_BYTES_WARN / 1048576:.0f} MB 建议上限（不阻断）")
    if errors:
        for e in errors:
            log("  ERROR: " + e)
        log("validate: FAILED")
        return 1
    log("validate: PASSED")
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--validate", action="store_true", help="只校验，不下载")
    ap.add_argument("--limit", type=int, default=0, help="本次最多处理候选数（0=全部）")
    args = ap.parse_args()

    if args.validate:
        sys.exit(validate())

    os.makedirs(DOCS_DIR, exist_ok=True)
    seeds = read_jsonl(SEEDS_PATH)
    if not seeds:
        log("seeds.jsonl 为空")
        sys.exit(1)
    seen = set()
    n = 0
    for seed in seeds:
        aid = seed.get("arxiv_id", "")
        if not aid or aid in seen:
            continue
        seen.add(aid)
        if args.limit and n >= args.limit:
            break
        n += 1
        try:
            collect(seed)
        except Exception as e:
            record_failure(aid, "unexpected", f"{type(e).__name__}: {e}")
            log(f"[FAIL unexpected] {aid}: {e}")
    log("采集结束，运行 --validate 校验。")


if __name__ == "__main__":
    main()
