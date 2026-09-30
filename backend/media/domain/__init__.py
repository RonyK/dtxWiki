"""Framework-free contracts for the shared media domain."""

from .available import AvailableBlobCandidate, AvailableBlobRecord
from .blob import (
    BlobEpoch,
    BlobIncarnation,
    BlobReference,
    BlobState,
    ScannerVerdict,
    SecurityBlockedBlob,
    SecurityState,
    Sha256Digest,
    StoredBlob,
    StoredBlobInspection,
    WorkspaceBlobIdentity,
)
from .failure import (
    FailureDisposition,
    MediaFailure,
    MediaFailureCode,
    MediaFailurePhase,
    MediaStagingBytesExceededError,
    MediaStagingError,
    MediaStagingLimitExceededError,
)
from .format import FormatCategory, FormatDescriptor, FormatFacts
from .quota import PhysicalQuotaFacts, PhysicalQuotaKind

__all__ = [
    "AvailableBlobCandidate",
    "AvailableBlobRecord",
    "BlobEpoch",
    "BlobIncarnation",
    "BlobReference",
    "BlobState",
    "FormatCategory",
    "FormatDescriptor",
    "FormatFacts",
    "FailureDisposition",
    "MediaFailure",
    "MediaFailureCode",
    "MediaFailurePhase",
    "MediaStagingBytesExceededError",
    "MediaStagingError",
    "MediaStagingLimitExceededError",
    "PhysicalQuotaFacts",
    "PhysicalQuotaKind",
    "ScannerVerdict",
    "SecurityBlockedBlob",
    "SecurityState",
    "Sha256Digest",
    "StoredBlob",
    "StoredBlobInspection",
    "WorkspaceBlobIdentity",
]
