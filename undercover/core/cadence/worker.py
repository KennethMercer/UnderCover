"""The cadence worker process: Whisper (transcripts) and the cadence engines, apart from the server.

    python -m undercover.core.cadence.worker      (started by client.py; one JSON request per line on stdin)

Ops: ping | load {engine} | tts {engine, text, ref, ref_text, out, seed, opts} | transcribe {files, words, model}
| free {what} | quit. Replies on the original stdout; everything libraries print goes to stderr (the log).
Exits when stdin closes (the server quit or crashed), so it never outlives UnderCover.

The import stand-ins phase 3 needed on AMD's Windows torch live here only (docs/research/cadence.md, "Facts"):
no torch.distributed (encodec names ReduceOp), torchaudio.load needs torchcodec (read with soundfile instead),
setuptools 81+ has no pkg_resources (Chatterbox's watermarker), F5's package imports its trainer.
"""
import json
import os
import sys
import threading
import time
import traceback

_OUT = None   # the real stdout (main() sets it): replies only
_OUT_LOCK = threading.Lock()


def reply(**msg):
    with _OUT_LOCK:
        _OUT.write(json.dumps(msg) + "\n")
        _OUT.flush()


from ..config import path  # noqa: E402  (sets the GPU environment before torch)
import numpy as np  # noqa: E402
import soundfile as sf  # noqa: E402
import torch  # noqa: E402


def _stub(name: str, **attrs):
    import importlib.machinery
    import types
    m = types.ModuleType(name)
    m.__spec__ = importlib.machinery.ModuleSpec(name, None)
    m.__dict__.update(attrs)
    sys.modules[name] = m
    return m


def _standins():
    if not hasattr(torch.distributed, "ReduceOp"):   # encodec (under Vocos, F5) only names the enum at import
        import enum

        class _ReduceOp(enum.Enum):
            SUM = 0
            AVG = 1
        torch.distributed.ReduceOp = _ReduceOp
    try:
        import pkg_resources  # noqa: F401
    except ImportError:   # Chatterbox's watermarker (perth) only uses resource_filename
        import importlib.resources
        _stub("pkg_resources", resource_filename=lambda pkg, name: str(importlib.resources.files(pkg) / name))
    import torchaudio

    def _sf_load(f, *a, **k):   # torchaudio 2.9 hands load() to torchcodec (not installed)
        x, sr = sf.read(str(f), dtype="float32", always_2d=True)
        return torch.from_numpy(x.T.copy()), sr
    torchaudio.load = _sf_load


DEVICE = "cpu"   # main() picks the GPU


def _progress(rid):
    return lambda msg, done=0, total=0: reply(id=rid, progress=msg, done=done, total=total)


def _ensure(files: dict, what: str, rid):
    from .. import setup_models
    from .engines import MODEL_SHA256
    missing = [f for f in files if not (path("models_dir") / f).exists()]
    if missing:
        p = _progress(rid)
        setup_models.ensure(lambda f, d, t: p(f"Downloading {what} (once): {d / 1e6:.0f} of {t / 1e6:.0f} MB", d, t),
                            files={f: files[f] for f in missing}, sha256=MODEL_SHA256)


# ---------------------------------------------------------------- engines

class Chatterbox:
    sr = 24000

    def __init__(self, rid):
        from .engines import CHATTERBOX_FILES
        _ensure(CHATTERBOX_FILES, "the cadence model (about 3.2 GB)", rid)
        from chatterbox.tts import ChatterboxTTS
        self.m = ChatterboxTTS.from_local(path("models_dir") / "tts" / "chatterbox", DEVICE)
        self.sr = self.m.sr
        self.ref = None

    def __call__(self, text, ref, ref_text, seed, opts):
        exag = float(opts.get("exaggeration", 0.5))
        if self.ref != (ref, exag):   # the person's conditioning, computed once per reference clip
            self.m.prepare_conditionals(ref, exaggeration=exag)
            self.ref = (ref, exag)
        torch.manual_seed(seed)
        wav = self.m.generate(text, exaggeration=exag, cfg_weight=float(opts.get("cfg_weight", 0.3)),
                              temperature=float(opts.get("temperature", 0.8)))
        return wav.squeeze(0).cpu().numpy().astype(np.float32)


class F5:
    sr = 24000

    def __init__(self, rid):
        from .engines import F5_FILES
        _ensure(F5_FILES, "F5-TTS", rid)
        for mod, attrs in (("f5_tts.model.trainer", {"Trainer": None}), ("datasets", {"Dataset": object, "load_from_disk": None}),
                           ("wandb", {}), ("cached_path", {"cached_path": lambda p, *a, **k: p})):
            try:
                __import__(mod)
            except ImportError:
                sys.modules.pop(mod, None)
                _stub(mod, **attrs)
        from f5_tts.api import F5TTS
        m = path("models_dir") / "tts" / "f5"
        self.m = F5TTS(model="F5TTS_v1_Base", ckpt_file=str(m / "model_1250000.safetensors"),
                       vocoder_local_path=str(m / "vocos"), device=DEVICE)

    def __call__(self, text, ref, ref_text, seed, opts):
        wav, sr, _ = self.m.infer(str(ref), ref_text, text, nfe_step=int(opts.get("nfe_step", 32)), seed=seed,
                                  remove_silence=False, show_info=lambda *a, **k: None)
        self.sr = sr
        return np.asarray(wav, np.float32)


