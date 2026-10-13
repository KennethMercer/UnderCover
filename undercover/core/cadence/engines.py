"""The cadence engines UnderCover knows (metadata only: the code that runs them is in worker.py).

Each engine reads one sentence at a time: text + a reference clip of the person (+ its transcript for engines that
time their speech from it) -> 24 kHz speech. The best-measured engine is also MIT-licensed (Chatterbox), so a public
build and the user's machine use the same default; engines whose weights aren't OSI-licensed are add-ons
(core/addons.py) that the user installs and accepts themselves.

Measured 2026-10-11 on the RX 7900 XTX (docs/research/cadence.md): Chatterbox 0.5-0.6x realtime at cfg 0.3 (3.6 GB),
F5-TTS 2.9-3.5x (0.9 GB), XTTS-v2 0.84x (2.1 GB).
"""
from .. import addons, setup_models
from ..config import CONFIG, path

HF = "https://huggingface.co"
# Chatterbox weights, pinned to the revision phase 3 measured (5 files, ~3.2 GB) -> models/tts/chatterbox/
CHATTERBOX_REV = "5bb1f6ee58e50c3b8d408bc82a6d3740c2db6e18"
CHATTERBOX_FILES = {f"tts/chatterbox/{f}": f"{HF}/ResembleAI/chatterbox/resolve/{CHATTERBOX_REV}/{f}"
                    for f in ("ve.safetensors", "t3_cfg.safetensors", "s3gen.safetensors", "tokenizer.json", "conds.pt")}
# Whisper (openai-whisper checks the file's SHA-256 itself) -> models/whisper/
WHISPER_URL = ("https://openaipublic.azureedge.net/main/whisper/models/"
               "aff26ae408abcba5fbf8813c21e62b0941638c5f6eebfb145be0c9839262a19a/large-v3-turbo.pt")
F5_REV, VOCOS_REV = "84e5a410d9cead4de2f847e7c9369a6440bdfaca", "0feb3fdd929bcd6649e0e7c5a688cf7dd012ef21"
F5_FILES = {"tts/f5/model_1250000.safetensors": f"{HF}/SWivid/F5-TTS/resolve/{F5_REV}/F5TTS_v1_Base/model_1250000.safetensors",
            "tts/f5/vocos/config.yaml": f"{HF}/charactr/vocos-mel-24khz/resolve/{VOCOS_REV}/config.yaml",
            "tts/f5/vocos/pytorch_model.bin": f"{HF}/charactr/vocos-mel-24khz/resolve/{VOCOS_REV}/pytorch_model.bin"}
MODEL_FILES = {"chatterbox": CHATTERBOX_FILES, "f5": F5_FILES}
# SHA-256 of each file above (Hugging Face's LFS records; small files hashed here), checked after every download
MODEL_SHA256 = {
    "tts/chatterbox/ve.safetensors": "f0921cab452fa278bc25cd23ffd59d36f816d7dc5181dd1bef9751a7fb61f63c",
    "tts/chatterbox/t3_cfg.safetensors": "914cb1696f47527fe8852ca8f1fe1fa63cb34f76f9c715e84e067b744dd0da81",
    "tts/chatterbox/s3gen.safetensors": "2b78103c654207393955e4900aac14a12de8ef25f4b09424f1ef91941f161d4e",
    "tts/chatterbox/tokenizer.json": "d71e3a44eabb1784df9a68e9f95b251ecbf1a7af6a9f50835856b2ca9d8c14a5",
    "tts/chatterbox/conds.pt": "6552d70568833628ba019c6b03459e77fe71ca197d5c560cef9411bee9d87f4e",
    "tts/f5/model_1250000.safetensors": "670900fd14e6c458b95da6e9ed317cdb20dbaf7a1c02ac06a05475a9d32b6a38",
    "tts/f5/vocos/config.yaml": "da9033922f969a47f0c160010226919e59f27761fd5066f3828d46de6650b0fc",
    "tts/f5/vocos/pytorch_model.bin": "97ec976ad1fd67a33ab2682d29c0ac7df85234fae875aefcc5fb215681a91b2a",
    "whisper/large-v3-turbo.pt": "aff26ae408abcba5fbf8813c21e62b0941638c5f6eebfb145be0c9839262a19a",
}

setup_models.register_pinned(CHATTERBOX_FILES, MODEL_SHA256, "chatterbox")   # the Doctor's list (phase 10)
setup_models.register_pinned({"whisper/large-v3-turbo.pt": WHISPER_URL}, MODEL_SHA256, "whisper")
setup_models.register_pinned(F5_FILES, MODEL_SHA256, "f5")

