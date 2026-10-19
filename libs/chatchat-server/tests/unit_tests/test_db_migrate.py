"""数据库 schema 迁移框架的离线单元测试（任务 001）。

全部测试使用临时目录中的临时 SQLite 数据库或内存数据库，不连接
模型服务、外部网络或真实用户数据库。
"""
import datetime
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError, OperationalError

from chatchat.server.db.base import Base
from chatchat.server.db.migrate.base import (
    MigrationError,
    MigrationRegistry,
    apply_migrations,
    current_version,
    pending_migrations,
)
from chatchat.server.db.migrate.backup import (
    BackupError,
    backup_sqlite_db,
    sqlite_db_file,
)
from chatchat.server.db.migrate.migrations import build_registry
from chatchat.server.db.models.conversation_model import ConversationModel
from chatchat.server.db.models.message_model import MessageModel


# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def temp_db_file(tmp_path):
    """临时目录中的空 SQLite 文件型数据库路径。"""
    path = tmp_path / "info.db"
    path.touch()
    return path


def _file_engine(path):
    return create_engine(f"sqlite:///{path}")


def _test_registry() -> MigrationRegistry:
    """测试用注册表：v1 建表，v2 业务 DML，v3 业务 DML。"""
    registry = MigrationRegistry()

    def v1(conn):
        conn.execute(
            text(
                "CREATE TABLE test_items (id INTEGER PRIMARY KEY, "
                "value TEXT NOT NULL)"
            )
        )

    def v2(conn):
        conn.execute(text("INSERT INTO test_items (value) VALUES ('v2')"))

    def v3(conn):
        conn.execute(text("INSERT INTO test_items (value) VALUES ('v3')"))

    registry.register(1, "test-v1", v1)
    registry.register(2, "test-v2", v2)
    registry.register(3, "test-v3", v3)
    return registry


def _failing_registry(fail_version: int) -> MigrationRegistry:
    """v1 正常，``fail_version`` 版本抛异常，v3 在其后。"""
    registry = MigrationRegistry()

    def v1(conn):
        conn.execute(
            text(
                "CREATE TABLE failing_items (id INTEGER PRIMARY KEY, "
                "value TEXT NOT NULL)"
            )
        )

    def v2(conn):
        conn.execute(text("INSERT INTO failing_items (value) VALUES ('partial')"))
        raise RuntimeError("simulated migration failure")

    def v3(conn):
        conn.execute(text("INSERT INTO failing_items (value) VALUES ('never')"))

    registry.register(1, "ok", v1)
    registry.register(2, "fail", v2)
    registry.register(3, "after-fail", v3)
    assert fail_version == 2
    return registry


# ---------------------------------------------------------------------------
# 版本注册与顺序
# ---------------------------------------------------------------------------


def test_builtin_registry_order_and_latest():
    registry = build_registry()
    assert [m.version for m in registry.migrations] == [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22]
    assert [m.name for m in registry.migrations] == [
        "baseline", "user_account", "conversation_owner",
        "openai_file", "legacy_conversation_owner", "user_memory",
        "agent_run", "research", "research_evidence", "research_comparison", "research_report", "research_run_stage_artifacts",
        "memory_preferences",
        "session_context",
        "cross_session_recall",
        "context_correctness",
        "supervision_collaboration",
        "supervision_work_loop",
        "supervision_research_brief",
        "supervision_ai_review",
        "supervision_literature",
        "supervision_latex",
    ]
    assert registry.latest_version == 22


def test_registry_rejects_duplicate_version():
    registry = MigrationRegistry()
    registry.register(1, "a", lambda conn: None)
    with pytest.raises(MigrationError):
        registry.register(1, "b", lambda conn: None)


def test_registry_rejects_gap():
    registry = MigrationRegistry()
    registry.register(1, "a", lambda conn: None)
    with pytest.raises(MigrationError):
        registry.register(3, "c", lambda conn: None)
    assert [m.version for m in registry.migrations] == [1]


def test_registry_must_start_at_one():
    registry = MigrationRegistry()
    with pytest.raises(MigrationError):
        registry.register(2, "b", lambda conn: None)
    assert registry.migrations == []


def test_registry_rejects_non_positive_version():
    registry = MigrationRegistry()
    with pytest.raises(MigrationError):
        registry.register(0, "zero", lambda conn: None)


# ---------------------------------------------------------------------------
# 首次升级 / 幂等 / 顺序
# ---------------------------------------------------------------------------


def test_first_upgrade_on_empty_file_db(temp_db_file):
    registry = build_registry()
    engine = _file_engine(temp_db_file)
    with engine.connect() as conn:
        assert current_version(conn) is None
        result = apply_migrations(registry, conn)
        assert [m.version for m in result.applied] == [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22]
        assert result.final_version == 22
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT version, name FROM schema_migrations")
        ).fetchall()
        assert [(r[0], r[1]) for r in rows] == [
            (1, "baseline"), (2, "user_account"), (3, "conversation_owner"),
            (4, "openai_file"), (5, "legacy_conversation_owner"), (6, "user_memory"),
            (7, "agent_run"), (8, "research"), (9, "research_evidence"), (10, "research_comparison"), (11, "research_report"), (12, "research_run_stage_artifacts"), (13, "memory_preferences"), (14, "session_context"), (15, "cross_session_recall"), (16, "context_correctness"),
            (17, "supervision_collaboration"),
            (18, "supervision_work_loop"),
            (19, "supervision_research_brief"),
            (20, "supervision_ai_review"),
            (21, "supervision_literature"),
            (22, "supervision_latex"),
        ]
        # 空库尚无业务表 conversation，v3 跳过 ALTER（由 create_all 建带新字段表）
        assert not inspect(conn).has_table("conversation")
    engine.dispose()


def test_current_version_does_not_create_version_table(temp_db_file):
    engine = _file_engine(temp_db_file)
    with engine.connect() as conn:
        assert current_version(conn) is None
        assert not inspect(conn).has_table("schema_migrations")
    engine.dispose()


def test_double_upgrade_is_idempotent(temp_db_file):
    registry = build_registry()
    engine = _file_engine(temp_db_file)
    with engine.connect() as conn:
        first = apply_migrations(registry, conn)
        assert [m.version for m in first.applied] == [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22]
        second = apply_migrations(registry, conn)
        assert second.applied == []
        assert second.final_version == 22
    with engine.connect() as conn:
        assert conn.execute(
            text("SELECT COUNT(*) FROM schema_migrations")
        ).scalar() == 22
    engine.dispose()


def test_no_pending_migration_does_not_run_before_upgrade(temp_db_file):
    registry = build_registry()
    engine = _file_engine(temp_db_file)
    callbacks = []
    with engine.connect() as conn:
        apply_migrations(registry, conn)
        result = apply_migrations(
            registry,
            conn,
            before_upgrade=lambda: callbacks.append("called"),
        )
    assert result.applied == []
    assert callbacks == []
    engine.dispose()


def test_in_memory_db_upgrades_without_backup(tmp_path):
    """内存数据库可以正常升级（不要求文件备份）。"""
    registry = build_registry()
    engine = create_engine("sqlite:///:memory:")
    assert sqlite_db_file(engine) is None
    with engine.connect() as conn:
        result = apply_migrations(registry, conn)
        assert result.final_version == 22
        assert current_version(conn) == 22
    engine.dispose()


def test_multiple_versions_run_in_order(temp_db_file):
    registry = _test_registry()
    engine = _file_engine(temp_db_file)
    with engine.connect() as conn:
        result = apply_migrations(registry, conn)
        assert [m.version for m in result.applied] == [1, 2, 3]
        assert result.final_version == 3
        values = [
            r[0]
            for r in conn.execute(text("SELECT value FROM test_items ORDER BY id"))
        ]
        assert values == ["v2", "v3"]
    engine.dispose()


