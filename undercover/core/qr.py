"""A small QR code encoder (phase 11: the phone pairing link), standard library only.

Byte mode, error correction level M, versions 1-10 (up to 213 bytes: a pairing link is about 45). The mask is chosen by
the standard's penalty rules. `matrix(text)` gives rows of booleans (True = dark); `svg(text)` an SVG with a quiet zone.
Written after the ISO/IEC 18004 layout as Project Nayuki's reference encoder describes it.
"""

# version -> (EC codewords per block, [(blocks, data codewords per block), ...]) for level M
_BLOCKS = {
    1: (10, [(1, 16)]), 2: (16, [(1, 28)]), 3: (26, [(1, 44)]), 4: (18, [(2, 32)]), 5: (24, [(2, 43)]),
    6: (16, [(4, 27)]), 7: (18, [(4, 31)]), 8: (22, [(2, 38), (2, 39)]), 9: (22, [(3, 36), (2, 37)]),
    10: (26, [(4, 43), (1, 44)]),
}
_ALIGN = {1: [], 2: [6, 18], 3: [6, 22], 4: [6, 26], 5: [6, 30], 6: [6, 34], 7: [6, 22, 38], 8: [6, 24, 42],
          9: [6, 26, 46], 10: [6, 28, 50]}
_FORMAT_M = 0   # level M's two format bits (L = 1, M = 0, Q = 3, H = 2)

# GF(256) with the QR polynomial x^8 + x^4 + x^3 + x^2 + 1
_EXP, _LOG = [0] * 512, [0] * 256
_x = 1
for _i in range(255):
    _EXP[_i], _LOG[_x] = _x, _i
    _x <<= 1
    if _x & 0x100:
        _x ^= 0x11D
for _i in range(255, 512):
    _EXP[_i] = _EXP[_i - 255]


def _mul(a: int, b: int) -> int:
    return 0 if a == 0 or b == 0 else _EXP[_LOG[a] + _LOG[b]]


def rs_ecc(data: list[int], n: int) -> list[int]:
    """The n Reed-Solomon error correction codewords for `data`."""
    gen = [1]
    for i in range(n):   # (x - a^0)(x - a^1)...(x - a^(n-1))
        gen = [a ^ _mul(b, _EXP[i]) for a, b in zip(gen + [0], [0] + gen)]
    rem = [0] * n
    for d in data:
        f = d ^ rem[0]
        rem = rem[1:] + [0]
        for j in range(n):
            rem[j] ^= _mul(gen[j + 1], f)
    return rem


def _codewords(data: bytes) -> tuple[int, list[int]]:
    for ver, (ec, groups) in _BLOCKS.items():
        cap = sum(b * d for b, d in groups)
        count_bits = 8 if ver < 10 else 16
        if 4 + count_bits + 8 * len(data) <= cap * 8:
            break
    else:
        raise ValueError(f"too long for a QR code here ({len(data)} bytes)")
    bits = [0, 1, 0, 0] + [(len(data) >> i) & 1 for i in reversed(range(count_bits))]
    for byte in data:
        bits += [(byte >> i) & 1 for i in reversed(range(8))]
    bits += [0] * min(4, cap * 8 - len(bits))   # terminator
    bits += [0] * (-len(bits) % 8)
    words = [int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8)]
    pad = 0xEC
    while len(words) < cap:
        words.append(pad)
        pad ^= 0xEC ^ 0x11
    blocks, at = [], 0
    for b, d in groups:
        for _ in range(b):
            blocks.append(words[at:at + d])
            at += d
    eccs = [rs_ecc(b, ec) for b in blocks]
    out = [b[i] for i in range(max(map(len, blocks))) for b in blocks if i < len(b)]
    out += [e[i] for i in range(ec) for e in eccs]
    return ver, out


def _bch(value: int, poly: int, deg: int) -> int:
    rem = value
    for _ in range(deg):
        rem = (rem << 1) ^ ((rem >> (deg - 1)) * poly)
    return value << deg | rem


def format_bits(mask: int) -> int:
    """The 15 format bits for level M and this mask (BCH(15,5), XOR 0x5412)."""
    rem = _FORMAT_M << 3 | mask
    for _ in range(10):
        rem = (rem << 1) ^ ((rem >> 9) * 0x537)
    return ((_FORMAT_M << 3 | mask) << 10 | rem) ^ 0x5412


def version_bits(ver: int) -> int:
    """The 18 version bits (versions 7 and up; BCH(18,6))."""
    rem = ver
    for _ in range(12):
        rem = (rem << 1) ^ ((rem >> 11) * 0x1F25)
    return ver << 12 | rem


