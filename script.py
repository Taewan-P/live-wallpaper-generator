#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import shutil
import struct
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


CONTAINER_TYPES = {
    "moov",
    "trak",
    "mdia",
    "minf",
    "stbl",
    "dinf",
    "edts",
    "udta",
    "meta",
    "ilst",
    "sinf",
    "schi",
    "mvex",
    "moof",
    "traf",
    "skip",
    "wide",
    "tapt",
}

STBL_ORDER = [
    "stsd",
    "sgpd",
    "csgm",
    "stts",
    "ctts",
    "cslg",
    "stss",
    "sdtp",
    "stsc",
    "stsz",
    "stco",
    "co64",
]

X265_PARAMS = "keyint=60:min-keyint=60:scenecut=0:bframes=4:b-adapt=2:b-pyramid=1:temporal-layers=3"


class ScriptError(Exception):
    pass


@dataclass
class Atom:
    type_code: str
    offset: int
    size: int
    header_size: int
    raw_body: bytes = b""
    children: list["Atom"] = field(default_factory=list)

    @property
    def is_container(self) -> bool:
        return bool(self.children)

    def clone_opaque(self) -> "Atom":
        return Atom(
            type_code=self.type_code,
            offset=self.offset,
            size=self.size,
            header_size=self.header_size,
            raw_body=self.raw_body,
            children=[child.clone_opaque() for child in self.children],
        )

    def encoded(self) -> bytes:
        body = self.raw_body if not self.children else b"".join(child.encoded() for child in self.children)
        size = self.header_size + len(body)
        if self.header_size == 16 or size > 0xFFFFFFFF:
            header = struct.pack(">I4sQ", 1, self.type_code.encode("latin1"), size)
        else:
            header = struct.pack(">I4s", size, self.type_code.encode("latin1"))
        return header + body


def parse_atoms(blob: bytes, start: int = 0, end: int | None = None) -> list[Atom]:
    end = len(blob) if end is None else end
    pos = start
    atoms: list[Atom] = []

    while pos + 8 <= end:
        atom_start = pos
        size = struct.unpack_from(">I", blob, pos)[0]
        atom_type = blob[pos + 4 : pos + 8].decode("latin1", "replace")
        header_size = 8
        pos += 8

        if size == 1:
            if pos + 8 > end:
                break
            size = struct.unpack_from(">Q", blob, pos)[0]
            header_size = 16
            pos += 8
        elif size == 0:
            size = end - atom_start

        if size < header_size or atom_start + size > end:
            break

        body_start = atom_start + header_size
        body_end = atom_start + size
        body = blob[body_start:body_end]

        if atom_type in CONTAINER_TYPES:
            children = parse_atoms(blob, body_start, body_end)
            child_bytes = sum(len(child.encoded()) for child in children)
            if children and child_bytes == len(body):
                atoms.append(
                    Atom(
                        type_code=atom_type,
                        offset=atom_start,
                        size=size,
                        header_size=header_size,
                        raw_body=b"",
                        children=children,
                    )
                )
            else:
                atoms.append(
                    Atom(
                        type_code=atom_type,
                        offset=atom_start,
                        size=size,
                        header_size=header_size,
                        raw_body=body,
                    )
                )
        else:
            atoms.append(
                Atom(
                    type_code=atom_type,
                    offset=atom_start,
                    size=size,
                    header_size=header_size,
                    raw_body=body,
                )
            )

        pos = atom_start + size

    return atoms


def find_child(atom: Atom, type_code: str) -> Atom | None:
    for child in atom.children:
        if child.type_code == type_code:
            return child
    return None


def remove_children(atom: Atom, type_codes: set[str]) -> None:
    atom.children = [child for child in atom.children if child.type_code not in type_codes]


def patch_full_atom_flags(body: bytes, flags: int) -> bytes:
    if len(body) < 4:
        raise ScriptError("Atom body too small to patch flags")
    version = body[0:1]
    return version + flags.to_bytes(3, "big") + body[4:]


def patch_vmhd(body: bytes) -> bytes:
    if len(body) < 12:
        raise ScriptError("vmhd atom body too small")
    patched = bytearray(body)
    patched[4:6] = struct.pack(">H", 64)
    patched[6:12] = struct.pack(">HHH", 32768, 32768, 32768)
    return bytes(patched)


