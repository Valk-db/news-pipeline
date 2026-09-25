"""Embedding service for semantic enrichment using pgvector."""

from typing import List, Dict, Any, Optional
import numpy as np
import logging

logger = logging.getLogger(__name__)


class EmbeddingService:
    """Generate and manage embeddings for articles and stories."""

    def __init__(
        self,
        model_name: str = "sentence-transformers/all-MiniLM-L6-v2",
        use_local: bool = True,
        api_key: Optional[str] = None,
    ):
        """
        Initialize embedding service.

        Args:
            model_name: Model to use (local or API)
            use_local: If True, use sentence-transformers locally. If False, use API.
            api_key: API key for remote embedding service (Cohere, OpenAI, etc.)
        """
        self.model_name = model_name
        self.use_local = use_local
        self.api_key = api_key
        self._local_model = None
        self.dimensions = 384  # Default for all-MiniLM-L6-v2

    def _get_local_model(self):
        """Lazy load local sentence-transformers model."""
        if self._local_model is None:
            try:
                from sentence_transformers import SentenceTransformer
                self._local_model = SentenceTransformer(self.model_name)
                self.dimensions = self._local_model.get_sentence_embedding_dimension()
                logger.info(f"Loaded local embedding model: {self.model_name} ({self.dimensions} dims)")
            except ImportError:
                logger.error("sentence-transformers not installed. Install with: pip install sentence-transformers")
                raise
            except Exception as e:
                logger.error(f"Failed to load local model: {e}")
                raise
        return self._local_model

    async def generate_embedding(self, text: str) -> List[float]:
        """Generate embedding for a single text."""
        if self.use_local:
            return await self._generate_local(text)
        else:
            return await self._generate_api(text)

    async def generate_embeddings(self, texts: List[str]) -> List[List[float]]:
        """Generate embeddings for multiple texts (batch)."""
        if self.use_local:
            return await self._generate_local_batch(texts)
        else:
            return await self._generate_api_batch(texts)

    async def _generate_local(self, text: str) -> List[float]:
        """Generate embedding using local model."""
        model = self._get_local_model()
        embedding = model.encode(text, convert_to_numpy=True)
        return embedding.tolist()

    async def _generate_local_batch(self, texts: List[str]) -> List[List[float]]:
        """Generate embeddings using local model (batch)."""
        model = self._get_local_model()
        embeddings = model.encode(texts, convert_to_numpy=True, batch_size=32, show_progress_bar=False)
        return embeddings.tolist()

    async def _generate_api(self, text: str) -> List[float]:
        """Generate embedding using remote API."""
        # Support for Cohere, OpenAI, etc.
        if "cohere" in self.model_name.lower():
            return await self._generate_cohere(text)
        elif "openai" in self.model_name.lower():
            return await self._generate_openai(text)
        else:
            raise ValueError(f"Unsupported API model: {self.model_name}")

    async def _generate_cohere(self, text: str) -> List[float]:
        """Generate embedding using Cohere API."""
        import httpx

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        data = {
            "model": "embed-english-v3.0",
            "texts": [text],
            "input_type": "search_document",
        }

        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                "https://api.cohere.ai/v1/embed",
                headers=headers,
                json=data,
            )
            response.raise_for_status()
            result = response.json()
            return result["embeddings"][0]

    async def _generate_openai(self, text: str) -> List[float]:
        """Generate embedding using OpenAI API."""
        import httpx

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        data = {
            "model": "text-embedding-3-small",
            "input": text,
        }

        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                "https://api.openai.com/v1/embeddings",
                headers=headers,
                json=data,
            )
            response.raise_for_status()
            result = response.json()
            return result["data"][0]["embedding"]

    async def _generate_api_batch(self, texts: List[str]) -> List[List[float]]:
        """Generate embeddings using remote API (batch)."""
        if "cohere" in self.model_name.lower():
            return await self._generate_cohere_batch(texts)
        elif "openai" in self.model_name.lower():
            return await self._generate_openai_batch(texts)
        else:
            raise ValueError(f"Unsupported API model: {self.model_name}")

    async def _generate_cohere_batch(self, texts: List[str]) -> List[List[float]]:
        """Generate embeddings using Cohere API (batch)."""
        import httpx

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        data = {
            "model": "embed-english-v3.0",
            "texts": texts,
            "input_type": "search_document",
        }

        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(
                "https://api.cohere.ai/v1/embed",
                headers=headers,
                json=data,
            )
            response.raise_for_status()
            result = response.json()
            return result["embeddings"]

    async def _generate_openai_batch(self, texts: List[str]) -> List[List[float]]:
        """Generate embeddings using OpenAI API (batch)."""
        import httpx

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        data = {
            "model": "text-embedding-3-small",
            "input": texts,
        }

        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(
                "https://api.openai.com/v1/embeddings",
                headers=headers,
                json=data,
            )
            response.raise_for_status()
            result = response.json()
            return [item["embedding"] for item in result["data"]]


