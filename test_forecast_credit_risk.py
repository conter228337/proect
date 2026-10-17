import unittest

import numpy as np
import pandas as pd

from forecast_credit_risk import (
    FEATURES,
    _next_year_pairs,
    _prepare_panel,
    _training_pairs_as_of,
    generate_forecasts,
)


class ForecastCreditRiskTests(unittest.TestCase):
    def test_prepare_panel_preserves_inn_as_text_and_accepts_blank_targets(self):
        panel = _prepare_panel(
            pd.DataFrame(
                {
                    "ИНН": ["0012345678"],
                    "Год": ["2024"],
                    "дефолт (по облигациям)": [""],
                    "рейтинг": ["AA(RU)"],
                }
            )
        )
        self.assertEqual(panel.loc[0, "inn"], "0012345678")
        self.assertTrue(pd.isna(panel.loc[0, "default"]))

    def test_training_pairs_only_use_the_following_calendar_year(self):
        panel = _prepare_panel(
            pd.DataFrame(
                {
                    "inn": ["1234567890", "1234567890", "1234567890"],
                    "year": [2021, 2022, 2024],
                    "default": [0, 1, 0],
                    "rating": ["AA", "A", "BBB"],
                    "sentiment_score": [-0.1, -0.2, 0.3],
                }
            )
        )
        features, targets = _next_year_pairs(panel)
        self.assertEqual(targets["feature_year"].tolist(), [2021])
        self.assertEqual(targets["target_year"].tolist(), [2022])
        self.assertEqual(targets["default"].tolist(), [1])
        self.assertEqual(features.loc[0, "sentiment_score"], -0.1)

    def test_training_labels_after_forecast_cutoff_are_excluded(self):
        panel = _prepare_panel(
            pd.DataFrame(
                {
                    "inn": ["1234567890"] * 3,
                    "year": [2020, 2021, 2022],
                    "default": [0, 1, 0],
                }
            )
        )
        features, targets = _next_year_pairs(panel)
        _, available_targets = _training_pairs_as_of(features, targets, 2021)
        self.assertEqual(available_targets["target_year"].tolist(), [2021])

    def test_insufficient_labels_produce_status_instead_of_fake_prediction(self):
        panel = pd.DataFrame(
            {
                "ИНН": ["0012345678", "0012345678"],
                "Год": [2023, 2024],
                "дефолт (по облигациям)": [0, 0],
                "рейтинг": ["AA", "A"],
                "Тональность отчёта (RuBERT, positive - negative)": [0.2, -0.1],
            }
        )
        forecast = generate_forecasts(panel)
        self.assertEqual(len(forecast), 1)
        self.assertTrue(
            np.isnan(forecast.loc[0, "estimated_default_probability"])
        )
        self.assertEqual(forecast.loc[0, "predicted_rating"], "")
        self.assertIn(
            "недостаточно данных", forecast.loc[0, "default_model_status"]
        )
        self.assertIn(
            "недостаточно данных", forecast.loc[0, "rating_model_status"]
        )
        self.assertEqual(forecast.loc[0, "forecast_year"], 2025)

    def test_missing_feature_columns_are_created_as_nan(self):
        panel = _prepare_panel(pd.DataFrame({"inn": ["1234567890"], "year": [2024]}))
        self.assertTrue(all(feature in panel.columns for feature in FEATURES))
        self.assertTrue(pd.isna(panel.loc[0, "sentiment_score"]))

    def test_invalid_binary_default_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "вне бинарного диапазона"):
            _prepare_panel(
                pd.DataFrame(
                    {"inn": ["1234567890"], "year": [2024], "default": [2]}
                )
            )

    def test_models_train_when_history_has_enough_labels(self):
        records = []
        for company_index in range(40):
            inn = f"{company_index + 1000000000:010d}"
            for year in (2020, 2021, 2022):
                default = int(company_index < 12)
                if year == 2022:
                    default = int(12 <= company_index < 24)
                rating = ("AAA", "BBB", "B")[company_index % 3]
                records.append(
                    {
                        "inn": inn,
                        "year": year,
                        "default": default,
                        "rating": rating,
                        "sentiment_score": (company_index % 10) / 10,
                    }
                )

        forecast = generate_forecasts(pd.DataFrame(records))
        self.assertEqual(len(forecast), 40)
        self.assertTrue(forecast["estimated_default_probability"].between(0, 1).all())
        self.assertTrue(forecast["predicted_rating"].isin(
            ("AAA", "BBB-", "BBB", "BBB+", "B+", "B", "B-")
        ).all())
        self.assertTrue(forecast["default_model_status"].eq("ok").all())
        self.assertTrue(forecast["rating_model_status"].eq("ok").all())


if __name__ == "__main__":
    unittest.main()
