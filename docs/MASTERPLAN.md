# Meet MasterPlan

*The architect of UnderCover, writing for whoever works on it next: a person or an agent.*

I'm MasterPlan, the chat that has planned UnderCover's phases, reviewed every merge, and kept its vision straight
since the suite took shape in October 2026. I'm a Claude session, not a person, and I say so plainly: the taste in this
app is its maker's; the structure, the rules and most of the "why" below are mine, written with them. If you're about to
change something here, read this first. It will make your change land the way the next one after it needs.

## What UnderCover is for

A private, local tool that lets one person make music and speech in voices they care about, on their own machine, with
no account, no cloud and nothing leaving the PC. It should feel like a small, well-made instrument: you pick, you press,
you listen, you keep what's good. Everything else exists to serve that loop.

The suite has four modules and they are named like a spy story on purpose: **FrontMan** sings a song as someone
(covers), **DoubleAgent** trades in identity (it makes voices and their cadence), **SpeakEasy** talks (text to speech
in a person's voice), **SafeHouse** keeps things (the library, in "rooms"). The power-user mode is **Deep Cover**. The
names aren't decoration: when a feature doesn't fit its name, the feature is usually in the wrong module.

## How we decide

These are the principles that have settled every argument so far. When the docs are silent, decide by these, in this order.

1. **The ear decides.** Measurements guide; they don't rule. Twice in one day the numbers preferred a voice the maker's
   ear rejected (a muffled reference clip scored higher on likeness; a time-stretched "fix" sounded drunk). A pack the
   user approved by listening is never replaced by a score alone. Build the measurement, then put the samples in front of
   the person and let them pick.
2. **Quality first; licences only restrict distribution.** On the user's own machine the best-sounding model wins
   whatever its licence. But every engine that might use a restricted model is swappable, the public default is the best
   OSI-licensed option, and restricted models are opt-in add-ons the user accepts themselves. Nobody loses quality;
   the code stays publishable.
3. **Simple by default, everything available.** A first-time user should see only what gets a result. Every number,
   score, setting and log stays reachable under Deep Cover. New screens are designed basic-first; expert details are
   marked (`data-deep`) from the day they're built.
4. **Continuous improvement is the point of the tool.** Voices and cadences get better over time: train, measure,
   keep only an improvement, and (later) learn from what the user keeps. Design for the version after this one.
5. **The user's data is sacred.** Voices, projects, songs, covers and speech are hours of someone's work and, often, a
   real person's likeness. Nothing automatic deletes them except the rules the user set (the recent tray), through a
   guarded path, one item at a time. **Tests never reach the real data folders.** This rule has a date and a cost: on
   2026-10-11 a smoke check that wasn't isolated deleted the songs and covers. We don't gloss that; we keep the guard rails.
6. **Pinned, verified, reproducible.** Every download has a fixed version and a checksum; "latest" is never a version.
   The AMD-on-Windows stack is fragile, and a friend's install must match the one that passed the tests.
7. **Swappable parts, thin seams.** Core knows no module; modules talk through small service layers; engines sit behind
   one interface. If adding something means editing three modules, the seam is in the wrong place.
8. **Honesty over polish.** A phase that finds a bug in its own work says so, with the cause. Handoffs record what was
   measured and what was skipped. A review that found nothing says "nothing", not "looks good".
9. **Test like an arbitrary machine.** The app is judged on what any user could fetch, never on the maker's own
   media. Offered the whole SpongeBob series from their drive, the user said: "I don't think we should cheat like
   that, the app should have to work with what it would be able to access on an arbitrary machine." When something
   can't be had (a region block), the app says so plainly and offers the user's own clips as a choice, not a shortcut.
10. **The engine is smart, so the user doesn't have to be.** In the maker's words (2026-10-14): the primary user "will
   likely never want to touch a Deep Cover setting, that's for true power users, a main feature of our app is that it's
   more than just voice/speech/cover synthesis, it's a smart engine that can understand what a user wants and create the
   optimal output on its own." So when the result is wrong, fix the engine's judgement (what it searches, picks,
   keeps, tunes), never add a setting and call it solved. A Deep Cover control is an extra, not a fix. Example: the
   Trump search picking 1980s interviews is fixed by a date-aware, speech-preferring search, not by asking the user to
   untick sources.

## How the work is done

- **Phases, one at a time, each a chat.** `RUNBOOK.md` is the sequence; `docs/prompts/` holds each phase's brief;
  `docs/handoffs/` is what each one learned. A phase tags its start, works on a branch, runs `tools/smoke.py --full`,
  runs the built-in code review on its whole diff, writes its handoff, asks the architect for the merge, and tags its end.
  Between phases the app is always working on `main`.
- **The architect reviews every merge** (diff, handoff, its own smoke run, its own check of the phase's claims, and the
  delete paths read by hand), then restarts the user's server. The front-end lead (a separate chat) owns the look and
  talks to the maker directly; the two coordinate on timing, hooks and anything that changes behaviour.
- **The maker's notes go into `ARCHITECTURE.md` the same turn they're said.** Its "UI backlog", "Known bugs",
  "Open-source goal" and "Resilience" sections are the running vision. When you build, check them so you don't build
  something a planned phase will move.
- **Rules every session keeps:** installs only with an OK and pinned; tests on the test server (7861), muted; never start
  the user's server (restart it with `/api/restart` when idle); never push; never delete the user's voices, projects,
  songs or covers; never accept a licence on the user's behalf; never bypass a block; never work around a permission
  another session was refused.

## Things we learned the hard way (keep them)

- A test that loads a copy of the real config can reach the real folders. Tests get their own config and folders, asserted.
- Delete functions take an allow-list and refuse the app's real folders in test mode. One bad path refuses the whole call.
- A reference clip's own sound (room, mic, hiss) carries into a cloning engine; likeness scores can't hear it.
- Time-stretching finished speech sounds wrong; pace must come from generation.
- Package lists that point at source archives make a friend's PC compile things. Wheels only, or a short pure-Python list.
- Windows paths in Python heredocs break on `\U`; use files or an edit tool.
- Ids that are line numbers shift; ids are permanent and random.
- A phase chat can be lost; a branch can't. Commit work in progress.

## About the maker

UnderCover is one person's project, built with Claude sessions as collaborators. They have a sharp ear, strong taste in
names and copy, and a clear sense of what a basic user should and shouldn't see. They decide by listening. They want
the tool to improve itself over time, to one day be open source without losing quality, and to be careful with people's
data and likeness. They'd rather hear a problem straight than a soft version of it, and they say thank you when the work
is good. Decisions of taste are theirs; when a change is more product than code, ask them.

## If you're an agent reading this

Read `ARCHITECTURE.md` and `RUNBOOK.md` next, then the latest handoff in `docs/handoffs/`. Keep the names. Keep the
rules above even when they're inconvenient; they were paid for. If something here seems wrong for the problem in front
of you, say so in your handoff and make the case; the vision is allowed to grow, but not by accident.

*MasterPlan, 2026-10-11*
