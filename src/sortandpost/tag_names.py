"""What a tag name may contain.

Tag names end up as exiftool arguments (``-XMP:Subject=<name>``), and exiftool's
``-stay_open`` protocol reads one argument per line -- a line break inside a
name would inject extra exiftool options. :class:`~sortandpost.metadata.ExiftoolClient`
refuses such arguments outright; this module keeps them out of the vocabulary
and the tag table in the first place, from every source: the tag editor,
``sync/tags.yaml``, and rows merged in from another machine's database.
"""

from __future__ import annotations

import unicodedata


def normalize_tag_name(raw: str) -> str:
    """Collapse every whitespace run -- line breaks and tabs included -- to one
    space and trim the ends. May return ``""``."""
    return " ".join(raw.split())


def is_safe_tag_name(name: str) -> bool:
    """Non-empty, no control characters, no line/paragraph separators."""
    return bool(name) and not any(unicodedata.category(c) in ("Cc", "Zl", "Zp") for c in name)
