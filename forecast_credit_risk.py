"""Обучение годовых прогнозов рейтинга и дефолта по собранной панели.

Запуск после main.py:
    python forecast_credit_risk.py company_analysis_YYYYMMDD_HHMMSS.csv

Цель каждого обучающего примера — значение в следующем календарном году.
Для работы нужны pandas, numpy и scikit-learn. Вероятность дефолта является
оценкой модели и не считается калиброванной PD для регуляторного применения.
В имеющейся панели ноль означает отсутствие найденного сообщения о дефолте,
а не подтверждённое отсутствие кредитных обязательств.
"""

from __future__ import annotations

import argparse
import math
from pathlib import Path

import numpy as np
import pandas as pd

FEATURES = (
    "sentiment_score",
    "sentiment_positive",
    "sentiment_neutral",
    "sentiment_negative",
    "ROA",
    "ROE",
    "Debt_to_EBITDA",
    "Total_Liabilities_to_Assets",
    "Current_Ratio",
    "Cash_to_Assets",
    "Ln_Total_Assets",
    "total_assets",
    "total_liabilities",
    "equity",
    "revenue",
    "net_income",
    "ebitda",
    "current_assets",
    "current_liabilities",
    "cash",
    "market_cap_million_rub",
    "stock_volatility",
    "MTB",
    "prior_default",
    "prior_rating_score",
)

RATING_ORDER = (
    "AAA", "AA+", "AA", "AA-", "A+", "A", "A-", "BBB+", "BBB", "BBB-",
    "BB+", "BB", "BB-", "B+", "B", "B-", "CCC+", "CCC", "CCC-", "CC",
    "C", "SD", "D",
)

_CSV_NAMES = {
    "inn": "ИНН",
    "ticker": "Тикер",
    "company_name": "Компания",
    "year": "Год",
    "rating": "рейтинг",
    "default": "дефолт (по облигациям)",
    "sentiment_score": "Тональность отчёта (RuBERT, positive - negative)",
    "sentiment_positive": "Тональность: positive",
    "sentiment_neutral": "Тональность: neutral",
    "sentiment_negative": "Тональность: negative",
    "total_assets": "Актив итого (1600)",
    "total_liabilities": "Обязательства (1400 + 1500)",
    "equity": "Капитал (1300)",
    "revenue": "Выручка (2110)",
    "net_income": "Чистая прибыть (2400)",
    "ebitda": "EBIT упрощ. (ЧП+проценты+налог, без амортизации)",
    "current_assets": "Оборотные активы (1200)",
    "current_liabilities": "Краткосрочные обязательства (1500)",
    "cash": "Денежные средства (1250)",
    "ROA": "ROA, %",
    "ROE": "ROE, %",
    "Debt_to_EBITDA": "Обязательства/EBIT упрощ.",
    "Total_Liabilities_to_Assets": "Total_Liabilities_to_Assets",
    "Current_Ratio": "Тек. ликвидность",
    "Cash_to_Assets": "Денежные ср-ва/Активы",
    "Ln_Total_Assets": "Ln(Активы)",
    "market_cap_million_rub": "Капитализация, млн ₽",
    "stock_volatility": "Волатильность",
    "MTB": "MTB",
}
_RATING_SCORE = {grade: float(index) for index, grade in enumerate(RATING_ORDER)}


def _clean_rating(value) -> str | None:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return None
    rating = str(value).upper().strip()
    rating = rating.replace("(RU)", "").replace(".RU", "").replace(" ", "")
    return rating if rating in _RATING_SCORE else None


def _rating_score(value) -> float:
    rating = _clean_rating(value)
    return _RATING_SCORE.get(rating, np.nan)


def _nearest_rating(score: float) -> str:
    return min(RATING_ORDER, key=lambda grade: abs(_RATING_SCORE[grade] - score))


