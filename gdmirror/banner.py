"""Block-letter wordmark for the splash screen.

The glyphs are an ANSI Shadow style face: solid blocks for the front of each
letter and box-drawing characters for the bevel down its right and bottom edges.
The depth is therefore part of the letterform rather than something the renderer
fakes by stamping offset copies, which means the wordmark reads as three
dimensional in plain monospace too - useful, since the README shows the same
output with no colour at all.
"""

from __future__ import annotations

from rich.text import Text

GLYPH_H = 6
GAP = 0          # the glyphs carry their own side bearing
LINE_GAP = 1     # blank rows between stacked words

FACE = "█"
BEVEL = set("╗╝║═╔╚╔")

GLYPHS: dict[str, list[str]] = {
    "G": [
        " ██████╗ ",
        "██╔════╝ ",
        "██║  ███╗",
        "██║   ██║",
        "╚██████╔╝",
        " ╚═════╝ ",
    ],
    "D": [
        "██████╗ ",
        "██╔══██╗",
        "██║  ██║",
        "██║  ██║",
        "██████╔╝",
        "╚═════╝ ",
    ],
    "M": [
        "███╗   ███╗",
        "████╗ ████║",
        "██╔████╔██║",
        "██║╚██╔╝██║",
        "██║ ╚═╝ ██║",
        "╚═╝     ╚═╝",
    ],
    "I": [
        "██╗",
        "██║",
        "██║",
        "██║",
        "██║",
        "╚═╝",
    ],
    "R": [
        "██████╗ ",
        "██╔══██╗",
        "██████╔╝",
        "██╔══██╗",
        "██║  ██║",
        "╚═╝  ╚═╝",
    ],
    "O": [
        " ██████╗ ",
        "██╔═══██╗",
        "██║   ██║",
        "██║   ██║",
        "╚██████╔╝",
        " ╚═════╝ ",
    ],
    "V": [
        "██╗   ██╗",
        "██║   ██║",
        "██║   ██║",
        "╚██╗ ██╔╝",
        " ╚████╔╝ ",
        "  ╚═══╝  ",
    ],
    "E": [
        "███████╗",
        "██╔════╝",
        "█████╗  ",
        "██╔══╝  ",
        "███████╗",
        "╚══════╝",
    ],
    " ": ["    "] * GLYPH_H,
}

WORDMARK = "GDMIRROR"  # 64 cells wide, clears an 80-column terminal

# face gradient, left to right
FACE_FROM = (0x00, 0xF5, 0xD4)
FACE_TO = (0x9B, 0xFF, 0x3C)
BEVEL_COLOUR = (0x0D, 0x6E, 0x5F)
SWEEP = (0xFF, 0xFF, 0xFF)


def _hex(rgb: tuple[int, int, int]) -> str:
    return "#{:02x}{:02x}{:02x}".format(*rgb)


def _mix(a: tuple[int, int, int], b: tuple[int, int, int], t: float) -> tuple[int, int, int]:
    t = max(0.0, min(1.0, t))
    return tuple(int(round(x + (y - x) * t)) for x, y in zip(a, b))  # type: ignore[return-value]


def glyph_rows(words: str | list[str]) -> list[str]:
    """Lay `words` out as plain text rows, stacked and centred if there are several."""
    if isinstance(words, str):
        words = [words]

    blocks: list[list[str]] = []
    for word in words:
        rows = [""] * GLYPH_H
        for ch in word:
            glyph = GLYPHS.get(ch.upper(), GLYPHS[" "])
            width = max(len(line) for line in glyph)
            for r in range(GLYPH_H):
                rows[r] += glyph[r].ljust(width) + " " * GAP
        blocks.append([row.rstrip() for row in rows])

    total = max((max(len(r) for r in block) for block in blocks), default=0)
    out: list[str] = []
    for index, block in enumerate(blocks):
        if index:
            out += [""] * LINE_GAP
        pad = (total - max(len(r) for r in block)) // 2
        out += [" " * pad + row for row in block]
    return out


def glyph_cells(words: str | list[str]) -> tuple[list[tuple[int, int]], int]:
    """Filled (row, col) positions and the total width. Kept for measurement."""
    rows = glyph_rows(words)
    cells = [
        (r, c)
        for r, row in enumerate(rows)
        for c, ch in enumerate(row)
        if ch != " "
    ]
    return cells, max((len(r) for r in rows), default=0)


def render(
    words: str | list[str] = WORDMARK,
    *,
    reveal: float = 1.0,
    sweep: float | None = None,
    dim: float = 0.0,
) -> Text:
    """Colour the wordmark.

    reveal  0..1 fraction of columns drawn, for the wipe-in
    sweep   0..1 position of the bright band, or None
    dim     0..1 how far to fade the face toward black, for the idle pulse
    """
    rows = glyph_rows(words)
    width = max((len(r) for r in rows), default=0)
    limit = reveal * width - 1
    band = sweep * (width * 1.4) - width * 0.2 if sweep is not None else None

    text = Text()
    for row in rows:
        for col in range(width):
            ch = row[col] if col < len(row) else " "
            if ch == " " or col > limit:
                text.append(" ")
                continue
            if ch in BEVEL:
                text.append(ch, style=_hex(BEVEL_COLOUR))
                continue

            colour = _mix(FACE_FROM, FACE_TO, col / max(width - 1, 1))
            if dim:
                colour = _mix(colour, (0, 0, 0), dim)
            if band is not None:
                distance = abs(col - band)
                if distance < 6:
                    colour = _mix(colour, SWEEP, 1 - distance / 6)
            text.append(ch, style=_hex(colour))
        text.append("\n")
    return text


def plain(words: str | list[str] = WORDMARK) -> str:
    """Uncoloured wordmark, for documentation."""
    return "\n".join(glyph_rows(words))


def rule(width: int = 60, phase: float = 0.0) -> Text:
    """Thin animated divider: a dot of light travelling along a dim line."""
    text = Text()
    head = phase * width
    for i in range(width):
        distance = abs(i - head)
        if distance < 4:
            text.append("━", style=_hex(_mix(BEVEL_COLOUR, SWEEP, 1 - distance / 4)))
        else:
            text.append("━", style="#0d3b34")
    return text