ENGINES = {
    "chatterbox": {"name": "Chatterbox", "licence": "MIT", "addon": None, "ref_text": False, "rtf": 1.0,   # with RVC, measured phase 4
                   "vram_gb": 3.6, "size_mb": 3200, "module": "chatterbox",
                   "about": "Resemble AI's open model (MIT code and weights); closest to each person's cadence in our tests. "
                            "Every output carries Resemble's inaudible watermark."},
    "f5": {"name": "F5-TTS", "licence": "CC-BY-NC-4.0", "addon": "f5", "ref_text": True, "rtf": 3.0, "vram_gb": 0.9,
           "size_mb": 1400, "module": "f5_tts",
           "about": "Fast (3-4x realtime); flattens some intonation. Non-commercial weights: an add-on you accept."},
    "xtts": {"name": "XTTS-v2", "licence": "CPML", "addon": "xtts", "ref_text": False, "rtf": 0.84, "vram_gb": 2.1,
             "size_mb": 1900, "module": "TTS", "wired": False,
             "about": "Coqui's model; registered only (not used by SpeakEasy yet)."},
}


# Whisper's initial prompt (punctuated, conversational English): the worker's transcripts and SpeakEasy's listen-back
# hear with the same one, so a confidence threshold measured in one holds in the other
WHISPER_PROMPT = "Well, thank you. So, as I was saying, it's been a long road, hasn't it? Let me tell you why."


def engine_info(engine: str) -> dict:
    return ENGINES.get(engine) or ENGINES["chatterbox"]


def _module_present(mod: str) -> bool:
    import importlib.util
    try:
        return importlib.util.find_spec(mod) is not None
    except (ImportError, ValueError):
        return False


def engine_ready(engine: str) -> bool:
    """Can this setup read with the engine? (its packages are installed; an add-on must also be accepted). Its model
    files may still be missing: the worker downloads them on first use."""
    e = ENGINES.get(engine)
    if not e or e.get("wired") is False:
        return False
    if e["addon"] and not addons.available(e["addon"]):
        return False
    return _module_present(e["module"])


def default_engine() -> str:
    """[cadence] engine if it's usable here, else the open default (Chatterbox)."""
    want = (CONFIG.get("cadence") or {}).get("engine", "chatterbox")
    return want if engine_ready(want) else "chatterbox"


def weights_present(engine: str) -> bool:
    return all((path("models_dir") / f).exists() for f in MODEL_FILES.get(engine, {}))


# ---------------------------------------------------------------- the add-ons (non-OSI weights)

addons.register(addons.Addon(
    id="f5", name="F5-TTS", adds="Makes SpeakEasy's \"own cadence\" speech about 4x faster to make, with slightly flatter intonation.",
    licence="CC-BY-NC-4.0", licence_url="https://huggingface.co/SWivid/F5-TTS",
    licence_summary=("The code is MIT, but the model weights are Creative Commons Attribution-NonCommercial 4.0: "
                     "you may use what it makes for personal, non-commercial purposes only."),
    size_mb=1450, files=F5_FILES, sha256=MODEL_SHA256, imports=["f5_tts", "vocos", "x_transformers"], modules=["speakeasy"],
    packages=["f5-tts==1.1.22", "vocos==0.1.0", "encodec==0.1.1", "x-transformers==2.31.7", "einx==0.4.3",
              "frozendict==2.4.7", "torch-einops-utils==0.1.36", "torchdiffeq==0.2.5", "ema-pytorch==0.8.3",
              "hydra-core==1.3.7", "unidecode==1.4.0", "transformers-stream-generator==0.0.5", "loguru==0.7.3",
              "win32-setctime==1.2.0"]))
addons.register(addons.Addon(
    id="xtts", name="XTTS-v2", adds="Coqui's voice-cloning speech model. Not used by anything yet; it's listed so you know it's coming.",
    licence="CPML", licence_url="https://coqui.ai/cpml",
    licence_summary="Coqui Public Model License: non-commercial use of the model and what it makes.",
    size_mb=1900, wired=False, imports=["TTS"], packages=["coqui-tts==0.27.5"], modules=["speakeasy"]))

# the open parts the cadence engine is built from (listed in the Add-ons room; never installed or removed there)
addons.register(addons.Addon(
    id="chatterbox", name="Chatterbox", included=True, modules=["speakeasy", "doubleagent"],
    adds="Reads text in a person's own cadence (SpeakEasy) from a short clip of them.",
    licence="MIT", licence_url="https://github.com/resemble-ai/chatterbox",
    licence_summary=addons.OPEN.format("MIT") + " Code and weights. Everything it makes carries Resemble AI's inaudible watermark.",
    size_mb=3200, paths=["tts/chatterbox"], check=lambda: weights_present("chatterbox")))
addons.register(addons.Addon(
    id="whisper", name="Whisper", included=True, modules=["doubleagent"],
    adds="Writes down what's said in a voice's clips, so its cadence can be learned.",
    licence="MIT", licence_url="https://github.com/openai/whisper",
    licence_summary=addons.OPEN.format("MIT") + " Code and weights (large-v3-turbo).",
    size_mb=1600, paths=["whisper"], check=lambda: (path("models_dir") / "whisper" / "large-v3-turbo.pt").exists()))