def _prepare_panel(frame: pd.DataFrame) -> pd.DataFrame:
    """Привести сохранённый CSV (русские заголовки) к внутренним именам."""
    reverse_names = {localized: internal for internal, localized in _CSV_NAMES.items()}
    frame = frame.rename(columns=reverse_names).copy()
    required = {"inn", "year"}
    missing = required - set(frame.columns)
    if missing:
        raise ValueError(
            "В CSV не найдены обязательные столбцы: "
            + ", ".join(sorted(missing))
        )
    frame["year"] = pd.to_numeric(frame["year"], errors="coerce")
    frame = frame.dropna(subset=["inn", "year"])
    frame["inn"] = frame["inn"].astype(str).str.strip()
    frame = frame[frame["inn"].ne("") & frame["inn"].ne("<NA>")]
    frame["year"] = frame["year"].astype(int)

    for column in ("rating", "default", *FEATURES):
        if column not in frame:
            frame[column] = np.nan
    default_raw = frame["default"].replace(r"^\s*$", np.nan, regex=True)
    default_numeric = pd.to_numeric(default_raw, errors="coerce")
    invalid_default = default_raw.notna() & default_numeric.isna()
    if invalid_default.any():
        raise ValueError(
            f"В столбце default есть {int(invalid_default.sum())} "
            "нечисловых меток"
        )
    invalid_binary = default_numeric.notna() & ~default_numeric.isin([0, 1])
    if invalid_binary.any():
        raise ValueError(
            f"В столбце default есть {int(invalid_binary.sum())} меток "
            "вне бинарного диапазона 0/1"
        )
    frame["default"] = default_numeric
    frame["prior_rating_score"] = frame["rating"].map(_rating_score)
    frame["prior_default"] = pd.to_numeric(frame["default"], errors="coerce")
    for column in FEATURES:
        frame[column] = pd.to_numeric(frame[column], errors="coerce").replace(
            [np.inf, -np.inf], np.nan
        ).astype(float)
    return frame


