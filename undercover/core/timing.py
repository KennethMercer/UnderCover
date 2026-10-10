"""Per-stage timing, appended to logs/timings.jsonl so runs can be compared."""
import json
import time
from contextlib import contextmanager
from datetime import datetime

from .config import path

VERBOSE = True  # print each timing line (the CLI turns this off and shows a progress bar)


@contextmanager
def timed(stage: str, **fields):
    """Time a block; extra fields (and anything added to the yielded dict) are logged with it."""
    record = {"stage": stage, **fields}
    t0 = time.perf_counter()
    try:
        yield record
    finally:
        record["seconds"] = round(time.perf_counter() - t0, 3)
        if record.get("audio_seconds"):
            record["realtime"] = round(record["audio_seconds"] / max(record["seconds"], 1e-6), 1)
        record["at"] = datetime.now().isoformat(timespec="seconds")
        with open(path("logs_dir") / "timings.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
        if VERBOSE:
            print(f"[time] {stage}: {record['seconds']:.2f}s"
                  + (f"  ({record['realtime']:.1f}x realtime)" if "realtime" in record else ""))
