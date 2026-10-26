# UnderCover

UnderCover makes songs and speech in voices you choose, entirely on your own PC. There is no account, and nothing you
make leaves the machine.

It runs as a local web page and uses your graphics card. It is made of four parts.

FrontMan sings a song in another voice. You can drop in a song file or type a request such as "Pink Pony Club as
Plankton".

DoubleAgent trains voices. It finds clean clips of a person, keeps only that person's voice, trains a model and can
improve it overnight.

SpeakEasy reads text aloud in a voice, in that person's own manner of speaking once the voice has learnt it.

SafeHouse keeps your covers, speech, songs and voices. Its rooms are Library, Running, Storage (where the Vault keeps backups), Add-ons, System (with the Doctor) and Settings.

The standard pages show what you need to get a result. Scores, settings, logs and engine choices are in Deep Cover,
which you switch on from the top bar.

## Requirements

- Windows 10 (version 2004 or later) or Windows 11, 64-bit. It has been tested on Windows 11.
- An AMD graphics card supported by ROCm for Windows: Radeon RX 7700 or newer in the RX 7000 series, RX 9060, RX 9070,
  Radeon AI PRO R9700, or Radeon PRO W7800 or W7900. NVIDIA cards from the GTX 900 series on are supported
  too, and it has been tested on an RTX 2080. Without a supported card UnderCover runs on the processor, much more slowly and without voice
  training.
- About 22.5 GB of free disk space. The installer checks that it fits before it starts.
- An internet connection for setup, which downloads about 11.4 GB with an AMD card, 12.0 GB with an NVIDIA card or 9.3 GB
  without a supported card. That is everything: Python,
  PyTorch for your card, the tools and all the AI models. No more AI models download after setup. What you ask for later, such as songs, clips of a person or a voice from the community, comes from YouTube or Hugging Face, and an add-on, a repair, Away from home's Tailscale or a fix for the YouTube downloader download only when needed.

## Installing

1. Run `UnderCover-Setup-1.1.0.exe`. It installs for your Windows user only, in `%LOCALAPPDATA%\UnderCover`, and does
   not need administrator rights. The installer is not signed, so Windows may show a warning; choose More info and
   then Run anyway.
2. Choose where your files are kept. The defaults are fine. Optional add-ons are off. Ticking one accepts its licence,
   which you can read from the same page.
3. Setup then runs inside the installer, with one bar for the whole install and one for the current step. Every
   download is at a fixed version with its checksum verified. On our test connection, about 75 Mbit/s, it took about
   22 minutes. It ends with a self-test. Cancel keeps what is done, and running the installer again carries on from there.
4. Start UnderCover from the Start menu. It runs in the background with an icon near the clock and opens in your
   browser at `http://127.0.0.1:7860`. To quit, use the icon near the clock, or SafeHouse's System room.

Step-by-step instructions are in [installer/GUIDE.md](installer/GUIDE.md). To run from a source checkout, use
`powershell -ExecutionPolicy Bypass -File installer\setup.ps1` and then `scripts\UnderCover.bat`.

To update, run a newer installer. It installs over the old one in the same folder and keeps your files and settings.

To uninstall, use Uninstall UnderCover in the Start menu, or Windows Settings, Apps. The folders that hold your songs,
covers, speech, voices and models stay, and the uninstaller lists them at the end so you can delete them yourself.

Your settings are stored in `config.toml` in the app folder, which holds only the values you or setup have changed.
The defaults are in `undercover/config.default.toml`, so updates can add new defaults without changing yours.

The repository contains `undercover/` (the application), `installer/`, `scripts/` (`UnderCover.bat`, the
command-line cover tool `cover.bat` and the GPU check), `tests/` (`python tests/smoke.py`) and `docs/`.

## First run

Type a song and a voice into FrontMan, such as "Pink Pony Club as Plankton", and press Make it. If you don't have the voice yet, UnderCover first finds and checks one, or trains it, and the plan says how long before you press. A song's first cover also takes longer than the next ones, because UnderCover separates the song once and keeps the result. UnderCover also
comes with a short excerpt of the song *Hold On*, which is public domain, and a demo voice, Agent Smoke, made for
UnderCover and not based on a real person.

You can use UnderCover from your phone on your home Wi-Fi. Turn it on in SafeHouse's Settings room and pair the phone
with the QR code it shows.

## Privacy

UnderCover only accepts connections from your own PC and sends no usage data. Setup downloads its models from their
publishers, pinned to fixed versions and verified by checksum. Anything you ask UnderCover to find, such as a song or
clips of a person, comes through yt-dlp. When YouTube stops working, UnderCover fetches a newer YouTube downloader
from PyPI, tests it and keeps it only if it works. Deep Cover's System room can switch that off. Phone access and
access from outside your home are off until you turn them on, and each phone has to be paired.

Your voices, songs, covers and speech are kept in ordinary folders that you choose. Covers and speech you don't send
to SafeHouse roll off after the next fifteen; Send to SafeHouse keeps one for good. UnderCover never deletes a voice
on its own: a replaced or removed voice is kept in SafeHouse's Storage until you delete it yourself.

Speech read with the Chatterbox engines (SpeakEasy's default) carries Resemble AI's inaudible watermark, so a detector
may flag it as synthetic.

Only use voices you have the right to use, and say that a recording is synthetic when you share it.

## Troubleshooting

In SafeHouse, the System room has the Doctor, which checks the installation, fixes common problems and can save a
diagnostics file. The file contains text only, no audio, and shortens your folder names.

If songs from YouTube stop downloading, press Check for a fix in the System room.

The Start menu's Repair UnderCover checks the installed Python, tools and AI models against setup's record and puts back only
what is wrong, without opening the app. Your voices, covers and settings are not affected.

The history of changes is in [CHANGELOG.md](CHANGELOG.md).

## Licence

UnderCover is free software under the GNU Affero General Public License, version 3 or later: you may use, change and share it, and anyone who shares a changed version, or lets other people use one over a network, must offer them its source under the same licence. The text is in [LICENSE](LICENSE); [NOTICE](NOTICE) credits the projects and models UnderCover is built on and says what the licence does not cover. The AI models it downloads are under their publishers' terms, a voice carries the terms of whoever made it, and the names UnderCover, FrontMan, DoubleAgent, SpeakEasy, SafeHouse and Deep Cover identify this project. The song *Hold On* and the demo voice Agent Smoke are public domain (CC0). [CONTRIBUTING.md](CONTRIBUTING.md) has the house rules.
