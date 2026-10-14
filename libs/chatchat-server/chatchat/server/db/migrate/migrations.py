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
- v8（任务 017）：新增论文研究领域表：私有 ``research_project`` /
  ``research_run`` / ``project_paper`` / ``evidence_card`` /
  ``research_report`` 与公开可复用的 ``paper_record``；私有资源按
  ``owner_id`` 隔离（子资源经项目继承），公开论文元数据以 DOI/ArXiv ID
  部分唯一索引保证规范复用。
- v9（任务 018）：新增用户私有的论文原文单元表 ``paper_unit``（带
  page/section/block_index 稳定定位，槽位唯一以支持幂等重解析），为
  ``evidence_card`` 追加子问题/证据类型/可核对原文/单元指针/校验标记/
  审核状态/幂等键等列，为 ``project_paper`` 追加全文可得性列；只加表加列，
  不改列不删表，v8 已有证据与论文数据完整保留。
- v10（任务 018）：新增 ``comparison_unit``（论文在某比较维度上的结论，
  必须引用具体证据卡片）与 ``research_conflict``（冲突两侧各自引用证据，
  幂等键去重）；为 ``research_project`` 追加可调整的比较维度，为
  ``research_run`` 追加 ``parent_run_id``/``round_no``，使补充检索成为
  轮次有界的可见新运行。
- v11（任务 018）：为 ``research_report`` 追加 ``report_key``（版本链：
  编辑保存新版本，不覆盖旧版本）、``citations_json``/``evidence_ids_json``
  （报告事实与证据卡片、论文的稳定引用映射）以及生成状态与生成模型；
  旧报告行的 ``report_key`` 回填为自身 id。
- v12（任务 018）：放宽 ``research_run.stage`` 取值以支持证据闭环阶段
  （读原文/提取证据/证据复核/覆盖分析/比较/报告/报告复核）；SQLite 无法
  对已有表增删 CHECK 约束，故按官方做法重建表并按列交集拷贝全部行，
  幂等、失败整体回滚，行 id（= LangGraph thread_id）不变。
- v13（智能聊天记忆第一期）：``memory`` 增加 ``category``（个人偏好类别，
  空表示非用户管理的个人偏好），``user_account`` 增加
  ``memory_preferences_enabled``（个人记忆注入总开关）；只加列加索引，
  不改列不删表，旧记忆行 ``category`` 为 NULL，行为不变。
- v14（智能聊天记忆第二期）：``session_summary`` 增加 topic/goal/confirmed/
  constraints/open_questions 与 ``pinned_json``（会话私有上下文的结构化槽位），
  ``conversation`` 增加 ``temporary``（临时对话，不保留会话上下文）；只加列，
  不新建表、不迁移行，既有摘要文本与摘要位置标记原样保留。

