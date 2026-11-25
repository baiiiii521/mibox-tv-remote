#!/usr/bin/env python3
"""生成遥控器 App 图标（PWA / iOS 主屏用）。"""
import os
from PIL import Image, ImageDraw

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
S = 1024
img = Image.new("RGB", (S, S), "#05070a")
d = ImageDraw.Draw(img)

# 背景渐变
for y in range(S):
    t = y / S
    r = int(13 + (5 - 13) * t)
    g = int(17 + (7 - 17) * t)
    b = int(22 + (10 - 22) * t)
    d.line([(0, y), (S, y)], fill=(r, g, b))

# 顶部青色光晕
glow = Image.new("L", (S, S), 0)
gd = ImageDraw.Draw(glow)
for i in range(60):
    rr = 520 - i * 7
    gd.ellipse([S // 2 - rr, -300 - i * 4, S // 2 + rr, 300 - i * 4], fill=int(3 + i * 0.9))
img = Image.composite(Image.new("RGB", (S, S), "#0e3b45"), img, glow.filter(__import__("PIL.ImageFilter", fromlist=["ImageFilter"]).GaussianBlur(60)))
d = ImageDraw.Draw(img)

cx = cy = S // 2
# 外圈
d.ellipse([cx - 372, cy - 372, cx + 372, cy + 372], outline=(255, 255, 255, 30), width=6)
d.ellipse([cx - 340, cy - 340, cx + 340, cy + 340], fill=None, outline=(120, 200, 210), width=3)

# 四个方向箭头
def chevron(direction, dist=236, size=52, w=40):
    if direction == "up":
        pts = [(cx - size, cy - dist + size), (cx, cy - dist - size // 2), (cx + size, cy - dist + size)]
    elif direction == "down":
        pts = [(cx - size, cy + dist - size), (cx, cy + dist + size // 2), (cx + size, cy + dist - size)]
    elif direction == "left":
        pts = [(cx - dist + size, cy - size), (cx - dist - size // 2, cy), (cx - dist + size, cy + size)]
    else:
        pts = [(cx + dist - size, cy - size), (cx + dist + size // 2, cy), (cx + dist - size, cy + size)]
    d.line(pts, fill=(226, 236, 244), width=w, joint="curve")

for dirn in ("up", "down", "left", "right"):
    chevron(dirn)

# 中心 OK 圆
d.ellipse([cx - 132, cy - 132, cx + 132, cy + 132], fill=(94, 234, 212))
d.ellipse([cx - 132, cy - 132, cx + 40, cy + 132], fill=(140, 245, 228))

for size in (512, 192, 180):
    img.resize((size, size), Image.LANCZOS).save(os.path.join(OUT, "icon-%d.png" % size), "PNG", optimize=True)
img.resize((180, 180), Image.LANCZOS).save(os.path.join(OUT, "apple-touch-icon.png"), "PNG", optimize=True)
print("icons ok")
