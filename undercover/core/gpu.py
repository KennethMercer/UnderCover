"""The GPU models every module shares: the stem separator and the converter (HuBERT, RMVPE and the cached voices).

Loaded on first use and kept loaded, so covers, voice checks and training all reuse one copy.
"""
import gc
import sys


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
    gc.collect()
    torch = sys.modules.get("torch")
    if torch is not None:
        try:
            torch.cuda.empty_cache()
        except Exception:
            pass
