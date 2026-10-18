"""AI 静态预审的运行时接线：模型注入 + 提交后自动预审的后台派发。

放在这里而不是路由或仓储里，是为了打断一条必然出现的循环导入：
``supervision_ai_review_routes`` 需要模型工厂（依赖注入），
``supervision_work_routes`` 需要在正式提交成功后触发自动预审，而
``supervision_ai_review_routes`` 又复用了 ``supervision_work_routes`` 的错误
口径。本模块只依赖仓储、模型工厂与容量闸门，谁都可以 import 它。

两件事：

1. **模型注入**（``get_ai_review_model``）：返回统一容量闸门覆盖下的生成模型，
   但用 ``capacity_mode="external"``——预审按 AC 批次取放容量，租约身份
   （``student_check`` / ``mentor_review`` / ``auto_review``、业务对象 ``AR-rN``、
   发起人）只有仓储层知道，模型自己不能再取一次（上限 1 且不可重入，必然自锁）。
   配对的适配器是 ``CapacityLeaseAdapter``；模型在发请求前会确认租约确实在
   （``ModelCapacityLeaseNotHeldError``），所以"由业务层取容量"不会退化成
   "谁都没取容量"。

2. **自动预审派发**（``queue_auto_review``）：正式提交成功后调用。开关默认关闭
   （§7.1），关闭时什么都不创建；打开时只创建一个 ``queued`` 运行，再交给后台
   线程执行。**任何失败都不回滚、不影响已经成功的提交**（§7.2 第2条、P-11）。

后台线程只在真实服务里起：pytest 进程内不起（``PYTEST_CURRENT_TEST``），否则
测试会拿到一个真的在打模型、真的在写 SQLite 的野线程；``execute`` 接口与
``CHATCHAT_AI_REVIEW_AUTO_DISPATCH=0`` 提供等价的显式控制。
"""
from __future__ import annotations

import os
import threading
from typing import Any, Optional

from chatchat.server.model_capacity import (
    CAPACITY_MODE_EXTERNAL,
    CONSUMER_MENTOR_REVIEW,
    CapacityLeaseAdapter,
)
from chatchat.utils import build_logger

logger = build_logger(__name__)

#: 预审与自检的模型对象按进程缓存：构造与缓存模型对象不持有任何容量。
_model_cache: dict = {}

#: 预审按批次取放容量用的共享适配器（无状态，可复用）。
capacity_adapter = CapacityLeaseAdapter(default_consumer=CONSUMER_MENTOR_REVIEW)


def get_ai_review_model() -> Any:
    """返回预审/自检使用的生成模型；模型不可用时返回 ``None``（→ 503）。

    每次调用都真正经过统一容量闸门：租约由 ``capacity_adapter`` 在仓储层的
    每个 AC 批次前后取放（AI预审方案 §12.2.1）。构造失败不缓存，下一次调用
    再试，避免模型配置热更新后永久停在"未接入"。
    """
    cached = _model_cache.get("model")
    if cached is not None:
        return cached
    try:
        from chatchat.settings import Settings
        from chatchat.server.utils import get_ChatOpenAI, get_default_llm

        model = get_ChatOpenAI(
            model_name=get_default_llm(),
            temperature=Settings.model_settings.TEMPERATURE,
            max_tokens=Settings.model_settings.MAX_TOKENS,
            streaming=False,
            verbose=False,
            consumer=CONSUMER_MENTOR_REVIEW,
            capacity_mode=CAPACITY_MODE_EXTERNAL,
        )
    except Exception:  # 模型没配好：预审排队但不假装跑过
        logger.exception("构造 AI 预审生成模型失败（预审将返回 503）")
        return None
    if model is None:
        return None
    _model_cache["model"] = model
    return model


def reset_ai_review_model_cache() -> None:
    """测试与运维用：清掉模型缓存，下次调用重新构造。"""
    _model_cache.clear()


def _auto_dispatch_enabled() -> bool:
    if os.environ.get("CHATCHAT_AI_REVIEW_AUTO_DISPATCH", "1").strip() == "0":
        return False
    # 测试进程里不起后台线程：预审执行由 execute 接口显式驱动。
    return not os.environ.get("PYTEST_CURRENT_TEST")


def _execute_run(project_id: str, run_id: str, actor_id: str) -> None:
    """后台线程体：执行一个已排队的预审，失败只记日志（P-11、P-26）。"""
    from chatchat.server.db.repository import supervision_ai_review_repository as ai
    from chatchat.server.db.session import session_scope

    def worker() -> None:
        try:
            with session_scope() as session:
                ai.run_review(
                    session,
                    project_ref=project_id,
                    run_ref=run_id,
                    actor_id=actor_id,
                    llm=get_ai_review_model(),
                    capacity=capacity_adapter,
                )
        except Exception as exc:  # 预审失败绝不能波及提交或导师审核
            logger.warning(
                "自动预审 {} 执行失败（提交与导师审核不受影响）：{}", run_id, exc,
                exc_info=True,
            )

    thread = threading.Thread(
        target=worker, name=f"ai-review-{run_id[:12]}", daemon=True
    )
    thread.start()
    return thread


def queue_auto_review(
    *, project_id: str, submission_id: str, actor_id: str = ""
) -> Optional[dict]:
    """正式提交成功后的自动预审触发点（§7.2）。

    返回创建出来的运行摘要 ``{code, status, dispatched}``；开关关闭时返回
    ``None``。调用方必须包在 ``try/except`` 里：这里失败不能影响提交本身。
    """
    from chatchat.server.db.repository import supervision_ai_review_repository as ai
    from chatchat.server.db.session import session_scope

    with session_scope() as session:
        run = ai.schedule_auto_review(
            session,
            project_ref=project_id,
            submission_ref=submission_id,
            actor_id=actor_id,
        )
        if run is None:
            return None
        summary = {
            "run_id": run.id,
            "code": run.code,
            "status": run.status,
            "project_id": run.project_id,
            "triggered_by": run.triggered_by,
            "dispatched": False,
        }
        run_id, project_of_run, triggered_by = (
            run.id, run.project_id, run.triggered_by,
        )

    if summary["status"] == "queued" and _auto_dispatch_enabled():
        if get_ai_review_model() is None:
            logger.warning(
                "自动预审 {} 已排队，但生成模型不可用；等待模型接入后重新运行",
                summary["code"],
            )
        else:
            _execute_run(project_of_run, run_id, triggered_by)
            summary["dispatched"] = True
    return summary


__all__ = [
    "capacity_adapter",
    "get_ai_review_model",
    "queue_auto_review",
    "reset_ai_review_model_cache",
]
