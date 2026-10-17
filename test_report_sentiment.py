import tempfile
import unittest
from pathlib import Path

from report_sentiment import (
    analyze_report_paths,
    analyze_report_text,
    split_text,
)


class StubClassifier:
    def __call__(self, chunks, **kwargs):
        return [
            [
                {"label": "positive", "score": 0.7},
                {"label": "neutral", "score": 0.2},
                {"label": "negative", "score": 0.1},
            ]
            for _ in chunks
        ]


class ReportSentimentTests(unittest.TestCase):
    def test_split_text_respects_limit(self):
        chunks = split_text("слово " * 20, max_chars=17)
        self.assertTrue(chunks)
        self.assertTrue(all(len(chunk) <= 17 for chunk in chunks))
        self.assertEqual(" ".join(chunks).split(), ("слово " * 20).split())

    def test_analyze_text_averages_class_scores(self):
        result = analyze_report_text("Компания показывает рост.", StubClassifier())
        self.assertAlmostEqual(result["sentiment_score"], 0.6)
        self.assertAlmostEqual(result["sentiment_positive"], 0.7)
        self.assertAlmostEqual(result["sentiment_neutral"], 0.2)
        self.assertAlmostEqual(result["sentiment_negative"], 0.1)
        self.assertEqual(result["sentiment_chunks"], 1)

    def test_empty_text_does_not_load_model(self):
        result = analyze_report_text(" \n\t ")
        self.assertEqual(result["sentiment_chunks"], 0)
        self.assertEqual(result["sentiment_text_chars"], 4)

    def test_text_file_is_read_and_document_count_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            report = Path(directory) / "report.txt"
            report.write_text("Рост выручки.", encoding="utf-8")
            result = analyze_report_paths([report], StubClassifier())
        self.assertEqual(result["sentiment_document_count"], 1)
        self.assertGreater(result["sentiment_text_chars"], 0)

    def test_unknown_model_label_is_explicit_error(self):
        classifier = lambda chunks, **kwargs: [
            [{"label": "LABEL_0", "score": 1.0}] for _ in chunks
        ]
        with self.assertRaisesRegex(ValueError, "неизвестный класс"):
            analyze_report_text("Отчёт.", classifier)


if __name__ == "__main__":
    unittest.main()
