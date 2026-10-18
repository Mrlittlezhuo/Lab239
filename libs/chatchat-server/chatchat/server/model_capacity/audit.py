"""生成模型调用点遗漏检查（§16.1B 第 5/6 条）。

两条底线：

1. **绕开闸门的生成模型构造点必须被发现**。所有生成模型都应经
   ``get_ChatOpenAI`` → ``GatedChatOpenAI``；直接写 ``ChatOpenAI(`` 就绕过了
   闸门。本模块扫描 ``chatchat/`` 源码里的生成模型构造点，除允许清单外必须
   为空；``KNOWN_UNGATED_GENERATION_SITES`` 记录**当前已知但还没接上闸门**的
   历史调用点（受里程碑0B 的文件边界限制没有改动），新增一条就会让自动化
   测试失败，从而把"新增消费者未登记"暴露出来。
2. **Embedding 等独立资源不得被塞进生成模型闸门**。上游约束只针对共享账号
   的生成模型；向量检索走的是另一个模型（甚至另一个服务），混进生成队列会
   让知识库检索莫名其妙地排队等待。``embedding_paths_are_not_gated()`` 检查
   embedding 工厂没有被包装。

启动检查（``audit_generation_consumers``）可以在 lifespan 里调用，结果写日志；
同一份数据也用于 ``tests/unit_tests/test_model_capacity_gate.py``。
"""
from __future__ import annotations

import inspect
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from chatchat.utils import build_logger

logger = build_logger(__name__)

#: 生成模型构造点扫描模式。负向回顾断言排除 ``get_ChatOpenAI(``、
#: ``GatedChatOpenAI(``、``OpenAIEmbeddings(`` 这类同后缀的名字，只留真正
#: 直接构造模型对象的位置。
CONSTRUCTION_PATTERNS: Dict[str, re.Pattern] = {
    "ChatOpenAI(": re.compile(r"(?<![A-Za-z0-9_])ChatOpenAI\s*\("),
    "ChatPlatformAI(": re.compile(r"(?<![A-Za-z0-9_])ChatPlatformAI\s*\("),
    "OpenAI(": re.compile(r"(?<![A-Za-z0-9_])OpenAI\s*\("),
}

#: 闸门模块自身允许直接引用基类的位置（子类定义、委托调用、扫描器自己的源码）。
#: 路径相对于 ``chatchat/`` 包根目录。
GATE_MODULE_DIR = "server/model_capacity"

#: 已知但尚未接上闸门的生成模型构造点（"文件:模式名"，不带行号以免漂移）。
#: 集成阶段已经把两条都清掉：平台模型换成 ``GatedChatPlatformAI``
#: （chat/chat.py），补全式接口换成 ``GatedOpenAI``（utils.get_OpenAI）。
#: 清单为空是**当前状态**：新增一条未接闸门的构造点就会让启动检查与
#: 自动化测试失败，这正是 §16.1B 第 5 条要的效果。
KNOWN_UNGATED_GENERATION_SITES: frozenset = frozenset()

#: 明确**不能**进生成模型闸门的位置：embedding 客户端。命中这里属于预期，
#: 出现在别处则说明有人把向量模型塞进了生成队列。
EMBEDDING_SITES: frozenset = frozenset(
    {
        "server/localai_embeddings.py:OpenAI(",
    }
)

#: embedding 工厂函数与类名（用于"没有被包装"检查）
EMBEDDING_FACTORY_NAMES: Tuple[str, ...] = (
    "get_Embeddings",
    "check_embed_model",
)


def default_source_root() -> Path:
    """``chatchat/`` 包根目录；扫描结果里的路径都相对它（如
    ``server/utils.py``）。"""
    return Path(__file__).resolve().parents[2]


def scan_generation_construction_points(
    root: Optional[Path] = None,
) -> List[Dict[str, Any]]:
    """扫描源码里的生成模型构造点，返回 ``{site, pattern, path, line, code}``。"""
    base = Path(root) if root is not None else default_source_root()
    found: List[Dict[str, Any]] = []
    for path in sorted(base.rglob("*.py")):
        text = path.read_text(encoding="utf-8", errors="replace")
        lines = text.splitlines()
        try:
            rel = path.resolve().relative_to(base.resolve()).as_posix()
        except ValueError:
            rel = path.as_posix()
        for name, pattern in CONSTRUCTION_PATTERNS.items():
            for match in pattern.finditer(text):
                line = text[: match.start()].count("\n")
                found.append(
                    {
                        "site": f"{rel}:{name}",
                        "path": rel,
                        "pattern": name,
                        "line": line + 1,
                        "code": (lines[line] if line < len(lines) else "").strip(),
                    }
                )
    return found


