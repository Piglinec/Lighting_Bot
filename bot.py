"""Telegram-бот: график отключений света «Волиньобленерго» (Волынская область).

Каждые CHECK_INTERVAL_MIN минут проверяет график и пишет подписчикам:
  • когда опубликован график на новый день (с часами отключений для их подочереди);
  • когда график изменился (новые и прежние часы);
  • за REMIND_BEFORE_MIN минут до начала отключения.

Запуск:  python bot.py           (настройки берутся из .env рядом с файлом или из переменных окружения)
Проверка без Telegram:  python bot.py --dry-run
Один проход и выход (для GitHub Actions / cron):  python bot.py --once
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

import source
from source import QUEUES, DaySchedule, SourceError, fmt_time

TZ = ZoneInfo("Europe/Kyiv")
BASE_DIR = Path(__file__).resolve().parent
log = logging.getLogger("volyn-bot")

WEEKDAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]


# ───────────────────────── настройки ─────────────────────────

def load_env(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


class Config:
    def __init__(self) -> None:
        self.token = os.environ.get("BOT_TOKEN", "").strip()
        self.interval = max(1, int(os.environ.get("CHECK_INTERVAL_MIN", "5")))
        self.remind = max(0, int(os.environ.get("REMIND_BEFORE_MIN", "30")))
        self.send_image = os.environ.get("SEND_IMAGE", "1") not in ("0", "false", "no")
        self.default_queue = os.environ.get("DEFAULT_QUEUE", "").strip()
        self.admin = os.environ.get("ADMIN_CHAT_ID", "").strip()
        allowed = os.environ.get("ALLOWED_CHAT_IDS", "").replace(" ", "")
        self.allowed = {x for x in allowed.split(",") if x}
        self.state_file = Path(os.environ.get("STATE_FILE", str(BASE_DIR / "state.json")))
        if self.default_queue and self.default_queue not in QUEUES:
            raise SystemExit(f"DEFAULT_QUEUE должен быть одним из: {', '.join(QUEUES)}")


# ───────────────────────── Telegram API ─────────────────────────

class TelegramError(Exception):
    def __init__(self, code: int, description: str):
        super().__init__(f"{code}: {description}")
        self.code = code


class Telegram:
    def __init__(self, token: str):
        self.base = f"https://api.telegram.org/bot{token}/"

    def _request(self, method: str, data: bytes, content_type: str, timeout: int) -> dict:
        for attempt in range(3):
            req = Request(self.base + method, data=data, headers={"Content-Type": content_type})
            try:
                with urlopen(req, timeout=timeout) as r:
                    return json.load(r)["result"]
            except HTTPError as e:
                try:
                    body = json.load(e)
                except Exception:
                    body = {}
                retry = (body.get("parameters") or {}).get("retry_after")
                if e.code == 429 and retry and attempt < 2:
                    time.sleep(int(retry) + 1)
                    continue
                raise TelegramError(e.code, body.get("description", str(e))) from None
            except OSError as e:
                if attempt < 2:
                    time.sleep(3)
                    continue
                # не выводим URL: в нём токен
                raise TelegramError(0, f"сеть: {type(e).__name__}") from None
        raise TelegramError(0, "не удалось выполнить запрос")

    def call(self, method: str, timeout: int = 30, **params) -> dict:
        data = json.dumps(params).encode()
        return self._request(method, data, "application/json", timeout)

    def send_photo(self, chat_id, photo: bytes, caption: str, **params) -> dict:
        boundary = uuid.uuid4().hex
        fields = {"chat_id": str(chat_id), "caption": caption, "parse_mode": "HTML", **params}
        parts = []
        for k, v in fields.items():
            if isinstance(v, (dict, list)):
                v = json.dumps(v)
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="photo"; filename="gpv.png"\r\n'
            f"Content-Type: image/png\r\n\r\n".encode() + photo + b"\r\n"
        )
        parts.append(f"--{boundary}--\r\n".encode())
        return self._request("sendPhoto", b"".join(parts), f"multipart/form-data; boundary={boundary}", 60)


# ───────────────────────── тексты ─────────────────────────

def fmt_date(d: str) -> str:
    x = date.fromisoformat(d)
    return f"{x:%d.%m} ({WEEKDAYS[x.weekday()]})"


def hours(spans) -> str:
    total = sum(b - a for a, b in spans) / 60
    return f"{total:g} ч"


def describe(spans) -> str:
    if not spans:
        return "отключений нет ✅"
    return "\n".join(f"🔴 {fmt_time(a)}–{fmt_time(b)}" for a, b in spans) + f"\nВсего без света: {hours(spans)}"


def day_text(day: dict, queue: str, title: str, previous=None) -> str:
    spans = day["queues"][queue]
    lines = [f"⚡ <b>{title}</b>", f"Подочередь <b>{queue}</b> · {fmt_date(day['date'])}", "", describe(spans)]
    if previous is not None:
        lines += ["", "<i>Было:</i>", describe(previous)]
    stamp = source.uploaded_local(day.get("uploaded", ""), TZ)
    if stamp:
        lines += ["", f"<i>График по состоянию на {stamp}</i>"]
    return "\n".join(lines)


HELP = (
    "Я слежу за графиком отключений «Волиньобленерго» и пишу:\n"
    "• когда опубликован график на новый день;\n"
    "• когда график изменился;\n"
    "• незадолго до начала отключения.\n\n"
    "/queue — выбрать подочередь\n"
    "/now — график на сегодня и завтра\n"
    "/remind 30 — напоминать за 30 мин (0 — выключить)\n"
    "/stop — отписаться\n\n"
    f"Узнать подочередь по адресу: {source.ADDRESS_PAGE}"
)


def queue_keyboard() -> dict:
    rows = [[{"text": f"{g}.{s}", "callback_data": f"q:{g}.{s}"} for s in (1, 2)] for g in range(1, 7)]
    return {"inline_keyboard": rows}


# ───────────────────────── бот ─────────────────────────

class Bot:
    def __init__(self, cfg: Config, tg: Telegram | None, fetch=source.fetch_schedules, now=lambda: datetime.now(TZ)):
        self.cfg, self.tg, self.fetch, self.now = cfg, tg, fetch, now
        self.state = self._load()
        self.offset = self.state.get("offset", 0)
        self.next_check = 0.0

    # ---- состояние ----
    def _load(self) -> dict:
        try:
            st = json.loads(self.cfg.state_file.read_text(encoding="utf-8"))
        except FileNotFoundError:
            st = {}
        st.setdefault("chats", {})
        st.setdefault("days", {})
        st.setdefault("reminded", [])
        if self.cfg.admin and self.cfg.default_queue:
            st["chats"].setdefault(self.cfg.admin, {"queue": self.cfg.default_queue, "remind": self.cfg.remind})
        return st

    def save(self) -> None:
        tmp = self.cfg.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.cfg.state_file)

    # ---- отправка ----
    def send(self, chat_id, text: str, **extra) -> None:
        try:
            self.tg.call("sendMessage", chat_id=chat_id, text=text, parse_mode="HTML",
                         link_preview_options={"is_disabled": True}, **extra)
        except TelegramError as e:
            self._on_send_error(chat_id, e)

    def send_photo(self, chat_id, image: bytes, caption: str) -> None:
        try:
            self.tg.send_photo(chat_id, image, caption)
        except TelegramError as e:
            if e.code in (400, 413):          # не прошла картинка — шлём текстом
                self.send(chat_id, caption)
            else:
                self._on_send_error(chat_id, e)

    def _on_send_error(self, chat_id, e: TelegramError) -> None:
        log.warning("не удалось отправить в %s: %s", chat_id, e)
        if e.code == 403:                      # пользователь заблокировал бота
            self.state["chats"].pop(str(chat_id), None)

    def notify(self, chat_id, text: str, image: bytes | None) -> None:
        if image and self.cfg.send_image and len(text) <= 1024:
            self.send_photo(chat_id, image, text)
        else:
            self.send(chat_id, text)

    # ---- проверка графика ----
    def check(self) -> None:
        today = self.now().date()
        try:
            days = self.fetch(today)
        except SourceError as e:
            log.warning("источник недоступен: %s", e)
            self._source_problem(str(e))
            return
        if self.state.pop("source_down_since", None) and self.state.pop("source_alerted", False) and self.cfg.admin:
            self.send(self.cfg.admin, "✅ Получение графика восстановлено.")

        known = self.state["days"]
        for d in days:
            prev = known.get(d.date)
            if prev and prev.get("path") == d.path:
                continue                        # эта версия графика уже обработана
            log.info("новая версия графика на %s: %s (%s)", d.date, d.path, "ok" if d.queues else d.error)
            self._announce(d, prev)
            known[d.date] = {"date": d.date, "path": d.path, "uploaded": d.uploaded,
                             "queues": d.queues, "error": d.error}
        cutoff = (today - timedelta(days=2)).isoformat()
        self.state["days"] = {k: v for k, v in known.items() if k >= cutoff}
        self.state["reminded"] = [k for k in self.state["reminded"] if k.split("|")[1] >= cutoff]
        self.save()

    def _announce(self, d: DaySchedule, prev: dict | None) -> None:
        day = {"date": d.date, "queues": d.queues, "uploaded": d.uploaded}
        prev_queues = prev.get("queues") if prev else None
        for chat_id, sub in list(self.state["chats"].items()):
            q = sub.get("queue")
            if not q:
                continue
            if d.queues is None:
                text = (f"⚡ <b>{'Обновлён' if prev else 'Опубликован'} график на {fmt_date(d.date)}</b>\n"
                        f"Не удалось распознать картинку автоматически ({d.error}) — посмотрите сами.\n"
                        f"{d.url}")
                if self.cfg.admin and chat_id == self.cfg.admin:
                    text += "\n\n<i>Похоже, сменился шаблон картинки — нужно обновить парсер.</i>"
                self.notify(chat_id, text, d.image)
                continue
            new = d.queues[q]
            if prev_queues is None:
                self.notify(chat_id, day_text(day, q, "Опубликован график"), d.image)
            elif [list(s) for s in prev_queues.get(q, [])] != [list(s) for s in new]:
                self.notify(chat_id, day_text(day, q, "График изменён", previous=prev_queues.get(q, [])), d.image)
            # если изменились только другие подочереди — молчим

    def _source_problem(self, err: str) -> None:
        now = time.time()
        since = self.state.setdefault("source_down_since", now)
        if self.cfg.admin and not self.state.get("source_alerted") and now - since >= 3600:
            self.send(self.cfg.admin, f"⚠️ Больше часа не удаётся получить график.\n<code>{err[:300]}</code>")
            self.state["source_alerted"] = True
        self.save()

    # ---- напоминания ----
    def reminders(self) -> None:
        now = self.now()
        changed = False
        for chat_id, sub in list(self.state["chats"].items()):
            q, before = sub.get("queue"), sub.get("remind", self.cfg.remind)
            if not q or not before:
                continue
            for d, day in self.state["days"].items():
                for a, b in (day.get("queues") or {}).get(q, []):
                    if a == 0 and self._continues_from_previous_day(d, q):
                        continue                # отключение просто продолжается после полуночи
                    start = datetime.fromisoformat(d).replace(tzinfo=TZ) + timedelta(minutes=a)
                    left = (start - now).total_seconds() / 60
                    key = f"{chat_id}|{d}|{a}"
                    if 0 < left <= before and key not in self.state["reminded"]:
                        self.send(chat_id, f"⏰ Через ~{round(left)} мин отключение света\n"
                                           f"Подочередь {q}: {fmt_time(a)}–{fmt_time(b)}")
                        self.state["reminded"].append(key)
                        changed = True
        if changed:
            self.save()

    def _continues_from_previous_day(self, d: str, q: str) -> bool:
        prev = (date.fromisoformat(d) - timedelta(days=1)).isoformat()
        spans = (self.state["days"].get(prev, {}).get("queues") or {}).get(q, [])
        return bool(spans) and spans[-1][1] == 1440

    # ---- команды ----
    def allowed(self, chat_id: str) -> bool:
        return not self.cfg.allowed or chat_id in self.cfg.allowed or chat_id == self.cfg.admin

    def handle(self, upd: dict) -> None:
        if "callback_query" in upd:
            cq = upd["callback_query"]
            chat_id = str(cq["message"]["chat"]["id"])
            try:
                self.tg.call("answerCallbackQuery", callback_query_id=cq["id"])
            except TelegramError:
                pass
            data = cq.get("data", "")
            if self.allowed(chat_id) and data.startswith("q:") and data[2:] in QUEUES:
                self.set_queue(chat_id, data[2:])
            return
        msg = upd.get("message") or {}
        text = (msg.get("text") or "").strip()
        if not text.startswith("/"):
            return
        chat_id = str(msg["chat"]["id"])
        if not self.allowed(chat_id):
            self.send(chat_id, "Это личный бот. Доступ закрыт.")
            return
        cmd, _, arg = text.partition(" ")
        cmd = cmd.split("@")[0].lower()
        sub = self.state["chats"].get(chat_id)
        if cmd in ("/start", "/queue"):
            if cmd == "/start":
                self.send(chat_id, HELP)
            if arg.strip() in QUEUES:
                self.set_queue(chat_id, arg.strip())
            else:
                self.send(chat_id, "Выберите свою подочередь:", reply_markup=queue_keyboard())
        elif cmd == "/help":
            self.send(chat_id, HELP)
        elif cmd in ("/now", "/grafik"):
            if not sub:
                self.send(chat_id, "Сначала выберите подочередь:", reply_markup=queue_keyboard())
            else:
                self.show_current(chat_id, sub["queue"])
        elif cmd == "/remind":
            if not sub:
                self.send(chat_id, "Сначала выберите подочередь: /queue")
            elif arg.strip().isdigit() and int(arg) <= 600:
                sub["remind"] = int(arg)
                self.save()
                self.send(chat_id, f"Буду напоминать за {int(arg)} мин до отключения." if int(arg) else "Напоминания выключены.")
            else:
                self.send(chat_id, f"Сейчас: {sub.get('remind', self.cfg.remind)} мин. Пример: /remind 15 (0 — выключить)")
        elif cmd == "/stop":
            self.state["chats"].pop(chat_id, None)
            self.save()
            self.send(chat_id, "Вы отписались. Вернуться — /start")
        else:
            self.send(chat_id, HELP)

    def set_queue(self, chat_id: str, q: str) -> None:
        sub = self.state["chats"].setdefault(chat_id, {"remind": self.cfg.remind})
        sub["queue"] = q
        self.save()
        self.send(chat_id, f"✅ Подочередь <b>{q}</b>. Буду сообщать об отключениях и изменениях графика.")
        self.show_current(chat_id, q)

    def show_current(self, chat_id: str, q: str) -> None:
        today = self.now().date().isoformat()
        days = [d for k, d in sorted(self.state["days"].items()) if k >= today]
        if not days:
            self.send(chat_id, f"Графика на сегодня пока нет. Проверяю каждые {self.cfg.interval} мин.")
            return
        for d in days:
            if d.get("queues"):
                self.send(chat_id, day_text(d, q, "Текущий график"))
            else:
                self.send(chat_id, f"График на {fmt_date(d['date'])} не распознан автоматически: {source.API}{d['path']}")
        if all(k <= today for k in self.state["days"]) and self.now().hour >= 18:
            self.send(chat_id, "График на завтра ещё не опубликован.")

    # ---- главный цикл ----
    def run_once(self) -> None:
        """Один проход: ответить на накопившиеся команды, проверить график, разослать напоминания."""
        try:
            updates = self.tg.call("getUpdates", offset=self.offset, timeout=0,
                                   allowed_updates=["message", "callback_query"])
        except TelegramError as e:
            log.warning("getUpdates: %s", e)
            updates = []
        for upd in updates:
            self.offset = upd["update_id"] + 1
            try:
                self.handle(upd)
            except Exception:
                log.exception("ошибка обработки сообщения")
        if updates:
            # подтверждаем получение, чтобы Telegram не прислал эти сообщения снова
            self.tg.call("getUpdates", offset=self.offset, timeout=0)
        self.state["offset"] = self.offset
        # раз в неделю состояние меняется само — в репозитории появляется коммит,
        # и GitHub не отключает расписание за 60 дней «неактивности»
        self.state["heartbeat"] = self.now().strftime("%G-W%V")
        self.check()
        self.reminders()
        self.save()

    def run(self) -> None:
        me = self.tg.call("getMe")
        log.info("бот @%s запущен, проверка каждые %s мин", me.get("username"), self.cfg.interval)
        self.tg.call("setMyCommands", commands=[
            {"command": "now", "description": "График на сегодня и завтра"},
            {"command": "queue", "description": "Выбрать подочередь"},
            {"command": "remind", "description": "Напоминание до отключения, мин"},
            {"command": "stop", "description": "Отписаться"},
        ])
        while True:
            if time.time() >= self.next_check:
                try:
                    self.check()
                except Exception:
                    log.exception("ошибка проверки")
                self.next_check = time.time() + self.cfg.interval * 60
            try:
                self.reminders()
            except Exception:
                log.exception("ошибка напоминаний")
            try:
                updates = self._poll()
            except TelegramError as e:
                log.warning("getUpdates: %s", e)
                time.sleep(5)
                continue
            for upd in updates:
                self.offset = upd["update_id"] + 1
                try:
                    self.handle(upd)
                except Exception:
                    log.exception("ошибка обработки сообщения")

    def _poll(self) -> list:
        """Long polling: ждём сообщения не дольше 25 с, чтобы вовремя слать напоминания и проверки."""
        wait = int(max(1, min(25, self.next_check - time.time())))
        data = json.dumps({"offset": self.offset, "timeout": wait,
                           "allowed_updates": ["message", "callback_query"]}).encode()
        return self.tg._request("getUpdates", data, "application/json", timeout=wait + 15)


# ───────────────────────── запуск ─────────────────────────

def dry_run() -> None:
    days = source.fetch_schedules(datetime.now(TZ).date())
    if not days:
        print("В архиве нет графиков на сегодня и позже.")
    for d in days:
        print(f"\n=== {d.date}  {d.url}")
        if d.queues is None:
            print("  не распознано:", d.error)
            continue
        for q in QUEUES:
            print(f"  {q}: " + (", ".join(f"{fmt_time(a)}–{fmt_time(b)}" for a, b in d.queues[q]) or "без отключений"))


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="показать текущий график и выйти (без Telegram)")
    parser.add_argument("--once", action="store_true", help="один проход и выход (для GitHub Actions / cron)")
    args = parser.parse_args()
    load_env(BASE_DIR / ".env")
    if args.dry_run:
        dry_run()
        return
    cfg = Config()
    if not cfg.token:
        sys.exit("Не задан BOT_TOKEN (в .env или переменных окружения).")
    b = Bot(cfg, Telegram(cfg.token))
    b.run_once() if args.once else b.run()


if __name__ == "__main__":
    main()
