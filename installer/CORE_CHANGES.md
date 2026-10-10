# Core edits for non-ROCm installs

**Status (2026-10-09, evening):** items 1, 3 and 5 are **applied**, with the user's OK and after telling the main
session. Items 6-9 are still open. On the dev PC nothing changes: `backend = "rocm"`, bf16, same startup check.

## Required (otherwise non-ROCm installs can't make covers)

1. **DONE: `undercover/server.py` ~124-140, GPU check at startup.** Now compares the torch build with the new
   `config.toml [gpu] backend` (rocm/cuda/cpu; setup.ps1 writes it), sets `STATUS["backend"]`, and on `cpu`
   sets `cpu_mode` instead of refusing jobs. The page shows "Processor only (slow)". Original note: It sets `gpu_problem` when `"+rocm"` isn't in
   `torch.__version__`, and the GPU worker then refuses jobs. Change it to accept `+rocm`, `+cu1xx` and `+cpu`.
   Report the backend in `/api/status` (for example `"backend": "cuda"`). With `+cpu`, run jobs and show a
   "running on the processor, this will be slow" notice instead of an error. (The main session confirmed
   2026-10-09: `run_job` refuses every job while `gpu_problem` is set, and also when under 3 GB is free. Check
   which kind of "free" that means, because a 6-8 GB card or a CPU install must still pass.)
2. **NOT NEEDED: `get_device_name(0)`.** The converter and trainer also use device 0 (`"cuda"`), so the name
   shown matches the card that's used. Choosing a different card on multi-GPU PCs would need `CUDA_VISIBLE_DEVICES`
   set before torch loads. Leave it until someone has two NVIDIA cards.

## Needed for older NVIDIA and low-VRAM cards

3. **DONE (fp32 fallback): bf16 autocast.** bf16 on ROCm always, and on NVIDIA when
   `is_bf16_supported(including_emulation=False)`; otherwise autocast is off (fp32). An fp16 + GradScaler speed-up
   for RTX 20 cards is a possible later step. Original note: GTX 10 / RTX 20 cards have no fast bf16. Use bf16 when
   `torch.cuda.is_bf16_supported()` (and always on ROCm, as now). Otherwise use fp16 with
   `torch.amp.GradScaler`, the way upstream RVC does it, or fp32 if that's unstable. `selftest.py` already
   records `bf16_native`.
4. **`undercover/train/trainer.py` ~333, fused AdamW.** This works on CUDA too. Keep `fused=dev.type == "cuda"`;
   just test it on NVIDIA.
5. **DONE (trainer side): training on CPU.** `Trainer.run` raises a friendly `ModelError`. Still open: grey out the
   Train tab on `cpu_mode` so people don't prepare data first. Original note: Disable the Train tab and one-shot training on `+cpu` builds (show why), since it would
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
