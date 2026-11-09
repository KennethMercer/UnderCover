"""Library warnings that say nothing to UnderCover's users, repeated on every run in logs/server.log and the cadence
worker's log, so they hide what matters there. Each is filtered by its own message, nothing wider; anything else a
library says still shows."""
import warnings

KNOWN = [
    # Whisper on ROCm: no Triton (never used here, ARCHITECTURE "AMD ROCm on Windows"), so it uses its own slower kernels
    (UserWarning, r"Failed to launch Triton kernels"),
    # rotary_embedding_torch (F5-TTS's): torch's own deprecation of its autocast spelling, not ours to change
    (FutureWarning, r"`torch\.cuda\.amp\.autocast\(args\.\.\.\)` is deprecated"),
]


def known_warnings():
    for cat, msg in KNOWN:
        warnings.filterwarnings("ignore", message=msg, category=cat)
