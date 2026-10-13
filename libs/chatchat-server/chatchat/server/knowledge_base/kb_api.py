import urllib

from fastapi import Body, Depends

from chatchat.settings import Settings
from chatchat.server.auth.deps import require_password_changed
from chatchat.server.db.models.user_model import UserModel
from chatchat.server.db.repository.knowledge_base_repository import list_kbs_from_db
from chatchat.server.db.repository.knowledge_file_repository import get_kb_statistics
from chatchat.server.knowledge_base.kb_service.base import KBServiceFactory
from chatchat.server.knowledge_base.utils import validate_kb_name
from chatchat.server.utils import BaseResponse, ListResponse, get_default_embedding
from chatchat.utils import build_logger


logger = build_logger()


def log_kb_operation(user: UserModel, operation: str, knowledge_base_name: str, detail: str = ""):
    """记录公共知识库最小操作审计信息。

    操作者恒取自服务端认证上下文（``user``），不接受客户端指定；公共知识库
    保持全局共享，不引入 owner/ACL，仅记录「谁对哪个知识库做了什么」。
    """
    operator = getattr(user, "username", None) if user is not None else "unknown"
    if detail:
        logger.info(f"KB操作审计: 操作者={operator}, 操作={operation}, 知识库={knowledge_base_name}, 详情={detail}")
    else:
        logger.info(f"KB操作审计: 操作者={operator}, 操作={operation}, 知识库={knowledge_base_name}")


def list_kbs():
    # Get List of Knowledge Base
    return ListResponse(data=list_kbs_from_db())


def list_kb_statistics() -> ListResponse:
    """各知识库的入库数据统计，供知识库列表一次取齐。

    单独一个接口而不是并进 ``list_knowledge_bases``：后者返回
    ``KnowledgeBaseSchema``，其 ``file_count`` 是增删时维护的计数器，可能与实际
    行数漂移；统计接口按 ``knowledge_file`` 现算，两者口径不同，混在一个响应里
    会让调用方分不清该信哪个。
    """
    return ListResponse(data=get_kb_statistics())


def create_kb(
    knowledge_base_name: str = Body(..., examples=["samples"]),
    vector_store_type: str = Body(Settings.kb_settings.DEFAULT_VS_TYPE),
    kb_info: str = Body("", description="知识库内容简介，用于Agent选择知识库。"),
    embed_model: str = Body(get_default_embedding()),
    user: UserModel = Depends(require_password_changed),
) -> BaseResponse:
    # Create selected knowledge base
    if not validate_kb_name(knowledge_base_name):
        return BaseResponse(code=403, msg="Don't attack me")
    if knowledge_base_name is None or knowledge_base_name.strip() == "":
        return BaseResponse(code=404, msg="知识库名称不能为空，请重新填写知识库名称")

    kb = KBServiceFactory.get_service_by_name(knowledge_base_name)
    if kb is not None:
        return BaseResponse(code=404, msg=f"已存在同名知识库 {knowledge_base_name}")

    kb = KBServiceFactory.get_service(
        knowledge_base_name, vector_store_type, embed_model, kb_info=kb_info
    )
    try:
        kb.create_kb()
    except Exception as e:
        msg = f"创建知识库出错： {e}"
        logger.error(f"{e.__class__.__name__}: {msg}")
        return BaseResponse(code=500, msg=msg)

    log_kb_operation(
        user, "create", knowledge_base_name,
        f"vs_type={vector_store_type}, embed_model={embed_model}",
    )
    return BaseResponse(code=200, msg=f"已新增知识库 {knowledge_base_name}")


def delete_kb(
    knowledge_base_name: str = Body(..., examples=["samples"]),
    user: UserModel = Depends(require_password_changed),
) -> BaseResponse:
    # Delete selected knowledge base
    if not validate_kb_name(knowledge_base_name):
        return BaseResponse(code=403, msg="Don't attack me")
    knowledge_base_name = urllib.parse.unquote(knowledge_base_name)

    kb = KBServiceFactory.get_service_by_name(knowledge_base_name)

    if kb is None:
        return BaseResponse(code=404, msg=f"未找到知识库 {knowledge_base_name}")

    try:
        status = kb.clear_vs()
        status = kb.drop_kb()
        if status:
            log_kb_operation(user, "delete", knowledge_base_name)
            return BaseResponse(code=200, msg=f"成功删除知识库 {knowledge_base_name}")
    except Exception as e:
        msg = f"删除知识库时出现意外： {e}"
        logger.error(f"{e.__class__.__name__}: {msg}")
        return BaseResponse(code=500, msg=msg)

    return BaseResponse(code=500, msg=f"删除知识库失败 {knowledge_base_name}")
