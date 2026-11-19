"""Reusable LLM adapter contract check; provider fixtures belong to the adapter."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field

from backend.llm.adapter_registry import BUILTIN_NAMESPACE, LLMAdapterRegistry
from backend.spi.llm import (
    CanonicalTextRequest,
    CanonicalTextResult,
    Delivery,
    ExecutionContext,
    LLMAdapterError,
    LLMAdapterFactory,
    LLMErrorCode,
    TransportResponse,
)


@dataclass(frozen=True)
class RecordedCall:
    url: str
    # Credentials travel in headers; keep them out of assertion output.
    headers: Mapping[str, str] = field(repr=False)
    payload: Mapping[str, object] = field(repr=False)
    timeout_seconds: float = 0.0


@dataclass(frozen=True)
class WireCase:
    """One scripted provider reply and the canonical outcome it must produce.

    ``reply`` is what the transport returns, or the failure it raises.
    Exactly one of ``expected`` and ``expected_error`` (code, delivery) is
    set. ``check_wire`` asserts provider-specific request details.
    ``expected_sends`` is 1, or 0 for a request the adapter must refuse.
    """

    name: str
    request: CanonicalTextRequest
    reply: TransportResponse | LLMAdapterError | None
    expected: CanonicalTextResult | None = None
    expected_error: tuple[LLMErrorCode, Delivery] | None = None
    check_wire: Callable[[RecordedCall], None] | None = None
    expected_sends: int = 1


@dataclass
class _ScriptedTransport:
    reply: TransportResponse | LLMAdapterError | None
    calls: list[RecordedCall] = field(default_factory=list)

    async def post_json(self, url, *, headers, payload, timeout_seconds):
        self.calls.append(
            RecordedCall(
                url=url,
                headers=dict(headers),
                payload=json.loads(json.dumps(dict(payload))),
                timeout_seconds=timeout_seconds,
            )
        )
        if isinstance(self.reply, LLMAdapterError):
            raise self.reply
        assert self.reply is not None, "the adapter sent a request it should not"
        return self.reply


async def assert_llm_adapter_contract(
    factory: LLMAdapterFactory,
    *,
    credential: Mapping[str, str],
    cases: Iterable[WireCase],
    base_url: str | None = None,
    config: Mapping[str, object] | None = None,
    context: ExecutionContext | None = None,
) -> None:
    """Register ``factory`` as Core would and run each case through it.

    Checks the registration rules, the canonical outcome of every case, one
    provider call per ``generate`` (no hidden retry), the execution timeout,
    and that no credential value is placed in the URL or the body.
    """

    adapter_id = factory.descriptor.adapter_id
    namespace = adapter_id.split(".", 1)[0]
    registry = (
        LLMAdapterRegistry(builtin=[factory])
        if namespace == BUILTIN_NAMESPACE
        else LLMAdapterRegistry(plugins={namespace: [factory]})
    )
    context = context or ExecutionContext(timeout_seconds=30.0)
    secrets = [value for value in credential.values() if value]
    for case in cases:
        assert (case.expected is None) != (case.expected_error is None), case.name
        # More than one call per generate() would be a hidden retry.
        assert case.expected_sends in (0, 1), case.name
        transport = _ScriptedTransport(case.reply)
        port = registry.create(
            adapter_id,
            base_url=base_url,
            config=config or {},
            credential=credential,
            transport=transport,
        )
        try:
            result = await port.generate(case.request, context)
        except LLMAdapterError as exc:
            assert (exc.code, exc.delivery) == case.expected_error, (
                f"{case.name}: raised {exc.code}/{exc.delivery}"
            )
        else:
            assert case.expected_error is None, f"{case.name}: did not raise"
            assert result == case.expected, f"{case.name}: {result!r}"
        assert len(transport.calls) == case.expected_sends, (
            f"{case.name}: {len(transport.calls)} provider calls"
        )
        for call in transport.calls:
            assert call.timeout_seconds == context.timeout_seconds, case.name
            body = json.dumps(call.payload)
            assert not any(
                secret in call.url or secret in body for secret in secrets
            ), f"{case.name}: credential outside headers"
            if case.check_wire is not None:
                case.check_wire(call)


__all__ = ["RecordedCall", "WireCase", "assert_llm_adapter_contract"]
