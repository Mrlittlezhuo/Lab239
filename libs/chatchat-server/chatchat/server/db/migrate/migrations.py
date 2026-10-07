"""内置迁移注册。

- v1（任务 001）：建立 schema 版本记录表。
- v2（任务 002）：新增最小用户账户表 ``user_account``，不创建默认账户，
  不修改 conversation/message 与知识库表。
- v3（任务 003）：conversation 增加 ``owner_id``、``update_time`` 与
  ``(owner_id, update_time)`` 联合索引；旧行 ``owner_id`` 保持 NULL（由
  后续存量迁移任务归属），``update_time`` 回填为 ``create_time``。
- v4（任务 006）：新增 OpenAI 兼容接口（/v1/files）聊天图片/附件归属表
  ``openai_file``，使每张图片/附件由上传用户独占；仅 schema，不动既有文件、
  会话、消息与知识库。
- v5（任务 006）：为 ``owner_id IS NULL`` 的存量会话归属到一个明确的
  遗留用户 ``__legacy__``（role=user, status=active,
  must_change_password=1）；不修改 message 行、知识库表，不重建向量索引。
- v6（任务 007）：新增用户级分层长期记忆表 ``memory`` 与短期会话摘要表
  ``session_summary``；长期记忆按用户隔离、独立于 conversation/message，
  不进入公共知识库；会话摘要记录"已摘要到的位置"以支持增量摘要。
- v7（任务 011）：新增单 Agent 运行表 ``agent_run`` 与步骤表 ``agent_step``；
  运行/步骤按 ``owner_id`` 隔离，仅保存结构化计划、工具行为、观察摘要与
  用户可见状态，不保存模型隐藏思维链。

已发布版本不得修改；后续业务 schema 变化追加新版本迁移函数。
"""
from __future__ import annotations

from sqlalchemy import Connection, text

from chatchat.server.db.migrate.base import (
    Migration,
    MigrationRegistry,
    VERSION_TABLE,
)


def _v1_baseline(conn: Connection) -> None:
    """v1：建立 schema 版本记录表（迁移框架基线）。"""
    conn.execute(
        text(
            f"""
            CREATE TABLE IF NOT EXISTS {VERSION_TABLE} (
                version INTEGER PRIMARY KEY,
                name VARCHAR(255) NOT NULL,
                applied_at TIMESTAMP NOT NULL
            )
            """
        )
    )


_USER_ACCOUNT_TABLE = """
CREATE TABLE user_account (
    id VARCHAR(32) PRIMARY KEY,
    username VARCHAR(64) NOT NULL,
    display_name VARCHAR(100) NOT NULL,
    password_hash VARCHAR(255) NOT NULL,
    role VARCHAR(16) NOT NULL DEFAULT 'user',
    status VARCHAR(16) NOT NULL DEFAULT 'active',
    must_change_password BOOLEAN NOT NULL DEFAULT TRUE,
    auth_version INTEGER NOT NULL DEFAULT 1,
    create_time TIMESTAMP NOT NULL,
    update_time TIMESTAMP NOT NULL,
    CHECK (role IN ('admin', 'user')),
    CHECK (status IN ('active', 'disabled')),
    CHECK (auth_version >= 1)
)
"""

_USER_ACCOUNT_INDEXES = [
    "CREATE UNIQUE INDEX ix_user_account_username ON user_account (username)",
    "CREATE INDEX ix_user_account_role ON user_account (role)",
    "CREATE INDEX ix_user_account_status ON user_account (status)",
]


def _v2_user_account(conn: Connection) -> None:
    """v2：新增最小用户账户表（仅 schema，不创建默认账户）。"""
    conn.execute(text(_USER_ACCOUNT_TABLE))
    for stmt in _USER_ACCOUNT_INDEXES:
        conn.execute(text(stmt))


