"""Explicitly invoked public plugin conformance checks."""

from backend.testing.spi_conformance.auth import assert_auth_method_contract
from backend.testing.spi_conformance.llm import (
    RecordedCall,
    WireCase,
    assert_llm_adapter_contract,
)

__all__ = [
    "RecordedCall",
    "WireCase",
    "assert_auth_method_contract",
    "assert_llm_adapter_contract",
]
