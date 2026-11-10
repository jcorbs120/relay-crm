"""Draws the Relay CRM icon (PNG for the UI, ICO for Windows)."""
from pathlib import Path
from PIL import Image, ImageDraw

root = Path(__file__).resolve().parent.parent
S = 512
img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(img)
d.rounded_rectangle([16, 16, S - 16, S - 16], radius=110, fill=(31, 78, 121, 255))
# envelope
d.rounded_rectangle([96, 150, 416, 362], radius=26, fill=(255, 255, 255, 255))
d.line([(108, 166), (256, 276), (404, 166)], fill=(31, 78, 121, 255), width=26, joint="curve")
# sync arrow badge
d.ellipse([300, 300, 470, 470], fill=(46, 160, 110, 255), outline=(31, 78, 121, 255), width=14)
d.arc([338, 338, 432, 432], start=200, end=500, fill=(255, 255, 255, 255), width=16)
d.polygon([(418, 342), (444, 392), (392, 386)], fill=(255, 255, 255, 255))
img.save(root / "relay" / "static" / "icon.png")
img.save(root / "packaging" / "icon.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
print("icons written")
