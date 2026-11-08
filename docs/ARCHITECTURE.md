# UnderCover architecture

This document describes how UnderCover is built, as of 1.1.4: the names, the modules, the code layout, the data on
disk, the engines and why they were chosen, the page, and how the app looks after itself. It is the contract a change
is measured against. What the project is for and how decisions are made is in [MASTERPLAN.md](MASTERPLAN.md); how to
install is in [README.md](../README.md) and [installer/GUIDE.md](../installer/GUIDE.md); how to contribute is in
[CONTRIBUTING.md](../CONTRIBUTING.md); the theme file format is in [THEMES.md](THEMES.md).

Comments in the code that cite `docs/research/...`, `docs/handoffs/...` or `PLAN.md` refer to the maintainers' working
notes, which are not part of this repository. Their conclusions are summarised here, in "Why these engines".

Contents

1. [Names](#names)
2. [Modules](#modules)
3. [Code layout](#code-layout)
4. [Settings and folders](#settings-and-folders)
5. [Data contracts](#data-contracts)
6. [Why these engines](#why-these-engines)
7. [UI](#ui)
8. [Open-source goal](#open-source-goal)
9. [Resilience and self-healing](#resilience-and-self-healing)
10. [Tests, scripts and the installer](#tests-scripts-and-the-installer)

## Names

| | Name | What it is |
|---|---|---|
| Suite | UnderCover | the whole app: the Python package `undercover`, the launcher, the installer |
| Covers | FrontMan | a song sung in another voice, built by hand or in one shot from a line of text |
| Voices | DoubleAgent | finds clean clips of a person, trains and improves their voice, runs the overnight queue |
| Speech | SpeakEasy | text to speech in a chosen voice, in that person's own cadence once the voice has one |
| Files | SafeHouse | the library and everything about files, in rooms: Library, Running, Storage, Add-ons, System, Settings |
| Deep Cover | | the expert view: numbers, settings, logs and engine detail, hidden by default |

The copy keeps a light spy-noir tone where it already has one (rooms, Deep Cover, Agent Smoke the demo voice). New copy
is plain first.

## Modules

| Module | Makes | Uses |
|---|---|---|
| FrontMan | covers | voices, through DoubleAgent's service |
| DoubleAgent | RVC voices, cadence packs, the overnight queue's work | clips from YouTube, other sites yt-dlp reaches, or uploads |
| SpeakEasy | speech | voices and cadence packs, through DoubleAgent's service |
| SafeHouse | | everything: lists, renames, exports, deletes, moves, backs up |

FrontMan's one-shot asks DoubleAgent for "a good voice of X" (`doubleagent.service.resolve_voice`): a voice trained
here, else a community voice that passes a measured check against real clips of the person, else a new training.
Pause / Resume and the overnight queue are suite-level (`core/worklist.py`). Every voice gets a preview line, "Go
undercover as <name>.", rendered by SpeakEasy as the last step of whatever made or changed it (`core/voicepreview.py`).

## Code layout

```
undercover/
  VERSION                  the one place the version lives (core/about.py and the installer read it)
  config.default.toml      the shipped defaults; config.toml next to the package holds only what differs
  server.py, cli.py, relaunch.py, tray.py, addons.py, doctor.py, ytdlp.py, models.py
                           thin entry points: python -m undercover.<name>
  core/                    shared code; imports no module
    config, fsutil (crash-safe writes), timing, relocate, storage (the folder groups, moves, the allow-list),
    setup_models (PINNED: every model file, its sources and SHA-256), separate, speaker (WeSpeaker prints), readers
    (the Kokoro model), voices (install, search, staging, set aside, display names), voicepreview, engine/ (convert.py
    + the vendored rvc/), gpu (the shared separator and converter), jobs (Job, the GPU worker, SSE events, ETA),
    worklist (Pause / Resume, the overnight queue, registries), cadence/ (engines, client, worker, render), addons,
    youtube, ytdlp (self-healing), doctor, manifest, about, diagnostics, phone, qr, tailnet (away from home), themes,
    ear (thumbs), tray (the recent tray), trayicon (the system tray), gamepause, vault, platform (OS-specific calls)
  doubleagent/             project, sources, subject, trainer, voicecheck, clipsound, cadence, night (queue items),
                           improved (a retrained voice waiting beside the current one), frozen (the Improve test),
                           service (the public surface), api
  frontman/                songs, pipeline (separate -> convert -> remix), remix, fit, leadgate, oneshot, ambiguity,
                           duets (who sings which part), edits (the edit list), history, cli, api
  speakeasy/               tts (the Readers), speak (the speech job, saved speech), listenback, tags (directions in
                           the text), api
  safehouse/               library, smaller (shrink an index, FLAC a project), api
  app/                     server.py (the HTTP shell, shared routes, the request gate), doctor_api, ear_api, firstrun,
                           firstrun_api, gamepause_api, lowprofile_api, phone_api, themes_api, vault_api
  web/                     index.html (one page, one view per module), shell.js/.css, app.js/.css, look.css, jobs.js,
                           one script per module and room, pair.html, away.html, manifest.webmanifest, fonts/ (OFL,
                           bundled), themes/ (the five shipped)
  assets/                  sample/ (the CC0 sample song "Hold On"), demo-voice/agent-smoke/ (its model files are built
                           by scripts/make_demo_voice.py and pinned by manifest.json), reader-prints.json, separator/
installer/                 UnderCover.iss, build.ps1, setup.ps1, python.ps1, detect_gpu.ps1, space.ps1, installed.ps1
                           (what an update already has), keptcheck.ps1 (checks kept files), selftest.py,
                           downloads.py + downloads.tsv, pin_hashes.py, constraints.txt, requirements/, sizes.json,
                           addons.isi + sizes.isi (generated by build.ps1)
scripts/                   UnderCover.bat (the launcher), cover.bat (the CLI), check_env.py, make_demo_voice.py,
                           reader_prints.py
tests/                     smoke.py, fake_tailscale.py, installer_e2e.ps1
```

The cadence engines and Whisper run in a worker process (`python -m undercover.core.cadence.worker`): one JSON object
per line over pipes, audio through files. The server starts it on first use and stops it after `[cadence] idle_minutes`,
on `gpu.free()` and at exit. A warm-up loads it ahead of the first speech: a `speakwarm` GPU job
(`speakeasy.speak.queue_warm_up`) that loads each engine the speech packs read with and runs every pack's one-off pace
calibration, queued once the server is idle after its start (the preview backfill's rules) and again by
`POST /api/speak/warm` when the page opens SpeakEasy. It gives way at once to any other GPU work. SpeakEasy's estimate
(`speak.start_cost`) adds the start-up only when the worker is cold, and `/api/speak/estimate` returns it as `first_s`
and `first_why` so the page says it. The import stand-ins AMD's Windows build of torch needs live in the worker only.

### Adding to the suite

- Modules plug into core through registries, not imports: `core.worklist.register_kind` (a queue item kind: run and
  estimate), `register_resume`, `register_pausable`, `register_control_lookup`, and `core.jobs.register_user_error`.
  `app/server.py` imports every module at startup so these run; a new module has to be imported there too.
- Shared GPU models live in `core.gpu.MODELS`. After installing or replacing a voice, call `gpu.forget_voice(name)`.
- Cross-module needs go through `doubleagent.service`. Whatever starts a module's work lives in that module
  (`start_oneshot` is in `frontman/oneshot.py`, because core cannot import FrontMan).
- Keep the thin entry points when code moves: a running server's Restart looks for modules at their old paths.
- A module's page is a tab (`#tab-<module>`, `data-tab`) and a view in `index.html`, an entry in `MODULES` in `shell.js`,
  and a script that listens for the `uc:module` event. Its long-job kinds go in `OWNER` in `work.js`.
- A page never opens a job's event stream itself. `watchJob(id, fn)` in `jobs.js` hands every event in order, from the
  stream or, while it is quiet, from `/api/jobs` summaries (`ev.synthetic`). Smoke fails if any other script contains
  `new EventSource`.
- Every OS-specific call goes through `core/platform.py`. Elsewhere it does the portable thing or raises `NotOnThisOS`
  with a plain sentence, and `supports(feature)` lets a page hide what cannot work here. Smoke fails if another module
  makes such a call.
- Expert-only elements carry `data-deep`; `[data-pc]` marks controls a paired phone does not get.

### Dependency rules

Checked on the import graph by `tests/smoke.py`:

1. `core` imports nothing from the modules, `safehouse` or `app`.
2. `doubleagent` imports `core` only.
3. `frontman` and `speakeasy` import `core` and only `doubleagent.service` from DoubleAgent, never each other.
4. `safehouse` may import any module (it manages their files); only `app` imports `safehouse`.
5. `app` may import everything; nothing imports `app` except the `undercover/server.py` entry point.

One exception by name, not by import: `core/gpu.py` reads `sys.modules` for `undercover.speakeasy.listenback` to drop
listen-back's loaded ear before a training (nothing is imported; a listen-back that was never loaded is left alone).

### Stable things

- The data locations and `[paths]` keys (next section). Always reach a folder through `config.path(key)`.
- The route prefixes the page uses (`/api/covers`, `/api/auto`, `/api/train/*`, `/api/work/*`, `/api/speak/*`,
  `/api/library/*`, `/api/jobs`, `/api/voices`, `/api/status`, and the rest the route files list).
- The entry points: `scripts/UnderCover.bat` and the installer's launcher run `python -m undercover.server`;
  `scripts/cover.bat` runs `-m undercover.cli`; the tray is `pythonw -m undercover.tray`.
- Moving the app folder is allowed: `core/relocate.py` fixes stored absolute paths at the next start, but does nothing
  when the recorded folder still exists and holds an UnderCover checkout (a second checkout is not a move).

## Settings and folders

Settings are two files. `undercover/config.default.toml` ships with the code and is never edited on a PC. `config.toml`
in the app folder holds only what differs: what setup wrote and what the person changed. The app writes settings there
and nowhere else (`config.set_values`). `UNDERCOVER_CONFIG=<file>` points a server at another settings file; every
sandbox and test server uses that.

Files live in three groups, each one folder holding sub-folders, so every default is the app folder itself
(`core/storage.py`, SafeHouse's Storage room, the installer's folder page):

| Group | `[paths]` keys | Holds |
|---|---|---|
| your library | `uploads_dir`, `output_dir`, `voices_dir` (default `<models_dir>/voices`), `themes_dir` (default `<library>/themes`) | songs, covers and speech, voices with their cadence packs, themes |
| working files | `cache_dir` | stems, training projects, previews, caches, the staged yt-dlp pairs |
| model files | `models_dir` | everything UnderCover downloads: RVC base models, HuBERT, RMVPE, the separators, WeSpeaker, Kokoro, Chatterbox, Whisper |

Voices belong to the library, not the model files: they are the person's own work and nothing can download them again.
A move is a job: copy, check every file arrived, switch `[paths]`, rewrite stored paths, then remove the old copy. Code
that deletes or moves files takes an allow-list (`storage.ALLOWED_ROOTS`, `UNDERCOVER_TEST_ROOTS`) and refuses the
app's real folders in test mode. Writes go through `core/fsutil.py`, so a power cut leaves the old file or the new one.

## Data contracts

### Voice

`<voices_dir>/<name>/`: `model.pth`, optional `model.index` (a compact fp16 form; `core/engine/rvc/index.py` reads
faiss indexes without faiss), `meta.json`, and `preview.mp3` + `preview.json` (the "Go undercover as" clip; not part of
the model). Files are found next to `meta.json`; stored absolute paths are a hint. The folder name is the id and never
changes. Downloaded `.pth` files are loaded with `torch.load(weights_only=True)`, and a voice's `config` is held to
RVC's own shapes before its network is built (`core.voices.check_config`, at install for a community `.pth` and a
voice pack alike, and at load): the 18 values, the sizes capped, the upsampling equal to the sample rate's 10 ms hop
(32, 40 or 48 kHz), and the config agreeing with its own weights; anything else is refused with the reason.

`meta.extra` keys in use: `trained`, `project`, `epoch`, `train_minutes`, `likeness` (the voice check's score at export),
`f0_median`, `f0_range` [p5, p50, p95], `from_speech`, `person` (stored at export from the project), `person_range`,
`person_range_speech` (community voices checked against a person), `accent` when known, `demo` (Agent Smoke),
`improved_pending` / `pending_for` (an Improve candidate waiting), `tts` (the cadence pack, below).

`meta.display` is what the pages show (set in SafeHouse, `POST /api/library/voices/<id>/display`, at most 80
characters); missing means the default (`core.voices.default_display`). Every voice API returns `display`, `trained`,
`demo`, `likeness_word`, `ear` {up, down}, `waiting_beside` and `preview` through `core.voices.voice_info(v)`.
Server-written strings use `core.voices.label_of(id)`; ids stay in every data field.

Two hidden folders sit one level under the voices, so no list sees them. `voices/.staging/<name>` holds a community
voice downloaded for a one-shot's check; it joins the voices only when the check passes, and a failed or stopped check
discards it (deletes inside the staging folder only). `voices/.aside/<name>-<stamp>` holds a voice UnderCover moved out
of the way instead of deleting (a replaced voice, a candidate not kept), its `meta.json` renamed `meta.json.aside`;
SafeHouse's Storage room lists them with why and when, and deletes one only when asked twice.

### Voice pack (`.ucvoice`)

A voice as one file (1.1, `core/voicepack.py`), made and opened in SafeHouse (Library, a voice's detail: Export as a
voice pack; the Library's Import a voice pack, or a dropped `.ucvoice`). A zip of exactly these members:

- `manifest.json`: canonical JSON (sorted keys, no insignificant whitespace, UTF-8), written first;
- `weights.safetensors`: the generator's tensors, never a pickle; the `.pth`'s other entries (config, sr, f0, version,
  info) as JSON in its metadata under `rvc`;
- `index.ucindex` (optional): the compact fp16 index (`UCIDX16`), raw numbers.

The manifest: `format` "ucvoice", `format_version` 1, `min_app` "1.1.0", `app`, `created`, `replaces` (null),
`pack_id` (SHA-256 of the canonical manifest without `pack_id` and `signatures`), `publisher` {key, name, feed} and
`signatures` [] (empty until 2.0 signs), `lineage` {parent, root, generation} (null, null, 0 for a first export),
`voice` {name, display, person}, `subject` {kind: self | public_figure | private_person | fictional | synthetic, name,
consent: self | written | none | unknown, basis (1-500 characters, the exporter's own words), contact}, `provenance`
{origin: trained here | downloaded, source, installed, demo_of}, `licence` {id, url, text}, `engine` {family "rvc",
version, sr, f0, f0_range}, `test` {print (the person's WeSpeaker print, 256 numbers, only when the exporter ticks it),
publisher_scores (advisory)}, `preview` {line}, `cadence` {included: false, why}, `files` [{name, bytes, sha256}].
A cadence pack doesn't travel (it holds real clips of the person).
Import: every member must be listed with its size and SHA-256 and match; a member that can carry code (.pth, .pt,
.ckpt, .pkl, .pickle, .bin, .npy, .npz, .joblib) or isn't one of the three refuses the pack, as do a changed manifest
(its pack_id), a newer `format_version`, a subject that's missing or malformed, and a private person's voice without
their agreement (consent none or unknown; a private person also needs a contact). The weights are read with
safetensors and the voice's `model.pth` is written here from them, then checked as a community download's
(`voices.inspect_pth`, weights_only); the import goes through `.staging` (`voicepreview.import_voice`) and takes its
own name or the first free `<name>-2`, never over a voice. The imported voice's meta keeps the pack in `extra.pack`
{pack_id, created, lineage, subject, licence, publisher, origin, source}. The voice print reaches core through
`voicepack.PRINT` (DoubleAgent's `service.person_print`, set by the app): its project's confirmed reference speaker.

### Cadence pack

A voice "has cadence" when `<voice>/tts/ref.wav` exists and `extra.tts` describes it: `engine`, `ref`, `ref_text`,
`clip`, `project`, `cfg_weight`, `exaggeration`, `tempo` (1.0; time-stretching was heard as slurred), `gap_s` /
`para_gap_s` (the person's own gaps), `data_minutes`, `scores` (likeness, wer, cadence_gap, rate, score, sound,
score_voice, the pace and pitch readings, heard), `pitch_nudge` (semitones), `speak_hz` / `speak_hz_from` (the median
pitch of the reference clip, measured with RMVPE when the pack is made, or once on its first reading for an older pack,
which is then marked `speak_hz_later`; null when it can't be measured), `person` (their measured cadence), `before`,
`made`, `v`, `verdicts`, optional `engine_pin`. `tts/cadence.json` keeps every candidate tried. An older pack's numeric
`pitch_nudge` was measured against the old target (`f0_median`), so once it aims at `speak_hz` the nudge is dropped,
unless the user chose that pack by ear (`cadence.reading_nudge`).

A voice's cadence never changes without the person's ear once it has one. A better pack found later waits in
`tts/pending/`; both packs read the same line (`sample.wav` + `sample.json` beside each) and the person keeps one.
Keeping the pending one moves the live pack to `tts/previous/`; keeping the current one moves the pending pack to
`tts/older/<stamp>-turned-down/`. Packs are moved with `core.storage.move_within`, never deleted by this code. Routes:
`GET /api/train/voices/<v>/cadence`, `POST .../cadence/ab`, `GET .../cadence/sample/current|pending`,
`POST .../cadence/verdict {keep, why}`. Packs are engine-agnostic within the Chatterbox family (`engines.FAMILY`), and
`engine_pin` keeps a voice on one engine by ear, never switched within a speech.

### Training project

`<cache_dir>/training/<name>/`: `project.json` (sources, phrases with reason, score, sung, noise, floor; a source's
`kind` "scene" / "talk" and `site`; the reference speaker and its `hiss_limit`; train state), `embeddings.npz`,
`raw/<src>.wav`, `clips/<id>.flac` (one phrase of separated, de-reverbed voice), `transcripts.json` ({"model", "clips":
{id: text}}), `cadence/` (stretches of the person's real talk and `person.json`: pace, pitch and gaps, also per source),
`train/` (`data.pt`, `resume.pt` while unfinished, `e<N>.pth` saves, previews, `history.jsonl`). The clips are
speaker-verified speech or singing of one person and are also the material for cadence. An Improve adds
`<voices_dir>/<name>/test/`, the voice's frozen test (below).

### Speech

`output/speech/`: `<name>.wav` (+ `.mp3`) and `<id>.json` (id: 10 hex characters; title, text, voice, reader,
reader_auto, speed, pitch, created, duration, sr, seconds, job, mode "cadence" or "reader", engine, pace, finished, kept,
kept_at). While a job runs, each sentence is written to `cache/previews/speech_<job>/s0000.wav ...` so the page plays
the first while the rest are made.

### Cover history

`output/history.jsonl`, one JSON object per finished cover (`frontman/history.py`): `id` (permanent, 10 hex
characters; every cover route uses it), `output`, `song`, `voice`, `title` (+ `title_set` after a rename), `stems`,
`converted_vocal`, `converted_backing`, `notes`, `seconds`, `options`, `created`, `kept`, `kept_at`. `history.record`
is the one place a finished cover is added. A duet's `options` carry `duet_voice`, `duet_way`, `duet_octave`,
`duet_backing` and `duet_plan` (`[[start_s, end_s, "A" | "B"], ...]` from 0 to the song's end), so Make again sings it
the same way. The file is parsed once per change (kept by its size and mtime under the history lock, dropped by every
write, so another process's write is seen too); readers get copies of the rows, and the per-cover "is its file there"
check still runs on every read. A hash-named song's title (its sidecar JSON) is remembered by the sidecar's mtime.

### Recent tray

Each module (covers, speech) has a recent tray: its latest 15 finished tracks, newest first, kept or not. "Send to
SafeHouse" keeps a track for good. When a track finishes successfully and the tray would hold 16, the one that falls out
leaves: kept, nothing happens; unkept, its files and history line are deleted through `core.storage.remove_files` and
logged in `logs/server.log`. At most one track leaves per finish, never a kept one, only after a successful record, and
a failing roll-off removes nothing and never fails the cover or speech. `/api/history` and `/api/speak/recent` return
the tray only; the library lists everything. Each property is proved in `tests/smoke.py`.

### Jobs

`core.jobs.Job(kind, title=None)` with SSE events at `/api/jobs/<id>/events`. `GET /api/jobs` lists jobs running or
finished in the last hour as `Job.snapshot()`: `id, kind, state, title, project, created, ended, pausing, stage,
message, events, progress`, and when known `epoch, epochs, eta, total_left, stage_left, index, of, elapsed`, `retry`
(`POST /api/jobs/<id>/retry` replays a failed cover, speech, one-shot or install), a one-shot's `plan`, a finished job's
`final`. Kinds registered with `register_pausable` pause (a clean stop plus a resume spec); covers and speech never
pause. Every progress event carries `total_left` in seconds (`jobs.STAGES`, `cache/eta.json`). A heartbeat
(`logs/heartbeat*.jsonl`) records each running job's resume spec for the Doctor; `jobs.START_GATES` lets the game pause
ask before a queued GPU job starts.

### Overnight queue

`core/worklist.py` runs items one after another when the GPU is free, from a start time if set, and only items whose
estimate finishes before "finish by". A paused item is resumed first when the queue next starts; an item that does not
fit lets a shorter one go ahead. DoubleAgent's kinds are `improve`, `new`, `cadence` (follows its voice's item) and
`newcad`. An Improve is estimated on the new voice's estimator (`night._estimate_improve`): the clean minutes it
should add (about a quarter of the new raw minutes) at this PC's measured preparation rate for the kind of person, a
refine's fixed budget or a retrain's budget run past its plan as trainings here do, then the export and the frozen test.
An Improve's retrained voice waits as `<voice>-next` beside the current one (`doubleagent/improved.py`):
`GET /api/train/voices/<v>/improved`, `POST .../improved/ab`, `POST .../improved/keep {keep: "new"|"current"}`,
`POST .../improved/undo`. The voice not kept is set aside, never deleted.

## Why these engines

The numbers below were measured on an RX 7900 XTX (24 GB, ROCm on Windows, no Triton). Speed is "x realtime": seconds of
audio made per second of compute. Likeness is the cosine between WeSpeaker voice prints; the same speaker scores above
about 0.5, different speakers below 0.3. An engine ships only when its code and weights are OSI-licensed; the rest are
add-ons the person installs and accepts.

### Speech: Chatterbox-Turbo, with Chatterbox as the fallback

RVC copies timbre, not delivery: a stock Reader read a slow speaker faster than a fast one. So SpeakEasy reads with a
zero-shot cloning engine, given one 7-11 s clip of the person and its transcript, and puts the RVC voice on top.
Candidates were run on the same ten sentences and scored on likeness, Whisper word error rate, and the person's measured
cadence (speaking and articulation rate, pauses, pitch range, spread, movement and declination).

| Engine | Code / weights | Likeness with RVC | Cadence | Speed | VRAM |
|---|---|---|---|---|---|
| Kokoro + RVC (the Readers) | Apache-2.0 / Apache-2.0 | 0.75-0.82 | reads everyone the same way | 2.5-3.9x | 2 GB |
| Chatterbox, cfg_weight 0.3, + RVC | MIT / MIT | 0.74-0.83 | closest to each person; still 10-15% fast | 0.5-0.6x | 3.6 + 2 GB |
| F5-TTS + RVC | MIT / CC-BY-NC | 0.73-0.80 | right direction; flattens a deep voice | 1.9-2.9x | 0.9 + 2 GB |
| XTTS-v2 + RVC | MPL-2.0 / CPML | 0.74-0.82 | fast for everyone | 0.8x | 2.1 + 2 GB |
| CosyVoice 3 | Apache-2.0 / Apache-2.0 | 0.75 | close averages; 9.1 GB, needed a decode fix | 0.45x | 3.7 GB |

Chatterbox at `cfg_weight` 0.3 came closest to both test speakers' delivery and is MIT for code and weights, so it
became the cadence engine. RVC on top brings every engine to the same likeness and changes cadence by at most 0.03, so
the voice conversion stays the last step whichever engine reads. A short fine-tune of F5-TTS raised likeness but moved
delivery away from the person, so packs are zero-shot, not trained.

Chatterbox blurred some words inside a sentence ("evening" heard as "eving") and varied from take to take.
Chatterbox-Turbo, the same package family with a one-step decoder, was tested through the same path and chosen blind:
cleaner on hard words, about twice as fast, the same cadence and memory (`ENGINES["chatterbox-turbo"]`: rtf 2.5 for the
engine alone, 3.6 GB, 2.8 GB of files). It is the default (`[cadence] engine`). Turbo ignores `cfg_weight` and `exaggeration`, copies the pace
of its reference clip, and reads lower than the person, so `render` moves its median pitch onto where the person talks:
the pack's `speak_hz` (`cadence.speech_target`, held inside the voice's own `f0_range`), or the voice's `f0_median`
when there is none. The training median can be the wrong register for a calm reading: a voice trained on rallies sits
at its shouting pitch (180 Hz against 143 in its reading clip), and the lower one was chosen blind. Songs keep
`f0_median`. Speeding up or slowing a reading, through the reference clip or before RVC, was heard as slurred both
times it was tried; that code is inert. An install with only
Chatterbox's files keeps reading with it until Turbo's arrive. Every Chatterbox output carries Resemble's inaudible
watermark. Non-commercial engines (F5-TTS, XTTS-v2) are add-ons.

XTTS-v2 (1.1) is the `xtts` row of `ENGINES` and the `xtts` add-on: coqui-tts 0.27.5 and its own needs, installed
`--no-deps` from `installer/requirements/addon-xtts.txt` (`pin_hashes.py --addons` writes it from PyPI's hashes), and three
pinned model files (`model.pth`, `config.json`, `vocab.json`, 1.9 GB) in `models/tts/xtts/`. Like Chatterbox it reads from
the clip alone; it is not in the Chatterbox family, so a pack made with it reads with it, and a Chatterbox pack keeps
reading with Chatterbox. The worker (`XTTS` in `cadence/worker.py`) loads the files directly (no Coqui download or
terms prompt: the licence is accepted on the add-on page) with the model's own settings, and puts in three stand-ins for
English reading on torch 2.9 (a Korean romaniser its tokenizer imports, a transformers helper gone in 5.x, coqui's
torchcodec check; audio goes through soundfile). Its licence text is the copy in the model's repo at the pinned revision
(Coqui's own site is gone).

### The Readers: Kokoro

Kokoro (82M, Apache-2.0 code and weights, `kokoro-onnx` on the CPU) is the quick path for a voice without a cadence
pack and the timing reference for listen-back. It runs at 5.8x realtime in fp32 on the CPU (the int8 model is 4.5x
slower there). The automatic Reader is picked by sex, accent and pitch: the voice print's nearest labelled Readers vote
on sex (`core/speaker.sex_vote`, `assets/reader-prints.json`), the accent is American unless the voice is known British,
then the closest measured speaking pitch among Readers graded good enough to pick.

### Voice conversion: RVC v2 with RMVPE

Voices are RVC v2 pitch-guided models (`core/engine/convert.py`, a port of RVC's inference onto the vendored modules in
`core/engine/rvc/`, MIT). HuBERT gives content features, RMVPE the pitch, the optional `.index` pulls features toward
the voice's training set (`index_ratio` 0.75). A backing stem can hold a stack of voices, and RMVPE's strongest line then
changes every few frames: `f0fix.hold_line` keeps one line through every sung run that has a second one (a part that rests
for a moment is a rest, not a hop to another part), holds its place through quiet, and leaves runs of one voice as they are
(`Settings.one_line`, on every backing take, never on a lead). Inside one sung note, where two parts cross and the track would move from one to the other mid-word, `f0fix.hold_parts` keeps the part it was on until the note ends or that part stops singing; a step of the tune with no second part under it is left as sung. A cover's stems are decoded once: `load_16k` keeps the last four vocals as read-only 16 kHz arrays, keyed by resolved path, size and mtime (a rewritten stem is read again). Songs are converted in 60 s chunks cut near quiet points:
60 s halves VRAM against 120 s (3.8 vs 7.6 GB) at the same warm speed. Training (`doubleagent/trainer.py`) follows upstream RVC's recipe,
fine-tuning the official v2 base models in bf16, with a few held-out phrases for a validation loss and previews. The
budget scales with the data: 450 steps per kept minute, at least 8000, continued while the held-out loss falls, up to
twice the plan. A fixed budget had given a 130-minute retrain 8 passes over its data and made the voice worse.

### Voice prints: WeSpeaker

Speaker identity is a WeSpeaker ResNet34-LM embedding (VoxCeleb, 256-d, ONNX on the CPU, CC-BY-4.0). It keeps only
the target's phrases in a project (`match_threshold` 0.45), finds the reference speaker among the voices in the clips,
scores a voice's likeness, and gives the Readers' sex vote. Its limits are known: it says who, not how natural; it
saturates inside a person (every candidate of one person scores 0.75-0.85); and it barely hears a small pitch shift or a
muffled band.

### Separation: Mel-Band Roformer

`audio-separator` runs three Roformer models (`[separation]`). The vocal split is Mel-Band Roformer Kim FT (unwa): in a
side-by-side it left about 10 dB less instrument bleed in the vocal than BS-Roformer 1296 and ran about 2.5x faster. A
de-reverb pass (anvuew) runs by default, because reverb tails smear HuBERT features and pitch; the remix puts matched
space back. A karaoke model (aufr33 / viperx) splits lead from backing when asked. Quality presets set the overlap:
fast 12.3x realtime, high 6.6x (the default), max 3.4x. Before conversion, `frontman/leadgate.py` sends a pitched
instrument the split called "vocal" back into the instrumental. Lines the karaoke split left in the backing while the
lead is silent are handled by `chorus`: `convert` moves them into the conversion, `top` moves only their top voice, and
`keep` (the default) leaves them in the backing. With `keep`, what's left of the lead under those lines is silent (with
fades), so no voice sings the split's leftovers, and the note says who sings the lines: the original singers, or the
backing's voice when the backing is converted. Under `top`, a stretch where the backing's re-split finds no top voice
(`leadgate.top_missing`: its top 30 dB or more under the backing, as in Toxic's stacked choruses) is kept the same way.

### Listen-back: Whisper

Transcripts and the listen-back guard use openai-whisper large-v3-turbo on the GPU (MIT): 13-17x realtime, 4.6 GB,
0.4% word error on known text, with a short punctuated starting prompt so the text keeps its punctuation. faster-whisper
was ruled out: no ROCm build on Windows, and its OpenMP runtime cannot share a process with torch. After the engine
reads a sentence, listen-back (`speakeasy/listenback.py`) hears it back: a word missing, read with low confidence or
added inside the line gets the sentence read again with a new seed, and the least bad take is kept (`cost`, word by
word: ten extra words weigh ten, not one). Words past the text's last word are a stray ending (1.1: an engine repeated
its last phrase on every seed, so no re-read could mend it): `trim_ending` cuts the take TRAIL_S after that word,
never into the next word heard, with a short fade, and no re-read is spent on it.

A hang is caught the same way (`listenback.hangs`, heard in the take's audio): two words with no pause marked between
them, whose vowels are 0.2 s or more apart with one silence of 0.1 s or more in between. That's a held closure and a
restart. A consonant cluster spans as long, with short closures between noise. Each reading's first seed is
`speak.SEED` plus its sentence's index, kept with the speech, so the same text reads the same. A thumbs-down for its
pace or its pauses reads it again with a fresh seed (`reseed` on `/api/speak`).

Its word stretch and comma lengthening are off by
default: blind, the re-reads fixed the word and the other two did not. Whisper is language-model biased (it hears
"evening" in "eving"), so it catches gross slips; the ear is the judge.

### Directions in the text (tier 1)

SpeakEasy's text takes directions in square brackets (`speakeasy/tags.py`). A direction applies to the sentence it
sits in, and none is ever read out:
- `[pause]`, or `[pause 2s]` up to 5 s: silence at that spot. The sentence is read as two pieces, the first ending in
  that `hold`.
- `[louder]` / `[softer]`: ±4 dB, by the level the sentence is set to.
- `[higher]` / `[lower]`: ±2 semitones on the reading's pitch, held in the voice's `f0_range`
  (`cadence.held_pitch`).

One step each; they don't stack. These are done around the engine, in both reading paths.

Chatterbox-Turbo has tags of its own: sounds ([laugh], [chuckle], [sigh], [cough], [gasp], [groan], [sniff],
[shush], [clear throat]) and styles ([whispering], [happy], [angry]). The parser knows them, with friendly short forms
(`[whisper]` → `[whispering]`). None goes to the engine yet: in the blind pairs, [laugh] was heard but didn't sound
real, and the styles couldn't be told from none. They're taken out and named under the result as not used yet. Once
the ear passes one, it goes in `tags.ENGINE_ON`; for a cadence voice it's then passed through in Turbo's text (read
from its tokenizer's `added_tokens.json`).

An unknown tag is read as nothing and named once under the result. An editor's bracket stays text: [sic], a
capitalised [Name], a number. Pieces keep their directions: `join_short` never joins across a written pause or a
change of direction. Listen-back hears only the words: a sound tag's noise is no extra word, and no stray ending to
cut.

Pace and energy aren't directions: a stretched reading was heard as slurred, twice.

### How a cadence pack is picked

"Train cadence" takes minutes, not hours: transcribe the project's clips, rebuild stretches of the person's talk and
measure how they speak, try `CANDIDATES` = 5 reference clips (different sources first), read the same test sentences
with each through the engine and the voice, and score each pack. The score is likeness minus weighted terms:

- the cadence gap (`W_GAP` 0.25) and word error (`W_WER` 1.0) of the readings against the person's measured cadence;
- the clip's sound (`W_SOUND` 1.0; `doubleagent/clipsound.py`), because a cloning engine copies the clip's room, mic and
  band along with the person, and likeness cannot hear that. Five plain measures from the frames where the person
  talks: energy above 4 kHz relative to 0.3-4 kHz (muffled below -22 dB), the spectrum's 50 dB edge (bandlimited below 9
  kHz), the per-band quiet floor (hiss above -30 dB), the decay after loud frames (a reverberant room above -240 dB/s)
  and the share of clipped samples (above 2e-4), each charged past its threshold, capped at 0.15 each and 0.30 in all.
  It ranked 96 of 96 degraded copies below their originals and agreed with the ear on the first blind pair it changed;
- pitch, corrected rather than penalised. A pack's pitch is steady across readings (spread 0.2-0.8 st) while the engine
  drifts by semitones through RVC's octave-only correction, so each candidate gets a `pitch_nudge` (0.1 st steps,
  nothing within 0.5 st) that puts its readings on the person's median, and packs are compared as heard (`pitch_pen`).
  Pitch inside the range of the person's own sources is theirs; only pitch past that range is corrected;
- pace is measured and kept but not scored: it moves about 10% between readings, and as a penalty it would have
  overturned a blind verdict.

A new pack replaces nothing by itself. For a voice that already speaks, it is offered only when it beats the live pack
by more than `KEEP_MARGIN` = 0.02 (the best of five noisy scores is biased upward; a 0.013 margin was a tie by ear), and
then waits in `tts/pending/` for the ear.

### Duets by sections

A cover can be shared by two voices taking turns (`frontman/duets.py`). The lead is converted once in each voice,
inside `Converter.shared_f0`, so the pitch track is read once. Both voices sing in one key for the song, each at its
own best octave: `duets.joint_fit` picks the key with the lowest `fit.pick_fit` cost, each voice's cost read on the
melody its sections hold and weighed by its share of the sung melody (so the fit runs after the plan). When one of
them sits `FAR_ST` (3) semitones or more from where they'd sing alone, the fit's note says so. The two takes are then
stitched by the plan.

- **Lines** are the lead's own: stretches of singing between silences of 100 ms or more, 35 dB under its loud level
  (`duets.phrases`). A line under 1.5 s joins the next across a gap under 0.6 s.
- **Cuts** fall in the middle of those silences, or at the lead's drop between two lines sung without one (below), with a
  40 ms equal-power crossfade there, so no voice changes inside a word. A gap in Whisper's word times is never a cut:
  they stop early on a held note.
- **A chorus** is a line most of whose words are inside a run of 6 that comes back 15 or more words later in Whisper's
  words on the whole vocal (lead and backing mixed, through the speech worker). A run of 2 or more such lines is a
  chorus block.
- **A pre-chorus** that repeats reads as chorus at first. `duets.mark_pre_chorus` gives it back to the verse: the lines
  opening a chorus block whose words come back in fewer chorus blocks than the chorus's own lines. A pre-chorus sung
  before every chorus can't be told from the chorus by repeats, and stays in it.
- **Between sentences** (`duets.sentence_edges`): a chorus's edge in a silence under `SENTENCE_GAP_S` (0.5 s) has to prove
  itself by the words. The chorus starts where the words that come back as far as its own lines start, and ends earlier
  when its last words don't come back, each at the first line start from there: Whisper's sentence end
  (`.?!:;`, not a comma) before the word, or its capital on it, unless that's a name (`duets._names`: capitals in a row
  that come back). When that line start has no silence, the row splits at the lead's own drop (`DIP_DB`, 15 dB or more
  down) within `DIP_BEFORE_S`/`DIP_AFTER_S` of Whisper's start of the word. A short drop that goes nearly silent
  (under `MIN_BREAK_S`, `STOP_DB` down) is a word's own consonant, and is passed over. With no such place, the edge stays
  where it was.
- **Ways** (`duets.WAYS`): `turns` puts one singer on the verses and the other on the choruses, so the voice changes at
  every verse-to-chorus boundary; `lines` alternates lines. One singer at a time: two converts of the same line sounded fake. `DUET_DEFAULT` is the
  one place the default is set.
- **Who takes what** (`duets.turns_by_register`, `by_register`): with both voices' ranges measured, each verse and
  chorus is priced for each singer (`fit.pick_fit` on its melody, at the two voices' key on the whole song and each
  one's octave; with the fit off, at the cover's own key and octaves), and the way round that suits the song better
  wins, each section's price gap capped at `REGISTER_MARGIN` (0.04). Without them, the first voice takes the verses. A
  section leaves its side only when the other's price is `REGISTER_MARGIN` or more below. Each singer keeps `MIN_SHARE`
  (15%) of the sung melody or more: first the exceptions go, and past that the two trade (verse and chorus, then the
  other way round). The plan line says who takes what ("B takes the verses and A the choruses, by range"). A plan from
  Make again is kept as it is.
- **The backing** (`duet_backing`, `duets.BACKINGS`): by the singer on the lead (`singers`, `BACKING_DEFAULT`), by the
  one who isn't (`other`, `duets.other_singer`: the same changes, each part in the other voice, so the backing changes
  hands with Turns), or the original. Each singer takes the backing at their own octave for the
  song, in either case, so `other` costs no third conversion. A backing voice isn't used in a duet. The one-shot keeps
  the original when the backing is stacked harmony (`oneshot.chordal_backing`); nothing is converted when there's
  hardly any backing (`pipeline._duet_backing`, under `BACKING_QUIET_LUFS`). When the song's lead and backing are
  already split in the cache, `/api/estimate` says so before Convert (`backing_quiet`, from `pipeline.backing_quiet`:
  the same line on the raw split, which the lead gate only makes quieter), and the duet controls say it. A song
  never split says nothing: nothing is separated just to know. The
  backing has its own plan (`duets.backing_plan`): its phrases are its singing between its own silences, each is sung
  whole by whoever sings most of the lead under it, and its voice changes only inside its own silence, at the point
  nearest the lead's change.
- **Too few words** (under 20 heard): there's no chorus to find, and the voices take every two lines in turn.
- **On the page** (`renderDuet`, `renderDuetPlan` in `app.js`): the second singer is picked from the same voice rows, and
  the Convert bar says the backing in words. The strip is the plan of the newest cover of this song by these two, from the
  history, and the sentence under it is that cover's note. Before the first cover the page shows a general sentence and
  no strip, because only the engine can find a plan. A way that isn't ready is hidden (`lines`) or shut (I'll choose).

A song sung as a duet already (`separate.lead_missing`: the split finds no lead line) is sung by one voice, and the
notes say why; a duet's two singers take turns on its whole vocal instead, backing and all. The cover's
options take `duet_voice` (an installed voice that isn't the lead), `duet_way`, `duet_backing`, `duet_octave` (-2 to 2) and a plan in
`duets.check_plan`'s shape, all checked in `frontman/api._duet_options`. The one-shot reads "<song> as A and B" (also
"with", "&", "a duet of ...") when the second singer is a voice here (`oneshot.split_duet`).

### Edit list

One list per cover, `cache/converted/edits/<cover id>/edit.json`, its snippets beside it (`frontman/edits.py`). A duet's is written
when the cover is recorded, keeping the words it heard. Any other cover's is made the first time it's opened (`POST
/api/history/<id>/edits/open`, about 18 s to hear the words). The history line only carries `edits: n`, how many the list holds,
redo included, and a kept edit's `edited_from: <id>`.

- **An edit** asks for a `singer` or an `octave` on the `lead` or the `backing`, over a stretch given by times, by words or by a
  section. The page and an AI send the same request (`by`, and `note`, plain text, at most 200 characters). `key` is reserved and
  refused. Later edits paint over earlier ones.
- **`check_edit` is the one gate**, and it answers before any GPU: every field, the stretch resolved, its edges snapped, and the plan it
  leaves through `check_plan`. A word index counts only against `words_hash`. An edge goes to the nearest silence (the backing's
  own, for the backing), or for the lead to a dip when there's no silence within 2 s, and the edit says what it moved past. With no place
  to land it's refused, naming the word.
- **An octave edit is priced before it's sung** (`edits.price`): `fit.pick_fit` at the cover's key on the stretch's notes, at the
  edit's octave against the singer's own. When the cost rises by `POOR_COST` (0.25) or more, the answer says it's a poor fit and
  why (the share of notes out of the voice's range, or how far from where it's comfortable). It's said, never refused. The pitch
  tracks are read once and kept with the list (`pitch.npz`: the lead's, and the backing's one line held; read back as data, no
  pickles). They're read when the cover is recorded, when the list is first opened, or with the next edit of an older list, and
  Keep carries them. `POST .../edits` answers `{job, edit, fit}`; `POST .../edits/price` runs the gate and the price and sings
  nothing, for the page to say it before the edit is offered.
- **`compile_list`** is pure: plans and edits as far as the cursor -> parts `[start, end, who, octave, src, base]`. `plan_of` makes
  the lead a plan, so `duet_plan` stays true. A stretch given back to what the base has there needs no snippet.
- **Rendering** converts the stretch with 0.5 s of context either side, through the cover's own settings, lays it over the base takes
  with a voice change's crossfade, and mixes the **whole song** (`remix`): a stretch mixed alone comes out of balance. The cover isn't
  touched. **Undo and redo** move the cursor and mix again, with no conversion. A new edit cuts off what lay past the cursor.
- **Keep** writes a new cover beside the old one, with its list and its own copy of the base takes, and doesn't roll the old one off
  the tray. A version is kept once. A cover with an unkept list rolls off like any other, its list with it. Make again takes a
  kept cover's singers from `duet_plan` and doesn't carry its octave edits yet.
- **Stored data:** `load()` checks every field of `edit.json` and answers "no list" for anything off, a file over 4 MB included. File
  names come from an edit's id and nothing else. The list goes with its cover through `storage.remove_files`, and Storage's "Temporary
  conversion files" leaves the base takes of a cover with edits, and offers nothing with no history to read. Clean up offers a folder
  with no line in the history, unticked, and nothing in that case either: the library may be on a drive that's away.
- **Routes** are in `frontman/api.py`, one job at a time per cover in the GPU queue, PC only (`app/phone_api`). Page: not yet.

### The one-shot's checks and gates

A community voice found for a one-shot is checked before it is used (`doubleagent/voicecheck.py`): phrases spoken by
other people in clips of the target (interviewers, hosts; the bundled Readers when the clips have none) are converted
with the model, shifted by whole octaves toward the target's pitch, and their voice prints are compared with the
target's real voice. The other speakers' own similarity is the baseline. Verdicts: good from 0.45, weak from 0.30, bad
below, with a bootstrap lower bound because few phrases leave a wide interval. A voice that fails never joins the
voices. The subject of a request is read from what a search finds (`doubleagent/subject.py`): a real person gets
interviews and speeches (recent first; never talk about them), a singer their songs with the vocals separated, a
fictional character its scenes and voice lines, never cast interviews or podcasts about it. An ambiguous bare title
asks which song before anything starts (`frontman/ambiguity.py`); with no answer it runs with the first and says so.

### Improve: offered only when it measures better on the person's own lines

Several retrains of well-fed voices in a row were rejected by ear, and the likeness score disagreed with the ear in
both directions (one candidate measured better and lost; one measured the same and won). So `doubleagent/frozen.py`
builds each voice one frozen test when it is first improved (`<voice>/test/`): a donor pool of other people's clean
clips of the same sex, and 12 of the person's own transcribed lines read once through their cadence pack, the raw takes
frozen so every candidate converts the same reading. Scores are a vector per input (likeness, harmonicity, spectral
contrast, noise, spectral distance, content survival), compared as paired differences with a bootstrap interval. A
candidate is offered only when, on the person's own lines, likeness is not worse by more than the seed band (0.015),
harmonicity is up by at least 0.05 and spectral contrast by at least 0.35, each outside its interval, with donor
likeness as a guard. That bar reproduced every blind verdict on record; a false offer costs more than a missed
improvement, so these numbers are tightened, never loosened. Otherwise Improve reports "tried, no improvement: yours is
kept" and the candidate parks in Set aside.

A singer has no speech of their own to read back, so their frozen test (1.1) holds up to 12 of their own sung
phrases instead (3-9 s, three per song at most), cut from their songs and separated from the music. From the moment
the test is made they're held out of every training (`Phrase.frozen`): a candidate judged on them never learned
them (the approved voice was trained before, so it has: a lean against offering, the safe side). Each voice converts
them at their own key and is scored on the same measures plus two of singing's own against the source melody: the
median cents it strays from it (`pitch_err`) and the share of voiced frames that stay voiced (`voicing`). The bar on
them is the speech bar's terms with a pitch guard (no more than 10 cents further off the melody). It is provisional,
and a verdict judged on singing says so: no blind verdict on a singer's Improve exists yet to fit it to. The
Improve A/B plays two of the frozen sung phrases, each in its own key.

Before anything trains, a source gate (`frozen.GATE`) admits a source only if its kept phrases sit near the approved
voice (mean cosine to the reference at least 0.62, median phrase score at least 0.50), in its register (median pitch
within 5 semitones of the voice's own) and the pipeline kept more than a tenth of it; left-out sources are reported
with why. A well-fed voice (8 minutes or more, likeness 0.6 or more, new data under 1.5x the old) is refined from its
own weights (lr 2e-5, about 2000 steps for 10-20 minutes of new data); a thin voice or a big data jump is retrained from
scratch with the scaled budget, because refine learns little from a fivefold jump. Whatever is offered, the person
listens to two of the test's own lines through both voices, blind, and keeps one.

## UI

### Shell

One page, one sticky top bar: the UnderCover wordmark (four bars in the module colours), the four modules as tabs with
an icon each, the training chip and the Deep Cover glasses. On a phone (under 680 px) the modules become a bottom tab
bar. Shared tokens come from `app.css`; each module sets `--accent` only, through `body[data-module]`: FrontMan amber,
DoubleAgent violet, SpeakEasy blue, SafeHouse grey. The look (`look.css`, "Case File") tells surfaces apart by tone
rather than borders. Fonts are bundled in `web/fonts` (OFL); no remote fonts or scripts. The pages carry a
Content-Security-Policy: own origin only, inline scripts by hash.

A running strip sits under the top bar on every module: a 2 px line of light in the owning module's colour, the job in
plain words (what it does and why), a realistic time left ("about 3 min left"), Pause / Resume / Queue, and "+N more"
into the queue drawer. Every waiting state says what it waits for (the GPU, a queue, a pause, a game); nothing looks
hung. A tab left open across an update compares `/api/status`'s `build` with the one it loaded and offers a reload
(`#uc-updated`); `whatsnew.js` shows the newest note from `whatsnew.json` once.

The First assignment (1.1) is a spotlight tour on a fresh install's first visit to FrontMan (`firstrun.js`): the page dims, the
control in question is cut out and lit, one card sits beside it in the dark (never over it). Four steps: the one-shot line
(with a one-press "Hold On as Agent Smoke", the sample song in the demo voice); listen and Send to SafeHouse; the SafeHouse tab,
then the kept cover in the Library; DoubleAgent's "Who should it sound like?". Doing the thing moves it on; Next only where
there's nothing to do. While the first cover is made the page stays dark: the app's own progress (the running strip, the
one-shot's steps) is lit but can't be pressed, and a panel says what the job is doing in its own words with a tip every ten
seconds; a failed cover offers Try again (the job's retry). When the job's time left is over three minutes (`TOUR_LONG`), the
panel offers "Leave it running and look around": the veil lifts and the job goes on. When it ends, a small "Your first cover
is ready" card in the corner (or "didn't finish") brings the tour back at step 2 on one press, not a jump. Skip asks once and ends it for good; Settings has Replay. The
page reads `/api/status`'s `tour` {step, skipped, done} (an existing install has it done) and posts `POST /api/firstrun`
{step | skip | done | replay} (`app/firstrun.py`, in `tutorial.json` beside the config). `first_run` on the status is the
models download's flag, not the tour's.

Low profile (1.0.2) sits in the top bar beside the glasses: one click keeps UnderCover to half the graphics card and
working in bursts, so a game gets the card most of the time; on, the mark lights with a small ½, and the strip reads
"Working at a low profile". The numbers are Deep Cover's, behind a "▾" on the control (no Settings row): the memory
limit in whole GB (the card's size as the top), the share of the time it works, and "on by itself for games". The
page reads `/api/status`'s `low_profile` {on, limit_gb, burst (a 0–1 share), auto} and `vram_gb` for the top of the
slider, and changes it with `POST /api/lowprofile` {on?, limit_gb?, burst?, auto?}; without `low_profile` in the
status the control stays hidden. The game pause's question offers "Low profile" first (`/api/gamepause` answer `low`)
when the control is there. The tray menu carries the same switch (`core/trayicon.py`).

### The modules

FrontMan: the one-shot line as the page ("Pink Pony Club as Plankton"), a plan card in three plain lines before anything
starts, "Set it up yourself" (song and voice side by side, each with its search first), the result, the recent tray
(the newest 5, Show more for all 15). A weak existing voice may
make the cover, labelled honestly, with training offered now or tonight.

DoubleAgent: voice cards (likeness as a word: great / good / fair / poor; speak or sing; the preview button; Listen and
choose for a waiting cadence or Improve; on a downloaded voice, Train my own with Now or Tonight on the card, Speaking or
singing taken from the voice's name when it says and asked when it doesn't), filter chips above the cards (likeness, can
speak, trained here or downloaded; they combine, All clears them, the browser remembers them), the overnight queue with its morning report in words ("better", "about the
same", "not better: yours is kept"), the four-step new-voice wizard.

SpeakEasy: a text box with a time estimate, a voice picker grouped "Trained in DoubleAgent" / "Downloaded" with each
voice's cadence status, Pace (Slower, Natural or Faster, made at generation, never time-stretched), "Now speaking"
sentence by sentence while the rest are made, WAV / MP3 when finished, Recent (the newest 5, Show more for all 15).

SafeHouse: a strip of rooms (`#safehouse/<room>`; on a phone all six show, three by two, the icon over the name): Library
(Kept / Recent views, counting the kind that's chosen), Running (now, tonight with its start time once a job waits, recent,
last night), Storage (usage in three groups, Clean up, where your files live and moving them, Make smaller, Set aside, the
Vault), Add-ons, System (the Doctor, GPU and models, the YouTube downloader, What's new, Restart, Quit, the diagnostics
file), Settings (Look, Deep Cover, themes, Phone access, away from home, the game pause). The Library's summary
(`/api/library/summary`) is kept for up to 15 s while the paths setting, the history file and the counted folders'
mtimes stay the same, and dropped by every SafeHouse route that writes; its sizes come from one walk of the storage
groups (`storage.where(folders)`). `/api/status`'s stale-code stamp is re-walked at most every 5 s.

### Deep Cover

The default view shows only what a person would intuitively do: pick, press, listen, keep, train a voice, make speech.
Everything else is Deep Cover: advanced settings, scores and estimate breakdowns, engine names and times, storage
numbers, logs, ids, folder links. Elements carry `data-deep`; `body:not(.deep) [data-deep]` hides them; the switch is in
the top bar and remembered. Nothing is removed, only hidden. New screens are designed basic-first, and the engine's
judgement is fixed before a setting is added.

### Themes

A theme is a grading of the look plus a type change, tokens only (`core/themes.py`, `TOKENS`; the contract is
[THEMES.md](THEMES.md)), so a shared theme cannot break a page or hide anything, and nothing in a theme executes. Five
ship in `web/themes` (casefile, carbon, dossier, gaslight, midnight); the person's own live in `themes_dir`. One check
runs at import, at save and every time a library theme is served; import and export is one `.uctheme` zip of at most
4 MB. Theming is Deep Cover.

### Phone access and away from home

Off by default. On, the server also listens on the PC's private home-network address, and every request that does not
come from this PC must carry a paired device's token (`Handler._gate`): our own Host, a same-origin Origin on anything
but GET, an HttpOnly SameSite=Strict cookie stored on the PC as its SHA-256; else 401. Pairing is a QR code of
`http://<address>:<port>/pair#<code>` (six characters, five minutes, once). `app/phone_api.ROUTES` is the one table of
every route and its policy (open / home / phone / pc). Away from home (`core/tailnet.py`) runs a hidden userspace
Tailscale (pinned MSI, hashes checked, no driver, no service) whose Funnel points only at a third, dedicated listener
with its own away token (`away.html`). Never plain port forwarding. Windows-only for now (`platform.supports("away")`).

Private (Deep Cover, 1.1.4: "Private: my phone runs Tailscale", `phone.away_private`): Serve instead of Funnel, to the
same listener and name, so only devices on the user's tailnet reach it and nothing is public. Switching resets the
serve config first, so the two are never on together; the driver checks the mode it wants (`tailnet._funnel_on`).

The home-screen app (1.1.4) lives at the away address, the one of the two that is HTTPS, so it is the only place a
service worker and notifications are allowed. `web/sw.js` (served there only, `/sw.js`, policy open) caches no page
and nothing of the user's. It keeps the home-network address (`home_url` in `/api/status`, sent only to a paired phone
at the away address, accepted only in private-address form), and when a navigation gets no answer in 20 s it serves a
page of its own that says so and links to that address. The page itself (`/`, policy home) opens on the home Wi-Fi
without a token: it holds no data, and the way back from the away app is a link from another site, on which a
SameSite=Strict cookie doesn't ride (the page's own requests after it do). At the away address it needs the token.
Job alerts are local only (`web/phonealerts.js`): while the page runs on a phone, a finished job gives a banner, a buzz
on Android, the app badge and, at the away address with the phone's permission, a notification through the worker.
No push service, so a locked phone or one that has stopped the page hears nothing. The words are generic ("Cover
ready"), and the overnight queue's results are silent.

Request rules on every listener (1.1): an Origin, when a request has one, is this server's own scheme, host and port;
a request another site's page makes (`Sec-Fetch-Site: cross-site`) is refused, except following a link to a page;
JSON bodies are capped per route (`JSON_MAX`, 1 MB; 413, never read) while uploads keep their own limits; every read of
a request times out after 30 s; every response says `X-Content-Type-Options: nosniff` and `Referrer-Policy:
no-referrer`. The phone and away listeners take `MAX_LIVE` (32) connections at once (503 past it). The away listener's
`Server` header and its refusals before a token name nothing. A device token lasts 30 days from its last use
(`phone.TOKEN_IDLE_S`; each use renews it and its cookie). Wrong pairing codes are counted per address (5) and in all
(20). A phone may not cancel or retry work only the PC starts. Links the app fetches for someone (a voice install, a
training source, a song from a link) reach only the internet: `core/safeurl.py` refuses a host that is or resolves to
this PC, the home network or a link-local address, and checks every redirect. Since 1.1.1 the check is also made at
connect time: inside `safeurl.guarded()` (per thread) every socket connect and send-to is checked by the address it
really uses, a name resolved there and the connection pinned to a checked address. Every yt-dlp call runs inside it
(`core/youtube.py`'s `YoutubeDL`, its `urlopen` too), only yt-dlp's own HTTP, HLS and DASH downloaders run (no program
gets a link), one stream at a time, one fragment thread. Never a part of a stream (`section_start` / `section_end`,
e.g. a YouTube clip link) and never a stream that is live, about to be, or just ended and still being processed
(`is_live`, `live_status`; a past stream kept as a video is an ordinary file), whatever downloader yt-dlp would pick,
so the answer doesn't depend on ffmpeg being there. No download grows past `youtube.MAX_BYTES` (2 GB): yt-dlp's
`max_filesize` for a size declared up front, and a progress hook on every download for what actually arrives, both
said plainly. The audio made from it is bounded by time too (ffmpeg `-t`): a song a second past its 20 minutes, by
ffmpeg's own pass (a file that came as FLAC skips yt-dlp's conversion, and its header may say less than it holds), and
refused when it gets there; a training source at what a source keeps. `open_ydl` bounds every audio conversion
yt-dlp runs at `files.MAX_AUDIO_S` in one place (`bounded_args`: on the list yt-dlp would really use, whichever key it
comes from, written under the key it reads first; a caller's tighter `-t` is kept). Every other decode
of a file it was given (the waveform, the loudness, a dropped recording) stops at `files.MAX_AUDIO_S` (3 hours); a
song is taken by the length its waveform pass decoded, not the one its header gives. It is refused past that bound,
and refused when nothing decodes at all. A stem's level (the backing measure, `separate._rms_db`) is measured over its
first `files.MAX_AUDIO_S`, read in blocks, never the whole file in memory; a stem that can't be read is remembered
(`pipeline.backing_quiet`), not read again on every estimate. A configured proxy is reached at its own address, and links behind it are checked by name. A voice zip unpacks under a running size
cap (`max_download_gb`) with a member limit, links in it skipped. Windows PowerShell is started by its full path.

### System tray

`core/trayicon.py` (`pythonw -m undercover.tray`, pystray) is its own light process over the HTTP API, with no torch.
The server starts it; one per user; it leaves with the server after a Quit. The icon shows idle, working (the module's
bar), problem or stopped. Menu: Open, the running jobs with time left, Pause / Resume, Phone access, Restart, Quit,
Start with Windows (off by default; a Startup-folder shortcut made only on the person's click). It also asks the game
pause's question (`core/gamepause.py`): when a game starts while UnderCover has GPU work, "Pause UnderCover
while you play?" (Pause / Keep both running / Don't ask for this game), asked and never assumed. Not `core/tray.py`,
which is the recent tray of covers and speech. A game is any one of (1.1, `gamepause._look`; the readers are in
`core/platform`): the program in front fills its monitor (exclusive, Windows' "busy", or a window covering the whole
monitor that isn't maximized); a program from a game library is running (`GAME_DIRS`: Steam's `steamapps\common`,
Epic, GOG, Xbox, Riot, EA, Ubisoft; its helpers, the launchers and always-on tools such as Wallpaper Engine are not),
which also sees a game in its menus or behind another window; or a program holds the 3D engine past `GPU_PCT` (30 %)
for `GPU_FOR_S` (15 s), from Windows' GPU Engine counters (UnderCover's own Python and Windows' own processes never
count).

### The ear

A thumb up or down on every cover and speech (`core/ear.py`, `ear.json` next to the config). A thumbs down asks why and
offers one fix when its reason has one (unlike: Improve tonight; key: an octave; pace: slower or faster). Liked voices
rise in FrontMan's basic list and break ties in the one-shot. Nothing is changed or deleted because of a thumb. Every
A/B in the app is blind: no labels, no reveal before the choice. A hidden classic-player skin (`html[data-skin=winamp]`)
is reached by triple-clicking the wordmark or typing "llama" outside a text box.

## Open-source goal

UnderCover is AGPL-3.0-or-later; "Hold On" and Agent Smoke are CC0 (see LICENSE and NOTICE). Rules that keep it
publishable:

- Engines that may use restricted weights are swappable: the default is the best OSI-licensed option for code and
  weights, and restricted models (CC-BY-NC, CPML) are optional add-ons a person enables and accepts themselves, never
  bundled. A module keeps working on its open default when an add-on is missing.
- Add-ons are managed in one registry (`core/addons.py`: licence, size, acceptance; SHA-256 checked installs) and reached
  three ways: SafeHouse's Add-ons room, the installer's optional unticked step, `python -m undercover.addons`.
- No personal data in the repository. Voices of real people, songs, covers, speech, training projects, `config.toml`,
  `manifest.json` and the demo voice's model files are git-ignored. Product docs never name a real person's voice.
- Shared voice packs, when they come, are self-describing and never pickles.

## Resilience and self-healing

The installer and the app survive version mismatches and fix common problems themselves (`core/doctor.py`, the Doctor).

- Pinned, verified installs. Every download (PyTorch per backend, packages, ffmpeg, Deno, uv, Python, models) has a
  fixed version and a checksum. Model files are in `core/setup_models.PINNED` (path under `models_dir`, sources tried in
  order, SHA-256, the part it belongs to); each module registers its own files on import. Downloads go to
  `<file>.part`, resume with an HTTP Range request, and replace the file only after the checksum passes. The installer
  downloads everything up front (`installer/downloads.tsv`), so no model downloads on first use.
- Self-healing yt-dlp, the one exception to "never latest" (`core/ytdlp.py`). YouTube changes something every few weeks
  and yt-dlp ships the fix within days, so a pinned pair breaks between releases. Only yt-dlp and the yt-dlp-ejs its own
  `pin` extra names may follow PyPI. Detect: the same breakage on several videos within a window, more failures than
  successes. Fetch: the newest yt-dlp and the solver its release pins, both checked against PyPI's published SHA-256,
  and both vouched for by GitHub too (1.1): the solver's wheel by its release asset's digest, yt-dlp's wheel package
  file by file against its release's source tarball (itself checked against the release's SHA2-256SUMS; `version.py`
  may differ only in two plain strings). No vouching, no update. A release younger than 48 h is never taken by itself:
  the System room offers it ("Update now").
  Stage and test: into `<cache>/ytdlp/<yt-dlp>+ejs<solver>/` (every file's SHA-256 kept in its `pair.json`, checked at
  every start: a pair whose files differ, or that has no hashes, falls back to the last known good, then the venv's,
  with a log line), then resolve known public videos; swap in only if at least as good, and only when nothing is using
  yt-dlp. Roll back: `state.json` keeps the last known good pair. Say so:
  `logs/doctor.log` and the System room, with a Deep Cover switch to turn it off. The venv's pinned pair
  (`installer/requirements/common.txt`) is the last resort and is never upgraded.
- A recorded manifest (`manifest.json` in the app folder, git-ignored; `core/manifest.py`): Python, venv, backend and torch
  file, GPU and driver, every pinned package's installed version, the tools, every model file's size and SHA-256.
  `kind: "installed"` is written by setup at its end; `"observed"` by the app, once, for an older or hand-built install.
  The pins in `installer/requirements/*.txt` are the source of truth for what a Fix installs.
- The Doctor (`python -m undercover.doctor [--full] [--json]`, `/api/doctor`, the System room) checks Python and the
  venv, the PyTorch build against `[gpu] backend`, the card and its driver (full mode), every pinned package, ffmpeg and
  Deno, the model files (size fast; SHA-256 in full mode), free space, the `[paths]` folders, the port, leftovers,
  interrupted work, and the firewall while Phone access is on. Fast mode runs under 2 s with no torch import; full mode
  is a job. Each result has `id, group, state (ok | info | warn | bad), title, say, detail, fix {id, label, how}`. Fixes:
  `stale`, `model:<path>` / `models` (the new copy checks out before the old is replaced), `packages` (only the differing
  hashed pin lines, on the person's click, never PyTorch), `cpu`, `resume`, `manifest`; `setup.ps1 -Repair` is offered in
  words only. The Doctor never touches voices, songs, covers, speech or training projects and never changes Windows or
  driver settings.
- Safe healing at startup (`doctor.heal_at_startup`, before the server listens; `logs/doctor.log`): a pinned model file
  that is missing or the wrong size is fetched again; leftovers older than 6 h are removed through
  `storage.remove_files`, never inside training projects, previews or the voices, never in a folder that running or
  paused work owns; work a crash interrupted resumes once by itself (a training with its `resume.pt`, transcripts) or is
  put back as paused; work that died twice is held.
- Installer repair (`setup.ps1 -Repair`) runs the Doctor as JSON and redoes only what it reports; `-DryRun` shows the
  plan. It keeps the venv, `config.toml` and every data folder.
- A diagnostics file (`core/diagnostics.py`, `GET /api/doctor/bundle.zip`): the logs, the manifest, `config.toml`, a
  fresh report. Text only, no audio; the person's folders shortened to `<UnderCover>`, `<library>`, `<working files>`,
  `<model files>`, `<voices>` and `%USERPROFILE%`.
- Low profile (`core/lowprofile.py`, 1.0.2): UnderCover keeps to part of the graphics card and works in bursts, so a
  game gets the card most of the time. One number from the user, `limit_gb`, is the whole of UnderCover on the card:
  the server process is capped with PyTorch's per-process memory fraction (a hard limit on ROCm and CUDA: its allocator
  refuses more, as a full card would), the cadence worker gets the share of the job that starts it (speech: the largest
  engine it may read with, `engines.py` vram_gb, a voice's pinned one included; transcripts: Whisper's 5 GB; a cadence
  training: both), through its environment (`UNDERCOVER_GPU_LIMIT_GB`); its cap can't move, so a worker with less than
  the next job needs is ended first, and one holding more than a job that doesn't fit needs gives its share back and that share comes out of the server's cap while the worker is loaded; a
  training stops the worker first and gets the whole limit. Under the cap, what the next step doesn't need is freed
  (`gpu.release`: the separator before a conversion or a speech; the separator, the converter and listen-back's ear
  before a training). Every GPU job records its peak (`gpu_peak_gb`, `gpu_alloc_gb`, `gpu_start_gb` on its snapshot);
  `cache/gpu_need.json` keeps each kind's allocated peaks, and a kind that needs more than the cap allows waits and says
  so ("needs about 9 GB; Low profile allows 6", `waits_for: "Low profile"`) instead of running out of memory; an
  out-of-memory under the cap teaches the need and the job is retried once the cap lifts. Under a tight cap a training
  step is made of two micro-batches whose gradients add up (gradient accumulation; the generator's random slice and
  noise replayed for its own pass): the plan's batch, samples and steps stay, so a voice trained under Low profile is
  the same voice, only slower. While a training runs under it, covers wait for its end instead of running between its
  steps; a job that doesn't fit goes to the back of the queue, so the ones behind it that fit run. An out-of-memory inside the
  worker is the worker's, not the job's: it never teaches the server-side need. An overnight item is sized by its own
  kind (new, improve, cadence, newcad), not as the queue's job. `burst` (0.25–0.75) is the share of the time it works: each step loop (training steps, conversion
  and separation chunks, sentences) calls `Pacer.tick()`, which waits for the GPU and pauses in proportion to the step
  (20 ms to 1 s). Speeds measured under it are kept apart (`-lp` keys in speak_speed.json, train_speed.json and
  eta.json; `low_profile: true` on timings.jsonl records), and an estimate with none yet takes 1/burst as its first
  guess. The game pause's answer `low` turns it on for that game (remembered in `[low_profile] for_games`; `auto`
  turns it on for any game without asking) and the game's closing turns it off again, only then. Settings:
  `[low_profile] on, limit_gb, burst, auto, for_games`; `POST /api/lowprofile` and `/api/status.low_profile`
  (absent on a PC with no card to share, which hides the control). Test servers (`UNDERCOVER_TEST_ROOTS`) run with it on
  at 12 GB unless `UNDERCOVER_LOW_PROFILE` says otherwise ("0" off, or a limit), so a test run leaves half the card.
- Vaulting (`core/vault.py`): a quiet, incremental backup of what cannot be downloaded again (voices trained here, kept
  covers and speech, settings, the ear) to a folder on another drive, as plain files named by the SHA-256 of their
  original bytes, with a snapshot per run and a Restore that asks before replacing. Off until a folder is picked.

## Tests, scripts and the installer

`tests/smoke.py` is the one test suite, about 180 checks. `--unit-only` runs without a server in about a minute; the
plain run adds the API, one cover and one speech; `--full` adds a short training with Pause / Resume, transcripts and
cadence (10-25 minutes). For anything that makes or deletes files it starts its own sandbox server on the first free
port of 7862-7869 (or `UNDERCOVER_SANDBOX_PORT`) with scratch folders, its own settings file (`UNDERCOVER_CONFIG`) and
`UNDERCOVER_TEST_ROOTS` set, and it compares every real folder before and after the run: any change fails it.
`--port N` checks a running server read-only. Smoke also checks the dependency rules, the platform helper rule, the
`EventSource` rule, that every tab and view exists, that every route is in the phone table, and the roll-off's
properties. The golden-set bookkeeping check reads the maintainers' own notes and runs only when `UNDERCOVER_DEV_ROOT`
names them; unset, it's skipped. `tests/fake_tailscale.py` stands in for Tailscale; `tests/installer_e2e.ps1` runs the real installer end to
end, and with `-UpgradeCheck` (also after every `-CancelCheck`) the update in place: the released 1.0.0 installed and
used (a kept cover and speech, a setting, a voice of its own name), this installer over it, and what survived checked. `scripts/check_env.py` verifies the torch build and the GPU; `scripts/make_demo_voice.py` builds Agent Smoke from
a Kokoro Reader reading a public-domain text, inside smoke's sandbox; `scripts/reader_prints.py` builds
`assets/reader-prints.json`.

The installer (`installer/UnderCover.iss`, Inno Setup) installs per user with no admin prompt. `build.ps1` writes
`addons.isi` from the add-ons registry and `downloads.tsv` from the pinned lists (one line per file: group, destination,
URLs, SHA-256, bytes, pin), then runs ISCC. The wizard's download page fetches every file and checks each hash; after it
nothing goes online. Files kept from an earlier run (Stop, or a run that broke off) are checked by SHA-256 again before
they count (`keptcheck.ps1`, run hidden so the page stays live); a damaged one is deleted and downloaded again. LICENSE,
NOTICE and README.md go into the install folder.
An update downloads only what changed (1.0.3): `installer/installed.ps1` lists what the install being updated already
has (a package at its exact pin in its own setup-made `.venv`, read from the venv itself; uv, Python, ffmpeg and Deno
where setup reuses them), the download page skips those, and setup installs only the pins that differ (still
`--require-hashes`, offline). A venv setup didn't make, one on the wrong Python, or one that won't start counts as
having nothing; a repair reinstalls the packages as before. `setup.ps1` installs a private Python (`python.ps1`, via uv), the packages for the detected backend
(`detect_gpu.ps1`: rocm, cuda128, cuda126 or cpu; `requirements/*.txt`, hashed by `pin_hashes.py` and constrained by
`constraints.txt`), ffmpeg and Deno, puts the models in place (`python -m undercover.models install --offline`), writes
the GPU settings, runs `selftest.py`, writes the manifest and makes a launcher. `space.ps1` and `sizes.json` give the
wizard real disk needs per drive. A fresh install seeds the sample song and the demo voice once (`app/firstrun.py`);
a seed the person deletes does not come back.
