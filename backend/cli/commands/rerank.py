"""`dtxwiki rerank` — inspect the configured rerank endpoint.

dtxWiki connects to an external cross-encoder rerank endpoint (TEI / Infinity /
vLLM `/rerank`, or a Cohere/Voyage rerank API); it does NOT run the model
server. `doctor` probes that endpoint so an operator can confirm the connection
before enabling reranking in search.
"""

from __future__ import annotations

import time

import typer

rerank_app = typer.Typer(help="Inspect the configured rerank endpoint.")

_PROBE_QUERY = "What is the capital of France?"
_PROBE_DOCUMENTS = [
    "Bananas are a yellow fruit rich in potassium.",
    "Paris is the capital and most populous city of France.",
    "The Great Wall of China is visible from low orbit.",
]
_EXPECTED_TOP = 1  # the Paris document


@rerank_app.command(name="doctor")
def doctor(
    db: str | None = typer.Option(None, help="Override database URL."),
) -> None:
    """Probe the configured rerank provider: reachability, model, the ordering
    on a tiny fixed query, and latency. dtxWiki only connects to the endpoint;
    it does not manage it."""
    from backend.app.settings import Settings
    from backend.llm.base import RerankRequest
    from backend.llm.registry import build_reranker

    settings = Settings(database_url=db) if db else Settings()
    mode = settings.rerank_provider.strip().lower()
    typer.echo(f"rerank_provider: {mode or 'none'}")
    if settings.rerank_base_url:
        typer.echo(f"base_url:        {settings.rerank_base_url}")
    if settings.rerank_model:
        typer.echo(f"model:           {settings.rerank_model}")
    typer.echo(f"candidate_k:     {settings.rerank_candidate_k}")
    typer.echo(f"top_n:           {settings.rerank_top_n}")

    try:
        reranker = build_reranker(settings)
    except ValueError as exc:
        typer.echo(f"\nconfig error: {exc}")
        raise typer.Exit(code=1) from exc

    if reranker is None:
        typer.echo(
            "\nno rerank provider configured — search ranking is fusion-only "
            "(keyword/semantic/hybrid still work). To connect a rerank server, "
            "set DTXWIKI_RERANK_PROVIDER=api and DTXWIKI_RERANK_BASE_URL to your "
            "TEI/Infinity/vLLM (or Cohere/Voyage) rerank endpoint."
        )
        return

    started = time.perf_counter()
    try:
        response = reranker.rerank(
            RerankRequest(query=_PROBE_QUERY, documents=_PROBE_DOCUMENTS)
        )
    except Exception as exc:  # surface any endpoint failure plainly
        typer.echo(f"\nPROBE FAILED: {type(exc).__name__}: {exc}")
        typer.echo(
            "check that the endpoint is running and reachable, the model is "
            "served, and DTXWIKI_RERANK_BASE_URL points at the /rerank path."
        )
        raise typer.Exit(code=1) from exc

    elapsed_ms = (time.perf_counter() - started) * 1000.0
    if not response.results:
        typer.echo("\nPROBE FAILED: endpoint returned no results.")
        raise typer.Exit(code=1)
    top_index = response.results[0].index
    typer.echo(f"returned ranks:  {[result.index for result in response.results]}")
    typer.echo(f"latency:         {elapsed_ms:.0f} ms")
    if top_index != _EXPECTED_TOP:
        typer.echo(
            f"\nWARNING: expected document {_EXPECTED_TOP} ranked first, got "
            f"{top_index}. The endpoint is reachable but the ordering looks "
            "wrong — verify the model is a reranker (cross-encoder)."
        )
        raise typer.Exit(code=1)
    typer.echo("\nOK — rerank endpoint reachable and ordering is sane.")
