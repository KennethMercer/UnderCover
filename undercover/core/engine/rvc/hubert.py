"""Minimal HuBERT-base (ContentVec) feature extractor in plain torch.

Loads RVC's Transformers-format checkpoint (assets/hubert_base: config.json + pytorch_model.bin)
without needing the `transformers` package. Layer/key layout mirrors transformers.HubertModel
(feat_extract_norm="group", do_stable_layer_norm=False), so the state dict loads as-is.

RVC v1 uses final_proj(hidden_states[9]) (256-D); v2 uses the last layer output (768-D).
"""
import json
from pathlib import Path

import torch
import torch.nn.functional as F
from torch import nn


class _ConvLayer(nn.Module):
    def __init__(self, cin, cout, k, s, group_norm: bool):
        super().__init__()
        self.conv = nn.Conv1d(cin, cout, k, s, bias=False)
        if group_norm:
            self.layer_norm = nn.GroupNorm(cout, cout, affine=True)

    def forward(self, x):
        x = self.conv(x)
        if hasattr(self, "layer_norm"):
            x = self.layer_norm(x)
        return F.gelu(x)


class _FeatureExtractor(nn.Module):
    def __init__(self, cfg):
        super().__init__()
        dims, ks, ss = cfg["conv_dim"], cfg["conv_kernel"], cfg["conv_stride"]
        self.conv_layers = nn.ModuleList(
            _ConvLayer(1 if i == 0 else dims[i - 1], dims[i], ks[i], ss[i], group_norm=(i == 0))
            for i in range(len(dims)))

    def forward(self, x):  # (B, T) -> (B, C, frames)
        x = x[:, None]
        for layer in self.conv_layers:
            x = layer(x)
        return x


class _FeatureProjection(nn.Module):
    def __init__(self, cfg):
        super().__init__()
        self.layer_norm = nn.LayerNorm(cfg["conv_dim"][-1], eps=cfg["layer_norm_eps"])
        self.projection = nn.Linear(cfg["conv_dim"][-1], cfg["hidden_size"])

    def forward(self, x):
        return self.projection(self.layer_norm(x))


class _PosConv(nn.Module):
    def __init__(self, cfg):
        super().__init__()
        h, k = cfg["hidden_size"], cfg["num_conv_pos_embeddings"]
        conv = nn.Conv1d(h, h, k, padding=k // 2, groups=cfg["num_conv_pos_embedding_groups"])
        self.conv = nn.utils.parametrizations.weight_norm(conv, name="weight", dim=2)
        self.remove = 1 if k % 2 == 0 else 0

    def forward(self, x):  # (B, T, H)
        y = self.conv(x.transpose(1, 2))
        if self.remove:
            y = y[:, :, :-self.remove]
        return F.gelu(y).transpose(1, 2)


class _Attention(nn.Module):
    def __init__(self, h, heads):
        super().__init__()
        self.heads = heads
        self.q_proj, self.k_proj, self.v_proj, self.out_proj = (nn.Linear(h, h) for _ in range(4))

    def forward(self, x, mask=None):
        b, t, h = x.shape
        q, k, v = (p(x).view(b, t, self.heads, h // self.heads).transpose(1, 2)
                   for p in (self.q_proj, self.k_proj, self.v_proj))
        y = F.scaled_dot_product_attention(q, k, v, attn_mask=mask)
        return self.out_proj(y.transpose(1, 2).reshape(b, t, h))


class _FeedForward(nn.Module):
    def __init__(self, h, inter):
        super().__init__()
        self.intermediate_dense = nn.Linear(h, inter)
        self.output_dense = nn.Linear(inter, h)

    def forward(self, x):
        return self.output_dense(F.gelu(self.intermediate_dense(x)))


class _Layer(nn.Module):  # post-norm transformer layer
    def __init__(self, cfg):
        super().__init__()
        h, eps = cfg["hidden_size"], cfg["layer_norm_eps"]
        self.attention = _Attention(h, cfg["num_attention_heads"])
        self.layer_norm = nn.LayerNorm(h, eps=eps)
        self.feed_forward = _FeedForward(h, cfg["intermediate_size"])
        self.final_layer_norm = nn.LayerNorm(h, eps=eps)

    def forward(self, x, mask=None):
        x = self.layer_norm(x + self.attention(x, mask))
        return self.final_layer_norm(x + self.feed_forward(x))


class _Encoder(nn.Module):
    def __init__(self, cfg):
        super().__init__()
        self.pos_conv_embed = _PosConv(cfg)
        self.layer_norm = nn.LayerNorm(cfg["hidden_size"], eps=cfg["layer_norm_eps"])
        self.layers = nn.ModuleList(_Layer(cfg) for _ in range(cfg["num_hidden_layers"]))


class HubertModel(nn.Module):
    def __init__(self, cfg: dict):
        super().__init__()
        assert cfg.get("feat_extract_norm") == "group" and not cfg.get("do_stable_layer_norm"), \
            "only HuBERT-base style checkpoints are supported"
        self.feature_extractor = _FeatureExtractor(cfg)
        self.feature_projection = _FeatureProjection(cfg)
        self.encoder = _Encoder(cfg)
        self.final_proj = nn.Linear(cfg["hidden_size"], cfg["classifier_proj_size"])
        self.masked_spec_embed = nn.Parameter(torch.zeros(cfg["hidden_size"]))  # unused at inference

    @torch.no_grad()
    def extract(self, wav16k: torch.Tensor, version: str) -> torch.Tensor:
        """wav16k: (B, T) float at 16 kHz -> (B, frames, 256|768). One frame per 320 samples (20 ms)."""
        x = self.feature_extractor(wav16k).transpose(1, 2)
        x = self.feature_projection(x)
        x = x + self.encoder.pos_conv_embed(x)
        x = self.encoder.layer_norm(x)
        stop = 9 if version == "v1" else len(self.encoder.layers)
        for layer in self.encoder.layers[:stop]:
            x = layer(x)
        return self.final_proj(x) if version == "v1" else x


def load_hubert(model_dir: Path, device, half: bool) -> HubertModel:
    cfg = json.loads((Path(model_dir) / "config.json").read_text(encoding="utf-8"))
    model = HubertModel(cfg)
    sd = torch.load(Path(model_dir) / "pytorch_model.bin", map_location="cpu", weights_only=True)
    missing, unexpected = model.load_state_dict(sd, strict=False)
    if missing or unexpected:
        raise RuntimeError(f"HuBERT checkpoint mismatch: missing={missing[:5]} unexpected={unexpected[:5]}")
    model = model.to(device).eval()
    return model.half() if half else model.float()
