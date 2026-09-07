"""The real logic is unimplemented, but the CLI surfaces (argparse wiring, help
text, module imports) should be valid from the scaffold on."""

from __future__ import annotations

import pytest

from sortandpost.db.__main__ import main as db_main
from sortandpost.indexer.pipeline import Stage


def test_db_cli_parses_and_stubs() -> None:
    with pytest.raises(NotImplementedError):
        db_main(["snapshot"])
    with pytest.raises(NotImplementedError):
        db_main(["restore", "/tmp/x.db.zst"])


def test_db_cli_rejects_unknown_command() -> None:
    with pytest.raises(SystemExit):
        db_main(["frobnicate"])


def test_pipeline_stage_bitmask_composition() -> None:
    assert Stage.BROWSABLE == (
        Stage.STAT
        | Stage.DECODE
        | Stage.HASH
        | Stage.THUMBNAILS
        | Stage.METADATA
        | Stage.RECONCILE
    )
    assert Stage.CLIP not in Stage.BROWSABLE
    assert Stage.COMPLETE == Stage.BROWSABLE | Stage.CLIP
