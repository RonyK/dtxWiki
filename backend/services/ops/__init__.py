"""Shared operator-operations core.

Thin, reusable building blocks for the operator lifecycle (install / update /
rollback / data-migration): install-method detection, package-index resolution,
systemd service control, SQLite write-lock probing, and HTTP health polling.

``setup production``, ``dtxwiki update`` and ``dtxwiki rollback`` all build on
these so the service-control / health-check / lock-probe logic lives in one
place instead of being re-implemented per command.
"""
