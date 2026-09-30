"""Shared media kernel.

The package root intentionally performs no composition or convenience imports.
Bounded contexts depend on the narrow ``domain`` and ``ports`` modules they use,
while concrete adapters are wired only by ``backend.app.container``.
"""
