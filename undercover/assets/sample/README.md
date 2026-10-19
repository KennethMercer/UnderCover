# The sample song

`sample.json` names the audio file next to it and how the pages show it:

```json
{"title": "...", "artist": "the credit line", "file": "sample.flac", "seconds": 45,
 "licence": "UnderCover's own recording", "notice": "one line for the NOTICE file"}
```

A fresh install adds it to the library once, as a normal song marked `sample: true` (undercover/app/firstrun.py), beside the
demo voice Agent Smoke. If the user deletes it, it stays deleted. Nothing in 1.0 points a new user at it: the one-shot can
use it by name ("Hold On as Agent Smoke" plans this copy, with nothing searched or downloaded), and the tutorial redesign
will build on it. No audio file here means no sample song. To swap the song, replace the audio file and edit `sample.json`.
