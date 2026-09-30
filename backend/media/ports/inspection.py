"""Detection, exact validation, and scanner capabilities."""

from dataclasses import dataclass
from typing import Protocol

from backend.media.domain import FormatFacts, MediaFailure, ScannerVerdict
from backend.media.ports.storage import ReadHandle


@dataclass(frozen=True, slots=True)
class ScanResult:
    verdict: ScannerVerdict
    provider: str | None
    version: str | None
    failure: MediaFailure | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.verdict, ScannerVerdict):
            raise ValueError("verdict must be a ScannerVerdict")
        if self.verdict is ScannerVerdict.NOT_CONFIGURED:
            if self.provider is not None or self.version is not None:
                raise ValueError("not_configured scan cannot name a scanner")
        elif (
            not isinstance(self.provider, str)
            or not self.provider.strip()
            or len(self.provider) > 160
            or not isinstance(self.version, str)
            or not self.version.strip()
            or len(self.version) > 160
        ):
            raise ValueError("configured scan requires bounded provider and version")
        if self.failure is not None and not isinstance(self.failure, MediaFailure):
            raise ValueError("failure must be a MediaFailure")
        if self.verdict is ScannerVerdict.CLEAN and self.failure is not None:
            raise ValueError("clean scan cannot carry a failure")


class MediaScanner(Protocol):
    def scan(self, content: ReadHandle, *, byte_budget: int) -> ScanResult: ...


class FormatValidator(Protocol):
    def validate(self, content: ReadHandle, *, byte_budget: int) -> FormatFacts: ...
