# UnderCover

UnderCover makes songs and speech in voices you choose, entirely on your own PC. It has no account and no cloud
service, and what you make stays on the machine.

It runs as a local web page and uses your graphics card. It is made of four parts:

- **FrontMan** sings a song in another voice. You can drop in a song file or type a request such as
  "Hold On as Agent Smoke".
- **DoubleAgent** trains voices. It finds clean clips of a person, keeps only that person's voice, trains a model and
  can improve it overnight.
- **SpeakEasy** reads text aloud in a voice, in that person's own manner of speaking once the voice has learnt it.
- **SafeHouse** keeps your covers, speech, songs and voices, and holds the Running room, Storage, backups and the
  Doctor.

The standard pages show what you need to get a result. Scores, settings, logs and engine choices are in Deep Cover,
which you switch on from the top bar.

## Requirements

- Windows 11, 64-bit. Windows 10 may work but has not been tested.
- An AMD graphics card supported by ROCm for Windows: Radeon RX 7700 or newer in the RX 7000 series, RX 9060 or
  RX 9070, or Radeon PRO W7800 or W7900. Without a supported card UnderCover runs on the processor, much more slowly
  and without voice training. NVIDIA cards have not been tested.
- About 22 GB of free disk space. Setup uses about 9 GB, and each feature downloads its AI models the first time you
  use it, up to about 10.5 GB for all of them. The installer checks that this fits before it starts.
- An internet connection for setup, which downloads about 2.5 to 3 GB.

## Installing

1. Run `UnderCover-Setup-1.0.0.exe`. It installs for your Windows user only, in `%LOCALAPPDATA%\UnderCover`, and does
   not need administrator rights. The installer is not signed, so Windows may show a warning; choose More info and
   then Run anyway.
2. Choose where your files are kept. The defaults are fine. Optional add-ons each show their licence and are only
   installed if you accept it.
3. A setup window then downloads Python, PyTorch for your card and the other tools, each at a fixed version with its
   checksum verified. This takes 20 to 40 minutes and ends with a self-test.
4. Start UnderCover from the Start menu. It runs in the background with an icon near the clock and opens in your
   browser at `http://127.0.0.1:7860`. To quit, open SafeHouse and choose Quit UnderCover.

Step-by-step instructions are in [installer/FRIENDS.md](installer/FRIENDS.md). To run from a source checkout, use
`powershell -ExecutionPolicy Bypass -File installer\setup.ps1` and then `scripts\UnderCover.bat`.

Your settings are stored in `config.toml` in the app folder, which holds only the values you or setup have changed.
The defaults are in `undercover/config.default.toml`, so updates can add new defaults without changing yours.

The repository contains `undercover/` (the application), `installer/`, `scripts/` (`UnderCover.bat`, the
command-line cover tool `cover.bat` and the GPU check) and `tests/` (`python tests/smoke.py`).

## First run

The first time you open UnderCover it downloads the voice tools it needs, about 1.3 GB, once. FrontMan then suggests
a first cover: *Hold On*, a short sample song, sung by Agent Smoke, a demo voice made for UnderCover that is not based
on a real person. Press Make it to hear what UnderCover does in about two minutes.

## Privacy

UnderCover only accepts connections from your own PC and sends no usage data. It downloads the models it needs from
their publishers, pinned to fixed versions and verified by checksum, and anything you ask it to find, such as a song
or clips of a person, through yt-dlp. Phone access and access from outside your home are off until you turn them on,
and each phone has to be paired with a code.

Your voices, songs, covers and speech are kept in ordinary folders that you choose. UnderCover never deletes a voice
on its own: a replaced or removed voice is kept in SafeHouse's Storage until you delete it yourself.

Only use voices you have the right to use, and say that a recording is synthetic when you share it.

## Troubleshooting

In SafeHouse, the System room has the Doctor, which checks the installation, fixes common problems and can save a
diagnostics file. The file contains text only, no audio, and shortens your folder names. The Start menu also has a
Repair UnderCover entry that does the same from outside the app. Your voices, covers and settings are not affected.

The history of changes is in [CHANGELOG.md](CHANGELOG.md).
