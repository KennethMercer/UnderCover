# Changelog

What changed in UnderCover, by release. The headings summarise; the detail is in `docs/handoffs/` and the git tags
named in brackets.

## 1.0.0 (not tagged yet: after the release-candidate pass and the installer test)

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
