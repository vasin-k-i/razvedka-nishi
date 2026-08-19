#!/usr/bin/env python3
"""Частотность фраз: единая точка для всех проектов (кэш → Wordstat → Arsenkin).

Зачем. Частотность нужна везде — SEO-заводы (гейт на спрос перед генерацией),
разбор ниш, разовые проверки руками. До этого модуля каждый проект ходил в Wordstat
через Yandex Cloud напрямую и упирался в квоту **100 запросов в ЧАС на аккаунт**
(общую на все проекты). 19.08.2026 на замере 503 страниц poehalitur это дало не просто
остановку, а ТИХУЮ ПОРЧУ ДАННЫХ: top_requests на исчерпанной квоте возвращает пустой
{}, ноль читался как «спроса нет», и 93% страниц ошибочно попали в «мёртвые» вместо
честных 40–48%.

Здесь три уровня, каждый закрывает дыру предыдущего:

1. **Глобальный кэш** `~/.seo-freq/cache.json` — общий на ВСЕ проекты. Одна и та же
   фраза, померенная заводом, больше не меряется заново разбором ниш. Дешевле любого API.
2. **Yandex Cloud Wordstat** — бесплатно в рамках облака, но 100 запросов/час и строго
   ПО ОДНОЙ фразе за вызов. Годится для десятков фраз, не для сотен.
3. **Arsenkin Tools** — платный добор, когда Wordstat кончился или фраз много.
   Берёт ПАЧКУ за одну задачу (проверено: 3 фразы = 1 задача = 3 юнита), поэтому
   на объёме он не «запасной», а прямо основной путь: 1 юнит/фразу ≈ 0.021–0.03 ₽.

Пустой ответ провайдера НИКОГДА не пишется в кэш как 0 — это «не замерено».

Использование в коде:
    from freq import frequency
    res = frequency(["озеро рица", "памятник мордюковой"])   # {фраза: {"freq": int, "source": str}}

CLI:
    python3 modules/freq.py "озеро рица" "хаджохская теснина"
    python3 modules/freq.py --in phrases.txt --out freqs.json
    python3 modules/freq.py --in phrases.txt --no-paid        # только кэш+Wordstat
    python3 modules/freq.py --stats                            # что в кэше и сколько потрачено

Ключи (значения НЕ здесь): YANDEX_CLOUD_API_KEY + YC_FOLDER_ID (Wordstat),
ARSENKIN_API_TOKEN (добор). Берутся из окружения, затем из .env проекта,
затем из общего стора ~/Projects/.secrets/.
"""
from __future__ import annotations

import argparse
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# ── глобальный кэш и журнал ──────────────────────────────────────────────────
HOME_DIR = Path.home() / ".seo-freq"
CACHE_PATH = HOME_DIR / "cache.json"
SPEND_PATH = HOME_DIR / "spend.json"

REGION_RU = 225
DEFAULT_WS = "base"          # базовая частотность (широкое соответствие)
ARSENKIN_BATCH = 300         # фраз за одну задачу; тот же гейт, что в arsenkin_api
ARSENKIN_DAILY_UNITS = 2000  # потолок юнитов в сутки на все проекты
WORDSTAT_THROTTLE = 0.25


def _load_env() -> None:
    """Ключи: окружение → .env проекта → общий стор ~/Projects/.secrets/."""
    here = Path(__file__).resolve()
    cands = [
        here.parents[1] / ".env",                       # seo-agent/.env
        here.parents[2] / ".env",                       # корень проекта
        Path.home() / "Projects" / ".secrets" / "yandex-cloud.env",
        Path.home() / "Projects" / ".secrets" / "arsenkin-sites.env",
    ]
    for c in cands:
        if not c.exists():
            continue
        for line in c.read_text().splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip().strip('"').strip("'")
                if v and not os.environ.get(k):
                    os.environ[k] = v


