"""Thin wrapper around ``open_clip`` (build-order step 5a).

``torch`` + ``open-clip-torch`` are heavy (~hundreds of MB) and are *not*
installed in the lightweight dev/test environment, so the import is guarded the
same way ``rawpy`` / ``pillow_heif`` are in :mod:`sortandpost.indexer.decode`.
Call :func:`clip_available` before constructing a :class:`ClipModel` where the
caller must degrade gracefully (the web app), or catch
:class:`ClipModelUnavailable` from :meth:`ClipModel.load`.

``clip.device`` defaults to ``"auto"`` -> CUDA / Apple MPS / CPU for whatever
the host has (a Docker container gets CPU -- no MPS/CUDA passthrough on macOS).
Both encoders return an L2-normalized ``float32`` matrix, shape ``(n, dim)``.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

from sortandpost.config import ClipConfig
from sortandpost.cpu import CpuProfile, clip_profile, set_thread_priority

log = logging.getLogger(__name__)

try:  # torch/open-clip are optional in the dev env; required for a real CLIP run
    import open_clip
    import torch

    _IMPORT_ERROR: ModuleNotFoundError | None = None
except ModuleNotFoundError as exc:  # pragma: no cover - exercised only without torch
    open_clip = None
    torch = None
    _IMPORT_ERROR = exc


def apply_cpu_profile(profile: CpuProfile) -> None:
    """Point torch's thread count and the calling thread's priority at *profile*.
    Called at the start of each CLIP pass so a slow/fast change made in Settings
    takes effect on the next pass, never mid-pass."""
    if torch is not None:
        torch.set_num_threads(profile.threads)
    set_thread_priority(profile.lower_priority)


# open_clip embedding dims for the architectures we ship a default for. Lets a
# compatibility check answer without downloading/loading weights.
KNOWN_DIMS: dict[str, int] = {
    "ViT-B-32": 512,
    "ViT-B-16": 512,
    "ViT-L-14": 768,
    "ViT-L-14-336": 768,
    "ViT-H-14": 1024,
}


class ClipModelUnavailable(RuntimeError):
    """``torch`` / ``open-clip-torch`` are not installed in this environment."""


def clip_available() -> bool:
    return _IMPORT_ERROR is None


def _try_load(config: ClipConfig, cache_dir: Path, *, offline: bool) -> tuple[bool, str]:
    """Attempt to construct the configured model/tokenizer from ``cache_dir``.

    ``offline=True`` forces ``HF_HUB_OFFLINE=1`` for the call, so it succeeds
    only if every required file is already cached -- a "is it downloaded?"
    check with no network access. ``offline=False`` clears any process-wide
    ``HF_HUB_OFFLINE`` for the call (even if the app itself was launched with
    it set, as the documented offline dev run does), so missing files are
    fetched -- an explicit "make sure it's downloaded" action. Always used on
    the CPU device and never touches :class:`ClipModel` / ``State.clip_model``
    -- this is a cheap existence probe, not the model the indexer will use.
    """
    if _IMPORT_ERROR is not None:
        return False, "torch / open-clip-torch are not installed in this environment"
    cache_dir.mkdir(parents=True, exist_ok=True)
    prev_offline = os.environ.get("HF_HUB_OFFLINE")
    if offline:
        os.environ["HF_HUB_OFFLINE"] = "1"
    else:
        os.environ.pop("HF_HUB_OFFLINE", None)
    try:
        model, _, _preprocess = open_clip.create_model_and_transforms(
            config.model,
            pretrained=config.pretrained,
            cache_dir=str(cache_dir),
            device="cpu",
        )
        del model
        open_clip.get_tokenizer(config.model)
        return True, ""
    except Exception as exc:
        return False, str(exc)
    finally:
        if prev_offline is None:
            os.environ.pop("HF_HUB_OFFLINE", None)
        else:
            os.environ["HF_HUB_OFFLINE"] = prev_offline


def check_cached(config: ClipConfig, cache_dir: Path) -> tuple[bool, str]:
    """``(True, "")`` if ``config.model``/``config.pretrained`` are already
    cached in ``cache_dir`` well enough to load with no network access;
    otherwise ``(False, <reason>)``."""
    return _try_load(config, cache_dir, offline=True)


def ensure_downloaded(config: ClipConfig, cache_dir: Path) -> tuple[bool, str]:
    """Force a (re)download of any weight/tokenizer files missing from
    ``cache_dir``, ignoring any process-wide ``HF_HUB_OFFLINE``. A no-op
    network-wise if everything is already cached. ``(True, "")`` on success,
    ``(False, <reason>)`` on failure (e.g. offline with nothing cached yet)."""
    return _try_load(config, cache_dir, offline=False)


def known_dim(model: str) -> int | None:
    return KNOWN_DIMS.get(model)


def _resolve_device(requested: str) -> str:
    """Map ``"auto"`` to the best backend this host has; pass anything else
    through. ``"auto"`` picks CUDA, then Apple MPS, then CPU -- so a native run
    on Apple Silicon uses the GPU while a Docker container (no passthrough)
    lands on CPU."""
    want = (requested or "auto").strip().lower()
    if want != "auto":
        return want
    if torch is None:  # pragma: no cover - guarded by load() before this runs
        return "cpu"
    if torch.cuda.is_available():
        return "cuda"
    if getattr(torch.backends, "mps", None) is not None and torch.backends.mps.is_available():
        return "mps"
    return "cpu"


class ClipModel:
    """Lazily-loaded CLIP encoder pair. Safe to construct without ``torch``;
    :meth:`load` (called implicitly by the encoders) raises if it is missing."""

    def __init__(self, config: ClipConfig, cache_dir: Path) -> None:
        self.config = config
        self.cache_dir = Path(cache_dir)
        self._model: Any = None
        self._preprocess: Any = None
        self._tokenizer: Any = None
        self._dim: int | None = KNOWN_DIMS.get(config.model)
        # Resolved on load(): "auto" -> mps / cuda / cpu for whatever this host
        # has. Stays "cpu" under Docker (no MPS/CUDA passthrough on macOS).
        self.device: str = config.device

    @property
    def name(self) -> str:
        return self.config.model

    @property
    def pretrained(self) -> str:
        return self.config.pretrained

    @property
    def dim(self) -> int:
        if self._dim is None:
            # No known dim for this arch -- infer it from one text encode.
            self.encode_text(["_"])
        assert self._dim is not None
        return self._dim

    def profile(self) -> CpuProfile:
        """The CPU budget for the *current* ``clip.speed`` setting."""
        return clip_profile(self.config.speed, self.config.batch_size)

    def load(self) -> None:
        if self._model is not None:
            return
        if _IMPORT_ERROR is not None:
            raise ClipModelUnavailable(
                "open-clip-torch / torch are not installed; install them for native "
                "CLIP indexing or run the app in Docker"
            ) from _IMPORT_ERROR

        self.device = _resolve_device(self.config.device)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        torch.set_num_threads(self.profile().threads)
        model, _, preprocess = open_clip.create_model_and_transforms(
            self.config.model,
            pretrained=self.config.pretrained,
            cache_dir=str(self.cache_dir),
            device=self.device,
        )
        model.eval()
        self._model = model
        self._preprocess = preprocess
        self._tokenizer = open_clip.get_tokenizer(self.config.model)
        log.info(
            "loaded CLIP %s / %s on %s",
            self.config.model,
            self.config.pretrained,
            self.device,
        )

    # -- encoders ------------------------------------------------------- #
    def encode_images(self, images: list[Image.Image]) -> np.ndarray:
        self.load()
        if not images:
            return np.zeros((0, self._dim or 0), dtype=np.float32)
        assert self._model is not None and self._preprocess is not None
        batch_size = max(1, self.config.batch_size)
        chunks: list[np.ndarray] = []
        with torch.inference_mode():
            for start in range(0, len(images), batch_size):
                batch = images[start : start + batch_size]
                tensor = torch.stack([self._preprocess(im.convert("RGB")) for im in batch])
                tensor = tensor.to(self.device)
                feats = self._model.encode_image(tensor).float()
                feats = feats / feats.norm(dim=-1, keepdim=True).clamp_min(1e-12)
                chunks.append(feats.cpu().numpy().astype(np.float32))
        out = np.asarray(np.concatenate(chunks, axis=0), dtype=np.float32)
        self._dim = int(out.shape[1])
        return out

    def encode_text(self, texts: list[str]) -> np.ndarray:
        self.load()
        if not texts:
            return np.zeros((0, self._dim or 0), dtype=np.float32)
        assert self._model is not None and self._tokenizer is not None
        tokens = self._tokenizer(texts).to(self.device)
        with torch.inference_mode():
            feats = self._model.encode_text(tokens).float()
            feats = feats / feats.norm(dim=-1, keepdim=True).clamp_min(1e-12)
        out = np.asarray(feats.cpu().numpy(), dtype=np.float32)
        self._dim = int(out.shape[1])
        return out
