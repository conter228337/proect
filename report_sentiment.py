"""Извлечение текста публичных отчётов и оценка тональности моделью RuBERT.

Дополнительные зависимости для этого этапа: transformers, torch и pymupdf.
Модель загружается лениво при первом отчёте; её имя можно переопределить
переменной окружения SENTIMENT_MODEL.
"""

from __future__ import annotations

import io
import math
import os
import re
import zipfile
from pathlib import Path

MODEL_NAME = os.environ.get(
    "SENTIMENT_MODEL", "blanchefort/rubert-base-cased-sentiment"
)
MAX_REPORT_CHARS = 100_000
MAX_CHUNK_CHARS = 1_800
MAX_PDF_PAGES = 200
MAX_ARCHIVE_MEMBER_BYTES = 50 * 1024 * 1024
MAX_ARCHIVE_TOTAL_BYTES = 100 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 500
MAX_ARCHIVE_DEPTH = 3

_classifier = None


def _get_classifier():
    global _classifier
    if _classifier is None:
        try:
            from transformers import pipeline
        except ImportError as exc:
            raise RuntimeError(
                "Для анализа тональности установите зависимости: "
                "pip install transformers torch pymupdf"
            ) from exc
        _classifier = pipeline(
            "text-classification",
            model=MODEL_NAME,
            tokenizer=MODEL_NAME,
            device=-1,
            top_k=None,
        )
    return _classifier


def split_text(text: str, max_chars: int = MAX_CHUNK_CHARS) -> list[str]:
    """Разбить текст на непустые фрагменты, не превышающие max_chars."""
    if max_chars < 1:
        raise ValueError("max_chars должен быть положительным")
    text = re.sub(r"\s+", " ", text or "").strip()
    chunks = []
    while text:
        if len(text) <= max_chars:
            chunks.append(text)
            break
        end = text.rfind(" ", 0, max_chars + 1)
        if end <= 0:
            end = max_chars
        chunks.append(text[:end].strip())
        text = text[end:].strip()
    return [chunk for chunk in chunks if chunk]


def _extract_pdf(data: bytes) -> str:
    try:
        import fitz
    except ImportError as exc:
        raise RuntimeError(
            "Для чтения PDF установите PyMuPDF: pip install pymupdf"
        ) from exc

    text = []
    with fitz.open(stream=data, filetype="pdf") as document:
        for page in document[:MAX_PDF_PAGES]:
            text.append(page.get_text())
    return "\n".join(text)


def _extract_bytes(data: bytes, name: str, depth: int = 0) -> str:
    suffix = Path(name).suffix.lower()
    if data.startswith(b"%PDF-") or suffix == ".pdf":
        return _extract_pdf(data)
    if data.startswith(b"PK\x03\x04") or suffix == ".zip":
        if depth >= MAX_ARCHIVE_DEPTH:
            raise ValueError(f"Превышена глубина вложенных архивов: {name}")
        chunks = []
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            members = archive.infolist()
            if len(members) > MAX_ARCHIVE_MEMBERS:
                raise ValueError(f"Слишком много файлов в архиве: {name}")
            total_size = sum(member.file_size for member in members if not member.is_dir())
            if total_size > MAX_ARCHIVE_TOTAL_BYTES:
                raise ValueError(f"Слишком большой распакованный архив: {name}")
            for member in members:
                if member.is_dir():
                    continue
                if member.file_size > MAX_ARCHIVE_MEMBER_BYTES:
                    raise ValueError(
                        f"Слишком большой файл в архиве: {member.filename}"
                    )
                member_suffix = Path(member.filename).suffix.lower()
                if member_suffix in {".pdf", ".zip", ".txt", ".html", ".htm"}:
                    member_data = archive.read(member)
                    chunks.append(
                        _extract_bytes(member_data, member.filename, depth + 1)
                    )
        return "\n".join(chunks)
    if suffix in {".txt", ".html", ".htm"}:
        return data.decode("utf-8-sig", errors="replace")
    return ""


