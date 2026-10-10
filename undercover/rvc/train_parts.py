"""RVC training-only parts: discriminators, GAN/mel/KL losses, spectrogram and mel helpers.

Vendored from RVC-Project/Retrieval-based-Voice-Conversion-WebUI (MIT, see LICENSE-RVC), commit 81eed5e8f6:
infer/module/models.py (MultiPeriodDiscriminatorV2, DiscriminatorS, DiscriminatorP), train/losses.py,
train/mel_processing.py. Logic unchanged; loss lists return floats only where upstream logged them.
"""
import torch
from torch import nn
from torch.nn import Conv1d, Conv2d
from torch.nn import functional as F
from torch.nn.utils import spectral_norm, weight_norm

from . import modules
from .commons import get_padding


# ---------------------------------------------------------------- discriminators

class MultiPeriodDiscriminatorV2(nn.Module):
    def __init__(self, use_spectral_norm=False):
        super().__init__()
        periods = [2, 3, 5, 7, 11, 17, 23, 37]
        discs = [DiscriminatorS(use_spectral_norm=use_spectral_norm)]
        discs += [DiscriminatorP(i, use_spectral_norm=use_spectral_norm) for i in periods]
        self.discriminators = nn.ModuleList(discs)

    def forward(self, y, y_hat):
        y_d_rs, y_d_gs, fmap_rs, fmap_gs = [], [], [], []
        for d in self.discriminators:
            y_d_r, fmap_r = d(y)
            y_d_g, fmap_g = d(y_hat)
            y_d_rs.append(y_d_r)
            y_d_gs.append(y_d_g)
            fmap_rs.append(fmap_r)
            fmap_gs.append(fmap_g)
        return y_d_rs, y_d_gs, fmap_rs, fmap_gs


class DiscriminatorS(nn.Module):
    def __init__(self, use_spectral_norm=False):
        super().__init__()
        norm_f = spectral_norm if use_spectral_norm else weight_norm
        self.convs = nn.ModuleList([
            norm_f(Conv1d(1, 16, 15, 1, padding=7)),
            norm_f(Conv1d(16, 64, 41, 4, groups=4, padding=20)),
            norm_f(Conv1d(64, 256, 41, 4, groups=16, padding=20)),
            norm_f(Conv1d(256, 1024, 41, 4, groups=64, padding=20)),
            norm_f(Conv1d(1024, 1024, 41, 4, groups=256, padding=20)),
            norm_f(Conv1d(1024, 1024, 5, 1, padding=2)),
        ])
        self.conv_post = norm_f(Conv1d(1024, 1, 3, 1, padding=1))

    def forward(self, x):
        fmap = []
        for layer in self.convs:
            x = F.leaky_relu(layer(x), modules.LRELU_SLOPE)
            fmap.append(x)
        x = self.conv_post(x)
        fmap.append(x)
        return torch.flatten(x, 1, -1), fmap


class DiscriminatorP(nn.Module):
    def __init__(self, period, kernel_size=5, stride=3, use_spectral_norm=False):
        super().__init__()
        self.period = period
        norm_f = spectral_norm if use_spectral_norm else weight_norm
        pad = (get_padding(kernel_size, 1), 0)
        chans = [1, 32, 128, 512, 1024]
        convs = [norm_f(Conv2d(a, b, (kernel_size, 1), (stride, 1), padding=pad)) for a, b in zip(chans, chans[1:])]
        convs.append(norm_f(Conv2d(1024, 1024, (kernel_size, 1), 1, padding=pad)))
        self.convs = nn.ModuleList(convs)
        self.conv_post = norm_f(Conv2d(1024, 1, (3, 1), 1, padding=(1, 0)))

    def forward(self, x):
        fmap = []
        b, c, t = x.shape
        if t % self.period != 0:
            n_pad = self.period - (t % self.period)
            x = F.pad(x, (0, n_pad), "reflect")
            t += n_pad
        x = x.view(b, c, t // self.period, self.period)
        for layer in self.convs:
            x = F.leaky_relu(layer(x), modules.LRELU_SLOPE)
            fmap.append(x)
        x = self.conv_post(x)
        fmap.append(x)
        return torch.flatten(x, 1, -1), fmap


# ---------------------------------------------------------------- losses

def feature_loss(fmap_r, fmap_g):
    loss = 0
    for dr, dg in zip(fmap_r, fmap_g):
        for rl, gl in zip(dr, dg):
            loss += torch.mean(torch.abs(rl.float().detach() - gl.float()))
    return loss * 2


def discriminator_loss(disc_real_outputs, disc_generated_outputs):
    loss = 0
    for dr, dg in zip(disc_real_outputs, disc_generated_outputs):
        loss += torch.mean((1 - dr.float()) ** 2) + torch.mean(dg.float() ** 2)
    return loss


def generator_loss(disc_outputs):
    loss = 0
    for dg in disc_outputs:
        loss += torch.mean((1 - dg.float()) ** 2)
    return loss


def kl_loss(z_p, logs_q, m_p, logs_p, z_mask):
    z_p, logs_q, m_p, logs_p, z_mask = (t.float() for t in (z_p, logs_q, m_p, logs_p, z_mask))
    kl = logs_p - logs_q - 0.5
    kl += 0.5 * ((z_p - m_p) ** 2) * torch.exp(-2.0 * logs_p)
    return torch.sum(kl * z_mask) / torch.sum(z_mask)


# ---------------------------------------------------------------- spectrograms

_mel_basis: dict = {}
_hann: dict = {}


def spectrogram_torch(y, n_fft, hop_size, win_size):
    """(B, T) waveform -> (B, n_fft//2+1, frames) linear magnitude (RVC: reflect pad, center=False)."""
    key = f"{win_size}_{y.dtype}_{y.device}"
    if key not in _hann:
        _hann[key] = torch.hann_window(win_size).to(dtype=y.dtype, device=y.device)
    p = int((n_fft - hop_size) / 2)
    y = F.pad(y.unsqueeze(1), (p, p), mode="reflect").squeeze(1)
    spec = torch.stft(y, n_fft, hop_length=hop_size, win_length=win_size, window=_hann[key],
                      center=False, pad_mode="reflect", normalized=False, onesided=True, return_complex=True)
    return torch.sqrt(spec.real.pow(2) + spec.imag.pow(2) + 2e-7)


def spec_to_mel_torch(spec, n_fft, num_mels, sampling_rate, fmin=0.0, fmax=None):
    key = f"{fmax}_{num_mels}_{spec.dtype}_{spec.device}"
    if key not in _mel_basis:
        from librosa.filters import mel as librosa_mel_fn
        mel = librosa_mel_fn(sr=sampling_rate, n_fft=n_fft, n_mels=num_mels, fmin=fmin, fmax=fmax)
        _mel_basis[key] = torch.from_numpy(mel).to(dtype=spec.dtype, device=spec.device)
    return torch.log(torch.clamp(torch.matmul(_mel_basis[key], spec), min=2e-6))


def mel_spectrogram_torch(y, n_fft, num_mels, sampling_rate, hop_size, win_size, fmin=0.0, fmax=None):
    return spec_to_mel_torch(spectrogram_torch(y, n_fft, hop_size, win_size), n_fft, num_mels, sampling_rate, fmin, fmax)