def patch_hdlr(body: bytes) -> bytes:
    if len(body) < 12:
        return body
    patched = bytearray(body)
    if patched[8:12] == b"url ":
        patched[8:12] = b"alis"
    return bytes(patched)


def patch_elst(body: bytes) -> bytes:
    if len(body) < 16:
        return body
    patched = bytearray(body)
    version = patched[0]
    entry_count = struct.unpack_from(">I", patched, 4)[0]
    if entry_count < 1:
        return body

    if version == 1:
        if len(patched) >= 24:
            patched[16:24] = struct.pack(">q", 0)
    else:
        if len(patched) >= 16:
            patched[12:16] = struct.pack(">i", 0)
    return bytes(patched)


def read_fixed_16_16(value: bytes) -> float:
    return struct.unpack(">I", value)[0] / 65536.0


def write_fixed_16_16(value: float) -> bytes:
    return struct.pack(">I", int(round(value * 65536.0)))


def parse_stsz(body: bytes) -> list[int]:
    if len(body) < 12:
        raise ScriptError("stsz atom body too small")
    sample_size, sample_count = struct.unpack_from(">II", body, 4)
    if sample_size > 0:
        return [sample_size] * sample_count
    expected = 12 + (sample_count * 4)
    if len(body) < expected:
        raise ScriptError("stsz sample table truncated")
    return list(struct.unpack_from(f">{sample_count}I", body, 12))


def parse_stsc(body: bytes) -> list[dict[str, int]]:
    if len(body) < 8:
        raise ScriptError("stsc atom body too small")
    entry_count = struct.unpack_from(">I", body, 4)[0]
    expected = 8 + (entry_count * 12)
    if len(body) < expected:
        raise ScriptError("stsc table truncated")
    entries = []
    pos = 8
    for _ in range(entry_count):
        first_chunk, samples_per_chunk, sample_description_id = struct.unpack_from(">III", body, pos)
        entries.append(
            {
                "first_chunk": first_chunk,
                "samples_per_chunk": samples_per_chunk,
                "sample_description_id": sample_description_id,
            }
        )
        pos += 12
    return entries


def parse_chunk_offsets(body: bytes, type_code: str) -> list[int]:
    if len(body) < 8:
        raise ScriptError(f"{type_code} atom body too small")
    entry_count = struct.unpack_from(">I", body, 4)[0]
    if type_code == "co64":
        expected = 8 + (entry_count * 8)
        if len(body) < expected:
            raise ScriptError("co64 table truncated")
        return list(struct.unpack_from(f">{entry_count}Q", body, 8))
    expected = 8 + (entry_count * 4)
    if len(body) < expected:
        raise ScriptError("stco table truncated")
    return list(struct.unpack_from(f">{entry_count}I", body, 8))


def patch_chunk_offsets(atom: Atom, delta: int) -> None:
    if atom.type_code == "stco":
        body = bytearray(atom.raw_body)
        entry_count = struct.unpack_from(">I", body, 4)[0]
        pos = 8
        for _ in range(entry_count):
            value = struct.unpack_from(">I", body, pos)[0]
            struct.pack_into(">I", body, pos, value + delta)
            pos += 4
        atom.raw_body = bytes(body)
        return

    if atom.type_code == "co64":
        body = bytearray(atom.raw_body)
        entry_count = struct.unpack_from(">I", body, 4)[0]
        pos = 8
        for _ in range(entry_count):
            value = struct.unpack_from(">Q", body, pos)[0]
            struct.pack_into(">Q", body, pos, value + delta)
            pos += 8
        atom.raw_body = bytes(body)
        return

    for child in atom.children:
        patch_chunk_offsets(child, delta)


def parse_stts_base_duration(body: bytes) -> int:
    if len(body) < 16:
        return 1000
    entry_count = struct.unpack_from(">I", body, 4)[0]
    if entry_count < 1:
        return 1000
    return struct.unpack_from(">I", body, 12)[0]