ENGINE_CLASSES = {"chatterbox": Chatterbox, "f5": F5}
LOADED: dict = {}
WHISPER: dict = {}
PROMPT = "Well, thank you. So, as I was saying, it's been a long road, hasn't it? Let me tell you why."


def engine(name, rid):
    if name not in LOADED:
        if name not in ENGINE_CLASSES:
            raise ValueError(f"Unknown cadence engine '{name}'.")
        for other in list(LOADED):   # one engine at a time (VRAM)
            del LOADED[other]
        torch.cuda.empty_cache()
        reply(id=rid, progress="Loading the cadence model")
        LOADED[name] = ENGINE_CLASSES[name](rid)
    return LOADED[name]


def whisper_model(name, rid):
    if name not in WHISPER:
        import whisper
        from .engines import WHISPER_URL
        if name == "large-v3-turbo":
            _ensure({"whisper/large-v3-turbo.pt": WHISPER_URL}, "the speech recogniser (about 1.6 GB)", rid)
        reply(id=rid, progress="Loading the speech recogniser")
        WHISPER.clear()
        WHISPER[name] = whisper.load_model(name, device=DEVICE, download_root=str(path("models_dir") / "whisper"))
    return WHISPER[name]


def load16(f) -> np.ndarray:
    from scipy.signal import resample_poly
    x, sr = sf.read(str(f), dtype="float32", always_2d=True)
    x = x.mean(1)
    if sr != 16000:
        from math import gcd
        g = gcd(sr, 16000)
        x = resample_poly(x, 16000 // g, sr // g).astype(np.float32)
    return x


# ---------------------------------------------------------------- ops

def op_tts(rid, req):
    e = engine(req["engine"], rid)
    t = time.perf_counter()
    y = e(req["text"], req["ref"], req.get("ref_text", ""), int(req.get("seed", 1234)), req.get("opts") or {})
    if DEVICE == "cuda":
        torch.cuda.synchronize()
    gen = time.perf_counter() - t
    y = np.nan_to_num(y)
    sf.write(req["out"], y, e.sr, subtype="FLOAT")
    return {"sr": e.sr, "seconds": round(len(y) / e.sr, 3), "gen_s": round(gen, 3)}


def op_transcribe(rid, req):
    m = whisper_model(req.get("model") or "large-v3-turbo", rid)
    out, files = [], req["files"]
    for k, f in enumerate(files):
        r = m.transcribe(load16(f), language="en", fp16=DEVICE == "cuda", condition_on_previous_text=False,
                         temperature=0.0, initial_prompt=PROMPT, word_timestamps=bool(req.get("words")))
        text = r["text"].strip()
        if req.get("words"):
            out.append({"text": text, "words": [[w["word"], round(w["start"], 2), round(w["end"], 2)]
                                                 for s in r["segments"] for w in s.get("words", [])]})
        else:
            out.append(text)
        if k % 10 == 9:
            reply(id=rid, progress="transcribing", done=k + 1, total=len(files))
    return {"results": out}


def op_free(rid, req):
    what = req.get("what", "all")
    if what in ("whisper", "all"):
        WHISPER.clear()
    if what in ("engine", "all"):
        LOADED.clear()
    import gc
    gc.collect()
    torch.cuda.empty_cache()
    return {}


def op_ping(rid, req):
    info = {"pid": os.getpid(), "device": DEVICE, "torch": torch.__version__}
    if DEVICE == "cuda":
        info["vram_gb"] = round(torch.cuda.memory_allocated() / 2**30, 2)
    return info


OPS = {"tts": op_tts, "transcribe": op_transcribe, "free": op_free, "ping": op_ping,
       "load": lambda rid, req: (engine(req["engine"], rid), {})[1]}


def main():
    global _OUT, DEVICE
    # replies go to the real stdout; anything else printed (libraries, progress bars) goes to the log
    _OUT = os.fdopen(os.dup(1), "w", encoding="utf-8", buffering=1)
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    _standins()
    DEVICE = "cuda" if torch.cuda.is_available() else "cpu"
    for raw in sys.stdin.buffer:
        try:
            req = json.loads(raw)
        except ValueError:
            continue
        if req.get("op") == "quit":
            break
        rid = req.get("id")
        try:
            reply(id=rid, ok=True, **OPS[req["op"]](rid, req))
        except Exception as e:   # noqa: BLE001 - reported to the app, the worker carries on
            traceback.print_exc()
            msg = str(e) or e.__class__.__name__
            if isinstance(e, torch.OutOfMemoryError) or "out of memory" in msg.lower():
                LOADED.clear()
                WHISPER.clear()
                torch.cuda.empty_cache()
                msg = "The GPU ran out of memory while making speech. " + msg[:200]
            reply(id=rid, ok=False, error=msg[:600], type=e.__class__.__name__)
    sys.stderr.flush()
    os._exit(0)   # some libraries leave threads running that would keep the process alive


if __name__ == "__main__":
    main()
