"""Use-case contracts and framework-free services for the media kernel."""

from .content_transport import (
    DEFAULT_MAX_HTTP_RANGE_BYTES,
    ByteRangeNotSatisfiable,
    HttpByteRange,
    if_none_match_matches,
    if_range_allows_range,
    parse_http_byte_range,
)
from .contracts import (
    AdmissionCommand,
    AdmissionDisposition,
    AdmissionResult,
    AdmittedBlob,
    BlobAvailable,
    FencedDisposition,
    GcCommand,
    GcOperation,
    GcResult,
    GenerationFencedCommand,
    GenerationFencedResult,
    InstallCommand,
    InstallDisposition,
    InstallResult,
    SecurityCommand,
    SecurityOperation,
    SecurityResult,
)
from .format_registry import FormatRegistry, FormatRegistryContractError
from .install_validated import InstallValidatedBlob
from .register_available import RegisterAvailableBlob

__all__ = [
    "AdmissionCommand",
    "AdmissionDisposition",
    "AdmissionResult",
    "AdmittedBlob",
    "BlobAvailable",
    "ByteRangeNotSatisfiable",
    "DEFAULT_MAX_HTTP_RANGE_BYTES",
    "FencedDisposition",
    "FormatRegistry",
    "FormatRegistryContractError",
    "GcCommand",
    "GcOperation",
    "GcResult",
    "GenerationFencedCommand",
    "GenerationFencedResult",
    "InstallCommand",
    "InstallDisposition",
    "InstallResult",
    "InstallValidatedBlob",
    "HttpByteRange",
    "RegisterAvailableBlob",
    "SecurityCommand",
    "SecurityOperation",
    "SecurityResult",
    "if_none_match_matches",
    "if_range_allows_range",
    "parse_http_byte_range",
]
