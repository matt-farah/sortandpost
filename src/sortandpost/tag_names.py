"""What a tag name may contain.

Tag names end up as exiftool arguments (``-XMP:Subject=<name>``), and exiftool's
``-stay_open`` protocol reads one argument per line -- a line break inside a
name would inject extra exiftool options. :class:`~sortandpost.metadata.ExiftoolClient`
refuses such arguments outright; this module keeps them out of the vocabulary
and the tag table in the first place, from every source: the tag editor,
``sync/tags.yaml``, keywords imported from photo files, and rows merged in from
another machine's database.

A new tag also has a length cap (:data:`MAX_TAG_NAME_LEN`): a file can carry a
3000-character "keyword", and nothing that long is a tag anyone typed.
"""

from __future__ import annotations

import unicodedata

MAX_TAG_NAME_LEN = 100


def normalize_tag_name(raw: str) -> str:
    """Collapse every whitespace run -- line breaks and tabs included -- to one
    space and trim the ends. May return ``""``."""
    return " ".join(raw.split())


def is_safe_tag_name(name: str) -> bool:
    """Non-empty, no control characters, no line/paragraph separators."""
    return bool(name) and not any(unicodedata.category(c) in ("Cc", "Zl", "Zp") for c in name)


def is_valid_new_tag_name(name: str) -> bool:
    """Safe (:func:`is_safe_tag_name`) and at most :data:`MAX_TAG_NAME_LEN`
    characters -- the check for a tag about to be created. Writing a file's
    keywords only needs :func:`is_safe_tag_name`, so an older, longer tag still
    goes into the file."""
    return is_safe_tag_name(name) and len(name) <= MAX_TAG_NAME_LEN
