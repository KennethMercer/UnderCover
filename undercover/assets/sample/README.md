# The First assignment's sample song

`sample.json` names the audio file next to it and how the pages show it:

```json
{"title": "...", "artist": "the credit line", "file": "sample.flac", "seconds": 45,
 "licence": "UnderCover's own recording", "notice": "one line for the NOTICE file"}
```

No audio file here = no sample song: the First assignment's first step then asks for any song. Swapping the song = replacing
the audio file and editing `sample.json`. On first run (and on Replay, if the user deleted it) it's added to the library as
a normal song marked `sample: true` (undercover/app/tutorial.py).
