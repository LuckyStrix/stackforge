"""Filaments tab: the filament database editor (from filamentdb_gui), split by class."""

APP = "filament library"

# What the preview composites over. Two contrasting backings, because that is
# exactly the pair that pins a td down -- a filament whose td is wrong usually
# still looks plausible over one of them.
PREVIEW_BASES = [("over white", "#F4F5F0"), ("over black", "#1A1A1C")]