def parse_ctts_offsets(body: bytes) -> list[int]:
    if len(body) < 8:
        return []
    version = body[0]
    entry_count = struct.unpack_from(">I", body, 4)[0]
    expected = 8 + (entry_count * 8)
    if len(body) < expected:
        return []
    pos = 8
    offsets: list[int] = []
    for _ in range(entry_count):
        sample_count = struct.unpack_from(">I", body, pos)[0]
        if version == 1:
            composition_offset = struct.unpack_from(">i", body, pos + 4)[0]
        else:
            composition_offset = struct.unpack_from(">I", body, pos + 4)[0]
        offsets.extend([composition_offset] * sample_count)
        pos += 8
    return offsets


def expand_chunk_sample_counts(stsc_entries: list[dict[str, int]], total_chunks: int) -> list[int]:
    counts: list[int] = []
    for index, entry in enumerate(stsc_entries):
        start_chunk = entry["first_chunk"]
        next_chunk = stsc_entries[index + 1]["first_chunk"] if index + 1 < len(stsc_entries) else total_chunks + 1
        length = max(0, next_chunk - start_chunk)
        counts.extend([entry["samples_per_chunk"]] * length)
    return counts[:total_chunks]


def extract_temporal_ids(stsz_sizes: list[int], chunk_offsets: list[int], stsc_entries: list[dict[str, int]], blob: bytes) -> list[int]:
    if not stsc_entries or not chunk_offsets or not stsz_sizes:
        return []

    chunk_sample_counts = expand_chunk_sample_counts(stsc_entries, len(chunk_offsets))
    temporal_ids: list[int] = []
    sample_index = 0

    for chunk_index, chunk_offset in enumerate(chunk_offsets):
        if chunk_index >= len(chunk_sample_counts):
            break

        sample_count = chunk_sample_counts[chunk_index]
        current_pos = chunk_offset

        for _ in range(sample_count):
            if sample_index >= len(stsz_sizes):
                break
            sample_size = stsz_sizes[sample_index]
            if current_pos + 6 <= len(blob):
                header = blob[current_pos : current_pos + 6]
                tid_plus_one = header[5] & 0x07
                temporal_ids.append(tid_plus_one - 1 if tid_plus_one > 0 else -1)
            else:
                temporal_ids.append(-1)
            current_pos += sample_size
            sample_index += 1

    return temporal_ids


def generate_csgm_payload(temporal_ids: list[int]) -> bytes:
    if not temporal_ids:
        return b""

    base_indices = [index for index, value in enumerate(temporal_ids) if value == 0]
    pattern = temporal_ids

    if len(base_indices) >= 2:
        interval = base_indices[1] - base_indices[0]
        if interval > 0 and (base_indices[0] + interval) <= len(temporal_ids):
            candidate = temporal_ids[base_indices[0] : base_indices[0] + interval]
            check_limit = min(len(temporal_ids), base_indices[0] + interval * 5)
            consistent = True
            for start in range(base_indices[0], check_limit, interval):
                chunk = temporal_ids[start : min(start + interval, len(temporal_ids))]
                if chunk != candidate[: len(chunk)]:
                    consistent = False
                    break
            if consistent:
                pattern = candidate

    packed = bytearray()
    for index in range(0, len(pattern), 2):
        first = min(pattern[index] + 1, 15) & 0x0F
        second = 0
        if index + 1 < len(pattern):
            second = min(pattern[index + 1] + 1, 15) & 0x0F
        packed.append((first << 4) | second)
    return bytes(packed)


def make_atom(type_code: str, body: bytes) -> bytes:
    return struct.pack(">I4s", len(body) + 8, type_code.encode("latin1")) + body


def create_dim_atom(tag: str, width: float, height: float) -> bytes:
    body = b"\x00\x00\x00\x00" + write_fixed_16_16(width) + write_fixed_16_16(height)
    return make_atom(tag, body)


def create_tapt_atom(width: float, height: float) -> Atom:
    body = create_dim_atom("clef", width, height) + create_dim_atom("prof", width, height) + create_dim_atom("enof", width, height)
    return Atom(type_code="tapt", offset=0, size=0, header_size=8, raw_body=body)


