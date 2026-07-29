from pathlib import Path

from PIL import Image, ImageDraw


root = Path(__file__).resolve().parents[1]
assets = root / "assets"
assets.mkdir(exist_ok=True)

size = 256
image = Image.new("RGBA", (size, size), "#07101d")
draw = ImageDraw.Draw(image)
draw.rounded_rectangle((20, 20, 236, 236), radius=42, fill="#168cd1")
draw.rounded_rectangle((45, 45, 211, 211), radius=28, fill="#0b1d31")

nodes = [(66, 128), (128, 66), (190, 128), (128, 190), (128, 128)]
for a, b in [(0, 4), (1, 4), (2, 4), (3, 4)]:
    draw.line((nodes[a], nodes[b]), fill="#53d8ff", width=10)
for index, (x, y) in enumerate(nodes):
    radius = 22 if index == 4 else 16
    color = "#ffffff" if index == 4 else "#ffbd4a"
    draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill=color, outline="#0b1d31", width=5)

image.save(assets / "meshlab.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
