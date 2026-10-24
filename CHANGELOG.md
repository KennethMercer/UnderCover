# Changelog

What changed in UnderCover, by release. The headings summarise; the detail is in the git history, under the names
in brackets.

## 1.0.1

**Licence** (licence, v101)
- UnderCover is licensed under the GNU Affero General Public License, version 3 or later (LICENSE, NOTICE,
  CONTRIBUTING.md). "Hold On" and Agent Smoke are public domain (CC0 1.0).
- The install folder has LICENSE, NOTICE and README.md.

**The installer** (v101)
- A long file name on the download page is shortened in the middle, so the line fits.
- Files kept from an earlier run are checked against their SHA-256 again before they're used; a damaged one is
  downloaded again.

**Honest times** (v101)
- The speech engine gets ready while UnderCover is idle, so the first speech of a session starts sooner. When it
  isn't ready yet, the time SpeakEasy says includes the start-up and names it.
- Improve's estimate comes from how this PC trains: its preparation rate, its training speed, and whether it refines
  or retrains.
- The overnight report's Improve line says whole minutes, or "no new clips", once.

**Small things** (hotfix-meta, v101)
- Running from source, a fresh library's voice list failed: the demo voice's settings file carried a note the
  voice list didn't expect. Voices now load whatever extra notes their settings carry.
- An import's Install button names its second round (the voice's index) and its preview line, instead of counting
  to 100 % twice.
- A community voice chosen by the one-shot's check is listed under the name the check used.
- "A" or "an" goes by the sound of the word: a unique voice, an R&B version.

## 1.0.0 (2026-10-24)

**An installer that does it all up front** (rc-fix6, rc-fix8, rc-polish1)
- Every file setup needs, about 11.4 GB for an AMD card or 12.0 GB for NVIDIA, downloads on the installer's own page,
  each one checked against its SHA-256. A download that breaks off is tried again by itself, Stop keeps what's done,
  and running the installer again carries on from there.
- After the downloads, setup works offline. The window stays usable throughout, and Show details opens the log.
- No AI model downloads the first time it's used.

**Covers in one go** (rc-fix7, rc-polish1)
- Type a song and a voice, such as "Pink Pony Club as Plankton". A voice from the community is checked against real
  clips of the person first, and the bundled Readers stand in when the clips have no other voices to compare.
- A voice that fails its check never joins your voices. A check that can't be done offers training or adding clips.

**Hear a voice before you pick it** (rc-preview)
- Every voice has a play button that says "Go undercover as" its name. The clip is made when the voice is.

**Honest times** (rc-eta, rc-fix10)
- Training is estimated from how fast this PC trains, and the time left follows the training. The bar never goes back.
- Long speech is estimated from its words at this PC's speed.

**Stop means stop** (rc-fix10)
- SpeakEasy's Stop ends the work on the graphics card within a couple of seconds. Long run-on text is read in shorter
  pieces.

**Easier to read** (rc-ui1, rc-fix9)
- The facts under a name are chips instead of a dotted line. FrontMan's song and voice searches look like searches
  and sit in the same place.
- Show in folder works when the path has a space in it.

**Improve that improves** (phase 26, smallfix7)
- Every voice gets a frozen test: the person's own lines and other people's clips, scored the same way every time.
- Improve refines a well-fed voice or retrains from scratch, with a training budget that grows with the clips.
- A retrained voice is offered only when it measures better on the person's own words; otherwise yours is kept and
  the attempt waits in Set aside. The card and the report say what changed, in words.
- Sources that don't sound like the person are left out before training.

**Voices from the right material** (smallfix7)
- The search prefers the person's own speeches and recent interviews; talk *about* them and decades-old material are
  left out. "Do it all" prepares the best sources first and checks each one is theirs.
- An unticked source never trains. SpeakEasy's Reader is picked by voice print and pitch, American by default.

**Nothing is deleted behind your back** (Internal Affairs passes 3-7)
- A run onto an existing voice waits beside it for your ear. Replace, Remove and Clean up set voices aside, with
  Bring it back. Deleting for good happens only in Storage, by you.
- Storage's folder move is transactional: a failure puts everything back.

**Tonight means tonight** (smallfix1-6, startfix)
- The overnight queue starts at the time you set, packs the night, resumes paused work first, and says honestly when
  it'll be done. The time you set sticks.
- A speaking voice gets its cadence automatically. Train tonight instead moves a live job to the queue.
- The report says what happened in words.

**The pages, made calmer** (qa1-qa20)
- One question on FrontMan, one Tonight in the Running room, Storage in two groups, one sticky band on a phone.
- Plainer words everywhere; times from one estimator; confirmations that can't be double-clicked.

**A tidier app folder** (layout)
- The root holds the README, the changelog and the app's folders: `undercover/`, `installer/`, `scripts/` (UnderCover.bat
  and the other things you run) and `tests/`. Shortcuts to the old UnderCover.bat follow it once, by themselves.
  The default settings ship in
  `undercover/config.default.toml`; your `config.toml` keeps only what differs, and an older full one is slimmed
  once at start, with the full file kept beside it.

**SpeakEasy** (phase 23, smallfix7)
- Chatterbox-Turbo is the default cadence engine: about twice as fast, cleaner on hard words.

## 0.4.0 (2026-10-13, the first installer build)

**The suite** (phases 0-2c, rooms)
- FrontMan, DoubleAgent, SpeakEasy and SafeHouse in one shell, with a running bar, a job feed and the Library.

**Covers** (phases 6, 7, 15, 21, 22)
- The one-shot: type what you want, and it finds the song and the voice, asking when the song is ambiguous.
- Keep or roll off, display names, register-aware key fit, chorus kept, true-peak mastering.

**Voices** (phases 4, 9, 12, 19, 24)
- Cadence packs: SpeakEasy reads in the person's own pace and pitch. Keep this one, by ear.
- More clean voice per source; lossless clips; Make smaller, never worse.

**Your machine, your rules** (phases 4b, 8, 10, 11, 14, 16, 18, 20)
- Pinned, checksum-verified downloads; add-ons whose licences you accept yourself.
- The Doctor: checks, fixes, startup healing and a diagnostics file.
- Phone access on your home network, then away from home through a private link; a tray icon.
- Self-healing yt-dlp; themes; game pause.

**Getting started** (phases 17, 25)
- The First assignment with Agent Smoke and *Hold On*; What's new; the thumbs and Your ear.
- Vaulting: quiet, incremental backups to another drive.
- The Windows installer: per-user, no admin, with Repair.
