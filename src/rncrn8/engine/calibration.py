"""Calibrate a persona's stated confidence against what actually happened.

Isotonic regression (pool adjacent violators) fits a monotone map from stated
confidence to observed hit rate. With few points the fit is jumpy, so each block
is shrunk toward the identity by its own count, n_b / (n_b + prior_strength): a
persona with no record is taken at its word, one with a long record at its record,
and one lucky outcome at 0.9 cannot lift every high confidence it ever states.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Block:
    lo: float
    hi: float
    mean: float
    n: int

    @property
    def centre(self) -> float:
        return (self.lo + self.hi) / 2.0


def pav(points: list[tuple[float, float]]) -> list[Block]:
    """Pool adjacent violators on (x, y) pairs; returns monotone non-decreasing blocks.

    Points with the same x are one block from the start: a persona that always says 0.6 has
    one hit rate at 0.6, not one block per outcome. Without this, ties sorted by y put a miss
    first and the lookup at 0.6 returned that single miss. Adjacent blocks with the same hit
    rate are one block too: the fit is the same either way, and the shrinkage then counts all
    their outcomes together instead of treating three misses in a row as three records of one."""
    if not points:
        return []
    by_x: dict[float, list[float]] = {}
    for x, y in points:
        acc = by_x.setdefault(x, [0.0, 0.0])
        acc[0] += y
        acc[1] += 1.0
    blocks: list[list[float]] = [[x, x, acc[0], acc[1]] for x, acc in sorted(by_x.items())]  # lo, hi, sum, n
    merged: list[list[float]] = []
    for b in blocks:
        merged.append(b)
        while len(merged) >= 2 and merged[-2][2] / merged[-2][3] > merged[-1][2] / merged[-1][3]:
            last = merged.pop()
            prev = merged.pop()
            merged.append([prev[0], last[1], prev[2] + last[2], prev[3] + last[3]])
    pooled: list[list[float]] = []
    for b in merged:
        if pooled and abs(pooled[-1][2] / pooled[-1][3] - b[2] / b[3]) < 1e-12:
            pooled[-1] = [pooled[-1][0], b[1], pooled[-1][2] + b[2], pooled[-1][3] + b[3]]
        else:
            pooled.append(b)
    return [Block(lo=b[0], hi=b[1], mean=b[2] / b[3], n=int(b[3])) for b in pooled]


class Calibrator:
    """Maps stated confidence to calibrated confidence for one persona."""

    def __init__(self, points: list[tuple[float, bool]], prior_strength: float = 5.0, min_points: int = 3):
        import math

        points = [(x, y) for x, y in points if isinstance(x, (int, float)) and math.isfinite(x)]
        self.n = len(points)
        self.prior_strength = prior_strength
        self.blocks: list[Block] = pav([(x, 1.0 if y else 0.0) for x, y in points]) if self.n >= min_points else []
        self.alpha = (self.n / (self.n + prior_strength)) if self.blocks else 0.0  # overall blend, for display
        # Each block is shrunk toward the persona's word by its own count, so a block built on one
        # outcome moves the value far less than one built on ten. The block edge values are then made
        # monotone, since a persona saying 0.6 can never deserve less trust than when it says 0.5.
        self._edges: list[tuple[float, float]] = []
        floor = 0.0
        for b in self.blocks:
            a = self.block_alpha(b)
            lo_v = max(floor, b.lo + a * (b.mean - b.lo))
            hi_v = max(lo_v, b.hi + a * (b.mean - b.hi))
            self._edges.append((lo_v, hi_v))
            floor = hi_v

    def block_alpha(self, block: Block) -> float:
        """How much this block's record counts against the persona's word: n_b / (n_b + prior_strength)."""
        return block.n / (block.n + self.prior_strength)

    @property
    def identity(self) -> bool:
        return not self.blocks

    def isotonic(self, x: float) -> float:
        """The raw fit. Piecewise: block mean inside a block, linear between block centres, flat beyond the ends."""
        b = self.blocks
        if not b:
            return x
        for blk in b:
            if blk.lo <= x <= blk.hi:
                return blk.mean
        if x < b[0].centre:
            return b[0].mean
        if x > b[-1].centre:
            return b[-1].mean
        for left, right in zip(b, b[1:]):
            if left.centre <= x <= right.centre:
                span = right.centre - left.centre
                t = 0.0 if span <= 0 else (x - left.centre) / span
                return left.mean + t * (right.mean - left.mean)
        return x

    def shrunk(self, x: float) -> float:
        """The fit blended with the persona's word, block by block, for x inside the stated range.
        Inside a block: x + a_b * (mean_b - x). Between blocks: a straight line from one block's end
        to the next block's start, so the map is continuous."""
        for blk, (lo_v, hi_v) in zip(self.blocks, self._edges):
            if blk.lo <= x <= blk.hi:
                if blk.hi > blk.lo:
                    return lo_v + (x - blk.lo) / (blk.hi - blk.lo) * (hi_v - lo_v)
                return lo_v
        for i in range(len(self.blocks) - 1):
            left, right = self.blocks[i], self.blocks[i + 1]
            if left.hi < x < right.lo:
                l_hi, r_lo = self._edges[i][1], self._edges[i + 1][0]
                return l_hi + (x - left.hi) / (right.lo - left.hi) * (r_lo - l_hi)
        return x

    def __call__(self, x: float, reach: float = 0.2) -> float:
        """Inside the range of confidences the persona has actually stated, the shrunk fit. Outside
        that range the fit is a guess: its correction fades to nothing over `reach`, so a persona that
        has only ever said 0.6 to 0.75 is taken near its word when it says 0.5."""
        x = min(max(x, 0.0), 1.0)
        if self.identity:
            return x
        lo, hi = self.blocks[0].lo, self.blocks[-1].hi
        if x < lo:
            edge = self._edges[0][0]
            fade = max(0.0, 1.0 - (lo - x) / reach)
            y = min(x + (edge - lo) * fade, edge)  # never above the value at the edge: stays monotone
        elif x > hi:
            edge = self._edges[-1][1]
            fade = max(0.0, 1.0 - (x - hi) / reach)
            y = max(x + (edge - hi) * fade, edge)  # never below the value at the edge
        else:
            y = self.shrunk(x)
        return min(max(y, 0.01), 0.99)

    def curve(self, grid: tuple[float, ...] = (0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9)) -> list[tuple[float, float]]:
        return [(x, self(x)) for x in grid]
