from __future__ import annotations

from langchain_classic.retrievers import EnsembleRetriever
from langchain_core.vectorstores import VectorStore
from langchain_community.retrievers import BM25Retriever
from langchain_core.retrievers import BaseRetriever

from chatchat.server.file_rag.retrievers.base import BaseRetrieverService


class EnsembleRetrieverService(BaseRetrieverService):
    def do_init(
        self,
        retriever: BaseRetriever = None,
        top_k: int = 5,
    ):
        self.vs = None
        self.top_k = top_k
        self.retriever = retriever

    @staticmethod
    def from_vectorstore(
        vectorstore: VectorStore,
        top_k: int,
        score_threshold: int | float,
    ):
        faiss_retriever = vectorstore.as_retriever(
            search_type="similarity_score_threshold",
            search_kwargs={"score_threshold": score_threshold, "k": top_k},
        )
        # TODO: 换个不用torch的实现方式
        # from cutword.cutword import Cutter
        import jieba

        # cutter = Cutter()
        # docstore._dict 是 {faiss_doc_id: Document}。BM25Retriever.from_documents 在
        # langchain_community 0.4.x 里会去取 `d.id`，而 FAISS docstore 存的是普通
        # `Document`（没有 id），于是每次检索都抛
        # `'Document' object has no attribute 'id'` —— 知识库检索和 RAG 对话全废。
        # id 本来就是这张表的 key，走 from_texts 把它显式传进去。
        doc_entries = list(vectorstore.docstore._dict.items())
        if doc_entries:
            doc_ids, docs = zip(*doc_entries)
            bm25_retriever = BM25Retriever.from_texts(
                texts=[doc.page_content for doc in docs],
                metadatas=[doc.metadata for doc in docs],
                ids=list(doc_ids),
                preprocess_func=jieba.lcut_for_search,
            )
            bm25_retriever.k = top_k
            retrievers = [bm25_retriever, faiss_retriever]
            weights = [0.5, 0.5]
        else:
            # 空知识库：没有语料可建 BM25 索引（旧写法在这里是 zip() 解包报错），
            # 退化成只用向量检索
            retrievers = [faiss_retriever]
            weights = [1.0]

        ensemble_retriever = EnsembleRetriever(
            retrievers=retrievers, weights=weights
        )
        return EnsembleRetrieverService(retriever=ensemble_retriever, top_k=top_k)

    def get_relevant_documents(self, query: str):
        # langchain_core 1.x 的 retriever 只剩 `invoke`（`get_relevant_documents`
        # 已移除），这里的方法名是上层（faiss_kb_service.do_search）在调，保持不变
        return self.retriever.invoke(query)[: self.top_k]
