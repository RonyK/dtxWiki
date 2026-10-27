"""Explicitly invoked public plugin conformance checks."""

from backend.testing.spi_conformance.auth import assert_auth_method_contract

__all__ = ["assert_auth_method_contract"]
