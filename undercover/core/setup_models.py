"""Download the shared RVC models (HuBERT/ContentVec features, RMVPE pitch) if missing.

    python -m undercover.core.setup_models

Every download is pinned (a fixed revision or release) and checked against its SHA-256 (SHA256 below; phase 4b,
ARCHITECTURE.md "Resilience and self-healing"). A file that doesn't match is deleted and the download fails.

PINNED (phase 10) is the one list of every model file UnderCover can fetch: path under models_dir -> its sources (the
first that serves the right bytes wins), its SHA-256 and the part it belongs to. Core files register below; modules
register theirs when imported (register_pinned), like the add-ons registry. The Doctor checks the files against it and
re-fetches a broken one (refetch: the new copy is checked before it replaces the old, so nothing is deleted first).
Downloads resume: an interrupted one leaves <file>.part, and the next try asks the server for the rest.
"""
import hashlib
import os

from .config import CONFIG, path
from .voices import ModelError

RVC_REV = "e6d0c1a17da07c33557852f9dfa2bd44cc75737d"   # lj1995/VoiceConversionWebUI (MIT), checked 2026-10-11
SOURCE = f"https://huggingface.co/lj1995/VoiceConversionWebUI/resolve/{RVC_REV}"
FILES = {  # local path under models_dir -> path in the RVC asset repo
    f"{CONFIG['conversion']['hubert_dir']}/config.json": "hubert_base/config.json",
    f"{CONFIG['conversion']['hubert_dir']}/preprocessor_config.json": "hubert_base/preprocessor_config.json",
    f"{CONFIG['conversion']['hubert_dir']}/pytorch_model.bin": "hubert_base/pytorch_model.bin",
    CONFIG["conversion"]["rmvpe"]: "rmvpe.pt",
}
FILE_SIZES = {   # bytes of the pinned copies (measured 2026-10-18): a cut-off download is found from its size, and the
    # first start's progress (core/jobs) is measured against them
    f"{CONFIG['conversion']['hubert_dir']}/config.json": 1492,
    f"{CONFIG['conversion']['hubert_dir']}/preprocessor_config.json": 225,
    f"{CONFIG['conversion']['hubert_dir']}/pytorch_model.bin": 189206711,
    CONFIG["conversion"]["rmvpe"]: 181184272,
}
# Voice training: RVC v2 pitch-guided base models (generator + discriminator) per sample rate, and the
# WeSpeaker ResNet34-LM speaker-embedding model (ONNX, runs on CPU) used to keep only the target's voice.
SPEAKER_MODEL = "speaker/wespeaker_resnet34_lm.onnx"
SPEAKER_URL = ("https://huggingface.co/Wespeaker/wespeaker-voxceleb-resnet34-LM/resolve/"
               "f0c48c298fd835726c27956a5d617bad7115627e/voxceleb_resnet34_LM.onnx")   # CC-BY-4.0
# SHA-256 of every pinned model file, by its URL's file part (from Hugging Face's LFS records, or hashed here when the
# file is too small for LFS). Modules add theirs (Kokoro, Chatterbox, add-ons) with sha256= on ensure().
SHA256 = {
    "hubert_base/config.json": "0346950779dfb7f9316fa74ed846e2b8a22a08eedfdc5387b73f327cb1a4a7cf",
    "hubert_base/preprocessor_config.json": "7c1976a680fb7acc757cd36fb08eef878fa36c70b4c9d2d595df9c608bbbbf0e",
    "hubert_base/pytorch_model.bin": "cc8c20f4b90a520757260197a3ff2505705a7adbd20ad9eeaa4e1a9b38442ef5",
    "rmvpe.pt": "6d62215f4306e3ca278246188607209f09af3dc77ed4232efdd069798c4ec193",
    "pretrained_v2/f0G32k.pth": "2332611297b8d88c7436de8f17ef5f07a2119353e962cd93cda5806d59a1133d",
    "pretrained_v2/f0D32k.pth": "bd7134e7793674c85474d5145d2d982e3c5d8124fc7bb6c20f710ed65808fa8a",
    "pretrained_v2/f0G40k.pth": "3b2c44035e782c4b14ddc0bede9e2f4a724d025cd073f736d4f43708453adfcb",
    "pretrained_v2/f0D40k.pth": "6b6ab091e70801b28e3f41f335f2fc5f3f35c75b39ae2628d419644ec2b0fa09",
    "pretrained_v2/f0G48k.pth": "b5d51f589cc3632d4eae36a315b4179397695042edc01d15312e1bddc2b764a4",
    "pretrained_v2/f0D48k.pth": "2269b73c7a4cf34da09aea99274dabf99b2ddb8a42cbfb065fb3c0aa9a2fc748",
    "voxceleb_resnet34_LM.onnx": "7bb2f06e9df17cdf1ef14ee8a15ab08ed28e8d0ef5054ee135741560df2ec068",
}


