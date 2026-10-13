"""Train an RVC v2 (pitch-guided) voice from a project's kept phrases, fine-tuning the official base models.

Recipe follows upstream RVC train.py (commit 81eed5e8f6): 3.7 s slices with 0.3 s overlap, 48 Hz high-pass,
peak/raw blend normalisation; HuBERT layer-12 features (repeated x2 to 100 fps) and RMVPE pitch; AdamW
(1e-4, betas .8/.99), lr decay .999875 per epoch; losses: LSGAN + feature matching + 45 x mel L1 + KL.
Differences: bf16 autocast instead of fp16 + GradScaler (no loss scaling needed), the whole dataset lives
on the GPU, and a few held-out phrases give a validation loss and listening previews.

Files in <project>/train/: data.pt (prepared tensors), resume.pt (G, D, optimisers), e<N>.pth (inference
weights saved every save_every epochs), previews/real.wav + e<N>.wav, history.jsonl (one line per epoch).
"""
import hashlib
import json
import math
import random
import time
from pathlib import Path

import numpy as np
import torch
import torch.nn.functional as F

from ..core.config import CONFIG, path
from ..core import fsutil
from ..core.voices import ModelError, slug
from ..core.jobs import Control  # noqa: F401 - used by the training loop; callers import it from here
from ..core.timing import timed
from .project import CLIP_SR, Project

TCFG = CONFIG["training"]
SLICE, OVERLAP = 3.7, 0.3
F0_MIN, F0_MAX = 50.0, 1100.0
F0_MEL_MIN, F0_MEL_MAX = 1127 * np.log(1 + F0_MIN / 700), 1127 * np.log(1 + F0_MAX / 700)

# RVC configs (v1/40k.json, v2/32k.json, v2/48k.json at the vendored commit; v2 40k uses the v1 40k layout)
SR_CFG = {
    32000: dict(n_fft=1024, hop=320, mels=80, segment=12800, up=[10, 8, 2, 2], up_k=[20, 16, 4, 4]),
    40000: dict(n_fft=2048, hop=400, mels=125, segment=12800, up=[10, 10, 2, 2], up_k=[16, 16, 4, 4]),
    48000: dict(n_fft=2048, hop=480, mels=128, segment=17280, up=[12, 10, 2, 2], up_k=[24, 20, 4, 4]),
}
# Training length is a budget of optimizer steps (batch 8), so time is about the same whatever the dataset size.
# Measured on the 7900 XTX: ~0.3 s per step (bf16) -> quick ~5 min, auto ~40 min, long ~80 min.
LENGTHS = {"quick": 1000, "auto": 8000, "long": 16000}
SAVE_STEPS = 400   # a listening preview + inference save about every 2 minutes of training


def model_config(sr: int) -> list:
    c = SR_CFG[sr]
    return [c["n_fft"] // 2 + 1, c["segment"] // c["hop"], 192, 192, 768, 2, 6, 3, 0, "1", [3, 7, 11],
            [[1, 3, 5]] * 3, c["up"], 512, c["up_k"], 109, 256, sr]


def coarse_f0(f0: np.ndarray) -> np.ndarray:
    mel = 1127 * np.log(1 + f0 / 700)
    v = mel > 0
    mel[v] = (mel[v] - F0_MEL_MIN) * 254 / (F0_MEL_MAX - F0_MEL_MIN) + 1
    return np.rint(np.clip(mel, 1, 255)).astype(np.int64)


# ================================================================ data preparation

def _norm(x: np.ndarray) -> np.ndarray | None:
    m = np.abs(x).max()
    if not np.isfinite(m) or m <= 0 or m > 2.5:
        return None
    return (x / m * (0.9 * 0.75) + 0.25 * x).astype(np.float32)


