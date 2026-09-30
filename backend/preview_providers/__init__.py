"""Trusted, server-bundled Preview provider composition.

This package contains implementations only.  Runtime composition remains an
explicit application concern so importing the package never enables a provider
or changes a Workspace capability policy.
"""

from backend.preview_providers.pillow_image import PillowImagePreviewProvider
from backend.spi.preview import PreviewProviderPlugin


def build_builtin_preview_providers() -> tuple[PreviewProviderPlugin, ...]:
    """Return the closed set of trusted providers shipped with this build."""

    return (PillowImagePreviewProvider(),)


__all__ = ["PillowImagePreviewProvider", "build_builtin_preview_providers"]
