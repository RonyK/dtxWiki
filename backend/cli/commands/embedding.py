"""`dtxwiki embedding` — inspect the configured embedding endpoint.

dtxWiki connects to an external OpenAI-compatible embedding endpoint
(Ollama / vLLM / TEI / a cloud API); it does NOT run or manage the model
server. `doctor` probes that endpoint so an operator can confirm the
connection before indexing.
"""

from __future__ import annotations

import time

import typer

embedding_app = typer.Typer(help="Inspect the configured embedding endpoint.")


@embedding_app.command(name="doctor")
def doctor(
    db: str | None = typer.Option(None, help="Override database URL."),
) -> None:
    """Probe the configured embedding provider (e.g. an Ollama/vLLM/TEI
    OpenAI-compatible endpoint): reachability, model, returned dimension, and
    latency. dtxWiki only connects to the endpoint; it does not manage it."""
    from backend.app.settings import Settings
    from backend.llm.base import EmbeddingRequest
    from backend.llm.registry import build_embedding_provider

    settings = Settings(database_url=db) if db else Settings()
    mode = settings.embedding_provider.strip().lower()
    typer.echo(f"embedding_provider: {mode or 'none'}")
    if settings.embedding_base_url:
        typer.echo(f"base_url:           {settings.embedding_base_url}")
    if settings.embedding_model:
        typer.echo(f"model:              {settings.embedding_model}")
    typer.echo(
        f"configured dim:     {settings.embedding_dim or '(provider default)'}"
    )

    try:
        embedder = build_embedding_provider(settings)
    except ValueError as exc:
        typer.echo(f"\nconfig error: {exc}")
        raise typer.Exit(code=1) from exc

    if embedder is None:
        typer.echo(
            "\nno embedding provider configured — semantic/hybrid search is off "
            "(keyword + navigational index still work). To connect a local model "
            "server, set DTXWIKI_EMBEDDING_PROVIDER=openai_compatible and "
            "DTXWIKI_EMBEDDING_BASE_URL to your Ollama/vLLM /v1 endpoint."
        )
        return

    typer.echo(f"declared dim:       {embedder.dimensions}")
    started = time.perf_counter()
    try:
        response = embedder.embed(EmbeddingRequest(texts=["dtxwiki embedding probe"]))
    except Exception as exc:  # surface any endpoint failure plainly
        typer.echo(f"\nPROBE FAILED: {type(exc).__name__}: {exc}")
        typer.echo(
            "check that the endpoint is running and reachable, the model is "
            "pulled/served, and DTXWIKI_EMBEDDING_DIM matches the model."
        )
        raise typer.Exit(code=1) from exc

    elapsed_ms = (time.perf_counter() - started) * 1000.0
    returned_dim = len(response.embeddings[0]) if response.embeddings else 0
    typer.echo(f"returned dim:       {returned_dim}")
    typer.echo(f"latency:            {elapsed_ms:.0f} ms")
    if returned_dim != embedder.dimensions:
        typer.echo(
            f"\nWARNING: returned dim {returned_dim} != configured "
            f"{embedder.dimensions}. Set DTXWIKI_EMBEDDING_DIM={returned_dim} and "
            "run `dtxwiki reindex`."
        )
        raise typer.Exit(code=1)
    typer.echo("\nOK — embedding endpoint reachable and dimension matches.")
