"""RVC feature index without faiss.

RVC ships `added_*.index` files written by faiss (IndexIVFFlat). For retrieval RVC only needs the raw
stored vectors and an exact-ish k-NN, so we parse the file ourselves and do brute-force k-NN on the GPU
(exact, and fast: ~100k x 768 vectors). This avoids a faiss dependency entirely.
"""
import os
import struct
from pathlib import Path

import numpy as np
import torch


class IndexFormatError(Exception):
    pass


class _Reader:
    def __init__(self, data: bytes):
        self.b, self.p = data, 0

    def take(self, n: int) -> bytes:
        if self.p + n > len(self.b):
            raise IndexFormatError("unexpected end of file")
        out = self.b[self.p:self.p + n]
        self.p += n
        return out

    def fourcc(self) -> bytes:
        return self.take(4)

    def i32(self): return struct.unpack("<i", self.take(4))[0]
    def i64(self): return struct.unpack("<q", self.take(8))[0]
    def u64(self): return struct.unpack("<Q", self.take(8))[0]
    def u8(self): return self.take(1)[0]

    def header(self):
        d, ntotal = self.i32(), self.i64()
        self.i64(); self.i64()  # dummies
        self.u8()               # is_trained
        metric = self.i32()
        if metric > 1:
            self.take(4)        # metric_arg
        return d, ntotal


def read_ivf_flat(path: Path) -> np.ndarray:
    """Return all stored vectors (ntotal, d) float32 from a faiss IndexIVFFlat file."""
    r = _Reader(Path(path).read_bytes())
    if r.fourcc() != b"IwFl":
        raise IndexFormatError("not an IVF-Flat index (only RVC-style IVF Flat indexes are supported)")
    d, ntotal = r.header()
    nlist, _nprobe = r.u64(), r.u64()
    # coarse quantizer (IndexFlat): header + centroid vector, which we skip
    if r.fourcc() not in (b"IxF2", b"IxFI", b"IxFl"):
        raise IndexFormatError("unsupported quantizer")
    qd, _ = r.header()
    n = r.u64()
    r.take(n * 4 if n == nlist * qd else n)  # count is in floats (newer faiss) or bytes
    # direct map
    dm_type = r.u8()
    r.take(r.u64() * 8)
    if dm_type == 2:  # hashtable
        r.take(r.u64() * 16)
    if r.fourcc() != b"ilar":
        raise IndexFormatError("unexpected inverted-list format")
    il_nlist, code_size = r.u64(), r.u64()
    if code_size != d * 4:
        raise IndexFormatError(f"code size {code_size} != d*4 ({d * 4})")
    kind = r.fourcc()
    if kind == b"full":
        sizes = [r.u64() for _ in range(r.u64())]
    elif kind == b"sprs":
        flat = [r.u64() for _ in range(r.u64())]
        sizes = [0] * il_nlist
        for no, sz in zip(flat[0::2], flat[1::2]):
            sizes[no] = sz
    else:
        raise IndexFormatError(f"unsupported inverted-list type {kind!r}")
    chunks = []
    for sz in sizes:
        if sz:
            chunks.append(np.frombuffer(r.take(sz * code_size), dtype=np.float32).reshape(sz, d))
            r.take(sz * 8)  # ids
    vecs = np.concatenate(chunks) if chunks else np.zeros((0, d), np.float32)
    if len(vecs) != ntotal:
        raise IndexFormatError(f"read {len(vecs)} vectors, header says {ntotal}")
    return vecs


