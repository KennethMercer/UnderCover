"""Command-line cover tool.

    cover song.mp3 --voice plankton --key 2 --octave 0
    cover song.mp3 --voice "spongebob" --download      # install the best search match if missing
"""
import argparse
import sys
import time
from pathlib import Path


def main():
    ap = argparse.ArgumentParser(prog="cover", description="Make an AI voice cover of a song.")
    ap.add_argument("song", type=Path)
    ap.add_argument("--voice", required=True, help="installed voice name")
    ap.add_argument("--key", type=float, help="transpose the whole song (music + voice), semitones")
    ap.add_argument("--octave", type=int, help="move only the new voice by whole octaves (-1, 0, +1)")
    ap.add_argument("--backing-voice", help="second installed voice for the backing vocals (splits lead/backing)")
    ap.add_argument("--backing-octave", type=int, help="move only the backing voice by whole octaves")
    ap.add_argument("--backing-db", type=float, help="backing vocal level offset in dB")
    ap.add_argument("--pitch", type=float, help="advanced: vocal-only semitones; anything but a multiple of 12 is out of key")
    ap.add_argument("--index-ratio", type=float)
    ap.add_argument("--protect", type=float)
    ap.add_argument("--volume-envelope", type=float)
    ap.add_argument("--dereverb", action="store_true", default=None, help="remove reverb before converting")
    ap.add_argument("--karaoke", action="store_true", default=None, help="convert lead vocal only, keep backing vocals")
    ap.add_argument("--quality", choices=["fast", "high", "max"], help="separation quality (default high)")
    ap.add_argument("--reverb", type=float, help="add reverb to the new vocal (0.1-0.3)")
    ap.add_argument("--vocal-db", type=float, help="vocal level offset in dB")
    ap.add_argument("--format", choices=["wav", "flac", "mp3"])
    ap.add_argument("--download", action="store_true", help="if the voice isn't installed, install the best search match")
    ap.add_argument("-o", "--out", type=Path, help="output path (extension set by --format)")
    a = ap.parse_args()

    from ..core.voices import ModelError, download_and_install, get_voice, search
    from .pipeline import CoverEngine, CoverOptions
    from .remix import RemixError
    from ..core import timing
    timing.VERBOSE = False

    if a.pitch is not None and a.pitch % 12 and a.key is None and a.octave is None:
        print(f"Warning: --pitch {a.pitch:g} shifts only the vocal, so it will be out of key with the music. "
              f"Use --key {a.pitch:g} to move the whole song, or --octave for whole octaves.")
    if not a.song.exists():
        sys.exit(f"Song not found: {a.song}")
    try:
        try:
            get_voice(a.voice)
        except ModelError:
            if not a.download:
                res = search(a.voice, limit=5)
                msg = f"No voice called '{a.voice}' is installed."
                if res:
                    msg += " Top matches online:\n" + "\n".join(f"  {i}. {r.name}  ({r.repo})" for i, r in enumerate(res, 1))
                    msg += f"\nRe-run with --download to install #1, or: python -m undercover.core.voices download <n> --name {a.voice}"
                sys.exit(msg)
            res = search(a.voice, limit=1)
            if not res:
                sys.exit(f"No voice called '{a.voice}' found online either. Try a different name.")
            print(f"Installing '{res[0].name}' from {res[0].repo} ...")
            download_and_install(res[0], name=a.voice)

        start = time.perf_counter()
        from .pipeline import estimate
        est = estimate(a.song, CoverOptions(voice=a.voice, quality=a.quality, karaoke=a.karaoke, dereverb=a.dereverb,
                                            key=a.key, octave=a.octave, pitch=a.pitch, backing_voice=a.backing_voice))
        print(f"Estimated {est['total']:.0f}s" + (" (stems cached)" if est["stems_cached"] else ""))
        last = {"stage": None}

        def progress(stage, frac, msg):
            if stage != last["stage"] and last["stage"]:
                print()
            last["stage"] = stage
            if stage == "done":
                return
            bar = "#" * int(frac * 24)
            print(f"\r[{time.perf_counter() - start:6.1f}s] {msg:<40.40s} [{bar:<24}] {frac * 100:3.0f}%", end="", flush=True)

        opts = CoverOptions(voice=a.voice, pitch=a.pitch, key=a.key, octave=a.octave, index_ratio=a.index_ratio, protect=a.protect,
                            volume_envelope=a.volume_envelope, dereverb=a.dereverb, karaoke=a.karaoke, quality=a.quality,
                            reverb=a.reverb, vocal_db=a.vocal_db, format=a.format,
                            backing_voice=a.backing_voice, backing_octave=a.backing_octave, backing_db=a.backing_db)
        if a.backing_voice:
            get_voice(a.backing_voice)
        r = CoverEngine().make_cover(a.song, opts, a.out, progress)
    except (ModelError, RemixError, FileNotFoundError) as e:
        sys.exit(str(e))
    s = r.seconds
    print(f"\nDone in {s['total']:.1f}s (separate {s['separate']:.1f}s, convert {s['convert']:.1f}s, mix {s['remix']:.1f}s)")
    for n in r.notes:
        print("Note:", n)
    print(r.output)


if __name__ == "__main__":
    main()
