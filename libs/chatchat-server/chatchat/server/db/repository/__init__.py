from .conversation_repository import *
from .knowledge_base_repository import *
from .knowledge_file_repository import *
from .message_repository import *
from .human_message_event_repository import *
from .memory_repository import *
from .session_summary_repository import *
from .agent_run_repository import *
from .research_repository import *

# 触及 user_account 的仓储（user_repository / supervision_repository）刻意不进
# 本包的星号导入：`knowledge_base.migrate.create_tables()` 会先导入本包，任何
# 间接导入 user_model 的模块都会把 user_account 注册进 Base.metadata，旧建表
# 路径于是绕过 v2 认证迁移直接建出用户表。需要时用
# `from chatchat.server.db.repository import supervision_repository as repo`
# 显式取子模块，回归由 test_auth.py 的
# test_legacy_create_tables_does_not_bypass_v2_migration 钉住。