def create_single_sgpd(grouping_type: str, entry_count: int, default_length: int, payload_words: list[int]) -> bytes:
    payload = b"".join(struct.pack(">I", word) for word in payload_words)
    body = bytearray()
    body += struct.pack(">I", 0x01000000)
    body += grouping_type.encode("latin1")
    body += struct.pack(">I", default_length)
    body += struct.pack(">I", entry_count)
    for _ in range(entry_count):
        body += payload
    return bytes(body)


def create_sgpd_atoms(base_duration: int) -> list[Atom]:
    tscl = create_single_sgpd("tscl", 5, 20, [0, base_duration, 1, 0, 128])
    tsas = create_single_sgpd("tsas", 1, 4, [0])
    return [
        Atom(type_code="sgpd", offset=0, size=0, header_size=8, raw_body=tscl),
        Atom(type_code="sgpd", offset=0, size=0, header_size=8, raw_body=tsas),
    ]


def create_csgm_atom(sub_type: str, payload: bytes, sample_count: int) -> Atom:
    body = bytearray()
    body += struct.pack(">I", 0)
    body += sub_type.encode("latin1")
    for value in (0, 4, 2, 1, 1, 16):
        body += struct.pack(">I", value)
    body += struct.pack(">I", max(0, sample_count - 1))
    body += payload
    return Atom(type_code="csgm", offset=0, size=0, header_size=8, raw_body=bytes(body))


def create_csgm_atoms(temporal_ids: list[int]) -> list[Atom]:
    payload = generate_csgm_payload(temporal_ids)
    sample_count = len(temporal_ids)
    return [
        create_csgm_atom("tscl", payload, sample_count),
        create_csgm_atom("tsas", payload, sample_count),
    ]


def create_cslg_atom(ctts_offsets: list[int]) -> Atom | None:
    if not ctts_offsets:
        return None
    max_offset = max(ctts_offsets)
    body = struct.pack(">IIIIII", 0, 0, 0, max_offset, 0, 0)
    return Atom(type_code="cslg", offset=0, size=0, header_size=8, raw_body=body)


def reorder_stbl_children(stbl: Atom) -> None:
    indexed = list(enumerate(stbl.children))
    def order_key(item: tuple[int, Atom]) -> tuple[int, int]:
        index, atom = item
        try:
            return (STBL_ORDER.index(atom.type_code), index)
        except ValueError:
            return (999, index)
    stbl.children = [atom for _, atom in sorted(indexed, key=order_key)]


