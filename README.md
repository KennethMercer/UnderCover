# UnderCover

**Songs and speech in the voices you choose, made on your own PC.** No account, no cloud: nothing you make leaves the
machine.

UnderCover runs in your browser and works on your graphics card. Four rooms, one crew:

| | |
|---|---|
| **FrontMan** | Sings a song in another voice. Drop in a song, or type what you want: *"Hold On as Agent Smoke"*. |
| **DoubleAgent** | Builds voices. Finds clean clips of a person, keeps only theirs, trains a voice, and can improve it overnight. |
| **SpeakEasy** | Reads text aloud in a voice, with that person's own way of speaking when the voice has learnt it. |
| **SafeHouse** | Keeps everything: covers, speech, songs and voices. Also the Running room, Storage, backups and the Doctor. |

The plain pages show only what gets a result. Scores, settings, logs and engine choices wait behind **Deep Cover**,
the glasses in the top bar.

## What it needs

- **Windows 11**, 64-bit. Windows 10 may work; it isn't tested.
- **An AMD graphics card with ROCm for Windows:** Radeon RX 7000 or RX 9000 (RX 7700 and up, RX 9060 / 9070), or
  Radeon PRO W7800 / W7900. That's the tested setup. Without one it runs on the processor: slowly, and without voice
  training. NVIDIA builds in place but hasn't been tested on real hardware.
- **About 15 GB free disk space:** 8.3 GB after setup, then the models each feature fetches the first time you use
  it (covers 1.2 GB, speech 2.8 GB, listen-back 1.6 GB, training about 2 GB).
- **A good internet connection for the first hour:** setup downloads about 2.5-3 GB.

## Install

1. Run **`UnderCover-Setup-1.0.0.exe`**. It installs for you only (`%LOCALAPPDATA%\UnderCover`) and never asks for
   admin rights. Windows may say it protected your PC, because the installer isn't signed: **More info**, then
   **Run anyway**.
2. Choose where your files go (the defaults are fine), and skip the optional add-ons unless you want them. Each one
   shows its licence and installs only if you accept it yourself.
3. A setup window downloads Python, PyTorch for your card and the tools, every one at a fixed version with a checked
   checksum. It takes 20-40 minutes and ends with a self-test.
4. Start **UnderCover** from the Start menu. It runs in the background (an icon by the clock) and opens in your
   browser at `http://127.0.0.1:7860`. To quit: SafeHouse, then **Quit UnderCover**.

The step-by-step for friends is [installer/FRIENDS.md](installer/FRIENDS.md). From a source checkout:
`powershell -ExecutionPolicy Bypass -File installer\setup.ps1`, then `scripts\UnderCover.bat`.

**Your settings** live in `config.toml` in the app folder, and it holds only what you (or setup) changed. The defaults
ship with the code in `undercover/config.default.toml`, so an update brings new defaults without touching your
choices. The folder itself: `undercover/` (the app), `installer/`, `scripts/` (what you run: `UnderCover.bat`, the
command-line cover tool `cover.bat`, the GPU check) and `tests/` (`python tests/smoke.py`).

## First run

**The First assignment** walks you through a cover in about ten minutes: a short sample song, *Hold On*, sung by
**Agent Smoke**, a demo voice made for UnderCover (not a real person). It ends by showing you how to train a voice of
your own tonight. You can replay it from SafeHouse's Settings.

## Private by design

- The app listens on this PC only. No telemetry, no account, no analytics.
- It downloads only the parts it needs (models from their publishers, pinned and checksum-verified) and what *you* ask
  it to find (a song or clips of a person, through yt-dlp).
- **Phone access** and **away from home** are off until you switch them on. Each phone pairs once with a code.
- Your voices, songs, covers and speech live in plain folders you choose. Nothing deletes a voice behind your back:
  a replaced or removed voice waits in SafeHouse's Storage until you delete it yourself.

Use voices you have the right to use, and say a recording is synthetic when you share it.


## When something breaks

**SafeHouse → System → the Doctor** checks the install, fixes the common problems itself, and can save a diagnostics
file (text only, no audio, your folder names shortened). **Start menu → Repair UnderCover** does the same from outside
the app. Your voices, covers and settings stay.

What changed and when: [CHANGELOG.md](CHANGELOG.md).
