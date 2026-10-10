"""Download the shared RVC models (HuBERT/ContentVec features, RMVPE pitch) if missing.

    python -m undercover.core.setup_models
"""
from .config import CONFIG, path

SOURCE = "https://huggingface.co/lj1995/VoiceConversionWebUI/resolve/main"
FILES = {  # local path under models_dir -> path in the RVC asset repo
    f"{CONFIG['conversion']['hubert_dir']}/config.json": "hubert_base/config.json",
    f"{CONFIG['conversion']['hubert_dir']}/preprocessor_config.json": "hubert_base/preprocessor_config.json",
    f"{CONFIG['conversion']['hubert_dir']}/pytorch_model.bin": "hubert_base/pytorch_model.bin",
    CONFIG["conversion"]["rmvpe"]: "rmvpe.pt",
}
# Voice training: RVC v2 pitch-guided base models (generator + discriminator) per sample rate, and the
# WeSpeaker ResNet34-LM speaker-embedding model (ONNX, runs on CPU) used to keep only the target's voice.
SPEAKER_MODEL = "speaker/wespeaker_resnet34_lm.onnx"
SPEAKER_URL = "https://huggingface.co/Wespeaker/wespeaker-voxceleb-resnet34-LM/resolve/main/voxceleb_resnet34_LM.onnx"


def base_model_files(sr: int) -> dict:
    k = f"{sr // 1000}k"
    return {f"rvc/pretrained_v2/f0G{k}.pth": f"{SOURCE}/pretrained_v2/f0G{k}.pth",
            f"rvc/pretrained_v2/f0D{k}.pth": f"{SOURCE}/pretrained_v2/f0D{k}.pth"}


def ensure(progress=None, files: dict | None = None) -> list[str]:
    """Fetch any missing model files (default: the shared conversion models); returns the ones downloaded.
    `files` maps a path under models_dir to a full URL."""
    import requests
    got = []
    files = files or {local: f"{SOURCE}/{remote}" for local, remote in FILES.items()}
    for local, url in files.items():
        dest = path("models_dir") / local
        if dest.exists() and dest.stat().st_size > 0:
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(dest.suffix + ".part")
        with requests.get(url, stream=True, timeout=60) as r:
            r.raise_for_status()
            total, done = int(r.headers.get("content-length") or 0), 0
            with open(tmp, "wb") as f:
                for chunk in r.iter_content(1 << 20):
                    f.write(chunk)
                    done += len(chunk)
                    if progress:
                        progress(local, done, total)
        tmp.replace(dest)
        got.append(local)
    return got


if __name__ == "__main__":
    got = ensure(lambda f, d, t: print(f"\r{f}: {d / 1e6:.0f}/{t / 1e6:.0f} MB", end="", flush=True))
    print("\nDownloaded: " + ", ".join(got) if got else "All shared models present.")
