from backend.app.settings import Settings
from backend.llm.base import EmbeddingProvider, RerankProvider
from backend.llm.embedding import (
    DeterministicEmbedding,
    OpenAICompatibleEmbedding,
)
from backend.llm.rerank import ApiReranker


def resolve_embedding_provider(
    settings: Settings,
) -> tuple[EmbeddingProvider | None, str | None]:
    """Turn an embedding misconfiguration into a reason string.

    A bad embedding setting only costs the semantic/hybrid lanes; keyword search
    still works, so search must degrade instead of failing the request.
    """

    try:
        return build_embedding_provider(settings), None
    except (ValueError, RuntimeError) as exc:
        return None, str(exc)


def resolve_reranker(
    settings: Settings,
) -> tuple[RerankProvider | None, str | None]:
    """Same degrade contract as :func:`resolve_embedding_provider` for reranking."""

    try:
        return build_reranker(settings), None
    except (ValueError, RuntimeError) as exc:
        return None, str(exc)


def build_embedding_provider(settings: Settings) -> EmbeddingProvider | None:
    mode = settings.embedding_provider.strip().lower()
    if mode in {"", "none"}:
        return None
    if mode == "deterministic":
        return DeterministicEmbedding(dim=settings.embedding_dim or 768)
    if mode == "openai_compatible":
        if not settings.embedding_base_url:
            raise ValueError(
                "DTXWIKI_EMBEDDING_BASE_URL is required when "
                "DTXWIKI_EMBEDDING_PROVIDER=openai_compatible"
            )
        return OpenAICompatibleEmbedding(
            base_url=settings.embedding_base_url,
            model=settings.embedding_model or "text-embedding-3-small",
            dim=settings.embedding_dim or 1536,
            api_key=settings.embedding_api_key,
        )
    raise ValueError(
        "DTXWIKI_EMBEDDING_PROVIDER must be one of: "
        "none, deterministic, openai_compatible"
    )


def build_reranker(settings: Settings) -> RerankProvider | None:
    mode = settings.rerank_provider.strip().lower()
    if mode in {"", "none"}:
        return None
    if mode == "api":
        if not settings.rerank_base_url:
            raise ValueError(
                "DTXWIKI_RERANK_BASE_URL is required when "
                "DTXWIKI_RERANK_PROVIDER=api"
            )
        return ApiReranker(
            base_url=settings.rerank_base_url,
            model=settings.rerank_model or "rerank",
            api_key=settings.rerank_api_key,
        )
    raise ValueError("DTXWIKI_RERANK_PROVIDER must be one of: none, api")


def assert_embedding_dim(
    provider: EmbeddingProvider | None, expected_dim: int
) -> None:
    """Boot/setup guard: the active provider's vector width must equal the dim
    the index was built with. A mismatch means the model changed -> reindex."""
    if provider is not None and provider.dimensions != expected_dim:
        raise RuntimeError(
            f"active embedding dim {provider.dimensions} != index dim "
            f"{expected_dim}; run `dtxwiki reindex --embedding <model>` after "
            "changing the embedding model."
        )