def normalize(phrase: str) -> str:
    """Привести фразу к виду, который принимают ОБА провайдера.

    ⚠️ Arsenkin отвечает 422 JSON_VALIDATION_ERROR на ЛЮБУЮ пунктуацию в фразе и роняет
    ЗАДАЧУ ЦЕЛИКОМ, а не одну строку: сначала поймали на кавычках-ёлочках
    («мемориальный комплекс «малая земля»»), потом на запятой («кабардинка на выходные,
    3 дня») — 180 фраз не замерились из-за одной. Поэтому подход белого списка:
    оставляем только буквы, цифры и пробел. Заодно уходят операторы Wordstat
    (" ! + [ ]), иначе одна тема кэшировалась бы под разными ключами.
    """
    import re as _re

    s = phrase.strip().lower()
    s = s.replace("ё", "е")  # Wordstat не различает, а кэш иначе двоится
    s = _re.sub(r"[^0-9a-zа-я\s-]", " ", s)   # белый список: буквы, цифры, пробел, дефис
    s = _re.sub(r"\s*-\s*", " ", s)           # дефис как разделитель: API всё равно его так и трактует
    s = _re.sub(r"\s+", " ", s).strip()
    return s


def _cache_key(phrase: str, region: int, ws: str) -> str:
    return f"{region}|{ws}|{normalize(phrase)}"


def load_cache() -> dict:
    if not CACHE_PATH.exists():
        return {}
    try:
        return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def save_cache(cache: dict) -> None:
    HOME_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CACHE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(cache, ensure_ascii=False, indent=1, sort_keys=True),
                   encoding="utf-8")
    tmp.replace(CACHE_PATH)  # атомарно: параллельные заводы не порвут файл


def _spend_today() -> dict:
    today = time.strftime("%Y-%m-%d")
    if SPEND_PATH.exists():
        try:
            d = json.loads(SPEND_PATH.read_text(encoding="utf-8"))
            if d.get("date") == today:
                return d
        except json.JSONDecodeError:
            pass
    return {"date": today, "arsenkin_units": 0, "wordstat_calls": 0}


def _record_spend(*, units: int = 0, calls: int = 0) -> dict:
    d = _spend_today()
    d["arsenkin_units"] += units
    d["wordstat_calls"] += calls
    HOME_DIR.mkdir(parents=True, exist_ok=True)
    SPEND_PATH.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    return d


# ── провайдер 1: Yandex Cloud Wordstat ───────────────────────────────────────
def _wordstat(phrases: list[str], region: int) -> dict[str, int]:
    """По одной фразе за вызов, 100 вызовов в час на аккаунт.

    ⚠️ Частотность берём из totalCount. НЕ сверять сид со строками results вручную:
    API нормализует фразу по-своему (дефис → пробел), из-за чего «переславль-залесский»
    показывал 0 вместо 698 233.
    ⚠️ Пустой ответ = квота/ошибка, а НЕ ноль. Останавливаемся, чтобы не портить кэш.
    """
    api_key = (os.environ.get("YANDEX_CLOUD_API_KEY") or "").strip()
    folder = (os.environ.get("YC_FOLDER_ID") or "").strip()
    if not api_key or not folder:
        return {}

    url = "https://searchapi.api.cloud.yandex.net/v2/wordstat/topRequests"
    ctx = ssl.create_default_context()
    out: dict[str, int] = {}
    for ph in phrases:
        body = json.dumps({"folderId": folder, "phrase": ph, "numPhrases": 5,
                           "regions": [str(region)]}, ensure_ascii=False).encode()
        req = urllib.request.Request(url, data=body, method="POST")
        req.add_header("Authorization", f"Api-Key {api_key}")
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=40, context=ctx) as r:
                resp = json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            if e.code == 429:
                print(f"  [wordstat] квота 100/час исчерпана на {len(out)} фразах "
                      f"→ остаток добираем Арсенкиным")
                break
            print(f"  [wordstat] HTTP {e.code} на «{ph}» — пропуск")
            continue
        except Exception as e:  # noqa: BLE001
            print(f"  [wordstat] сеть на «{ph}»: {e}")
            continue
        if not resp or "totalCount" not in resp:
            print(f"  [wordstat] пустой ответ на {len(out)} фразах — это НЕ ноль, останавливаюсь")
            break
        out[ph] = int(str(resp.get("totalCount") or 0))
        time.sleep(WORDSTAT_THROTTLE)
    if out:
        _record_spend(calls=len(out))
    return out