def _v3_conversation_owner(conn: Connection) -> None:
    """v3：conversation 增加 owner_id / update_time 与联合索引。

    - 业务表 conversation 由应用启动的 ``create_all`` 创建，独立于版本迁移，
      因此在只跑迁移的"空库"上可能尚不存在；此时直接跳过（后续由
      create_all 用新版模型创建带新字段的表）。
    - 已存在的表：缺少 ``owner_id``/``update_time`` 时分别补充；
      ``update_time`` 仅在新加列时回填为 ``create_time``，无值时用当前时间。
    - 旧行 ``owner_id`` 保持 NULL（存量归属由后续任务处理）。
    - 不修改既有 conversation/message 的行数与内容。
    - 重复执行安全：已存在的列/索引不重复创建。
    """
    from sqlalchemy import inspect

    inspector = inspect(conn)
    if "conversation" not in inspector.get_table_names():
        # 业务表尚未创建；由 create_all 用新版模型创建
        return

    existing_cols = {c["name"] for c in inspector.get_columns("conversation")}
    if "owner_id" not in existing_cols:
        conn.execute(text("ALTER TABLE conversation ADD COLUMN owner_id VARCHAR(32)"))
    if "update_time" not in existing_cols:
        conn.execute(text("ALTER TABLE conversation ADD COLUMN update_time TIMESTAMP"))
        conn.execute(text(
            "UPDATE conversation SET update_time = COALESCE(create_time, CURRENT_TIMESTAMP)"
        ))
    conn.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_conversation_owner_update_time "
        "ON conversation (owner_id, update_time)"
    ))


_OPENAI_FILE_TABLE = """
CREATE TABLE IF NOT EXISTS openai_file (
    id VARCHAR(128) PRIMARY KEY,
    owner_id VARCHAR(32) NOT NULL,
    filename VARCHAR(255) NOT NULL,
    purpose VARCHAR(64) NOT NULL DEFAULT 'assistants',
    created_at INTEGER NOT NULL,
    size INTEGER
)
"""


def _v4_openai_file(conn: Connection) -> None:
    """v4：新增 OpenAI 兼容接口（/v1/files）聊天图片/附件归属表。

    - 每张图片/附件由上传用户独占；读取/列表/删除必须校验 owner_id。
    - owner_id 来自服务端认证上下文，不接受客户端指定。
    - 仅 schema，不创建/移动/删除既有文件；不修改 conversation/message
      与知识库表；不重建向量索引。
    - 重复执行安全：表已存在时不重复创建。
    """
    # DDL 已含 IF NOT EXISTS（幂等）
    conn.execute(text(_OPENAI_FILE_TABLE))
    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_openai_file_owner_id ON openai_file (owner_id)"))


_LEGACY_USER_ID = "__legacy__"
_LEGACY_USERNAME = "__legacy__"
_LEGACY_DISPLAY = "__legacy__（升级前无归属的旧会话）"


def _v5_legacy_conversation_owner(conn: Connection) -> None:
    """v5：为 owner_id 为 NULL 的存量会话归属到明确的遗留用户。

    - 幂等：``conversation`` 不存在时跳过；无 NULL 行时不新建用户；
      遗留用户已存在（按 username）时复用其 id。
    - 数据保留：只 UPDATE conversation.owner_id，不修改 message 行；
      会话与消息数量升级前后不变。
    - 不移动知识库目录，不重建公共向量索引，不加 owner/ACL。
    - 失败即回滚且版本不记录（由迁移框架保证）。
    """
    from sqlalchemy import inspect

    inspector = inspect(conn)
    if "conversation" not in inspector.get_table_names():
        # 业务表尚未创建；由 create_all 用新版模型创建带 owner_id 的表
        return

    existing_cols = {c["name"] for c in inspector.get_columns("conversation")}
    if "owner_id" not in existing_cols:
        # 缺少 owner_id（未跑 v3），无从归属；由 v3 先补齐
        return

    # 是否仍有 NULL 归属的旧会话
    null_count = conn.execute(
        text("SELECT COUNT(*) FROM conversation WHERE owner_id IS NULL")
    ).scalar()
    if not null_count:
        # 无存量需要归属；不新建用户
        return

    # 取（或创建）遗留用户
    row = conn.execute(
        text("SELECT id FROM user_account WHERE username = :u"),
        {"u": _LEGACY_USERNAME},
    ).fetchone()
    if row is None:
        conn.execute(
            text(
                """
                INSERT INTO user_account (
                    id, username, display_name, password_hash,
                    role, status, must_change_password, auth_version,
                    create_time, update_time
                ) VALUES (
                    :id, :u, :d, :h, 'user', 'active', 1, 1,
                    CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                )
                """
            ),
            {
                "id": _LEGACY_USER_ID,
                "u": _LEGACY_USERNAME,
                "d": _LEGACY_DISPLAY,
                "h": "no-hash-set",
            },
        )
        legacy_id = _LEGACY_USER_ID
    else:
        legacy_id = row[0]

    conn.execute(
        text("UPDATE conversation SET owner_id = :o WHERE owner_id IS NULL"),
        {"o": legacy_id},
    )