def test_pending_migrations_computed_from_db_version(temp_db_file):
    registry = _test_registry()
    engine = _file_engine(temp_db_file)
    with engine.connect() as conn:
        apply_migrations(registry, conn)
        # 截断版本记录到 v1，模拟只执行到 v1 的旧库
        conn.execute(text("DELETE FROM schema_migrations WHERE version > 1"))
        conn.commit()
    with engine.connect() as conn:
        pending = pending_migrations(registry, conn)
        assert [m.version for m in pending] == [2, 3]
    engine.dispose()


# ---------------------------------------------------------------------------
# 失败回滚
# ---------------------------------------------------------------------------


def test_failed_version_is_not_recorded_and_rolls_back(temp_db_file):
    registry = _failing_registry(2)
    engine = _file_engine(temp_db_file)
    with pytest.raises(RuntimeError, match="simulated migration failure"):
        with engine.connect() as conn:
            apply_migrations(registry, conn)
    with engine.connect() as conn:
        assert current_version(conn) == 1
        assert conn.execute(text("SELECT COUNT(*) FROM failing_items")).scalar() == 0
        versions = [
            r[0] for r in conn.execute(text("SELECT version FROM schema_migrations"))
        ]
        assert versions == [1]
    engine.dispose()


def test_failed_migration_stops_later_versions(temp_db_file):
    registry = _failing_registry(2)
    engine = _file_engine(temp_db_file)
    with pytest.raises(RuntimeError):
        with engine.connect() as conn:
            apply_migrations(registry, conn)
    with engine.connect() as conn:
        assert current_version(conn) == 1
    engine.dispose()


def test_failed_first_migration_rolls_back_sqlite_ddl(temp_db_file):
    registry = MigrationRegistry()

    def failing_ddl(conn):
        conn.execute(text("CREATE TABLE should_rollback (id INTEGER)"))
        raise RuntimeError("simulated DDL failure")

    registry.register(1, "failing-ddl", failing_ddl)
    engine = _file_engine(temp_db_file)
    with pytest.raises(RuntimeError, match="simulated DDL failure"):
        with engine.connect() as conn:
            apply_migrations(registry, conn)
    with engine.connect() as conn:
        assert not inspect(conn).has_table("should_rollback")
        assert not inspect(conn).has_table("schema_migrations")
    engine.dispose()


# ---------------------------------------------------------------------------
# 版本回退拒绝
# ---------------------------------------------------------------------------


def test_downgrade_refused(temp_db_file):
    registry = _test_registry()
    engine = _file_engine(temp_db_file)
    with engine.connect() as conn:
        apply_migrations(registry, conn)
    smaller = MigrationRegistry()
    smaller.register(1, "only-v1", lambda conn: None)
    with engine.connect() as conn:
        with pytest.raises(MigrationError):
            pending_migrations(smaller, conn)
        with pytest.raises(MigrationError):
            apply_migrations(smaller, conn)
    engine.dispose()


def test_non_contiguous_database_history_is_rejected(temp_db_file):
    registry = _test_registry()
    engine = _file_engine(temp_db_file)
    with engine.connect() as conn:
        apply_migrations(registry, conn)
        conn.execute(text("DELETE FROM schema_migrations WHERE version = 2"))
        conn.commit()
        with pytest.raises(MigrationError, match="不连续"):
            current_version(conn)
    engine.dispose()


# ---------------------------------------------------------------------------
# SQLite 备份
# ---------------------------------------------------------------------------


def test_sqlite_db_file_detection(temp_db_file):
    assert sqlite_db_file(_file_engine(temp_db_file)) == str(temp_db_file)
    # 用字符串 URL 判断，避免在非 SQLite 驱动环境下加载方言
    assert sqlite_db_file("sqlite:///:memory:") is None
    assert sqlite_db_file("sqlite:///file::memory:") is None
    assert sqlite_db_file("sqlite:///file:tmp") is None
    assert sqlite_db_file("postgresql://u:p@localhost/db") is None
    assert sqlite_db_file(f"sqlite:///{temp_db_file}") == str(temp_db_file)
    for engine in (
        _file_engine(temp_db_file),
        create_engine("sqlite:///:memory:"),
    ):
        engine.dispose()


def test_backup_created_before_upgrade_and_readable(temp_db_file):
    registry = _test_registry()
    engine = _file_engine(temp_db_file)
    # 模拟升级前已存在的业务数据
    with engine.connect() as conn:
        conn.execute(
            text("CREATE TABLE pre_existing (id INTEGER PRIMARY KEY, value TEXT)")
        )
        conn.execute(text("INSERT INTO pre_existing (value) VALUES ('old-data')"))
        conn.commit()

    backups = []

    def before_upgrade():
        path = backup_sqlite_db(str(temp_db_file))
        backups.append(path)
        assert Path(path).is_file()

    with engine.connect() as conn:
        apply_migrations(registry, conn, before_upgrade=before_upgrade)
    # 每次升级只备份一次（本次共 3 个迁移）
    assert len(backups) == 1
    # 备份内容可读，且是升级前快照：含预置数据，不含 v1 才创建的 test_items
    conn = sqlite3.connect(backups[0])
    try:
        rows = [r for r in conn.execute("SELECT value FROM pre_existing")]
        assert [r[0] for r in rows] == ["old-data"]
        tables = [
            r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        ]
        assert "pre_existing" in tables
        assert "test_items" not in tables
    finally:
        conn.close()
    engine.dispose()


def test_backup_not_overwritten(temp_db_file):
    first = backup_sqlite_db(str(temp_db_file))
    second = backup_sqlite_db(str(temp_db_file))
    assert first != second
    assert Path(first).is_file() and Path(second).is_file()


def test_backup_name_collision_never_overwrites(
    temp_db_file, monkeypatch
):
    from chatchat.server.db.migrate import backup as backup_module

    class FixedDateTime(datetime.datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2024, 1, 2, 3, 4, 5, tzinfo=tz)

    fake_datetime_module = SimpleNamespace(
        datetime=FixedDateTime,
        timezone=datetime.timezone,
    )
    fake_uuid = SimpleNamespace(hex="deadbeef" * 4)
    monkeypatch.setattr(backup_module, "_dt", fake_datetime_module)
    monkeypatch.setattr(backup_module.uuid, "uuid4", lambda: fake_uuid)

    collision = Path(f"{temp_db_file}.bak-20240102T030405-deadbeef.db")
    collision.write_bytes(b"keep-existing-backup")
    with pytest.raises(BackupError, match="唯一"):
        backup_sqlite_db(str(temp_db_file))
    assert collision.read_bytes() == b"keep-existing-backup"


def test_backup_includes_committed_wal_content(tmp_path):
    db_path = tmp_path / "wal.db"
    source = sqlite3.connect(str(db_path))
    try:
        source.execute("PRAGMA journal_mode=WAL")
        source.execute("PRAGMA wal_autocheckpoint=0")
        source.execute("CREATE TABLE items (value TEXT)")
        source.commit()
        source.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        source.execute("INSERT INTO items VALUES ('committed-in-wal')")
        source.commit()

        backup_path = backup_sqlite_db(str(db_path))
        with sqlite3.connect(backup_path) as backup:
            assert backup.execute("SELECT value FROM items").fetchall() == [
                ("committed-in-wal",)
            ]
    finally:
        source.close()


def test_backup_missing_source_raises(temp_db_file):
    missing = temp_db_file.parent / "nope.db"
    with pytest.raises(BackupError):
        backup_sqlite_db(str(missing))


# ---------------------------------------------------------------------------
# v2 user_account 迁移
# ---------------------------------------------------------------------------