# ── провайдер 2: Arsenkin Tools ──────────────────────────────────────────────
def _arsenkin_post(path: str, body: dict, token: str) -> dict:
    url = f"https://arsenkin.ru/api/tools/{path}"
    data = json.dumps(body, ensure_ascii=False).encode()
    last = None
    for attempt in range(4):  # сервис периодически рвёт соединение — нужен ретрай
        req = urllib.request.Request(url, data=data, method="POST")
        req.add_header("Authorization", f"Bearer {token}")
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            payload = e.read().decode("utf-8", "replace")[:300]
            if e.code == 429 and attempt < 3:
                time.sleep(2 ** attempt)
                continue
            raise RuntimeError(f"Arsenkin HTTP {e.code}: {payload}") from e
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2 ** attempt)
    raise RuntimeError(f"Arsenkin недоступен: {last}")


def _arsenkin(phrases: list[str], region: int, ws: str) -> dict[str, int]:
    """Пачкой за одну задачу: /set → /check(poll) → /get. 1 юнит за фразу.

    ⚠️ Параметры вложены в объект `data` — плоское тело даёт 422 JSON_VALIDATION_ERROR.
    ⚠️ Терминальный статус — именно "finish" (не ready/done).
    """
    token = (os.environ.get("ARSENKIN_API_TOKEN") or "").strip()
    if not token or not phrases:
        return {}

    spent = _spend_today()["arsenkin_units"]
    room = ARSENKIN_DAILY_UNITS - spent
    if room <= 0:
        print(f"  [arsenkin] дневной потолок {ARSENKIN_DAILY_UNITS} юнитов выбран — стоп")
        return {}

    out: dict[str, int] = {}
    todo = phrases[:room]
    for i in range(0, len(todo), ARSENKIN_BATCH):
        out.update(_arsenkin_batch(todo[i:i + ARSENKIN_BATCH], region, ws, token))
    return out


def _arsenkin_batch(batch: list[str], region: int, ws: str, token: str) -> dict[str, int]:
    """Одна задача. При 422 пачка делится пополам, пока не отсеется битая фраза.

    ⚠️ Арсенкин валит ЗАДАЧУ ЦЕЛИКОМ из-за одной плохой строки: 180 маршрутов
    не замерились из-за единственной запятой. normalize() чистит вход, но полагаться
    только на него нельзя — сервис может не принять и что-то ещё, поэтому делим
    пополам и теряем максимум одну фразу вместо всей пачки.
    """
    if not batch:
        return {}
    try:
        s = _arsenkin_post("set", {"tools_name": "wordstat",
                                   "data": {"type": 1, "queries": batch,
                                            "regions": [region], "ws": [ws], "device": ""}}, token)
    except RuntimeError as e:
        if "422" not in str(e):
            raise
        if len(batch) == 1:
            print(f"  [arsenkin] фраза не принята, пропускаю: «{batch[0]}»")
            return {}
        mid = len(batch) // 2
        print(f"  [arsenkin] 422 на пачке {len(batch)} — делю пополам")
        return {**_arsenkin_batch(batch[:mid], region, ws, token),
                **_arsenkin_batch(batch[mid:], region, ws, token)}

    tid, cost = s.get("task_id"), s.get("cost")
    if not tid:
        print(f"  [arsenkin] задача не поставлена: {s}")
        return {}
    print(f"  [arsenkin] задача {tid}: {len(batch)} фраз, {cost} юнитов")
    for _ in range(120):
        time.sleep(5)
        c = _arsenkin_post("check", {"task_id": tid}, token)
        if str(c.get("status")) == "finish":
            break
    else:
        print(f"  [arsenkin] задача {tid} не завершилась за 10 мин — пропуск")
        return {}

    g = _arsenkin_post("get", {"task_id": tid}, token)
    table = (((g.get("result") or {}).get("data") or {}).get("result") or {})
    out: dict[str, int] = {}
    for ph, by_region in table.items():
        val = (by_region or {}).get(str(region), {}).get(ws)
        if val is not None:
            out[ph] = int(val)
    _record_spend(units=int(cost or len(batch)))
    return out


