# UnderCover installer

A small Windows installer for friends' PCs. It copies the app code, then `setup.ps1` detects the graphics card and
downloads the matching PyTorch and tools. Nothing heavy is bundled.

**Status (2026-10-09):** written and dry-run tested on the dev PC. **No real install has been run yet**, and the
`.exe` hasn't been built (Inno Setup isn't installed). The core app still needs the edits in
[CORE_CHANGES.md](CORE_CHANGES.md) before NVIDIA or CPU installs can make covers.

## Files

| File | What it does | Tested |
|---|---|---|
| `detect_gpu.ps1` | Reads the Windows GPU list and `nvidia-smi`, then picks `rocm` / `cuda128` / `cuda126` / `cpu`. Read-only. | On the dev PC: picks the 7900 XTX over the iGPU |
| `setup.ps1` | uv → Python 3.12 → venv → PyTorch for the backend → other packages pinned around it → ffmpeg + Deno → GPU name in config.toml → self-test → `UnderCover.cmd` launcher. `-DryRun` prints every step. | Dry run only |
| `selftest.py` | Checks imports, tools, the torch build, GPU visibility, fp16/bf16 support, VRAM and a quick speed figure. | In the dev venv: passes on ROCm, and correctly fails when told to expect CUDA |
| `requirements/torch-*.txt` | Exact PyTorch 2.9.1 builds per backend. | All wheels confirmed to exist for cp312/win_amd64 |
| `requirements/common.txt` | Every other package, pinned from the dev venv. | Matches the dev venv |
| `UnderCover.iss` | Inno Setup script: per-user install to `%LOCALAPPDATA%\UnderCover`, no admin, Start-menu shortcuts. | Not compiled |
| `CORE_CHANGES.md` | The app edits needed for non-ROCm machines (deferred). | n/a |

## Which build each PC gets

| Hardware | Backend | PyTorch |
|---|---|---|
| NVIDIA RTX 20 series and newer (compute 7.5+), including RTX 50 | `cuda128` | 2.9.1+cu128 |
| NVIDIA GTX 900/10 series (compute 5.0-7.4) | `cuda126` | 2.9.1+cu126 (cu128 dropped these cards) |
| AMD RX 9070/9060, RX 7900/7800/7700, PRO W7900/W7800, AI PRO R9700 | `rocm` | AMD 2.9.1+rocm7.2.1 (same as dev) |
| Older AMD, RX 7600, Intel, no GPU | `cpu` | 2.9.1+cpu |

The AMD list comes from AMD's Windows ROCm 7.2.1 compatibility page (gfx1200/1201/1100/1101), checked
2026-10-09. Windows only gives product names, so `detect_gpu.ps1` matches names. Update that table when AMD
adds cards.

## Safety rules carried over from the dev setup

- PyTorch is installed first, from its own index only. The other packages are then installed constrained to the
  exact torch that landed, so nothing can swap the build. This is the same idea as `constraints.txt`.
- Everything lives in the install folder (`tools\uv`, `tools\python`, `tools\ffmpeg`, `tools\deno`, `.venv`,
  `cache\uv`). No PATH or registry changes, no admin rights.
- `setup.ps1` refuses to touch a `.venv` it didn't create (marker file `.venv\undercover-setup.txt`), so pointing
  it at `D:\UnderCover` can't damage the dev venv.

## Sizes

- Installer `.exe`: about 1 MB.
- Setup downloads: PyTorch 2-3 GB (ROCm and CUDA are the largest), other packages about 0.5 GB, ffmpeg about
  100 MB, Deno about 40 MB, Python about 30 MB.
- Disk after setup: the ROCm venv is 6.4 GB on the dev PC (the ROCm SDK wheels are big); CUDA should be similar.
  Plan for about 10 GB free in total, including models.
- First start: about 3 GB of models (separation 2.6 GB, HuBERT + RMVPE + RVC base about 0.56 GB, speaker
  model 26 MB). The app already downloads these on first use.

## Next steps

1. **Install Inno Setup 6 on the dev PC** to build the `.exe` (needs your OK):
   `winget install --id JRSoftware.InnoSetup -e`
2. **Real test on this PC:** `setup.ps1 -InstallDir D:\UnderCoverTest` (a separate folder, so the dev install is
   untouched). This proves the ROCm path end to end. Use test port 7861 when starting it.
3. Apply [CORE_CHANGES.md](CORE_CHANGES.md) (after `git init`, and with the main session's OK).
4. **NVIDIA test** on a friend's PC or a rented cloud Windows GPU machine. Also test the CPU path
   (`-Backend cpu` works on any PC).
5. Optional: code-sign the `.exe` to avoid the SmartScreen warning.

## Known gaps

- `onnx-weekly==1.24.0.dev20261005` (pulled in by audio-separator) is a nightly build. PyPI may delete old
  nightlies, which would break installs. Replace it with a stable `onnx` once audio-separator allows that.
- The ffmpeg URL is gyan.dev's "latest release" link, not a pinned version. Pin it, with a checksum, before
  sharing the installer widely.
- No updater yet. To update, run a newer installer over the old one. Settings, voices and covers are kept.