def test_v2_creates_user_account_schema(temp_db_file):
    """空库从 v1→v2 顺序升级，user_account 字段与唯一约束正确，无默认账户。"""
    registry = build_registry()
    engine = _file_engine(temp_db_file)
    with engine.connect() as conn:
        result = apply_migrations(registry, conn)
        # v1→v2→v3→v4→v5；v3 在空库无 conversation 表时跳过 ALTER
        assert [m.version for m in result.applied] == [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22]
        cols = {c["name"] for c in inspect(conn).get_columns("user_account")}
        expected = {
            "id", "username", "password_hash", "display_name",
            "role", "status", "create_time", "update_time",
            "must_change_password", "auth_version",
        }
        assert expected <= cols
        idx_names = {i["name"] for i in inspect(conn).get_indexes("user_account")}
        assert "ix_user_account_username" in idx_names
        checks = " ".join(
            c["sqltext"] for c in inspect(conn).get_check_constraints("user_account")
        )
        assert "role IN" in checks
        assert "status IN" in checks
        assert "auth_version >= 1" in checks
        assert conn.execute(text("SELECT COUNT(*) FROM user_account")).scalar() == 0
    engine.dispose()


def test_v2_only_runs_on_existing_v1_db(temp_db_file):
    """已有 v1 库只执行 v2；重复运行不重复执行。"""
    registry = build_registry()
    engine = _file_engine(temp_db_file)
    # 模拟一个已迁移到 v1 的库：预写版本记录（不跑真实 v1，避免重复写版本）
    with engine.connect() as conn:
        conn.execute(text(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "version INTEGER PRIMARY KEY, name VARCHAR(255) NOT NULL, "
            "applied_at TIMESTAMP NOT NULL)"
        ))
        conn.execute(text(
            "INSERT INTO schema_migrations (version, name, applied_at) "
            "VALUES (1, 'baseline', CURRENT_TIMESTAMP)"
        ))
        conn.commit()
    with engine.connect() as conn:
        result = apply_migrations(registry, conn)
        # 已有 v1：执行 v2、v3、v4、v5（v3 无 conversation 表，跳过 ALTER）
        assert [m.version for m in result.applied] == [2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22]
        assert result.final_version == 22
        assert conn.execute(text("SELECT COUNT(*) FROM user_account")).scalar() == 0
        result2 = apply_migrations(registry, conn)
        assert result2.applied == []
        assert result2.final_version == 22
    engine.dispose()


def test_v2_failure_not_recorded_and_rolled_back(temp_db_file):
    """v2 失败时不记录 v2、user_account 不残留、无默认账户。"""
    registry = MigrationRegistry()

    def v1(conn):
        conn.execute(text(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "version INTEGER PRIMARY KEY, name VARCHAR(255) NOT NULL, "
            "applied_at TIMESTAMP NOT NULL)"
        ))

    def v2_fail(conn):
        # 先建表再在同一事务内失败，验证 DDL 随失败回滚、user_account 不残留
        conn.execute(text("CREATE TABLE user_account (id INTEGER PRIMARY KEY)"))
        raise RuntimeError("simulated v2 failure")

    registry.register(1, "baseline", v1)
    registry.register(2, "user_account-fail", v2_fail)
    engine = _file_engine(temp_db_file)
    with pytest.raises(RuntimeError):
        with engine.connect() as conn:
            apply_migrations(registry, conn)
    with engine.connect() as conn:
        assert current_version(conn) == 1
        assert not inspect(conn).has_table("user_account")
        versions = [
            r[0] for r in conn.execute(text("SELECT version FROM schema_migrations"))
        ]
        assert versions == [1]
    engine.dispose()


def test_v2_refuses_preexisting_incompatible_user_table(temp_db_file):
    """无 v2 记录却已有同名表时拒绝静默认领，保留原表并不记录 v2。"""
    engine = _file_engine(temp_db_file)
    with engine.connect() as conn:
        conn.execute(text(
            "CREATE TABLE schema_migrations ("
            "version INTEGER PRIMARY KEY, name VARCHAR(255) NOT NULL, "
            "applied_at TIMESTAMP NOT NULL)"
        ))
        conn.execute(text(
            "INSERT INTO schema_migrations VALUES "
            "(1, 'baseline', CURRENT_TIMESTAMP)"
        ))
        conn.execute(text("CREATE TABLE user_account (legacy_id INTEGER)"))
        conn.commit()

    with engine.connect() as conn:
        with pytest.raises(OperationalError, match="already exists"):
            apply_migrations(build_registry(), conn)
    with engine.connect() as conn:
        assert current_version(conn) == 1
        assert [c["name"] for c in inspect(conn).get_columns("user_account")] == [
            "legacy_id"
        ]
    engine.dispose()


# ---------------------------------------------------------------------------
# v3 conversation owner 迁移
# ---------------------------------------------------------------------------


def test_v3_upgrades_existing_v2_db_and_preserves_data(tmp_path):
    """已有 v1、v2 记录且已有旧 conversation/message 表的库，执行 v3、v4；
    conversation 字段/联合索引新增、openai_file 表创建，旧数据保留，重复执行安全。"""
    db_path = tmp_path / "v3_upgrade.db"
    engine = _file_engine(db_path)
    # 模拟一个已迁移到 v2、且已有旧 conversation/message 业务表的库
    with engine.connect() as conn:
        conn.execute(text(
            "CREATE TABLE schema_migrations ("
            "version INTEGER PRIMARY KEY, name VARCHAR(255) NOT NULL, "
            "applied_at TIMESTAMP NOT NULL)"
        ))
        conn.execute(text(
            # 模拟 v2 建出的完整 user_account（与 _v2_user_account DDL 一致），
            # v5 需要 username 等列来定位/创建遗留用户
            "CREATE TABLE user_account ("
            "id VARCHAR(32) PRIMARY KEY, "
            "username VARCHAR(64) NOT NULL, "
            "display_name VARCHAR(100) NOT NULL, "
            "password_hash VARCHAR(255) NOT NULL, "
            "role VARCHAR(16) NOT NULL DEFAULT 'user', "
            "status VARCHAR(16) NOT NULL DEFAULT 'active', "
            "must_change_password BOOLEAN NOT NULL DEFAULT TRUE, "
            "auth_version INTEGER NOT NULL DEFAULT 1, "
            "create_time TIMESTAMP NOT NULL, "
            "update_time TIMESTAMP NOT NULL)"
        ))
        conn.execute(text(
            "INSERT INTO schema_migrations (version, name, applied_at) VALUES "
            "(1, 'baseline', CURRENT_TIMESTAMP), "
            "(2, 'user_account', CURRENT_TIMESTAMP)"
        ))
        # 旧 conversation：无 owner_id/update_time，create_time 部分为空
        conn.execute(text(
            "CREATE TABLE conversation ("
            "id VARCHAR(32) PRIMARY KEY, name VARCHAR(50), "
            "chat_type VARCHAR(50), create_time TIMESTAMP)"
        ))
        conn.execute(text(
            "INSERT INTO conversation (id, name, chat_type, create_time) VALUES "
            "('old-a', '旧对话A', 'local', CURRENT_TIMESTAMP), "
            "('old-b', '旧对话B', 'local', NULL)"
        ))
        conn.execute(text(
            "CREATE TABLE message ("
            "id VARCHAR(32) PRIMARY KEY, conversation_id VARCHAR(32))"
        ))
        conn.execute(text(
            "INSERT INTO message (id, conversation_id) VALUES "
            "('old-a-m', 'old-a'), ('old-b-m', 'old-b')"
        ))
        conn.commit()

    with engine.connect() as conn:
        before_conv = conn.execute(
            text("SELECT id, name, chat_type FROM conversation ORDER BY id")
        ).fetchall()
        before_msg = conn.execute(
            text("SELECT id, conversation_id FROM message ORDER BY id")
        ).fetchall()

    with engine.connect() as conn:
        result = apply_migrations(build_registry(), conn)
        assert [m.version for m in result.applied] == [3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22]
        assert result.final_version == 22
        # v4 创建 openai_file 表
        assert inspect(conn).has_table("openai_file")

        cols = {c["name"] for c in inspect(conn).get_columns("conversation")}
        assert {"owner_id", "update_time", "create_time", "id", "name", "chat_type"} <= cols
        idx_names = {i["name"] for i in inspect(conn).get_indexes("conversation")}
        assert "ix_conversation_owner_update_time" in idx_names

    # 旧数据保留：内容不变，行数不变
    with engine.connect() as conn:
        after_conv = conn.execute(
            text("SELECT id, name, chat_type FROM conversation ORDER BY id")
        ).fetchall()
        after_msg = conn.execute(
            text("SELECT id, conversation_id FROM message ORDER BY id")
        ).fetchall()
        assert after_conv == before_conv
        assert after_msg == before_msg
        # v5：旧行 owner 已由 NULL 归属到明确的遗留用户（不再 NULL）
        assert conn.execute(text(
            "SELECT COUNT(*) FROM conversation WHERE owner_id IS NULL"
        )).scalar() == 0
        # 两个旧会话都指向同一个遗留用户
        legacy_rows = conn.execute(text(
            "SELECT COUNT(DISTINCT owner_id) FROM conversation"
        )).scalar()
        assert legacy_rows == 1
        # 恰好新建了一个遗留用户
        assert conn.execute(text(
            "SELECT COUNT(*) FROM user_account WHERE username = '__legacy__'"
        )).scalar() == 1
        # update_time 回填：有 create_time 的用 create_time，NULL 的用当前时间
        assert conn.execute(text(
            "SELECT COUNT(*) FROM conversation WHERE update_time IS NULL"
        )).scalar() == 0

    # 重复升级：无待执行迁移，字段不重复新增
    with engine.connect() as conn:
        result2 = apply_migrations(build_registry(), conn)
        assert result2.applied == []
        assert result2.final_version == 22
    engine.dispose()


