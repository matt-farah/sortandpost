"""CLI surfaces (argparse wiring, help text, module imports) stay valid even
where the underlying feature is still a stub."""

from __future__ import annotations

import pytest

from sortandpost.db.__main__ import main as db_main
from sortandpost.indexer.pipeline import Stage


def test_db_cli_rejects_unknown_command() -> None:
    with pytest.raises(SystemExit):
        db_main(["frobnicate"])


def test_db_cli_requires_a_subcommand() -> None:
    with pytest.raises(SystemExit):
        db_main([])


def test_pipeline_stage_bitmask_composition() -> None:
    assert Stage.BROWSABLE == (
        Stage.STAT | Stage.DECODE | Stage.HASH | Stage.THUMBNAILS | Stage.METADATA | Stage.RECONCILE
    )
    assert Stage.CLIP not in Stage.BROWSABLE
    assert Stage.COMPLETE == Stage.BROWSABLE | Stage.CLIP