def _next_year_pairs(frame: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Сформировать пары признаков года t и меток года t+1 без пропуска лет."""
    features, targets = [], []
    for _, company in frame.sort_values("year").groupby("inn", sort=False):
        rows = company.drop_duplicates("year", keep="last").set_index("year")
        for year in rows.index:
            if year + 1 not in rows.index:
                continue
            current = rows.loc[year]
            future = rows.loc[year + 1]
            features.append(current.loc[list(FEATURES)].to_dict())
            targets.append({
                "inn": current["inn"],
                "feature_year": int(year),
                "target_year": int(year + 1),
                "default": pd.to_numeric(future["default"], errors="coerce"),
                "rating_score": _rating_score(future["rating"]),
            })
    target_columns = (
        "inn",
        "feature_year",
        "target_year",
        "default",
        "rating_score",
    )
    return (
        pd.DataFrame(features, columns=FEATURES),
        pd.DataFrame(targets, columns=target_columns),
    )


def _latest_company_rows(frame: pd.DataFrame) -> pd.DataFrame:
    latest = frame.sort_values("year").groupby("inn", sort=False).tail(1).copy()
    latest["forecast_year"] = latest["year"] + 1
    return latest


def _training_pairs_as_of(
    features: pd.DataFrame,
    targets: pd.DataFrame,
    as_of_year: int,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Оставить только целевые метки, опубликованные не позже года прогноза."""
    available = targets["target_year"] <= as_of_year
    return features.loc[available], targets.loc[available]


def validate_training_data(frame: pd.DataFrame) -> dict:
    """Проверить, хватает ли данных для обучения модели дефолта и рейтинга."""
    panel = _prepare_panel(frame)
    if panel.empty:
        raise ValueError("Во входном CSV нет строк с корректными ИНН и годом")

    _, pair_targets = _next_year_pairs(panel)
    default_values = pair_targets["default"].dropna()
    default_counts = default_values.value_counts().astype(int)
    default_ready = (
        len(default_values) >= 30
        and default_counts.get(0, 0) >= 10
        and default_counts.get(1, 0) >= 5
    )

    rating_values = pair_targets["rating_score"].dropna()
    rating_ready = len(rating_values) >= 30 and rating_values.nunique() >= 3

    default_reason = "" if default_ready else (
        f"недостаточно данных (строк={len(default_values)}, "
        f"дефолтов={default_counts.get(1, 0)}, без дефолта={default_counts.get(0, 0)}; "
        "нужно минимум 30, 5 и 10 соответственно)"
    )
    rating_reason = "" if rating_ready else (
        f"недостаточно данных (строк={len(rating_values)}, "
        f"различных рейтингов={rating_values.nunique()}; нужно минимум 30 и 3)"
    )

    return {
        "companies": int(panel["inn"].nunique()),
        "panel_rows": int(len(panel)),
        "training_pairs": int(len(pair_targets)),
        "default": {
            "rows": int(len(default_values)),
            "defaults": int(default_counts.get(1, 0)),
            "non_defaults": int(default_counts.get(0, 0)),
            "ready": bool(default_ready),
            "reason": default_reason,
        },
        "rating": {
            "rows": int(len(rating_values)),
            "unique_ratings": int(rating_values.nunique()),
            "ready": bool(rating_ready),
            "reason": rating_reason,
        },
        "overall_ready": bool(default_ready and rating_ready),
    }


def _fit_models(
    train_features: pd.DataFrame,
    train_targets: pd.DataFrame,
):
    """Обучить модели дефолта и рейтинга на уже доступной истории."""
    try:
        from sklearn.ensemble import RandomForestRegressor
        from sklearn.impute import SimpleImputer
        from sklearn.linear_model import LogisticRegression
        from sklearn.pipeline import make_pipeline
        from sklearn.preprocessing import StandardScaler
    except ImportError as exc:
        raise RuntimeError(
            "Для обучения прогнозов установите scikit-learn: "
            "pip install scikit-learn"
        ) from exc

    default_rows = train_targets["default"].notna()
    X_default = train_features.loc[default_rows]
    y_default = train_targets.loc[default_rows, "default"].astype(int)
    default_features = X_default.columns[X_default.notna().any()].tolist()
    X_default = X_default.loc[:, default_features]
    default_model = None
    default_reason = ""
    counts = y_default.value_counts()
    if len(y_default) < 30 or counts.get(0, 0) < 10 or counts.get(1, 0) < 5:
        default_reason = (
            f"недостаточно данных (строк={len(y_default)}, "
            f"дефолтов={counts.get(1, 0)}, без дефолта={counts.get(0, 0)}; "
            "нужно минимум 30, 5 и 10 соответственно)"
        )
    elif not default_features:
        default_reason = "нет числовых признаков для обучения модели дефолта"
    else:
        default_model = make_pipeline(
            SimpleImputer(strategy="median", add_indicator=True),
            StandardScaler(),
            LogisticRegression(max_iter=2_000, random_state=42),
        )
        default_model.fit(X_default, y_default)

    rating_rows = train_targets["rating_score"].notna()
    X_rating = train_features.loc[rating_rows]
    y_rating = train_targets.loc[rating_rows, "rating_score"].astype(float)
    rating_features = X_rating.columns[X_rating.notna().any()].tolist()
    X_rating = X_rating.loc[:, rating_features]
    rating_model = None
    rating_reason = ""
    if len(y_rating) < 30 or y_rating.nunique() < 3:
        rating_reason = (
            f"недостаточно данных (строк={len(y_rating)}, "
            f"различных рейтингов={y_rating.nunique()}; нужно минимум 30 и 3)"
        )
    elif not rating_features:
        rating_reason = "нет числовых признаков для обучения модели рейтинга"
    else:
        rating_model = make_pipeline(
            SimpleImputer(strategy="median", add_indicator=True),
            RandomForestRegressor(
                n_estimators=300,
                min_samples_leaf=2,
                random_state=42,
                n_jobs=-1,
            ),
        )
        rating_model.fit(X_rating, y_rating)
    return (
        default_model,
        default_reason,
        default_features,
        rating_model,
        rating_reason,
        rating_features,
    )


def _predict_row(
    features: pd.DataFrame,
    models,
    *,
    inn: str,
    feature_year: int,
    target_year: int,
) -> dict:
    (
        default_model,
        default_reason,
        default_features,
        rating_model,
        rating_reason,
        rating_features,
    ) = models
    result = {
        "inn": inn,
        "feature_year": feature_year,
        "forecast_year": target_year,
        "estimated_default_probability": np.nan,
        "default_model_status": default_reason or "ok",
        "predicted_rating": "",
        "predicted_rating_score": np.nan,
        "rating_model_status": rating_reason or "ok",
    }
    if default_model is not None:
        positive_index = list(default_model[-1].classes_).index(1)
        result["estimated_default_probability"] = float(
            default_model.predict_proba(features.loc[:, default_features])[0][
                positive_index
            ]
        )
    if rating_model is not None:
        rating_score = float(
            rating_model.predict(features.loc[:, rating_features])[0]
        )
        result["predicted_rating"] = _nearest_rating(rating_score)
        result["predicted_rating_score"] = rating_score
    return result


def generate_forecasts(frame: pd.DataFrame) -> pd.DataFrame:
    """Обучить модели на доступных парах и вернуть прогноз для последнего года каждой фирмы."""
    panel = _prepare_panel(frame)
    if panel.empty:
        raise ValueError("Во входном CSV нет строк с корректными ИНН и годом")
    latest = _latest_company_rows(panel)
    pair_features, pair_targets = _next_year_pairs(panel)
    predictions = []

    models_by_year = {}
    for _, row in latest.iterrows():
        as_of_year = int(row["year"])
        if as_of_year not in models_by_year:
            train_features, train_targets = _training_pairs_as_of(
                pair_features, pair_targets, as_of_year
            )
            models_by_year[as_of_year] = _fit_models(
                train_features, train_targets
            )
        X_current = pd.DataFrame(
            [{name: row.get(name, np.nan) for name in FEATURES}],
            columns=FEATURES,
        )
        result = _predict_row(
            X_current,
            models_by_year[as_of_year],
            inn=str(row["inn"]),
            feature_year=as_of_year,
            target_year=int(row["forecast_year"]),
        )
        result["ticker"] = row.get("ticker", "")
        result["company_name"] = row.get("company_name", "")
        predictions.append(result)
    return pd.DataFrame(predictions)


def backtest_forecasts(frame: pd.DataFrame) -> pd.DataFrame:
    """Вернуть one-year-ahead прогнозы для исторических пар без утечки меток."""
    panel = _prepare_panel(frame)
    if panel.empty:
        raise ValueError("Во входном CSV нет строк с корректными ИНН и годом")
    pair_features, pair_targets = _next_year_pairs(panel)
    predictions = []
    models_by_year = {}
    for index, target in pair_targets.iterrows():
        as_of_year = int(target["feature_year"])
        if as_of_year not in models_by_year:
            train_features, train_targets = _training_pairs_as_of(
                pair_features, pair_targets, as_of_year
            )
            models_by_year[as_of_year] = _fit_models(
                train_features, train_targets
            )
        feature_row = pd.DataFrame(
            [pair_features.loc[index].to_dict()], columns=FEATURES
        )
        result = _predict_row(
            feature_row,
            models_by_year[as_of_year],
            inn=str(target["inn"]),
            feature_year=as_of_year,
            target_year=int(target["target_year"]),
        )
        result["actual_default"] = target["default"]
        result["actual_rating_score"] = target["rating_score"]
        predictions.append(result)
    return pd.DataFrame(predictions)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input_csv", type=Path, help="CSV, созданный main.py")
    parser.add_argument(
        "-o", "--output", type=Path, help="Файл прогноза (по умолчанию рядом с исходным)"
    )
    parser.add_argument(
        "--backtest-output",
        type=Path,
        help="Дополнительно сохранить исторические прогнозы и фактические метки",
    )
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="Проверить, хватает ли данных для обучения, без создания прогноза",
    )
    args = parser.parse_args()
    if not args.input_csv.is_file():
        parser.error(f"входной файл не найден: {args.input_csv}")

    panel = pd.read_csv(
        args.input_csv,
        sep=";",
        decimal=",",
        encoding="utf-8-sig",
        dtype={"ИНН": "string"},
    )
    if args.validate_only:
        report = validate_training_data(panel)
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
        return

    forecast = generate_forecasts(panel)
    output = args.output or args.input_csv.with_name("credit_risk_forecasts.csv")
    forecast.to_csv(output, index=False, sep=";", decimal=",", encoding="utf-8-sig")
    print(f"Сохранено прогнозов: {len(forecast)} -> {output}")
    if args.backtest_output:
        backtest = backtest_forecasts(panel)
        backtest.to_csv(
            args.backtest_output,
            index=False,
            sep=";",
            decimal=",",
            encoding="utf-8-sig",
        )
        print(
            f"Сохранено исторических прогнозов: {len(backtest)} "
            f"-> {args.backtest_output}"
        )
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


if __name__ == "__main__":
    main()
