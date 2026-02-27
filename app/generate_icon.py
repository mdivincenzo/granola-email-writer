#!/usr/bin/env python3
"""Generate a 1024x1024 PNG icon for Follow-Up.app.

Uses only Python stdlib (struct, zlib) to write a minimal PNG.
Creates a dark rounded rectangle with a green checkmark and envelope silhouette.
"""
import struct
import zlib
import os
import subprocess

SIZE = 1024
BG = (17, 17, 22)        # #111116
GREEN = (34, 197, 94)    # #22c55e
DARK = (9, 9, 11)        # #09090b
WHITE = (250, 250, 250)  # #fafafa


def make_png(width, height, pixels):
    """Create a PNG file from a flat list of (r, g, b, a) tuples."""
    def chunk(chunk_type, data):
        c = chunk_type + data
        return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)

    header = b"\x89PNG\r\n\x1a\n"
    ihdr = chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))

    raw_data = bytearray()
    for y in range(height):
        raw_data.append(0)  # filter type: None
        for x in range(width):
            r, g, b, a = pixels[y * width + x]
            raw_data.extend([r, g, b, a])

    compressed = zlib.compress(bytes(raw_data), 9)
    idat = chunk(b"IDAT", compressed)
    iend = chunk(b"IEND", b"")

    return header + ihdr + idat + iend


def distance(x1, y1, x2, y2):
    return ((x1 - x2) ** 2 + (y1 - y2) ** 2) ** 0.5


def rounded_rect(x, y, cx, cy, w, h, radius):
    """Check if (x, y) is inside a rounded rectangle centered at (cx, cy)."""
    # Offset from center
    dx = abs(x - cx)
    dy = abs(y - cy)
    hw, hh = w / 2, h / 2

    if dx > hw or dy > hh:
        return False, 0

    # Inside main body
    if dx <= hw - radius or dy <= hh - radius:
        return True, 1.0

    # Corner check
    corner_x = hw - radius
    corner_y = hh - radius
    if dx > corner_x and dy > corner_y:
        d = distance(dx, dy, corner_x, corner_y)
        if d <= radius:
            # Anti-alias the edge
            aa = max(0, min(1, radius - d + 0.5))
            return True, aa
        return False, 0

    return True, 1.0


def draw_line_thick(pixels, width, height, x1, y1, x2, y2, thickness, color):
    """Draw a thick anti-aliased line."""
    r, g, b = color
    # Bounding box
    min_x = max(0, int(min(x1, x2) - thickness - 1))
    max_x = min(width - 1, int(max(x1, x2) + thickness + 1))
    min_y = max(0, int(min(y1, y2) - thickness - 1))
    max_y = min(height - 1, int(max(y1, y2) + thickness + 1))

    dx = x2 - x1
    dy = y2 - y1
    line_len = max(distance(x1, y1, x2, y2), 0.001)

    for py in range(min_y, max_y + 1):
        for px in range(min_x, max_x + 1):
            # Project point onto line segment
            t = max(0, min(1, ((px - x1) * dx + (py - y1) * dy) / (line_len * line_len)))
            closest_x = x1 + t * dx
            closest_y = y1 + t * dy
            d = distance(px, py, closest_x, closest_y)

            if d < thickness + 1:
                alpha = max(0, min(1, thickness - d + 0.5))
                idx = py * width + px
                existing = pixels[idx]
                ea = existing[3] / 255.0
                na = alpha
                # Alpha composite
                out_a = na + ea * (1 - na)
                if out_a > 0:
                    out_r = int((r * na + existing[0] * ea * (1 - na)) / out_a)
                    out_g = int((g * na + existing[1] * ea * (1 - na)) / out_a)
                    out_b = int((b * na + existing[2] * ea * (1 - na)) / out_a)
                    pixels[idx] = (out_r, out_g, out_b, int(out_a * 255))