def patch_wallpaper_atoms(blob: bytes) -> bytes:
    top_level = parse_atoms(blob)
    atom_map = {atom.type_code: atom for atom in top_level}

    ftyp = atom_map.get("ftyp")
    moov = atom_map.get("moov")
    mdat = atom_map.get("mdat")

    if not ftyp or not moov or not mdat:
        raise ScriptError("Expected ftyp, moov, and mdat atoms in transcoded file")

    trak = find_child(moov, "trak")
    if not trak:
        raise ScriptError("trak atom not found")
    mdia = find_child(trak, "mdia")
    if not mdia:
        raise ScriptError("mdia atom not found")
    minf = find_child(mdia, "minf")
    if not minf:
        raise ScriptError("minf atom not found")
    stbl = find_child(minf, "stbl")
    if not stbl:
        raise ScriptError("stbl atom not found")

    stsz = find_child(stbl, "stsz")
    stsc = find_child(stbl, "stsc")
    chunk_offsets_atom = find_child(stbl, "stco") or find_child(stbl, "co64")
    tkhd = find_child(trak, "tkhd")

    if not stsz or not stsc or not chunk_offsets_atom or not tkhd:
        raise ScriptError("Required sample table atoms are missing")

    sample_sizes = parse_stsz(stsz.raw_body)
    stsc_entries = parse_stsc(stsc.raw_body)
    chunk_offsets = parse_chunk_offsets(chunk_offsets_atom.raw_body, chunk_offsets_atom.type_code)
    temporal_ids = extract_temporal_ids(sample_sizes, chunk_offsets, stsc_entries, blob)

    tkhd.raw_body = patch_full_atom_flags(tkhd.raw_body, 15)
    width = read_fixed_16_16(tkhd.raw_body[-8:-4]) if len(tkhd.raw_body) >= 8 else 0.0
    height = read_fixed_16_16(tkhd.raw_body[-4:]) if len(tkhd.raw_body) >= 4 else 0.0

    vmhd = find_child(minf, "vmhd")
    if vmhd:
        vmhd.raw_body = patch_vmhd(vmhd.raw_body)

    hdlr = find_child(mdia, "hdlr")
    if hdlr:
        hdlr.raw_body = patch_hdlr(hdlr.raw_body)

    edts = find_child(trak, "edts")
    if edts:
        elst = find_child(edts, "elst")
        if elst:
            elst.raw_body = patch_elst(elst.raw_body)

    remove_children(trak, {"tapt"})
    remove_children(stbl, {"sgpd", "csgm", "cslg"})

    tapt_atom = create_tapt_atom(width, height)
    tkhd_index = next((index for index, child in enumerate(trak.children) if child.type_code == "tkhd"), None)
    if tkhd_index is None:
        trak.children.append(tapt_atom)
    else:
        trak.children.insert(tkhd_index + 1, tapt_atom)

    stts = find_child(stbl, "stts")
    ctts = find_child(stbl, "ctts")
    base_duration = parse_stts_base_duration(stts.raw_body) if stts else 1000
    ctts_offsets = parse_ctts_offsets(ctts.raw_body) if ctts else []

    stbl.children.extend(create_sgpd_atoms(base_duration))
    stbl.children.extend(create_csgm_atoms(temporal_ids))
    cslg_atom = create_cslg_atom(ctts_offsets)
    if cslg_atom:
        stbl.children.append(cslg_atom)
    reorder_stbl_children(stbl)

    ftyp_bytes = ftyp.encoded()
    mdat_bytes = mdat.encoded()
    original_mdat_content_start = mdat.offset + mdat.header_size
    new_mdat_content_start = len(ftyp_bytes) + 8 + mdat.header_size
    delta = new_mdat_content_start - original_mdat_content_start
    patch_chunk_offsets(moov, delta)

    moov_bytes = moov.encoded()
    wide_bytes = struct.pack(">I4s", 8, b"wide")
    return ftyp_bytes + wide_bytes + mdat_bytes + moov_bytes


def require_tool(name: str) -> None:
    if shutil.which(name):
        return
    raise ScriptError(f"Required executable '{name}' was not found in PATH")


def run_command(command: list[str]) -> None:
    completed = subprocess.run(command, capture_output=True, text=True)
    if completed.returncode == 0:
        return
    message = completed.stderr.strip() or completed.stdout.strip() or "Unknown subprocess failure"
    raise ScriptError(message)


def ffprobe_stream(input_path: Path, ffprobe_bin: str) -> dict[str, Any]:
    command = [
        ffprobe_bin,
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_streams",
        "-show_format",
        "-print_format",
        "json",
        str(input_path),
    ]
    completed = subprocess.run(command, capture_output=True, text=True)
    if completed.returncode != 0:
        message = completed.stderr.strip() or completed.stdout.strip() or "ffprobe failed"
        raise ScriptError(message)
    data = json.loads(completed.stdout)
    streams = data.get("streams") or []
    if not streams:
        raise ScriptError("No video stream found in input")
    return streams[0]


def parse_ratio(value: str | None) -> float:
    if not value or value in {"0/0", "N/A"}:
        return 0.0
    if "/" in value:
        numerator, denominator = value.split("/", 1)
        denominator_value = float(denominator)
        return 0.0 if denominator_value == 0 else float(numerator) / denominator_value
    return float(value)


def needs_fps_normalization(frame_rate: float) -> int | None:
    if frame_rate <= 0:
        return None
    rounded = round(frame_rate)
    if abs(frame_rate - rounded) > 0.001 and abs(frame_rate - rounded) < 0.1:
        return int(rounded)
    return None


def should_tonemap(stream: dict[str, Any]) -> bool:
    color_trc = (stream.get("color_transfer") or "").lower()
    color_primaries = (stream.get("color_primaries") or "").lower()
    color_space = (stream.get("color_space") or "").lower()

    if color_trc in {"smpte2084", "arib-std-b67"}:
        return True
    if "bt2020" in color_primaries or "bt2020" in color_space:
        return True
    if "smpte432" in color_primaries or "p3" in color_primaries:
        return True
    return False


