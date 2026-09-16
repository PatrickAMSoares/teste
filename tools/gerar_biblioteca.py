#!/usr/bin/env python3
"""Gera uma biblioteca sintética de fotos para testes de desempenho e precisão.

As imagens imitam fotografias reais (céu com gradiente, objetos, textura e
grão) e recebem EXIF plausível. A partir de cada "original" são criadas
variações com relação conhecida, gravadas em ``verdade.json``:

``exact``   cópia byte a byte
``visual``  mesma foto recompactada, redimensionada, convertida ou reexportada
``crop``    recorte de 5% a 12%
``burst``   outra foto da mesma cena, tirada segundos depois (NÃO é duplicata)
``unique``  cena diferente

Uso::

    python tools/gerar_biblioteca.py --saida /caminho/biblioteca --originais 900
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import shutil
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np
from PIL import Image, ImageEnhance, ImageFilter
from PIL.ExifTags import TAGS

_TAG = {v: k for k, v in TAGS.items()}

CAMERAS = [
    ("Canon", "Canon EOS R6", "RF24-70mm F2.8 L IS USM"),
    ("NIKON CORPORATION", "NIKON Z 6_2", "NIKKOR Z 24-70mm f/4 S"),
    ("SONY", "ILCE-7M4", "FE 35mm F1.8"),
    ("Apple", "iPhone 14 Pro", "iPhone 14 Pro back camera 6.86mm f/1.78"),
    ("samsung", "SM-S918B", "Samsung Galaxy S23 Ultra"),
    ("FUJIFILM", "X-T5", "XF16-55mmF2.8 R LM WR"),
]


# --------------------------------------------------------------------- cenas
def _smooth_field(rng: random.Random, h: int, w: int, cells: int) -> np.ndarray:
    """Campo aleatório suave (base das nuvens, manchas e texturas)."""
    small = np.random.default_rng(rng.randrange(1 << 30)).random((cells, cells)).astype(np.float32)
    img = Image.fromarray((small * 255).astype(np.uint8), mode="L").resize((w, h), Image.Resampling.BICUBIC)
    return np.asarray(img, dtype=np.float32) / 255.0


def make_scene(seed: int, width: int = 1280, height: int = 960, variant: int = 0) -> Image.Image:
    """Cria uma "fotografia" procedural determinística a partir da semente.

    ``variant > 0`` produz **a mesma cena** fotografada instantes depois: os
    objetos deslocam-se poucos pixels, a exposição varia de leve e o grão do
    sensor é outro. É o caso mais difícil para um detector de duplicatas - duas
    fotos parecidíssimas que NÃO são a mesma fotografia.
    """
    rng = random.Random(seed * 7919)          # mesma composição para todas as variantes
    nprng = np.random.default_rng(seed * 104729 + variant * 7717)

    yy, xx = np.mgrid[0:height, 0:width].astype(np.float32)
    ny, nx = yy / height, xx / width

    # Céu / fundo com gradiente e matiz própria de cada cena.
    top = np.array([rng.uniform(40, 235), rng.uniform(40, 235), rng.uniform(60, 245)], dtype=np.float32)
    bottom = np.array([rng.uniform(20, 200), rng.uniform(30, 210), rng.uniform(20, 190)], dtype=np.float32)
    canvas = top[None, None, :] * (1 - ny[:, :, None]) + bottom[None, None, :] * ny[:, :, None]

    # Nuvens / manchas de iluminação.
    clouds = _smooth_field(rng, height, width, rng.choice([4, 6, 8]))
    canvas += (clouds[:, :, None] - 0.5) * rng.uniform(30, 90)

    # Linha do horizonte e "terreno" texturizado.
    horizon = rng.uniform(0.45, 0.75)
    ground_mask = (ny > horizon).astype(np.float32)
    ground_color = np.array([rng.uniform(30, 160), rng.uniform(40, 170), rng.uniform(20, 120)], dtype=np.float32)
    texture = _smooth_field(rng, height, width, 48)
    ground = ground_color[None, None, :] * (0.65 + 0.7 * texture[:, :, None])
    canvas = canvas * (1 - ground_mask[:, :, None]) + ground * ground_mask[:, :, None]

    # Objetos com bordas nítidas (dão alta frequência real à imagem).
    for _ in range(rng.randint(3, 8)):
        cx, cy = rng.uniform(0.1, 0.9), rng.uniform(0.15, 0.95)
        rx, ry = rng.uniform(0.03, 0.18), rng.uniform(0.04, 0.24)
        shift = variant * rng.uniform(0.006, 0.03)   # objetos se movem entre os disparos
        dist = ((nx - cx - shift) / rx) ** 2 + ((ny - cy) / ry) ** 2
        mask = (dist < 1.0).astype(np.float32)
        color = np.array([rng.uniform(10, 245), rng.uniform(10, 245), rng.uniform(10, 245)], dtype=np.float32)
        canvas = canvas * (1 - mask[:, :, None]) + color[None, None, :] * mask[:, :, None]

    # Listras finas (grades, galhos, detalhes arquitetônicos).
    for _ in range(rng.randint(1, 4)):
        angle = rng.uniform(0, math.pi)
        freq = rng.uniform(60, 220)
        stripes = ((np.cos(angle) * xx + np.sin(angle) * yy) % freq) < rng.uniform(1.5, 4.0)
        tint = rng.uniform(-70, 70)
        canvas[stripes] += tint

    # Grão de sensor.
    canvas += nprng.normal(0, rng.uniform(2.5, 7.0), canvas.shape)
    if variant:
        # Pequena variação de exposição entre disparos da mesma sequência.
        canvas *= 1.0 + (nprng.random() - 0.5) * 0.06
    image = Image.fromarray(np.clip(canvas, 0, 255).astype(np.uint8), mode="RGB")
    if rng.random() < 0.25:
        image = image.filter(ImageFilter.GaussianBlur(rng.uniform(0.3, 0.8)))
    return image


def build_exif(seed: int, when: str, subsec: str, software: str = "") -> Image.Exif:
    rng = random.Random(seed)
    make, model, lens = CAMERAS[seed % len(CAMERAS)]
    exif = Image.Exif()
    exif[_TAG["Make"]] = make
    exif[_TAG["Model"]] = model
    if software:
        exif[_TAG["Software"]] = software
    sub = exif.get_ifd(0x8769)
    sub[_TAG["DateTimeOriginal"]] = when
    sub[_TAG["SubsecTimeOriginal"]] = subsec
    sub[_TAG["LensModel"]] = lens
    sub[_TAG["ISOSpeedRatings"]] = rng.choice([100, 200, 400, 800, 1600])
    sub[_TAG["FNumber"]] = (rng.choice([18, 28, 40, 56]), 10)
    sub[_TAG["ExposureTime"]] = (1, rng.choice([60, 125, 250, 500, 1000]))
    sub[_TAG["FocalLength"]] = (rng.choice([24, 35, 50, 70, 85]), 1)
    return exif


# ------------------------------------------------------------------ variações
def _timestamp(seed: int, offset_s: int = 0) -> str:
    base = 1_600_000_000 + (seed * 3607) % 100_000_000 + offset_s
    return time.strftime("%Y:%m:%d %H:%M:%S", time.gmtime(base))


def generate_one(job: tuple) -> list[dict]:
    """Gera um original e suas variações. Executado nos processos de trabalho."""
    seed, out_dir, options = job
    rng = random.Random(seed * 31337)
    out = Path(out_dir)
    folder = out / f"pasta_{seed % options['folders']:03d}"
    folder.mkdir(parents=True, exist_ok=True)
    records: list[dict] = []

    width = rng.choice([1280, 1600, 1920])
    height = int(width * rng.choice([0.75, 0.75, 0.6667]))
    scene = make_scene(seed, width, height)
    when = _timestamp(seed)
    subsec = f"{rng.randint(0, 99):02d}"

    original = folder / f"IMG_{seed:05d}.jpg"
    scene.save(original, quality=rng.choice([92, 94, 96]), exif=build_exif(seed, when, subsec).tobytes())
    records.append({"path": str(original), "origin": seed, "relation": "original"})

    def record(path: Path, relation: str) -> None:
        records.append({"path": str(path), "origin": seed, "relation": relation})

    roll = rng.random()

    # 1. cópia exata (arquivo idêntico, outro nome/pasta)
    if roll < options["exact_ratio"]:
        copy_folder = out / f"pasta_{(seed + 1) % options['folders']:03d}"
        copy_folder.mkdir(parents=True, exist_ok=True)
        target = copy_folder / f"IMG_{seed:05d} (1).jpg"
        shutil.copy2(original, target)
        record(target, "exact")

    # 2. duplicatas visuais (recompressão, redimensionamento, conversão, edição)
    if rng.random() < options["visual_ratio"]:
        kinds = rng.sample(
            ["recomprimida", "reduzida", "png", "webp", "brilho", "sem_exif", "muito_comprimida"],
            k=rng.randint(1, 3),
        )
        for kind in kinds:
            if kind == "recomprimida":
                target = folder / f"IMG_{seed:05d}_copia.jpg"
                scene.save(target, quality=rng.choice([70, 78, 85]), exif=build_exif(seed, when, subsec).tobytes())
            elif kind == "reduzida":
                factor = rng.choice([0.5, 0.6, 0.75])
                small = scene.resize((int(width * factor), int(height * factor)), Image.Resampling.LANCZOS)
                target = folder / f"IMG_{seed:05d}_web.jpg"
                small.save(target, quality=rng.choice([75, 82, 88]), exif=build_exif(seed, when, subsec, "Adobe Photoshop 25.0").tobytes())
            elif kind == "png":
                target = folder / f"IMG_{seed:05d}.png"
                scene.save(target)
            elif kind == "webp":
                target = folder / f"IMG_{seed:05d}.webp"
                scene.save(target, quality=rng.choice([75, 85]))
            elif kind == "brilho":
                target = folder / f"IMG_{seed:05d}_editada.jpg"
                edited = ImageEnhance.Brightness(scene).enhance(rng.uniform(0.9, 1.12))
                edited = ImageEnhance.Contrast(edited).enhance(rng.uniform(0.94, 1.08))
                edited.save(target, quality=88, exif=build_exif(seed, when, subsec, "Lightroom 13").tobytes())
            elif kind == "sem_exif":
                target = folder / f"foto_{seed:05d}_final.jpg"
                scene.save(target, quality=86)
            else:
                target = folder / f"IMG-{seed:05d}-WA0001.jpg"
                whats = scene.resize((int(width * 0.55), int(height * 0.55)), Image.Resampling.LANCZOS)
                whats.save(target, quality=rng.choice([38, 45, 55]))
            record(target, "visual")

    # 3. recorte leve (não deve ser tratado como duplicata exata)
    if rng.random() < options["crop_ratio"]:
        margin = rng.uniform(0.05, 0.12)
        box = (
            int(width * margin), int(height * margin),
            int(width * (1 - margin)), int(height * (1 - margin)),
        )
        target = folder / f"IMG_{seed:05d}_recorte.jpg"
        scene.crop(box).save(target, quality=90, exif=build_exif(seed, when, subsec).tobytes())
        record(target, "crop")

    # 4. foto em sequência (rajada): MESMA cena, instante diferente -> não é duplicata
    if rng.random() < options["burst_ratio"]:
        for n in range(1, rng.randint(2, 3)):
            burst = make_scene(seed, width, height, variant=n)
            target = folder / f"IMG_{seed:05d}_{n}.jpg"
            burst_when = _timestamp(seed, offset_s=n * rng.randint(1, 4))
            burst.save(target, quality=94, exif=build_exif(seed, burst_when, f"{rng.randint(0, 99):02d}").tobytes())
            record(target, "burst")

    # 5. fotos com problema (para a análise de qualidade)
    if rng.random() < options["bad_ratio"]:
        kind = rng.choice(["desfocada", "escura", "clara", "pequena"])
        if kind == "desfocada":
            target = folder / f"IMG_{seed:05d}_tremida.jpg"
            scene.filter(ImageFilter.GaussianBlur(rng.uniform(4, 9))).save(target, quality=90)
        elif kind == "escura":
            target = folder / f"IMG_{seed:05d}_escura.jpg"
            ImageEnhance.Brightness(scene).enhance(0.12).save(target, quality=90)
        elif kind == "clara":
            target = folder / f"IMG_{seed:05d}_estourada.jpg"
            ImageEnhance.Brightness(scene).enhance(2.6).save(target, quality=90)
        else:
            target = folder / f"IMG_{seed:05d}_mini.jpg"
            scene.resize((240, int(240 * height / width))).save(target, quality=80)
        record(target, "bad")

    return records


def main() -> int:
    parser = argparse.ArgumentParser(description="Gera uma biblioteca de fotos sintética para testes")
    parser.add_argument("--saida", required=True, help="pasta de destino")
    parser.add_argument("--originais", type=int, default=800, help="quantidade de cenas distintas")
    parser.add_argument("--pastas", type=int, default=24, help="em quantas pastas distribuir")
    parser.add_argument("--processos", type=int, default=max(1, (os.cpu_count() or 4) - 1))
    parser.add_argument("--exata", type=float, default=0.22)
    parser.add_argument("--visual", type=float, default=0.55)
    parser.add_argument("--recorte", type=float, default=0.12)
    parser.add_argument("--rajada", type=float, default=0.30)
    parser.add_argument("--ruins", type=float, default=0.10)
    parser.add_argument("--limpar", action="store_true", help="apaga a pasta antes de gerar")
    args = parser.parse_args()

    out = Path(args.saida)
    if args.limpar and out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True, exist_ok=True)

    options = {
        "folders": max(1, args.pastas),
        "exact_ratio": args.exata,
        "visual_ratio": args.visual,
        "crop_ratio": args.recorte,
        "burst_ratio": args.rajada,
        "bad_ratio": args.ruins,
    }
    jobs = [(seed, str(out), options) for seed in range(1, args.originais + 1)]

    started = time.perf_counter()
    records: list[dict] = []
    with ProcessPoolExecutor(max_workers=args.processos) as pool:
        for index, result in enumerate(pool.map(generate_one, jobs, chunksize=4), start=1):
            records.extend(result)
            if index % 50 == 0:
                print(f"  {index}/{len(jobs)} cenas ({len(records)} arquivos)", flush=True)

    truth_path = out / "verdade.json"
    truth_path.write_text(json.dumps(records, indent=1, ensure_ascii=False), encoding="utf-8")
    total_bytes = sum(Path(r["path"]).stat().st_size for r in records)
    elapsed = time.perf_counter() - started
    counts: dict[str, int] = {}
    for record in records:
        counts[record["relation"]] = counts.get(record["relation"], 0) + 1
    print(f"\n{len(records)} arquivos gerados em {elapsed:.1f}s ({total_bytes / 1e9:.2f} GB)")
    for relation, count in sorted(counts.items()):
        print(f"  {relation:10s}: {count}")
    print(f"Gabarito: {truth_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
