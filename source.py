"""Получение и распознавание графика погодинных отключений (ГПВ) «Волиньобленерго».

Сайт energy.volyn.ua закрыт защитой Cloudflare, но картинку графика он берёт
из открытого API api-voe-poweron.inneti.net:
  /api/archive_gpv_graphs?page=1  — архив опубликованных картинок (дата графика + время загрузки)
  /media/YYYY/MM/xxx_GPV.png      — сама картинка 1750×2250: две таблицы
                                    (00:00–12:00 и 12:00–24:00), 12 строк-подочередей
                                    (1.1 … 6.2) × 24 получасовых ячейки.
Цвет ячейки: светлый — свет есть, тёмный — отключение.
"""
from __future__ import annotations

import io
import json
from dataclasses import dataclass, field
from datetime import date, datetime
from urllib.request import Request, urlopen

from PIL import Image

API = "https://api-voe-poweron.inneti.net"
SITE_PAGE = "https://energy.volyn.ua/spozhyvacham/perervy-u-elektropostachanni/hrafik-vidkliuchen/"
ADDRESS_PAGE = "https://energy.volyn.ua/spozhyvacham/perervy-u-elektropostachanni/cherga/"
USER_AGENT = "VolynLightBot/1.0 (+telegram bot)"

QUEUES = [f"{g}.{s}" for g in range(1, 7) for s in (1, 2)]

# Шаблон картинки (проверен на графике от 25.11.2026)
IMG_SIZE = (1750, 2250)
ON = (185, 200, 215)      # есть электроснабжение
OFF = (68, 110, 155)      # отключение
WHITE = (255, 255, 255)   # линии сетки
TABLE_TOP = (419, 1309)   # y верхней границы первой строки каждой таблицы
ROW_H, COL_W, COL_X0 = 60, 65, 175
LEGEND_ON, LEGEND_OFF = (570, 2110), (920, 2110)
COLOR_TOLERANCE = 24

Span = tuple[int, int]  # (начало, конец) в минутах от полуночи, конец до 1440


class SourceError(Exception):
    """Не удалось получить данные из API."""


class ParseError(Exception):
    """Картинка не совпала с известным шаблоном."""


@dataclass
class DaySchedule:
    date: str                      # 'YYYY-MM-DD'
    path: str                      # /media/... — меняется при каждой новой версии графика
    uploaded: str                  # время загрузки (ISO, UTC)
    image: bytes = b""
    queues: dict[str, list[Span]] | None = None   # None — не удалось распознать
    error: str = ""

    @property
    def url(self) -> str:
        return API + self.path


def http_get(url: str, accept: str | None = None, timeout: int = 30) -> bytes:
    headers = {"User-Agent": USER_AGENT}
    if accept:
        headers["Accept"] = accept
    try:
        with urlopen(Request(url, headers=headers), timeout=timeout) as r:
            data = r.read(15_000_001)
    except Exception as e:  # сеть, HTTP-ошибки, таймауты
        raise SourceError(f"{url}: {e}") from e
    if len(data) > 15_000_000:
        raise SourceError(f"{url}: завелика відповідь")
    return data


def fetch_archive(get=http_get) -> list[dict]:
    raw = get(API + "/api/archive_gpv_graphs?page=1", accept="application/ld+json")
    try:
        members = json.loads(raw)["hydra:member"]
    except Exception as e:
        raise SourceError(f"неочікувана відповідь архіву: {e}") from e
    if not isinstance(members, list):
        raise SourceError("архів: очікувався список")
    return members


def latest_per_date(records: list[dict], from_date: str) -> dict[str, dict]:
    """Самая свежая картинка для каждой даты графика начиная с from_date."""
    best: dict[str, dict] = {}
    for r in records:
        d = (r.get("dateGraph") or "")[:10]
        img = r.get("imageUrl") or {}
        path = img.get("contentUrl") or ""
        if not d or d < from_date or not path.startswith("/media/") or ".." in path:
            continue
        uploaded = img.get("uploadTimestamp") or r.get("dateCreate") or ""
        key = (uploaded, r.get("id") or 0)
        if d not in best or key > best[d]["_key"]:
            best[d] = {"path": path, "uploaded": uploaded, "_key": key}
    return {d: {"path": v["path"], "uploaded": v["uploaded"]} for d, v in sorted(best.items())}


def _close(a, b) -> bool:
    return all(abs(x - y) <= COLOR_TOLERANCE for x, y in zip(a, b))


def to_spans(slots: list[bool]) -> list[Span]:
    spans, start = [], None
    for i, off in enumerate(slots + [False]):
        if off and start is None:
            start = i * 30
        elif not off and start is not None:
            spans.append((start, i * 30))
            start = None
    return spans


def parse_png(data: bytes) -> dict[str, list[Span]]:
    """Картинка → {'1.1': [(480, 660), ...], ...}. При любом несовпадении — ParseError."""
    try:
        im = Image.open(io.BytesIO(data)).convert("RGB")
    except Exception as e:
        raise ParseError(f"не картинка: {e}") from e
    if im.size != IMG_SIZE:
        raise ParseError(f"невідомий розмір картинки {im.size}")
    px = im.getpixel
    if not _close(px(LEGEND_ON), ON) or not _close(px(LEGEND_OFF), OFF):
        raise ParseError("легенда не збігається з шаблоном")

    slots = {q: [] for q in QUEUES}
    for top in TABLE_TOP:
        for row, q in enumerate(QUEUES):
            y = top + row * ROW_H + ROW_H // 2
            for col in range(24):
                x = COL_X0 + col * COL_W + COL_W // 2
                if not _close(px((x, top + row * ROW_H - 1)), WHITE) or not _close(px((COL_X0 + col * COL_W - 1, y)), WHITE):
                    raise ParseError("сітка таблиці змістилася")
                samples = [px((x + dx, y + dy)) for dx, dy in ((0, 0), (-10, -10), (10, 10), (-10, 10), (10, -10))]
                if all(_close(s, OFF) for s in samples):
                    slots[q].append(True)
                elif all(_close(s, ON) for s in samples):
                    slots[q].append(False)
                else:
                    raise ParseError(f"невідомий колір клітинки (підчерга {q}, стовпець {col + 1})")
    return {q: to_spans(s) for q, s in slots.items()}


def fetch_schedules(today: date, get=http_get) -> list[DaySchedule]:
    """Актуальные графики на сегодня и следующие дни (обычно сегодня и завтра)."""
    result = []
    for d, info in latest_per_date(fetch_archive(get), today.isoformat()).items():
        day = DaySchedule(date=d, path=info["path"], uploaded=info["uploaded"])
        day.image = get(API + info["path"])
        try:
            day.queues = parse_png(day.image)
        except ParseError as e:
            day.error = str(e)
        result.append(day)
    return result


def fmt_time(m: int) -> str:
    return f"{m // 60:02d}:{m % 60:02d}"


def uploaded_local(uploaded: str, tz) -> str:
    try:
        return datetime.fromisoformat(uploaded).astimezone(tz).strftime("%d.%m %H:%M")
    except Exception:
        return ""
