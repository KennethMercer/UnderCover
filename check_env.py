"""Verify the ROCm PyTorch environment.

    python check_env.py          full report + FP16/FP32 matmul benchmark
    python check_env.py --quick  fast guard to run after every pip install

Exits non-zero if torch is not the ROCm build or the configured GPU isn't usable.
"""
import os
import sys
import time
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG = tomllib.loads((ROOT / "config.toml").read_text(encoding="utf-8"))
GPU_CFG = CONFIG["gpu"]

if GPU_CFG.get("hip_visible_devices"):
    os.environ["HIP_VISIBLE_DEVICES"] = GPU_CFG["hip_visible_devices"]

import torch  # noqa: E402  (must come after HIP_VISIBLE_DEVICES is set)


def fail(msg: str) -> None:
    print(f"\nFAIL: {msg}")
    sys.exit(1)


def pick_device() -> int:
    """Return the index of the GPU whose name matches config [gpu].device_name."""
    want = GPU_CFG["device_name"].lower()
    for i in range(torch.cuda.device_count()):
        if want in torch.cuda.get_device_name(i).lower():
            return i
    fail(f"no GPU matching '{GPU_CFG['device_name']}' found")


def bench_matmul(dtype: torch.dtype, n: int = 8192, iters: int = 20) -> float:
    a = torch.randn(n, n, device="cuda", dtype=dtype)
    b = torch.randn(n, n, device="cuda", dtype=dtype)
    for _ in range(3):  # warm-up (kernel selection)
        a @ b
    torch.cuda.synchronize()
    t0 = time.perf_counter()
    for _ in range(iters):
        a @ b
    torch.cuda.synchronize()
    elapsed = time.perf_counter() - t0
    return 2 * n**3 * iters / elapsed / 1e12


def main() -> None:
    quick = "--quick" in sys.argv

    print(f"Python        {sys.version.split()[0]}")
    print(f"torch         {torch.__version__}")
    print(f"HIP           {torch.version.hip}")
    if not torch.version.hip or "rocm" not in torch.__version__:
        fail("torch is NOT the ROCm build (something overwrote it). Reinstall AMD's wheels.")
    if not torch.cuda.is_available():
        fail("torch.cuda.is_available() is False. Check the AMD driver.")

    print(f"Devices       {torch.cuda.device_count()}")
    for i in range(torch.cuda.device_count()):
        print(f"  [{i}] {torch.cuda.get_device_name(i)}")

    idx = pick_device()
    torch.cuda.set_device(idx)
    props = torch.cuda.get_device_properties(idx)
    free, total = torch.cuda.mem_get_info(idx)
    print(f"Active        [{idx}] {props.name} ({getattr(props, 'gcnArchName', '?')})")
    print(f"VRAM          {total / 2**30:.1f} GB total, {free / 2**30:.1f} GB free")

    # Sanity: a small FP16 op must produce correct results.
    x = torch.ones(256, 256, device="cuda", dtype=torch.float16)
    if (x @ x)[0, 0].item() != 256:
        fail("FP16 matmul returned a wrong result")

    if quick:
        print("\nOK (quick)")
        return

    print("\nMatmul benchmark (8192x8192)")
    for name, dt in (("FP32", torch.float32), ("FP16", torch.float16), ("BF16", torch.bfloat16)):
        try:
            print(f"  {name}  {bench_matmul(dt):6.1f} TFLOPS")
        except Exception as e:  # report but don't abort on an unsupported dtype
            print(f"  {name}  error: {e}")
    print(f"Peak alloc    {torch.cuda.max_memory_allocated() / 2**30:.2f} GB")
    print("\nOK: Phase 0 environment verified")


if __name__ == "__main__":
    main()