_MASKS = [
    lambda r, c: (r + c) % 2 == 0, lambda r, c: r % 2 == 0, lambda r, c: c % 3 == 0, lambda r, c: (r + c) % 3 == 0,
    lambda r, c: (r // 2 + c // 3) % 2 == 0, lambda r, c: (r * c) % 2 + (r * c) % 3 == 0,
    lambda r, c: ((r * c) % 2 + (r * c) % 3) % 2 == 0, lambda r, c: ((r + c) % 2 + (r * c) % 3) % 2 == 0,
]


class _Grid:
    def __init__(self, ver: int):
        self.ver, self.n = ver, ver * 4 + 17
        self.m = [[False] * self.n for _ in range(self.n)]
        self.fn = [[False] * self.n for _ in range(self.n)]

    def put(self, x: int, y: int, dark: bool):   # x = column, y = row
        self.m[y][x], self.fn[y][x] = bool(dark), True

    def functions(self):
        n = self.n
        for i in range(n):
            self.put(6, i, i % 2 == 0)
            self.put(i, 6, i % 2 == 0)
        for cx, cy in ((3, 3), (n - 4, 3), (3, n - 4)):
            for dy in range(-4, 5):
                for dx in range(-4, 5):
                    if 0 <= cx + dx < n and 0 <= cy + dy < n:
                        self.put(cx + dx, cy + dy, max(abs(dx), abs(dy)) not in (2, 4))
        al = _ALIGN[self.ver]
        for i, ax in enumerate(al):
            for j, ay in enumerate(al):
                if (i, j) in ((0, 0), (0, len(al) - 1), (len(al) - 1, 0)):
                    continue   # the finder corners
                for dy in range(-2, 3):
                    for dx in range(-2, 3):
                        self.put(ax + dx, ay + dy, max(abs(dx), abs(dy)) != 1)
        self.format(0)   # reserves the format areas; drawn again with the chosen mask
        if self.ver >= 7:
            bits = version_bits(self.ver)
            for i in range(18):
                dark, a, b = (bits >> i) & 1, n - 11 + i % 3, i // 3
                self.put(a, b, dark)
                self.put(b, a, dark)

    def format(self, mask: int):
        bits, n = format_bits(mask), self.n
        bit = lambda i: (bits >> i) & 1   # noqa: E731
        for i in range(6):
            self.put(8, i, bit(i))
        self.put(8, 7, bit(6))
        self.put(8, 8, bit(7))
        self.put(7, 8, bit(8))
        for i in range(9, 15):
            self.put(14 - i, 8, bit(i))
        for i in range(8):
            self.put(n - 1 - i, 8, bit(i))
        for i in range(8, 15):
            self.put(8, n - 15 + i, bit(i))
        self.put(8, n - 8, True)   # the dark module

    def data(self, words: list[int]):
        n, i, total = self.n, 0, len(words) * 8
        right = n - 1
        while right >= 1:
            if right == 6:
                right = 5
            for vert in range(n):
                for j in range(2):
                    x = right - j
                    y = n - 1 - vert if ((right + 1) & 2) == 0 else vert
                    if not self.fn[y][x] and i < total:
                        self.m[y][x] = bool((words[i >> 3] >> (7 - (i & 7))) & 1)
                        i += 1
            right -= 2

    def masked(self, mask: int) -> list[list[bool]]:
        f = _MASKS[mask]
        return [[self.m[r][c] ^ (not self.fn[r][c] and f(r, c)) for c in range(self.n)] for r in range(self.n)]


def _penalty(m: list[list[bool]]) -> int:
    n, score = len(m), 0
    lines = m + [list(col) for col in zip(*m)]
    for line in lines:   # runs of five or more
        run = 1
        for a, b in zip(line, line[1:]):
            if a == b:
                run += 1
            else:
                score += run - 2 if run >= 5 else 0
                run = 1
        score += run - 2 if run >= 5 else 0
        s = "".join("1" if v else "0" for v in line)   # finder-like patterns
        for pat in ("10111010000", "00001011101"):
            at = s.find(pat)
            while at != -1:
                score += 40
                at = s.find(pat, at + 1)
    for r in range(n - 1):   # 2x2 blocks
        for c in range(n - 1):
            if m[r][c] == m[r][c + 1] == m[r + 1][c] == m[r + 1][c + 1]:
                score += 3
    dark = sum(map(sum, m))
    score += abs(dark * 20 - n * n * 10) // (n * n) * 10
    return score


def matrix(text: str) -> list[list[bool]]:
    """The QR code for `text` (UTF-8) as rows of booleans, True = dark."""
    ver, words = _codewords(text.encode("utf-8"))
    g = _Grid(ver)
    g.functions()
    g.data(words)
    best = None
    for mask in range(8):
        g.format(mask)
        m = g.masked(mask)
        p = _penalty(m)
        if best is None or p < best[0]:
            best = (p, mask, m)
    g.format(best[1])
    return g.masked(best[1])


def svg(text: str, quiet: int = 4) -> str:
    """An SVG of the code: black modules on white with a quiet zone, scalable (no fixed size; the page sets it)."""
    m = matrix(text)
    n = len(m) + 2 * quiet
    path = "".join(f"M{c + quiet} {r + quiet}h1v1h-1z" for r, row in enumerate(m) for c, v in enumerate(row) if v)
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {n} {n}" shape-rendering="crispEdges" role="img" '
            f'aria-label="QR code"><rect width="{n}" height="{n}" fill="#fff"/><path d="{path}" fill="#000"/></svg>')