def build_filter_chain(stream: dict[str, Any]) -> str:
    filters: list[str] = []

    frame_rate = parse_ratio(stream.get("avg_frame_rate") or stream.get("r_frame_rate"))
    normalized_fps = needs_fps_normalization(frame_rate)
    if normalized_fps:
        filters.append(f"fps=fps={normalized_fps}")

    if should_tonemap(stream):
        filters.append("zscale=transfer=linear:npl=100")
        filters.append("tonemap=hable")
        filters.append("zscale=transfer=bt709:primaries=bt709:matrix=bt709")
        filters.append("format=yuv420p10le")
    else:
        filters.append("format=yuv420p10le")

    return ",".join(filters)


def transcode_for_live_wallpaper(
    input_path: Path,
    temp_output: Path,
    ffmpeg_bin: str,
    ffprobe_bin: str,
    start_time: float | None,
    end_time: float | None,
) -> None:
    stream = ffprobe_stream(input_path, ffprobe_bin)
    filter_chain = build_filter_chain(stream)

    command = [
        ffmpeg_bin,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
    ]

    if start_time is not None:
        command += ["-ss", str(start_time)]

    command += ["-i", str(input_path)]

    if end_time is not None:
        if start_time is not None and end_time > start_time:
            command += ["-t", str(end_time - start_time)]
        else:
            command += ["-to", str(end_time)]

    command += [
        "-map",
        "0:v:0",
        "-an",
        "-vf",
        filter_chain,
        "-c:v",
        "libx265",
        "-preset",
        "medium",
        "-crf",
        "18",
        "-pix_fmt",
        "yuv420p10le",
        "-tag:v",
        "hvc1",
        "-x265-params",
        X265_PARAMS,
        "-color_range",
        "tv",
        "-color_primaries",
        "bt709",
        "-color_trc",
        "bt709",
        "-colorspace",
        "bt709",
        "-movflags",
        "+write_colr",
        "-video_track_timescale",
        "240000",
        str(temp_output),
    ]
    run_command(command)


def process_video(
    input_path: Path,
    output_path: Path,
    ffmpeg_bin: str,
    ffprobe_bin: str,
    start_time: float | None,
    end_time: float | None,
) -> None:
    input_path = input_path.expanduser().resolve()
    output_path = output_path.expanduser().resolve()

    if not input_path.exists():
        raise ScriptError(f"Input file does not exist: {input_path}")

    output_path.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="live-wallpaper-") as temp_dir:
        temp_output = Path(temp_dir) / "transcoded.mov"
        transcode_for_live_wallpaper(input_path, temp_output, ffmpeg_bin, ffprobe_bin, start_time, end_time)
        patched = patch_wallpaper_atoms(temp_output.read_bytes())
        output_path.write_bytes(patched)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Convert a video into a MOV/MP4 with the same macOS live-wallpaper compatibility patches used in this project."
    )
    parser.add_argument("-i", "--input", required=True, help="Input video path")
    parser.add_argument("-o", "--output", required=True, help="Output video path (.mov recommended)")
    parser.add_argument("--ffmpeg", default="ffmpeg", help="ffmpeg executable to use")
    parser.add_argument("--ffprobe", default="ffprobe", help="ffprobe executable to use")
    parser.add_argument("--start", type=float, default=None, help="Optional trim start time in seconds")
    parser.add_argument("--end", type=float, default=None, help="Optional trim end time in seconds")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    try:
        require_tool(args.ffmpeg)
        require_tool(args.ffprobe)

        if args.start is not None and args.start < 0:
            raise ScriptError("--start must be >= 0")
        if args.end is not None and args.end < 0:
            raise ScriptError("--end must be >= 0")
        if args.start is not None and args.end is not None and args.end <= args.start:
            raise ScriptError("--end must be greater than --start")

        process_video(
            input_path=Path(args.input),
            output_path=Path(args.output),
            ffmpeg_bin=args.ffmpeg,
            ffprobe_bin=args.ffprobe,
            start_time=args.start,
            end_time=args.end,
        )
        print(f"Wrote patched live-wallpaper video to {Path(args.output).expanduser().resolve()}")
        return 0
    except ScriptError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("error: interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
