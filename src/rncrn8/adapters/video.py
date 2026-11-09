"""Video: a timeline built from what a vision pipeline already produced.

This repo does no vision. It expects a folder with any of:
  transcript.srt | transcript.vtt | transcript.json | transcript.txt   (what is said, with times)
  frames.json | frames.csv                                              (what is seen, with times)
  meta.json                                                             (title, duration, summary, ...)
or a single .srt / .vtt file. Everything is merged into windows of N seconds, each a section
with ref t=mm:ss, VISUAL lines then AUDIO lines.
"""

from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass
from pathlib import Path

from ..packet import ReviewPacket, Section
from .base import AdapterError

TRANSCRIPT_NAMES = ("transcript.srt", "transcript.vtt", "transcript.json", "transcript.txt")
FRAME_NAMES = ("frames.json", "frames.csv", "shots.json")

_TS = re.compile(r"(?:(\d+):)?(\d{1,2}):(\d{2})(?:[.,](\d{1,3}))?")
_CUE = re.compile(r"(\d{1,2}:\d{2}(?::\d{2})?(?:[.,]\d{1,3})?)\s*-->\s*(\d{1,2}:\d{2}(?::\d{2})?(?:[.,]\d{1,3})?)")
_LINE_TS = re.compile(r"^\s*\[?((?:\d+:)?\d{1,2}:\d{2}(?:[.,]\d{1,3})?)\]?\s*[-:]?\s*(.*)$")


@dataclass(frozen=True)
class Cue:
    start: float
    text: str
    channel: str  # "audio" | "visual"


def parse_time(value) -> float:
    """Seconds from a number, '12.5', 'mm:ss', 'hh:mm:ss', 'mm:ss,mmm'."""
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip()
    try:
        return float(s)
    except ValueError:
        pass
    m = _TS.fullmatch(s)
    if not m:
        raise AdapterError(f"cannot read time '{value}'")
    h = int(m.group(1)) if m.group(1) else 0
    mi, se = int(m.group(2)), int(m.group(3))
    ms = int(m.group(4).ljust(3, "0")) if m.group(4) else 0
    return h * 3600 + mi * 60 + se + ms / 1000.0


def fmt_time(seconds: float) -> str:
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{sec:02d}" if h else f"{m:02d}:{sec:02d}"


def parse_subtitles(text: str) -> list[Cue]:
    """SRT or WebVTT: cue timing lines followed by text until a blank line."""
    cues: list[Cue] = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        m = _CUE.search(lines[i])
        if not m:
            i += 1
            continue
        start = _safe_time(m.group(1))
        i += 1
        if start is None:
            continue
        buf = []
        while i < len(lines) and lines[i].strip():
            buf.append(re.sub(r"<[^>]+>", "", lines[i]).strip())
            i += 1
        body = " ".join(buf).strip()
        if body:
            cues.append(Cue(start=start, text=body, channel="audio"))
    return cues


def parse_transcript_json(data) -> list[Cue]:
    items = data.get("segments", data) if isinstance(data, dict) else data
    cues = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        t = item.get("start", item.get("t", item.get("time")))
        txt = item.get("text", item.get("caption", ""))
        if t is None or not str(txt).strip():
            continue
        start = _safe_time(t)
        if start is None:
            continue
        cues.append(Cue(start=start, text=str(txt).strip()[:2000], channel="audio"))
    return cues


def parse_transcript_txt(text: str) -> list[Cue]:
    cues = []
    plain = []
    for line in text.splitlines():
        if not line.strip():
            continue
        m = _LINE_TS.match(line)
        if m and m.group(2).strip():
            try:
                cues.append(Cue(start=parse_time(m.group(1)), text=m.group(2).strip(), channel="audio"))
                continue
            except AdapterError:
                pass
        plain.append(line.strip())
    if plain and not cues:
        cues.append(Cue(start=0.0, text=" ".join(plain), channel="audio"))
    return cues


def _safe_time(value) -> float | None:
    """A cue with an unreadable or negative time is dropped or clamped, never fatal for the whole folder."""
    try:
        t = parse_time(value)
    except AdapterError:
        return None
    if t != t or t == float("inf"):
        return None
    return max(0.0, t)


