#!/usr/bin/env python3
"""
Chess Screen Assistant (GUI)

Reads a chess.com board from your screen every N seconds (default 5), recognises the
pieces, follows the game, and shows Stockfish's best move on a live board with an arrow.

Install:
    pip install python-chess mss numpy pillow
    + download Stockfish: https://stockfishchess.org/download/

Run:
    python chess_assistant_gui.py

Quick start:
    1. Browse to your Stockfish executable (or put stockfish.exe next to the app).
    2. Open chess.com so the whole board is visible, then click "1. Find board". The app
       hides itself, finds the board on any monitor, and works out which colour is at the
       bottom. ("Select manually" still works: drag a rough box and it snaps to the edges.)
    3. Click "Test read". The board on the left shows exactly what was read from the screen;
       check it against chess.com.
    4. Click "2. Start". A game already in progress is picked up automatically -- no FEN
       needed.

How reading works: every square is rescaled, its own colour is measured (so last-move and
check highlights, and the little move badges, don't matter), and the piece silhouette is
compared with piece shapes learned from a starting position. A built-in set for chess.com's
default pieces is included; if you use another piece set, show the starting position on
screen (analysis board -> reset) and click "Learn pieces" once.

How tracking works: the app keeps its own internal game. Each read it searches for the
shortest sequence of legal moves (up to 3 plies, so it copes with several moves happening
between reads) that produces the position on screen. Squares covered by arrows are ignored.
If the screen jumps in a way no moves explain (analysis navigation, takebacks), the position
is simply re-read from the screen.

Hand-editable board (works whenever tracking is stopped):
    * Drag a piece, or click it and then click its destination. Only legal moves
      are accepted; Stockfish analyses the new position and draws the best move.
    * "Free edit" lets you move anything anywhere, place pieces from the palette
      under the board, and right-click to remove a piece. Use "Switch turn" to
      change whose move it is.
    * Undo / Reset step back or start over; "Board -> FEN" copies the position
      into "Start FEN" so tracking can begin from it.

Please use it for analysing your own games, playing bots, and studying.
Using an engine during live games against humans violates chess.com's Fair Play
policy and will get your account closed.

Notes:
  * macOS: grant your terminal/Python "Screen Recording" permission.
  * Linux: screen capture needs X11 (Wayland generally blocks it).
  * Flat board themes (default green) work best. Promotions are read from the screen.
"""

import base64
import json
import math
import os
import queue
import random
import shutil
import subprocess
import sys
import threading
import time
import zlib
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import chess
import chess.engine
import mss
import numpy as np
from PIL import Image, ImageDraw, ImageTk

# Make Tk and the screen grabber agree on pixel coordinates on Windows hi-DPI.
if sys.platform.startswith("win"):
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass

ARROW = "#1b8a3c"

# Neutral charcoal and light-gray colors used by the application controls.
UI_COLORS = {
    "window": "#24272b", "panel": "#2d3136", "surface": "#383d43",
    "field": "#41464d", "text": "#e5e7e9", "muted": "#b7bcc2",
    "border": "#50565e", "hover": "#4a5057", "pressed": "#202327",
}

# Board themes. best_from = the piece Stockfish wants to move, best_to = where it goes.
THEMES = {
    "Classic Green": dict(light="#eeeed2", dark="#769656", hl_light="#f6f669", hl_dark="#baca2b",
                          best_from=("#8ecbff", "#4f9fe0"), best_to=("#ffa6cf", "#e0609f")),
    "Wood": dict(light="#f0d9b5", dark="#b58863", hl_light="#f7ec74", hl_dark="#dac34b",
                 best_from=("#8ecbff", "#4f9fe0"), best_to=("#9ef0a8", "#3fbf63")),
    "Ocean": dict(light="#dee3e6", dark="#8ca2ad", hl_light="#f6f669", hl_dark="#c9d23a",
                  best_from=("#ffd08a", "#e8962e"), best_to=("#9ef0a8", "#3fbf63")),
    "Rose": dict(light="#f8e3e8", dark="#c97b93", hl_light="#f6f669", hl_dark="#d8c93f",
                 best_from=("#8ecbff", "#4f9fe0"), best_to=("#9ef0a8", "#3fbf63")),
    "Purple": dict(light="#ece9f5", dark="#8b7bb8", hl_light="#f6f669", hl_dark="#c8c93c",
                   best_from=("#ffd08a", "#e8962e"), best_to=("#9ef0a8", "#3fbf63")),
    "Midnight": dict(light="#7d8aa0", dark="#3d4a5f", hl_light="#d8c85a", hl_dark="#a89a2e",
                     best_from=("#7fe3ff", "#2fb4dc"), best_to=("#9ef0a8", "#2fa050")),
}
DEFAULT_THEME = "Classic Green"
PREFS_PATH = os.path.join(os.path.expanduser("~"), ".chess_screen_assistant.json")


# ==========================================================================
# Screen analysis
# ==========================================================================
# --- tunables --------------------------------------------------------------
CELL = 48                    # every square is rescaled to CELL x CELL pixels
PAD = 3                      # pixels ignored on every side (anti-aliasing bleed)
INNER = CELL - 2 * PAD
FEAT = INNER // 2            # resolution of the piece templates
FG_THRESHOLD = 24.0          # RGB distance from the square's own colour = "piece"
HIGHLIGHT_THRESHOLD = 9.0    # RGB distance from a plain square = "highlighted"
EMPTY_AREA = 0.15            # a square with less ink than this is empty
MATCH_MAX = 0.22             # worst template distance that is still accepted
MATCH_MARGIN = 0.03         # best must beat the runner-up by this much


def _build_masks():
    ys, xs = np.mgrid[0:INNER, 0:INNER]
    x, y = (xs + PAD + 0.5) / CELL, (ys + PAD + 0.5) / CELL
    keep = np.ones((INNER, INNER), bool)
    keep &= ~((x < 0.20) & (y < 0.24))   # rank numbers
    keep &= ~((x > 0.74) & (y < 0.25))   # move badges (check mark, !, ?...)
    keep &= ~((x > 0.80) & (y > 0.80))   # file letters
    keep &= ~((x < 0.18) & (y > 0.80))   # a neighbouring square's badge
    ring = np.zeros((CELL, CELL), bool)  # frame used to measure the square's own colour
    ring[2:6, :] = ring[CELL - 6:CELL - 2, :] = True
    ring[:, 2:6] = ring[:, CELL - 6:CELL - 2] = True
    ring[:10, :10] = ring[:10, -10:] = ring[-10:, :10] = ring[-10:, -10:] = False
    return keep, ring


_KEEP, _RING = _build_masks()
_PARITY = (np.add.outer(np.arange(8), np.arange(8)) % 2).astype(bool)


def to_square(row, col, black_at_bottom):
    """Screen (row, col) -> python-chess square."""
    if black_at_bottom:
        return chess.square(7 - col, row)
    return chess.square(col, 7 - row)


def grab(sct, region):
    return np.array(sct.grab(region))[:, :, :3]  # BGRA -> BGR


MAX_HOLES = 0.20             # enclosed background bigger than this is not part of a piece
MAX_AREA = 0.85              # real pieces fill at most ~0.70 of the window; a filled square is ~1.0


def _fill_holes(fg):
    """
    fg: (N, H, W) bool. Fill the enclosed gaps inside each silhouette (hollow pieces).
    A square whose enclosed gap is huge (an overlay ring around the square) is left as is.
    """
    free = ~fg
    outside = np.zeros_like(fg)
    outside[:, 0, :] = free[:, 0, :]
    outside[:, -1, :] = free[:, -1, :]
    outside[:, :, 0] |= free[:, :, 0]
    outside[:, :, -1] |= free[:, :, -1]
    for _ in range(4 * INNER):
        grown = outside.copy()
        grown[:, 1:, :] |= outside[:, :-1, :]
        grown[:, :-1, :] |= outside[:, 1:, :]
        grown[:, :, 1:] |= outside[:, :, :-1]
        grown[:, :, :-1] |= outside[:, :, 1:]
        grown &= free
        if np.array_equal(grown, outside):
            break
        outside = grown
    filled = ~outside
    holes = (filled & free).sum(axis=(1, 2)) / float(INNER * INNER)
    return np.where((holes > MAX_HOLES)[:, None, None], fg, filled)


class CellInfo:
    """Everything measured from one frame, in screen (row, col) coordinates."""

    def __init__(self, img_bgr, fg_threshold=FG_THRESHOLD):
        pil = Image.fromarray(np.ascontiguousarray(img_bgr[:, :, ::-1]))
        pil = pil.resize((8 * CELL, 8 * CELL), Image.Resampling.BOX)
        cells = np.asarray(pil, dtype=np.float32).reshape(8, CELL, 8, CELL, 3)
        cells = cells.transpose(0, 2, 1, 3, 4)                        # row, col, y, x, rgb

        # The colour of each square itself (so highlights and check marks don't matter)
        self.bg = np.median(cells[:, :, _RING], axis=2)               # (8, 8, 3)

        inner = cells[:, :, PAD:CELL - PAD, PAD:CELL - PAD]
        dist = np.sqrt(((inner - self.bg[:, :, None, None, :]) ** 2).sum(axis=4))
        sil = _fill_holes((dist > fg_threshold).reshape(64, INNER, INNER))
        sil = sil.reshape(8, 8, INNER, INNER) & _KEEP
        lum = inner[..., 0] * 0.299 + inner[..., 1] * 0.587 + inner[..., 2] * 0.114

        self.area = sil.sum(axis=(2, 3)) / float(_KEEP.sum())         # (8, 8)
        self.feat = sil.reshape(8, 8, FEAT, 2, FEAT, 2).mean(axis=(3, 5))
        self.occupied = self.area > EMPTY_AREA
        self.oversized = self.area > MAX_AREA
        self.white = np.zeros((8, 8), bool)
        for r, c in zip(*np.where(self.occupied)):
            self.white[r, c] = np.median(lum[r, c][sil[r, c]]) > 150.0

        # Highlighted = the square's colour differs from the plain squares of its parity
        self.highlighted = np.zeros((8, 8), bool)
        for p in (False, True):
            sel = _PARITY == p
            base = np.median(self.bg[sel], axis=0)
            self.highlighted |= sel & (np.linalg.norm(self.bg - base, axis=2) > HIGHLIGHT_THRESHOLD)

    def white_is_at_bottom(self):
        """Guess the board orientation from where the white and black pieces stand."""
        rows_w = np.where(self.occupied & self.white)[0]
        rows_b = np.where(self.occupied & ~self.white)[0]
        if len(rows_w) == 0 or len(rows_b) == 0:
            return None
        return bool(rows_w.mean() > rows_b.mean())


# --------------------------------------------------------------------------
# Piece templates (learned from the starting position)
# --------------------------------------------------------------------------
START_BACK_RANK = "RNBQKBNR"


def start_piece(square):
    rank, file = chess.square_rank(square), chess.square_file(square)
    if rank == 0:
        return START_BACK_RANK[file]
    if rank == 1:
        return "P"
    if rank == 6:
        return "p"
    if rank == 7:
        return START_BACK_RANK[file].lower()
    return None


def learn_templates(info, black_at_bottom):
    """Build {symbol: FEATxFEAT array} from a frame showing the starting position."""
    sums, counts = {}, {}
    problems = 0
    for r in range(8):
        for c in range(8):
            sq = to_square(r, c, black_at_bottom)
            want = start_piece(sq)
            if want is None:
                problems += bool(info.occupied[r, c])
                continue
            if not info.occupied[r, c] or bool(info.white[r, c]) != want.isupper():
                problems += 1
                continue
            sums[want] = sums.get(want, 0) + info.feat[r, c]
            counts[want] = counts.get(want, 0) + 1
    if problems or len(sums) != 12:
        raise ValueError(
            "That doesn't look like the starting position with the colour you picked at the "
            f"bottom ({problems} squares disagree). Show a fresh starting position and try again.")
    return {s: sums[s] / counts[s] for s in sums}


def pack_templates(templates):
    arr = np.stack([templates[s] for s in "PNBRQKpnbrqk"])
    raw = np.round(arr * 255).astype(np.uint8).tobytes()
    return base64.b64encode(zlib.compress(raw, 9)).decode("ascii")


