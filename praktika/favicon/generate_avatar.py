import random
import struct
import zlib


def create_png(width, height, image_data):
    def write_chunk(chunk_type, data):
        chunk_len = struct.pack(">I", len(data))
        chunk_crc = struct.pack(">I", zlib.crc32(chunk_type + data) & 0xFFFFFFFF)
        return chunk_len + chunk_type + data + chunk_crc

    png_signature = b"\x89PNG\r\n\x1a\n"
    ihdr_chunk = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    idat_data = zlib.compress(
        b"".join(
            b"\x00" + image_data[y * width * 4 : (y + 1) * width * 4]
            for y in range(height)
        ),
        9,
    )
    return (
        png_signature
        + write_chunk(b"IHDR", ihdr_chunk)
        + write_chunk(b"IDAT", idat_data)
        + write_chunk(b"IEND", b"")
    )


def create_avatar_png(size: int = 256) -> bytes:
    bg_color = [255, 255, 255, 255]  # white
    bar_color = [0, 0, 0, 255]       # black

    scale = size / 32
    bar_width = max(1, round(4 * scale))
    gap = max(1, round(3 * scale))
    num_bars = 4
    line_height = size - gap

    image_data = bytearray(bg_color * size * size)

    x = gap
    for _ in range(num_bars):
        y_start = random.randint(0, size - 1)
        for y in range(line_height):
            y_pos = (y + y_start) % size
            for x_off in range(bar_width):
                idx = (y_pos * size + x + x_off) * 4
                image_data[idx : idx + 4] = bar_color
        x += bar_width + gap

    return create_png(size, size, image_data)


if __name__ == "__main__":
    png_data = create_avatar_png(size=256)
    out_path = "avatar.png"
    with open(out_path, "wb") as f:
        f.write(png_data)
    print(f"Saved {out_path}")