def parse_frames_json(data) -> list[Cue]:
    items = data.get("frames", data.get("shots", data)) if isinstance(data, dict) else data
    cues = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        t = item.get("t", item.get("time", item.get("start", item.get("timestamp"))))
        d = item.get("description", item.get("text", item.get("caption", "")))
        if t is None or not str(d).strip():
            continue
        start = _safe_time(t)
        if start is None:
            continue
        cues.append(Cue(start=start, text=str(d).strip()[:2000], channel="visual"))
    return cues


def parse_frames_csv(text: str) -> list[Cue]:
    cues = []
    reader = csv.DictReader(text.splitlines())
    for row in reader:
        keys = {k.lower().strip(): v for k, v in row.items() if k}
        t = keys.get("t", keys.get("time", keys.get("start")))
        d = keys.get("description", keys.get("text", keys.get("caption")))
        if t is None or not (d or "").strip():
            continue
        start = _safe_time(t)
        if start is None:
            continue
        cues.append(Cue(start=start, text=d.strip()[:2000], channel="visual"))
    return cues


def load_folder(folder: Path) -> tuple[list[Cue], dict]:
    cues: list[Cue] = []
    meta: dict = {}
    for name in TRANSCRIPT_NAMES:
        p = folder / name
        if p.exists():
            text = p.read_text(encoding="utf-8", errors="replace")
            if name.endswith((".srt", ".vtt")):
                cues += parse_subtitles(text)
            elif name.endswith(".json"):
                cues += parse_transcript_json(json.loads(text))
            else:
                cues += parse_transcript_txt(text)
            break
    for name in FRAME_NAMES:
        p = folder / name
        if p.exists():
            text = p.read_text(encoding="utf-8", errors="replace")
            cues += parse_frames_json(json.loads(text)) if name.endswith(".json") else parse_frames_csv(text)
            break
    mp = folder / "meta.json"
    if mp.exists():
        try:
            meta = json.loads(mp.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise AdapterError(f"{mp} is not valid JSON") from exc
    return cues, meta


def timeline_packet(cues: list[Cue], title: str, window: float = 5.0, meta: dict | None = None) -> ReviewPacket:
    if not cues:
        raise AdapterError("no transcript cues or frame descriptions found")
    if window <= 0:
        raise AdapterError("window must be positive seconds")
    bins: dict[int, list[Cue]] = {}
    for c in sorted(cues, key=lambda c: (c.start, 0 if c.channel == "visual" else 1)):
        bins.setdefault(int(c.start // window), []).append(c)
    sections: list[Section] = []
    for b in sorted(bins):
        start = b * window
        visual = [c.text for c in bins[b] if c.channel == "visual"]
        audio = [c.text for c in bins[b] if c.channel == "audio"]
        body = []
        if visual:
            body.append("VISUAL: " + " | ".join(visual))
        if audio:
            body.append("AUDIO: " + " ".join(audio))
        sections.append(Section(ref=f"t={fmt_time(start)}", title=f"{fmt_time(start)}-{fmt_time(start + window)}", text="\n".join(body)))
    meta = dict(meta or {})
    metadata = {str(k): str(v) for k, v in meta.items() if k not in ("title", "summary") and isinstance(v, (str, int, float))}
    metadata.setdefault("window_seconds", str(int(window) if float(window).is_integer() else window))
    metadata.setdefault("end", fmt_time(max(c.start for c in cues)))
    has_visual = any(c.channel == "visual" for c in cues)
    has_audio = any(c.channel == "audio" for c in cues)
    channels = " and ".join(x for x, ok in (("frame descriptions", has_visual), ("transcript", has_audio)) if ok)
    summary = meta.get("summary") or f"Timeline from {channels}, in {metadata['window_seconds']}s windows. Refs are the window start time."
    return ReviewPacket(kind="video", title=str(meta.get("title") or title), summary=summary, sections=sections, metadata=metadata)


class VideoAdapter:
    name = "video"
    kind = "video"

    def matches(self, target: Path) -> bool:
        if target.is_file():
            return target.suffix.lower() in {".srt", ".vtt"}
        if target.is_dir():
            return any((target / n).exists() for n in TRANSCRIPT_NAMES + FRAME_NAMES)
        return False

    def build(self, target: Path, **options) -> ReviewPacket:
        window = float(options.get("window") or 5.0)
        title = options.get("title") or target.stem
        if target.is_file():
            cues = parse_subtitles(target.read_text(encoding="utf-8", errors="replace"))
            packet = timeline_packet(cues, title, window, {})
        else:
            cues, meta = load_folder(target)
            packet = timeline_packet(cues, title, window, meta)
        packet.metadata.setdefault("target", target.name)
        return packet