def _expected(url: str, sha256: dict | None) -> str | None:
    tail = url.split("/resolve/", 1)[-1].split("/", 1)[-1] if "/resolve/" in url else url.rsplit("/", 1)[-1]
    for table in (sha256 or {}, SHA256):
        for k, v in table.items():
            if url.endswith("/" + k) or tail == k:
                return v
    return None


def sha256_of(p) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 22), b""):
            h.update(chunk)
    return h.hexdigest()


def base_model_files(sr: int) -> dict:
    k = f"{sr // 1000}k"
    return {f"rvc/pretrained_v2/f0G{k}.pth": f"{SOURCE}/pretrained_v2/f0G{k}.pth",
            f"rvc/pretrained_v2/f0D{k}.pth": f"{SOURCE}/pretrained_v2/f0D{k}.pth"}


def missing(files: dict) -> dict:
    """{path under models_dir: expected bytes} -> those not fully there yet. A file shorter than expected counts as
    missing: audio-separator writes straight to the final name, so a cut-off download looks present."""
    m = path("models_dir")
    out = {}
    for f, n in files.items():
        try:
            have = (m / f).stat().st_size
        except OSError:
            have = 0
        if have <= 0 or (n and have < n):
            out[f] = n
    return out


def fetched_share(todo: dict) -> float:
    """How much of `todo` ({path under models_dir: expected bytes}) has arrived, 0-1: each file, or its .part while
    UnderCover's own download writes it. For a bar that moves while the models download."""
    m = path("models_dir")
    total = sum(todo.values()) or 1
    got = 0
    for f, n in todo.items():
        p = m / f
        have = 0
        for q in (p, p.with_name(p.name + ".part")):
            try:
                have = max(have, q.stat().st_size)
            except OSError:
                pass
        got += min(have, n)
    return got / total


class DownloadStopped(ModelError):
    """A download stopped part-way because Stop was pressed (its .part stays: a later try resumes it)."""


def ensure(progress=None, files: dict | None = None, sha256: dict | None = None, cancel=None) -> list[str]:
    """Fetch any missing model files (default: the shared conversion models); returns the ones downloaded.
    `files` maps a path under models_dir to a full URL (file:// works too, for tests); `sha256` maps a path under
    models_dir (or the URL's file part) to its SHA-256, checked after the download."""
    got = []
    files = files or {local: f"{SOURCE}/{remote}" for local, remote in FILES.items()}
    for local, url in files.items():
        dest = path("models_dir") / local
        if dest.exists() and dest.stat().st_size > 0:
            continue
        want = (sha256 or {}).get(local) or _expected(url, sha256) or (PINNED.get(local) or {}).get("sha256")
        from . import storage
        if storage.ALLOWED_ROOTS is not None and storage.refuse([dest]):   # a test never fills the real models folder
            raise ModelError(f"Test mode: {local} is missing, and downloading it would write outside the test's folders "
                             f"({storage.refuse([dest])}).")
        fetch(url, dest, want, local, progress, cancel=cancel)
        got.append(local)
        if want:
            _record(local, dest, want)
    return got


