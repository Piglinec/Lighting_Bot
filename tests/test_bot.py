import io
import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import bot  # noqa: E402
import source  # noqa: E402
from source import QUEUES, DaySchedule  # noqa: E402

# Реальный график на 25.11.2026 (как считан с картинки на сайте): '#' — отключение
REAL_ROWS = [
    "................######..", "................######..", "........................", "........................",
    "........................", "........................", "........................", "........................",
    "........................", "........................", "........................", "........................",
    "........................", "........................", "......................##", "...............##.......",
    "...............#######..", "........................", "........#######.........", "........#######.........",
    "........................", "........................", "........................", "........................",
]


def make_png(rows=REAL_ROWS, size=source.IMG_SIZE, legend_off=source.OFF) -> bytes:
    if size != source.IMG_SIZE:
        buf = io.BytesIO()
        Image.new("RGB", size, source.WHITE).save(buf, "PNG")
        return buf.getvalue()
    im = Image.new("RGB", size, source.WHITE)
    px = im.load()
    for t, top in enumerate(source.TABLE_TOP):
        for r in range(12):
            for c in range(24):
                color = source.OFF if rows[t * 12 + r][c] == "#" else source.ON
                x0, y0 = source.COL_X0 + c * source.COL_W, top + r * source.ROW_H
                for x in range(x0 + 1, x0 + source.COL_W - 1):
                    for y in range(y0 + 1, y0 + source.ROW_H - 1):
                        px[x, y] = color
    for (x, y), color in ((source.LEGEND_ON, source.ON), (source.LEGEND_OFF, legend_off)):
        for dx in range(-5, 6):
            for dy in range(-5, 6):
                px[x + dx, y + dy] = color
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


class FakeTG:
    def __init__(self):
        self.sent = []

    def call(self, method, timeout=30, **p):
        if method == "sendMessage":
            self.sent.append((str(p["chat_id"]), p["text"]))
        return {}

    def send_photo(self, chat_id, photo, caption, **p):
        assert photo.startswith(b"\x89PNG")
        self.sent.append((str(chat_id), "[фото] " + caption))
        return {}


class ParserTest(unittest.TestCase):
    def test_real_schedule(self):
        q = source.parse_png(make_png())
        self.assertEqual(q["1.1"], [(480, 660)])          # 08:00–11:00
        self.assertEqual(q["2.1"], [(1380, 1440)])        # 23:00–24:00
        self.assertEqual(q["2.2"], [(1170, 1230)])        # 19:30–20:30
        self.assertEqual(q["3.1"], [(1170, 1380)])        # 19:30–23:00
        self.assertEqual(q["4.1"], q["4.2"])
        self.assertEqual(q["4.1"], [(960, 1170)])         # 16:00–19:30
        self.assertEqual(q["6.2"], [])

    def test_crossing_noon_is_one_span(self):
        rows = ["." * 22 + "##"] + ["." * 24] * 11 + ["##" + "." * 22] + ["." * 24] * 11
        self.assertEqual(source.parse_png(make_png(rows))["1.1"], [(660, 780)])

    def test_unknown_template_rejected(self):
        with self.assertRaises(source.ParseError):
            source.parse_png(make_png(size=(1700, 2250)))
        with self.assertRaises(source.ParseError):
            source.parse_png(make_png(legend_off=(200, 0, 0)))
        with self.assertRaises(source.ParseError):
            source.parse_png(b"not an image")

    def test_latest_per_date(self):
        recs = [
            {"id": 1, "dateGraph": "2026-11-25T00:00:00+00:00",
             "imageUrl": {"contentUrl": "/media/a.png", "uploadTimestamp": "2026-11-25T10:00:00+00:00"}},
            {"id": 2, "dateGraph": "2026-11-25T00:00:00+00:00",
             "imageUrl": {"contentUrl": "/media/b.png", "uploadTimestamp": "2026-11-25T13:40:25+00:00"}},
            {"id": 3, "dateGraph": "2026-11-24T00:00:00+00:00",
             "imageUrl": {"contentUrl": "/media/old.png", "uploadTimestamp": "2026-11-24T13:00:00+00:00"}},
            {"id": 4, "dateGraph": "2026-11-26T00:00:00+00:00",
             "imageUrl": {"contentUrl": "/../evil", "uploadTimestamp": "2026-11-25T20:00:00+00:00"}},
        ]
        self.assertEqual(source.latest_per_date(recs, "2026-11-25"),
                         {"2026-11-25": {"path": "/media/b.png", "uploaded": "2026-11-25T13:40:25+00:00"}})

    def test_fetch_schedules_with_fake_http(self):
        png = make_png()
        archive = {"hydra:member": [{"id": 9, "dateGraph": "2026-11-25T00:00:00+00:00",
                                     "imageUrl": {"contentUrl": "/media/x.png", "uploadTimestamp": "2026-11-25T13:40:25+00:00"}}]}

        def get(url, accept=None):
            return json.dumps(archive).encode() if "archive" in url else png

        days = source.fetch_schedules(datetime(2026, 11, 25).date(), get=get)
        self.assertEqual(len(days), 1)
        self.assertEqual(days[0].queues["1.1"], [(480, 660)])


class BotFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        cfg = bot.Config.__new__(bot.Config)
        cfg.token, cfg.interval, cfg.remind, cfg.send_image = "x", 5, 30, True
        cfg.default_queue, cfg.admin, cfg.allowed = "", "", set()
        cfg.state_file = Path(self.tmp.name) / "state.json"
        self.clock = datetime(2026, 11, 25, 7, 0, tzinfo=bot.TZ)
        self.days = []
        self.tg = FakeTG()
        self.bot = bot.Bot(cfg, self.tg, fetch=lambda today: self.days, now=lambda: self.clock)

    def tearDown(self):
        self.tmp.cleanup()

    def day(self, path, rows=REAL_ROWS):
        png = make_png(rows)
        return DaySchedule("2026-11-25", path, "2026-11-25T13:40:25+00:00", png, source.parse_png(png))

    def msg(self, text, chat=111):
        self.bot.handle({"update_id": 1, "message": {"chat": {"id": chat}, "text": text}})

    def test_full_flow(self):
        self.msg("/start")
        self.assertIn("Выберите свою подочередь", self.tg.sent[-1][1])
        self.bot.handle({"update_id": 2, "callback_query": {"id": "c", "data": "q:1.1", "message": {"chat": {"id": 111}}}})
        self.msg("/start 3.1", chat=222)
        self.tg.sent.clear()

        # 1) график опубликован
        self.days = [self.day("/media/v1.png")]
        self.bot.check()
        texts = dict(self.tg.sent)
        self.assertIn("Опубликован график", texts["111"])
        self.assertIn("08:00–11:00", texts["111"])
        self.assertIn("19:30–23:00", texts["222"])
        self.assertIn("15:40", texts["111"])          # время загрузки в киевском времени
        self.assertTrue(texts["111"].startswith("[фото]"))

        # 2) та же версия — тишина
        self.tg.sent.clear()
        self.bot.check()
        self.assertEqual(self.tg.sent, [])

        # 3) изменилась только 1.1 — пишем только подписчику 1.1, с прежними часами
        rows = list(REAL_ROWS)
        rows[0] = "................########"
        self.days = [self.day("/media/v2.png", rows)]
        self.bot.check()
        self.assertEqual([c for c, _ in self.tg.sent], ["111"])
        self.assertIn("График изменён", self.tg.sent[0][1])
        self.assertIn("08:00–12:00", self.tg.sent[0][1])
        self.assertIn("Было:", self.tg.sent[0][1])
        self.assertIn("08:00–11:00", self.tg.sent[0][1])

        # 4) напоминание за 30 минут — один раз
        self.tg.sent.clear()
        self.clock = datetime(2026, 11, 25, 7, 40, tzinfo=bot.TZ)
        self.bot.reminders()
        self.bot.reminders()
        self.assertEqual(len(self.tg.sent), 1)
        self.assertIn("Через ~20 мин", self.tg.sent[0][1])

        # 5) /now, /remind, /stop; состояние переживает перезапуск
        self.tg.sent.clear()
        self.msg("/now", chat=222)
        self.assertIn("Текущий график", self.tg.sent[-1][1])
        self.msg("/remind 0", chat=222)
        self.assertEqual(self.bot.state["chats"]["222"]["remind"], 0)
        self.msg("/stop", chat=222)
        self.assertNotIn("222", self.bot.state["chats"])
        again = bot.Bot(self.bot.cfg, self.tg, fetch=lambda t: self.days, now=lambda: self.clock)
        self.assertEqual(again.state["chats"]["111"]["queue"], "1.1")
        self.assertEqual(again.state["days"]["2026-11-25"]["path"], "/media/v2.png")

    def test_unrecognized_image_is_forwarded(self):
        self.msg("/start 2.1")
        self.tg.sent.clear()
        self.days = [DaySchedule("2026-11-25", "/media/ad.png", "", make_png(size=(1000, 1000)), None, "неизвестный размер")]
        self.bot.check()
        self.assertIn("Не удалось распознать", self.tg.sent[0][1])

    def test_run_once(self):
        updates = [{"update_id": 41, "message": {"chat": {"id": 333}, "text": "/start 5.1"}}]
        tg = self.tg
        orig = tg.call

        def call(method, timeout=30, **p):
            if method == "getUpdates":
                return [u for u in updates if u["update_id"] >= p.get("offset", 0)]
            return orig(method, timeout, **p)

        tg.call = call
        self.days = [self.day("/media/v1.png")]
        self.bot.run_once()
        self.assertEqual(self.bot.state["chats"]["333"]["queue"], "5.1")
        self.assertEqual(self.bot.state["offset"], 42)
        saved = json.loads(self.bot.cfg.state_file.read_text())
        self.assertEqual(saved["offset"], 42)
        self.assertIn("2026-11-25", saved["days"])
        n = len(tg.sent)
        again = bot.Bot(self.bot.cfg, tg, fetch=lambda t: self.days, now=lambda: self.clock)
        again.run_once()                       # та же команда второй раз не обрабатывается
        self.assertEqual(len(tg.sent), n)

    def test_private_mode(self):
        self.bot.cfg.allowed = {"111"}
        self.msg("/start", chat=999)
        self.assertIn("Доступ закрыт", self.tg.sent[-1][1])


if __name__ == "__main__":
    unittest.main()
