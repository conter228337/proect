"""CLI-реализация кредитного прогноза без GUI.

Вместо Tkinter скрипт читает CSV с панелью и сохраняет результаты в выходные CSV
файлы. Это делает проект пригодным для headless-окружений и CI.

Примеры:
    python model_gui_advanced.py --input company_analysis_auto_growth.csv
    python model_gui_advanced.py --input company_analysis_auto_growth.csv \
        --output credit_risk_forecasts_ready.csv \
        --backtest-output credit_risk_backtest_ready.csv
    python model_gui_advanced.py --input company_analysis_auto_growth.csv --validate-only
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

from forecast_credit_risk import (
    backtest_forecasts,
    generate_forecasts,
    validate_training_data,
)


def _default_input() -> Path:
    candidates = [
        Path("company_analysis_auto_growth.csv"),
        Path("company_analysis_20261017.csv"),
        Path("company_analysis_20261014_153634.csv"),
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0]


def _maybe_read_input(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(f"Входной CSV не найден: {path}")
    return pd.read_csv(path, sep=";", decimal=",", encoding="utf-8-sig")


def _print_report(report: dict) -> None:
    print(f"Компаний: {report['companies']}")
    print(f"Строк в панели: {report['panel_rows']}")
    print(f"Пары для обучения: {report['training_pairs']}")
    print(
        "Default ready: "
        f"{report['default']['ready']} | "
        f"rows={report['default']['rows']}, defaults={report['default']['defaults']}, "
        f"non_defaults={report['default']['non_defaults']}"
    )
    print(
        "Rating ready: "
        f"{report['rating']['ready']} | "
        f"rows={report['rating']['rows']}, unique={report['rating']['unique_ratings']}"
    )
    print(f"Overall ready: {report['overall_ready']}")
    if report["default"]["reason"]:
        print("Default: " + report["default"]["reason"])
    if report["rating"]["reason"]:
        print("Rating: " + report["rating"]["reason"])


def _write_csv(frame: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(path, index=False, sep=";", decimal=",", encoding="utf-8-sig")
    print(f"Записано {len(frame)} строк в {path}")


def run(args: argparse.Namespace) -> int:
    input_path = Path(args.input).expanduser()
    if not input_path.exists() and args.input == "":
        input_path = _default_input()
    panel = _maybe_read_input(input_path)

    report = validate_training_data(panel)
    _print_report(report)

    if args.summary_json:
        print("\nJSON summary:")
        print(json.dumps(report, ensure_ascii=False, indent=2))

    if args.validate_only:
        return 0

    forecast = generate_forecasts(panel)
    forecast_path = Path(args.output).expanduser() if args.output else input_path.with_name("credit_risk_forecasts.csv")
    _write_csv(forecast, forecast_path)

    if args.backtest_output:
        backtest = backtest_forecasts(panel)
        backtest_path = Path(args.backtest_output).expanduser()
        _write_csv(backtest, backtest_path)

    if not forecast.empty:
        unavailable = forecast[
            forecast["default_model_status"].ne("ok")
            | forecast["rating_model_status"].ne("ok")
        ]
        if not unavailable.empty:
            print(
                "Для части целей данных недостаточно; причины записаны "
                "в default_model_status и rating_model_status."
            )

    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="CLI-обработка прогнозов кредитного риска без графического интерфейса."
    )
    parser.add_argument(
        "--input",
        default=str(_default_input()),
        help="CSV с панелью данных. По умолчанию используется company_analysis_auto_growth.csv, если он есть.",
    )
    parser.add_argument(
        "--output",
        default="",
        help="Путь для CSV с итоговым прогнозом. По умолчанию рядом с входным CSV.",
    )
    parser.add_argument(
        "--backtest-output",
        default="",
        help="Путь для CSV с историческим backtest. Если не указан, бэктест не создаётся.",
    )
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="Проверить достаточность данных без обучения и записи прогноза.",
    )
    parser.add_argument(
        "--summary-json",
        action="store_true",
        help="Дополнительно вывести JSON-сводку в stdout.",
    )
    return parser


if __name__ == "__main__":
    parser = build_parser()
    try:
        raise SystemExit(run(parser.parse_args()))
    except FileNotFoundError as exc:
        parser.error(str(exc))
    except Exception as exc:  # pragma: no cover - CLI user-facing guard
        parser.exit(status=1, message=f"Ошибка: {exc}\n")