def extract_report_text(paths: list[str | Path]) -> str:
    """Извлечь доступный текст из PDF/ZIP/TXT файлов; OCR изображений не выполняется."""
    chunks = []
    total_chars = 0
    for path_value in paths:
        if not path_value:
            continue
        path = Path(path_value)
        if not path.is_file():
            raise FileNotFoundError(f"Файл отчёта не найден: {path}")
        if path.stat().st_size > MAX_ARCHIVE_MEMBER_BYTES:
            raise ValueError(f"Файл отчёта больше 50 МБ: {path}")
        text = _extract_bytes(path.read_bytes(), path.name)
        if text:
            remaining = MAX_REPORT_CHARS - total_chars
            if remaining <= 0:
                break
            text = text[:remaining]
            chunks.append(text)
            total_chars += len(text)
    return "\n".join(chunks).strip()


def _normalized_scores(output) -> tuple[float, float, float]:
    """Преобразовать ответ Transformers в (positive, neutral, negative)."""
    if isinstance(output, dict):
        output = [output]
    scores = {"positive": 0.0, "neutral": 0.0, "negative": 0.0}
    for item in output:
        label = str(item.get("label", "")).lower()
        if "pos" in label:
            key = "positive"
        elif "neg" in label:
            key = "negative"
        elif "neu" in label:
            key = "neutral"
        else:
            raise ValueError(
                f"Модель {MODEL_NAME!r} вернула неизвестный класс тональности: "
                f"{item.get('label')!r}"
            )
        score = float(item["score"])
        if not math.isfinite(score) or score < 0:
            raise ValueError(f"Некорректная оценка тональности: {score!r}")
        scores[key] = score
    total = sum(scores.values())
    if total <= 0:
        raise ValueError("Модель не вернула вероятности классов тональности")
    return tuple(scores[key] / total for key in ("positive", "neutral", "negative"))


def analyze_report_text(text: str, classifier=None) -> dict[str, float | int | str]:
    """Оценить среднюю тональность фрагментов отчёта."""
    chunks = split_text(text)
    empty = {
        "sentiment_score": math.nan,
        "sentiment_positive": math.nan,
        "sentiment_neutral": math.nan,
        "sentiment_negative": math.nan,
        "sentiment_chunks": 0,
        "sentiment_text_chars": len(text or ""),
        "sentiment_model": MODEL_NAME,
        "sentiment_status": (
            "no extractable text" if text else "no report text"
        ),
    }
    if not chunks:
        return empty

    model = classifier or _get_classifier()
    outputs = model(
        chunks,
        batch_size=8,
        truncation=True,
        max_length=512,
        top_k=None,
    )
    if isinstance(outputs, dict):
        outputs = [outputs]
    if len(outputs) != len(chunks):
        raise ValueError(
            f"Модель обработала {len(outputs)} фрагментов из {len(chunks)}"
        )
    probabilities = [_normalized_scores(output) for output in outputs]
    positive = sum(row[0] for row in probabilities) / len(probabilities)
    neutral = sum(row[1] for row in probabilities) / len(probabilities)
    negative = sum(row[2] for row in probabilities) / len(probabilities)
    return {
        "sentiment_score": positive - negative,
        "sentiment_positive": positive,
        "sentiment_neutral": neutral,
        "sentiment_negative": negative,
        "sentiment_chunks": len(chunks),
        "sentiment_text_chars": len(text),
        "sentiment_model": MODEL_NAME,
        "sentiment_status": (
            "ok; text truncated at 100000 chars"
            if len(text) >= MAX_REPORT_CHARS
            else "ok"
        ),
    }


def analyze_report_paths(
    paths: list[str | Path], classifier=None
) -> dict[str, float | int | str]:
    """Извлечь текст перечисленных документов и получить оценку RuBERT."""
    text = extract_report_text(paths)
    result = analyze_report_text(text, classifier=classifier)
    result["sentiment_document_count"] = sum(bool(path) for path in paths)
    if not paths:
        result["sentiment_status"] = "no report documents"
    elif not text:
        result["sentiment_status"] = (
            "no extractable text; scanned documents require OCR"
        )
    return result
