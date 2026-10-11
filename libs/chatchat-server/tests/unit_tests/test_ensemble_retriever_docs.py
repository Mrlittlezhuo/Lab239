"""FAISS 混合检索（BM25 + 向量）的回归测试（离线，不起 FAISS、不调模型）。

现场：知识库检索和 RAG 对话全部失败，报
`AttributeError: 'Document' object has no attribute 'id'`。
`chatchat/server/file_rag/retrievers/ensemble.py` 把 `vectorstore.docstore._dict.values()`
直接喂给 `BM25Retriever.from_documents`，而 langchain_community 0.4.x 的
`from_documents` 会取 `d.id` —— FAISS docstore 里存的是普通 `Document`，没有这个字段。
id 其实就是这张 docstore 的 key，只是没被带上。

修完 BM25 那步后又撞到第二处：langchain_core 1.x 的 retriever 只剩 `invoke`，
`get_relevant_documents` 已移除。
"""
from typing import List

from langchain_core.documents import Document
from langchain_core.retrievers import BaseRetriever

from chatchat.server.file_rag.retrievers.ensemble import EnsembleRetrieverService


class StubVectorRetriever(BaseRetriever):
    """向量检索侧的桩：返回固定文档，不参与 BM25 的构造。"""

    def _get_relevant_documents(self, query: str, *, run_manager) -> List[Document]:
        return [Document(page_content="向量侧命中", metadata={"src": "vs"})]


class StubDocStore:
    def __init__(self, mapping: dict):
        self._dict = mapping


class StubVectorStore:
    """只实现 ensemble 真正用到的两样东西：docstore._dict 和 as_retriever。"""

    def __init__(self, docs_by_id: dict):
        self.docstore = StubDocStore(docs_by_id)

    def as_retriever(self, *, search_type=None, search_kwargs=None):
        return StubVectorRetriever()


def _kb_docs() -> dict:
    # FAISS docstore 的真实形状：key 是 doc id，value 是没有 id 的普通 Document
    return {
        "doc-1": Document(page_content="RAG 检索增强生成的原理", metadata={"source": "a.txt"}),
        "doc-2": Document(page_content="红烧肉的做法与火候", metadata={"source": "b.txt"}),
        "doc-3": Document(page_content="向量数据库的索引类型", metadata={"source": "c.txt"}),
    }


class TestFromVectorstore:
    def test_plain_documents_without_id_no_longer_crash(self):
        service = EnsembleRetrieverService.from_vectorstore(
            StubVectorStore(_kb_docs()), top_k=2, score_threshold=0.4
        )

        assert service.retriever is not None

    def test_bm25_keeps_the_faiss_doc_ids(self):
        service = EnsembleRetrieverService.from_vectorstore(
            StubVectorStore(_kb_docs()), top_k=2, score_threshold=0.4
        )
        bm25 = service.retriever.retrievers[0]

        assert sorted(doc.id for doc in bm25.docs) == ["doc-1", "doc-2", "doc-3"]

    def test_bm25_uses_jieba_tokenization(self):
        service = EnsembleRetrieverService.from_vectorstore(
            StubVectorStore(_kb_docs()), top_k=2, score_threshold=0.4
        )
        bm25 = service.retriever.retrievers[0]

        assert "检索" in bm25.preprocess_func("检索增强生成")

    def test_empty_kb_falls_back_to_vector_retrieval_only(self):
        # 旧写法在空知识库上是 zip() 解包报错
        service = EnsembleRetrieverService.from_vectorstore(
            StubVectorStore({}), top_k=2, score_threshold=0.4
        )

        assert len(service.retriever.retrievers) == 1


class TestRetrieval:
    def test_query_hits_the_matching_document(self):
        service = EnsembleRetrieverService.from_vectorstore(
            StubVectorStore(_kb_docs()), top_k=1, score_threshold=0.4
        )

        docs = service.get_relevant_documents("红烧肉怎么做")

        assert docs and "红烧肉" in docs[0].page_content

    def test_result_is_capped_at_top_k(self):
        service = EnsembleRetrieverService.from_vectorstore(
            StubVectorStore(_kb_docs()), top_k=1, score_threshold=0.4
        )

        assert len(service.get_relevant_documents("检索")) == 1

    def test_empty_kb_returns_the_vector_side_docs(self):
        service = EnsembleRetrieverService.from_vectorstore(
            StubVectorStore({}), top_k=2, score_threshold=0.4
        )

        docs = service.get_relevant_documents("任意问题")

        assert [d.page_content for d in docs] == ["向量侧命中"]
