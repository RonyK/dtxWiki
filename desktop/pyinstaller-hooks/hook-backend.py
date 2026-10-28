"""Retain the source bytes used by bundled converter and Preview digests."""

# Import from the frozen archive, while also shipping the matching source.
# The registries hash their first-party execution closures from these files.
module_collection_mode = "pyz+py"