def test_v3_skips_when_conversation_table_missing(temp_db_file):
    """只到 v1、v2 且尚无 conversation 业务表的空库，执行 v3、v4；v3 跳过 ALTER，v4 建 openai_file。"""
    engine = _file_engine(temp_db_file)
    with engine.connect() as conn:
        conn.execute(text(
            "CREATE TABLE schema_migrations ("
            "version INTEGER PRIMARY KEY, name VARCHAR(255) NOT NULL, "
            "applied_at TIMESTAMP NOT NULL)"
        ))
        conn.execute(text(
            # 模拟 v2 建出的完整 user_account（与 _v2_user_account DDL 一致），
            # v5 需要 username 等列来定位/创建遗留用户
            "CREATE TABLE user_account ("
            "id VARCHAR(32) PRIMARY KEY, "
            "username VARCHAR(64) NOT NULL, "
            "display_name VARCHAR(100) NOT NULL, "
            "password_hash VARCHAR(255) NOT NULL, "
            "role VARCHAR(16) NOT NULL DEFAULT 'user', "
            "status VARCHAR(16) NOT NULL DEFAULT 'active', "
            "must_change_password BOOLEAN NOT NULL DEFAULT TRUE, "
            "auth_version INTEGER NOT NULL DEFAULT 1, "
            "create_time TIMESTAMP NOT NULL, "
            "update_time TIMESTAMP NOT NULL)"
        ))
        conn.execute(text(
            "INSERT INTO schema_migrations (version, name, applied_at) VALUES "
            "(1, 'baseline', CURRENT_TIMESTAMP), "
            "(2, 'user_account', CURRENT_TIMESTAMP)"
        ))
        conn.commit()

    with engine.connect() as conn:
        result = apply_migrations(build_registry(), conn)
        assert [m.version for m in result.applied] == [3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22]
        assert result.final_version == 22
        # conversation 表未被创建（由 create_all 负责）
        assert not inspect(conn).has_table("conversation")
        # v4 创建 openai_file 表
        assert inspect(conn).has_table("openai_file")
    engine.dispose()


# ---------------------------------------------------------------------------
# 数据保留
# ---------------------------------------------------------------------------


def _seed_conversations(session_factory, conv_ids):
    with session_factory() as session:
        for conv_id in conv_ids:
            session.add(
                ConversationModel(
                    id=conv_id, name=f"对话 {conv_id}", chat_type="local"
                )
            )
            session.add(MessageModel(id=conv_id + "-m", conversation_id=conv_id))
        session.commit()


def test_sample_data_preserved_through_noop_migrations(tmp_path):
    """空操作迁移（无 DDL）前后 conversation/message 数据数量与值一致。"""
    db_path = tmp_path / "preserved.db"
    Base.metadata.create_all(bind=create_engine(f"sqlite:///{db_path}"))

    from sqlalchemy.orm import sessionmaker

    engine = _file_engine(db_path)
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    conv_ids = ["conv-a", "conv-b"]
    _seed_conversations(SessionLocal, conv_ids)

    def noop(conn):
        pass

    registry = MigrationRegistry()
    registry.register(1, "noop-a", noop)
    registry.register(2, "noop-b", noop)

    with engine.connect() as conn:
        result = apply_migrations(registry, conn)
        assert result.final_version == 2

    with SessionLocal() as session:
        convs = session.query(ConversationModel).order_by(ConversationModel.id).all()
        msgs = session.query(MessageModel).order_by(MessageModel.id).all()
        assert [c.id for c in convs] == conv_ids
        assert [c.name for c in convs] == [f"对话 {c}" for c in conv_ids]
        assert [m.id for m in msgs] == [c + "-m" for c in conv_ids]
        assert len(convs) == 2 and len(msgs) == 2
    engine.dispose()


