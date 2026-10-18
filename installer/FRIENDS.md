# UnderCover: your briefing

UnderCover makes song covers in other voices (FrontMan), trains voices (DoubleAgent), reads text aloud in a voice
(SpeakEasy) and keeps it all in one place (SafeHouse). Everything runs on your own PC: nothing you make is uploaded
anywhere.

## Before you start

- **Windows 10 or 11, 64-bit.**
- **A graphics card helps a lot:**
  - an NVIDIA card (GTX 900 series or newer);
  - or an AMD RX 7000 / RX 9000.

  Other PCs still work on the processor alone, just slowly, and without voice training.
- **About 22 GB free disk space**, and a good internet connection for the first hour. The installer checks that it fits.

## Install

1. **Run `UnderCover-Setup-1.0.0.exe`.** Windows may say "Windows protected your PC": the installer isn't signed. Click
   **More info**, then **Run anyway**. It installs for you only and never asks for admin rights.
2. **"Where should UnderCover keep your files?"** The defaults are fine. Pick another drive if your C: drive is
   short on space.
3. **Optional add-ons:** you can skip these. Each one shows its licence, and only installs if you tick "I accept".
4. **Setup runs inside the installer** and downloads Python, PyTorch for your graphics card, and the tools. It takes
   10-40 minutes, and its two bars show the whole install and the current step. It ends with a self-test. If something
   goes wrong, it says what, in plain words. Cancel keeps what's done, and running the installer again carries on.
5. **Finish.** Start UnderCover from the Start menu. It runs in the background and opens in your browser.

## The first start

- **The first cover downloads the separation and voice models**, once. Training, de-reverb and speech each fetch
  their own models the first time you use them, up to about 10.5 GB for all of them.
- **The First assignment** walks you through a cover in about ten minutes. It uses a short sample song and Agent
  Smoke, a demo voice made for UnderCover (not a real person).
- **To quit:** SafeHouse, then **Quit UnderCover**.

## If something breaks

- **Start menu, Repair UnderCover.** The Doctor checks the install against its record and fixes only what's wrong.
  Your voices, covers and settings stay.
- **SafeHouse, System, the Doctor:** the same checks from inside the app, plus a diagnostics file you can send me. It
  holds no personal files.

## Optional extras (all off until you switch them on)

- **Phone access (SafeHouse, Settings):** use UnderCover from your phone on your home Wi-Fi. Each phone pairs once
  with a code.
- **Away from home:** reach it from anywhere through a private link. It needs a free Tailscale account and downloads
  about 39 MB the first time; nothing is installed on the phone. Pair the phone at home first.
- **Vaulting (SafeHouse):** quiet backups of your voices and covers to another drive.

## Uninstall

Windows Settings, Apps, UnderCover, Uninstall.
- **Removed:** the downloaded runtime and caches.
- **Kept:** your voices, covers, songs and training projects, and the AI models. The uninstaller lists the folders at
  the end; delete them yourself if you want them gone.
- **Away from home:** this PC's private link ends. It shows as offline in your Tailscale account, and you can remove
  it there.