def _load_clip(proj: Project, pid: str, sr: int) -> np.ndarray:
    import soundfile as sf
    from scipy import signal
    x, csr = sf.read(str(proj.clip_path(pid)), dtype="float32")
    if x.ndim > 1:
        x = x.mean(1)
    bh, ah = signal.butter(N=5, Wn=48, btype="high", fs=csr)
    x = signal.lfilter(bh, ah, x).astype(np.float32)
    if csr != sr:
        g = math.gcd(sr, csr)
        x = signal.resample_poly(x, sr // g, csr // g).astype(np.float32)
    return x


def _resample(x: np.ndarray, src: int, dst: int) -> np.ndarray:
    from scipy import signal
    g = math.gcd(src, dst)
    return signal.resample_poly(x, dst // g, src // g).astype(np.float32)


@torch.no_grad()
def _features(hubert, rmvpe, wav16: np.ndarray, device, half: bool):
    """-> (phone (T100, 768) fp16, pitch coarse (T100,), pitchf (T100,)) at 100 frames/s."""
    w = torch.from_numpy(wav16).to(device, torch.float16 if half else torch.float32)[None]
    feats = hubert.extract(w, "v2")[0].float()                     # (T50, 768)
    phone = feats.repeat_interleave(2, 0)                            # upstream: np.repeat(phone, 2, axis=0)
    f0 = rmvpe.infer_from_audio(wav16, thred=0.03)
    return phone.half().cpu(), feats.half().cpu(), f0.astype(np.float32)


def _phone(x: dict) -> torch.Tensor:
    """Training-rate (100 frames/s) content features of a prepared item, from either data.pt format."""
    if "phone50" in x:
        return x["phone50"].repeat_interleave(2, 0)[: len(x["pitch"])]
    return x["phone"]


def index_feats(data: dict) -> torch.Tensor:
    """The vectors for the voice's .index: the training pieces' 50 frames/s features."""
    if "feats" in data:
        return data["feats"]
    return torch.cat([i["phone50"] for i in data["items"]])


def choose_holdout(proj: Project) -> list[str]:
    kept = [p for p in proj.phrases if p.kept]
    good = sorted([p for p in kept if 3.0 <= p.dur <= 8.0], key=lambda p: -p.score)
    picked, used = [], set()
    for p in good:  # spread over sources first
        if p.source not in used:
            picked.append(p.id)
            used.add(p.source)
        if len(picked) >= TCFG["holdout_phrases"]:
            return picked
    for p in good:
        if p.id not in picked:
            picked.append(p.id)
        if len(picked) >= TCFG["holdout_phrases"]:
            break
    return picked


def data_key_for(proj: Project, sr: int) -> tuple[str, list[str], list[str]]:
    """(key, kept ids, holdout ids) for the current sample selection; the key changes when samples change."""
    kept = sorted(p.id for p in proj.phrases if p.kept)
    # a stored holdout whose phrases are all left out now (e.g. speech dropped for singing) would leave an empty set,
    # and an empty set used to be re-chosen on the next call: a different key, so a paused run restarted from zero
    holdout = [h for h in (proj.train.get("holdout") or []) if h in kept] or [h for h in choose_holdout(proj) if h in kept]
    key = f"{sr}:" + hashlib.sha1("|".join(kept + ["#"] + holdout).encode()).hexdigest()[:16]
    return key, kept, holdout


def prepare(proj: Project, hubert, rmvpe, sr: int, device, half: bool, progress=None) -> Path:
    """Slice, normalise and featurise the kept phrases. Cached by the exact sample set + sample rate."""
    report = progress or (lambda f: None)
    if any(p.reason == "not checked yet" for p in proj.phrases) and proj.reference.get("clip"):
        from .project import lock, score
        with lock(proj.name):   # phrases from an interrupted run: check them against the voice before training
            cur = Project.load(proj.name)
            score(cur)
            cur.save()
        proj.phrases = cur.phrases
    if sum(p.dur for p in proj.phrases if p.kept) < 60:
        raise ModelError("At least 1 minute of kept samples is needed to train (10+ minutes is better).")
    key, kept, holdout = data_key_for(proj, sr)
    tdir = proj.dir / "train"
    out = tdir / "data.pt"
    if out.exists() and proj.train.get("data_key") == key:
        return out
    tdir.mkdir(parents=True, exist_ok=True)
    c = SR_CFG[sr]
    items, hold = [], []
    train_ids = [k for k in kept if k not in holdout]
    with timed("train.prepare", phrases=len(kept), sr=sr):
        for n, pid in enumerate(holdout + train_ids):
            x = _load_clip(proj, pid, sr)
            if pid in holdout:
                pieces = [x]
            else:
                pieces, step, start = [], int(sr * (SLICE - OVERLAP)), 0
                while True:
                    if len(x) - start > (SLICE + OVERLAP) * sr:
                        pieces.append(x[start:start + int(SLICE * sr)])
                        start += step
                    else:
                        pieces.append(x[start:])
                        break
            for piece in pieces:
                piece = _norm(piece)
                if piece is None or len(piece) < sr * 0.5:
                    continue
                w16 = _resample(piece, sr, 16000)
                phone, feats50, f0 = _features(hubert, rmvpe, w16, device, half)
                n_frames = min(phone.shape[0], len(f0), len(piece) // c["hop"])
                if n_frames < c["segment"] // c["hop"] + 2:
                    continue
                # HuBERT features once, at their own 50 frames/s: the 100 frames/s training copy is just each frame
                # twice (expanded when loaded) and the index vectors are these same frames (data.pt ~halves)
                item = {"wav": torch.from_numpy(piece[:n_frames * c["hop"]]).half(),
                        "phone50": feats50, "pitch": torch.from_numpy(coarse_f0(f0[:n_frames])),
                        "pitchf": torch.from_numpy(f0[:n_frames])}
                if pid in holdout:
                    item["id"] = pid
                    hold.append(item)
                else:
                    items.append(item)
            report((n + 1) / len(kept))
    if not items:
        raise ModelError("None of the kept samples could be prepared for training.")
    fsutil.torch_save({"items": items, "holdout": hold, "sr": sr, "format": 2}, out)
    proj.train["data_key"] = key
    proj.train["holdout"] = holdout
    proj.train["pieces"] = len(items)
    proj.train["train_minutes"] = round(sum(len(i["wav"]) for i in items) / sr / 60, 2)
    proj.save_train()
    return out


# ================================================================ training

def _batches(items: list, bs: int) -> list[list[int]]:
    """Shuffle, then group similar lengths (like RVC's bucket sampler) so padding stays small."""
    idx = list(range(len(items)))
    random.shuffle(idx)
    out = []
    for s in range(0, len(idx), bs * 8):
        block = sorted(idx[s:s + bs * 8], key=lambda i: items[i]["n"])
        out += [block[j:j + bs] for j in range(0, len(block), bs)]
    random.shuffle(out)
    return [b for b in out if len(b) >= max(2, bs // 2)] or out


def _collate(items: list, ids: list[int], device):
    b = [items[i] for i in ids]
    T = max(x["n"] for x in b)
    hop = b[0]["hop"]
    B = len(b)
    phone = torch.zeros(B, T, 768, device=device)
    pitch = torch.zeros(B, T, dtype=torch.long, device=device)
    pitchf = torch.zeros(B, T, device=device)
    spec = torch.zeros(B, b[0]["spec"].shape[0], T, device=device)
    wave = torch.zeros(B, 1, T * hop, device=device)
    lens = torch.tensor([x["n"] for x in b], device=device)
    for k, x in enumerate(b):
        n = x["n"]
        phone[k, :n] = x["phone"]
        pitch[k, :n] = x["pitch"]
        pitchf[k, :n] = x["pitchf"]
        spec[k, :, :n] = x["spec"]
        wave[k, 0, :n * hop] = x["wav"]
    return phone, lens, pitch, pitchf, spec, lens, wave, torch.zeros(B, dtype=torch.long, device=device)


def _export_weights(net_g, sr: int, epoch: int, dest: Path):
    sd = {k: v.detach().half().cpu() for k, v in net_g.state_dict().items() if "enc_q" not in k}
    fsutil.torch_save({"weight": sd, "config": model_config(sr), "info": f"{epoch}epoch", "sr": f"{sr // 1000}k",
                "f0": 1, "version": "v2"}, dest)


class Trainer:
    def __init__(self, proj: Project, hubert, rmvpe, device="cuda", half=True, converter=None):
        self.proj, self.hubert, self.rmvpe = proj, hubert, rmvpe
        self.converter = converter   # with it, every save is judged by converting other people's phrases (_probe)
        self._probe_ok = None
        self.device = torch.device(device)
        self.half = half
        self.tdir = proj.dir / "train"
        (self.tdir / "previews").mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------ helpers
    def _hist(self) -> list[dict]:
        f = self.tdir / "history.jsonl"
        if not f.exists():
            return []
        return [json.loads(l) for l in f.read_text(encoding="utf-8").splitlines() if l.strip()]

    @torch.no_grad()
    def _validate(self, net_g, hold: list, c: dict, sr: int, seed=0) -> float:
        from ..core.engine.rvc.train_parts import mel_spectrogram_torch
        if not hold:
            return float("nan")
        net_g.eval()
        state = torch.random.get_rng_state()
        torch.manual_seed(seed)
        losses = []
        for x in hold:
            n = x["n"]
            o = net_g.infer(x["phone"][None], torch.tensor([n], device=self.device), x["pitch"][None],
                            x["pitchf"][None], torch.zeros(1, dtype=torch.long, device=self.device))[0][0, 0].float()
            m1 = mel_spectrogram_torch(o[None], c["n_fft"], c["mels"], sr, c["hop"], c["n_fft"])
            m2 = mel_spectrogram_torch(x["wav"][None].float(), c["n_fft"], c["mels"], sr, c["hop"], c["n_fft"])
            t = min(m1.shape[-1], m2.shape[-1])
            losses.append(F.l1_loss(m1[..., :t], m2[..., :t]).item())
        torch.random.set_rng_state(state)
        net_g.train()
        return float(np.mean(losses))

    @torch.no_grad()
    def _preview(self, net_g, x: dict, sr: int, dest: Path):
        import soundfile as sf
        net_g.eval()
        torch.manual_seed(0)
        o = net_g.infer(x["phone"][None], torch.tensor([x["n"]], device=self.device), x["pitch"][None],
                        x["pitchf"][None], torch.zeros(1, dtype=torch.long, device=self.device))[0][0, 0]
        net_g.train()
        o = o.float().cpu().numpy()
        peak = np.abs(o).max()
        sf.write(str(dest), o / peak * 0.9 if peak > 0.9 else o, sr, subtype="PCM_16")

    # ------------------------------------------------------------ main loop
    def run(self, control: Control, progress=None) -> dict:
        """Train until the epoch budget, early stop (auto), pause or stop. Returns proj.train."""
        from ..core.engine.rvc import commons
        from ..core.engine.rvc.synth import SynthesizerTrnMs768NSFsid
        from ..core.engine.rvc.train_parts import (MultiPeriodDiscriminatorV2, discriminator_loss, feature_loss,
                                                   generator_loss, kl_loss, mel_spectrogram_torch, spec_to_mel_torch,
                                                   spectrogram_torch)
        from ..core import setup_models
        if self.device.type != "cuda" or not torch.cuda.is_available():
            raise ModelError("Training a voice needs a supported graphics card. On the processor it would take days. "
                             "Covers with existing voices still work.")
        proj, st = self.proj, self.proj.train
        report = progress or (lambda **kw: None)
        sr = int(st.get("sr") or TCFG["sample_rate"])
        c = SR_CFG[sr]
        st["sr"] = sr
        length = st.get("length", "auto")
        bs = int(st.get("batch_size") or TCFG["batch_size"])

        report(stage="preparing", fraction=0.0, note="downloading base models")
        base = setup_models.base_model_files(sr)
        setup_models.ensure(files=base)
        report(stage="preparing", fraction=0.0, note="preparing samples")
        def prep_progress(f):
            control.service()   # covers queued meanwhile don't wait for data preparation either
            report(stage="preparing", fraction=f, note="preparing samples")
        data_file = prepare(proj, self.hubert, self.rmvpe, sr, self.device, self.half, progress=prep_progress)
        data = torch.load(data_file, map_location="cpu", weights_only=True)

        dev = self.device
        def to_dev(x):
            spec = spectrogram_torch(x["wav"].float().to(dev)[None], c["n_fft"], c["hop"], c["n_fft"])[0]
            phone = _phone(x)
            n = min(spec.shape[-1], phone.shape[0])
            return {"wav": x["wav"].float().to(dev)[:n * c["hop"]], "phone": phone.float().to(dev)[:n],
                    "pitch": x["pitch"].to(dev)[:n], "pitchf": x["pitchf"].to(dev)[:n],
                    "spec": spec[:, :n], "n": n, "hop": c["hop"]}
        items = [to_dev(x) for x in data["items"]]
        hold = [to_dev(x) for x in data["holdout"]]

        torch.manual_seed(1234)
        random.seed(1234)
        net_g = SynthesizerTrnMs768NSFsid(*model_config(sr), is_half=False).to(dev)
        net_d = MultiPeriodDiscriminatorV2().to(dev)
        # fused AdamW is a native kernel (not Triton) and works on ROCm Windows: optimizer time 0.9 -> 0.2 s/epoch (~8%)
        fused = dev.type == "cuda"
        opt_g = torch.optim.AdamW(net_g.parameters(), 1e-4, betas=(0.8, 0.99), eps=1e-9, fused=fused)
        opt_d = torch.optim.AdamW(net_d.parameters(), 1e-4, betas=(0.8, 0.99), eps=1e-9, fused=fused)
        resume = self.tdir / "resume.pt"
        if resume.exists() and st.get("epoch", 0) > 0 and st.get("data_key_trained") == st.get("data_key"):
            ck = torch.load(resume, map_location="cpu", weights_only=True)
            net_g.load_state_dict(ck["g"])
            net_d.load_state_dict(ck["d"])
            opt_g.load_state_dict(ck["opt_g"])
            opt_d.load_state_dict(ck["opt_d"])
            epoch0 = int(ck["epoch"])
            if st.get("epoch", 0) > epoch0:   # crashed after the last save point: forget the lost epochs
                hf = self.tdir / "history.jsonl"
                kept_hist = [h for h in self._hist() if h["epoch"] <= epoch0]
                hf.write_text("".join(json.dumps(h) + "\n" for h in kept_hist), encoding="utf-8")
                st["saves"] = [s for s in st.get("saves", []) if s["epoch"] <= epoch0]
                if (st.get("best") or 0) > epoch0:
                    st["best"] = max((s["epoch"] for s in st["saves"]), default=None)
                st["epoch"] = epoch0
        else:
            mdir = path("models_dir")
            g = torch.load(mdir / f"rvc/pretrained_v2/f0G{sr // 1000}k.pth", map_location="cpu", weights_only=True)["model"]
            d = torch.load(mdir / f"rvc/pretrained_v2/f0D{sr // 1000}k.pth", map_location="cpu", weights_only=True)["model"]
            net_g.load_state_dict(g)
            net_d.load_state_dict(d)
            epoch0 = 0
            for f in (self.tdir / "history.jsonl", *self.tdir.glob("e*.pth"), *(self.tdir / "previews").glob("e*.wav")):
                f.unlink(missing_ok=True)
            st.update(epoch=0, best=None, saves=[], resume_step=0, data_key_trained=st.get("data_key"))
            if hold:
                import soundfile as sf
                sf.write(str(self.tdir / "previews" / "real.wav"), hold[0]["wav"].cpu().numpy(), sr, subtype="PCM_16")
        lr = 1e-4 * (0.999875 ** epoch0)
        for o in (opt_g, opt_d):
            for pg in o.param_groups:
                pg["lr"] = lr
        net_g.train()
        net_d.train()
        seg = c["segment"] // c["hop"]
        hist = self._hist()
        best = min((h["val"] for h in hist if h.get("val") == h.get("val")), default=float("inf"))
        steps_per_epoch = len(_batches(items, bs))
        if not st.get("epochs"):   # fixed when the run starts, so resuming keeps the same plan
            st["epochs"] = int(np.clip(round(LENGTHS.get(length, LENGTHS["auto"]) / steps_per_epoch), 20, 2000))
        max_epochs = int(st["epochs"])
        save_every = max(1, round(SAVE_STEPS / steps_per_epoch))
        patience = max(10, max_epochs // 4)     # auto: stop when held-out loss hasn't improved for a quarter of the run
        st.update(state="training", steps_per_epoch=steps_per_epoch, error="")
        self._reprobe(st, sr)
        proj.save_train()
        # bf16 on ROCm (always, as before) and on NVIDIA cards with native bf16 (RTX 30+). Older NVIDIA cards
        # train in fp32: slower, but no loss scaling needed and nothing to destabilise.
        bf16 = torch.version.hip is not None or torch.cuda.is_bf16_supported(including_emulation=False)
        ac = dict(device_type="cuda", dtype=torch.bfloat16, enabled=self.half and dev.type == "cuda" and bf16)
        t_start = time.time()
        epoch = epoch0
        outcome = "done"
        with timed("train.run", project=proj.name, from_epoch=epoch0, pieces=len(items), batch=bs) as rec:
            while epoch < max_epochs:
                epoch += 1
                t0 = time.time()
                sums = {"mel": 0.0, "kl": 0.0, "fm": 0.0, "gen": 0.0, "disc": 0.0}
                batches = _batches(items, bs)
                for bi, ids in enumerate(batches):
                    control.service()
                    if control.wants_out():
                        break
                    phone, plen, pitch, pitchf, spec, slen, wave, sid = _collate(items, ids, dev)
                    with torch.autocast(**ac):
                        y_hat, ids_slice, x_mask, z_mask, (z, z_p, m_p, logs_p, m_q, logs_q) = net_g(
                            phone, plen, pitch, pitchf, spec, slen, sid)
                    with torch.autocast(device_type="cuda", enabled=False):
                        mel = spec_to_mel_torch(spec, c["n_fft"], c["mels"], sr)
                        y_mel = commons.slice_segments(mel, ids_slice, seg)
                        y_hat_mel = mel_spectrogram_torch(y_hat.float().squeeze(1), c["n_fft"], c["mels"], sr,
                                                          c["hop"], c["n_fft"])
                        wave_s = commons.slice_segments(wave, ids_slice * c["hop"], c["segment"])
                    with torch.autocast(**ac):
                        y_d_r, y_d_g, _, _ = net_d(wave_s, y_hat.detach())
                    loss_disc = discriminator_loss(y_d_r, y_d_g)
                    opt_d.zero_grad(set_to_none=True)
                    loss_disc.backward()
                    opt_d.step()
                    with torch.autocast(**ac):
                        y_d_r, y_d_g, fmap_r, fmap_g = net_d(wave_s, y_hat)
                    loss_mel = F.l1_loss(y_mel, y_hat_mel.float()) * 45
                    loss_kl = kl_loss(z_p, logs_q, m_p, logs_p, z_mask)
                    loss_fm = feature_loss(fmap_r, fmap_g)
                    loss_gen = generator_loss(y_d_g)
                    loss = loss_gen + loss_fm + loss_mel + loss_kl
                    if not torch.isfinite(loss):
                        raise ModelError("Training became unstable (the loss went to infinity). "
                                         "Try again; if it repeats, remove noisy samples.")
                    opt_g.zero_grad(set_to_none=True)
                    loss.backward()
                    opt_g.step()
                    for k, v in (("mel", loss_mel), ("kl", loss_kl), ("fm", loss_fm), ("gen", loss_gen), ("disc", loss_disc)):
                        sums[k] += float(v.detach())
                    if bi % 5 == 0:
                        left = max_epochs - epoch + 1 - (bi + 1) / len(batches)   # epochs still to run
                        if st.get("sec_per_epoch") and epoch - epoch0 > 1:
                            eta = st["sec_per_epoch"] * left                   # recent epochs: no warm-up skew
                        else:
                            done = epoch - 1 - epoch0 + (bi + 1) / len(batches)
                            eta = (time.time() - t_start) / max(done, 1e-6) * left
                        report(stage="training", epoch=epoch, epochs=max_epochs,
                               fraction=(epoch - 1 + (bi + 1) / len(batches)) / max_epochs, eta=eta,
                               sec_per_epoch=st.get("sec_per_epoch"))
                if control.wants_out() and bi < len(batches) - 1:
                    epoch -= 1   # partial epoch: don't count it
                    outcome = "paused" if control.pause else "stopped"
                    break
                for o in (opt_g, opt_d):
                    for pg in o.param_groups:
                        pg["lr"] *= 0.999875
                n = max(1, len(batches))
                val = self._validate(net_g, hold, c, sr)
                h = {"epoch": epoch, "secs": round(time.time() - t0, 2), "val": round(val, 4),
                     **{k: round(v / n, 4) for k, v in sums.items()}}
                hist.append(h)
                with open(self.tdir / "history.jsonl", "a", encoding="utf-8") as f:
                    f.write(json.dumps(h) + "\n")
                st["epoch"] = epoch
                st["sec_per_epoch"] = round(float(np.median([x["secs"] for x in hist[-10:]])), 2)
                st["sec_per_step"] = round(st["sec_per_epoch"] / steps_per_epoch, 3)
                saved = epoch % save_every == 0 or epoch == max_epochs
                if saved:
                    _export_weights(net_g, sr, epoch, self.tdir / f"e{epoch}.pth")
                    if hold:
                        self._preview(net_g, hold[0], sr, self.tdir / "previews" / f"e{epoch}.wav")
                    smooth = float(np.mean([x["val"] for x in hist[-5:]]))
                    st.setdefault("saves", []).append({"epoch": epoch, "val": round(smooth, 4)})
                    probe = self._probe(epoch, sr)
                    if probe:
                        st["saves"][-1].update(probe)
                        self._pick_best(st)
                    elif smooth < best:
                        best = smooth
                        st["best"] = epoch
                    # the resume point is 1.3 GB: refresh it about every 2,000 steps (and always on pause), not every save
                    if epoch * steps_per_epoch - st.get("resume_step", 0) >= 2000 or epoch == max_epochs:
                        fsutil.torch_save({"g": net_g.state_dict(), "d": net_d.state_dict(), "opt_g": opt_g.state_dict(),
                                    "opt_d": opt_d.state_dict(), "epoch": epoch}, resume)
                        st["resume_step"] = epoch * steps_per_epoch
                    self._prune_saves()
                proj.save_train()
                report(stage="training", epoch=epoch, epochs=max_epochs, fraction=epoch / max_epochs,
                       eta=st["sec_per_epoch"] * (max_epochs - epoch), saved=saved, history=h,
                       sec_per_epoch=st["sec_per_epoch"])
                # auto: stop once held-out loss hasn't improved for 100 epochs (after at least 150)
                if length == "auto" and epoch >= max_epochs // 2 and st.get("best") and epoch - st["best"] >= patience:
                    outcome = "early stop"
                    break
                if control.wants_out():
                    outcome = "paused" if control.pause else "stopped"
                    break
            rec.update(to_epoch=epoch, outcome=outcome)

        if outcome == "paused":
            fsutil.torch_save({"g": net_g.state_dict(), "d": net_d.state_dict(), "opt_g": opt_g.state_dict(),
                        "opt_d": opt_d.state_dict(), "epoch": epoch}, resume)
            st["state"] = "paused"
        else:
            if epoch > 0 and not (self.tdir / f"e{epoch}.pth").exists():
                _export_weights(net_g, sr, epoch, self.tdir / f"e{epoch}.pth")
                if hold:
                    self._preview(net_g, hold[0], sr, self.tdir / "previews" / f"e{epoch}.wav")
                st.setdefault("saves", []).append({"epoch": epoch, "val": round(float(np.mean([x["val"] for x in hist[-5:]])), 4)})
            st["state"] = "trained"
            st["outcome"] = outcome
        proj.save_train()
        del net_g, net_d, opt_g, opt_d, items, hold
        torch.cuda.empty_cache()
        return st

    def _probe(self, epoch: int, sr: int) -> dict | None:
        """Judge a save the way it'll be used: convert other speakers' phrases from the project and measure likeness to
        the target (WeSpeaker), pitch accuracy and how much of the words survive (voicecheck). Held-out mel loss can't
        do this (it measures rebuilding the target's own phrases, and goes flat early)."""
        if not self.converter or self._probe_ok is False:
            return None
        from . import voicecheck
        from ..core.engine.convert import LoadedVoice
        from ..core.voices import Voice
        from ..core.speaker import SpeakerEmbedder
        try:
            if not hasattr(self, "_emb"):
                self._emb = SpeakerEmbedder()
            v = Voice(name=f"{self.proj.name}-e{epoch}", dir=self.tdir, pth=self.tdir / f"e{epoch}.pth", index=None,
                      version="v2", sr=sr)
            if not hasattr(self, "_tests"):   # one fixed test per run, so saves are compared like for like
                self._tests = voicecheck.test_phrases(self.proj)
            lv = LoadedVoice(v, self.converter.device, self.converter.half)
            q = voicecheck.check(lv, self.proj, self.converter, self._emb, tests=self._tests)
            del lv
            self._probe_ok = True
            return {"identity": q.score, "low": q.low, "cents": q.pitch_cents, "content": q.content, "test": q.test}
        except ValueError:   # no other speakers in this project's clips: fall back to the held-out loss
            self._probe_ok = False
            return None

    def check_last(self, st: dict):
        """Do it all's "Checking it": the last save, when nothing measured it (an early stop's falls between the
        regular saves), measured against real clips like the others, so it can be picked too."""
        last = next((s for s in st.get("saves", []) if s["epoch"] == st.get("epoch")), None)
        if last is None or last.get("identity") is not None or not (self.tdir / f"e{last['epoch']}.pth").exists():
            return
        probe = self._probe(last["epoch"], st.get("sr") or CONFIG["training"]["sample_rate"])
        if probe:
            last.update(probe)
            self._pick_best(st)
            self.proj.save_train()

    def _reprobe(self, st: dict, sr: int):
        """Resuming: saves measured with a different test (e.g. speech before the project had a sung reference)
        can't be compared with new ones, so measure them again with this run's test."""
        if not self.converter or not st.get("saves"):
            return
        from . import voicecheck
        try:
            kind = voicecheck.test_phrases(self.proj)[2]
        except Exception:
            return
        stale = [s for s in st["saves"] if s.get("identity") is not None and s.get("test", "speech") != kind
                 and (self.tdir / f"e{s['epoch']}.pth").exists()]
        for s in stale:
            probe = self._probe(s["epoch"], sr)
            if probe:
                s.update(probe)
        if stale:
            self._pick_best(st)

    def _pick_best(self, st: dict):
        """Best save = most like the target among saves that still keep the words and the melody (content within 2%
        of the best content, pitch error within 10 cents of the best). Falls back to the held-out loss."""
        probed = [s for s in st.get("saves", []) if s.get("identity") is not None]
        kinds = {s.get("test", "speech") for s in probed}
        if len(kinds) > 1:   # never compare a sung score with a speech one: use the newer test's saves
            probed = [s for s in probed if s.get("test", "speech") == probed[-1].get("test", "speech")]
        if not probed:
            return
        top_c = max(s["content"] for s in probed)
        top_p = min(s["cents"] for s in probed if s.get("cents") is not None) if any(s.get("cents") is not None for s in probed) else None
        ok = [s for s in probed if s["content"] >= top_c - 0.02 and (top_p is None or s.get("cents") is None or s["cents"] <= top_p + 10)]
        st["best"] = max(ok or probed, key=lambda s: s["identity"])["epoch"]
        st["best_by"] = "conversion test"

    def _prune_saves(self, keep_last: int = 8):
        """Keep the best save and the last few (each inference save is ~55 MB)."""
        st = self.proj.train
        epochs = [s["epoch"] for s in st.get("saves", [])]
        keep = set(epochs[-keep_last:]) | {st.get("best")} | set(st.get("pinned", []))
        for s in list(st.get("saves", [])):
            if s["epoch"] not in keep:
                (self.tdir / f"e{s['epoch']}.pth").unlink(missing_ok=True)
                (self.tdir / "previews" / f"e{s['epoch']}.wav").unlink(missing_ok=True)
                st["saves"].remove(s)


# ================================================================ export to the voice library

def f0_range(data: dict) -> list | None:
    """[5th, 50th, 95th percentile] of the voiced pitch (Hz) the voice was trained on: the range it can render."""
    f0 = torch.cat([i["pitchf"].float() for i in data["items"]])
    f0 = f0[f0 > 0]
    if len(f0) < 100:
        return None
    return [round(float(torch.quantile(f0, q)), 1) for q in (0.05, 0.5, 0.95)]


def f0_median(data: dict) -> float | None:
    """Median pitch (Hz) of the voiced frames the voice was trained on; used to pick a cover's octave."""
    f0 = torch.cat([i["pitchf"].float() for i in data["items"]])
    f0 = f0[f0 > 0]
    return round(float(f0.median()), 1) if len(f0) else None


def export_voice(proj: Project, epoch: int | None = None, name: str | None = None, overwrite: bool = False):
    """Install a saved epoch (default: best) plus a freshly built index as a normal UnderCover voice."""
    from ..core.voices import install
    from ..core.engine.rvc.index import write_ivf_flat
    st = proj.train
    epoch = int(epoch or st.get("best") or st.get("epoch") or 0)
    tdir = proj.dir / "train"
    pth = tdir / f"e{epoch}.pth"
    if not pth.exists():
        raise ModelError(f"There's no save from epoch {epoch}. Pick one of the saves listed.")
    name = slug(name or st.get("voice_name") or proj.name)
    data_pt = tdir / "data.pt"
    if not data_pt.exists() and (proj.dir / "before-smaller" / "train" / "data.pt").exists():
        data_pt = proj.dir / "before-smaller" / "train" / "data.pt"   # Make smaller set it aside (phase 24): still there
    if not data_pt.exists():
        raise ModelError("This project's prepared data was cleared to save space (SafeHouse, Make smaller). Train more "
                         "rebuilds it (a few minutes), then this save can be added.")
    data = torch.load(data_pt, map_location="cpu", weights_only=True)
    feats = index_feats(data).float().numpy()
    if len(feats) > 200_000:  # RVC: shrink very large sets to 10k k-means centres first
        from sklearn.cluster import MiniBatchKMeans
        feats = MiniBatchKMeans(n_clusters=10000, batch_size=256 * 8, n_init=1, random_state=0).fit(feats).cluster_centers_
    with timed("train.index", vectors=len(feats)):
        idx = tdir / f"added_{name}.index"
        write_ivf_flat(idx, feats)
    mins = st.get("train_minutes", 0)
    v = install([pth, idx], name, source=f"trained in UnderCover: {proj.person}, {mins:.1f} min, epoch {epoch}",
                overwrite=overwrite)
    likeness = next((s.get("identity") for s in st.get("saves", []) if s["epoch"] == epoch), None)
    v.extra.update(trained=True, project=proj.name, epoch=epoch, train_minutes=mins, likeness=likeness,
                   f0_median=f0_median(data), f0_range=f0_range(data), from_speech=proj.from_speech())
    if (proj.person or "").strip():   # who it is for, whatever the source text looks like ("Earth, Wind & Fire")
        v.extra["person"] = proj.person.strip()
    fsutil.write_text(v.dir / "meta.json", json.dumps(v.to_json(), indent=2))
    idx.unlink(missing_ok=True)
    fsutil.link_duplicate(v.pth, pth)   # the exported save is the voice's own model.pth: one copy on disk (phase 24)
    st.update(exported=name, exported_epoch=epoch, state="exported")
    (tdir / "resume.pt").unlink(missing_ok=True)   # 1.3 GB; a finished voice doesn't need it (retraining starts fresh)
    # keep the exported save and the two best alternatives (to try another on the Train tab); the rest are 55 MB each
    alts = sorted([s for s in st.get("saves", []) if s["epoch"] != epoch and s.get("identity") is not None],
                  key=lambda s: -s["identity"])[:2]
    keep = {epoch} | {s["epoch"] for s in alts} | set(st.get("pinned", []))
    for s in list(st.get("saves", [])):
        if s["epoch"] not in keep:
            (tdir / f"e{s['epoch']}.pth").unlink(missing_ok=True)
            (tdir / "previews" / f"e{s['epoch']}.wav").unlink(missing_ok=True)
            st["saves"].remove(s)
    proj.save_train()
    return v
