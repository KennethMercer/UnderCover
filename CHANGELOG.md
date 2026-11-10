# Changelog

What changed in UnderCover, by release. The headings summarise; the detail is in the git history, under the names
in brackets.

## Not released yet

**Duets** (nobacking, holdparts, residue, notop, chordnote, duetpolish, duetwords, shapeh)
- The one-shot names both singers from the start ("Barry White & Michael Jackson, taking turns"), and the finished line
  gives the key the two share.
- A backing with harmonies is sung as one part, held through a crossing, instead of hopping between parts.
- In a chorus kept as it is, what's left of the lead under those lines stays silent, and the notes say who sings them.
- Before Convert, a duet says when the song has almost no backing, so the second singer has little to take there.
- Where the backing is a chord of several voices, the notes name the times, since one singer may waver there.
- The duet's words read plainly: who takes the verses and who the choruses, and who sings the backing.

**Shape it** (editpage, octprice2, shapeia, bugs-fm, firstopen, shapeh)
- New: a cover's own list of changes. Pick a stretch of its words, give it to the other singer or move it an octave,
  hear it sung again in seconds, undo, and keep the version you like as a new cover.
- Before an octave change, it says plainly when the voice won't fit there.
- It says in one sentence where a change lands, and under the other singer's backing the backing follows the lead.
- The first and last lines of a song can be changed, and a change sings from the cover's own cleaned lead.
- It works by keyboard, and a kept version has one name everywhere.

**The page** (bones1, findset, wave2, bones2p, polish3, polish4, polish5, polish6, polish7, firstpolish, phonecopy)
- Find a setting: type what you're after ("backup", "theme", "restart") and the right row comes up, Storage's
  included.
- One type scale, spacing on a grid, larger tap targets, and lines and quiet text you can see on every theme.
- A song's stems, a voice card's numbers and other expert detail stay in Deep Cover; Settings reads in a narrower
  column; the app's own housekeeping stays out of the basic view.
- A list that couldn't load says so, with Try again, and a failed refresh keeps what's on screen.
- A first install reads right: the demo voice and the sample song say what they are, and restart and quit say to use
  the Start menu.

**Quicker and steadier** (perf1, perf2, cap16k, bugs-core, bugs-da, bugs-vault, metaread, voicemissing)
- The Library, the voice list, voice checks and page loads are faster; a cover reads each stem once.
- Changing a setting no longer asks for a restart, and Restart and Quit wait for work that can't be paused.
- Overnight Improve and retraining start from the right place; a crash early in a training can't wipe its saves.
- Every form of `[pause]` is a pause, never read aloud, and text that isn't English is named as left out.
- Vault restores and Clean up stay safe, and settings written by hand keep working.
- A voice whose model file is missing says so by name, and how to get it back.

**Fixes** (quietdrops, plainerr2, bodyclose)
- The server's log keeps real problems only: a closed tab or a dropped connection no longer leaves an error there.
- A full disk, the PC running out of memory and no internet connection each say what to do, instead of "Something went wrong".
- An upload that drops while UnderCover's files are being moved says so, instead of saying contact was lost.
- Hardening of how the server answers an upload it refuses.

**Installer** (inst12, xttssize, xttsmeasured, port1)
- A PC with an NVIDIA card whose driver isn't working is told so plainly; Repair finds a card that works now.
- An update with moved folders counts space right, and uninstalling clears UnderCover's own cache but keeps your work.
- Ticking the XTTS-v2 add-on counts its 2 GB.

**Your phone** (onephone, linkflag)
- The phone link is off in this version and comes back in a later one. Phones paired on the home Wi-Fi before keep
  working there.

## 1.1.4 (2026-11-05)

**SpeakEasy** (hangfix, hangtune)
- A reading that stops oddly in the middle of a phrase is heard and read again. So is a short break between words
  that should run together ("was earned"). It's tried up to five times, and the smoothest take is kept. A thumbs-down
  for odd pauses or pace reads it again too, with a new take.

