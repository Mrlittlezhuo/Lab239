"""RAG 会话历史与结构化引用的离线单元测试。"""

from chatchat.server.api_server.conversation_routes import _msg_out
from chatchat.server.db.models.message_model import MessageModel
from chatchat.server.knowledge_base.utils import build_references, format_reference
from chatchat.settings import KBSettings, ToolSettings
from chatchat.webui_pages.auth_state import (
    RAG_CONVERSATION_LIST_KEY,
    RAG_CONVERSATION_SELECTOR_KEY,
    clear_private_state,
)
from chatchat.webui_pages.rag_helpers import (
    rag_conversation_options,
    rag_history_items,
    reference_markdown,
)


def test_build_references_preserves_source_details_and_legacy_markdown():
    docs = [
        {
            "page_content": "检索片段",
            "metadata": {"source": "论文 A.pdf", "page": 0},
            "score": 0.125,
        }
    ]

    refs = build_references("论文库", docs, "http://api.test/")

    assert refs == [
        {
            "index": 1,
            "knowledge_base": "论文库",
            "file_name": "论文 A.pdf",
            "url": (
                "http://api.test/knowledge_base/download_doc?"
                "knowledge_base_name=%E8%AE%BA%E6%96%87%E5%BA%93&"
                "file_name=%E8%AE%BA%E6%96%87+A.pdf"
            ),
            "content": "检索片段",
            "score": 0.125,
            "page": 0,
        }
    ]
    legacy = format_reference("论文库", docs, "http://api.test/")
    assert "出处 [1]" in legacy[0]
    assert "页码 0" in legacy[0]
    assert "检索分数 0.1250" in legacy[0]


def test_default_document_score_is_not_presented_as_real_score():
    refs = build_references(
        "kb",
        [{"page_content": "text", "metadata": {"source": "a.txt"}, "score": 3.0}],
        "http://api.test",
    )
    assert refs[0]["score"] is None


def test_default_retrieval_threshold_matches_langchain_range():
    assert 0 <= KBSettings().SCORE_THRESHOLD <= 1
    assert 0 <= ToolSettings().search_local_knowledgebase["score_threshold"] <= 1


def test_message_history_api_exposes_persisted_reference_metadata():
    message = MessageModel(
        id="m1",
        conversation_id="c1",
        chat_type="kb_chat",
        query="问题",
        response="回答 [1]",
        meta_data={"retrieval_status": "matched", "references": [{"index": 1}]},
    )

    output = _msg_out(message)

    assert output.metadata["retrieval_status"] == "matched"
    assert output.metadata["references"] == [{"index": 1}]


def test_rag_history_filters_conversations_and_restores_references():
    options = rag_conversation_options(
        [
            {"id": "normal", "name": "普通", "chat_type": "agent_chat"},
            {"id": "rag", "name": "资料问答", "chat_type": "kb_chat"},
        ]
    )
    assert options == [{"id": "rag", "name": "资料问答"}]

    messages = [
        {
            "id": "m1",
            "query": "问题",
            "response": "回答 [1]",
            "metadata": {
                "retrieval_status": "matched",
                "references": [
                    {
                        "index": 1,
                        "file_name": "paper.pdf",
                        "url": "http://api.test/paper.pdf",
                        "content": "证据",
                        "page": 2,
                        "score": 0.25,
                    }
                ],
            },
        }
    ]
    history = rag_history_items(messages)
    assert history[0] == {"role": "user", "content": "问题"}
    assert history[1]["content"] == "回答 [1]"
    assert "paper.pdf" in history[1]["references"]
    assert "页码 2" in history[1]["references"]
    assert "检索分数 0.2500" in history[1]["references"]


def test_reference_fallbacks_and_logout_clear_rag_state():
    assert reference_markdown({"docs": ["旧引用"]}) == "旧引用"
    assert "模型自身知识" in reference_markdown({"retrieval_status": "empty"})
    assert "连接失败" in reference_markdown(
        {"retrieval_status": "error", "error": "连接失败"}
    )

    session = {
        RAG_CONVERSATION_LIST_KEY: [{"id": "rag"}],
        RAG_CONVERSATION_SELECTOR_KEY: "rag",
        "unrelated": "keep",
    }
    clear_private_state(session)
    assert session == {"unrelated": "keep"}
