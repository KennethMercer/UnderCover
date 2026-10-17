r"""Build undercover/assets/reader-prints.json: a WeSpeaker voice print of each of SpeakEasy's Readers (Kokoro), with the
Reader's sex and accent, for the voice-print sex vote (core/speaker.py: sex_vote; speakeasy/tts.py: pick_reader).

The Readers are fixed (pinned Kokoro weights) and so is WeSpeaker, so the file is made once and shipped; run this again
only when either model changes. CPU only, about two minutes:

    cd <repo> && .venv\Scripts\python.exe tools\reader_prints.py
"""
import json
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("CUDA_VISIBLE_DEVICES", "")
os.environ.setdefault("HIP_VISIBLE_DEVICES", "")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402
from scipy.signal import resample_poly  # noqa: E402

from undercover.core.speaker import PRINTS, SpeakerEmbedder  # noqa: E402
from undercover.speakeasy import tts  # noqa: E402

# three everyday sentences, ~15 s a Reader: enough for a stable print
LINES = ["The weather was warm, and the market by the river was full of people buying bread and fruit.",
         "I never thought we would get this far, but here we are, and tomorrow we start again.",
         "Ladies and gentlemen, thank you all for coming tonight; it means more than I can say."]


def main():
    emb = SpeakerEmbedder()
    out = {}
    for r in tts.READERS:
        a = np.concatenate([tts.synth(t, r) for t in LINES])          # 24 kHz
        v = emb.embed(resample_poly(a, 2, 3).astype(np.float32))      # 16 kHz, unit length
        out[r] = {"sex": "male" if r[1] == "m" else "female", "accent": "british" if r[0] == "b" else "american",
                  "v": [round(float(x), 5) for x in v]}
        print(r, flush=True)
    PRINTS.write_text(json.dumps({"made": time.strftime("%Y-%m-%d"), "how": "tools/reader_prints.py: Kokoro v1.0, three lines "
                                  "each, WeSpeaker ResNet34-LM", "prints": out}, indent=None), encoding="utf-8")
    print("wrote", PRINTS, len(out), "prints")


if __name__ == "__main__":
    main()