def main():
    w, h = SIZE, SIZE
    pixels = [(0, 0, 0, 0)] * (w * h)

    cx, cy = w // 2, h // 2
    rect_size = int(w * 0.82)
    corner_r = int(w * 0.18)

    # Draw rounded rectangle background
    for y in range(h):
        for x in range(w):
            inside, alpha = rounded_rect(x, y, cx, cy, rect_size, rect_size, corner_r)
            if inside and alpha > 0:
                r, g, b = BG
                pixels[y * w + x] = (r, g, b, int(alpha * 255))

    # Draw envelope body (simple rectangle in the lower-center area)
    env_cx, env_cy = cx, cy + int(h * 0.06)
    env_w, env_h = int(w * 0.42), int(w * 0.28)
    env_r = int(w * 0.03)

    for y in range(h):
        for x in range(w):
            inside, alpha = rounded_rect(x, y, env_cx, env_cy, env_w, env_h, env_r)
            if inside and alpha > 0:
                r, g, b = DARK
                idx = y * w + x
                pixels[idx] = (r, g, b, int(alpha * 255))

    # Draw envelope flap (triangle lines)
    env_left = env_cx - env_w // 2
    env_right = env_cx + env_w // 2
    env_top = env_cy - env_h // 2
    flap_thickness = int(w * 0.012)

    draw_line_thick(pixels, w, h,
                    env_left + flap_thickness, env_top + flap_thickness,
                    env_cx, env_cy - int(h * 0.01),
                    flap_thickness, (40, 40, 48))
    draw_line_thick(pixels, w, h,
                    env_right - flap_thickness, env_top + flap_thickness,
                    env_cx, env_cy - int(h * 0.01),
                    flap_thickness, (40, 40, 48))

    # Draw green checkmark (upper right area, overlapping envelope)
    check_cx = cx + int(w * 0.12)
    check_cy = cy - int(h * 0.08)
    check_scale = w * 0.0028
    thickness = int(w * 0.035)

    # Checkmark: two line segments
    # Short stroke going down-right
    p1x = check_cx - int(60 * check_scale)
    p1y = check_cy + int(5 * check_scale)
    p2x = check_cx - int(15 * check_scale)
    p2y = check_cy + int(45 * check_scale)
    # Long stroke going up-right
    p3x = check_cx + int(65 * check_scale)
    p3y = check_cy - int(40 * check_scale)

    draw_line_thick(pixels, w, h, p1x, p1y, p2x, p2y, thickness, GREEN)
    draw_line_thick(pixels, w, h, p2x, p2y, p3x, p3y, thickness, GREEN)

    # Write PNG
    script_dir = os.path.dirname(os.path.abspath(__file__))
    icon_path = os.path.join(script_dir, "icon.png")
    png_data = make_png(w, h, pixels)
    with open(icon_path, "wb") as f:
        f.write(png_data)
    print("Icon written to %s (%d bytes)" % (icon_path, len(png_data)))

    # Convert to .icns using macOS tools
    iconset_dir = os.path.join(script_dir, "icon.iconset")
    os.makedirs(iconset_dir, exist_ok=True)

    sizes = [
        (16, "icon_16x16.png"),
        (32, "icon_16x16@2x.png"),
        (32, "icon_32x32.png"),
        (64, "icon_32x32@2x.png"),
        (128, "icon_128x128.png"),
        (256, "icon_128x128@2x.png"),
        (256, "icon_256x256.png"),
        (512, "icon_256x256@2x.png"),
        (512, "icon_512x512.png"),
        (1024, "icon_512x512@2x.png"),
    ]

    for size, name in sizes:
        out = os.path.join(iconset_dir, name)
        subprocess.run(
            ["sips", "-z", str(size), str(size), icon_path, "--out", out],
            capture_output=True,
        )

    icns_path = os.path.join(script_dir, "icon.icns")
    result = subprocess.run(
        ["iconutil", "-c", "icns", iconset_dir, "-o", icns_path],
        capture_output=True, text=True,
    )
    if result.returncode == 0:
        print("Icon converted to %s" % icns_path)
    else:
        print("iconutil failed: %s" % result.stderr)

    # Clean up iconset
    import shutil
    shutil.rmtree(iconset_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
