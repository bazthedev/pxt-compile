from __future__ import annotations

import argparse
import json
import lzma
import math
import shutil
import struct
import sys
from pathlib import Path
from typing import Dict, Iterable, List, Sequence, Tuple

from PIL import Image, ImageDraw, ImageFont


UF2_BLOCK_SIZE = 512
UF2_PAYLOAD_SIZE = 256
UF2_MAGIC_START0 = 0x0A324655
UF2_MAGIC_START1 = 0x9E5D5157
UF2_MAGIC_END = 0x0AB16F30
UF2_FLAG_NOFLASH = 0x00000001

SOURCE_MAGIC = b"\x41\x14\x0E\x2F\xB8\x2F\xA2\xBB"
PNG_IMAGE_MAGIC = 0x59347A7D
PNG_IMAGE_HEADER_SIZE = 36

DEFAULT_EDITOR_URL = "https://arcade.makecode.com/"
DEFAULT_EDITOR_VERSION = "0.0.0"
DEFAULT_TARGET = "arcade"
DEFAULT_UF2_BASE_ADDR = 0x10000000

PROJECT_FALLBACK_FILES = [
    "main.ts",
    "main.blocks",
    "README.md",
    "assets.json",
    "images.g.jres",
    "images.g.ts",
    "tilemap.g.jres",
    "tilemap.g.ts",
]


class PackError(Exception):
    pass


def to_u32le(values: Sequence[int]) -> bytes:
    return struct.pack("<" + "I" * len(values), *values)


def lzma_alone_compress(data: bytes) -> bytes:
    return lzma.compress(data, format=lzma.FORMAT_ALONE)


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def load_manifest(project_dir: Path) -> dict:
    manifest_path = project_dir / "pxt.json"
    if not manifest_path.exists():
        raise PackError(f"Missing pxt.json in {project_dir}")
    return json.loads(read_text(manifest_path))


def collect_project_files(project_dir: Path, include_tests: bool) -> Dict[str, str]:
    manifest = load_manifest(project_dir)
    ordered: List[str] = ["pxt.json"]
    seen = {"pxt.json"}

    def add(names: Iterable[str]) -> None:
        for name in names:
            if name not in seen:
                seen.add(name)
                ordered.append(name)

    files = manifest.get("files")
    if isinstance(files, list):
        add(str(name) for name in files)
    else:
        add(name for name in PROJECT_FALLBACK_FILES if (project_dir / name).exists())

    if include_tests:
        test_files = manifest.get("testFiles")
        if isinstance(test_files, list):
            add(str(name) for name in test_files)

    result: Dict[str, str] = {}
    for rel in ordered:
        path = project_dir / rel
        if not path.exists() or not path.is_file():
            continue
        result[rel.replace("\\", "/")] = read_text(path)

    if "pxt.json" not in result:
        raise PackError("Could not include pxt.json in packed project")
    return result


def get_project_name(files: Dict[str, str]) -> str:
    try:
        manifest = json.loads(files["pxt.json"])
    except Exception:
        return "Untitled"
    return str(manifest.get("name") or "Untitled")


def get_target_versions(files: Dict[str, str]) -> dict:
    try:
        manifest = json.loads(files["pxt.json"])
    except Exception:
        return {}
    target_versions = manifest.get("targetVersions")
    return target_versions if isinstance(target_versions, dict) else {}