def test_sample_data_preserved_through_real_upgrade(tmp_path):
    """真实 v1→v2 升级前后 conversation/message 示例数据数量与值一致，
    且未改动任何知识库表。"""
    db_path = tmp_path / "real_upgrade.db"
    # 模拟已有业务数据：预建 conversation/message 与 knowledge 表并填充
    engine = _file_engine(db_path)
    with engine.connect() as conn:
        conn.execute(text(
            "CREATE TABLE conversation (id VARCHAR(32) PRIMARY KEY, "
            "name VARCHAR(50), chat_type VARCHAR(50), create_time TIMESTAMP)"
        ))
        conn.execute(text(
            "CREATE TABLE message (id VARCHAR(32) PRIMARY KEY, "
            "conversation_id VARCHAR(32), chat_type VARCHAR(50))"
        ))
        conn.execute(text(
            "CREATE TABLE knowledge_base (id INTEGER PRIMARY KEY, name VARCHAR)"
        ))
        conn.execute(text(
            "INSERT INTO conversation (id, name, chat_type) VALUES "
            "('conv-x', '对话 conv-x', 'local'), "
            "('conv-y', '对话 conv-y', 'local'), "
            "('conv-z', '对话 conv-z', 'local')"
        ))
        conn.execute(text(
            "INSERT INTO message (id, conversation_id) VALUES "
            "('conv-x-m', 'conv-x'), ('conv-y-m', 'conv-y'), ('conv-z-m', 'conv-z')"
        ))
        conn.execute(text("INSERT INTO knowledge_base (name) VALUES ('samples')"))
        conn.commit()

    with engine.connect() as conn:
        before = set(inspect(conn).get_table_names())
    assert {"conversation", "message", "knowledge_base"} <= before

    with engine.connect() as conn:
        result = apply_migrations(build_registry(), conn)
        assert result.final_version == 22

    with engine.connect() as conn:
        after = set(inspect(conn).get_table_names())
        versions = [r[0] for r in conn.execute(text("SELECT version FROM schema_migrations"))]
        # v5：3 个旧会话都归属到同一个遗留用户（不再 NULL），且恰好一个遗留用户
        null_owner = conn.execute(text(
            "SELECT COUNT(*) FROM conversation WHERE owner_id IS NULL"
        )).scalar()
        distinct_owner = conn.execute(text(
            "SELECT COUNT(DISTINCT owner_id) FROM conversation"
        )).scalar()
        legacy_user = conn.execute(text(
            "SELECT COUNT(*) FROM user_account WHERE username = '__legacy__'"
        )).scalar()
    # 新增 user_account，原有表全部保留；知识库表未被改动
    assert "user_account" in after
    assert before <= after
    assert versions == [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22]
    assert null_owner == 0
    assert distinct_owner == 1
    assert legacy_user == 1

    with engine.connect() as conn:
        convs = conn.execute(text("SELECT id, name FROM conversation ORDER BY id")).fetchall()
        conv_cols = [c["name"] for c in inspect(conn).get_columns("conversation")]
        msgs = conn.execute(text("SELECT id, conversation_id FROM message ORDER BY id")).fetchall()
        kbs = conn.execute(text("SELECT name FROM knowledge_base")).fetchall()
    # v3 给既有 conversation 增加 owner_id/update_time，旧行内容不变
    assert {"owner_id", "update_time"} <= set(conv_cols)
    assert [c[0] for c in convs] == ["conv-x", "conv-y", "conv-z"]
    assert [c[1] for c in convs] == [f"对话 {i}" for i in ("conv-x", "conv-y", "conv-z")]
    assert [m[0] for m in msgs] == ["conv-x-m", "conv-y-m", "conv-z-m"]
    assert [m[1] for m in msgs] == ["conv-x", "conv-y", "conv-z"]
    assert [k[0] for k in kbs] == ["samples"]
    engine.dispose()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _cli(monkeypatch, tmp_path, *args):
    """运行 chatchat migrate CLI 子命令，指向临时数据库。"""
    from chatchat import settings as settings_module
    from chatchat.server.db.migrate import cli as migrate_cli

    db_path = tmp_path / "cli.db"
    db_path.touch()
    uri = f"sqlite:///{db_path}"
    monkeypatch.setattr(settings_module.Settings.basic_settings, "SQLALCHEMY_DATABASE_URI", uri)
    monkeypatch.setenv("CHATCHAT_ROOT", str(tmp_path))
    runner = CliRunner()
    return runner.invoke(migrate_cli.migrate, list(args)), db_path


def test_cli_status_shows_pending_and_final(tmp_path, monkeypatch):
    runner_result, db_path = _cli(monkeypatch, tmp_path, "status")
    assert runner_result.exit_code == 0
    assert "当前数据库版本" in runner_result.output
    assert "代码最新版本：22" in runner_result.output
    assert "v1" in runner_result.output
    assert "v2" in runner_result.output
    assert "v3" in runner_result.output
    assert "v4" in runner_result.output
    assert "v5" in runner_result.output
    assert "v7" in runner_result.output
    with sqlite3.connect(str(db_path)) as conn:
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    assert ("schema_migrations",) not in tables
    assert ("user_account",) not in tables

    upgrade_result, _ = _cli(monkeypatch, tmp_path, "upgrade")
    assert upgrade_result.exit_code == 0
    assert "已执行迁移" in upgrade_result.output
    assert "最终版本：22" in upgrade_result.output
    assert "备份" in upgrade_result.output

    status_result, _ = _cli(monkeypatch, tmp_path, "status")
    assert status_result.exit_code == 0
    assert "已是最新版本" in status_result.output


def test_cli_status_does_not_create_missing_database(tmp_path, monkeypatch):
    from chatchat import settings as settings_module
    from chatchat.server.db.migrate import cli as migrate_cli

    db_path = tmp_path / "missing.db"
    monkeypatch.setattr(
        settings_module.Settings.basic_settings,
        "SQLALCHEMY_DATABASE_URI",
        f"sqlite:///{db_path}",
    )
    monkeypatch.setenv("CHATCHAT_ROOT", str(tmp_path))
    result = CliRunner().invoke(migrate_cli.migrate, ["status"])
    assert result.exit_code == 0
    assert "v1" in result.output
    assert not db_path.exists()


def test_cli_upgrade_is_idempotent(tmp_path, monkeypatch):
    _, db_path = _cli(monkeypatch, tmp_path, "upgrade")
    result, _ = _cli(monkeypatch, tmp_path, "upgrade")
    assert result.exit_code == 0
    assert "无待执行迁移" in result.output
    assert "最终版本：22" in result.output
    assert Path(db_path).is_file()
    backups = [p for p in db_path.parent.iterdir() if ".bak-" in p.name]
    # 第二次升级无待执行迁移，不触发新的备份
    assert len(backups) == 1


def test_cli_upgrade_creates_backup_file(tmp_path, monkeypatch):
    result, db_path = _cli(monkeypatch, tmp_path, "upgrade")
    assert result.exit_code == 0
    backups = [p for p in Path(db_path.parent).iterdir() if ".bak-" in p.name]
    assert len(backups) == 1
    assert str(backups[0]) in result.output


def test_cli_upgrade_failure_exit_code(tmp_path, monkeypatch):
    from chatchat.server.db.migrate import cli as migrate_cli

    def fail_migration(*args, **kwargs):
        raise RuntimeError("simulated CLI migration failure")

    monkeypatch.setattr(migrate_cli, "apply_migrations", fail_migration)
    result, _ = _cli(monkeypatch, tmp_path, "upgrade")
    assert result.exit_code != 0
    assert "迁移失败" in result.output
    assert "simulated CLI migration failure" in result.output


def test_cli_backup_failure_stops_upgrade(tmp_path, monkeypatch):
    from chatchat.server.db.migrate import cli as migrate_cli

    def fail_backup(*args, **kwargs):
        raise BackupError("simulated backup failure")

    monkeypatch.setattr(migrate_cli, "backup_sqlite_db", fail_backup)
    result, db_path = _cli(monkeypatch, tmp_path, "upgrade")
    assert result.exit_code != 0
    assert "备份失败，升级已中止" in result.output
    with sqlite3.connect(str(db_path)) as conn:
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    assert ("schema_migrations",) not in tables


def test_cli_import_does_not_touch_database(tmp_path, monkeypatch):
    """导入 CLI 模块不产生任何数据库修改。"""
    from chatchat import settings as settings_module

    db_path = tmp_path / "fresh.db"
    uri = f"sqlite:///{db_path}"
    monkeypatch.setattr(
        settings_module.Settings.basic_settings,
        "SQLALCHEMY_DATABASE_URI",
        uri,
    )
    monkeypatch.setenv("CHATCHAT_ROOT", str(tmp_path))
    # 重新导入（已缓存则为 no-op），确认导入过程不创建/修改数据库文件
    import importlib
    import chatchat.server.db.migrate.cli as cli_mod

    importlib.reload(cli_mod)
    with sqlite3.connect(str(db_path)) as conn:
        tables = [
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        ]
    assert "schema_migrations" not in tables


# ---------------------------------------------------------------------------
# v7 agent_run / agent_step 迁移
# ---------------------------------------------------------------------------


