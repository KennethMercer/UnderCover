"""Download the shared RVC models (HuBERT/ContentVec features, RMVPE pitch) if missing.

    python -m undercover.core.setup_models

Every download is pinned (a fixed revision or release) and checked against its SHA-256 (SHA256 below; phase 4b,
ARCHITECTURE.md "Resilience and self-healing"). A file that doesn't match is deleted and the download fails.
"""
import hashlib

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


def ensure(progress=None, files: dict | None = None, sha256: dict | None = None) -> list[str]:
    """Fetch any missing model files (default: the shared conversion models); returns the ones downloaded.
    `files` maps a path under models_dir to a full URL (file:// works too, for tests); `sha256` maps a path under
    models_dir (or the URL's file part) to its SHA-256, checked after the download."""
    import requests
    got = []
    files = files or {local: f"{SOURCE}/{remote}" for local, remote in FILES.items()}
    for local, url in files.items():
        dest = path("models_dir") / local
        if dest.exists() and dest.stat().st_size > 0:
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(dest.suffix + ".part")
        if url.startswith("file://"):
            import shutil
            from urllib.request import url2pathname
            shutil.copyfile(url2pathname(url[len("file:"):]), tmp)
            _check(tmp, (sha256 or {}).get(local) or _expected(url, sha256), local)
            tmp.replace(dest)
            got.append(local)
            continue
        with requests.get(url, stream=True, timeout=60) as r:
            r.raise_for_status()
            total, done = int(r.headers.get("content-length") or 0), 0
            with open(tmp, "wb") as f:
                for chunk in r.iter_content(1 << 20):
                    f.write(chunk)
                    done += len(chunk)
                    if progress:
                        progress(local, done, total)
        _check(tmp, (sha256 or {}).get(local) or _expected(url, sha256), local)
        tmp.replace(dest)
        got.append(local)
    return got


def _check(tmp, want: str | None, local: str):
    if want and sha256_of(tmp) != want:
        tmp.unlink(missing_ok=True)
        raise ModelError(f"The download of {local} came out different from the expected file (checksum mismatch), so it "
                         "wasn't used. Try again; if it keeps happening, the source may have changed.")


if __name__ == "__main__":
    got = ensure(lambda f, d, t: print(f"\r{f}: {d / 1e6:.0f}/{t / 1e6:.0f} MB", end="", flush=True))
    print("\nDownloaded: " + ", ".join(got) if got else "All shared models present.")
