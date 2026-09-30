from backend.app.settings import Settings
from backend.llm.anthropic import AnthropicLLM
from backend.llm.base import EmbeddingProvider, LLMProvider, RerankProvider
from backend.llm.embedding import (
    DeterministicEmbedding,
    OpenAICompatibleEmbedding,
)
from backend.llm.openai import OpenAILLM
from backend.llm.rerank import ApiReranker


def build_llm_provider(settings: Settings) -> LLMProvider | None:
    provider = settings.llm_provider.strip().lower()
    if provider in {"", "none"}:
        return None
    if provider == "anthropic":
        return AnthropicLLM(api_key=settings.anthropic_api_key or "")
    if provider == "openai":
        return OpenAILLM(api_key=settings.openai_api_key or "")
    raise RuntimeError(
        "DTXWIKI_LLM_PROVIDER must be one of: none, anthropic, openai"
    )


def resolve_llm_provider(
    settings: Settings,
) -> tuple[LLMProvider | None, str | None]:
    """Build the LLM provider, turning a misconfiguration into a reason string.

    Ask composes answers with the provider when one is available and falls back
    to the deterministic evidence pack when it is not, so a bad provider setting
    must not take the whole Ask surface down. ``build_llm_provider`` raises so
    that setup/CLI paths still fail loudly; this wrapper is what the request
    path uses, and the returned reason is surfaced to the user as a warning.
    """

    try:
        return build_llm_provider(settings), None
    except (ValueError, RuntimeError) as exc:
        return None, str(exc)


def resolve_embedding_provider(
    settings: Settings,
) -> tuple[EmbeddingProvider | None, str | None]:
    """Same degrade contract as :func:`resolve_llm_provider` for embeddings.

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
    """Same degrade contract as :func:`resolve_llm_provider` for reranking."""

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