def unpack_templates(text):
    raw = zlib.decompress(base64.b64decode(text))
    arr = np.frombuffer(raw, np.uint8).astype(np.float32).reshape(12, FEAT, FEAT) / 255.0
    return {s: arr[i] for i, s in enumerate("PNBRQKpnbrqk")}


def template_distance(feat, tmpl):
    """0 = identical silhouettes, 1 = nothing in common. Tolerates a +-1 step misalignment."""
    padded = np.pad(feat, 1)
    total = max(1e-6, float(feat.sum() + tmpl.sum()))
    best = 1.0
    for dy in range(3):
        for dx in range(3):
            shifted = padded[dy:dy + FEAT, dx:dx + FEAT]
            best = min(best, float(np.abs(shifted - tmpl).sum()) / total)
    return best


class BoardRead:
    """What the screen shows, as python-chess squares."""

    def __init__(self, info, black_at_bottom, templates):
        self.pieces = {}              # square -> symbol, for squares read confidently
        self.unknown = set()          # occupied but not recognised (arrows, overlays...)
        self.highlighted = frozenset(
            to_square(int(r), int(c), black_at_bottom) for r, c in zip(*np.where(info.highlighted)))
        self.scores = {}
        for r, c in zip(*np.where(info.occupied)):
            sq = to_square(int(r), int(c), black_at_bottom)
            letters = "PNBRQK" if info.white[r, c] else "pnbrqk"
            ranked = sorted((template_distance(info.feat[r, c], templates[s]), s) for s in letters)
            (d1, s1), (d2, _s2) = ranked[0], ranked[1]
            self.scores[sq] = (d1, d2)
            if d1 <= MATCH_MAX and d2 - d1 >= MATCH_MARGIN and not info.oversized[r, c]:
                self.pieces[sq] = s1
            else:
                self.unknown.add(sq)
        self.known = [sq for sq in range(64) if sq not in self.unknown]

    def mismatch(self, board):
        pm = board.piece_map()
        got, n = self.pieces, 0
        for sq in self.known:
            p = pm.get(sq)
            if (p.symbol() if p else None) != got.get(sq):
                n += 1
        return n

    def diff_squares(self, board):
        pm = board.piece_map()
        got = self.pieces
        out = set()
        for sq in self.known:
            p = pm.get(sq)
            if (p.symbol() if p else None) != got.get(sq):
                out.add(sq)
        return out

    def key(self):
        """Hashable summary, used to tell whether two reads show the same thing."""
        return (tuple(sorted(self.pieces.items())), frozenset(self.unknown))

    def placement_fen(self):
        rows = []
        for rank in range(7, -1, -1):
            row, gap = "", 0
            for file in range(8):
                s = self.pieces.get(chess.square(file, rank))
                if s is None:
                    gap += 1
                else:
                    row += (str(gap) if gap else "") + s
                    gap = 0
            rows.append(row + (str(gap) if gap else ""))
        return "/".join(rows)


# --------------------------------------------------------------------------
# Finding the board on the screen
# --------------------------------------------------------------------------
RUN_TOLERANCE = 3            # per-channel difference that still counts as "the same colour"


def _runs(line, min_len):
    """Runs of (nearly) constant colour along a strip of pixels -> [(start, length)]."""
    if len(line) < 2:
        return []
    jump = np.abs(np.diff(line.astype(np.int16), axis=0)).max(axis=1) > RUN_TOLERANCE
    cuts = np.concatenate(([0], np.flatnonzero(jump) + 1, [len(line)]))
    lens = np.diff(cuts)
    keep = lens >= min_len
    return list(zip(cuts[:-1][keep].tolist(), lens[keep].tolist()))


def _eight_windows(runs):
    """
    Every group of eight consecutive blocks of equal width (separated by at most a few
    pixels of anti-aliasing) -> [(start, total_length)] with the outer edges estimated.
    """
    found, i, n = [], 0, len(runs)
    while i < n:
        j = i
        while (j + 1 < n and 0 <= runs[j + 1][0] - (runs[j][0] + runs[j][1]) <= 6
               and abs(runs[j + 1][1] - runs[i][1]) <= 4):
            j += 1
        for k in range(i, j - 6):
            gaps = [runs[m + 1][0] - (runs[m][0] + runs[m][1]) for m in range(k, k + 7)]
            pitch = (runs[k + 7][0] - runs[k][0]) / 7.0
            start = runs[k][0] - sum(gaps) / 7.0 / 2.0
            found.append((int(round(start)), int(round(8 * pitch))))
        i = j + 1
    return found


def _board_score(img, x0, y0, side):
    """How many of the 64 squares match a two-colour checkerboard starting here (0-64)."""
    h, w = img.shape[:2]
    if side < 160 or x0 < 0 or y0 < 0 or x0 + side > w or y0 + side > h:
        return 0
    cell = side / 8.0
    samples = np.zeros((8, 8, 3), np.float32)
    for r in range(8):
        for c in range(8):
            samples[r, c] = img[int(y0 + r * cell + 0.03 * cell), int(x0 + c * cell + 0.5 * cell)]
    near, meds = 0, []
    for p in (False, True):
        sel = samples[_PARITY == p]
        med = np.median(sel, axis=0)
        meds.append(med)
        near += int((np.linalg.norm(sel - med, axis=1) <= 12).sum())
    return near if float(np.linalg.norm(meds[0] - meds[1])) >= 40.0 else 0


def _cluster(votes, slack):
    """Merge nearby (a, b) keys, strongest first -> [((a, b), weight)]."""
    weight = {k: sum(v for q, v in votes.items()
                     if abs(q[0] - k[0]) <= slack and abs(q[1] - k[1]) <= slack) for k in votes}
    picked = []
    for k in sorted(votes, key=lambda k: -weight[k]):
        if all(abs(k[0] - p[0]) > 2 * slack or abs(k[1] - p[1]) > 2 * slack for p, _ in picked):
            picked.append((k, weight[k]))
    return picked


def find_boards(img_bgr, min_cell=20):
    """
    Locate chessboards in a screenshot. A board row is eight equally wide blocks of colour
    side by side, and so is a board column; the theme doesn't matter as long as it is flat.
    Returns [(x, y, side)], best match first.
    """
    h, w = img_bgr.shape[:2]
    votes = {}
    for y in range(h):
        for key in _eight_windows(_runs(img_bgr[y], min_cell)):
            votes[key] = votes.get(key, 0) + 1
    found = []
    # a board shows on many rows: the clean strip at the top of each of its 8 ranks
    for (x0, width), weight in [c for c in _cluster(votes, 2) if c[1] >= 8][:6]:
        col_votes = {}
        for x in range(max(0, x0), min(w, x0 + width)):
            for start, total in _eight_windows(_runs(img_bgr[:, x], min_cell)):
                if abs(total - width) <= 4:
                    key = (start, total)
                    col_votes[key] = col_votes.get(key, 0) + 1
        for (y0, total), weight_y in [c for c in _cluster(col_votes, 2) if c[1] >= 8][:3]:
            side = int(round((width + total) / 2.0))
            if 0 < x0 + side - w <= 8 or 0 < y0 + side - h <= 8:   # touching the screen edge
                side = min(side, w - x0, h - y0)
            score = _board_score(img_bgr, x0, y0, side)
            if score >= 54:
                found.append((score, x0, y0, side))
    found.sort(key=lambda t: (-round(t[3] / 8.0), -t[0]))
    seen, out = [], []
    for score, x0, y0, side in found:
        if not any(abs(x0 - a) < 8 and abs(y0 - b) < 8 and abs(side - c) < 16 for a, b, c in seen):
            seen.append((x0, y0, side))
            out.append((x0, y0, side))
    return out


# Piece shapes for chess.com's default piece set, learned from a starting position.
# Replaced by "Learn pieces" if you use a different set.
DEFAULT_TEMPLATES_B64 = (
    "eNq9Vy1jGzEMNQwMHDQcHBwUHCwsNBwsDBQbHCw8WFg4qJ9ReLAwMNA9f9ydZL/2kjaZQZq82tbH"
    "k+Vn564/9vtd/nv/Ek+H5n/3MY0/FpQMRgsWLO7BzKOdeZdBa4ley/JfGvxd9zTm98/HhMl3u+nL"
    "hJ12FivrB2A8xr8Ke4zzkG6esf+kwCWoVwUu4R9Yjf0GAUOkDqNpM9+CXAIys8NsOvRkmIhYuckz"
    "OCpwBBPnbb3B6vq6WIypDA5MBqQxbcStU0Fhq09rSDoo5alKnJpJPRaFTMVaQxAMmjibaO74yN53"
    "6GiCUa56wDGT77Do4ui7HYMms7IU2BKfQ/GjSVLJprPpLGlnm3hYIRDU5eABRdIElAdB3th5akhP"
    "jhZwAFVneZtBwxvT5zrhtNLknSRKyOVcvfc+J2RMSZFB9RadJelD1wURUXUPKHUOpG4LZJR5dqAL"
    "QHBA4IhO5jSVuiiXY8iIIwjSJcQVHsQkKdsnsanjtbw/mEnpW0LTfCk/c3mxqzPzl8lm3o5rs5Py"
    "MxuRJSiqxmx5zj2oAVGWYl8g7zQrAgUm7fVsSoQbh7gjjgt4FeIgkaArclfxq7ENMFsvyxdr1GYe"
    "JD7nhDtQEBh7MjMIZ/aG+D2Xml3l3B4iAPuYo6+fuMvGz/Xrbre68K+ZVtrIY6/gprHbVpWlWb4g"
    "VflgL9NjWX63qSodp8vr2Eq119hf2w959ROQyRZdST4tOxwUG89Ig51m8KiJ+4FU5TdlywMuxl6E"
    "VMXhkJwfg0POq8Ppt1TlANTBkj4BqjKgwwFPTMrRyGSlppd0mpGrAi60ANqiAzqGRhCSVZW+J25J"
    "NuzeH7XK4JBW7I6lIFUp+nJWjISG5Koqza5+udNa3jM4NIfQ2QbMCuQbqErEpYBXzxqSF9AqzxeQ"
    "N1KVk74fphvXq8VJyFNVldmOn94AXBMnUlOfBDCBStxWlQQU4LAhIP3ZAvJqqhI/4+aHT/iPd5zM"
    "MpA+oypzPgt/VlVKUZX5oyrGKhOhqpxB0qqSZw8oohd4RAKSobrAfSmiIwcLTMBEJEr5ZsRJ3xWJ"
    "1SP/MgEJwURGSfn0KQwaA2ajyGckC+PZ4Jmqki5UlagrnkGcfJ24N2MBz+Q="
)


def default_templates():
    return unpack_templates(DEFAULT_TEMPLATES_B64)


def frames_equal(a, b):
    """True when two captures of the board are (nearly) identical, i.e. nothing is animating."""
    if a.shape != b.shape:
        return False
    changed = (np.abs(a.astype(np.int16) - b.astype(np.int16)).max(axis=2) > 24).sum()
    return changed <= 0.0004 * a.shape[0] * a.shape[1]