def test_v7_creates_agent_run_schema_on_empty_db(temp_db_file):
    """空库升级到 v7：agent_run/agent_step 表结构与索引正确，重复升级幂等。"""
    registry = build_registry()
    engine = _file_engine(temp_db_file)
    with engine.connect() as conn:
        result = apply_migrations(registry, conn)
        assert [m.version for m in result.applied] == [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22]
        run_cols = {c["name"] for c in inspect(conn).get_columns("agent_run")}
        assert {
            "id", "owner_id", "conversation_id", "source_message_id",
            "goal", "plan_text", "status", "current_step", "max_steps",
            "final_result", "error", "create_time", "start_time", "end_time",
        } <= run_cols
        step_cols = {c["name"] for c in inspect(conn).get_columns("agent_step")}
        assert {
            "id", "run_id", "seq", "type", "status", "goal", "tool_name",
            "tool_input", "output_summary", "error_summary",
            "create_time", "start_time", "end_time",
        } <= step_cols
        run_idx = {i["name"] for i in inspect(conn).get_indexes("agent_run")}
        assert {"ix_agent_run_owner_id", "ix_agent_run_owner_conv"} <= run_idx
        step_idx = {i["name"] for i in inspect(conn).get_indexes("agent_step")}
        assert "ix_agent_step_run_id" in step_idx
        checks = " ".join(
            c["sqltext"]
            for c in inspect(conn).get_check_constraints("agent_run")
        )
        assert "status IN" in checks
        assert conn.execute(text("SELECT COUNT(*) FROM agent_run")).scalar() == 0
    # 幂等：再次升级不重复执行、不破坏表
    with engine.connect() as conn:
        result2 = apply_migrations(registry, conn)
        assert result2.applied == []
        assert result2.final_version == 22
        assert conn.execute(text("SELECT COUNT(*) FROM agent_run")).scalar() == 0
    engine.dispose()


def test_v7_upgrades_existing_v6_db_preserving_data(temp_db_file):
    """已有 v6 库升级到 v7：只新增两张表，既有数据完整保留。"""
    engine = _file_engine(temp_db_file)
    # 模拟一个已迁移到 v6 的库（版本记录 + 业务表与一行数据）
    with engine.connect() as conn:
        conn.execute(text(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "version INTEGER PRIMARY KEY, name VARCHAR(255) NOT NULL, "
            "applied_at TIMESTAMP NOT NULL)"
        ))
        for version, name in [
            (1, "baseline"), (2, "user_account"), (3, "conversation_owner"),
            (4, "openai_file"), (5, "legacy_conversation_owner"),
            (6, "user_memory"),
        ]:
            conn.execute(
                text(
                    "INSERT INTO schema_migrations (version, name, applied_at) "
                    "VALUES (:v, :n, CURRENT_TIMESTAMP)"
                ),
                {"v": version, "n": name},
            )
        conn.execute(text(
            "CREATE TABLE user_account ("
            "id VARCHAR(32) PRIMARY KEY, username VARCHAR(128) NOT NULL, "
            "password_hash VARCHAR(255) NOT NULL, display_name VARCHAR(128), "
            "role VARCHAR(16) NOT NULL DEFAULT 'user', "
            "status VARCHAR(16) NOT NULL DEFAULT 'active', "
            "create_time TIMESTAMP, update_time TIMESTAMP, "
            "must_change_password BOOLEAN NOT NULL DEFAULT 0, "
            "auth_version INTEGER NOT NULL DEFAULT 1)"
        ))
        conn.execute(
            text(
                "INSERT INTO user_account (id, username, password_hash) "
                "VALUES ('u1', 'legacy-user', 'x')"
            )
        )
        conn.commit()

    with engine.connect() as conn:
        result = apply_migrations(build_registry(), conn)
        assert [m.version for m in result.applied] == [7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22]
        assert result.final_version == 22
        # 新表已创建
        assert inspect(conn).has_table("agent_run")
        assert inspect(conn).has_table("agent_step")
        # 既有数据保留
        assert (
            conn.execute(text("SELECT COUNT(*) FROM user_account")).scalar() == 1
        )
        assert (
            conn.execute(
                text("SELECT username FROM user_account WHERE id = 'u1'")
            ).scalar()
            == "legacy-user"
        )
    engine.dispose()


def test_v20_ai_review_schema_matches_the_orm(temp_db_file):
    """v20 建出的三张预审表必须与 ORM 定义一致。

    预审模块的测试靠 ``Base.metadata.create_all`` 建表，迁移路径本身不会被它们
    覆盖；这里专门用真实迁移建库，再逐列比对 ORM，钉住"手写 DDL 与模型不漂移"
    （AGENTS.md：迁移必须幂等、可验证）。索引也一并按名字比对，否则队列查询会
    在真实库里静默退化成全表扫描。
    """
    from chatchat.server.db.models.supervision_ai_review_model import (
        SupervisionAIReviewFindingModel,
        SupervisionAIReviewRunModel,
        SupervisionAIReviewSettingModel,
    )

    engine = _file_engine(temp_db_file)
    with engine.connect() as conn:
        result = apply_migrations(build_registry(), conn)
        assert result.final_version == 22
        insp = inspect(conn)
        for model in (
            SupervisionAIReviewRunModel,
            SupervisionAIReviewFindingModel,
            SupervisionAIReviewSettingModel,
        ):
            table = model.__table__
            assert insp.has_table(table.name), table.name
            migrated = {column["name"] for column in insp.get_columns(table.name)}
            expected = {column.name for column in table.columns}
            assert migrated == expected, (table.name, expected - migrated, migrated - expected)
            indexed = {item["name"] for item in insp.get_indexes(table.name)}
            assert {ix.name for ix in table.indexes} <= indexed, table.name
        # 幂等：再跑一次不报错、不重复建表
        second = apply_migrations(build_registry(), conn)
        assert second.applied == []
        assert second.final_version == 22
    engine.dispose()


def test_v20_upgrade_keeps_existing_submission_and_review_rows(temp_db_file):
    """升级到 v20 前已存在的提交/审核/附件行必须原样保留（不重建、不清空）。"""
    from chatchat.server.db.migrate.migrations import _MIGRATIONS

    engine = _file_engine(temp_db_file)
    registry = build_registry()
    upto_v19 = MigrationRegistry()
    for version, name, upgrade in _MIGRATIONS:
        if version > 19:
            break
        upto_v19.register(version, name, upgrade)
    with engine.connect() as conn:
        # 先升到 v19（里程碑1 的十张表已在），插入真实业务行
        apply_migrations(upto_v19, conn)
        assert current_version(conn) == 19
        conn.execute(
            text(
                "INSERT INTO supervision_submission (id, code, project_id, task_id,"
                " version, author_id, summary, ac_snapshot, create_time)"
                " VALUES ('sub1','S-v1','p1','t1',1,'u1','摘要',"
                " '[{\"code\":\"AC-1\",\"statement\":\"跑通基线\"}]',"
                " '2026-01-01 00:00:00')"
            )
        )
        conn.commit()
        apply_migrations(registry, conn)
        assert current_version(conn) == 22
        row = conn.execute(
            text(
                "SELECT summary, ac_snapshot, status FROM supervision_submission"
                " WHERE id = 'sub1'"
            )
        ).one()
        assert row[0] == "摘要"
        assert "AC-1" in row[1]  # 提交里的验收标准快照原样保留
        assert row[2] == "submitted"
    engine.dispose()


def _assert_matches_orm(conn, models) -> None:
    """逐表比对迁移结果与 ORM 定义（列名、索引名、CHECK 约束名）。

    论文检索与 LaTeX 解析的测试都用 ``Base.metadata.create_all`` 建表，迁移
    路径本身不会被它们覆盖；这里钉住"手写 DDL 与模型不漂移"（AGENTS.md：
    迁移必须幂等、可验证）。索引名也比对，否则列表查询会在真实库里静默退化成
    全表扫描；CHECK 名比对是因为那几条口径（永不编译、草稿永不自动发布）是
    刻意放在数据库层的。
    """
    insp = inspect(conn)
    for model in models:
        table = model.__table__
        assert insp.has_table(table.name), table.name
        migrated = {column["name"] for column in insp.get_columns(table.name)}
        expected = {column.name for column in table.columns}
        assert migrated == expected, (table.name, expected - migrated, migrated - expected)
        indexed = {item["name"] for item in insp.get_indexes(table.name)}
        assert {ix.name for ix in table.indexes} <= indexed, (table.name, {ix.name for ix in table.indexes} - indexed)
        checks = {item["name"] for item in insp.get_check_constraints(table.name) if item.get("name")}
        declared = {
            constraint.name
            for constraint in table.constraints
            if getattr(constraint, "name", None) and type(constraint).__name__ == "CheckConstraint"
        }
        assert declared <= checks, (table.name, declared - checks)