# ── публичный API ────────────────────────────────────────────────────────────
def frequency(phrases, *, region: int = REGION_RU, ws: str = DEFAULT_WS,
              allow_paid: bool = True, verbose: bool = True) -> dict[str, dict]:
    """{фраза: {"freq": int, "source": "cache|wordstat|arsenkin"}}.

    Фразы, которые не удалось померить, в результат НЕ попадают — вызывающий код
    обязан отличать «нет спроса» от «не замерено».
    """
    _load_env()
    phrases = [normalize(p) for p in phrases if p and p.strip()]
    phrases = list(dict.fromkeys(p for p in phrases if p))
    cache = load_cache()

    out: dict[str, dict] = {}
    todo: list[str] = []
    for p in phrases:
        k = _cache_key(p, region, ws)
        if k in cache:
            out[p] = {"freq": cache[k], "source": "cache"}
        else:
            todo.append(p)
    if verbose:
        print(f"[freq] {len(phrases)} фраз: в кэше {len(out)}, мерить {len(todo)}")
    if not todo:
        return out

    got = _wordstat(todo, region)
    for p, v in got.items():
        out[p] = {"freq": v, "source": "wordstat"}
        cache[_cache_key(p, region, ws)] = v
    if got:
        save_cache(cache)

    rest = [p for p in todo if p not in got]
    if rest and allow_paid:
        if verbose:
            print(f"[freq] Wordstat закрыл {len(got)}, добираю Арсенкиным {len(rest)}")
        # Падение платного добора не должно ронять прогон: то, что уже померено
        # Wordstat'ом, сохранено, остальное доберём следующим запуском.
        try:
            got2 = _arsenkin(rest, region, ws)
        except Exception as e:  # noqa: BLE001
            print(f"  [arsenkin] добор не удался: {e}")
            got2 = {}
        for p, v in got2.items():
            out[p] = {"freq": v, "source": "arsenkin"}
            cache[_cache_key(p, region, ws)] = v
        if got2:
            save_cache(cache)
        rest = [p for p in rest if p not in got2]
    if rest and verbose:
        print(f"[freq] ⚠️ не замерено {len(rest)} фраз — это НЕ «спроса нет», повторить позже")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Частотность фраз: кэш → Wordstat → Arsenkin")
    ap.add_argument("phrases", nargs="*", help="фразы прямо в аргументах")
    ap.add_argument("--in", dest="infile", help="файл со фразами (по одной в строке)")
    ap.add_argument("--out", help="куда сложить JSON результата")
    ap.add_argument("--region", type=int, default=REGION_RU, help="регион Яндекса (225=РФ)")
    ap.add_argument("--ws", default=DEFAULT_WS, choices=["base", "quoted", "overal", "exact"])
    ap.add_argument("--no-paid", action="store_true", help="не трогать Арсенкина (только кэш+Wordstat)")
    ap.add_argument("--stats", action="store_true", help="показать кэш и траты за сегодня")
    args = ap.parse_args()

    if args.stats:
        cache = load_cache()
        sp = _spend_today()
        print(f"кэш: {len(cache)} фраз  ({CACHE_PATH})")
        print(f"сегодня: Arsenkin {sp['arsenkin_units']}/{ARSENKIN_DAILY_UNITS} юнитов, "
              f"Wordstat {sp['wordstat_calls']} вызовов")
        return 0

    phrases = list(args.phrases)
    if args.infile:
        phrases += [l.strip() for l in Path(args.infile).read_text(encoding="utf-8").splitlines()
                    if l.strip()]
    if not phrases:
        ap.error("нечего мерить: дай фразы аргументами или --in файл")

    res = frequency(phrases, region=args.region, ws=args.ws, allow_paid=not args.no_paid)
    for p in sorted(res, key=lambda x: -res[x]["freq"]):
        print(f"{res[p]['freq']:>9}  {p}   [{res[p]['source']}]")
    if args.out:
        Path(args.out).write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n→ {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
