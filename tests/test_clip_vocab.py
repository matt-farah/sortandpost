"""Vocabulary parsing + the cached text-embedding matrix. No torch."""

from __future__ import annotations

from pathlib import Path

import numpy as np

from sortandpost.clip.vocab import ensure_text_matrix, load_vocab

_YAML = """
scene:
  - beach
  - forest
subject:
  - dog
  - beach          # dup across facets -> keeps first facet, dropped here
lighting:
  - " golden hour "
"""


def test_load_vocab_order_dedup_and_facets(tmp_path: Path) -> None:
    f = tmp_path / "tags.yaml"
    f.write_text(_YAML, encoding="utf-8")
    vocab = load_vocab(f)

    assert vocab.names == ["beach", "forest", "dog", "golden hour"]
    assert vocab.facet_of("beach") == "scene"
    assert vocab.facet_of("dog") == "subject"
    assert vocab.facet_of("golden hour") == "lighting"
    assert vocab.facet_of("nope") is None


def test_vocab_hash_is_content_stable(tmp_path: Path) -> None:
    a = tmp_path / "a.yaml"
    b = tmp_path / "b.yaml"
    a.write_text("scene:\n  - beach\n  - forest\n", encoding="utf-8")
    b.write_text("scene:\n  - beach\n  - forest\n", encoding="utf-8")
    assert load_vocab(a).vocab_hash == load_vocab(b).vocab_hash

    b.write_text("scene:\n  - beach\n  - desert\n", encoding="utf-8")
    assert load_vocab(a).vocab_hash != load_vocab(b).vocab_hash


def test_missing_file_is_empty_vocab(tmp_path: Path) -> None:
    vocab = load_vocab(tmp_path / "nope.yaml")
    assert vocab.names == []
    assert ensure_text_matrix(
        _CountingEncoder(), vocab, prompt_templates=["{}"], cache_path=tmp_path / "t.npy"
    ).shape == (0, 0)


class _CountingEncoder:
    """Maps a prompt to a basis vector by keyword; counts encode_text calls."""

    def __init__(self) -> None:
        self.calls = 0

    def encode_text(self, texts: list[str]) -> np.ndarray:
        self.calls += 1
        out = np.zeros((len(texts), 3), dtype=np.float32)
        for i, t in enumerate(texts):
            if "beach" in t:
                out[i] = [1, 0, 0]
            elif "forest" in t:
                out[i] = [0, 1, 0]
            else:
                out[i] = [0, 0, 1]
        return out


def test_ensure_text_matrix_builds_normalizes_and_caches(tmp_path: Path) -> None:
    f = tmp_path / "tags.yaml"
    f.write_text("scene:\n  - beach\n  - forest\n", encoding="utf-8")
    vocab = load_vocab(f)
    cache = tmp_path / "text.npy"
    enc = _CountingEncoder()

    matrix = ensure_text_matrix(
        enc, vocab, prompt_templates=["a photo of {}", "{}"], cache_path=cache
    )
    assert matrix.shape == (2, 3)
    np.testing.assert_allclose(np.linalg.norm(matrix, axis=1), [1.0, 1.0], atol=1e-6)
    np.testing.assert_allclose(matrix[0], [1, 0, 0], atol=1e-6)
    assert cache.is_file()
    assert enc.calls == 1

    # second call: served from cache, encoder untouched
    again = ensure_text_matrix(enc, vocab, prompt_templates=["{}"], cache_path=cache)
    np.testing.assert_allclose(again, matrix)
    assert enc.calls == 1


def test_stale_cache_is_rebuilt(tmp_path: Path) -> None:
    f = tmp_path / "tags.yaml"
    f.write_text("scene:\n  - beach\n  - forest\n  - desert\n", encoding="utf-8")
    vocab = load_vocab(f)
    cache = tmp_path / "text.npy"
    with open(cache, "wb") as fh:  # wrong row count on disk
        np.save(fh, np.zeros((2, 3), dtype=np.float32))

    enc = _CountingEncoder()
    matrix = ensure_text_matrix(enc, vocab, prompt_templates=["{}"], cache_path=cache)
    assert matrix.shape == (3, 3)
    assert enc.calls == 1


def test_load_vocab_collapses_line_breaks_and_skips_control_chars(tmp_path: Path) -> None:
    f = tmp_path / "tags.yaml"
    f.write_text(
        'scene:\n  - "sunset\\n-if\\nsystem(1)"\n  - "bad\\x00name"\n  - beach\n',
        encoding="utf-8",
    )
    assert load_vocab(f).names == ["sunset -if system(1)", "beach"]


def test_load_vocab_skips_overlong_terms(tmp_path: Path) -> None:
    f = tmp_path / "tags.yaml"
    f.write_text(f"scene:\n  - {'x' * 101}\n  - beach\n", encoding="utf-8")
    assert load_vocab(f).names == ["beach"]