_MEMORY_TABLE = """
CREATE TABLE IF NOT EXISTS memory (
    id VARCHAR(32) PRIMARY KEY,
    owner_id VARCHAR(32) NOT NULL,
    memory_key VARCHAR(128) NOT NULL,
    type VARCHAR(16) NOT NULL DEFAULT 'fact',
    content VARCHAR(2000) NOT NULL,
    source VARCHAR(16) NOT NULL DEFAULT 'chat',
    source_conversation_id VARCHAR(32),
    source_message_id VARCHAR(32),
    importance INTEGER NOT NULL DEFAULT 1,
    enabled BOOLEAN NOT NULL DEFAULT 1,
    create_time TIMESTAMP,
    update_time TIMESTAMP,
    UNIQUE (owner_id, memory_key),
    CHECK (type IN ('preference', 'fact', 'goal', 'important')),
    CHECK (importance >= 1 AND importance <= 5)
)
"""

_SESSION_SUMMARY_TABLE = """
CREATE TABLE IF NOT EXISTS session_summary (
    id VARCHAR(32) PRIMARY KEY,
    owner_id VARCHAR(32) NOT NULL,
    conversation_id VARCHAR(32) NOT NULL,
    summary VARCHAR(4000) NOT NULL DEFAULT '',
    summarized_message_id VARCHAR(32),
    summarized_count INTEGER NOT NULL DEFAULT 0,
    create_time TIMESTAMP,
    update_time TIMESTAMP,
    UNIQUE (conversation_id),
    CHECK (summarized_count >= 0)
)
"""

_MEMORY_INDEXES = [
    "CREATE INDEX IF NOT EXISTS ix_memory_owner_id ON memory (owner_id)",
    "CREATE INDEX IF NOT EXISTS ix_memory_owner_type ON memory (owner_id, type)",
    "CREATE INDEX IF NOT EXISTS ix_session_summary_owner_id ON session_summary (owner_id)",
    "CREATE INDEX IF NOT EXISTS ix_session_summary_conversation_id ON session_summary (conversation_id)",
]


def _v6_user_memory(conn: Connection) -> None:
    """v6：新增用户级分层长期记忆表、短期会话摘要表，及用户级自动记忆开关。

    - ``memory``：用户级长期记忆，独立于 conversation/message，按用户隔离，
      不进入公共知识库/公共向量索引；owner_id 来自服务端认证上下文。
    - ``session_summary``：每会话一行的短期摘要，记录"已摘要到的位置"，
      支持增量摘要；不是长期事实（长期召回只读 memory）。
    - ``user_account.memory_auto_enabled``：用户级"自动提取长期记忆"开关；
      表尚未创建时跳过（由 create_all 用新版模型建带该列的表），已有表则
      补列并默认 TRUE（幂等）。
    - 仅 schema，不创建默认记忆/账户，不修改既有 user/conversation/message
      与知识库表，不重建向量索引；重复执行安全（IF NOT EXISTS / 已存在列跳过）。
    - 失败即回滚且版本不记录（由迁移框架保证）。
    """
    conn.execute(text(_MEMORY_TABLE))
    conn.execute(text(_SESSION_SUMMARY_TABLE))
    for stmt in _MEMORY_INDEXES:
        conn.execute(text(stmt))

    # 给 user_account 追加自动记忆开关（仅在表已存在时；幂等）
    from sqlalchemy import inspect

    inspector = inspect(conn)
    if "user_account" in inspector.get_table_names():
        existing_cols = {c["name"] for c in inspector.get_columns("user_account")}
        if "memory_auto_enabled" not in existing_cols:
            conn.execute(
                text(
                    "ALTER TABLE user_account "
                    "ADD COLUMN memory_auto_enabled BOOLEAN NOT NULL DEFAULT 1"
                )
            )


