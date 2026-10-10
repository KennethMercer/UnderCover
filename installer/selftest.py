"""Post-install check: is PyTorch the build we meant, can it use the GPU, and is everything else there?

    python selftest.py --backend rocm|cuda128|cuda126|cpu [--report logs/selftest.json]

Prints a plain summary and exits non-zero only for problems that stop UnderCover working.
Doesn't touch config.toml or download models. Works from the dev venv too (for testing).
"""
import argparse
import importlib
import json
import shutil
import sys
import time
from pathlib import Path

# Packages the app imports directly (module name -> what it's for).
MODULES = {
    "torch": "PyTorch", "torchaudio": "audio I/O", "audio_separator": "vocal separation",
    "onnxruntime": "speaker matching", "librosa": "audio analysis", "soundfile": "audio files",
    "scipy": "signal processing", "sklearn": "voice clustering", "yt_dlp": "YouTube downloads",
    "requests": "model downloads",
}
TOOLS = {"ffmpeg": "audio conversion and mixing", "deno": "YouTube (yt-dlp's JavaScript runtime)"}


def check_dtype(torch, dev, dtype) -> bool:
    """True if a small matmul in this dtype runs and gives finite numbers on this device."""
    try:
        a = torch.randn(256, 256, device=dev, dtype=dtype)
        r = (a @ a).float()
        return bool(torch.isfinite(r).all())
    except Exception:
        return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", required=True, choices=["rocm", "cuda128", "cuda126", "cpu"])
    ap.add_argument("--report")
    args = ap.parse_args()

    rep = {"backend": args.backend, "python": sys.version.split()[0], "problems": [], "warnings": []}
    print(f"Python {rep['python']}")

    missing = []
    for mod, why in MODULES.items():
        try:
            importlib.import_module(mod)
        except Exception as e:  # report every missing piece at once
            missing.append(f"{mod} ({why}): {type(e).__name__}: {e}")
    if missing:
        rep["problems"] += [f"can't import {m}" for m in missing]

    # The app's own config must load (setup.ps1 edits it; a stray BOM or bad edit breaks every start).
    app = Path(__file__).resolve().parent.parent
    if (app / "undercover" / "core" / "config.py").exists():
        sys.path.insert(0, str(app))
        try:
            from undercover.core.config import CONFIG
            rep["config_backend"] = CONFIG["gpu"].get("backend")
            want = "cuda" if args.backend.startswith("cuda") else args.backend
            if rep["config_backend"] != want:
                rep["problems"].append(f"config.toml says backend = {rep['config_backend']!r}, expected {want!r}")
        except Exception as e:
            rep["problems"].append(f"config.toml doesn't load: {type(e).__name__}: {e}")

    for exe, why in TOOLS.items():
        if not shutil.which(exe):
            rep["problems"].append(f"{exe} not found ({why})")

    try:
        import torch
    except Exception:
        return finish(rep, args.report)

    rep["torch"] = torch.__version__
    print(f"PyTorch {torch.__version__}")
    want = {"rocm": "+rocm", "cuda128": "+cu128", "cuda126": "+cu126", "cpu": "+cpu"}[args.backend]
    if want not in torch.__version__:
        rep["problems"].append(f"PyTorch is {torch.__version__}, expected a {want[1:]} build. Something replaced it.")

    if args.backend == "cpu":
        dev = torch.device("cpu")
        rep["device"] = "cpu"
    elif not torch.cuda.is_available():
        rep["problems"].append("PyTorch can't see the GPU. Updating the graphics driver usually fixes this.")
        return finish(rep, args.report)
    else:
        # Biggest GPU, so an integrated one never wins.
        idx = max(range(torch.cuda.device_count()), key=lambda i: torch.cuda.get_device_properties(i).total_memory)
        p = torch.cuda.get_device_properties(idx)
        dev = torch.device("cuda", idx)
        rep.update(device=p.name, vram_gb=round(p.total_memory / 2**30, 1))
        if args.backend.startswith("cuda"):
            rep["compute_capability"] = f"{p.major}.{p.minor}"
        print(f"GPU {p.name}, {rep['vram_gb']} GB")
        if rep["vram_gb"] < 6:
            rep["warnings"].append("under 6 GB of graphics memory: covers should work, training probably won't")
        elif rep["vram_gb"] < 10:
            rep["warnings"].append("under 10 GB of graphics memory: training may need smaller settings")

    rep["fp16"] = check_dtype(torch, dev, torch.float16) if dev.type == "cuda" else False
    rep["bf16"] = check_dtype(torch, dev, torch.bfloat16)
    if dev.type == "cuda":
        rep["bf16_native"] = bool(getattr(torch.cuda, "is_bf16_supported", lambda: False)())
        if not rep["bf16_native"]:
            rep["warnings"].append("no fast bf16 on this GPU: training needs the fp16/fp32 fallback")

    # Rough speed figure (also proves real GPU work, not just device enumeration).
    n, dt = 2048, torch.float16 if rep["fp16"] else torch.float32
    a = torch.randn(n, n, device=dev, dtype=dt)
    for _ in range(3):
        a @ a
    sync = torch.cuda.synchronize if dev.type == "cuda" else (lambda: None)
    sync()
    t = time.perf_counter()
    for _ in range(10):
        a @ a
    sync()
    rep["tflops"] = round(10 * 2 * n**3 / (time.perf_counter() - t) / 1e12, 2)
    print(f"Matmul {str(dt).split('.')[-1]}: {rep['tflops']} TFLOPS")

    return finish(rep, args.report)


def finish(rep: dict, report: str | None) -> int:
    for w in rep["warnings"]:
        print(f"Note: {w}")
    for p in rep["problems"]:
        print(f"PROBLEM: {p}")
    print("Self-test passed." if not rep["problems"] else "Self-test FAILED.")
    if report:
        Path(report).parent.mkdir(parents=True, exist_ok=True)
        Path(report).write_text(json.dumps(rep, indent=2), encoding="utf-8")
    return 1 if rep["problems"] else 0


if __name__ == "__main__":
    sys.exit(main())
