# Core edits the installer needs (deferred)

**Status: NOT applied.** The main UnderCover session said on 2026-10-09 that its batch is done, but it may resume.
Ask the user before editing. It was editing `server.py`, `train/trainer.py`, `convert.py` and
`autopilot.py`. Apply these only after it says it's finished, and after `git init` plus a first commit so each
change can be reviewed and undone. On the dev machine (ROCm, 7900 XTX) every change below must keep behaviour
exactly the same.

Line numbers are from 2026-10-09 and will drift.

## Required (otherwise non-ROCm installs can't make covers)

1. **`undercover/server.py` ~124-135, GPU check at startup.** It sets `gpu_problem` when `"+rocm"` isn't in
   `torch.__version__`, and the GPU worker then refuses jobs. Change it to accept `+rocm`, `+cu1xx` and `+cpu`.
   Report the backend in `/api/status` (for example `"backend": "cuda"`). With `+cpu`, run jobs and show a
   "running on the processor, this will be slow" notice instead of an error. (The main session confirmed
   2026-10-09: `run_job` refuses every job while `gpu_problem` is set, and also when under 3 GB is free. Check
   which kind of "free" that means, because a 6-8 GB card or a CPU install must still pass.)
2. **`undercover/server.py` ~125, `get_device_name(0)`.** Use the same device the converter uses. On a PC with
   two GPUs, index 0 can be the wrong one.

## Needed for older NVIDIA and low-VRAM cards

3. **`undercover/train/trainer.py` ~382, bf16 autocast.** GTX 10 / RTX 20 cards have no fast bf16. Use bf16 when
   `torch.cuda.is_bf16_supported()` (and always on ROCm, as now). Otherwise use fp16 with
   `torch.amp.GradScaler`, the way upstream RVC does it, or fp32 if that's unstable. `selftest.py` already
   records `bf16_native`.
4. **`undercover/train/trainer.py` ~333, fused AdamW.** This works on CUDA too. Keep `fused=dev.type == "cuda"`;
   just test it on NVIDIA.
5. **Training on CPU.** Disable the Train tab and one-shot training on `+cpu` builds (show why), since it would
   take days.
6. **VRAM tiers.** Separation chunk/batch size and training batch size should scale down under ~10 GB. Training
   peaked around 5 GB here. Read the VRAM at startup and pick smaller defaults; don't write them into
   config.toml, so a card change still works.

## Nice to have

7. **`config.toml [gpu]`.** `device_name = "7900 XTX"` is only read by `check_env.py`. setup.ps1 rewrites it for
   the detected card. `miopen_find_mode` / `hip_visible_devices` are ROCm-only and harmless elsewhere.
   `check_env.py` itself fails on non-ROCm torch by design; the installer uses `installer/selftest.py`
   instead, so leave `check_env.py` as the dev-machine guard.
8. **Restart button (`relaunch.py`).** It assumes the dev layout. Check that it still finds `.venv\Scripts\python.exe`
   under `%LOCALAPPDATA%\UnderCover`, and that `PATH` (tools\ffmpeg, tools\deno) survives the relaunch.
   UnderCover.cmd sets PATH, and child processes inherit it, so it should work.
9. **Settings on update.** The installer keeps an existing config.toml (`onlyifdoesntexist`), so keys added in
   newer versions will be missing. The config loader should fall back to built-in defaults for missing keys,
   or merge with a shipped `config.default.toml`.