def write_ivf_flat(path: Path, vecs: np.ndarray, seed: int = 0) -> int:
    """Write vectors as a faiss IndexIVFFlat file (L2), the layout RVC's `added_*.index` uses, so the result
    loads here and in faiss-based tools. Lists come from k-means with RVC's nlist rule; returns nlist."""
    from sklearn.cluster import MiniBatchKMeans
    vecs = np.ascontiguousarray(vecs, dtype=np.float32)
    n, d = vecs.shape
    nlist = max(1, min(int(16 * np.sqrt(n)), n // 39))
    if nlist > 1:
        km = MiniBatchKMeans(n_clusters=nlist, random_state=seed, batch_size=256 * 8, n_init=1, max_iter=50)
        assign = km.fit_predict(vecs)
        centroids = km.cluster_centers_.astype(np.float32)
    else:
        assign = np.zeros(n, np.int64)
        centroids = vecs.mean(0, keepdims=True)

    def header(dim, total):  # d, ntotal, two dummies (faiss writes 1<<20), is_trained, metric L2
        return struct.pack("<iqqqBi", dim, total, 1 << 20, 1 << 20, 1, 1)

    out = [b"IwFl", header(d, n), struct.pack("<QQ", nlist, 1),
           b"IxF2", header(d, nlist), struct.pack("<Q", nlist * d), centroids.tobytes(),
           struct.pack("<BQ", 0, 0),                                      # no direct map
           b"ilar", struct.pack("<QQ", nlist, d * 4), b"full", struct.pack("<Q", nlist)]
    order = [np.flatnonzero(assign == i) for i in range(nlist)]
    out.append(struct.pack(f"<{nlist}Q", *(len(o) for o in order)))
    for ids in order:
        if len(ids):
            out.append(vecs[ids].tobytes())
            out.append(ids.astype("<i8").tobytes())
    Path(path).write_bytes(b"".join(out))
    return nlist


# Compact index: the same vectors as fp16 with a tiny header, half the size of faiss's float32 IVF file (all
# retrieval needs is the vectors; fp16 rounding (~5e-4 relative) is far below what changes the output).
COMPACT_MAGIC = b"UCIDX16\n"


def is_compact(path: Path) -> bool:
    with open(path, "rb") as f:
        return f.read(len(COMPACT_MAGIC)) == COMPACT_MAGIC


def read_vectors(path: Path) -> np.ndarray:
    """All stored vectors (n, d) float32 from a compact or a faiss IVF-Flat index."""
    path = Path(path)
    if is_compact(path):
        raw = path.read_bytes()
        n, d = struct.unpack_from("<qq", raw, len(COMPACT_MAGIC))
        off = len(COMPACT_MAGIC) + 16
        if len(raw) != off + n * d * 2:
            raise IndexFormatError("compact index is truncated")
        return np.frombuffer(raw, np.float16, n * d, off).reshape(n, d).astype(np.float32)
    return read_ivf_flat(path)


def compact_in_place(path: Path) -> tuple[int, int]:
    """Rewrite an index file as the compact fp16 format (validated, crash-safe). Returns (old, new) bytes."""
    from ... import fsutil
    path = Path(path)
    old = path.stat().st_size
    if is_compact(path):
        return old, old
    vecs = read_ivf_flat(path).astype(np.float16)
    if not np.isfinite(vecs).all():
        raise IndexFormatError("index has values out of fp16 range")
    tmp = path.with_name(f"{path.name}.compact.tmp")
    with open(tmp, "wb") as f:
        f.write(COMPACT_MAGIC + struct.pack("<qq", *vecs.shape))
        f.write(vecs.tobytes())
        f.flush()
        os.fsync(f.fileno())
    fsutil._replace(tmp, path)
    return old, path.stat().st_size


CENTRES = 10000   # phase 24: an index above this becomes this many k-means centres (RVC's own way to shrink a big set)
ASIDE = "model.index.before-smaller"   # the full index, kept next to a shrunk one until Keep or Undo (Make smaller, export)
SHRINK_RESULT = "smaller.json"         # what the shrink measured, beside it


def shrink(path: Path, k: int = CENTRES, dest: Path | None = None, device: str | None = None) -> tuple[int, int]:
    """The index as k k-means centres, compact fp16, written to dest (default: in place, crash-safe). Agent Smoke's
    166,906 vectors (256 MB) -> 10k (15 MB): likeness 0.928 -> 0.923, within run-to-run noise. On the GPU when there
    is one (seconds; scikit-learn on the CPU takes hours). Deterministic (seed 0). -> (vectors before, after)"""
    from ... import fsutil
    path = Path(path)
    dest = Path(dest or path)
    x = read_vectors(path)
    if len(x) <= k:
        if dest != path:
            import shutil
            shutil.copyfile(path, dest)
        return len(x), len(x)
    dev = device or ("cuda" if torch.cuda.is_available() else "cpu")
    X = torch.from_numpy(x).to(dev)
    xx = (X * X).sum(1)
    gen = torch.Generator(device=dev).manual_seed(0)
    C = X[torch.randperm(len(X), device=dev, generator=gen)[:k]].clone()

    def assign(C):
        cc = (C * C).sum(1)
        lab = torch.empty(len(X), dtype=torch.long, device=dev)
        dist = torch.empty(len(X), device=dev)
        for i in range(0, len(X), 8192):
            dist[i:i + 8192], lab[i:i + 8192] = (xx[i:i + 8192, None] - 2 * X[i:i + 8192] @ C.T + cc[None]).min(1)
        return lab, dist
    for _ in range(25):
        lab, dist = assign(C)
        cnt = torch.bincount(lab, minlength=k).float()
        C = torch.zeros_like(C).index_add_(0, lab, X) / cnt.clamp(min=1)[:, None]
        empty = torch.nonzero(cnt == 0).flatten()
        if len(empty):   # an empty centre restarts at one of the points farthest from their own centre
            C[empty] = X[dist.topk(len(empty)).indices]
    v = C.float().cpu().numpy().astype(np.float16)
    del X, xx, C
    if dev == "cuda":
        torch.cuda.empty_cache()
    if not np.isfinite(v).all():
        raise IndexFormatError("the shrunk index has values out of fp16 range")
    tmp = dest.with_name(f"{dest.name}.shrink.tmp")
    with open(tmp, "wb") as f:
        f.write(COMPACT_MAGIC + struct.pack("<qq", *v.shape))
        f.write(v.tobytes())
        f.flush()
        os.fsync(f.fileno())
    fsutil._replace(tmp, dest)
    return len(x), len(v)


def export_faiss(path: Path, dest: Path) -> Path:
    """A faiss IVF-Flat copy for other RVC tools (compact indexes are UnderCover-only)."""
    if not is_compact(path):
        return Path(path)
    write_ivf_flat(dest, read_vectors(path))
    return dest


class FeatureIndex:
    """GPU k-NN over an RVC index; blends features toward the voice's training data."""

    def __init__(self, path: Path, device):
        vecs = read_vectors(path)
        self.device = device
        self.vecs = torch.from_numpy(vecs).to(device)                 # (N, d) float32
        self.sqnorm = (self.vecs * self.vecs).sum(1)                   # (N,)
        self.dim = vecs.shape[1]

    @torch.no_grad()
    def retrieve(self, feats: torch.Tensor, k: int = 8, chunk: int = 2048) -> torch.Tensor:
        """feats (T, d) -> (T, d): inverse-squared-distance weighted mean of k nearest stored vectors
        (same weighting as RVC's faiss path)."""
        q = feats.float()
        out = torch.empty_like(q)
        for s in range(0, q.shape[0], chunk):
            qc = q[s:s + chunk]
            dist = (qc * qc).sum(1, keepdim=True) - 2 * qc @ self.vecs.T + self.sqnorm[None]
            score, ix = dist.clamp_min_(1e-12).topk(k, dim=1, largest=False)
            w = 1.0 / score.square()
            w /= w.sum(1, keepdim=True)
            out[s:s + chunk] = (self.vecs[ix] * w[..., None]).sum(1)
        return out.to(feats.dtype)