def test_v21_literature_schema_matches_the_orm(temp_db_file):
    """v21 建出的七张论文检索表必须与 ORM 定义一致，且重复升级幂等。"""
    from chatchat.server.db.models.supervision_literature_model import (
        SupervisionLiteratureCitationModel,
        SupervisionLiteratureConclusionModel,
        SupervisionLiteratureEvidenceCardModel,
        SupervisionLiteratureExtractionModel,
        SupervisionLiteratureResultModel,
        SupervisionLiteratureSearchTaskModel,
        SupervisionLiteratureSubtaskModel,
    )

    engine = _file_engine(temp_db_file)
    with engine.connect() as conn:
        result = apply_migrations(build_registry(), conn)
        assert result.final_version == 22
        _assert_matches_orm(
            conn,
            (
                SupervisionLiteratureSearchTaskModel,
                SupervisionLiteratureSubtaskModel,
                SupervisionLiteratureResultModel,
                SupervisionLiteratureExtractionModel,
                SupervisionLiteratureEvidenceCardModel,
                SupervisionLiteratureConclusionModel,
                SupervisionLiteratureCitationModel,
            ),
        )
        second = apply_migrations(build_registry(), conn)
        assert second.applied == []
        assert second.final_version == 22
    engine.dispose()


def test_v21_partial_unique_indexes_are_enforced(temp_db_file):
    """候选论文的 doi/arxiv/标题去重是部分唯一索引：迁移后必须真的生效。

    仓储层靠这三个索引做项目内去重（P3-11 断言第二个检索任务不会重复入库），
    如果迁移漏掉 ``WHERE`` 条件，去重会在生产库里静默失效。
    """
    insert = (
        "INSERT INTO supervision_literature_result (id, code, project_id, search_task_id,"
        " subtask_id, dedup_key, doi, title, authors, abstract, source, sources,"
        " fulltext_status, relevance_level, relevance_reason, relevance_matched,"
        " relevance_missing, relevance_score_source, relevance_rules_version,"
        " recommended_action, semantic_analysis, decision, decision_reason, decided_by,"
        " decided_role, decision_history, extract_state, create_time)"
        " VALUES (:id, :code, 'p1', 't1', 's1', :dedup, :doi, :title, 'A', 'ab', 's',"
        " '[]', 'abstract', 'high', '', '[]', '[]', 'rule_keyword', 'lit-rank-v1',"
        " 'reference', '{}', 'pending', '', 'u1', 'student', '[]', 'none',"
        " '2026-01-01 00:00:00')"
    )
    engine = _file_engine(temp_db_file)
    with engine.connect() as conn:
        apply_migrations(build_registry(), conn)
        conn.execute(
            text(insert),
            {"id": "r1", "code": "P-001", "dedup": "k1", "doi": "10.1/a", "title": "同一篇"},
        )
        conn.commit()
        with pytest.raises(IntegrityError):
            conn.execute(
                text(insert),
                {"id": "r2", "code": "P-002", "dedup": "k2", "doi": "10.1/a", "title": "标题不同"},
            )
        conn.rollback()
        # 没有 doi 的行不受该索引约束（部分唯一），仍应能入库
        conn.execute(
            text(insert),
            {"id": "r3", "code": "P-003", "dedup": "k3", "doi": "", "title": "无 doi 的论文"},
        )
        conn.commit()
        assert conn.execute(
            text("SELECT COUNT(*) FROM supervision_literature_result WHERE project_id = 'p1'")
        ).scalar() == 2
    engine.dispose()


def test_v21_upgrade_keeps_existing_ai_review_rows(temp_db_file):
    """升级到 v21 前已存在的预审运行与逐条结论必须原样保留（不重建、不清空）。"""
    from sqlalchemy import insert

    from chatchat.server.db.migrate.migrations import _MIGRATIONS
    from chatchat.server.db.models.supervision_ai_review_model import (
        SupervisionAIReviewFindingModel,
        SupervisionAIReviewRunModel,
    )

    engine = _file_engine(temp_db_file)
    upto_v20 = MigrationRegistry()
    for version, name, upgrade in _MIGRATIONS:
        if version > 20:
            break
        upto_v20.register(version, name, upgrade)
    with engine.connect() as conn:
        apply_migrations(upto_v20, conn)
        assert current_version(conn) == 20
        # 用 ORM 的表描述写行（DDL 仍来自迁移），这样列名不会与模型漂移
        conn.execute(
            insert(SupervisionAIReviewRunModel.__table__).values(
                id="run1",
                code="AR-r1",
                revision=1,
                project_id="p1",
                task_id="t1",
                task_code="T-001",
                submission_id="s1",
                submission_code="S-001",
                submission_version=1,
                trigger_source="mentor_manual",
                triggered_by="u1",
                triggered_role="mentor",
                status="completed",
                check_snapshot={},
                draft_fingerprint="fp",
                ac_version=1,
                input_budget={},
                material_manifest=[],
                unread_materials=[],
                partially_read_materials=[],
                call_plan=[],
                total_batches=1,
                completed_batches=1,
                unchecked_acs=[],
                partial_results=False,
                model_calls=[],
                summary_state="存在缺失项",
                summary_note="说明",
                finding_counts={"partial": 1},
                model_name="m",
                rules_version="rules-1",
                parser_version="parser-1",
                whitelist_version="wl-1",
                budget_config_version="budget-1",
                security_flags=[],
                create_time=datetime.datetime(2026, 1, 1),
            )
        )
        conn.execute(
            insert(SupervisionAIReviewFindingModel.__table__).values(
                id="f1",
                run_id="run1",
                project_id="p1",
                ac_code="AC-1",
                ac_statement="跑通基线",
                ac_version=1,
                required=True,
                check_type="code",
                verdict="partial",
                evidence_depth="E2",
                support_relation="partially_supports",
                reason="缺结果文件",
                satisfied_parts="已有脚本",
                missing_parts="",
                evidence_refs=[],
                missing_materials=[],
                risks=[],
                mentor_focus="",
                injection_suspected=False,
                injection_note="",
                runtime_claim_flag=False,
                downgrade_reason="",
                validated=False,
                batch_index=0,
                create_time=datetime.datetime(2026, 1, 1),
            )
        )
        conn.commit()
        apply_migrations(build_registry(), conn)
        assert current_version(conn) == 22
        row = conn.execute(
            text(
                "SELECT summary_state, status FROM supervision_ai_review_run WHERE id = 'run1'"
            )
        ).one()
        assert row == ("存在缺失项", "completed")
        assert conn.execute(
            text("SELECT COUNT(*) FROM supervision_ai_review_finding WHERE run_id = 'run1'")
        ).scalar() == 1
    engine.dispose()


def test_v22_latex_schema_matches_the_orm(temp_db_file):
    """v22 建出的三张 LaTeX 表必须与 ORM 定义一致，且重复升级幂等。"""
    from chatchat.server.db.models.supervision_latex_model import (
        SupervisionLatexDraftConfirmationModel,
        SupervisionLatexProgressDraftModel,
        SupervisionLatexSnapshotModel,
    )

    engine = _file_engine(temp_db_file)
    with engine.connect() as conn:
        result = apply_migrations(build_registry(), conn)
        assert result.final_version == 22
        _assert_matches_orm(
            conn,
            (
                SupervisionLatexSnapshotModel,
                SupervisionLatexProgressDraftModel,
                SupervisionLatexDraftConfirmationModel,
            ),
        )
        second = apply_migrations(build_registry(), conn)
        assert second.applied == []
        assert second.final_version == 22
    engine.dispose()


