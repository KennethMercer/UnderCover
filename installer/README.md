# UnderCover installer

A small Windows installer for friends' PCs. It copies the app code, then `setup.ps1` detects the graphics card and
downloads the matching PyTorch and tools. Nothing heavy is bundled.

**Status (2026-10-09, late):**
- **AMD (ROCm): verified on the dev PC.** A real install into `D:\UnderCoverTest` passed the self-test. The
  server started on port 7861, downloaded 1.2 GB of models on first start, and made a 30 s cover in 17.9 s.
- **CPU: verified.** `-Backend cpu` into `D:\UnderCoverTestCPU`: 0.9 GB venv, self-test passed, the page shows
  "Processor only (slow)", the same cover took 66 s, and training is refused with a friendly message.
- **NVIDIA: not tested** (needs real hardware). The wheels exist and the code paths are in place.
- **`.exe` builds:** `dist\UnderCover-Setup-0.1.0.exe`, about 2.2 MB. Its install wizard hasn't been clicked
  through yet; setup.ps1, which it runs, has.
- Core edits needed for non-ROCm installs are applied; the remaining items are in [CORE_CHANGES.md](CORE_CHANGES.md).
- Bug found and fixed in the real run: Windows PowerShell wrote config.toml with a BOM, which made the app crash on
  start. setup.ps1 now writes UTF-8 without one, and selftest.py loads the config so this fails setup instead.

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
- First start: 1.2 GB of models (measured). The karaoke, de-reverb, training and speaker models (another ~2 GB)
  download the first time those features are used.
- CPU installs are much smaller: the venv is 0.9 GB.

## Next steps

1. **NVIDIA test** on a friend's PC or a rented cloud Windows GPU machine: a cover, plus a short training run
   (exercises the bf16/fp32 choice and fused AdamW).
2. Click through the `.exe` wizard once on this PC, choosing a test folder on the folder page.
3. Remaining items in [CORE_CHANGES.md](CORE_CHANGES.md): grey out training in CPU mode, VRAM tiers, restart
   button check, and settings merge on update.
4. Optional: code-sign the `.exe` to avoid the SmartScreen warning.

Rebuild the `.exe` after any change: `"%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe" installer\UnderCover.iss`
(Inno Setup 6.7.3, installed per-user with winget).

## Known gaps

- `onnx-weekly==1.24.0.dev20261005` (pulled in by audio-separator) is a nightly build. PyPI may delete old
  nightlies, which would break installs. Replace it with a stable `onnx` once audio-separator allows that.
- The ffmpeg URL is gyan.dev's "latest release" link, not a pinned version. Pin it, with a checksum, before
  sharing the installer widely.
- No updater yet. To update, run a newer installer over the old one. Settings, voices and covers are kept.
