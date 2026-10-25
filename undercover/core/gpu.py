"""The GPU models every module shares: the stem separator and the converter (HuBERT, RMVPE and the cached voices).

Loaded on first use and kept loaded, so covers, voice checks and training all reuse one copy.
"""
import gc
import os
import sys

LIMIT_ENV = "UNDERCOVER_GPU_LIMIT_GB"   # Low profile's cap for this process and the ones it starts (the cadence worker)


def limit_gb() -> float | None:
    """The graphics-memory cap this process runs under (whole or fractional GB, binary), or None for none."""
    try:
        v = float(os.environ.get(LIMIT_ENV) or 0)
    except ValueError:
        return None
    return v if v > 0 else None


def apply_cap(limit: float | None = None) -> float | None:
    """Low profile: PyTorch keeps this process under `limit` GB of graphics memory (its allocator refuses more, as a
    full card would), so a game keeps the rest; the environment's limit_gb() when none is given (the cadence worker
    gets its share that way). -> the cap applied, or None (no cap, no GPU, or a torch without it)."""
    lim = limit_gb() if limit is None else limit
    if lim is None:
        return None
    try:
        import torch
        if not torch.cuda.is_available():
            return None
        total = torch.cuda.get_device_properties(0).total_memory
        torch.cuda.set_per_process_memory_fraction(min(1.0, lim * 2**30 / total), 0)
        return lim
    except Exception:   # noqa: BLE001 - a torch without the call: no cap, said by the caller
        return None


def peak_gb() -> float | None:
    """The most graphics memory this process has held since the last reset_peak() (PyTorch's reserved, binary GB)."""
    torch = sys.modules.get("torch")
    try:
        return round(torch.cuda.max_memory_reserved() / 2**30, 2) if torch is not None and torch.cuda.is_available() else None
    except Exception:   # noqa: BLE001 - only a number for the record
        return None


def alloc_gb() -> tuple[float, float] | None:
    """(held now, the most held since reset_peak()): memory in tensors, not the allocator's cache (binary GB)."""
    torch = sys.modules.get("torch")
    try:
        if torch is None or not torch.cuda.is_available():
            return None
        return round(torch.cuda.memory_allocated() / 2**30, 2), round(torch.cuda.max_memory_allocated() / 2**30, 2)
    except Exception:   # noqa: BLE001
        return None


def reset_peak():
    torch = sys.modules.get("torch")
    try:
        if torch is not None and torch.cuda.is_available():
            torch.cuda.reset_peak_memory_stats()
    except Exception:   # noqa: BLE001
        pass


class Models:
    def __init__(self):
        self._sep = None
        self._conv = None

    @property
    def separator(self):
        if self._sep is None:
            from .separate import StemSeparator
            self._sep = StemSeparator()
        return self._sep

    @property
    def converter(self):
        if self._conv is None:
            from .engine.convert import Converter
            self._conv = Converter()
        return self._conv


MODELS = Models()


def release(step: str):
    """Drop what the next step doesn't need, so it doesn't count against Low profile's cap: before a conversion or a speech
    the separator; before a training the separator, the converter and listen-back's ear (all re-loaded on first use)."""
    if step in ("convert", "speak", "train") and MODELS._sep is not None:
        MODELS._sep = None
    if step == "train":
        MODELS._conv = None
        lb = sys.modules.get("undercover.speakeasy.listenback")
        if lb is not None:
            try:
                lb._ASR["model"] = None
            except Exception:   # noqa: BLE001
                pass
    gc.collect()
    torch = sys.modules.get("torch")
    if torch is not None:
        try:
            torch.cuda.empty_cache()
        except Exception:   # noqa: BLE001
            pass


def forget_voice(name: str):
    """Drop a voice from the converter's cache (it was replaced or removed); it's re-loaded on demand."""
    if MODELS._conv:
        MODELS._conv.voices.pop(name, None)


def free():
    """Free what UnderCover holds on the GPU (cached voices, allocator cache), e.g. after running out of memory."""
    try:
        if MODELS._conv:
            MODELS._conv.voices.clear()   # loaded voices (+ their indexes) are re-loaded on demand
    except Exception:
        pass
    cad = sys.modules.get(__package__ + ".cadence.client")
    if cad is not None:   # the cadence worker (a separate process) holds 4-8 GB while it's loaded
        try:
            cad.WORKER.stop("freeing GPU memory")
        except Exception:
            pass
    gc.collect()
    torch = sys.modules.get("torch")
    if torch is not None:
        try:
            torch.cuda.empty_cache()
        except Exception:
            pass