def snap_region(sct, region):
    """Tighten a roughly dragged selection onto the real board edges, if a board is there."""
    margin = max(24, region["width"] // 8)
    virt = sct.monitors[0]
    left = max(virt["left"], region["left"] - margin)
    top = max(virt["top"], region["top"] - margin)
    right = min(virt["left"] + virt["width"], region["left"] + region["width"] + margin)
    bottom = min(virt["top"] + virt["height"], region["top"] + region["height"] + margin)
    shot = grab(sct, {"left": left, "top": top, "width": right - left, "height": bottom - top})
    for x, y, side in find_boards(shot):
        if 0.8 * region["width"] <= side <= 1.25 * region["width"]:
            return {"left": left + x, "top": top + y, "width": side, "height": side}
    return region


def find_sequence(board, read, tolerance, max_plies):
    """
    Find the shortest sequence of legal moves from `board` that produces the position the
    screen shows. Squares the reader couldn't identify (arrows, overlays) are ignored and up
    to `tolerance` misread squares are forgiven. The highlighted squares only break ties.
    Returns (list_of_moves, ambiguous) or None.
    """
    root = board.copy(stack=False)
    highlighted = read.highlighted

    for depth in range(1, max_plies + 1):
        found = []  # (mismatch, highlight_penalty, path, final_placement)

        def rec(b, path, remaining):
            if remaining == 1:
                diff = read.diff_squares(b)
                if len(diff) > tolerance + 4:  # one move changes at most four squares
                    return
                for m in list(b.legal_moves):
                    # the square the piece leaves must look wrong on screen (or be unreadable)
                    if m.from_square not in diff and m.from_square not in read.unknown:
                        continue
                    b.push(m)
                    mismatch = read.mismatch(b)
                    if mismatch <= tolerance:
                        penalty = 0 if {m.from_square, m.to_square} <= highlighted else 1
                        found.append((mismatch, penalty, path + [m], b.board_fen()))
                    b.pop()
                return
            for m in list(b.legal_moves):
                if m.promotion and m.promotion != chess.QUEEN:
                    continue
                b.push(m)
                rec(b, path + [m], remaining - 1)
                b.pop()

        rec(root, [], depth)
        if found:
            found.sort(key=lambda t: (t[0], t[1]))
            best = found[0]
            ties = [f for f in found if f[:2] == best[:2]]
            ambiguous = len({f[3] for f in ties}) > 1
            return best[2], ambiguous
    return None


def guess_board(read, us):
    """
    Build a Board straight from what the screen shows (no move history). Returns None unless
    every piece was recognised and the result is a legal chess position.
    Side to move comes from the highlighted last-move squares, then from legality (the side
    not to move can't be in check), then defaults to `us`. Castling rights are assumed
    wherever king and rook stand on their home squares.
    """
    if read.unknown:
        return None
    p = read.pieces
    castling = ""
    if p.get(chess.E1) == "K":
        castling += ("K" if p.get(chess.H1) == "R" else "") + ("Q" if p.get(chess.A1) == "R" else "")
    if p.get(chess.E8) == "k":
        castling += ("k" if p.get(chess.H8) == "r" else "") + ("q" if p.get(chess.A8) == "r" else "")
    castling = castling or "-"

    # The piece that just moved stands on a highlighted square (ignore kings: a king
    # in check can be highlighted too, and it belongs to the side that is to move).
    movers = {p[sq].isupper() for sq in read.highlighted if sq in p and p[sq] not in "Kk"}
    order = []
    if len(movers) == 1:
        order.append(not next(iter(movers)))  # white moved -> black to move
    order += [us, not us]
    tried = []
    for white_to_move in order:
        if white_to_move in tried:
            continue
        tried.append(white_to_move)
        try:
            b = chess.Board(f"{read.placement_fen()} {'w' if white_to_move else 'b'} {castling} - 0 1")
        except ValueError:
            return None
        if b.is_valid():
            return b
    return None


def square_names(squares):
    return ", ".join(sorted(chess.square_name(s) for s in squares)) or "none"


def format_eval(score, name):
    """Describe a score that is already from `name`'s point of view ('White'/'Black')."""
    if score.is_mate():
        n = score.mate()
        if n == 0:
            return f"#0 ({name} is checkmated)"
        return f"#{n} ({name} mates in {n})" if n > 0 else f"#{n} ({name} gets mated in {-n})"
    cp = score.score()
    if cp >= 300:
        tag = f"{name} is winning"
    elif cp >= 100:
        tag = f"{name} is better"
    elif cp > 30:
        tag = f"slightly better for {name}"
    elif cp >= -30:
        tag = "equal"
    elif cp > -100:
        tag = f"slightly worse for {name}"
    elif cp > -300:
        tag = f"{name} is worse"
    else:
        tag = f"{name} is losing"
    return f"{cp / 100:+.2f} ({tag})"


# ==========================================================================
# Self-healing Stockfish wrapper
# ==========================================================================
class EngineHandle:
    """
    Wraps a Stockfish process. If the process dies (python-chess then reports
    "engine event loop dead" on every later call), it is restarted transparently
    and the search is retried once. The exit code is reported through `log`.
    """

    def __init__(self, path, log=None):
        self.path = path
        self.log = log or (lambda msg: None)
        self.engine = None

    def start(self):
        self.close()
        kwargs = {"stderr": subprocess.DEVNULL}
        if sys.platform.startswith("win"):
            # No black console window; also keeps Stockfish alive in a --windowed .exe
            kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
        self.engine = chess.engine.SimpleEngine.popen_uci(self.path, **kwargs)

    def _exit_code(self):
        try:
            return self.engine.returncode.result(timeout=1)
        except Exception:
            return "unknown"

    def analyse(self, board, seconds):
        last_error = None
        for attempt in (1, 2):
            try:
                if self.engine is None:
                    self.start()
                return self.engine.analyse(board, chess.engine.Limit(time=seconds))
            except Exception as e:  # EngineTerminatedError, timeouts, broken pipes...
                last_error = e
                code = self._exit_code() if self.engine is not None else "n/a"
                self.log(f"Stockfish stopped unexpectedly ({type(e).__name__}: {e}; "
                         f"exit code {code}). Restarting it…")
                self.close()
        raise RuntimeError(f"Stockfish keeps crashing on this position: {last_error}")

    def close(self):
        if self.engine is not None:
            try:
                self.engine.quit()
            except Exception:
                try:
                    self.engine.close()
                except Exception:
                    pass
            self.engine = None


# ==========================================================================
# Background worker (screen reading + engine)
# ==========================================================================
class Worker(threading.Thread):
    def __init__(self, cfg, q, stop_evt):
        super().__init__(daemon=True)
        self.cfg, self.q, self.stop = cfg, q, stop_evt
        self.exit_reason = None

    def post(self, *msg):
        self.q.put(msg)

    # ---- thread entry -----------------------------------------------------
    def run(self):
        engine = EngineHandle(self.cfg["engine"], log=lambda m: self.post("log", m))
        try:
            engine.start()
        except Exception as e:
            self.post("error", f"Could not start Stockfish: {e}")
            self.post("stopped")
            return
        try:
            with mss.mss() as sct:
                self.loop(sct, engine)
        except Exception as e:
            self.post("error", f"{type(e).__name__}: {e}")
        finally:
            engine.close()
            self.post("stopped", self.exit_reason)

    # ---- helpers ------------------------------------------------------------
    def read_stable(self, sct):
        """Read the board once it has stopped moving (no drag / slide animation)."""
        c = self.cfg
        prev = grab(sct, c["region"])
        for _ in range(8):
            if self.stop.wait(0.12):
                return None
            cur = grab(sct, c["region"])
            if frames_equal(prev, cur):
                return BoardRead(CellInfo(cur, c["fg_threshold"]), c["black_bottom"], c["templates"])
            prev = cur
        return None

    def publish(self, board, start):
        last = board.peek().uci() if board.move_stack else None
        moves = start.variation_san(board.move_stack) if board.move_stack else ""
        self.post("position", board.fen(), last, moves)

    def suggest(self, engine, board):
        c = self.cfg
        if board.is_game_over():
            self.post("best", None, f"Game over: {board.result()}")
            return
        if board.turn != c["us"] and not c["both"]:
            self.post("best", None, "Waiting for your opponent to move…")
            return
        self.post("status", "Stockfish is thinking…")
        info = engine.analyse(board, c["think"])
        best = info["pv"][0]
        score = info["score"].pov(c["us"])
        ev = format_eval(score, "White" if c["us"] == chess.WHITE else "Black")
        self.post("best", (best.uci(), board.san(best), ev), "")

    def initial_board(self, sct):
        """Starting position, the pasted FEN, or (for a game in progress) what the screen shows."""
        c = self.cfg
        if c["fen"]:
            return chess.Board(c["fen"])
        read = None
        for _ in range(4):
            read = self.read_stable(sct)
            if read is not None or self.stop.wait(0.3):
                break
        if read is None:
            self.post("log", "Couldn't read the board yet; assuming the starting position.")
            return chess.Board()
        if not read.unknown and read.placement_fen() == chess.Board().board_fen():
            return chess.Board()
        board = guess_board(read, c["us"])
        if board is None:
            self.post("log", f"Couldn't make sense of the position on screen "
                             f"({len(read.unknown)} unreadable squares); assuming the starting "
                             "position. Check the board is fully visible with no arrows over it.")
            return chess.Board()
        self.post("log", "Game already in progress: loaded the current position from the screen "
                         f"({'White' if board.turn else 'Black'} to move).")
        return board

    # ---- main loop ------------------------------------------------------------
    def loop(self, sct, engine):
        c = self.cfg
        board = self.initial_board(sct)
        start = board.copy()
        tol = c["tolerance"]
        pending = None       # unmatched reading waiting for a second, confirming read
        last_warn = None

        self.publish(board, start)
        self.suggest(engine, board)
        self.post("status", "Running. Waiting for the first read…")

        while not self.stop.is_set():
            cycle_started = time.monotonic()
            read = self.read_stable(sct)
            now = time.strftime("%H:%M:%S")

            if read is None:
                self.post("status", f"{now}  Board is changing (drag/animation), retrying…")
                if self.stop.wait(0.15):
                    break
                continue

            status = f"{now}  No new move."
            if len(read.unknown) > 8:
                status = f"{now}  Can't read the board ({len(read.unknown)} squares covered?)."
            elif read.mismatch(board) <= tol:
                pending, last_warn = None, None
            else:
                result = find_sequence(board, read, tol, c["max_plies"])
                if result:
                    seq, ambiguous = result
                    for m in seq:
                        who = "White" if board.turn == chess.WHITE else "Black"
                        san = board.san(m)
                        board.push(m)
                        self.post("log", f"{who}: {san}")
                    if len(seq) > 1:
                        self.post("log", f"(caught up on {len(seq)} moves since the last read)")
                    if ambiguous:
                        self.post("log", "Warning: several move orders fit; picked the first.")
                    pending = last_warn = None
                    status = f"{now}  Position updated."
                    self.publish(board, start)
                    self.suggest(engine, board)
                elif pending != read.key():
                    # Not reachable by legal moves: confirm with a second read before
                    # trusting it (a half-dragged piece looks like this for a moment).
                    pending = read.key()
                    self.post("status", f"{now}  Position changed, confirming…")
                    if self.stop.wait(0.4):
                        break
                    continue
                else:
                    new_board = guess_board(read, c["us"])
                    if new_board is not None:
                        board, start = new_board, new_board.copy()
                        pending = last_warn = None
                        self.post("log", "Position jumped (analysis navigation, takeback or lost "
                                         "sync): re-read it from the screen.")
                        status = f"{now}  Position re-synced from the screen."
                        self.publish(board, start)
                        self.suggest(engine, board)
                    elif last_warn != read.key():
                        last_warn = read.key()
                        self.post("log", "The screen doesn't match a legal position "
                                         f"({len(read.unknown)} unreadable squares). Is something "
                                         "covering the board?")
                        status = f"{now}  Can't read a valid position — tracking continues."

            if board.is_game_over():
                self.exit_reason = f"Game over: {board.result()}"
                self.post("status", self.exit_reason)
                break

            self.post("status", status)
            # Include screen reads and engine analysis in the configured interval.
            remaining = max(0.0, c["interval"] - (time.monotonic() - cycle_started))
            if remaining and self.stop.wait(remaining):
                break


# ==========================================================================
# Region selection (drag a box over a screenshot)
# ==========================================================================
def select_region(root):
    with mss.mss() as sct:
        mon = sct.monitors[1]
        shot = sct.grab(mon)
    img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")

    top = tk.Toplevel(root)
    top.attributes("-fullscreen", True)
    top.attributes("-topmost", True)
    top.update_idletasks()
    sw, sh = top.winfo_screenwidth(), top.winfo_screenheight()

    photo = ImageTk.PhotoImage(img.resize((sw, sh)))
    canvas = tk.Canvas(top, width=sw, height=sh, highlightthickness=0, cursor="crosshair")
    canvas.pack(fill="both", expand=True)
    canvas.create_image(0, 0, image=photo, anchor="nw")
    canvas.image = photo
    canvas.create_rectangle(0, 0, sw, 34, fill="black", outline="")
    canvas.create_text(
        sw // 2, 17, fill="white", font=("Helvetica", 13, "bold"),
        text="Drag a box around the 8x8 board squares (not the outer coordinates).  Esc = cancel",
    )

    state = {"start": None, "rect": None, "result": None}

    def press(e):
        state["start"] = (e.x, e.y)
        state["rect"] = canvas.create_rectangle(e.x, e.y, e.x, e.y, outline="#ff3b30", width=2)

    def drag(e):
        if state["rect"]:
            x0, y0 = state["start"]
            canvas.coords(state["rect"], x0, y0, e.x, e.y)

    def release(e):
        if not state["start"]:
            return
        x0, y0 = state["start"]
        x1, y1 = e.x, e.y
        left, top_, w, h = min(x0, x1), min(y0, y1), abs(x1 - x0), abs(y1 - y0)
        if w > 40 and h > 40:
            state["result"] = (left, top_, w, h)
        top.destroy()

    top.bind("<Escape>", lambda e: top.destroy())
    canvas.bind("<ButtonPress-1>", press)
    canvas.bind("<B1-Motion>", drag)
    canvas.bind("<ButtonRelease-1>", release)
    top.focus_force()
    root.wait_window(top)

    if not state["result"]:
        return None
    fx, fy = img.width / sw, img.height / sh
    left, top_, w, h = state["result"]
    side = int(min(w * fx, h * fy)) // 8 * 8
    return {
        "left": mon["left"] + int(left * fx),
        "top": mon["top"] + int(top_ * fy),
        "width": side,
        "height": side,
    }


class AnalysisService(threading.Thread):
    """
    Long-lived Stockfish process used for the hand-editable board. Only the most
    recent request matters: if you move again mid-analysis, the old one is dropped.
    """

    def __init__(self, engine_path, out_q):
        super().__init__(daemon=True)
        self.path, self.out = engine_path, out_q
        self.req = queue.Queue()

    def request(self, req_id, fen, think, us):
        self.req.put((req_id, fen, think, us))

    def shutdown(self):
        self.req.put(None)

    def run(self):
        engine = EngineHandle(self.path, log=lambda m: self.out.put(("log", m)))
        try:
            engine.start()
        except Exception as e:
            self.out.put(("manual_error", f"Could not start Stockfish: {e}"))
            return
        try:
            while True:
                item = self.req.get()
                while item is not None:  # drain: keep only the newest request
                    try:
                        nxt = self.req.get_nowait()
                    except queue.Empty:
                        break
                    item = nxt
                if item is None:
                    break

                req_id, fen, think, us = item
                board = chess.Board(fen)
                try:
                    info = engine.analyse(board, think)
                except Exception as e:  # keep the service alive after a failed search
                    self.out.put(("manual_error", f"{e}"))
                    continue
                if not self.req.empty():
                    continue  # a newer position is waiting; this result is stale

                pv, score = info.get("pv"), info.get("score")
                if not pv or score is None:
                    self.out.put(("manual_best", req_id, None, "No move found."))
                    continue
                # Eval is always from the selected colour's point of view
                ev = format_eval(score.pov(us), "White" if us == chess.WHITE else "Black")
                mine = board.turn == us
                self.out.put(("manual_best", req_id, (pv[0].uci(), board.san(pv[0]), ev, mine), ""))
        finally:
            engine.close()


def find_default_engine():
    """Look for a Stockfish binary next to the app (or exe), then on PATH."""
    if getattr(sys, "frozen", False):
        base = os.path.dirname(sys.executable)
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    for folder in (base, os.path.join(base, "stockfish")):
        if not os.path.isdir(folder):
            continue
        for name in sorted(os.listdir(folder)):
            low = name.lower()
            path = os.path.join(folder, name)
            if (low.startswith("stockfish") and os.path.isfile(path)
                    and not low.endswith((".zip", ".txt", ".md", ".py", ".dll"))):
                return path
    return shutil.which("stockfish") or ""


# ==========================================================================
# Main application window
# ==========================================================================
class App:
    BOARD_PX = 480
    HALO = ((-1.2, 0), (1.2, 0), (0, -1.2), (0, 1.2), (-1, -1), (1, -1), (-1, 1), (1, 1))

    def __init__(self, root):
        self.root = root
        root.title("Chess Screen Assistant")
        root.resizable(True, True)
        self.board_px = self.BOARD_PX
        self._resize_pending = False
        self.configure_ui_style()

        self.q = queue.Queue()
        self.worker = None
        self.stop_evt = None
        self.running = False          # True while the screen tracker is active
        self.region = None
        self.preview_photo = None

        # Board state shown on the left (shared by the tracker and manual editing)
        self.board = chess.Board()
        self.last_move = None
        self.best_move = None

        # Manual-editing state
        self.service = None           # AnalysisService
        self.req_id = 0
        self.undo_stack = []
        self.hist_start = chess.STARTING_FEN
        self.hist_moves = []
        self.selected = None
        self.drag_from = None
        self.drag_start = (0, 0)
        self.drag_last = (0, 0)
        self.dragging = False
        self.brush = None

        if sys.platform.startswith("win"):
            self.glyph_font = ("Segoe UI Symbol", 36)
        elif sys.platform == "darwin":
            self.glyph_font = ("Apple Symbols", 40)
        else:
            self.glyph_font = ("DejaVu Sans", 36)

        # Appearance settings
        self.theme_var = tk.StringVar(value=DEFAULT_THEME)
        self.performance_var = tk.StringVar(value="Mid PC")
        self.snow_var = tk.BooleanVar(value=False)
        self.rain_var = tk.BooleanVar(value=False)
        self.fx_amount_var = tk.DoubleVar(value=60)
        self.fx_fps_var = tk.IntVar(value=80)
        self.fx_fps_display_var = tk.StringVar(value="80 FPS")
        self.auto_play_var = tk.BooleanVar(value=False)
        self.arrow_var = tk.BooleanVar(value=True)
        self.hl_best_var = tk.BooleanVar(value=True)
        self.fx_particles = []
        self.settings_win = None
        self.theme_preview = None
        self.fx_overlay = None
        self.fx_canvas = None
        self._fx_last_time = time.perf_counter()
        self.templates = default_templates()
        self.load_prefs()

        self.build_ui()
        self.on_performance_preset(save=False)
        root.minsize(820, 700)
        root.grid_rowconfigure(0, weight=1)
        root.grid_columnconfigure(0, weight=1)
        self.create_fx_overlay()
        root.bind("<Configure>", self.on_root_configure, add="+")
        self.fx_rebuild()
        self.draw_board()
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.pump()
        self.fx_tick()

    def configure_ui_style(self):
        """Apply the gray UI palette and native rounded corners where supported."""
        c = UI_COLORS
        self.root.configure(background=c["window"])
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure(".", background=c["window"], foreground=c["text"],
                        font=("Segoe UI", 9))
        style.configure("TFrame", background=c["window"])
        style.configure("TLabelframe", background=c["panel"], foreground=c["text"],
                        bordercolor=c["panel"], lightcolor=c["panel"], darkcolor=c["panel"])
        style.configure("TLabelframe.Label", background=c["panel"], foreground=c["text"])
        style.configure("TLabel", background=c["window"], foreground=c["text"])
        style.configure("Muted.TLabel", background=c["window"], foreground=c["muted"])
        style.configure("TButton", background=c["surface"], foreground=c["text"],
                        bordercolor=c["surface"], lightcolor=c["surface"], darkcolor=c["surface"],
                        padding=(8, 4))
        style.map("TButton", background=[("pressed", c["pressed"]), ("active", c["hover"])])
        try:
            self._style_images = []

            def rounded_image(fill, size=(40, 36), radius=10):
                # Supersampling gives the transparent corners clean, soft edges.
                scale = 4
                large_size = (size[0] * scale, size[1] * scale)
                img = Image.new("RGBA", large_size, (0, 0, 0, 0))
                ImageDraw.Draw(img).rounded_rectangle(
                    (scale, scale, large_size[0] - scale - 1,
                     large_size[1] - scale - 1), radius=radius * scale, fill=fill)
                img = img.resize(size, Image.Resampling.LANCZOS)
                photo = ImageTk.PhotoImage(img, master=self.root)
                self._style_images.append(photo)
                return photo

            button_normal = rounded_image(c["surface"])
            button_hover = rounded_image(c["hover"])
            button_pressed = rounded_image(c["pressed"])
            style.element_create("RoundedButton.background", "image", button_normal,
                                 ("pressed", button_pressed), ("active", button_hover),
                                 border=12, sticky="nsew")
            style.layout("TButton", [("RoundedButton.background", {
                "sticky": "nsew", "children": [("Button.padding", {
                    "sticky": "nsew", "children": [("Button.label", {"sticky": "nsew"})]
                })]
            })])

            entry_bg = rounded_image(c["field"])
            style.element_create("RoundedEntry.background", "image", entry_bg,
                                 border=12, sticky="nsew")
            style.layout("TEntry", [("RoundedEntry.background", {
                "sticky": "nsew", "children": [("Entry.padding", {
                    "sticky": "nsew", "children": [("Entry.textarea", {"sticky": "nsew"})]
                })]
            })])

            panel_bg = rounded_image(c["panel"], size=(40, 40), radius=12)
            style.element_create("RoundedPanel.border", "image", panel_bg,
                                 border=12, sticky="nsew")

            def replace_element(layout, old, new):
                replaced = []
                for element, options in layout:
                    options = dict(options)
                    if "children" in options:
                        options["children"] = replace_element(options["children"], old, new)
                    replaced.append((new if element == old else element, options))
                return replaced

            for widget_style, old_element, new_element in (
                ("TSpinbox", "Spinbox.field", "RoundedEntry.background"),
                ("TCombobox", "Combobox.field", "RoundedEntry.background"),
                ("TLabelframe", "Labelframe.border", "RoundedPanel.border"),
            ):
                style.layout(widget_style, replace_element(
                    style.layout(widget_style), old_element, new_element))
        except (tk.TclError, AttributeError):
            # Keep the regular controls if the active Tk theme lacks image elements.
            pass
        style.configure("TCheckbutton", background=c["window"], foreground=c["text"])
        style.map("TCheckbutton", background=[("active", c["window"])],
                  foreground=[("disabled", c["muted"])])
        style.configure("TRadiobutton", background=c["window"], foreground=c["text"])
        style.map("TRadiobutton", background=[("active", c["window"])])
        style.configure("TEntry", fieldbackground=c["field"], foreground=c["text"],
                        insertcolor=c["text"], bordercolor=c["field"])
        style.configure("TSpinbox", fieldbackground=c["field"], foreground=c["text"],
                        insertcolor=c["text"], bordercolor=c["field"], arrowcolor=c["text"])
        style.configure("TCombobox", fieldbackground=c["field"], foreground=c["text"],
                        background=c["surface"], bordercolor=c["field"], arrowcolor=c["text"])
        style.map("TCombobox", fieldbackground=[("readonly", c["field"])],
                  foreground=[("readonly", c["text"])])
        style.configure("Horizontal.TScale", background=c["panel"], troughcolor=c["field"])
        if sys.platform.startswith("win"):
            self.root.after_idle(lambda: self.apply_rounded_corners(self.root))
            self.root.after_idle(self.apply_glass_backdrop)
            self.root.after_idle(lambda: self.apply_dark_titlebar(self.root))

    @staticmethod
    def apply_dark_titlebar(window):
        """Set a black caption and a light title on Windows native title bars."""
        try:
            import ctypes
            hwnd = ctypes.windll.user32.GetParent(window.winfo_id())
            black, white = ctypes.c_int(0x000000), ctypes.c_int(0xFFFFFF)
            for attribute, color in ((35, black), (36, white), (34, black)):
                ctypes.windll.dwmapi.DwmSetWindowAttribute(
                    hwnd, attribute, ctypes.byref(color), ctypes.sizeof(color))
        except Exception:
            pass  # Native caption colors are supported on Windows 11.

    def apply_glass_backdrop(self):
        """Use Windows' translucent Mica backdrop when the OS supports it."""
        try:
            import ctypes
            hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id())
            backdrop = ctypes.c_int(2)  # DWMSBT_MAINWINDOW (Mica)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd, 38, ctypes.byref(backdrop), ctypes.sizeof(backdrop))
            # A very small amount of translucency softens the gray UI without
            # reducing contrast or making text difficult to read.
            self.root.attributes("-alpha", 0.97)
        except Exception:
            pass  # Keep the opaque gray theme on systems without DWM backdrop support.

    def create_fx_overlay(self):
        """Create a click-through transparent layer for window-wide weather."""
        if not sys.platform.startswith("win"):
            return
        try:
            self.root.update_idletasks()
            key = "#010203"
            overlay = tk.Toplevel(self.root)
            self.fx_overlay = overlay
            overlay.overrideredirect(True)
            overlay.transient(self.root)
            overlay.configure(background=key)
            overlay.wm_attributes("-transparentcolor", key)
            self.fx_canvas = tk.Canvas(overlay, background=key, highlightthickness=0,
                                      bd=0, relief="flat")
            self.fx_canvas.pack(fill="both", expand=True)
            overlay.update_idletasks()

            # Layered + transparent + no-activate keeps all input on the real UI.
            import ctypes
            hwnd = ctypes.windll.user32.GetParent(overlay.winfo_id())
            get_long = ctypes.windll.user32.GetWindowLongW
            set_long = ctypes.windll.user32.SetWindowLongW
            get_long.restype = ctypes.c_long
            get_long.argtypes = (ctypes.c_void_p, ctypes.c_int)
            set_long.restype = ctypes.c_long
            set_long.argtypes = (ctypes.c_void_p, ctypes.c_int, ctypes.c_long)
            ex_style = get_long(hwnd, -20)
            set_long(hwnd, -20, ex_style | 0x00000020 | 0x00080000 | 0x08000000)
            self.root.bind("<FocusIn>", lambda _e: self.sync_fx_overlay(), add="+")
            self.sync_fx_overlay()
        except Exception:
            self.fx_overlay = self.fx_canvas = None

    def on_root_configure(self, event):
        if event.widget is self.root:
            self.root.after_idle(self.sync_fx_overlay)
            if not self._resize_pending:
                self._resize_pending = True
                self.root.after_idle(self.resize_ui_to_window)

    def resize_ui_to_window(self):
        """Scale the square board to the space available in the resizable window."""
        self._resize_pending = False
        if not self.root.winfo_exists() or not hasattr(self, "right_panel"):
            return
        available_width = (self.root.winfo_width() - self.right_panel.winfo_reqwidth() - 66)
        available_height = self.root.winfo_height() - 190
        size = max(280, min(900, available_width, available_height))
        if size == self.board_px:
            return
        self.board_px = size
        if hasattr(self, "glyph_font"):
            self.glyph_font = (self.glyph_font[0], max(20, round(36 * size / self.BOARD_PX)))
        self.canvas.configure(width=size, height=size)
        self.direction_label.configure(wraplength=size)
        self.draw_board()
        self.fx_rebuild()

    def sync_fx_overlay(self):
        """Keep the transparent particle layer aligned to the application client area."""
        overlay = self.fx_overlay
        if overlay is None or not overlay.winfo_exists():
            return
        if self.root.state() != "normal":
            overlay.withdraw()
            return
        x, y = self.root.winfo_rootx(), self.root.winfo_rooty()
        width, height = self.root.winfo_width(), self.root.winfo_height()
        if width < 2 or height < 2:
            return
        overlay.geometry(f"{width}x{height}+{x}+{y}")
        overlay.deiconify()
        overlay.lift()

    @staticmethod
    def apply_rounded_corners(window):
        """Request rounded native window corners on Windows 11."""
        try:
            import ctypes
            hwnd = ctypes.windll.user32.GetParent(window.winfo_id())
            preference = ctypes.c_int(2)  # DWMWCP_ROUND
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd, 33, ctypes.byref(preference), ctypes.sizeof(preference))
        except Exception:
            pass  # Older Windows versions do not expose this corner preference.

    # ---- UI construction --------------------------------------------------------
    def build_ui(self):
        pad = {"padx": 6, "pady": 3}
        outer = ttk.Frame(self.root, padding=10)
        outer.grid(row=0, column=0, sticky="nsew")

        # ---- Left column: board + editing toolbar ----
        left = ttk.Frame(outer)
        left.grid(row=0, column=0, sticky="n")

        self.canvas = tk.Canvas(left, width=self.board_px, height=self.board_px,
                                highlightthickness=0, cursor="hand2")
        self.canvas.grid(row=0, column=0)
        self.canvas.bind("<ButtonPress-1>", self.on_press)
        self.canvas.bind("<B1-Motion>", self.on_motion)
        self.canvas.bind("<ButtonRelease-1>", self.on_release)
        self.canvas.bind("<Button-3>", self.on_right_click)
        self.canvas.bind("<Button-2>", self.on_right_click)  # right-click on macOS

        tb = ttk.Frame(left)
        tb.grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.reset_btn = ttk.Button(tb, text="Reset", width=7, command=self.on_reset)
        self.undo_btn = ttk.Button(tb, text="Undo", width=7, command=self.on_undo)
        self.turn_btn = ttk.Button(tb, text="Switch turn", command=self.on_switch_turn)
        self.free_var = tk.BooleanVar(value=False)
        self.free_cb = ttk.Checkbutton(tb, text="Free edit", variable=self.free_var,
                                       command=self.on_free_toggle)
        self.settings_btn = ttk.Button(tb, text="Settings", command=self.open_settings)
        for i, w in enumerate((self.reset_btn, self.undo_btn, self.turn_btn, self.free_cb,
                               self.settings_btn)):
            w.grid(row=0, column=i, padx=3)

        # Piece palette (used in Free edit mode)
        pal = ttk.Frame(left)
        pal.grid(row=2, column=0, sticky="w", pady=(6, 0))
        self.palette = {}
        small = (self.glyph_font[0], 20)
        for i, (color, fg) in enumerate(((chess.WHITE, "#ffffff"), (chess.BLACK, "#111111"))):
            for j, pt in enumerate((chess.KING, chess.QUEEN, chess.ROOK,
                                    chess.BISHOP, chess.KNIGHT, chess.PAWN)):
                piece = chess.Piece(pt, color)
                lbl = tk.Label(pal, text=chess.Piece(pt, chess.BLACK).unicode_symbol(),
                               font=small, fg=fg, bg=UI_COLORS["surface"],
                               width=2, relief="raised", bd=2)
                lbl.grid(row=0, column=i * 6 + j, padx=1)
                lbl.bind("<Button-1>", lambda ev, p=piece: self.pick_brush(p))
                self.palette[piece] = lbl

        self.direction_label = ttk.Label(
            left, style="Muted.TLabel", wraplength=self.board_px, justify="left",
            text=("Drag a piece (or click it, then click its destination) and Stockfish "
                  "analyses the new position. Free edit: move anything anywhere, pick a "
                  "piece above and click a square to place it, right-click to remove."),
        )
        self.direction_label.grid(row=3, column=0, sticky="w", pady=(6, 0))
        self.auto_play_cb = ttk.Checkbutton(
            left, text="auto play stockfish move (crash backup)",
            variable=self.auto_play_var, command=self.on_auto_play_toggle)
        self.auto_play_cb.grid(row=4, column=0, sticky="w", pady=(5, 0))

        # ---- Right column ----
        right = ttk.Frame(outer)
        right.grid(row=0, column=1, sticky="n", padx=(12, 0))
        self.right_panel = right

        # Settings
        s = ttk.LabelFrame(right, text="Tracking & engine")
        s.grid(row=0, column=0, sticky="ew")
        self.engine_var = tk.StringVar(value=find_default_engine())
        self.color_var = tk.StringVar(value="white")
        self.interval_var = tk.StringVar(value="5")
        self.think_var = tk.StringVar(value="1.5")
        self.both_var = tk.BooleanVar(value=False)
        self.fen_var = tk.StringVar()
        self.occ_var = tk.StringVar(value=str(int(FG_THRESHOLD)))
        self.tol_var = tk.StringVar(value="1")

        ttk.Label(s, text="Stockfish:").grid(row=0, column=0, sticky="w", **pad)
        e = ttk.Entry(s, textvariable=self.engine_var, width=30)
        e.grid(row=0, column=1, **pad)
        b = ttk.Button(s, text="Browse…", command=self.browse_engine)
        b.grid(row=0, column=2, **pad)

        ttk.Label(s, text="I'm playing (bottom):").grid(row=1, column=0, sticky="w", **pad)
        cf = ttk.Frame(s)
        cf.grid(row=1, column=1, sticky="w", **pad)
        rb1 = ttk.Radiobutton(cf, text="White", variable=self.color_var, value="white",
                              command=self.on_color_change)
        rb2 = ttk.Radiobutton(cf, text="Black", variable=self.color_var, value="black",
                              command=self.on_color_change)
        rb1.pack(side="left")
        rb2.pack(side="left", padx=8)

        ttk.Label(s, text="Read every (s):").grid(row=2, column=0, sticky="w", **pad)
        sp1 = ttk.Spinbox(s, from_=1, to=60, increment=1, width=6, textvariable=self.interval_var)
        sp1.grid(row=2, column=1, sticky="w", **pad)

        ttk.Label(s, text="Think time (s):").grid(row=3, column=0, sticky="w", **pad)
        sp2 = ttk.Spinbox(s, from_=0.2, to=30, increment=0.5, width=6, textvariable=self.think_var)
        sp2.grid(row=3, column=1, sticky="w", **pad)

        ttk.Label(s, text="Performance preset:").grid(row=4, column=0, sticky="w", **pad)
        preset = ttk.Combobox(s, textvariable=self.performance_var,
                              values=("Potato PC", "Mid PC", "Good PC"),
                              state="readonly", width=18)
        preset.grid(row=4, column=1, columnspan=2, sticky="w", **pad)
        preset.bind("<<ComboboxSelected>>", self.on_performance_preset)

        cb = ttk.Checkbutton(s, text="Show best move for both sides (tracking)",
                             variable=self.both_var)
        cb.grid(row=5, column=0, columnspan=3, sticky="w", **pad)

        ttk.Label(s, text="Start FEN (optional):").grid(row=6, column=0, sticky="w", **pad)
        fe = ttk.Entry(s, textvariable=self.fen_var, width=30)
        fe.grid(row=6, column=1, columnspan=2, sticky="we", **pad)

        ff = ttk.Frame(s)
        ff.grid(row=7, column=1, columnspan=2, sticky="w", **pad)
        lf_btn = ttk.Button(ff, text="FEN → board", command=self.on_load_fen)
        bf_btn = ttk.Button(ff, text="Board → FEN", command=self.on_board_to_fen)
        lf_btn.pack(side="left")
        bf_btn.pack(side="left", padx=6)

        ttk.Label(s, text="Piece sensitivity (lower = more):").grid(row=8, column=0, sticky="w", **pad)
        sp3 = ttk.Spinbox(s, from_=8, to=80, increment=1, width=6, textvariable=self.occ_var)
        sp3.grid(row=8, column=1, sticky="w", **pad)

        ttk.Label(s, text="Misread tolerance (squares):").grid(row=9, column=0, sticky="w", **pad)
        sp4 = ttk.Spinbox(s, from_=0, to=4, increment=1, width=6, textvariable=self.tol_var)
        sp4.grid(row=9, column=1, sticky="w", **pad)

        # Widgets that are locked while the screen tracker is running
        self.config_widgets = [e, b, rb1, rb2, sp1, sp2, cb, fe, lf_btn, bf_btn, sp3, sp4,
                               self.reset_btn, self.undo_btn, self.turn_btn, self.free_cb,
                               self.auto_play_cb]

        # Buttons
        bf = ttk.Frame(right)
        bf.grid(row=1, column=0, sticky="ew", pady=8)
        self.auto_btn = ttk.Button(bf, text="1. Find board", command=self.on_auto_find)
        self.select_btn = ttk.Button(bf, text="Select manually", command=self.on_select)
        self.test_btn = ttk.Button(bf, text="Test read", command=self.on_test)
        self.learn_btn = ttk.Button(bf, text="Learn pieces", command=self.on_learn)
        self.start_btn = ttk.Button(bf, text="2. Start", command=self.on_start)
        self.stop_btn = ttk.Button(bf, text="Stop", command=self.on_stop, state="disabled")
        for i, w in enumerate((self.auto_btn, self.select_btn, self.test_btn,
                               self.learn_btn, self.start_btn, self.stop_btn)):
            w.grid(row=i // 3, column=i % 3, padx=3, pady=2)

        # Region preview + label
        self.region_var = tk.StringVar(
            value=("No board yet. Click '1. Find board' with chess.com open. You can switch away from "
                   "this app while tracking; keep the board visible on the desktop."))
        ttk.Label(right, textvariable=self.region_var, style="Muted.TLabel",
                  wraplength=420, justify="left").grid(
            row=2, column=0, sticky="w")
        self.preview = ttk.Label(right)
        self.preview.grid(row=3, column=0, sticky="w", pady=2)

        # Best move
        bm = ttk.LabelFrame(right, text="Best move")
        bm.grid(row=4, column=0, sticky="ew", pady=6)
        self.best_var = tk.StringVar(value="—")
        self.best_sub = tk.StringVar(value="Move a piece on the board, or start tracking.")
        ttk.Label(bm, textvariable=self.best_var, font=("Helvetica", 30, "bold"),
                  foreground=ARROW).pack(anchor="w", padx=8)
        ttk.Label(bm, textvariable=self.best_sub, wraplength=360, justify="left").pack(
            anchor="w", padx=8, pady=(0, 6))

        # Moves
        mf = ttk.LabelFrame(right, text="Game so far")
        mf.grid(row=5, column=0, sticky="ew")
        self.moves_txt = tk.Text(mf, width=46, height=4, wrap="word", state="disabled",
                                 relief="flat", background=UI_COLORS["field"],
                                 foreground=UI_COLORS["text"], insertbackground=UI_COLORS["text"])
        self.moves_txt.pack(padx=6, pady=4)

        # Log
        lf = ttk.LabelFrame(right, text="Log")
        lf.grid(row=6, column=0, sticky="ew", pady=6)
        self.log_txt = tk.Text(lf, width=46, height=6, wrap="word", state="disabled",
                               relief="flat", background=UI_COLORS["field"],
                               foreground=UI_COLORS["text"], insertbackground=UI_COLORS["text"])
        self.log_txt.pack(padx=6, pady=4)

        # Status bar
        self.status_var = tk.StringVar(value="Ready.")
        ttk.Label(outer, textvariable=self.status_var, style="Muted.TLabel").grid(
            row=1, column=0, columnspan=2, sticky="w", pady=(8, 0))

        self.refresh_palette()

    # ---- drawing ---------------------------------------------------------------------
    def flipped(self):
        return self.color_var.get() == "black"

    def sq_centre(self, sq):
        cell = self.board_px / 8
        f, rk = chess.square_file(sq), chess.square_rank(sq)
        col = 7 - f if self.flipped() else f
        row = rk if self.flipped() else 7 - rk
        return col * cell + cell / 2, row * cell + cell / 2

    def square_at(self, x, y):
        cell = self.board_px / 8
        col, row = int(x // cell), int(y // cell)
        if 0 <= col < 8 and 0 <= row < 8 and x >= 0 and y >= 0:
            return to_square(row, col, self.flipped())
        return None

    def draw_piece(self, cx, cy, piece, tag=None):
        c = self.canvas
        glyph = chess.Piece(piece.piece_type, chess.BLACK).unicode_symbol()
        fill, halo = (("#ffffff", "#000000") if piece.color == chess.WHITE
                      else ("#111111", "#ffffff"))
        tags = (tag,) if tag else ()
        for dx, dy in self.HALO:
            c.create_text(cx + dx, cy + dy, text=glyph, font=self.glyph_font, fill=halo, tags=tags)
        c.create_text(cx, cy, text=glyph, font=self.glyph_font, fill=fill, tags=tags)

    def draw_board(self):
        c = self.canvas
        c.delete("all")
        cell = self.board_px / 8
        flip = self.flipped()
        th = self.theme()

        for r in range(8):
            for col in range(8):
                sq = to_square(r, col, flip)
                light = (r + col) % 2 == 0
                color = th["light"] if light else th["dark"]
                marked = (self.last_move and sq in (self.last_move.from_square,
                                                    self.last_move.to_square))
                if marked or sq == self.selected:
                    color = th["hl_light"] if light else th["hl_dark"]
                if self.best_move and self.hl_best_var.get():
                    if sq == self.best_move.from_square:
                        color = th["best_from"][0] if light else th["best_from"][1]
                    elif sq == self.best_move.to_square:
                        color = th["best_to"][0] if light else th["best_to"][1]
                x0, y0 = col * cell, r * cell
                c.create_rectangle(x0, y0, x0 + cell, y0 + cell, fill=color, outline="")

                if col == 0:
                    c.create_text(x0 + 4, y0 + 8, text=str(chess.square_rank(sq) + 1),
                                  anchor="w", font=("Helvetica", max(8, round(8 * self.board_px / self.BOARD_PX)), "bold"),
                                  fill=th["dark"] if light else th["light"])
                if r == 7:
                    c.create_text(x0 + cell - 4, y0 + cell - 8,
                                  text=chess.FILE_NAMES[chess.square_file(sq)],
                                  anchor="e", font=("Helvetica", max(8, round(8 * self.board_px / self.BOARD_PX)), "bold"),
                                  fill=th["dark"] if light else th["light"])

                piece = self.board.piece_at(sq)
                if piece and not (self.dragging and sq == self.drag_from):
                    self.draw_piece(x0 + cell / 2, y0 + cell / 2, piece)

        # Legal destination hints for the selected piece
        if self.selected is not None and not self.free_var.get():
            targets = {}
            for m in self.board.legal_moves:
                if m.from_square == self.selected:
                    targets[m.to_square] = self.board.is_capture(m)
            for sq, capture in targets.items():
                cx, cy = self.sq_centre(sq)
                if capture:
                    rr = cell * 0.44
                    c.create_oval(cx - rr, cy - rr, cx + rr, cy + rr, outline="#444444", width=3)
                else:
                    rr = cell * 0.14
                    c.create_oval(cx - rr, cy - rr, cx + rr, cy + rr, fill="#4d4d4d", outline="")

        if self.best_move and self.arrow_var.get():
            x1, y1 = self.sq_centre(self.best_move.from_square)
            x2, y2 = self.sq_centre(self.best_move.to_square)
            c.create_line(x1, y1, x2, y2, width=cell * 0.14, fill=ARROW,
                          arrow=tk.LAST, arrowshape=(cell * 0.3, cell * 0.34, cell * 0.15),
                          capstyle=tk.ROUND)

        # On platforms without a click-through overlay, retain the board-local effect.
        if self.fx_canvas is None:
            self.fx_create_items()


    # ---- themes, preferences, weather effects --------------------------------------------
    def theme(self):
        return THEMES.get(self.theme_var.get(), THEMES[DEFAULT_THEME])

    def load_prefs(self):
        try:
            with open(PREFS_PATH, "r", encoding="utf-8") as f:
                d = json.load(f)
            if d.get("performance_preset") in ("Potato PC", "Mid PC", "Good PC"):
                self.performance_var.set(d["performance_preset"])
            if d.get("theme") in THEMES:
                self.theme_var.set(d["theme"])
            self.snow_var.set(bool(d.get("snow", False)))
            self.rain_var.set(bool(d.get("rain", False)))
            self.fx_amount_var.set(float(d.get("fx_amount", 60)))
            self.fx_fps_var.set(max(20, min(120, int(d.get("fx_fps", 80)))))
            self.fx_fps_display_var.set(f"{self.fx_fps_var.get()} FPS")
            self.arrow_var.set(bool(d.get("arrow", True)))
            self.hl_best_var.set(bool(d.get("highlight_best", True)))
            if d.get("templates"):
                self.templates = unpack_templates(d["templates"])
        except Exception:
            pass  # first run or unreadable file: keep defaults

    def templates_is_default(self):
        try:
            return pack_templates(self.templates) == pack_templates(default_templates())
        except Exception:
            return True

    def save_prefs(self):
        try:
            with open(PREFS_PATH, "w", encoding="utf-8") as f:
                json.dump({
                    "theme": self.theme_var.get(),
                    "performance_preset": self.performance_var.get(),
                    "snow": self.snow_var.get(),
                    "rain": self.rain_var.get(),
                    "fx_amount": float(self.fx_amount_var.get()),
                    "fx_fps": int(self.fx_fps_var.get()),
                    "arrow": self.arrow_var.get(),
                    "highlight_best": self.hl_best_var.get(),
                    "templates": (None if self.templates_is_default() else pack_templates(self.templates)),
                }, f)
        except Exception:
            pass

    def fx_rebuild(self):
        """Rebuild weather particles in window coordinates."""
        n = max(5, int(float(self.fx_amount_var.get())))
        if self.fx_canvas is not None:
            width = max(1, self.root.winfo_width())
            height = max(1, self.root.winfo_height())
        else:
            width = height = self.board_px
        parts = []
        if self.snow_var.get():
            for _ in range(n):
                parts.append(dict(kind="snow", x=random.uniform(0, width),
                                  y=random.uniform(0, height), vy=random.uniform(24, 62),
                                  vx=random.uniform(-9, 9), r=random.uniform(1.2, 3.0),
                                  phase=random.uniform(0, 6.28), drift=random.uniform(4, 12),
                                  item=None))
        if self.rain_var.get():
            for _ in range(n):
                parts.append(dict(kind="rain", x=random.uniform(0, width + 30),
                                  y=random.uniform(0, height), vy=random.uniform(300, 450),
                                  vx=-random.uniform(55, 90), length=random.uniform(9, 17),
                                  item=None))
        self.fx_particles = parts
        self._fx_last_time = time.perf_counter()
        self.fx_create_items()

    def fx_create_items(self):
        """Create persistent weather shapes on the click-through window overlay."""
        c = self.fx_canvas or self.canvas
        c.delete("fx")
        for p in self.fx_particles:
            if p["kind"] == "snow":
                r = p["r"]
                p["item"] = c.create_oval(p["x"] - r, p["y"] - r, p["x"] + r, p["y"] + r,
                                          fill="#f4f7fb", outline="#cbd7e6", tags="fx")
            else:
                p["item"] = c.create_line(p["x"], p["y"],
                                          p["x"] + p["vx"] * 0.035,
                                          p["y"] + p["length"],
                                          fill="#b9d7f5", width=1.2, tags="fx")

    def fx_tick(self):
        parts = self.fx_particles
        if parts:
            c = self.fx_canvas or self.canvas
            width = max(1, self.root.winfo_width()) if self.fx_canvas else self.board_px
            height = max(1, self.root.winfo_height()) if self.fx_canvas else self.board_px
            now = time.perf_counter()
            dt = min(0.05, max(0.0, now - self._fx_last_time))
            self._fx_last_time = now
            for p in parts:
                if p["kind"] == "snow":
                    p["phase"] += dt * 1.5
                    p["x"] += (p["vx"] + math.sin(p["phase"]) * p["drift"]) * dt
                    p["y"] += p["vy"] * dt
                    if p["y"] > height + 5:
                        p["y"], p["x"] = -5, random.uniform(0, width)
                    p["x"] = (p["x"] + width + 10) % (width + 10) - 5
                    r = p["r"]
                    c.coords(p["item"], p["x"] - r, p["y"] - r, p["x"] + r, p["y"] + r)
                else:
                    p["x"] += p["vx"] * dt
                    p["y"] += p["vy"] * dt
                    if p["y"] > height + p["length"] or p["x"] < -20:
                        p["y"], p["x"] = -p["length"], random.uniform(0, width + 30)
                    c.coords(p["item"], p["x"], p["y"],
                             p["x"] + p["vx"] * 0.035, p["y"] + p["length"])
        fps = max(20, min(120, int(self.fx_fps_var.get())))
        self.root.after(max(1, round(1000 / fps)), self.fx_tick)

    def on_fx_fps_change(self, value):
        fps = max(20, min(120, int(float(value))))
        self.fx_fps_display_var.set(f"{fps} FPS")
        self.save_prefs()

    def apply_settings(self, save=True):
        self.fx_rebuild()
        self.draw_board()
        self.draw_theme_preview()
        if save:
            self.save_prefs()

    def on_color_change(self):
        self.draw_board()
        # The eval is always shown from the selected colour's point of view
        if not self.running and self.service is not None:
            self.request_manual_analysis()

    def draw_theme_preview(self):
        pv = self.theme_preview
        if pv is None or not pv.winfo_exists():
            return
        pv.delete("all")
        th = self.theme()
        row1 = [th["light"], th["dark"]] * 4
        row2 = [th["hl_light"], th["hl_dark"], th["best_from"][0], th["best_from"][1],
                th["best_to"][0], th["best_to"][1], th["light"], th["dark"]]
        for i in range(8):
            pv.create_rectangle(i * 30, 0, i * 30 + 30, 30, fill=row1[i], outline="")
            pv.create_rectangle(i * 30, 30, i * 30 + 30, 60, fill=row2[i], outline="")

    def on_performance_preset(self, _event=None, save=True):
        """Apply the selected refresh and engine think-time preset."""
        presets = {
            "Potato PC": ("10", "3"),
            "Mid PC": ("5", "1.5"),
            "Good PC": ("1", "0.5"),
        }
        values = presets.get(self.performance_var.get())
        if not values:
            return
        interval, think = values
        self.interval_var.set(interval)
        self.think_var.set(think)
        if self.worker and self.worker.is_alive():
            self.worker.cfg["interval"] = float(interval)
            self.worker.cfg["think"] = float(think)
        if save:
            self.save_prefs()

    def open_settings(self):
        if self.settings_win is not None and self.settings_win.winfo_exists():
            self.settings_win.lift()
            return
        win = tk.Toplevel(self.root)
        self.settings_win = win
        win.title("Settings")
        win.resizable(False, False)
        win.transient(self.root)
        win.configure(background=UI_COLORS["window"])
        if sys.platform.startswith("win"):
            win.after_idle(lambda: self.apply_rounded_corners(win))
            win.after_idle(lambda: self.apply_dark_titlebar(win))

        def close():
            self.save_prefs()
            win.destroy()

        win.protocol("WM_DELETE_WINDOW", close)
        pad = {"padx": 8, "pady": 4}
        f = ttk.Frame(win, padding=12)
        f.pack()

        ttk.Label(f, text="Board theme", font=("Helvetica", 10, "bold")).grid(
            row=0, column=0, sticky="w", **pad)
        cb = ttk.Combobox(f, textvariable=self.theme_var, values=list(THEMES),
                          state="readonly", width=18)
        cb.grid(row=0, column=1, sticky="w", **pad)
        cb.bind("<<ComboboxSelected>>", lambda e: self.apply_settings())

        self.theme_preview = tk.Canvas(f, width=240, height=60, highlightthickness=1,
                                       highlightbackground=UI_COLORS["border"],
                                       background=UI_COLORS["panel"])
        self.theme_preview.grid(row=1, column=0, columnspan=2, **pad)
        self.draw_theme_preview()

        ttk.Label(f, text="Best move", font=("Helvetica", 10, "bold")).grid(
            row=2, column=0, sticky="w", **pad)
        ttk.Checkbutton(f, text="Highlight piece and target square", variable=self.hl_best_var,
                        command=self.apply_settings).grid(row=3, column=0, columnspan=2,
                                                          sticky="w", **pad)
        ttk.Checkbutton(f, text="Show arrow", variable=self.arrow_var,
                        command=self.apply_settings).grid(row=4, column=0, columnspan=2,
                                                          sticky="w", **pad)

        ttk.Label(f, text="Weather effects", font=("Helvetica", 10, "bold")).grid(
            row=5, column=0, sticky="w", **pad)
        ttk.Checkbutton(f, text="Falling snow", variable=self.snow_var,
                        command=self.apply_settings).grid(row=6, column=0, columnspan=2,
                                                          sticky="w", **pad)
        ttk.Checkbutton(f, text="Rain", variable=self.rain_var,
                        command=self.apply_settings).grid(row=7, column=0, columnspan=2,
                                                          sticky="w", **pad)
        ttk.Label(f, text="Amount").grid(row=8, column=0, sticky="w", **pad)
        ttk.Scale(f, from_=10, to=200, variable=self.fx_amount_var, length=140,
                  command=lambda v: self.apply_settings(save=False)).grid(
            row=8, column=1, sticky="w", **pad)

        ttk.Label(f, text="Animation FPS").grid(row=9, column=0, sticky="w", **pad)
        ttk.Scale(f, from_=20, to=120, variable=self.fx_fps_var, length=140,
                  command=self.on_fx_fps_change).grid(row=9, column=1, sticky="w", **pad)
        ttk.Label(f, textvariable=self.fx_fps_display_var, width=7).grid(
            row=9, column=2, sticky="w", **pad)

        ttk.Button(f, text="Close", command=close).grid(row=10, column=0,
                                                          columnspan=3, pady=(10, 0))

    # ---- small helpers -------------------------------------------------------------------
    def log(self, text):
        self.log_txt.configure(state="normal")
        self.log_txt.insert("end", f"{time.strftime('%H:%M:%S')}  {text}\n")
        self.log_txt.see("end")
        self.log_txt.configure(state="disabled")

    def set_moves(self, text):
        self.moves_txt.configure(state="normal")
        self.moves_txt.delete("1.0", "end")
        self.moves_txt.insert("end", text or "(no moves yet)")
        self.moves_txt.configure(state="disabled")

    def refresh_manual_moves(self):
        if self.hist_moves:
            try:
                self.set_moves(chess.Board(self.hist_start).variation_san(self.hist_moves))
                return
            except ValueError:
                pass
        self.set_moves("")

    def browse_engine(self):
        path = filedialog.askopenfilename(title="Select the Stockfish executable")
        if path:
            self.engine_var.set(path)

    def set_running(self, running):
        self.running = running
        if running:
            self.auto_play_var.set(False)
        for w in self.config_widgets + [self.auto_btn, self.select_btn, self.test_btn,
                                        self.learn_btn, self.start_btn]:
            w.configure(state="disabled" if running else "normal")
        self.stop_btn.configure(state="normal" if running else "disabled")
        self.canvas.configure(cursor="arrow" if running else "hand2")

    def on_auto_play_toggle(self):
        """Enable Stockfish auto-play on the app's internal board only."""
        if self.running:
            self.auto_play_var.set(False)
            return
        if self.auto_play_var.get():
            self.status_var.set("Auto-play is enabled on the in-app study board only.")
            self.request_manual_analysis()
        else:
            self.status_var.set("Auto-play is off.")

    def auto_play_stockfish_move(self, req_id, move):
        """Apply a current engine move to the editable analysis board, never the screen."""
        player_color = chess.BLACK if self.color_var.get() == "black" else chess.WHITE
        if (not self.auto_play_var.get() or self.running or req_id != self.req_id
                or self.board.turn != player_color or move not in self.board.legal_moves):
            return
        san = self.board.san(move)
        self.snapshot()
        self.board.push(move)
        self.hist_moves.append(move)
        self.last_move = move
        self.log(f"Auto-played Stockfish move on the in-app board: {san}")
        self.after_manual_change()

    # ---- manual board: state helpers ----------------------------------------------------------
    def snapshot(self):
        self.undo_stack.append((self.board.fen(), self.hist_start, list(self.hist_moves)))
        if len(self.undo_stack) > 300:
            self.undo_stack.pop(0)

    def after_manual_change(self, free_edit=False):
        if free_edit:
            self.board.ep_square = None
            self.board.castling_rights = self.board.clean_castling_rights()
            self.hist_start, self.hist_moves, self.last_move = self.board.fen(), [], None
        self.selected = None
        self.refresh_manual_moves()
        self.best_move = None
        self.draw_board()
        self.request_manual_analysis()

    def request_manual_analysis(self):
        """Ask Stockfish about the position currently on the board."""
        self.best_move = None
        if not self.board.is_valid():
            self.best_var.set("—")
            self.best_sub.set("This position isn't valid (each side needs exactly one king, "
                              "no pawns on the back rank, the side not to move can't be in check…).")
            return
        if self.board.is_game_over():
            self.best_var.set("—")
            outcome = self.board.outcome()
            self.best_sub.set(f"Game over: {outcome.result()}"
                              + (f" ({outcome.termination.name.lower()})" if outcome else ""))
            return
        path = self.engine_var.get().strip()
        if not path or not os.path.isfile(path):
            self.best_var.set("—")
            self.best_sub.set("Set the Stockfish path (Settings) to get analysis.")
            return
        try:
            think = max(0.1, float(self.think_var.get()))
        except ValueError:
            think = 1.5

        if self.service and (self.service.path != path or not self.service.is_alive()):
            self.service.shutdown()
            self.service = None
        if not self.service:
            self.service = AnalysisService(path, self.q)
            self.service.start()

        self.req_id += 1
        self.best_var.set("…")
        self.best_sub.set("Analysing…")
        us = chess.BLACK if self.color_var.get() == "black" else chess.WHITE
        self.service.request(self.req_id, self.board.fen(), think, us)

    def can_edit(self):
        if self.running:
            self.status_var.set("Stop tracking to edit the board by hand.")
            return False
        return True

    # ---- manual board: mouse handling ------------------------------------------------------------
    def on_press(self, e):
        if not self.can_edit():
            return
        sq = self.square_at(e.x, e.y)
        if sq is None:
            return
        free = self.free_var.get()

        # Free edit + palette brush: place a piece
        if free and self.brush:
            if self.brush.piece_type == chess.PAWN and chess.square_rank(sq) in (0, 7):
                self.status_var.set("Pawns can't go on the first or last rank.")
                return
            self.snapshot()
            self.board.set_piece_at(sq, self.brush)
            self.after_manual_change(free_edit=True)
            return

        # Click-to-move: a piece is already selected and this is its destination
        if self.selected is not None and self.selected != sq:
            sel_piece, target = self.board.piece_at(self.selected), self.board.piece_at(sq)
            same_side = free and sel_piece and target and sel_piece.color == target.color
            if not same_side and self.try_move(self.selected, sq):
                return

        piece = self.board.piece_at(sq)
        if piece and (free or piece.color == self.board.turn):
            self.selected = sq
            self.drag_from = sq
            self.drag_start = (e.x, e.y)
            self.dragging = False
        else:
            self.selected = None
            self.drag_from = None
        self.draw_board()

    def on_motion(self, e):
        if self.drag_from is None or self.running:
            return
        if not self.dragging:
            if abs(e.x - self.drag_start[0]) + abs(e.y - self.drag_start[1]) < 6:
                return
            self.dragging = True
            self.draw_board()  # redraw without the piece on its origin square
            piece = self.board.piece_at(self.drag_from)
            if piece:
                self.draw_piece(e.x, e.y, piece, tag="drag")
            self.drag_last = (e.x, e.y)
            return
        self.canvas.move("drag", e.x - self.drag_last[0], e.y - self.drag_last[1])
        self.drag_last = (e.x, e.y)

    def on_release(self, e):
        if self.drag_from is None:
            return
        src, was_dragging = self.drag_from, self.dragging
        self.drag_from, self.dragging = None, False
        self.canvas.delete("drag")
        if was_dragging:
            dst = self.square_at(e.x, e.y)
            if dst is not None and dst != src and self.try_move(src, dst):
                return
            self.selected = None  # illegal drop: piece snaps back
        self.draw_board()

    def on_right_click(self, e):
        if self.running or not self.free_var.get():
            return
        sq = self.square_at(e.x, e.y)
        if sq is not None and self.board.piece_at(sq):
            self.snapshot()
            self.board.remove_piece_at(sq)
            self.after_manual_change(free_edit=True)

    def try_move(self, src, dst):
        """Attempt to move src -> dst on the board. Returns True if the board changed."""
        piece = self.board.piece_at(src)
        if piece is None:
            return False
        promotes = piece.piece_type == chess.PAWN and chess.square_rank(dst) in (0, 7)

        if self.free_var.get():
            self.snapshot()
            moved = chess.Piece(chess.QUEEN, piece.color) if promotes else piece
            self.board.remove_piece_at(src)
            self.board.set_piece_at(dst, moved)
            self.board.turn = not piece.color
            self.after_manual_change(free_edit=True)
            return True

        move = chess.Move(src, dst, promotion=chess.QUEEN if promotes else None)
        if move not in self.board.legal_moves:
            return False
        self.snapshot()
        self.board.push(move)
        self.hist_moves.append(move)
        self.last_move = move
        self.after_manual_change()
        return True

    # ---- manual board: toolbar actions ----------------------------------------------------------------
    def on_free_toggle(self):
        if not self.free_var.get():
            self.brush = None
            self.refresh_palette()
        self.selected = None
        self.draw_board()

    def pick_brush(self, piece):
        if self.running:
            return
        if not self.free_var.get():
            self.status_var.set("Tick 'Free edit' to place pieces.")
            return
        self.brush = None if self.brush == piece else piece
        self.refresh_palette()

    def refresh_palette(self):
        for piece, lbl in self.palette.items():
            on = piece == self.brush
            lbl.configure(relief="sunken" if on else "raised",
                          bg="#e0c36a" if on else UI_COLORS["surface"])

    def on_reset(self):
        if not self.can_edit():
            return
        self.snapshot()
        self.board = chess.Board()
        self.hist_start, self.hist_moves, self.last_move = chess.STARTING_FEN, [], None
        self.after_manual_change()

    def on_undo(self):
        if not self.can_edit() or not self.undo_stack:
            return
        fen, start, moves = self.undo_stack.pop()
        self.board = chess.Board(fen)
        self.hist_start, self.hist_moves = start, moves
        self.last_move = moves[-1] if moves else None
        self.after_manual_change()

    def on_switch_turn(self):
        if not self.can_edit():
            return
        self.snapshot()
        self.board.turn = not self.board.turn
        self.after_manual_change(free_edit=True)

    def on_load_fen(self):
        if not self.can_edit():
            return
        fen = self.fen_var.get().strip()
        try:
            new_board = chess.Board(fen)
        except ValueError:
            messagebox.showerror("Bad FEN", "That FEN isn't valid.")
            return
        self.snapshot()
        self.board = new_board
        self.after_manual_change(free_edit=True)

    def on_board_to_fen(self):
        self.fen_var.set(self.board.fen())
        self.status_var.set("Copied the board position into 'Start FEN'.")

    # ---- screen tracker actions ------------------------------------------------------------------------
    def read_screen(self):
        """Capture the selected region once -> CellInfo (colours/pieces in screen coordinates)."""
        try:
            thr = float(self.occ_var.get())
        except ValueError:
            thr = FG_THRESHOLD
        with mss.mss() as sct:
            img = grab(sct, self.region)
        return CellInfo(img, thr)

    def make_read(self, info):
        return BoardRead(info, self.flipped(), self.templates)

    def show_region(self, region, how):
        self.region = region
        self.region_var.set(
            f"Board: {region['width']}×{region['height']} px at "
            f"({region['left']}, {region['top']}) — {how}. Tracking works while this app is "
            "unfocused; keep the board visible and unobstructed on the desktop.")
        with mss.mss() as sct:
            img = grab(sct, region)[:, :, ::-1]  # BGR -> RGB
        thumb = Image.fromarray(np.ascontiguousarray(img)).resize((140, 140))
        self.preview_photo = ImageTk.PhotoImage(thumb)
        self.preview.configure(image=self.preview_photo)

    def hide_while(self, fn):
        """Run fn() with this window out of the way so it isn't captured."""
        self.root.withdraw()
        self.root.update()
        time.sleep(0.5)  # give the OS time to hide our window
        try:
            return fn()
        finally:
            self.root.deiconify()

    def on_auto_find(self):
        self.status_var.set("Looking for a chessboard on your screen…")

        def search():
            found = []
            with mss.mss() as sct:
                for mon in sct.monitors[1:]:
                    for x, y, side in find_boards(grab(sct, mon)):
                        found.append((side, mon["left"] + x, mon["top"] + y))
            return found

        found = self.hide_while(search)
        if not found:
            self.status_var.set("No chessboard found.")
            messagebox.showinfo(
                "No board found",
                "I couldn't find a chessboard. Make sure the whole board is visible (not covered "
                "or minimised) and uses a flat colour theme, then try again.\n\n"
                "You can also use 'Select manually'.")
            return
        side, left, top = max(found)
        self.show_region({"left": left, "top": top, "width": side, "height": side}, "found automatically")
        info = self.read_screen()
        white_bottom = info.white_is_at_bottom()
        if white_bottom is not None:
            self.color_var.set("white" if white_bottom else "black")
            self.on_color_change()
            self.log(f"Board found. The pieces show {'White' if white_bottom else 'Black'} "
                     "at the bottom, so 'I'm playing' was set to match.")
        else:
            self.log("Board found.")
        self.report_read(info)
        self.status_var.set("Board found. Click 'Test read' to see what was read, then '2. Start'.")

    def report_read(self, info):
        read = self.make_read(info)
        self.log(f"Read {len(read.pieces)} pieces (32 at game start); "
                 f"unreadable squares: {square_names(read.unknown)}; "
                 f"highlighted squares: {square_names(read.highlighted)}")
        return read

    def on_select(self):
        def pick():
            return select_region(self.root)

        region = self.hide_while(pick)
        if not region or region["width"] < 80:
            self.status_var.set("Board selection cancelled.")
            return
        how = "as selected"
        try:
            with mss.mss() as sct:
                snapped = snap_region(sct, region)
            if snapped is not region:
                how = "snapped to the board edges"
            region = snapped
        except Exception:
            pass
        self.show_region(region, how)
        self.status_var.set("Board selected. Click 'Test read' to check detection.")

    def on_test(self):
        """Read the screen once and show what was read on the app board."""
        if not self.region:
            messagebox.showinfo("Find the board", "Click '1. Find board' first.")
            return
        info = self.read_screen()
        white_bottom = info.white_is_at_bottom()
        if white_bottom is not None and white_bottom == self.flipped():
            self.log(f"Warning: the pieces suggest {'White' if white_bottom else 'Black'} is at "
                     "the bottom, but 'I'm playing' says the opposite. Switch it if the board "
                     "below looks mirrored.")
        read = self.report_read(info)
        board = guess_board(read, chess.BLACK if self.flipped() else chess.WHITE)
        if board is None:
            self.status_var.set("Test read: couldn't build a legal position (see the Log).")
            return
        self.snapshot()
        self.board = board
        self.after_manual_change(free_edit=True)
        self.status_var.set("Test read: the board on the left now shows what was read from "
                            "the screen. Compare it with chess.com.")

    def on_learn(self):
        """Learn the piece shapes from a starting position shown on screen."""
        if not self.region:
            messagebox.showinfo("Find the board", "Click '1. Find board' first.")
            return
        info = self.read_screen()
        error = None
        for flip in (self.flipped(), not self.flipped()):
            try:
                templates = learn_templates(info, flip)
                break
            except ValueError as e:
                error = error or e
        else:
            messagebox.showerror("Learn pieces", str(error))
            return
        if flip != self.flipped():
            self.color_var.set("black" if flip else "white")
            self.on_color_change()
        self.templates = templates
        self.save_prefs()
        self.log("Learned the piece shapes from the starting position on screen.")
        self.status_var.set("Piece shapes learned and saved.")

    def on_start(self):
        engine = self.engine_var.get().strip()
        if not engine or not os.path.isfile(engine):
            messagebox.showerror("Stockfish not found", "Please browse to your Stockfish executable.")
            return
        if not self.region:
            messagebox.showinfo("Find the board", "Click '1. Find board' first.")
            return
        fen = self.fen_var.get().strip() or None
        if fen:
            try:
                chess.Board(fen)
            except ValueError:
                messagebox.showerror("Bad FEN", "That FEN isn't valid.")
                return
        try:
            info = self.read_screen()
            white_bottom = info.white_is_at_bottom()
            if white_bottom is not None and white_bottom == self.flipped():
                shown = "White" if white_bottom else "Black"
                if messagebox.askyesno(
                        "Board orientation",
                        f"The pieces on screen suggest {shown} is at the bottom, but "
                        f"'I'm playing' is set to {'Black' if white_bottom else 'White'}.\n\n"
                        f"Switch it to {shown}?"):
                    self.color_var.set("white" if white_bottom else "black")
                    self.on_color_change()
        except Exception:
            pass  # a failed pre-check must not block starting
        try:
            cfg = {
                "engine": engine,
                "region": self.region,
                "black_bottom": self.color_var.get() == "black",
                "us": chess.BLACK if self.color_var.get() == "black" else chess.WHITE,
                "interval": max(1.0, float(self.interval_var.get())),
                "think": max(0.1, float(self.think_var.get())),
                "both": self.both_var.get(),
                "fen": fen,
                "fg_threshold": float(self.occ_var.get()),
                "tolerance": int(float(self.tol_var.get())),
                "max_plies": 3,
                "templates": self.templates,
            }
        except ValueError:
            messagebox.showerror("Bad setting", "One of the numeric settings isn't a number.")
            return

        self.req_id += 1  # discard any in-flight manual analysis
        self.undo_stack.clear()
        self.selected = self.drag_from = None
        self.dragging = False
        self.board = chess.Board(fen) if fen else chess.Board()
        self.last_move = self.best_move = None
        self.best_var.set("—")
        self.best_sub.set("Starting…")
        self.set_moves("")
        self.draw_board()

        self.stop_evt = threading.Event()
        self.worker = Worker(cfg, self.q, self.stop_evt)
        self.worker.start()
        self.set_running(True)
        self.log("Started.")

    def on_stop(self):
        if self.stop_evt:
            self.stop_evt.set()
        self.status_var.set("Stopping…")

    def on_close(self):
        self.save_prefs()
        if self.stop_evt:
            self.stop_evt.set()
        if self.service:
            self.service.shutdown()
        for t in (self.worker, self.service):
            if t and t.is_alive():
                t.join(timeout=3)
        self.root.destroy()

    # ---- messages from background threads ------------------------------------------------------------------
    def pump(self):
        try:
            while True:
                kind, *data = self.q.get_nowait()
                if kind == "status":
                    self.status_var.set(data[0])
                elif kind == "log":
                    self.log(data[0])
                elif kind == "error":
                    self.log("ERROR: " + data[0])
                    messagebox.showerror("Error", data[0])
                elif kind == "position":
                    fen, last_uci, moves = data
                    self.board = chess.Board(fen)
                    self.last_move = chess.Move.from_uci(last_uci) if last_uci else None
                    self.best_move = None
                    self.set_moves(moves)
                    self.draw_board()
                elif kind == "best":
                    payload, note = data
                    if payload:
                        uci, san, ev = payload
                        self.best_var.set(san)
                        self.best_sub.set(f"{uci}   ·   eval {ev}")
                        self.best_move = chess.Move.from_uci(uci)
                    else:
                        self.best_var.set("—")
                        self.best_sub.set(note)
                        self.best_move = None
                    self.draw_board()
                elif kind == "manual_best":
                    req_id, payload, note = data
                    if req_id != self.req_id or self.running:
                        continue  # stale result
                    if payload:
                        uci, san, ev, mine = payload
                        self.best_var.set(san)
                        who = "Your" if mine else "Opponent's"
                        self.best_sub.set(f"{who} best move ({uci})   ·   eval {ev}")
                        self.best_move = chess.Move.from_uci(uci)
                    else:
                        self.best_var.set("—")
                        self.best_sub.set(note)
                        self.best_move = None
                    self.draw_board()
                    if payload and payload[3]:
                        self.auto_play_stockfish_move(req_id, chess.Move.from_uci(payload[0]))
                elif kind == "manual_error":
                    self.best_var.set("—")
                    self.best_sub.set(data[0])
                    self.log("ERROR: " + data[0])
                elif kind == "stopped":
                    self.set_running(False)
                    reason = data[0] if data else None
                    if reason:
                        self.status_var.set(reason)
                        self.log(reason)
                    elif self.stop_evt and self.stop_evt.is_set():
                        self.status_var.set("Tracking stopped by user. You can keep going manually.")
                        self.log("Tracking stopped by user.")
                    else:
                        self.status_var.set("Tracking ended unexpectedly; check the Log for details.")
                        self.log("Tracking ended unexpectedly.")
                    # Carry on manually from wherever the tracker ended
                    self.hist_start, self.hist_moves = self.board.fen(), []
                    self.undo_stack.clear()
                    self.request_manual_analysis()
        except queue.Empty:
            pass
        self.root.after(100, self.pump)


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