def build_png_payload(files: Dict[str, str]) -> bytes:
    target_versions = get_target_versions(files)
    payload = {
        "meta": {
            "cloudId": f"pxt/{target_versions.get('targetId', DEFAULT_TARGET)}",
            "targetVersions": target_versions,
            "editor": "tsprj",
            "name": get_project_name(files),
        },
        "source": json.dumps(files, ensure_ascii=False, separators=(",", ":")),
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def build_uf2_source_blob(files: Dict[str, str]) -> bytes:
    target_versions = get_target_versions(files)
    editor_version = str(target_versions.get("target") or DEFAULT_EDITOR_VERSION)

    meta = {
        "name": get_project_name(files),
        "comment": "",
        "status": "unpublished",
        "cloudId": f"pxt/{target_versions.get('targetId', DEFAULT_TARGET)}",
        "editor": "tsprj",
        "targetVersions": target_versions,
    }
    meta_json = json.dumps(meta, ensure_ascii=False, separators=(",", ":"))
    files_json = json.dumps(files, ensure_ascii=False, separators=(",", ":"))
    text = (meta_json + files_json).encode("utf-8")
    compressed = lzma_alone_compress(text)

    header_obj = {
        "compression": "LZMA",
        "headerSize": len(meta_json),
        "textSize": len(text),
        "name": get_project_name(files),
        "eURL": DEFAULT_EDITOR_URL,
        "eVER": editor_version,
        "pxtTarget": target_versions.get("targetId", DEFAULT_TARGET),
    }
    header_json = json.dumps(header_obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

    source_header = bytearray()
    source_header.extend(SOURCE_MAGIC)
    source_header.extend(struct.pack("<H", len(header_json)))
    source_header.extend(struct.pack("<I", len(compressed)))
    source_header.extend(struct.pack("<H", 0))
    source_header.extend(header_json)
    source_header.extend(compressed)

    pad = (-len(source_header)) % 16
    if pad:
        source_header.extend(b"\x00" * pad)
    return bytes(source_header)


def draw_default_canvas(project_name: str, width: int = 320, height: int = 240) -> Image.Image:
    image = Image.new("RGBA", (width, height), (245, 246, 248, 255))
    draw = ImageDraw.Draw(image)

    draw.rectangle((0, 0, width, 28), fill=(235, 235, 235, 255))
    draw.rectangle((16, 44, width - 16, height - 40), fill=(255, 255, 255, 255), outline=(190, 190, 190, 255), width=2)
    draw.text((12, 8), "MakeCode Arcade", fill=(40, 40, 40, 255), font=ImageFont.load_default())
    draw.text((24, height - 28), project_name, fill=(40, 40, 40, 255), font=ImageFont.load_default())
    return image


def encode_blob_into_png(canvas: Image.Image, blob: bytes) -> Image.Image:
    original_width = canvas.width
    original_height = canvas.height
    needed_bytes = PNG_IMAGE_HEADER_SIZE + len(blob)
    usable_bytes = (canvas.width * canvas.height - 1) * 3
    bpp = 1
    while bpp < 4:
        if usable_bytes * bpp >= needed_bytes * 8:
            break
        bpp += 1

    img_capacity = (usable_bytes * bpp) >> 3
    missing = needed_bytes - img_capacity
    added_lines = 0

    if missing > 0:
        bytes_per_line = original_width * 3
        added_lines = math.ceil(missing / bytes_per_line)
        expanded = Image.new("RGBA", (original_width, original_height + added_lines), (255, 255, 255, 255))
        expanded.alpha_composite(canvas, (0, 0))
        canvas = expanded

    image = canvas.convert("RGBA")
    data = bytearray(image.tobytes())
    # Match MakeCode's encoder: extra bytes start at the first byte of the added rows,
    # not at the end of the expanded image.
    added_offset = original_width * original_height * 4

    header = to_u32le(
        [
            PNG_IMAGE_MAGIC,
            len(blob),
            added_lines,
            0,
            0,
            0,
            0,
            0,
            0,
        ]
    )

    def encode(img: bytearray, ptr: int, bits_per_channel: int, source: bytes) -> int:
        shift = 0
        data_index = 0
        value = source[data_index]
        mask = (1 << bits_per_channel) - 1
        keep_going = True
        while keep_going:
            bits = (value >> shift) & mask
            left = 8 - shift
            if left <= bits_per_channel:
                data_index += 1
                if data_index >= len(source):
                    if left == 0:
                        break
                    keep_going = False
                    value = 0
                else:
                    value = source[data_index]
                bits |= (value << left) & mask
                shift = bits_per_channel - left
            else:
                shift += bits_per_channel
            img[ptr] = (img[ptr] & ~mask) | bits
            ptr += 1
            if (ptr & 3) == 3:
                img[ptr] = 0xFF
                ptr += 1
        return ptr

    encode(data, 0, 1, bytes([bpp]))
    ptr = 4
    ptr = encode(data, ptr, bpp, header)
    if added_lines == 0:
        ptr = encode(data, ptr, bpp, blob)
    else:
        first_chunk = img_capacity - len(header)
        ptr = encode(data, ptr, bpp, blob[:first_chunk])
        encode(data, added_offset, 8, blob[first_chunk:])

    ptr |= 3
    while ptr < len(data):
        data[ptr] = 0xFF
        ptr += 4

    return Image.frombytes("RGBA", image.size, bytes(data))


def pack_png(files: Dict[str, str], output_path: Path, carrier_path: Path | None) -> None:
    payload = build_png_payload(files)
    blob = lzma_alone_compress(payload)
    carrier = Image.open(carrier_path) if carrier_path else draw_default_canvas(get_project_name(files))
    try:
        encoded = encode_blob_into_png(carrier, blob)
        write_bytes(output_path, image_to_png_bytes(encoded))
    finally:
        carrier.close()


def image_to_png_bytes(image: Image.Image) -> bytes:
    import io

    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def split_blocks(payload: bytes, base_addr: int) -> List[Tuple[int, bytes]]:
    blocks: List[Tuple[int, bytes]] = []
    for offset in range(0, len(payload), UF2_PAYLOAD_SIZE):
        chunk = payload[offset : offset + UF2_PAYLOAD_SIZE]
        blocks.append((base_addr + offset, chunk))
    return blocks


def parse_uf2_template(path: Path) -> List[Tuple[int, int, int, bytes]]:
    data = path.read_bytes()
    if len(data) % UF2_BLOCK_SIZE:
        raise PackError("Template UF2 size is not a multiple of 512 bytes")

    blocks: List[Tuple[int, int, int, bytes]] = []
    for offset in range(0, len(data), UF2_BLOCK_SIZE):
        block = data[offset : offset + UF2_BLOCK_SIZE]
        fields = struct.unpack("<IIIIIIII", block[:32])
        magic0, magic1, flags, target_addr, payload_size, block_no, num_blocks, family = fields
        if magic0 != UF2_MAGIC_START0 or magic1 != UF2_MAGIC_START1:
            raise PackError("Invalid UF2 template magic")
        payload = block[32 : 32 + payload_size]
        blocks.append((flags, target_addr, family, payload))
    return blocks


def find_template_source_region(blocks: Sequence[Tuple[int, int, int, bytes]]) -> Tuple[int, int] | None:
    if not blocks:
        return None

    ordered = sorted(blocks, key=lambda item: item[1])
    base_addr = ordered[0][1]
    assembled = bytearray()
    current = base_addr

    for _flags, addr, _family, payload in ordered:
        if addr < current:
            continue
        if addr > current:
            assembled.extend(b"\x00" * (addr - current))
            current = addr
        assembled.extend(payload)
        current = addr + len(payload)

    offset = bytes(assembled).find(SOURCE_MAGIC)
    if offset < 0:
        return None

    json_len = struct.unpack_from("<H", assembled, offset + 8)[0]
    text_len = struct.unpack_from("<I", assembled, offset + 10)[0]
    total = 16 + json_len + text_len
    total += (-total) % 16
    return base_addr + offset, base_addr + offset + total


def encode_uf2_block(flags: int, target_addr: int, payload: bytes, block_no: int, num_blocks: int, family: int) -> bytes:
    block = bytearray(UF2_BLOCK_SIZE)
    struct.pack_into(
        "<IIIIIIII",
        block,
        0,
        UF2_MAGIC_START0,
        UF2_MAGIC_START1,
        flags,
        target_addr,
        len(payload),
        block_no,
        num_blocks,
        family,
    )
    block[32 : 32 + len(payload)] = payload
    struct.pack_into("<I", block, UF2_BLOCK_SIZE - 4, UF2_MAGIC_END)
    return bytes(block)


def pack_uf2(
    files: Dict[str, str],
    output_path: Path,
    template_path: Path | None,
    base_addr: int,
    family_id: int,
) -> None:
    source_blob = build_uf2_source_blob(files)

    block_specs: List[Tuple[int, int, int, bytes]] = []
    if template_path:
        block_specs.extend(parse_uf2_template(template_path))
        source_region = find_template_source_region(block_specs)
        if source_region:
            region_start, region_end = source_region
            filtered: List[Tuple[int, int, int, bytes]] = []
            for flags, addr, family, payload in block_specs:
                payload_end = addr + len(payload)
                if payload_end <= region_start or addr >= region_end:
                    filtered.append((flags, addr, family, payload))
            block_specs = filtered
            base_addr = region_start
        else:
            used_end = max((addr + len(payload) for _flags, addr, _family, payload in block_specs), default=base_addr)
            base_addr = (used_end + 15) & ~0xF
    source_blocks = split_blocks(source_blob, base_addr)
    for addr, payload in source_blocks:
        block_specs.append((UF2_FLAG_NOFLASH, addr, family_id, payload))

    encoded_blocks = [
        encode_uf2_block(flags, addr, payload, index, len(block_specs), family)
        for index, (flags, addr, family, payload) in enumerate(block_specs)
    ]
    write_bytes(output_path, b"".join(encoded_blocks))


def create_output(
    project_dir: Path,
    output_path: Path,
    fmt: str,
    include_tests: bool,
    carrier_png: Path | None,
    uf2_template: Path | None,
    uf2_base_addr: int,
    family_id: int,
) -> None:
    files = collect_project_files(project_dir, include_tests=include_tests)

    if output_path.exists():
        if output_path.is_dir():
            shutil.rmtree(output_path)
        else:
            output_path.unlink()

    if fmt == "png":
        pack_png(files, output_path, carrier_png)
    elif fmt == "uf2":
        pack_uf2(files, output_path, uf2_template, uf2_base_addr, family_id)
    else:
        raise PackError(f"Unsupported format: {fmt}")


def parse_int(value: str) -> int:
    return int(value, 0)


def main(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Pack a MakeCode Arcade project folder into a Magic PNG or a source-embedded UF2."
    )
    parser.add_argument("project_dir", type=Path, help="Folder containing pxt.json and project files")
    parser.add_argument("output", type=Path, help="Path to the output .png or .uf2 file")
    parser.add_argument("--format", choices=["png", "uf2"], required=True, help="Output format")
    parser.add_argument("--include-tests", action="store_true", help="Include testFiles from pxt.json")
    parser.add_argument("--carrier-png", type=Path, help="Optional PNG to use as the visual carrier")
    parser.add_argument("--uf2-template", type=Path, help="Optional compiled UF2 to preserve as the carrier")
    parser.add_argument("--uf2-base-addr", type=parse_int, default=DEFAULT_UF2_BASE_ADDR, help="Base address for source-only UF2 blocks")
    parser.add_argument("--family-id", type=parse_int, default=0, help="UF2 family ID for generated source blocks")
    args = parser.parse_args(list(argv))

    try:
        create_output(
            project_dir=args.project_dir,
            output_path=args.output,
            fmt=args.format,
            include_tests=args.include_tests,
            carrier_png=args.carrier_png,
            uf2_template=args.uf2_template,
            uf2_base_addr=args.uf2_base_addr,
            family_id=args.family_id,
        )
    except PackError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"Wrote {args.format.upper()} to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
