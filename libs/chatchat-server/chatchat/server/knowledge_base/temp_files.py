"""临时文件（文件对话）的属主注册表（进程内，任务 006）。

临时目录与临时向量库都存放在进程内存 / ``BASE_TEMP_DIR`` 下，进程重启即
失效，因此属主信息用同样的进程级生命周期：``temp_id -> owner_id`` 映射，
由上传时写入（owner 来自服务端认证上下文，不接受客户端指定）。

提供统一的属主校验守卫：非属主或不存在一律返回 404（不泄露存在性），保证
用户 A 无法检索/对话/删除用户 B 上传的临时文件。
"""
from __future__ import annotations

import threading
from typing import Dict, Optional

from fastapi import HTTPException, status

from chatchat.server.db.models.user_model import UserModel


class TempFileOwnership:
    """temp_id -> owner_id 的线程安全进程内注册表。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._owners: Dict[str, str] = {}

    def register(self, temp_id: str, user: UserModel) -> None:
        with self._lock:
            self._owners[temp_id] = user.id

    def owner_of(self, temp_id: str) -> Optional[str]:
        with self._lock:
            return self._owners.get(temp_id)

    def is_owned_by(self, temp_id: str, user: UserModel) -> bool:
        return self.owner_of(temp_id) == user.id

    def remove(self, temp_id: str) -> None:
        with self._lock:
            self._owners.pop(temp_id, None)

    def clear(self) -> None:
        with self._lock:
            self._owners.clear()


# 全局单例：与 memo_faiss_pool 同为进程级生命周期
temp_file_ownership = TempFileOwnership()


def assert_temp_owner(user: UserModel, temp_id: str) -> None:
    """非属主或不存在时抛 404（不泄露存在性）。"""
    if not is_owned_by_id(temp_id, user.id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="临时文件不存在或无权访问",
            headers={"WWW-Authenticate": "Bearer"},
        )


def is_owned_by_id(temp_id: str, user_id: str) -> bool:
    """纯 user_id 比较，不依赖 ORM 实例（可用于会话关闭后的线程池等场景）。"""
    return temp_file_ownership.owner_of(temp_id) == user_id


def assert_temp_owner_id(user_id: str, temp_id: str) -> None:
    """assert_temp_owner 的 user_id 版本：请求会话关闭后仍可安全校验。"""
    if not is_owned_by_id(temp_id, user_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="临时文件不存在或无权访问",
            headers={"WWW-Authenticate": "Bearer"},
        )


__all__ = [
    "TempFileOwnership",
    "temp_file_ownership",
    "assert_temp_owner",
    "is_owned_by_id",
    "assert_temp_owner_id",
]
