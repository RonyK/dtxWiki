"""Every Inbox setting, in one place.

Kinds and the service hold no numbers of their own: the container builds one
``InboxConfig`` and hands each part to what uses it. A kind with settings
takes its section as a required argument, so it cannot be built without this
module. To change a value, change it here; to let operators override one
later, overlay ``Settings`` onto these defaults where the container builds the
config. The page size bounds are part of the REST contract and are read at
import time, so they are not overridable.
"""

from dataclasses import dataclass, field, fields
from datetime import timedelta


def _require_positive(section: object) -> None:
    for item in fields(section):
        value = getattr(section, item.name)
        if isinstance(value, timedelta):
            if value <= timedelta(0):
                raise ValueError(f"{item.name} must be a positive duration")
        elif type(value) is not int or value <= 0:
            raise ValueError(f"{item.name} must be a positive integer")


@dataclass(frozen=True)
class InboxListingConfig:
    """Bounds on one list request."""

    page_size_default: int = 30
    page_size_max: int = 100
    # Status items returned per kind; more show as an overflow count.
    status_items_per_kind: int = 20
    # Unread lists ask a status source for this many extra candidates at most,
    # one per item the viewer has read, so read items do not crowd out unread.
    read_mark_scan_cap: int = 100
    # Batches the message stream reads ahead past items the viewer cannot see.
    stream_scan_batches: int = 5

    def __post_init__(self) -> None:
        _require_positive(self)
        if self.page_size_default > self.page_size_max:
            raise ValueError("page_size_default must not exceed page_size_max")


@dataclass(frozen=True)
class WorkspaceJoinConfig:
    # How long a requester sees the approval or rejection of their request.
    result_visible_for: timedelta = timedelta(days=14)

    def __post_init__(self) -> None:
        _require_positive(self)


@dataclass(frozen=True)
class AccessKeyExpiryConfig:
    # An Access Key shows this long before it expires, and this long after.
    warn_before: timedelta = timedelta(days=7)
    keep_after_expiry: timedelta = timedelta(days=7)

    def __post_init__(self) -> None:
        _require_positive(self)


@dataclass(frozen=True)
class InboxConfig:
    listing: InboxListingConfig = field(default_factory=InboxListingConfig)
    workspace_join: WorkspaceJoinConfig = field(default_factory=WorkspaceJoinConfig)
    access_key_expiry: AccessKeyExpiryConfig = field(
        default_factory=AccessKeyExpiryConfig
    )


DEFAULT_INBOX_CONFIG = InboxConfig()