def audit_generation_consumers(root: Optional[Path] = None) -> Dict[str, Any]:
    """启动检查：绕开闸门的生成调用点 + embedding 是否被误接闸门。

    返回 ``ok=False`` 表示出现了允许清单之外的新构造点，调用方（启动钩子或
    测试）据此告警/失败。
    """
    found = scan_generation_construction_points(root)
    ungated: List[Dict[str, Any]] = []
    expected: List[Dict[str, Any]] = []
    embedding: List[Dict[str, Any]] = []
    for item in found:
        if item["path"].startswith(GATE_MODULE_DIR):
            continue
        if item["site"] in EMBEDDING_SITES:
            embedding.append(item)
            continue
        if item["site"] in KNOWN_UNGATED_GENERATION_SITES:
            expected.append(item)
            continue
        ungated.append(item)
    stale_known = sorted(
        KNOWN_UNGATED_GENERATION_SITES - {item["site"] for item in expected}
    )
    result = {
        "ok": not ungated,
        "ungated": ungated,
        "known_ungated": expected,
        "stale_known_sites": stale_known,
        "embedding_sites": embedding,
        "scanned_patterns": sorted(CONSTRUCTION_PATTERNS),
    }
    if ungated:
        logger.warning(
            "发现 {} 个绕开模型容量闸门的生成模型构造点：{}",
            len(ungated),
            "; ".join(f"{item['site']} 第 {item['line']} 行" for item in ungated),
        )
    if stale_known:
        logger.info(
            "已知未接闸门的生成调用点清单里有 {} 条已经不存在（说明已接入或已删除）："
            "可以从 KNOWN_UNGATED_GENERATION_SITES 移除：{}",
            len(stale_known),
            "; ".join(stale_known),
        )
    return result


def embedding_paths_are_not_gated() -> Dict[str, Any]:
    """确认 embedding 工厂没有被接进生成模型闸门（§16.1B 第 6 条）。

    检查两件事：embedding 工厂源码里不引用容量闸门；embedding 模型类没有
    混入 ``ModelCapacityCallMixin``。任何一条不成立都说明向量侧被错误地塞进
    了生成模型队列。
    """
    from chatchat.server.utils import get_Embeddings  # 延迟导入：utils 依赖本包

    from .gated_model import ModelCapacityCallMixin

    problems: List[str] = []

    source = inspect.getsource(get_Embeddings)
    if "model_capacity" in source or "gated_call" in source:
        problems.append("get_Embeddings 里出现了容量闸门引用")

    embedding_classes: List[str] = []
    for name in ("OpenAIEmbeddings", "OllamaEmbeddings", "ZhipuAIEmbeddings"):
        try:
            if name == "OpenAIEmbeddings":
                from langchain_openai import OpenAIEmbeddings as cls
            elif name == "OllamaEmbeddings":
                from langchain_community.embeddings import OllamaEmbeddings as cls
            elif name == "ZhipuAIEmbeddings":
                from langchain_chatchat.embeddings.zhipuai import ZhipuAIEmbeddings as cls
            else:  # pragma: no cover
                continue
        except Exception as exc:  # 依赖缺失不算闸门问题，但要留痕
            problems.append(f"{name} 无法导入：{exc}")
            continue
        embedding_classes.append(name)
        if issubclass(cls, ModelCapacityCallMixin):
            problems.append(f"{name} 被接进了生成模型闸门")

    try:
        from chatchat.server.localai_embeddings import LocalAIEmbeddings as cls

        embedding_classes.append("LocalAIEmbeddings")
        if issubclass(cls, ModelCapacityCallMixin):
            problems.append("LocalAIEmbeddings 被接进了生成模型闸门")
    except ImportError as exc:  # pragma: no cover
        problems.append(f"LocalAIEmbeddings 无法导入：{exc}")

    return {
        "ok": not problems,
        "problems": problems,
        "checked_factories": list(EMBEDDING_FACTORY_NAMES),
        "checked_classes": embedding_classes,
    }


__all__ = [
    "CONSTRUCTION_PATTERNS",
    "GATE_MODULE_DIR",
    "KNOWN_UNGATED_GENERATION_SITES",
    "EMBEDDING_SITES",
    "EMBEDDING_FACTORY_NAMES",
    "default_source_root",
    "scan_generation_construction_points",
    "audit_generation_consumers",
    "embedding_paths_are_not_gated",
]