def _record(local: str, dest, want: str):
    try:   # the install record learns the new file (the Doctor checks its size at every start)
        from . import manifest
        manifest.note_model(local, dest.stat().st_size, want)
    except Exception:   # noqa: BLE001 - a download never fails over its record
        pass


def fetch(url: str, dest, want: str | None, local: str = "", progress=None, tries: int = 3, cancel=None):
    """Download url into dest through dest.part: resumed from where an earlier try stopped (an HTTP Range request),
    checked against want (SHA-256) and only then swapped in, so dest is never a half-written or wrong file."""
    import time
    from pathlib import Path
    dest = Path(dest)
    local = local or dest.name
    if not want:   # (1.1 hardening: a file nothing can check is never downloaded)
        raise ModelError(f"{local} has no known checksum, so it isn't downloaded.")
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    if url.startswith("file://"):
        import shutil
        from urllib.request import url2pathname
        shutil.copyfile(url2pathname(url[len("file:"):]), tmp)
    else:
        import requests
        for attempt in range(1, tries + 1):
            have = tmp.stat().st_size if tmp.exists() else 0
            try:
                headers = {"Range": f"bytes={have}-"} if have else {}
                with requests.get(url, stream=True, timeout=60, headers=headers) as r:
                    if r.status_code == 416:   # the .part already holds the whole file
                        break
                    r.raise_for_status()
                    resumed = have and r.status_code == 206
                    done = have if resumed else 0
                    total = done + int(r.headers.get("content-length") or 0)
                    with open(tmp, "ab" if resumed else "wb") as f:
                        for chunk in r.iter_content(1 << 20):
                            if cancel is not None and cancel():   # Stop: within one megabyte (Internal Affairs P2-4)
                                raise DownloadStopped(f"Stopped while downloading {local}.")
                            f.write(chunk)
                            done += len(chunk)
                            if progress:
                                progress(local, done, total)
                break
            except (requests.ConnectionError, requests.Timeout, requests.exceptions.ChunkedEncodingError):
                if attempt == tries:
                    raise
                time.sleep(2 * attempt)   # the .part stays: the next try carries on from it
    if sha256_of(tmp) != want:
        tmp.unlink(missing_ok=True)
        raise ModelError(f"The download of {local} came out different from the expected file (checksum mismatch), so it "
                         "wasn't used. Try again; if it keeps happening, the source may have changed.")
    os.replace(tmp, dest)


# ---------------------------------------------------------------- every pinned model file (phase 10)

PINNED: dict = {}   # path under models_dir -> {"urls": [...], "sha256": ..., "part": ..., "about": ...}
# Bytes of every model file setup installs (measured 2026-10-18 from copies matching their SHA-256; the two 32 kHz base
# models from their source's Content-Length): what setup's step and the first start measure progress against, and
# how the Doctor finds a cut-off copy from its size alone.
KNOWN_SIZES = {
    "rvc/hubert_base/config.json": 1492,
    "rvc/hubert_base/preprocessor_config.json": 225,
    "rvc/hubert_base/pytorch_model.bin": 189206711,
    "rvc/pretrained_v2/f0D32k.pth": 142875703,
    "rvc/pretrained_v2/f0D40k.pth": 142875703,
    "rvc/pretrained_v2/f0D48k.pth": 142875703,
    "rvc/pretrained_v2/f0G32k.pth": 73950049,
    "rvc/pretrained_v2/f0G40k.pth": 73106273,
    "rvc/pretrained_v2/f0G48k.pth": 75465569,
    "rvc/rmvpe.pt": 181184272,
    "separator/config_mel_band_roformer_kim_ft_unwa.yaml": 1721,
    "separator/dereverb_mel_band_roformer_anvuew.yaml": 1846,
    "separator/dereverb_mel_band_roformer_anvuew_sdr_19.1729.ckpt": 913107578,
    "separator/mel_band_roformer_karaoke_aufr33_viperx_sdr_10.1956.ckpt": 913096801,
    "separator/mel_band_roformer_karaoke_aufr33_viperx_sdr_10.1956_config.yaml": 1726,
    "separator/mel_band_roformer_kim_ft_unwa.ckpt": 913100690,
    "speaker/wespeaker_resnet34_lm.onnx": 26530309,
    "tts/chatterbox-turbo/added_tokens.json": 418,
    "tts/chatterbox-turbo/conds.pt": 169454,
    "tts/chatterbox-turbo/merges.txt": 456318,
    "tts/chatterbox-turbo/s3gen_meanflow.safetensors": 1064875036,
    "tts/chatterbox-turbo/special_tokens_map.json": 470,
    "tts/chatterbox-turbo/t3_turbo_v1.safetensors": 1915480052,
    "tts/chatterbox-turbo/tokenizer_config.json": 3878,
    "tts/chatterbox-turbo/ve.safetensors": 5695784,
    "tts/chatterbox-turbo/vocab.json": 999186,
    "tts/kokoro/kokoro-v1.0.onnx": 325532387,
    "tts/kokoro/voices-v1.0.bin": 28214398,
    "whisper/large-v3-turbo.pt": 1617941637,
}