_AGENT_RUN_TABLE = """
CREATE TABLE IF NOT EXISTS agent_run (
    id VARCHAR(32) PRIMARY KEY,
    owner_id VARCHAR(32) NOT NULL,
    conversation_id VARCHAR(32),
    source_message_id VARCHAR(32),
    goal VARCHAR(2000) NOT NULL,
    plan_text VARCHAR(8000) NOT NULL DEFAULT '',
    status VARCHAR(16) NOT NULL DEFAULT 'pending',
    current_step INTEGER NOT NULL DEFAULT 0,
    max_steps INTEGER NOT NULL DEFAULT 8,
    final_result VARCHAR(8000),
    error VARCHAR(1000),
    create_time TIMESTAMP,
    start_time TIMESTAMP,
    end_time TIMESTAMP,
    CHECK (status IN ('pending', 'running', 'completed', 'failed', 'cancelled')),
    CHECK (current_step >= 0),
    CHECK (max_steps >= 1)
)
"""

_AGENT_STEP_TABLE = """
CREATE TABLE IF NOT EXISTS agent_step (
    id VARCHAR(32) PRIMARY KEY,
    run_id VARCHAR(32) NOT NULL,
    seq INTEGER NOT NULL,
    type VARCHAR(16) NOT NULL DEFAULT 'tool_call',
    status VARCHAR(16) NOT NULL DEFAULT 'running',
    goal VARCHAR(500) NOT NULL DEFAULT '',
    tool_name VARCHAR(128),
    tool_input VARCHAR(2000),
    output_summary VARCHAR(2000),
    error_summary VARCHAR(1000),
    create_time TIMESTAMP,
    start_time TIMESTAMP,
    end_time TIMESTAMP,
    UNIQUE (run_id, seq),
    CHECK (type IN ('tool_call', 'answer')),
    CHECK (status IN ('running', 'completed', 'failed', 'cancelled')),
    CHECK (seq >= 1)
)
"""

_AGENT_RUN_INDEXES = [
    "CREATE INDEX IF NOT EXISTS ix_agent_run_owner_id ON agent_run (owner_id)",
    "CREATE INDEX IF NOT EXISTS ix_agent_run_owner_conv ON agent_run (owner_id, conversation_id)",
    "CREATE INDEX IF NOT EXISTS ix_agent_step_run_id ON agent_step (run_id)",
]


def _v7_agent_run(conn: Connection) -> None:
    """v7：新增单 Agent 运行/步骤表（任务 011）。

    - ``agent_run``：一次单 Agent 闭环运行（目标、结构化计划文本、状态、
      当前步骤、最终结果/错误、起止时间）；``owner_id`` 来自服务端认证
      上下文，查询/取消/读取一律按所有者限定。
    - ``agent_step``：运行内逐步持久化的执行轨迹（工具/回答步骤、状态、
      工具名与结构化输入、截断后的观察/错误摘要）；仅保存结构化计划、
      工具行为、观察摘要与用户可见状态，不保存模型隐藏思维链。
    - 仅 schema，不动既有 user/conversation/message/记忆/知识库表，
      不重建向量索引；重复执行安全（IF NOT EXISTS）。
    - 失败即回滚且版本不记录（由迁移框架保证）。
    """
    conn.execute(text(_AGENT_RUN_TABLE))
    conn.execute(text(_AGENT_STEP_TABLE))
    for stmt in _AGENT_RUN_INDEXES:
        conn.execute(text(stmt))


_MIGRATIONS = [
    (1, "baseline", _v1_baseline),
    (2, "user_account", _v2_user_account),
    (3, "conversation_owner", _v3_conversation_owner),
    (4, "openai_file", _v4_openai_file),
    (5, "legacy_conversation_owner", _v5_legacy_conversation_owner),
    (6, "user_memory", _v6_user_memory),
    (7, "agent_run", _v7_agent_run),
]


def build_registry() -> MigrationRegistry:
    """构建内置迁移注册表（每次调用返回独立实例）。"""
    registry = MigrationRegistry()
    for version, name, upgrade in _MIGRATIONS:
        registry.register(version, name, upgrade)
    return registry


__all__ = ["build_registry"]