def test_v22_latex_guards_are_pinned_in_the_database(temp_db_file):
    """「永不编译」「草稿永不自动发布」「必须学生确认」三条口径钉在 CHECK 上。

    这三条是产品口径而不是实现细节（§16.6）：任何写入想绕过仓储层，数据库
    必须直接拒绝；否则界面上那句"系统不会编译你的稿件"就成了空话。
    """
    from sqlalchemy import insert

    from chatchat.server.db.models.supervision_latex_model import (
        SupervisionLatexProgressDraftModel,
        SupervisionLatexSnapshotModel,
    )

    def snapshot_values(**overrides):
        values = {
            "id": "s1",
            "code": "LX-001",
            "project_id": "p1",
            "attachment_id": "a1",
            "attachment_code": "A-001",
            "attachment_name": "paper.zip",
            "attachment_sha256": "sha",
            "attachment_version": 1,
            "main_file": "main.tex",
            "main_status": "resolved",
            "main_path": "main.tex",
            "main_json": {},
            "parser_version": "p",
            "checker_version": "c",
            "zip_safety_version": "z",
            "limits_json": {},
            "parse_budget_json": {},
            "document_present": True,
            "outline": [],
            "counts": {},
            "findings": [],
            "finding_counts": {},
            "dangerous_findings": [],
            "partially_read": [],
            "not_read": [],
            "unreferenced_tex": [],
            "available_files": [],
            "extracted_files": [],
            "file_count": 1,
            "total_bytes": 10,
            "word_count": 10,
            "compiled": False,
            "created_by": "u1",
            "created_role": "student",
            "create_time": datetime.datetime(2026, 1, 1),
        }
        values.update(overrides)
        return values

    def draft_values(**overrides):
        values = {
            "id": "d1",
            "code": "PD-001",
            "project_id": "p1",
            "library_draft_id": "lib1",
            "draft_version": "v1",
            "parser_version": "p",
            "checker_version": "c",
            "zip_safety_version": "z",
            "status": "draft",
            "published": False,
            "requires_student_confirmation": True,
            "baseline_only": False,
            "snapshot_id": "s1",
            "snapshot_code": "LX-001",
            "attachment_id": "a1",
            "attachment_code": "A-001",
            "attachment_sha256": "sha",
            "attachment_version": 1,
            "main_file": "main.tex",
            "base_attachment_id": "a0",
            "base_attachment_code": "A-000",
            "base_attachment_sha256": "sha0",
            "base_attachment_version": 1,
            "base_main_file": "main.tex",
            "summary": "摘要",
            "caveat": "静态差异不能证明实验已执行",
            "items": [],
            "item_count": 0,
            "truncated": False,
            "dropped_items": 0,
            "draft_json": {},
            "diff_counts": {},
            "word_count_old": 10,
            "word_count_new": 20,
            "word_count_delta": 10,
            "model_summary": "",
            "model_summary_status": "",
            "model_summary_note": "",
            "model_name": "",
            "confirmed_progress_code": "",
            "confirmed_by": "",
            "confirmed_role": "",
            "discarded_by": "",
            "discarded_role": "",
            "discard_reason": "",
            "created_by": "u1",
            "created_role": "student",
            "create_time": datetime.datetime(2026, 1, 1),
        }
        values.update(overrides)
        return values

    engine = _file_engine(temp_db_file)
    with engine.connect() as conn:
        apply_migrations(build_registry(), conn)
        conn.execute(insert(SupervisionLatexSnapshotModel.__table__).values(snapshot_values()))
        conn.commit()
        with pytest.raises(IntegrityError):
            conn.execute(
                insert(SupervisionLatexSnapshotModel.__table__).values(
                    snapshot_values(id="s2", code="LX-002", compiled=True)
                )
            )
        conn.rollback()

        conn.execute(insert(SupervisionLatexProgressDraftModel.__table__).values(draft_values()))
        conn.commit()
        with pytest.raises(IntegrityError):
            conn.execute(
                insert(SupervisionLatexProgressDraftModel.__table__).values(
                    draft_values(id="d2", code="PD-002", published=True)
                )
            )
        conn.rollback()
        with pytest.raises(IntegrityError):
            conn.execute(
                insert(SupervisionLatexProgressDraftModel.__table__).values(
                    draft_values(id="d3", code="PD-003", requires_student_confirmation=False)
                )
            )
        conn.rollback()
        # 状态枚举同样钉在库上：草稿不能直接写成别的状态
        with pytest.raises(IntegrityError):
            conn.execute(
                insert(SupervisionLatexProgressDraftModel.__table__).values(
                    draft_values(id="d4", code="PD-004", status="published")
                )
            )
        conn.rollback()
    engine.dispose()


def test_v22_upgrade_keeps_existing_literature_rows(temp_db_file):
    """升级到 v22 前已存在的检索任务与候选论文必须原样保留（不重建、不清空）。"""
    from sqlalchemy import insert

    from chatchat.server.db.migrate.migrations import _MIGRATIONS
    from chatchat.server.db.models.supervision_literature_model import (
        SupervisionLiteratureResultModel,
        SupervisionLiteratureSearchTaskModel,
    )

    engine = _file_engine(temp_db_file)
    upto_v21 = MigrationRegistry()
    for version, name, upgrade in _MIGRATIONS:
        if version > 21:
            break
        upto_v21.register(version, name, upgrade)
    with engine.connect() as conn:
        apply_migrations(upto_v21, conn)
        assert current_version(conn) == 21
        conn.execute(
            insert(SupervisionLiteratureSearchTaskModel.__table__).values(
                id="t1",
                code="LT-001",
                project_id="p1",
                brief_id="b1",
                brief_code="RB-001",
                brief_version=1,
                brief_kind="shared",
                brief_confirmed_by="u1",
                brief_snapshot={},
                purpose="verify",
                goal="验证基线",
                keywords=["baseline"],
                synonyms=[],
                exclude_terms=[],
                sources=[],
                require_fulltext=False,
                result_limit=30,
                deep_extract_limit=3,
                search_plan={},
                search_plan_note="",
                search_plan_model="",
                search_plan_prompt_version="lit-prompt-v1",
                search_plan_confirmed_by="",
                status="pending",
                hits_total=0,
                deduped_total=0,
                stored_total=0,
                source_failures=[],
                close_reason="",
                created_by="u1",
                created_role="student",
                create_time=datetime.datetime(2026, 1, 1),
            )
        )
        conn.execute(
            insert(SupervisionLiteratureResultModel.__table__).values(
                id="r1",
                code="P-001",
                project_id="p1",
                search_task_id="t1",
                subtask_id="s1",
                dedup_key="k1",
                doi="10.1/a",
                title="论文",
                authors="作者",
                abstract="摘要",
                source="arxiv",
                sources=["arxiv"],
                fulltext_status="abstract",
                relevance_level="high",
                relevance_reason="命中关键词",
                relevance_matched=["baseline"],
                relevance_missing=[],
                relevance_score_source="rule_keyword",
                relevance_rules_version="lit-rank-v1",
                recommended_action="priority_read",
                semantic_analysis={},
                decision="included",
                decision_reason="纳入",
                decided_by="u1",
                decided_role="student",
                decision_history=[],
                extract_state="none",
                create_time=datetime.datetime(2026, 1, 1),
            )
        )
        conn.commit()
        apply_migrations(build_registry(), conn)
        assert current_version(conn) == 22
        task = conn.execute(
            text(
                "SELECT goal, status FROM supervision_literature_search_task WHERE id = 't1'"
            )
        ).one()
        assert task == ("验证基线", "pending")
        paper = conn.execute(
            text(
                "SELECT title, decision, decision_reason FROM supervision_literature_result"
                " WHERE id = 'r1'"
            )
        ).one()
        assert paper == ("论文", "included", "纳入")
    engine.dispose()


if __name__ == "__main__":
    import sys

    sys.exit(pytest.main([__file__, "-q"]))