def register_pinned(files: dict, sha256: dict, part: str, about: str = "", sizes: dict | None = None) -> None:
    """Add model files to the pinned list. files: path under models_dir -> URL or a list of URLs (tried in order);
    sha256: path -> SHA-256 (a file without one isn't registered: nothing could check it); sizes: path -> bytes, where
    known (a file of another size is cut off or damaged, found without reading it)."""
    for local, urls in files.items():
        want = sha256.get(local) or _expected(urls if isinstance(urls, str) else urls[0], sha256)
        if want:
            PINNED[local] = {"urls": [urls] if isinstance(urls, str) else list(urls), "sha256": want, "part": part,
                             "about": about, "size": (sizes or {}).get(local) or KNOWN_SIZES.get(local)}


def refetch(local: str, progress=None) -> None:
    """Download a pinned file again (missing or broken), from its first source that serves the right bytes. The old copy
    stays until the new one has passed its checksum, then is replaced (never deleted first)."""
    rec = PINNED.get(local)
    if not rec:
        raise ModelError(f"{local} isn't a file UnderCover knows how to download again.")
    from . import storage
    dest = path("models_dir") / local
    if not storage._inside(dest, path("models_dir")) or storage._same(dest, path("models_dir")):
        raise ModelError(f"{local} isn't inside the model files folder; nothing was downloaded.")
    if storage.ALLOWED_ROOTS is not None:   # test mode: the same guard as every delete and move (core/storage.py)
        why = storage.refuse([dest])
        if why:
            raise ModelError(why)
    last = None
    for url in rec["urls"]:
        try:
            fetch(url, dest, rec["sha256"], local, progress)
            _record(local, dest, rec["sha256"])
            return
        except InterruptedError:
            raise   # cancelled: not a reason to try the next source
        except Exception as e:   # noqa: BLE001 - try the next source; report the last problem
            last = e
    raise last if isinstance(last, ModelError) else ModelError(
        f"Couldn't download {local} again ({type(last).__name__}). Check the internet connection and try again.")


register_pinned({**{local: f"{SOURCE}/{remote}" for local, remote in FILES.items()},
                 **{k: v for sr in (32000, 40000, 48000) for k, v in base_model_files(sr).items()}}, SHA256, "rvc",
                sizes=FILE_SIZES)
register_pinned({SPEAKER_MODEL: SPEAKER_URL}, SHA256, "speaker")


if __name__ == "__main__":
    got = ensure(lambda f, d, t: print(f"\r{f}: {d / 1e6:.0f}/{t / 1e6:.0f} MB", end="", flush=True))
    print("\nDownloaded: " + ", ".join(got) if got else "All shared models present.")