# Global embedding service instance
_embedding_service: Optional[EmbeddingService] = None


def get_embedding_service(
    model_name: str = "sentence-transformers/all-MiniLM-L6-v2",
    use_local: bool = True,
    api_key: Optional[str] = None,
) -> EmbeddingService:
    """Get or create global embedding service."""
    global _embedding_service
    if _embedding_service is None:
        _embedding_service = EmbeddingService(model_name, use_local, api_key)
    return _embedding_service


async def embed_article(article_id: str, text: str) -> Dict[str, Any]:
    """
    Generate embedding for an article and return data for storage.

    Args:
        article_id: Article UUID
        text: Text to embed (title + body)

    Returns:
        Dict with embedding data for ArticleEmbedding model
    """
    service = get_embedding_service()
    embedding = await service.generate_embedding(text)

    return {
        "article_id": article_id,
        "model": service.model_name,
        "embedding": embedding,
        "dimensions": service.dimensions,
    }


async def embed_story(story_id: str, texts: List[str]) -> Dict[str, Any]:
    """
    Generate embedding for a story (aggregated from articles).

    Args:
        story_id: Story UUID
        texts: List of article texts to aggregate

    Returns:
        Dict with embedding data for StoryEmbedding model
    """
    service = get_embedding_service()

    # Combine texts (weighted average or concatenation)
    combined_text = " ".join(texts[:10])  # Limit to top 10 articles

    embedding = await service.generate_embedding(combined_text)

    return {
        "story_id": story_id,
        "model": service.model_name,
        "embedding": embedding,
        "dimensions": service.dimensions,
    }


async def find_similar_articles(
    query_embedding: List[float],
    limit: int = 10,
    threshold: float = 0.7,
) -> List[Dict[str, Any]]:
    """
    Find similar articles using pgvector similarity search.

    This is a placeholder - actual implementation requires database query.
    """
    # This would use pgvector's <-> operator for cosine distance
    # Example query:
    # SELECT article_id, embedding <-> $1 as distance
    # FROM article_embeddings
    # WHERE embedding <-> $1 < $2
    # ORDER BY distance ASC
    # LIMIT $3

    return []


async def find_similar_stories(
    query_embedding: List[float],
    limit: int = 10,
    threshold: float = 0.7,
) -> List[Dict[str, Any]]:
    """
    Find similar stories using pgvector similarity search.
    """
    return []


# Utility functions for embedding management
def cosine_similarity(a: List[float], b: List[float]) -> float:
    """Compute cosine similarity between two vectors."""
    a_np = np.array(a)
    b_np = np.array(b)
    return float(np.dot(a_np, b_np) / (np.linalg.norm(a_np) * np.linalg.norm(b_np)))


def normalize_embedding(embedding: List[float]) -> List[float]:
    """Normalize embedding to unit length."""
    arr = np.array(embedding)
    norm = np.linalg.norm(arr)
    if norm == 0:
        return embedding
    return (arr / norm).tolist()


def average_embeddings(embeddings: List[List[float]]) -> List[float]:
    """Compute average of multiple embeddings."""
    if not embeddings:
        return []
    arr = np.array(embeddings)
    avg = np.mean(arr, axis=0)
    return normalize_embedding(avg.tolist())


# For clustering viewpoints
async def cluster_embeddings(
    embeddings: List[List[float]],
    threshold: float = 0.7,
) -> List[List[int]]:
    """
    Cluster embeddings by similarity.

    Returns list of clusters (each cluster is list of indices).
    Simple greedy clustering - can be replaced with DBSCAN, etc.
    """
    if not embeddings:
        return []

    clusters = []
    used = set()

    for i, emb in enumerate(embeddings):
        if i in used:
            continue

        cluster = [i]
        used.add(i)

        for j, other_emb in enumerate(embeddings):
            if j in used:
                continue
            sim = cosine_similarity(emb, other_emb)
            if sim >= threshold:
                cluster.append(j)
                used.add(j)

        clusters.append(cluster)

    return clusters