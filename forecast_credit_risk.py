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


def generate_forecasts(frame: pd.DataFrame) -> pd.DataFrame:
    """Обучить модели на доступных парах и вернуть прогноз для последнего года каждой фирмы."""
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

    panel = _prepare_panel(frame)
    if panel.empty:
        raise ValueError("Во входном CSV нет строк с корректными ИНН и годом")
    latest = _latest_company_rows(panel)
    pair_features, pair_targets = _next_year_pairs(panel)
    predictions = []

    def fit_models(as_of_year):
        train_features, train_targets = _training_pairs_as_of(
            pair_features, pair_targets, as_of_year
        )
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

    models_by_year = {}
    for _, row in latest.iterrows():
        as_of_year = int(row["year"])
        if as_of_year not in models_by_year:
            models_by_year[as_of_year] = fit_models(as_of_year)
        (
            default_model,
            default_reason,
            default_features,
            rating_model,
            rating_reason,
            rating_features,
        ) = models_by_year[as_of_year]
        X_current = pd.DataFrame(
            [{name: row.get(name, np.nan) for name in FEATURES}],
            columns=FEATURES,
        )
        result = {
            "inn": row["inn"],
            "ticker": row.get("ticker", ""),
            "company_name": row.get("company_name", ""),
            "feature_year": int(row["year"]),
            "forecast_year": int(row["forecast_year"]),
            "estimated_default_probability": np.nan,
            "default_model_status": default_reason or "ok",
            "predicted_rating": "",
            "rating_model_status": rating_reason or "ok",
        }
        if default_model is not None:
            classes = list(default_model[-1].classes_)
            positive_index = classes.index(1)
            result["estimated_default_probability"] = float(
                default_model.predict_proba(X_current.loc[:, default_features])[0][
                    positive_index
                ]
            )
        if rating_model is not None:
            rating_score = float(
                rating_model.predict(X_current.loc[:, rating_features])[0]
            )
            result["predicted_rating"] = _nearest_rating(rating_score)
            result["predicted_rating_score"] = rating_score
        else:
            result["predicted_rating_score"] = np.nan
        predictions.append(result)
    return pd.DataFrame(predictions)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input_csv", type=Path, help="CSV, созданный main.py")
    parser.add_argument(
        "-o", "--output", type=Path, help="Файл прогноза (по умолчанию рядом с исходным)"
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
    forecast = generate_forecasts(panel)
    output = args.output or args.input_csv.with_name("credit_risk_forecasts.csv")
    forecast.to_csv(output, index=False, sep=";", decimal=",", encoding="utf-8-sig")
    print(f"Сохранено прогнозов: {len(forecast)} -> {output}")
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
