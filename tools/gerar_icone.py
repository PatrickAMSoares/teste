#!/usr/bin/env python3
"""Gera o ícone do aplicativo (.ico com vários tamanhos e .png para a interface).

Executar quando o desenho mudar::

    python tools/gerar_icone.py
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

DESTINO = Path(__file__).resolve().parent.parent / "src" / "photodedupe" / "resources"
FUNDO = (23, 27, 34)
AZUL = (59, 158, 255)
AZUL_ESCURO = (42, 134, 224)
CLARO = (233, 236, 241)
AMARELO = (255, 206, 61)


def desenhar(tamanho: int = 512) -> Image.Image:
    """Duas fotos sobrepostas (duplicatas) com uma estrela na melhor delas."""
    s = tamanho
    imagem = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    draw = ImageDraw.Draw(imagem)

    raio = int(s * 0.22)
    draw.rounded_rectangle([int(s * 0.04), int(s * 0.04), int(s * 0.96), int(s * 0.96)], raio, fill=FUNDO)

    # Foto de trás (a cópia, mais apagada).
    draw.rounded_rectangle(
        [int(s * 0.16), int(s * 0.14), int(s * 0.66), int(s * 0.64)],
        int(s * 0.06), fill=(58, 66, 80), outline=(90, 100, 118), width=max(1, s // 96),
    )

    # Foto da frente (a que será mantida).
    frente = [int(s * 0.32), int(s * 0.30), int(s * 0.86), int(s * 0.84)]
    draw.rounded_rectangle(frente, int(s * 0.06), fill=CLARO, outline=AZUL, width=max(2, s // 64))

    # "Paisagem" dentro da foto da frente.
    x0, y0, x1, y1 = frente
    margem = int(s * 0.035)
    draw.rectangle([x0 + margem, y0 + margem, x1 - margem, y1 - margem], fill=(198, 214, 232))
    draw.ellipse(
        [x0 + int(s * 0.07), y0 + int(s * 0.07), x0 + int(s * 0.16), y0 + int(s * 0.16)],
        fill=AMARELO,
    )
    draw.polygon(
        [
            (x0 + margem, y1 - margem),
            (x0 + int(s * 0.20), y0 + int(s * 0.22)),
            (x0 + int(s * 0.34), y1 - margem),
        ],
        fill=(72, 130, 96),
    )
    draw.polygon(
        [
            (x0 + int(s * 0.18), y1 - margem),
            (x0 + int(s * 0.34), y0 + int(s * 0.28)),
            (x1 - margem, y1 - margem),
        ],
        fill=(96, 158, 120),
    )

    # Estrela: "esta é a melhor versão".
    cx, cy, r = int(s * 0.80), int(s * 0.80), int(s * 0.135)
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=AZUL_ESCURO, outline=FUNDO, width=max(2, s // 80))
    pontos = []
    import math

    for i in range(10):
        angulo = -math.pi / 2 + i * math.pi / 5
        raio_ponta = r * (0.62 if i % 2 == 0 else 0.28)
        pontos.append((cx + raio_ponta * math.cos(angulo), cy + raio_ponta * math.sin(angulo)))
    draw.polygon(pontos, fill=AMARELO)
    return imagem


def main() -> int:
    DESTINO.mkdir(parents=True, exist_ok=True)
    base = desenhar(512)
    base.save(DESTINO / "photodedupe.png")
    tamanhos = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
    base.save(DESTINO / "photodedupe.ico", sizes=tamanhos)
    print(f"Ícone gravado em {DESTINO}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
