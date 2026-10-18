from __future__ import annotations

import csv
import json
import re
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parent
FORECAST_PATH = ROOT / "credit_risk_forecasts_ready.csv"
BACKTEST_PATH = ROOT / "credit_risk_backtest_ready.csv"
MOEX_PATH = ROOT / "training_moex_issuers.csv"
RFSD_PATH = ROOT / "training_moex_rfsd_financials.csv"
DISCLOSURE_PATH = ROOT / "training_disclosure_events.jsonl"
EVENT_DETAILS_PATH = ROOT / "training_event_details.jsonl"
MODEL_NOTICE = (
    "Оценка экспериментальная и не калибрована; используйте её только для "
    "исследовательских целей, не для кредитных решений."
)


def _read_rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as source:
        return list(csv.DictReader(source, delimiter=";"))


def _number(value: str) -> float | None:
    try:
        return float(value.replace(",", ".")) if value else None
    except ValueError:
        return None


def _normalise(value: str) -> str:
    return re.sub(r"[^0-9a-zа-яё]", "", value.lower())


def _company_answer(rows: list[dict[str, str]]) -> str:
    replies = []
    for row in rows[:5]:
        probability = _number(row.get("estimated_default_probability", ""))
        risk = "нет прогноза"
        if probability is not None and row.get("default_model_status") == "ok":
            risk = f"{probability:.1%}"
        name = row.get("company_name") or row.get("ticker") or "Компания"
        replies.append(
            f"{name} (ИНН {row.get('inn', '—')})\n"
            f"Прогноз на {row.get('forecast_year', '—')}: дефолт {risk}; "
            f"рейтинг {row.get('predicted_rating') or 'нет прогноза'}.\n"
            f"Статусы: default={row.get('default_model_status', '—')}; "
            f"rating={row.get('rating_model_status', '—')}"
        )
    if len(rows) > 5:
        replies.append(f"Найдено ещё записей: {len(rows) - 5}.")
    return "\n\n".join(replies) + f"\n\n{MODEL_NOTICE}"


def answer(message: str) -> str:
    query = message.strip()
    if not query:
        return "Напишите ИНН, название или тикер компании, либо запросите топ риска или сводку."

    forecasts = _read_rows(FORECAST_PATH)
    backtest_query = any(word in query.lower() for word in ("бэктест", "backtest", "истор"))
    if backtest_query:
        backtest = _read_rows(BACKTEST_PATH)
        inn = re.search(r"\b\d{10,12}\b", query)
        if not inn:
            return "Для исторического backtest укажите ИНН компании."
        company_rows = [row for row in backtest if row.get("inn") == inn.group()]
        if not company_rows:
            return f"В backtest нет компании с ИНН {inn.group()}."
        company_rows.sort(key=lambda row: row.get("feature_year", ""))
        lines = []
        for row in company_rows[-5:]:
            probability = _number(row.get("estimated_default_probability", ""))
            forecast = f"{probability:.1%}" if probability is not None else "нет прогноза"
            lines.append(
                f"{row.get('feature_year')}→{row.get('forecast_year')}: "
                f"прогноз дефолта {forecast}, факт {row.get('actual_default') or '—'}, "
                f"рейтинг {row.get('predicted_rating') or '—'}"
            )
        return (
            f"Исторические прогнозы для ИНН {inn.group()}:\n"
            + "\n".join(lines)
            + f"\n\n{MODEL_NOTICE}"
        )

    inn = re.search(r"\b\d{10,12}\b", query)
    if inn:
        matched = [row for row in forecasts if row.get("inn") == inn.group()]
        if matched:
            return _company_answer(matched)

    normalized_query = _normalise(query)
    matched = []
    for row in forecasts:
        aliases = [row.get("company_name", ""), row.get("ticker", "")]
        if any(alias and _normalise(alias) in normalized_query for alias in aliases):
            matched.append(row)
    if matched:
        return _company_answer(matched)

    if any(word in query.lower() for word in ("топ", "максим", "наибольш", "самый высокий")):
        count_match = re.search(r"\bтоп\s*(\d{1,2})\b", query.lower())
        limit = min(int(count_match.group(1)), 20) if count_match else 5
        ranked = [row for row in forecasts if _number(row.get("estimated_default_probability", "")) is not None]
        ranked.sort(key=lambda row: _number(row.get("estimated_default_probability", "")) or 0, reverse=True)
        return (
            f"Топ {min(limit, len(ranked))} из {len(ranked)} компаний по "
            "прогнозной вероятности дефолта:\n\n"
            + _company_answer(ranked[:limit])
        )

    if any(word in query.lower() for word in ("сводк", "сколько", "всего", "средн", "общ")):
        probabilities = [_number(row.get("estimated_default_probability", "")) for row in forecasts]
        valid = [value for value in probabilities if value is not None]
        ready_defaults = sum(row.get("default_model_status") == "ok" for row in forecasts)
        ready_ratings = sum(row.get("rating_model_status") == "ok" for row in forecasts)
        average = sum(valid) / len(valid) if valid else None
        year = forecasts[0].get("forecast_year", "—") if forecasts else "—"
        return (
            f"Сводка прогноза на {year}: компаний {len(forecasts)}; "
            f"default-модель готова для {ready_defaults}; рейтинг-модель для {ready_ratings}.\n"
            f"Средняя прогнозная вероятность дефолта среди рассчитанных значений: "
            f"{average:.1%}.\n\n{MODEL_NOTICE}"
            if average is not None
            else f"Средняя вероятность дефолта не рассчитана.\n\n{MODEL_NOTICE}"
        )

    return (
        "Не нашёл компанию в таблице прогнозов. Попробуйте указать точный ИНН, "
        "название или тикер; можно также запросить «топ 5 по риску», «сводку» "
        "или «бэктест ИНН …». Я отвечаю только по данным проекта и не веду свободный диалог."
    )


class ChatHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def end_headers(self) -> None:
        if self.headers.get("Origin") == "https://www.e-disclosure.ru":
            self.send_header("Access-Control-Allow-Origin", "https://www.e-disclosure.ru")
            self.send_header("Access-Control-Allow-Private-Network", "true")
            self.send_header("Vary", "Origin")
        super().end_headers()

    def do_OPTIONS(self) -> None:
        if self.headers.get("Origin") != "https://www.e-disclosure.ru":
            self.send_error(403)
            return
        self.send_response(204)
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Vary", "Origin")
        self.end_headers()

    def _send_json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/collection/details-next":
            from urllib.parse import parse_qs

            query = parse_qs(parsed.query)
            kind = query.get("kind", ["rating"])[0]
            if kind not in {"rating", "default"}:
                self._send_json({"error": "kind must be rating or default."}, 400)
                return
            limit = min(20, max(1, int(query.get("limit", ["10"])[0])))
            done = set()
            if EVENT_DETAILS_PATH.exists():
                with EVENT_DETAILS_PATH.open(encoding="utf-8") as source:
                    for line in source:
                        try:
                            done.add(json.loads(line)["guid"])
                        except (json.JSONDecodeError, KeyError):
                            continue
            items, seen = [], set()
            if DISCLOSURE_PATH.exists():
                with DISCLOSURE_PATH.open(encoding="utf-8") as source:
                    for line in source:
                        try:
                            company = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        for event in company.get("events", []):
                            name = event.get("eventName", "")
                            normalized = name.lower()
                            if kind == "rating":
                                matches = "рейтинг" in normalized and any(
                                    word in normalized for word in ("присвоени", "изменени", "кредитный")
                                )
                            else:
                                matches = any(
                                    word in normalized
                                    for word in ("дефолт", "неисполнении обязательств", "неисполнение обязательств")
                                )
                            guid = event.get("pseudoGUID", "")
                            if not matches or not guid or guid in done or guid in seen:
                                continue
                            seen.add(guid)
                            items.append({
                                "inn": company.get("inn", ""),
                                "guid": guid,
                                "event_name": name,
                                "pub_date": event.get("pubDate", ""),
                                "kind": kind,
                            })
                            if len(items) >= limit:
                                self._send_json({"kind": kind, "items": items, "remaining": None})
                                return
            self._send_json({"kind": kind, "items": items, "remaining": len(items)})
            return
        if parsed.path != "/api/collection/next":
            super().do_GET()
            return
        if not MOEX_PATH.exists() or not RFSD_PATH.exists():
            self._send_json({"error": "MOEX/RFSD input files are missing."}, 404)
            return
        from urllib.parse import parse_qs

        query = parse_qs(parsed.query)
        offset = max(0, int(query.get("offset", ["0"])[0]))
        limit = min(25, max(1, int(query.get("limit", ["10"])[0])))
        rfsd_inns = {row["inn"] for row in _read_rows(RFSD_PATH) if row.get("inn")}
        companies = [row for row in _read_rows(MOEX_PATH) if row.get("inn") in rfsd_inns]
        done = set()
        if DISCLOSURE_PATH.exists():
            with DISCLOSURE_PATH.open(encoding="utf-8") as source:
                for line in source:
                    try:
                        done.add(json.loads(line)["inn"])
                    except (json.JSONDecodeError, KeyError):
                        continue
        remaining = [row for row in companies if row.get("inn") not in done]
        self._send_json({"offset": offset, "items": remaining[offset:offset + limit], "remaining": len(remaining)})

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/collection/save-details":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > 5_000_000:
                    raise ValueError("Detail batch is empty or too large.")
                payload = json.loads(self.rfile.read(length))
                records = payload.get("records")
                if not isinstance(records, list) or len(records) > 25:
                    raise ValueError("A detail batch may contain at most 25 records.")
                existing = set()
                if EVENT_DETAILS_PATH.exists():
                    with EVENT_DETAILS_PATH.open(encoding="utf-8") as source:
                        for line in source:
                            try:
                                existing.add(json.loads(line)["guid"])
                            except (json.JSONDecodeError, KeyError):
                                continue
                saved = 0
                with EVENT_DETAILS_PATH.open("a", encoding="utf-8") as output:
                    for record in records:
                        guid = str(record.get("guid", ""))
                        text = record.get("text", "")
                        if not guid or not isinstance(text, str):
                            raise ValueError("Each detail must include guid and text.")
                        if guid in existing:
                            continue
                        output.write(json.dumps(record, ensure_ascii=False) + "\n")
                        existing.add(guid)
                        saved += 1
                self._send_json({"saved": saved})
            except (ValueError, json.JSONDecodeError) as exc:
                self._send_json({"error": str(exc)}, 400)
            return
        if path == "/api/collection/save":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > 5_000_000:
                    raise ValueError("Collection batch is empty or too large.")
                payload = json.loads(self.rfile.read(length))
                records = payload.get("records")
                if not isinstance(records, list) or len(records) > 25:
                    raise ValueError("A batch may contain at most 25 records.")
                existing = set()
                if DISCLOSURE_PATH.exists():
                    with DISCLOSURE_PATH.open(encoding="utf-8") as source:
                        for line in source:
                            try:
                                existing.add(json.loads(line)["inn"])
                            except (json.JSONDecodeError, KeyError):
                                continue
                saved = 0
                with DISCLOSURE_PATH.open("a", encoding="utf-8") as output:
                    for record in records:
                        inn = str(record.get("inn", ""))
                        if not re.fullmatch(r"\d{10}|\d{12}", inn):
                            raise ValueError("Each record must have a valid INN.")
                        if inn in existing:
                            continue
                        if not isinstance(record.get("events"), list):
                            raise ValueError("Each record must include an events list.")
                        output.write(json.dumps(record, ensure_ascii=False) + "\n")
                        existing.add(inn)
                        saved += 1
                self._send_json({"saved": saved})
            except (ValueError, json.JSONDecodeError) as exc:
                self._send_json({"error": str(exc)}, 400)
            return
        if path != "/api/chat":
            self.send_error(404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 8192:
                raise ValueError("Сообщение пустое или слишком длинное.")
            payload = json.loads(self.rfile.read(length))
            message = payload.get("message", "")
            if not isinstance(message, str) or len(message) > 500:
                raise ValueError("Максимальная длина сообщения: 500 символов.")
            result = {"reply": answer(message)}
            body = json.dumps(result, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (ValueError, json.JSONDecodeError) as exc:
            body = json.dumps({"error": str(exc)}, ensure_ascii=False).encode("utf-8")
            self.send_response(400)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except OSError as exc:
            body = json.dumps({"error": f"Не удалось прочитать результаты: {exc}"}, ensure_ascii=False).encode("utf-8")
            self.send_response(500)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)


if __name__ == "__main__":
    server = ThreadingHTTPServer(("0.0.0.0", 8787), ChatHandler)
    print("Risk forecast chat available on port 8787", flush=True)
    server.serve_forever()