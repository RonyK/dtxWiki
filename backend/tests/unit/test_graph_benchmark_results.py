"""Benchmark qualification must retain failed warmup attempts."""

from scripts.bench.graph._common import (
    OperationResult,
    benchmark_has_failures,
    run_benchmark,
)


def test_failed_warmup_prevents_a_successful_measured_run_from_qualifying():
    # One lifecycle scenario, without a parameter/input matrix: a cold first
    # request fails, then the warmed operation succeeds. Product API tests do
    # not exercise the benchmark's failure denominator or qualification gate.
    attempts = 0

    def operation():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("cold request failed")
        return OperationResult(elapsed_ms=1, response_bytes=20, gzip_bytes=10)

    result = run_benchmark(
        operation, runs=100, warmup=10, concurrency=1, timeout_seconds=1
    )

    assert attempts == 110
    assert result["summary"]["attempted"] == 100
    assert result["summary"]["completed"] == 100
    assert result["summary"]["failure_rate"] == 0
    assert result["warmup"]["attempted"] == 10
    assert result["warmup"]["completed"] == 9
    assert result["warmup"]["errors"] == 1
    assert result["warmup"]["failure_rate"] == 0.1
    assert result["warmup"]["failures"][0]["error"] == (
        "RuntimeError: cold request failed"
    )
    assert result["gate"]["eligible"] is True
    assert result["gate"]["passed"] is False
    assert benchmark_has_failures(result)
