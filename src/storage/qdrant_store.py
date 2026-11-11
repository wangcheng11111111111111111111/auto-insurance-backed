"""
Qdrant 向量数据库 - 知识库语义检索
将知识库术语向量化存储，支持自然语言查询匹配
"""
import json
import logging
from typing import Optional

from qdrant_client import QdrantClient
from qdrant_client.http import models as qmodels

from config.settings import settings
from src.services.llm_service import llm_service

logger = logging.getLogger(__name__)


class QdrantKnowledgeStore:
    """基于 Qdrant 的知识库向量存储"""

    COLLECTION_NAME = settings.knowledge.QDRANT_COLLECTION
    VECTOR_SIZE = settings.embedding.DIMENSIONS

    def __init__(self):
        self._client: Optional[QdrantClient] = None

    def connect(self):
        self._client = QdrantClient(
            host=settings.knowledge.QDRANT_HOST,
            port=settings.knowledge.QDRANT_PORT,
        )
        logger.info(
            "Qdrant 已连接: %s:%s",
            settings.knowledge.QDRANT_HOST,
            settings.knowledge.QDRANT_PORT,
        )

    def close(self):
        if self._client:
            self._client.close()
            logger.info("Qdrant 连接已关闭")

    @property
    def client(self) -> QdrantClient:
        if self._client is None:
            raise RuntimeError("Qdrant 未连接，请先调用 connect()")
        return self._client

    async def init_collection(self):
        """初始化集合，维度不匹配则自动重建"""
        collections = self.client.get_collections()
        existing = {c.name: c for c in collections.collections}

        if self.COLLECTION_NAME in existing:
            current_config = self.client.get_collection(self.COLLECTION_NAME)
            current_dim = current_config.config.params.vectors.size
            if current_dim != self.VECTOR_SIZE:
                logger.warning(
                    "集合维度不匹配 (当前=%d, 需要=%d)，自动重建",
                    current_dim, self.VECTOR_SIZE,
                )
                self.client.delete_collection(self.COLLECTION_NAME)
            else:
                logger.info("Qdrant 集合已存在: %s (dim=%d)", self.COLLECTION_NAME, self.VECTOR_SIZE)
                return

        self.client.create_collection(
            collection_name=self.COLLECTION_NAME,
            vectors_config=qmodels.VectorParams(
                size=self.VECTOR_SIZE,
                distance=qmodels.Distance.COSINE,
            ),
        )
        logger.info("创建 Qdrant 集合: %s (dim=%d)", self.COLLECTION_NAME, self.VECTOR_SIZE)

    async def upsert_terms(self, terms: list[dict]):
        """
        批量写入知识库术语

        Args:
            terms: [{"standard_term": "第三者责任险", "alias": "三者", "category": "insurance_type"}, ...]
        """
        if not terms:
            return

        texts = [f"{t['standard_term']}：{t['alias']}" for t in terms]
        embeddings = await llm_service.embed(texts)

        points = []
        for i, (term, embedding) in enumerate(zip(terms, embeddings)):
            points.append(
                qmodels.PointStruct(
                    id=i + 1,  # 简单自增 ID
                    vector=embedding,
                    payload={
                        "standard_term": term["standard_term"],
                        "alias": term["alias"],
                        "category": term.get("category", ""),
                    },
                )
            )

        self.client.upsert(
            collection_name=self.COLLECTION_NAME,
            points=points,
        )
        logger.info("写入 %d 条知识库术语到 Qdrant", len(points))

    async def search(self, query: str, top_k: int = 5, category: Optional[str] = None) -> list[dict]:
        """
        语义检索知识库术语

        Args:
            query: 用户查询文本（口语化表达）
            top_k: 返回数量
            category: 可选筛选分类

        Returns:
            [{"standard_term": "第三者责任险", "alias": "三者", "score": 0.95}, ...]
        """
        query_vector = (await llm_service.embed([query]))[0]

        search_filter = None
        if category:
            search_filter = qmodels.Filter(
                must=[qmodels.FieldCondition(key="category", match=qmodels.MatchValue(value=category))]
            )

        results = self.client.search(
            collection_name=self.COLLECTION_NAME,
            query_vector=query_vector,
            limit=top_k,
            query_filter=search_filter,
        )

        return [
            {
                "standard_term": hit.payload.get("standard_term", ""),
                "alias": hit.payload.get("alias", ""),
                "category": hit.payload.get("category", ""),
                "score": round(hit.score, 4),
            }
            for hit in results
        ]

    async def normalize(self, slang: str) -> str:
        """
        将口语化表达标准化为行业术语

        Args:
            slang: 口语化表达，如 "三者"

        Returns:
            标准术语，如 "第三者责任险"
        """
        results = await self.search(slang, top_k=1)
        if results and results[0]["score"] > 0.85:
            return results[0]["standard_term"]
        return slang

    async def get_aliases(self, standard_term: str) -> list[str]:
        """
        获取标准术语的所有别名

        Args:
            standard_term: 行业标准术语

        Returns:
            ["三者", "三责险", ...]
        """
        results = self.client.scroll(
            collection_name=self.COLLECTION_NAME,
            scroll_filter=qmodels.Filter(
                must=[qmodels.FieldCondition(
                    key="standard_term",
                    match=qmodels.MatchValue(value=standard_term),
                )]
            ),
            limit=20,
        )
        return [p.payload.get("alias", "") for p in results[0] if p.payload and p.payload.get("alias")]

    async def find_matches(self, slang: str, threshold: float = 0.7) -> list[dict]:
        """
        模糊匹配 - 返回所有超过阈值的匹配项

        Args:
            slang: 口语/缩写
            threshold: 相似度阈值

        Returns:
            匹配结果列表
        """
        results = await self.search(slang, top_k=5)
        return [r for r in results if r["score"] >= threshold]


qdrant_store = QdrantKnowledgeStore()