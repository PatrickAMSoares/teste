"""Leitura de metadados EXIF.

A leitura é sempre somente-leitura: o arquivo é aberto em modo binário de
leitura e nunca regravado. Os dados servem para três finalidades:

1. informar o usuário (câmera, lente, ISO, data da foto);
2. compor o índice de qualidade (fotos com EXIF completo tendem a ser originais);
3. evitar falsos positivos - duas fotos com horários de captura diferentes são
   fotos diferentes, mesmo que pareçam iguais (rajada / sequência).
"""

from __future__ import annotations

import datetime as _dt
import logging
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path

from PIL import ExifTags, Image

log = logging.getLogger(__name__)

_TAGS = {v: k for k, v in ExifTags.TAGS.items()}
_GPS_TAGS = {v: k for k, v in ExifTags.GPSTAGS.items()}

_EDITOR_HINTS = (
    "photoshop", "lightroom", "gimp", "paint", "snapseed", "picasa", "canva",
    "whatsapp", "instagram", "facebook", "irfanview", "xnview", "pillow",
    "imagemagick", "ffmpeg", "google photos", "capture one", "affinity",
)


@dataclass
class ExifData:
    """Metadados normalizados de uma foto."""

    present: bool = False
    camera_make: str = ""
    camera_model: str = ""
    lens: str = ""
    iso: int | None = None
    aperture: float | None = None
    shutter: str = ""
    focal_length: float | None = None
    taken_at: str = ""          # ISO 8601, sem fuso
    subsec: str = ""
    digitized_at: str = ""
    software: str = ""
    orientation: int | None = None
    gps_lat: float | None = None
    gps_lon: float | None = None
    artist: str = ""
    copyright: str = ""
    color_space: str = ""
    exif_image_width: int | None = None
    exif_image_height: int | None = None
    raw: dict = field(default_factory=dict)

    # ------------------------------------------------------------------ helpers
    @property
    def has_camera(self) -> bool:
        return bool(self.camera_make or self.camera_model)

    @property
    def has_gps(self) -> bool:
        return self.gps_lat is not None and self.gps_lon is not None

    @property
    def edited_by_software(self) -> bool:
        s = self.software.lower()
        return any(h in s for h in _EDITOR_HINTS)

    def capture_key(self) -> str:
        """Identidade do instante da captura (data + subsegundo), se houver."""
        if not self.taken_at:
            return ""
        return f"{self.taken_at}.{self.subsec}" if self.subsec else self.taken_at

    def completeness(self) -> float:
        """0 a 1: quanto do EXIF relevante está presente."""
        checks = [
            self.has_camera, bool(self.lens), self.iso is not None,
            self.aperture is not None, bool(self.shutter), bool(self.taken_at),
            self.focal_length is not None, bool(self.color_space),
        ]
        return sum(1 for c in checks if c) / len(checks)

    def camera_label(self) -> str:
        make = (self.camera_make or "").strip()
        model = (self.camera_model or "").strip()
        if make and model and model.lower().startswith(make.lower()):
            return model
        return " ".join(x for x in (make, model) if x)

    def summary_lines(self, show_gps: bool = False) -> list[tuple[str, str]]:
        """Pares (rótulo, valor) para exibição na interface."""
        out: list[tuple[str, str]] = []
        if self.camera_label():
            out.append(("Câmera", self.camera_label()))
        if self.lens:
            out.append(("Lente", self.lens))
        if self.taken_at:
            out.append(("Data da fotografia", self.taken_at.replace("T", " ")))
        if self.iso:
            out.append(("ISO", str(self.iso)))
        if self.aperture:
            out.append(("Abertura", f"f/{self.aperture:g}"))
        if self.shutter:
            out.append(("Velocidade", self.shutter))
        if self.focal_length:
            out.append(("Distância focal", f"{self.focal_length:g} mm"))
        if self.software:
            out.append(("Software", self.software))
        if self.artist:
            out.append(("Autor", self.artist))
        if self.has_gps:
            out.append(("Localização", f"{self.gps_lat:.6f}, {self.gps_lon:.6f}" if show_gps else "disponível (oculta)"))
        return out


def _to_float(value) -> float | None:
    try:
        if isinstance(value, tuple) and len(value) == 2:
            return float(value[0]) / float(value[1]) if value[1] else None
        return float(value)
    except Exception:  # noqa: BLE001
        return None


