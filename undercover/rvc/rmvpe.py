"""RMVPE pitch (F0) extractor.

Vendored from RVC-Project/Retrieval-based-Voice-Conversion-WebUI infer/rmvpe.py (MIT, see LICENSE-RVC),
commit 81eed5e8f6. Trimmed to the torch path (no DirectML/ONNX/CUDA-graph), decode vectorized.
"""
import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
from librosa.filters import mel as librosa_mel


class BiGRU(nn.Module):
    def __init__(self, input_features, hidden_features, num_layers):
        super().__init__()
        self.gru = nn.GRU(input_features, hidden_features, num_layers=num_layers,
                          batch_first=True, bidirectional=True)

    def forward(self, x):
        return self.gru(x)[0]


class ConvBlockRes(nn.Module):
    def __init__(self, in_channels, out_channels, momentum=0.01):
        super().__init__()
        self.conv = nn.Sequential(
            nn.Conv2d(in_channels, out_channels, (3, 3), (1, 1), (1, 1), bias=False),
            nn.BatchNorm2d(out_channels, momentum=momentum), nn.ReLU(),
            nn.Conv2d(out_channels, out_channels, (3, 3), (1, 1), (1, 1), bias=False),
            nn.BatchNorm2d(out_channels, momentum=momentum), nn.ReLU(),
        )
        if in_channels != out_channels:
            self.shortcut = nn.Conv2d(in_channels, out_channels, (1, 1))

    def forward(self, x):
        return self.conv(x) + (self.shortcut(x) if hasattr(self, "shortcut") else x)


class ResEncoderBlock(nn.Module):
    def __init__(self, in_channels, out_channels, kernel_size, n_blocks=1, momentum=0.01):
        super().__init__()
        self.conv = nn.ModuleList([ConvBlockRes(in_channels, out_channels, momentum)]
                                  + [ConvBlockRes(out_channels, out_channels, momentum) for _ in range(n_blocks - 1)])
        self.kernel_size = kernel_size
        if kernel_size is not None:
            self.pool = nn.AvgPool2d(kernel_size=kernel_size)

    def forward(self, x):
        for conv in self.conv:
            x = conv(x)
        return (x, self.pool(x)) if self.kernel_size is not None else x


class Encoder(nn.Module):
    def __init__(self, in_channels, in_size, n_encoders, kernel_size, n_blocks, out_channels=16, momentum=0.01):
        super().__init__()
        self.bn = nn.BatchNorm2d(in_channels, momentum=momentum)
        self.layers = nn.ModuleList()
        for _ in range(n_encoders):
            self.layers.append(ResEncoderBlock(in_channels, out_channels, kernel_size, n_blocks, momentum=momentum))
            in_channels, out_channels, in_size = out_channels, out_channels * 2, in_size // 2
        self.out_channel = out_channels

    def forward(self, x):
        concat = []
        x = self.bn(x)
        for layer in self.layers:
            t, x = layer(x)
            concat.append(t)
        return x, concat


class Intermediate(nn.Module):
    def __init__(self, in_channels, out_channels, n_inters, n_blocks, momentum=0.01):
        super().__init__()
        self.layers = nn.ModuleList([ResEncoderBlock(in_channels, out_channels, None, n_blocks, momentum)]
                                    + [ResEncoderBlock(out_channels, out_channels, None, n_blocks, momentum)
                                       for _ in range(n_inters - 1)])

    def forward(self, x):
        for layer in self.layers:
            x = layer(x)
        return x


class ResDecoderBlock(nn.Module):
    def __init__(self, in_channels, out_channels, stride, n_blocks=1, momentum=0.01):
        super().__init__()
        out_padding = (0, 1) if stride == (1, 2) else (1, 1)
        self.conv1 = nn.Sequential(
            nn.ConvTranspose2d(in_channels, out_channels, (3, 3), stride, (1, 1),
                               output_padding=out_padding, bias=False),
            nn.BatchNorm2d(out_channels, momentum=momentum), nn.ReLU(),
        )
        self.conv2 = nn.ModuleList([ConvBlockRes(out_channels * 2, out_channels, momentum)]
                                   + [ConvBlockRes(out_channels, out_channels, momentum) for _ in range(n_blocks - 1)])

    def forward(self, x, concat_tensor):
        x = torch.cat((self.conv1(x), concat_tensor), dim=1)
        for conv2 in self.conv2:
            x = conv2(x)
        return x


