"""Text quality, diversity, calibration, and seed summary metrics."""

from __future__ import annotations

import re
import string
from functools import lru_cache
from numbers import Integral

import numpy as np
from nltk.translate.bleu_score import SmoothingFunction, sentence_bleu
from rouge_score import rouge_scorer


@lru_cache(maxsize=1)
def _rouge_scorer() -> rouge_scorer.RougeScorer:
    return rouge_scorer.RougeScorer(["rougeL"], use_stemmer=True)


def normalize_answer(s: str) -> str:
    """Lowercase, remove ASCII punctuation and English articles, and collapse spaces."""
    s = s.lower().translate(str.maketrans("", "", string.punctuation))
    return " ".join(re.sub(r"\b(a|an|the)\b", " ", s).split())


def rouge_l(prediction: str, references: list[str]) -> float:
    """Return the maximum normalized, stemmed ROUGE-L F-measure in percent."""
    prediction = normalize_answer(prediction)
    refs = [normalize_answer(ref) for ref in references]
    refs = [ref for ref in refs if ref]
    if not prediction or not refs:
        return 0.0
    scorer = _rouge_scorer()
    return 100.0 * max(scorer.score(ref, prediction)["rougeL"].fmeasure for ref in refs)


def corpus_rouge_l(predictions: list[str], references: list[list[str]]) -> float:
    """Return mean ROUGE-L in percent for a nonempty, aligned corpus."""
    if len(predictions) != len(references) or not predictions:
        raise ValueError("predictions and references must have equal, nonzero lengths")
    return float(np.mean([rouge_l(p, r) for p, r in zip(predictions, references)]))


def _validate_positive_integer(value: int, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, Integral) or value < 1:
        raise ValueError(f"{name} must be a positive integer")


def _ngram_counts(texts: list[str], n: int) -> tuple[int, int]:
    unique: set[tuple[str, ...]] = set()
    total = 0
    for text in texts:
        tokens = normalize_answer(text).split()
        for i in range(len(tokens) - n + 1):
            unique.add(tuple(tokens[i:i + n]))
            total += 1
    return len(unique), total


def distinct_n(texts: list[str], n: int) -> float:
    """Return unique divided by total normalized n-grams across texts."""
    _validate_positive_integer(n, "n")
    unique, total = _ngram_counts(texts, n)
    return unique / total if total else 0.0


def distinct_n_per_prompt(samples: list[list[str]], n: int) -> float:
    """Return mean distinct-n over prompts with at least one n-gram."""
    _validate_positive_integer(n, "n")
    scores = []
    for group in samples:
        unique, total = _ngram_counts(group, n)
        if total:
            scores.append(unique / total)
    return float(np.mean(scores)) if scores else 0.0


def self_bleu(group: list[str]) -> float:
    """Return mean leave-one-out smoothed BLEU-4 in percent, or NaN if undefined."""
    tokens = [normalize_answer(text).split() for text in group]
    tokens = [sample for sample in tokens if sample]
    if len(tokens) < 2:
        return float("nan")
    smoothing = SmoothingFunction().method1
    scores = [
        sentence_bleu(
            tokens[:j] + tokens[j + 1:],
            hypothesis,
            weights=(0.25, 0.25, 0.25, 0.25),
            smoothing_function=smoothing,
        )
        for j, hypothesis in enumerate(tokens)
    ]
    return 100.0 * float(np.mean(scores))


def _mean_without_nan(values: list[float]) -> float:
    valid = [value for value in values if not np.isnan(value)]
    return float(np.mean(valid)) if valid else float("nan")


def mean_self_bleu(samples: list[list[str]]) -> float:
    """Return mean prompt self-BLEU ignoring NaN, or NaN if none are defined."""
    return _mean_without_nan([self_bleu(group) for group in samples])


def semantic_diversity(embeddings: np.ndarray) -> float:
    """Return mean unordered-pair cosine distance after dropping zero rows."""
    rows = np.asarray(embeddings, dtype=float)
    if rows.ndim != 2:
        raise ValueError("embeddings must have shape (K, d)")
    if not np.all(np.isfinite(rows)):
        raise ValueError("embeddings must be finite")
    if rows.shape[1] == 0:
        return float("nan")
    # Scaling first avoids overflow or underflow when computing row norms.
    scales = np.max(np.abs(rows), axis=1)
    rows = rows[scales > 0] / scales[scales > 0, None]
    if len(rows) < 2:
        return float("nan")
    rows = rows / np.linalg.norm(rows, axis=1, keepdims=True)
    distances = [
        1.0 - float(np.clip(np.dot(rows[i], rows[j]), -1.0, 1.0))
        for i in range(len(rows))
        for j in range(i + 1, len(rows))
    ]
    return float(np.mean(distances))


def mean_semantic_diversity(groups: list[np.ndarray]) -> float:
    """Return mean prompt cosine diversity ignoring NaN, or NaN if undefined."""
    return _mean_without_nan([semantic_diversity(group) for group in groups])


def expected_calibration_error(
    confidences: np.ndarray, correct: np.ndarray, n_bins: int = 15
) -> float:
    """Return top-label ECE as a fraction using right-closed equal-width bins."""
    _validate_positive_integer(n_bins, "n_bins")
    confidences = np.asarray(confidences, dtype=float)
    correct = np.asarray(correct)
    if confidences.ndim != 1 or correct.ndim != 1:
        raise ValueError("confidences and correct must be one-dimensional")
    if len(confidences) != len(correct) or len(confidences) == 0:
        raise ValueError("confidences and correct must have equal, nonzero lengths")
    if not np.all(np.isfinite(confidences)) or np.any(
        (confidences < 0) | (confidences > 1)
    ):
        raise ValueError("confidences must be finite and in [0, 1]")
    if not np.all((correct == 0) | (correct == 1)):
        raise ValueError("correct must contain only bool or 0/1 values")
    upper_edges = np.arange(1, n_bins + 1, dtype=float) / n_bins
    bins = np.searchsorted(upper_edges, confidences, side="left")
    ece = 0.0
    for i in range(n_bins):
        mask = bins == i
        if np.any(mask):
            ece += float(np.mean(mask)) * abs(
                float(np.mean(correct[mask])) - float(np.mean(confidences[mask]))
            )
    return ece


def summarize_seeds(values: list[float]) -> tuple[float, float, int]:
    """Return mean, sample standard deviation, and count after removing NaN."""
    valid = np.asarray(values, dtype=float)
    valid = valid[~np.isnan(valid)]
    n = len(valid)
    mean = float(np.mean(valid)) if n else float("nan")
    std = float(np.std(valid, ddof=1)) if n >= 2 else float("nan")
    return mean, std, n
