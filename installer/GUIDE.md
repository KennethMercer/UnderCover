# Installing UnderCover

UnderCover makes song covers in other voices (FrontMan), trains voices (DoubleAgent), reads text aloud in a voice
(SpeakEasy) and keeps it all in one place (SafeHouse). Everything runs on your own PC, and nothing you make is uploaded
anywhere.

## Before you start

- Windows 10 (version 2004 or later) or Windows 11, 64-bit.
- A graphics card helps a lot. AMD RX 7700 and up, and the RX 9000 cards, are supported. NVIDIA cards from the GTX 900 series on are
  supported too (tested on an RTX 2080). Other PCs still work on the processor alone, just slowly, and without voice training.
- About 22.5 GB of free disk space. The installer checks that it fits.
- An internet connection for the install, which downloads about 11.4 GB with an AMD card, 12.0 GB with an NVIDIA card or 9.3 GB without a supported card. After that, no more AI models download. Songs, clips and community voices you ask for come from YouTube or Hugging Face.

## Install

1. Run `UnderCover-Setup-1.0.0.exe`. Windows may say "Windows protected your PC", because the installer isn't signed.
   Click More info, then Run anyway. It installs for you only and never asks for admin rights.
2. On "Where should UnderCover keep your files?" the defaults are fine. Pick another drive if your C: drive is short on
   space.
3. You can skip the optional add-ons. Each one shows its licence and installs only if you tick it.
4. Setup runs inside the installer. It downloads Python, PyTorch for your graphics card, the tools and the AI models.
   On our test connection, about 75 Mbit/s, it took about 22 minutes. Its two bars show the whole install and the
   current step, and it ends with a self-test. If something goes wrong, it says what in plain words. Cancel keeps
   what's done, and running the installer again carries on.
5. Start UnderCover from the Start menu. It runs in the background and opens in your browser.

## The first start

Type a song and a voice into FrontMan, such as "Pink Pony Club as Plankton", and press Make it. If you don't have the voice yet, UnderCover first finds and checks one, or trains it, and the plan says how long before you press. A song's first cover also takes longer than the next ones, because UnderCover separates the song once and keeps the result. A short sample song
and Agent Smoke, a demo voice made for UnderCover and not a real person, are there to try.

To quit, use the icon near the clock, or SafeHouse's System room.

## If something breaks

The Start menu's Repair UnderCover checks the install against its record and puts back only what's wrong. Your voices,
covers and settings stay.

SafeHouse's System room has the Doctor, with the same checks from inside the app, and a diagnostics file you can send
me. It holds no personal files. If songs from YouTube stop downloading, press Check for a fix in the same room.

## Optional extras

These are all off until you switch them on.

Phone access, in SafeHouse's Settings room, lets you use UnderCover from your phone on your home Wi-Fi. Each phone
pairs once with a QR code.

Away from home reaches it from anywhere through a private link. It needs a free Tailscale account and downloads about
39 MB when you switch it on. Nothing is installed on the phone. Pair the phone at home first.

Vaulting, in SafeHouse, makes quiet backups of your voices, songs, covers, speech and training projects to another drive.

## Updating and uninstalling

To update, run a newer installer. It installs over the old one and keeps your files and settings.

To uninstall, use Uninstall UnderCover in the Start menu, or Windows Settings, Apps. It removes the app, Python and the
caches. Your voices, covers, songs, training projects and the AI models stay, and the uninstaller lists their folders
at the end so you can delete them yourself. If you used Away from home, this PC's private link ends; it shows as
offline in your Tailscale account, where you can remove it.