已发布版本不得修改；后续业务 schema 变化追加新版本迁移函数。
"""
from __future__ import annotations

from sqlalchemy import Connection, inspect, text

from chatchat.server.db.migrate.base import (
    Migration,
    MigrationError,
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


_RESEARCH_PROJECT_TABLE = """
CREATE TABLE IF NOT EXISTS research_project (
    id VARCHAR(32) PRIMARY KEY,
    owner_id VARCHAR(32) NOT NULL,
    name VARCHAR(200) NOT NULL,
    research_question VARCHAR(2000) NOT NULL,
    scope VARCHAR(2000) NOT NULL DEFAULT '',
    status VARCHAR(16) NOT NULL DEFAULT 'active',
    current_stage VARCHAR(32) NOT NULL DEFAULT '',
    create_time TIMESTAMP,
    update_time TIMESTAMP,
    CHECK (status IN ('active', 'archived'))
)
"""

_RESEARCH_RUN_TABLE = """
CREATE TABLE IF NOT EXISTS research_run (
    id VARCHAR(32) PRIMARY KEY,
    project_id VARCHAR(32) NOT NULL,
    owner_id VARCHAR(32) NOT NULL,
    plan_json VARCHAR(8000) NOT NULL DEFAULT '',
    status VARCHAR(16) NOT NULL DEFAULT 'pending',
    stage VARCHAR(32) NOT NULL DEFAULT 'draft',
    agent_run_id VARCHAR(32),
    sources_json VARCHAR(4000) NOT NULL DEFAULT '[]',
    summary_json VARCHAR(8000) NOT NULL DEFAULT '',
    error VARCHAR(1000),
    create_time TIMESTAMP,
    start_time TIMESTAMP,
    end_time TIMESTAMP,
    update_time TIMESTAMP,
    CHECK (status IN ('pending', 'awaiting_confirmation', 'running',
                      'completed', 'failed', 'cancelled')),
    CHECK (stage IN ('draft', 'plan', 'retrieval', 'dedup', 'screening',
                     'persist', 'done'))
)
"""

_PAPER_RECORD_TABLE = """
CREATE TABLE IF NOT EXISTS paper_record (
    id VARCHAR(32) PRIMARY KEY,
    title VARCHAR(1000) NOT NULL,
    authors VARCHAR(2000) NOT NULL DEFAULT '',
    year INTEGER,
    abstract VARCHAR(8000) NOT NULL DEFAULT '',
    doi VARCHAR(200),
    arxiv_id VARCHAR(100),
    url VARCHAR(1000),
    source VARCHAR(50) NOT NULL DEFAULT '',
    sources_json VARCHAR(4000) NOT NULL DEFAULT '[]',
    fulltext_status VARCHAR(16) NOT NULL DEFAULT 'unknown',
    title_key VARCHAR(200),
    create_time TIMESTAMP,
    update_time TIMESTAMP
)
"""

_PROJECT_PAPER_TABLE = """
CREATE TABLE IF NOT EXISTS project_paper (
    id VARCHAR(32) PRIMARY KEY,
    project_id VARCHAR(32) NOT NULL,
    paper_id VARCHAR(32) NOT NULL,
    owner_id VARCHAR(32) NOT NULL,
    source VARCHAR(50) NOT NULL DEFAULT '',
    relevance FLOAT,
    screening_status VARCHAR(16) NOT NULL DEFAULT 'pending_review',
    screening_reason VARCHAR(1000) NOT NULL DEFAULT '',
    first_run_id VARCHAR(32),
    create_time TIMESTAMP,
    update_time TIMESTAMP,
    UNIQUE (project_id, paper_id),
    CHECK (screening_status IN ('pending_review', 'included', 'excluded'))
)
"""

_EVIDENCE_CARD_TABLE = """
CREATE TABLE IF NOT EXISTS evidence_card (
    id VARCHAR(32) PRIMARY KEY,
    project_id VARCHAR(32) NOT NULL,
    paper_id VARCHAR(32) NOT NULL,
    owner_id VARCHAR(32) NOT NULL,
    run_id VARCHAR(32),
    content VARCHAR(8000) NOT NULL DEFAULT '',
    locator_json VARCHAR(2000) NOT NULL DEFAULT '',
    version INTEGER NOT NULL DEFAULT 1,
    create_time TIMESTAMP,
    update_time TIMESTAMP
)
"""

_RESEARCH_REPORT_TABLE = """
CREATE TABLE IF NOT EXISTS research_report (
    id VARCHAR(32) PRIMARY KEY,
    project_id VARCHAR(32) NOT NULL,
    owner_id VARCHAR(32) NOT NULL,
    title VARCHAR(500) NOT NULL DEFAULT '',
    content VARCHAR(10000) NOT NULL DEFAULT '',
    version INTEGER NOT NULL DEFAULT 1,
    create_time TIMESTAMP,
    update_time TIMESTAMP
)
"""

_RESEARCH_INDEXES = [
    "CREATE INDEX IF NOT EXISTS ix_research_project_owner_id ON research_project (owner_id)",
    "CREATE INDEX IF NOT EXISTS ix_research_project_owner_update ON research_project (owner_id, update_time)",
    "CREATE INDEX IF NOT EXISTS ix_research_run_project ON research_run (project_id)",
    "CREATE INDEX IF NOT EXISTS ix_research_run_owner_create ON research_run (owner_id, create_time)",
    # 稳定标识的部分唯一索引：同一 DOI / ArXiv ID 只保留一条规范记录
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_paper_record_doi ON paper_record (doi) "
    "WHERE doi IS NOT NULL AND doi <> ''",
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_paper_record_arxiv_id ON paper_record (arxiv_id) "
    "WHERE arxiv_id IS NOT NULL AND arxiv_id <> ''",
    "CREATE INDEX IF NOT EXISTS ix_paper_record_title_key ON paper_record (title_key)",
    "CREATE INDEX IF NOT EXISTS ix_project_paper_owner ON project_paper (owner_id)",
    "CREATE INDEX IF NOT EXISTS ix_project_paper_project_screen "
    "ON project_paper (project_id, screening_status)",
    "CREATE INDEX IF NOT EXISTS ix_evidence_card_project ON evidence_card (project_id)",
    "CREATE INDEX IF NOT EXISTS ix_evidence_card_paper ON evidence_card (paper_id)",
    "CREATE INDEX IF NOT EXISTS ix_research_report_project ON research_report (project_id)",
    "CREATE INDEX IF NOT EXISTS ix_research_report_owner ON research_report (owner_id)",
]


def _v8_research(conn: Connection) -> None:
    """v8：新增论文研究领域表（任务 017）。

    - 私有资源（项目/运行/项目论文/证据/报告）带 ``owner_id``，鉴权
      一律 id + owner 双条件；子资源经项目继承所有权。
    - ``paper_record`` 为公开论文元数据规范表，可跨项目复用；DOI 与
      ArXiv ID 用部分唯一索引去重，不保存用户私有文件信息。
    - 仅 schema，不动既有 user/conversation/message/记忆/agent_run/
      知识库表，不重建向量索引；重复执行安全（IF NOT EXISTS）。
    - 失败即回滚且版本不记录（由迁移框架保证）。
    """
    conn.execute(text(_RESEARCH_PROJECT_TABLE))
    conn.execute(text(_RESEARCH_RUN_TABLE))
    conn.execute(text(_PAPER_RECORD_TABLE))
    conn.execute(text(_PROJECT_PAPER_TABLE))
    conn.execute(text(_EVIDENCE_CARD_TABLE))
    conn.execute(text(_RESEARCH_REPORT_TABLE))
    for stmt in _RESEARCH_INDEXES:
        conn.execute(text(stmt))


# --- v9（任务 018 阶段 A）：论文原文单元 + 可核对证据卡片 ---

_PAPER_UNIT_TABLE = """
CREATE TABLE IF NOT EXISTS paper_unit (
    id VARCHAR(32) PRIMARY KEY,
    project_id VARCHAR(32) NOT NULL,
    paper_id VARCHAR(32) NOT NULL,
    owner_id VARCHAR(32) NOT NULL,
    run_id VARCHAR(32),
    unit_kind VARCHAR(16) NOT NULL DEFAULT 'block',
    page INTEGER,
    section VARCHAR(200) NOT NULL DEFAULT '',
    block_index INTEGER NOT NULL DEFAULT 0,
    text VARCHAR(8000) NOT NULL DEFAULT '',
    char_count INTEGER NOT NULL DEFAULT 0,
    content_hash VARCHAR(64) NOT NULL DEFAULT '',
    source_kind VARCHAR(32) NOT NULL DEFAULT '',
    create_time TIMESTAMP,
    update_time TIMESTAMP,
    UNIQUE (project_id, paper_id, unit_kind, block_index),
    CHECK (unit_kind IN ('block', 'abstract'))
)
"""

_PAPER_UNIT_INDEXES = [
    "CREATE INDEX IF NOT EXISTS ix_paper_unit_project_paper "
    "ON paper_unit (project_id, paper_id)",
    "CREATE INDEX IF NOT EXISTS ix_paper_unit_owner ON paper_unit (owner_id)",
]

# evidence_card 追加列（SQLite 支持常量默认值的 NOT NULL 追加，旧行自动回填）
_EVIDENCE_CARD_ADD_COLUMNS = [
    "sub_question_id VARCHAR(64) NOT NULL DEFAULT ''",
    "evidence_type VARCHAR(32) NOT NULL DEFAULT ''",
    "summary VARCHAR(2000) NOT NULL DEFAULT ''",
    "quote VARCHAR(4000) NOT NULL DEFAULT ''",
    "unit_id VARCHAR(32)",
    "quote_verified INTEGER NOT NULL DEFAULT 0",
    "level VARCHAR(16) NOT NULL DEFAULT 'fulltext'",
    "source_kind VARCHAR(32) NOT NULL DEFAULT ''",
    "extraction_model VARCHAR(200) NOT NULL DEFAULT ''",
    "extraction_mode VARCHAR(16) NOT NULL DEFAULT 'llm'",
    "review_status VARCHAR(16) NOT NULL DEFAULT 'pending'",
    "review_note VARCHAR(1000) NOT NULL DEFAULT ''",
    "reviewed_at TIMESTAMP",
    "dedup_key VARCHAR(64) NOT NULL DEFAULT ''",
]

_PROJECT_PAPER_ADD_COLUMNS = [
    "fulltext_state VARCHAR(16) NOT NULL DEFAULT 'unknown'",
    "unit_count INTEGER NOT NULL DEFAULT 0",
    "fulltext_hash VARCHAR(64) NOT NULL DEFAULT ''",
    "fulltext_time TIMESTAMP",
]

_V9_INDEXES = [
    "CREATE INDEX IF NOT EXISTS ix_evidence_card_project_subq "
    "ON evidence_card (project_id, sub_question_id)",
    "CREATE INDEX IF NOT EXISTS ix_evidence_card_review "
    "ON evidence_card (project_id, review_status)",
    # 提取重试幂等：同一 (项目, 论文, 子问题, 类型, 原文) 只保留一张卡片。
    # 空串不参与唯一约束，因此旧行（无 dedup_key）不会被误判重复。
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_evidence_card_dedup ON evidence_card (dedup_key) "
    "WHERE dedup_key IS NOT NULL AND dedup_key <> ''",
]


def _add_missing_columns(
    conn: Connection, table: str, definitions: list
) -> None:
    """按列名补齐缺失列；已存在的列不动，旧行的既有值不被覆盖。"""
    from sqlalchemy import inspect

    inspector = inspect(conn)
    if table not in inspector.get_table_names():
        # 业务表尚未创建（只跑迁移的空库）；由 create_all 用新版模型创建
        return
    existing = {c["name"] for c in inspector.get_columns(table)}
    for definition in definitions:
        column = definition.split()[0]
        if column not in existing:
            conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {definition}"))


def _v9_research_evidence(conn: Connection) -> None:
    """v9：新增 ``paper_unit``，为 ``evidence_card`` / ``project_paper`` 补列（任务 018 阶段 A）。

    - ``paper_unit`` 保存**用户私有**的论文原文块及其稳定定位
      （page/section/block_index/content_hash）；同一槽位唯一，重跑解析
      按内容哈希幂等替换，不产生重复单元。
    - ``evidence_card`` 追加子问题、证据类型、可核对原文 ``quote``、
      单元指针、校验标记、提取方式、审核状态与幂等键；**只加列不改列、
      不删表、不重建**，v8 已有证据行的 content/locator/version 全部保留
      （新列取默认值，``review_status='pending'``）。
    - ``project_paper`` 追加本项目内该论文的全文可得性；公开
      ``paper_record`` 不新增任何用户私有内容。
    - 重复执行安全：``IF NOT EXISTS`` + 列存在性检查；失败即回滚且版本
      不记录（由迁移框架保证）。
    """
    conn.execute(text(_PAPER_UNIT_TABLE))
    for stmt in _PAPER_UNIT_INDEXES:
        conn.execute(text(stmt))
    _add_missing_columns(conn, "evidence_card", _EVIDENCE_CARD_ADD_COLUMNS)
    _add_missing_columns(conn, "project_paper", _PROJECT_PAPER_ADD_COLUMNS)
    for stmt in _V9_INDEXES:
        conn.execute(text(stmt))


# --- v10（任务 018 阶段 B）：比较单元、冲突记录与有界补充检索 ---

_COMPARISON_UNIT_TABLE = """
CREATE TABLE IF NOT EXISTS comparison_unit (
    id VARCHAR(32) PRIMARY KEY,
    project_id VARCHAR(32) NOT NULL,
    paper_id VARCHAR(32) NOT NULL,
    dimension_id VARCHAR(64) NOT NULL,
    owner_id VARCHAR(32) NOT NULL,
    run_id VARCHAR(32),
    content VARCHAR(4000) NOT NULL DEFAULT '',
    evidence_ids_json VARCHAR(4000) NOT NULL DEFAULT '[]',
    level VARCHAR(16) NOT NULL DEFAULT 'fulltext',
    version INTEGER NOT NULL DEFAULT 1,
    create_time TIMESTAMP,
    update_time TIMESTAMP,
    UNIQUE (project_id, paper_id, dimension_id)
)
"""

_RESEARCH_CONFLICT_TABLE = """
CREATE TABLE IF NOT EXISTS research_conflict (
    id VARCHAR(32) PRIMARY KEY,
    project_id VARCHAR(32) NOT NULL,
    owner_id VARCHAR(32) NOT NULL,
    run_id VARCHAR(32),
    sub_question_id VARCHAR(64) NOT NULL DEFAULT '',
    dimension_id VARCHAR(64) NOT NULL DEFAULT '',
    topic VARCHAR(500) NOT NULL DEFAULT '',
    description VARCHAR(4000) NOT NULL DEFAULT '',
    side_a_json VARCHAR(4000) NOT NULL DEFAULT '[]',
    side_b_json VARCHAR(4000) NOT NULL DEFAULT '[]',
    status VARCHAR(16) NOT NULL DEFAULT 'open',
    resolution VARCHAR(1000) NOT NULL DEFAULT '',
    dedup_key VARCHAR(64) NOT NULL DEFAULT '',
    version INTEGER NOT NULL DEFAULT 1,
    create_time TIMESTAMP,
    update_time TIMESTAMP,
    CHECK (status IN ('open', 'resolved'))
)
"""

_V10_INDEXES = [
    "CREATE INDEX IF NOT EXISTS ix_comparison_unit_project ON comparison_unit (project_id)",
    "CREATE INDEX IF NOT EXISTS ix_comparison_unit_owner ON comparison_unit (owner_id)",
    "CREATE INDEX IF NOT EXISTS ix_research_conflict_project ON research_conflict (project_id)",
    "CREATE INDEX IF NOT EXISTS ix_research_conflict_owner ON research_conflict (owner_id)",
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_research_conflict_dedup "
    "ON research_conflict (dedup_key) WHERE dedup_key IS NOT NULL AND dedup_key <> ''",
]

_RESEARCH_PROJECT_ADD_COLUMNS_V10 = ["dimensions_json VARCHAR(4000) NOT NULL DEFAULT ''"]

_RESEARCH_RUN_ADD_COLUMNS_V10 = [
    "parent_run_id VARCHAR(32)",
    "round_no INTEGER NOT NULL DEFAULT 1",
]


def _v10_research_comparison(conn: Connection) -> None:
    """v10：新增比较单元与冲突记录，为补充检索加轮次边界（任务 018 阶段 B）。

    - ``comparison_unit``：某论文在某比较维度上的结论，**必须**带
      ``evidence_ids_json``（依据的证据卡片），使比较结果可反查原文；
      ``(project_id, paper_id, dimension_id)`` 唯一，重新生成幂等替换。
    - ``research_conflict``：冲突两侧各自保存证据引用，``dedup_key``
      部分唯一索引保证重复检测不产生重复记录。
    - ``research_project.dimensions_json``：用户可调整的比较维度；
      ``research_run.parent_run_id``/``round_no``：补充检索是可见的新运行，
      轮次有上限，不会无限自循环。
    - 只加表加列，不改列不删表；v8/v9 已有研究数据完整保留；
      重复执行安全（``IF NOT EXISTS`` + 列存在性检查）。
    """
    conn.execute(text(_COMPARISON_UNIT_TABLE))
    conn.execute(text(_RESEARCH_CONFLICT_TABLE))
    _add_missing_columns(conn, "research_project", _RESEARCH_PROJECT_ADD_COLUMNS_V10)
    _add_missing_columns(conn, "research_run", _RESEARCH_RUN_ADD_COLUMNS_V10)
    for stmt in _V10_INDEXES:
        conn.execute(text(stmt))


# --- v11（任务 018 阶段 C）：版本化报告与引用映射 ---

_RESEARCH_REPORT_ADD_COLUMNS_V11 = [
    "report_key VARCHAR(32) NOT NULL DEFAULT ''",
    "run_id VARCHAR(32)",
    "status VARCHAR(16) NOT NULL DEFAULT 'generated'",
    "citations_json VARCHAR(8000) NOT NULL DEFAULT '[]'",
    "evidence_ids_json VARCHAR(8000) NOT NULL DEFAULT '[]'",
    "unsupported VARCHAR(4000) NOT NULL DEFAULT ''",
    "generated_by VARCHAR(200) NOT NULL DEFAULT ''",
    "error VARCHAR(1000)",
]


def _v11_research_report(conn: Connection) -> None:
    """v11：为 ``research_report`` 追加版本链与引用映射列（任务 018 阶段 C）。

    - ``report_key`` + ``version``：同一逻辑报告的每个版本都是独立行，
      用户编辑保存新版本，**不覆盖**旧版本；用唯一索引代替表内约束
      （SQLite 无法对已有表追加 UNIQUE 约束）。
    - ``citations_json``/``evidence_ids_json``：报告事实与证据卡片、论文
      的稳定关联，使引用可反查、导出可核对。
    - 只加列不改列不删表；v8 已有报告行保留（``report_key`` 回填为其
      自身 ``id``，``version`` 保持原值，因此旧报告自成一条版本链）。
    - 重复执行安全（列存在性检查 + ``IF NOT EXISTS``）。
    """
    _add_missing_columns(conn, "research_report", _RESEARCH_REPORT_ADD_COLUMNS_V11)
    # 旧报告没有版本链：把 report_key 回填为自身 id，保持"一行一版本"
    conn.execute(
        text("UPDATE research_report SET report_key = id WHERE report_key IS NULL OR report_key = ''")
    )
    conn.execute(
        text(
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_research_report_key_version "
            "ON research_report (report_key, version)"
        )
    )


# --- v12（任务 018 阶段 E）：研究运行阶段扩展（证据闭环） ---

#: 证据闭环新增的阶段：读原文→提取证据→证据复核→覆盖分析→比较→
#: 报告→报告复核。人工复核点复用 ``awaiting_confirmation`` 状态，靠
#: ``stage`` 表明正在复核什么，因此不新增状态值。
_RESEARCH_RUN_STAGES_V12 = (
    "draft",
    "plan",
    "retrieval",
    "dedup",
    "screening",
    "persist",
    "fulltext",
    "evidence",
    "evidence_review",
    "coverage",
    "comparison",
    "report",
    "report_review",
    "done",
)

#: 重建后 ``research_run`` 的全部列（必须与下面的 DDL 一致；用于校验旧表
#: 列集，遇到预期之外的列直接失败，绝不静默丢数据）
_RESEARCH_RUN_COLUMNS_V12 = (
    "id",
    "project_id",
    "owner_id",
    "plan_json",
    "status",
    "stage",
    "agent_run_id",
    "sources_json",
    "summary_json",
    "error",
    "create_time",
    "start_time",
    "end_time",
    "update_time",
    "parent_run_id",
    "round_no",
)

_RESEARCH_RUN_TABLE_V12 = f"""
CREATE TABLE research_run__v12 (
    id VARCHAR(32) PRIMARY KEY,
    project_id VARCHAR(32) NOT NULL,
    owner_id VARCHAR(32) NOT NULL,
    plan_json VARCHAR(8000) NOT NULL DEFAULT '',
    status VARCHAR(16) NOT NULL DEFAULT 'pending',
    stage VARCHAR(32) NOT NULL DEFAULT 'draft',
    agent_run_id VARCHAR(32),
    sources_json VARCHAR(4000) NOT NULL DEFAULT '[]',
    summary_json VARCHAR(8000) NOT NULL DEFAULT '',
    error VARCHAR(1000),
    create_time TIMESTAMP,
    start_time TIMESTAMP,
    end_time TIMESTAMP,
    update_time TIMESTAMP,
    parent_run_id VARCHAR(32),
    round_no INTEGER NOT NULL DEFAULT 1,
    CHECK (status IN ('pending', 'awaiting_confirmation', 'running',
                      'completed', 'failed', 'cancelled')),
    CHECK (stage IN ({", ".join(repr(s) for s in _RESEARCH_RUN_STAGES_V12)}))
)
"""

_RESEARCH_RUN_INDEXES_V12 = [
    "CREATE INDEX IF NOT EXISTS ix_research_run_project ON research_run (project_id)",
    "CREATE INDEX IF NOT EXISTS ix_research_run_owner_create "
    "ON research_run (owner_id, create_time)",
]


def _v12_research_run_stage(conn: Connection) -> None:
    """v12：放宽 ``research_run.stage`` 取值，支持证据闭环阶段（任务 018 阶段 E）。

    SQLite 不能对已有表增删 ``CHECK`` 约束，因此按官方做法重建表：
    建新表 → 按列交集拷贝全部行 → 删旧表 → 改名 → 重建索引。

    - **保留旧数据**：按旧表实际列名拷贝，行内容与 id/thread_id 不变
      （checkpoint 以 ``run_id`` 为 thread_id，绝不因重建而失效）；
    - **幂等**：旧表 DDL 已包含新阶段时直接返回，重复执行不改动；
    - **失败可恢复**：整个过程在迁移框架的单个事务内，任何异常（含未知
      列）都回滚全部变更且不记录版本，旧表保持原样；
    - 不新增/删除列，不改其它表，不动知识库与向量索引。
    """
    row = conn.execute(
        text(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'research_run'"
        )
    ).first()
    if row is None:
        # 该库还没有 research_run（v8 未执行过）：无需重建
        return
    ddl = row[0] or ""
    if "evidence_review" in ddl:
        return  # 已是扩展后的约束

    old_columns = [c["name"] for c in inspect(conn).get_columns("research_run")]
    new_columns = {
        c["name"] for c in inspect(conn).get_columns("research_run__v12")
    } if inspect(conn).has_table("research_run__v12") else set()
    unknown = [c for c in old_columns if c not in _RESEARCH_RUN_COLUMNS_V12]
    if unknown:
        raise MigrationError(
            "research_run 存在预期之外的列，拒绝重建以免丢数据："
            f"{unknown}。请先人工确认该库的来源。"
        )
    if new_columns and not set(old_columns).issubset(new_columns):
        # 上一次尝试留下的半成品表（同事务内不会发生，防御性检查）
        raise MigrationError("research_run__v12 列集与旧表不匹配，中止重建")

    cols = ", ".join(old_columns)
    conn.execute(text(_RESEARCH_RUN_TABLE_V12))
    conn.execute(
        text(
            f"INSERT INTO research_run__v12 ({cols}) SELECT {cols} FROM research_run"
        )
    )
    conn.execute(text("DROP TABLE research_run"))
    conn.execute(text("ALTER TABLE research_run__v12 RENAME TO research_run"))
    for stmt in _RESEARCH_RUN_INDEXES_V12:
        conn.execute(text(stmt))


# --- v13（智能聊天记忆第一期）：个人偏好类别与注入总开关 ---

_MEMORY_ADD_COLUMNS_V13 = [
    # 空 = 不是用户在"我的记忆"页管理的个人偏好（例如自动提取的长期事实），
    # 因此旧行不需要回填，也不改变既有召回行为。
    "category VARCHAR(32)",
]

_USER_ACCOUNT_ADD_COLUMNS_V13 = [
    "memory_preferences_enabled BOOLEAN NOT NULL DEFAULT 1",
]

_V13_INDEXES = [
    "CREATE INDEX IF NOT EXISTS ix_memory_owner_category ON memory (owner_id, category)",
]


def _v13_memory_preferences(conn: Connection) -> None:
    """v13：为个人偏好记忆加类别列与用户级注入开关（智能聊天记忆第一期）。

    - ``memory.category``：个人偏好类别（answer_style / technical_background /
      work_style / long_term_goal）。可空，旧行为 NULL，因此**不回填、不改动
      任何既有记忆行**；类别取值由应用层白名单校验——SQLite 不能对已有表
      追加 ``CHECK``，加约束需要整表重建，收益不值。
    - ``user_account.memory_preferences_enabled``：个人记忆注入总开关，默认
      TRUE，与 v6 的 ``memory_auto_enabled``（自动提取开关）职责不同；关闭
      只停止注入，**不删除**已保存的记忆。
    - 只加列加索引，不改列、不删表、不重建，知识库与向量索引不受影响；
      重复执行安全（列存在性检查 + ``IF NOT EXISTS``）；失败即回滚且版本
      不记录（由迁移框架保证）。
    """
    _add_missing_columns(conn, "memory", _MEMORY_ADD_COLUMNS_V13)
    _add_missing_columns(conn, "user_account", _USER_ACCOUNT_ADD_COLUMNS_V13)
    if "memory" in inspect(conn).get_table_names():
        for stmt in _V13_INDEXES:
            conn.execute(text(stmt))


# --- v14（智能聊天记忆第二期）：会话私有上下文与临时对话 ---

_SESSION_SUMMARY_ADD_COLUMNS_V14 = [
    # 会话上下文的结构化槽位（设计方案 §3）。全部 NOT NULL DEFAULT ''，
    # 旧摘要行只多出一段空槽位，summary 原文一字不改。
    "topic VARCHAR(200) NOT NULL DEFAULT ''",
    "goal VARCHAR(500) NOT NULL DEFAULT ''",
    "confirmed TEXT NOT NULL DEFAULT ''",
    "constraints TEXT NOT NULL DEFAULT ''",
    "open_questions TEXT NOT NULL DEFAULT ''",
    # 用户手工编辑/确认过的条目（含来源消息位置），自动摘要更新时不得静默
    # 覆盖这些结论（设计方案 §4.2）。
    "pinned_json TEXT NOT NULL DEFAULT '[]'",
]

_CONVERSATION_ADD_COLUMNS_V14 = [
    # 临时对话：不创建、不更新、不保留会话上下文（设计方案 §4.4）。
    # 默认 FALSE，既有会话行为不变。
    "temporary BOOLEAN NOT NULL DEFAULT 0",
]


def _v14_session_context(conn: Connection) -> None:
    """v14：把会话摘要扩展为结构化会话上下文，并为会话加临时对话标记。

    - ``session_summary`` 增加 topic/goal/confirmed/constraints/open_questions
      与 ``pinned_json``：会话上下文仍复用"每会话一行"的既有表，不新建表、
      不迁移行，既有 ``summary`` 与摘要位置标记原样保留；
    - ``conversation.temporary``：临时对话标记，默认 0；
    - 只加列，不改列、不删表、不重建，知识库与向量索引不受影响；重复执行
      安全（列存在性检查）；失败即回滚且版本不记录。
    """
    _add_missing_columns(conn, "session_summary", _SESSION_SUMMARY_ADD_COLUMNS_V14)
    _add_missing_columns(conn, "conversation", _CONVERSATION_ADD_COLUMNS_V14)


_MIGRATIONS = [
    (1, "baseline", _v1_baseline),
    (2, "user_account", _v2_user_account),
    (3, "conversation_owner", _v3_conversation_owner),
    (4, "openai_file", _v4_openai_file),
    (5, "legacy_conversation_owner", _v5_legacy_conversation_owner),
    (6, "user_memory", _v6_user_memory),
    (7, "agent_run", _v7_agent_run),
    (8, "research", _v8_research),
    (9, "research_evidence", _v9_research_evidence),
    (10, "research_comparison", _v10_research_comparison),
    (11, "research_report", _v11_research_report),
    (12, "research_run_stage_artifacts", _v12_research_run_stage),
    (13, "memory_preferences", _v13_memory_preferences),
    (14, "session_context", _v14_session_context),
]


def build_registry() -> MigrationRegistry:
    """构建内置迁移注册表（每次调用返回独立实例）。"""
    registry = MigrationRegistry()
    for version, name, upgrade in _MIGRATIONS:
        registry.register(version, name, upgrade)
    return registry


__all__ = ["build_registry"]
