# -*- coding: utf-8 -*-
"""Оригинальная иконка BaseLauncher: тёмно-синий скруглённый квадрат, «список баз» + «запуск».

Все полупрозрачные элементы рисуются на отдельном слое и накладываются через
alpha_composite: если писать RGBA-цвет прямо в базовое изображение, PIL заменяет пиксели
вместе с альфой, и полупрозрачные фигуры становятся «дырами» (в просмотрщиках выглядят
белыми), а белый треугольник внутри такого круга исчезает.
"""
import pathlib

from PIL import Image, ImageDraw

OUT = pathlib.Path(r'D:\git_repos\baselauncher\assets')
OUT.mkdir(parents=True, exist_ok=True)
S = 1024

top, bot = (58, 126, 219), (21, 38, 78)
col = Image.new('RGB', (1, S))
for y in range(S):
    k = y / (S - 1)
    col.putpixel((0, y), tuple(int(top[i] + (bot[i] - top[i]) * k) for i in range(3)))
base = col.resize((S, S)).convert('RGBA')

shape = Image.new('L', (S, S), 0)
ImageDraw.Draw(shape).rounded_rectangle([0, 0, S - 1, S - 1], radius=int(S * 0.22), fill=255)

canvas = Image.new('RGBA', (S, S), (0, 0, 0, 0))
canvas.paste(base, (0, 0), shape)


def layer():
    return Image.new('RGBA', (S, S), (0, 0, 0, 0))


d = ImageDraw.Draw(canvas)
rows = [(0.285, 30, (255, 255, 255, 255)),
        (0.445, 26, (255, 255, 255, 165)),
        (0.605, 22, (255, 255, 255, 110))]
for cy, h, c in rows:
    y = int(S * cy)
    d.rounded_rectangle([int(S * 0.20), y, int(S * 0.615), y + h], radius=h // 2, fill=c)

r = int(S * 0.033)
cx, cy = int(S * 0.145), int(S * 0.285) + 15
d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(126, 231, 135, 255))

cxc, cyc, rc = int(S * 0.715), int(S * 0.585), int(S * 0.215)
ring = layer()
ImageDraw.Draw(ring).ellipse([cxc - rc, cyc - rc, cxc + rc, cyc + rc],
                             fill=(255, 255, 255, 38),
                             outline=(255, 255, 255, 170), width=int(S * 0.017))
canvas = Image.alpha_composite(canvas, ring)

play = layer()
ImageDraw.Draw(play).polygon([(cxc - rc * 0.30, cyc - rc * 0.46),
                              (cxc - rc * 0.30, cyc + rc * 0.46),
                              (cxc + rc * 0.48, cyc)], fill=(255, 255, 255, 255))
canvas = Image.alpha_composite(canvas, play)

gloss = layer()
ImageDraw.Draw(gloss).ellipse([-int(S * 0.30), -int(S * 0.85), int(S * 1.30), int(S * 0.40)],
                              fill=(255, 255, 255, 22))
canvas = Image.alpha_composite(canvas, gloss)

out = Image.new('RGBA', (S, S), (0, 0, 0, 0))
out.paste(canvas, (0, 0), shape)

sizes = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
out.resize((256, 256), Image.LANCZOS).save(OUT / 'baselauncher.ico', format='ICO', sizes=sizes)
out.resize((512, 512), Image.LANCZOS).save(OUT / 'baselauncher-512.png', format='PNG')
out.resize((128, 128), Image.LANCZOS).save(OUT / 'baselauncher-128.png', format='PNG')
print('ICO', (OUT / 'baselauncher.ico').stat().st_size,
      'PNG512', (OUT / 'baselauncher-512.png').stat().st_size)