def _clean(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        value = value.decode("utf-8", "ignore")
    return str(value).replace("\x00", "").strip()


def _shutter_text(exposure) -> str:
    val = _to_float(exposure)
    if val is None or val <= 0:
        return ""
    if val >= 1:
        return f"{val:g} s"
    frac = Fraction(val).limit_denominator(16000)
    return f"1/{round(1 / val)} s" if frac.numerator == 1 or val < 0.5 else f"{val:g} s"


def _parse_datetime(text: str) -> str:
    text = _clean(text)
    if not text:
        return ""
    for fmt in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y:%m:%d %H:%M", "%Y-%m-%dT%H:%M:%S"):
        try:
            return _dt.datetime.strptime(text[:19], fmt).isoformat(timespec="seconds")
        except ValueError:
            continue
    return ""


def _gps_decimal(coord, ref: str) -> float | None:
    try:
        deg, minute, sec = (_to_float(c) or 0.0 for c in coord)
        value = deg + minute / 60.0 + sec / 3600.0
        if ref and ref.upper() in ("S", "W"):
            value = -value
        return round(value, 7)
    except Exception:  # noqa: BLE001
        return None


def read_exif(path: str | Path, image: Image.Image | None = None) -> ExifData:
    """Extrai o EXIF de um arquivo (ou de uma imagem PIL já aberta)."""
    data = ExifData()
    try:
        img = image if image is not None else Image.open(path)
        exif = img.getexif()
        if not exif:
            if image is None:
                img.close()
            return data
        raw: dict[str, object] = {}
        merged: dict[int, object] = dict(exif)
        for ifd_name in ("Exif", "GPSInfo"):
            tag_id = _TAGS.get(ifd_name) or (34665 if ifd_name == "Exif" else 34853)
            try:
                sub = exif.get_ifd(tag_id)
            except Exception:  # noqa: BLE001
                sub = None
            if sub:
                if ifd_name == "GPSInfo":
                    gps = {ExifTags.GPSTAGS.get(k, str(k)): v for k, v in sub.items()}
                    lat = gps.get("GPSLatitude")
                    lon = gps.get("GPSLongitude")
                    if lat and lon:
                        data.gps_lat = _gps_decimal(lat, _clean(gps.get("GPSLatitudeRef", "")))
                        data.gps_lon = _gps_decimal(lon, _clean(gps.get("GPSLongitudeRef", "")))
                else:
                    merged.update(sub)

        for tag_id, value in merged.items():
            name = ExifTags.TAGS.get(tag_id, str(tag_id))
            if isinstance(value, bytes):
                if len(value) > 128:
                    continue
                value = value.decode("utf-8", "ignore").replace("\x00", "")
            if isinstance(value, (int, float, str)):
                raw[name] = value
            elif isinstance(value, tuple) and len(value) <= 4:
                raw[name] = [str(v) for v in value]

        data.present = True
        data.camera_make = _clean(merged.get(_TAGS.get("Make")))
        data.camera_model = _clean(merged.get(_TAGS.get("Model")))
        data.lens = _clean(merged.get(_TAGS.get("LensModel"))) or _clean(merged.get(_TAGS.get("LensMake")))
        iso = merged.get(_TAGS.get("ISOSpeedRatings")) or merged.get(_TAGS.get("PhotographicSensitivity"))
        iso_val = _to_float(iso[0] if isinstance(iso, (tuple, list)) and iso else iso)
        data.iso = int(iso_val) if iso_val else None
        data.aperture = _to_float(merged.get(_TAGS.get("FNumber")))
        data.shutter = _shutter_text(merged.get(_TAGS.get("ExposureTime")))
        data.focal_length = _to_float(merged.get(_TAGS.get("FocalLength")))
        data.taken_at = _parse_datetime(merged.get(_TAGS.get("DateTimeOriginal")) or "") or _parse_datetime(
            merged.get(_TAGS.get("DateTime")) or ""
        )
        data.digitized_at = _parse_datetime(merged.get(_TAGS.get("DateTimeDigitized")) or "")
        data.subsec = _clean(merged.get(_TAGS.get("SubsecTimeOriginal")))
        data.software = _clean(merged.get(_TAGS.get("Software")))
        orient = merged.get(_TAGS.get("Orientation"))
        data.orientation = int(orient) if isinstance(orient, int) else None
        data.artist = _clean(merged.get(_TAGS.get("Artist")))
        data.copyright = _clean(merged.get(_TAGS.get("Copyright")))
        cs = merged.get(_TAGS.get("ColorSpace"))
        data.color_space = {1: "sRGB", 65535: "Adobe RGB / não calibrado"}.get(cs, "") if cs else ""
        w = _to_float(merged.get(_TAGS.get("ExifImageWidth")))
        h = _to_float(merged.get(_TAGS.get("ExifImageHeight")))
        data.exif_image_width = int(w) if w else None
        data.exif_image_height = int(h) if h else None
        data.raw = raw
        if image is None:
            img.close()
    except Exception as exc:  # noqa: BLE001 - EXIF corrompido nunca derruba a análise
        log.debug("EXIF indisponível para %s: %s", path, exc)
    return data


def same_capture_moment(a: ExifData, b: ExifData) -> bool | None:
    """Compara o instante de captura de duas fotos.

    Retorna ``True`` (mesma captura), ``False`` (capturas distintas) ou ``None``
    quando a informação não existe em pelo menos uma delas.
    """
    ka, kb = a.capture_key(), b.capture_key()
    if not ka or not kb:
        return None
    return ka == kb