class Decoder(nn.Module):
    def __init__(self, in_channels, n_decoders, stride, n_blocks, momentum=0.01):
        super().__init__()
        self.layers = nn.ModuleList()
        for _ in range(n_decoders):
            self.layers.append(ResDecoderBlock(in_channels, in_channels // 2, stride, n_blocks, momentum))
            in_channels //= 2

    def forward(self, x, concat):
        for i, layer in enumerate(self.layers):
            x = layer(x, concat[-1 - i])
        return x


class DeepUnet(nn.Module):
    def __init__(self, kernel_size, n_blocks, en_de_layers=5, inter_layers=4, in_channels=1, en_out_channels=16):
        super().__init__()
        self.encoder = Encoder(in_channels, 128, en_de_layers, kernel_size, n_blocks, en_out_channels)
        self.intermediate = Intermediate(self.encoder.out_channel // 2, self.encoder.out_channel, inter_layers, n_blocks)
        self.decoder = Decoder(self.encoder.out_channel, en_de_layers, kernel_size, n_blocks)

    def forward(self, x):
        x, concat = self.encoder(x)
        return self.decoder(self.intermediate(x), concat)


class E2E(nn.Module):
    def __init__(self, n_blocks, n_gru, kernel_size, en_de_layers=5, inter_layers=4, in_channels=1, en_out_channels=16):
        super().__init__()
        self.unet = DeepUnet(kernel_size, n_blocks, en_de_layers, inter_layers, in_channels, en_out_channels)
        self.cnn = nn.Conv2d(en_out_channels, 3, (3, 3), padding=(1, 1))
        self.fc = nn.Sequential(BiGRU(3 * 128, 256, n_gru), nn.Linear(512, 360), nn.Dropout(0.25), nn.Sigmoid())

    def forward(self, mel):
        mel = mel.transpose(-1, -2).unsqueeze(1)
        x = self.cnn(self.unet(mel)).transpose(1, 2).flatten(-2)
        return self.fc(x)


class MelSpectrogram(nn.Module):
    def __init__(self, n_mel_channels, sampling_rate, win_length, hop_length, n_fft=None,
                 mel_fmin=0, mel_fmax=None, clamp=1e-5):
        super().__init__()
        n_fft = win_length if n_fft is None else n_fft
        basis = librosa_mel(sr=sampling_rate, n_fft=n_fft, n_mels=n_mel_channels, fmin=mel_fmin, fmax=mel_fmax, htk=True)
        self.register_buffer("mel_basis", torch.from_numpy(basis).float())
        self.register_buffer("window", torch.hann_window(win_length))
        self.n_fft, self.hop_length, self.win_length, self.clamp = n_fft, hop_length, win_length, clamp

    def forward(self, audio):
        fft = torch.stft(audio, n_fft=self.n_fft, hop_length=self.hop_length, win_length=self.win_length,
                         window=self.window, center=True, return_complex=True)
        mel = torch.matmul(self.mel_basis, fft.abs())
        return torch.log(torch.clamp(mel, min=self.clamp))


class RMVPE:
    def __init__(self, model_path, device, half: bool):
        self.device, self.half = device, half
        self.mel_extractor = MelSpectrogram(128, 16000, 1024, 160, None, 30, 8000).to(device)
        model = E2E(4, 1, (2, 2))
        model.load_state_dict(torch.load(model_path, map_location="cpu", weights_only=True))
        model = model.eval().to(device)
        self.model = model.half() if half else model.float()
        cents = 20 * np.arange(360) + 1997.3794084376191
        self.cents_mapping = np.pad(cents, (4, 4))  # 368

    @torch.no_grad()
    def infer_from_audio(self, audio: np.ndarray, thred: float = 0.03) -> np.ndarray:
        """16 kHz mono float audio -> F0 in Hz per 10 ms frame (0 = unvoiced)."""
        mel = self.mel_extractor(torch.from_numpy(audio).float().to(self.device).unsqueeze(0))
        n_frames = mel.shape[-1]
        n_pad = 32 * ((n_frames - 1) // 32 + 1) - n_frames
        if n_pad:
            mel = F.pad(mel, (0, n_pad))
        hidden = self.model(mel.half() if self.half else mel)[:, :n_frames]
        salience = hidden.squeeze(0).float().cpu().numpy()
        return self._decode(salience, thred)

    def _decode(self, salience: np.ndarray, thred: float) -> np.ndarray:
        center = np.argmax(salience, axis=1) + 4
        sal = np.pad(salience, ((0, 0), (4, 4)))
        idx = center[:, None] + np.arange(-4, 5)[None, :]                 # (frames, 9) window around peak
        win = np.take_along_axis(sal, idx, axis=1)
        cents = (win * self.cents_mapping[idx]).sum(1) / win.sum(1)
        cents[sal.max(axis=1) <= thred] = 0
        f0 = 10 * (2 ** (cents / 1200))
        f0[f0 == 10] = 0
        return f0
