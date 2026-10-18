"""模型容量闸门观测 API（里程碑0B · 重新设计方案 §8.2 最后一段、§16.1B）。

只读，不改任何业务状态：

- ``GET /server/model_capacity``        闸门运行时快照；
- ``GET /server/model_capacity/status`` 同一份数据的别名路径。

返回内容就是 §8.2 要求的"运行时占用证据"：当前占用数、容量上限、进程生命
周期内的最大占用值、当前持有者（消费者类型、业务对象、用户、请求编号、排队
序号、取得时间、最后心跳）、等待队列（含排队位置与前方任务数）、最近的释放
记录（释放时间与释放原因）、三条通道的分布与消费者登记表。

鉴权：要求已登录且已完成强制改密（``require_password_changed``）。占用记录里
只有用户 ID 与业务对象标识，不含提示词、回答正文或密钥，因此不需要额外脱敏；
也不返回任何模型密钥或 api_base。

取消入口不在这里：0B 只要求"排队可取消"的能力（
``get_gate().cancel(request_id=...)``），把它接到页面按钮属于集成阶段。
"""
from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Depends

from chatchat.server.auth.deps import require_password_changed
from chatchat.server.model_capacity import describe_consumers, get_gate

model_capacity_router = APIRouter(
    prefix="/server/model_capacity", tags=["Model Capacity"]
)


@model_capacity_router.get(
    "",
    summary="模型容量闸门运行时快照（需登录，只读）",
    dependencies=[Depends(require_password_changed)],
)
def model_capacity_status() -> Dict[str, Any]:
    """当前占用、容量上限、最大占用值、持有者、等待队列与释放历史。"""
    return get_gate().status()


@model_capacity_router.get(
    "/status",
    summary="同上（别名路径，便于前端按 /status 约定接入）",
    dependencies=[Depends(require_password_changed)],
)
def model_capacity_status_alias() -> Dict[str, Any]:
    return get_gate().status()


@model_capacity_router.get(
    "/consumers",
    summary="生成模型消费者登记表（需登录，只读）",
    dependencies=[Depends(require_password_changed)],
)
def model_capacity_consumers() -> Dict[str, Any]:
    """登记了哪些生成模型消费者、各自属于哪条业务通道。

    排查"某个调用为什么排到后面/前面"时先看这里：通道由登记表推导，
    调用点只声明消费者类型。
    """
    return {"consumers": describe_consumers()}


__all__ = ["model_capacity_router"]