**SpeakEasy** (tags1)
- Directions in the text: `[pause]` (or `[pause 2s]`), `[louder]`, `[softer]`, `[higher]` and `[lower]`. A tag
  SpeakEasy doesn't know, or doesn't use yet (the speech engine's own `[laugh]` or `[whispering]`), is never read out;
  it's named under the result.

**FrontMan** (duets1, duets4, duets5, duets6, duets7, duets8ui)
- Duets: pick a second singer and they share the song, one on the verses and the other on the choruses, so the voice
  changes as each chorus comes in. Their ranges decide who takes which, and a section that clearly suits the other
  singer goes to them. The cover's notes say who took what. The change lands between sentences, even where the singer
  doesn't stop for breath. The backing vocals are sung by the two singers too, each backing line whole by whoever has the lead
  under it or by the one who hasn't, or kept as the original: your choice, beside the second singer. Once a
  song has been sung, the page draws who sang which part. In the one-shot, name both: "<song> as <one singer> and <another>".

**SafeHouse** (consult2)
- Storage gives one total. The room's number and its tab's now agree, and every model file counts, Whisper's included.

**The page** (look6)
- On a phone, every SafeHouse room is on show and the top bar is quieter. The Library's numbers follow what you've
  picked, and Running keeps its clock until there's work for it.

**Phone** (phone12)
- The away link works as a home-screen app. When the PC doesn't answer there, it says so and offers the home address.
- While UnderCover is open on the phone, it tells you when a job finishes. No notification service is involved.
- In Deep Cover, away from home can be private: only devices signed in to your Tailscale reach it, with nothing on
  the public internet.

**Fixes** (tidy1)
- A file that only looks like a song (nothing in it plays) is refused with a plain message.

## 1.1.3 (2026-10-29)

**Hardening** (sections, bytecap, decodecap)
- Links are handled the same way on every PC, whether or not ffmpeg is installed.
- A download from a link stays within a sensible size and length.

**Fixes** (decodecap)
- Dropping a song again that UnderCover couldn't read no longer removes the copy already in your uploads.

## 1.1.2 (2026-10-29)

**SpeakEasy** (speakhz)
- A voice with cadence now reads at the pitch the person talks at in its reference clip, not at the median of
  everything it was trained on. A voice trained on speeches to crowds no longer reads a calm line at its shouting
  pitch.

## 1.1.1 (2026-10-28)

**Hardening** (netguard)
- Links UnderCover fetches for you are checked again at the moment each connection is made, including every address
  the YouTube downloader follows by itself.

## 1.1.0 (2026-10-26)

**Hardening** (harden1)
- Stricter checks on requests to UnderCover's server, on the PC and from phones: who may call it, how much a request
  may send, how long a connection may wait, and how many may be open at once.
- Phone pairings now expire after 30 days without use; a phone you use keeps working.
- Links UnderCover fetches for you only reach the internet, and voice files are unpacked with size limits.
- Updated parts are checked before they're used, and nothing is installed without its checksum.
- The Phone access setting says that the home connection isn't encrypted.
- The first start after updating may use the YouTube downloader that came with this release once, while a newer one
  it fetched earlier is checked again.

**The First assignment, as a tour** (firstrun-tour)
- A fresh install opens on a four-step tour: your first cover (one press, about a minute), listening and keeping it,
  finding it in SafeHouse, and the start of a new voice. While the cover is made, the page stays dimmed with the app's
  own progress lit and a changing tip. Skip asks once; Settings replays it.

**Recent, Train my own, filters** (pagepolish2)
- FrontMan's and SpeakEasy's Recent show the five newest, with Show more for the rest of the fifteen.
- Every downloaded voice offers Train my own, with Now or Tonight on its card; it asks speaking or singing when the
  voice doesn't say.
- Your voices' filters have All, and remember what you picked.

**A person's own pace** (pace)
- Speech in a person's cadence picks slower, clearer clips of them as its reference, so it no longer rushes.

**The installer's add-ons** (addonpage)
- The add-on page has one row per add-on, each a single line with its download size.

**Speech that runs on past its text** (lbextra)
- When the speech engine carries on past the end of a sentence (repeating its last words, or a garbled tail), the
  extra is now cut off at the sentence's last word. A take with many extra words no longer counts the same as one
  missing a single word, so the better take is kept.

**Games are seen behind other windows** (gamedetect)
- The game pause, and Low profile's "on by itself for games", now see a game that isn't in front: a game from a
  Steam, Epic, GOG, Xbox, Riot, EA or Ubisoft library while it runs (its menus, or alt-tabbed out), and any program
  keeping the graphics card's 3D engine busy. Before, only a game filling the screen counted, so a game you had
  switched away from could be slowed by UnderCover's work.

**XTTS-v2 add-on** (xtts)
- Coqui's XTTS-v2 is back as an optional engine for SpeakEasy's speech in a person's cadence: added in SafeHouse,
  Add-ons (or ticked in the installer), 1.9 GB, under its non-commercial licence, which you read and accept there.
  It needs less graphics memory than the default and pauses more inside a sentence, but reads about three times
  slower. Without it nothing changes.

**Improve for singers** (singers)
- Improve can now judge a singing voice: on short phrases of the singer's own songs, kept out of the training, it
  offers a retrained voice only when it sounds clearly better there and follows the melody as well. The bar is new
  for singers and will be tuned by the first blind listening.

**Voice packs** (voicepack)
- A voice can be saved as one file, a `.ucvoice` voice pack, from its page in SafeHouse's Library, and opened
  on another PC with UnderCover (Import a voice pack, or drop it in). The pack says whose voice it is and on
  what basis it's shared, in the exporter's own words; a private person's voice is only shared with their
  agreement. Its weights are never a pickle, every file inside is checked before it's used, and an imported
  voice never replaces one you have. The way a voice talks (its cadence) stays on the PC that made it.
- A voice model whose settings ask for a network RVC doesn't build is refused with the reason, whether it comes as a
  voice pack or a downloaded model (cfgbounds): a crafted one could otherwise have used up the memory as it loaded.

## 1.0.3 (2026-10-25)

**Updates download only what changed** (v103)
- Installing a new version over an older one keeps the packages and tools it already has at the right versions,
  and downloads only what changed. Before, every update downloaded several GB again (1.0.2 over 1.0.0: 108 of
  137 files that hadn't changed).

## 1.0.2 (2026-10-25)

**Low profile** (lowprofile-page, v102)
- Low profile, in the top bar beside Deep Cover, keeps UnderCover to half the graphics card and works in bursts, so a
  game gets the card most of the time. The limit is one number for the whole of UnderCover; a job that needs more than
  it allows waits and says so instead of failing.
- When a full-screen game starts, the question offers it first, and remembers the answer for that game. Deep Cover
  holds the exact limit, how much of the time it works, and turning it on by itself for games.
- Times shown while it's on come from speeds measured under it; the strip says "Working at a low profile".
- Every test server runs with it on at 12 GB, so a test run leaves half the card.

## 1.0.1 (2026-10-24)

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
