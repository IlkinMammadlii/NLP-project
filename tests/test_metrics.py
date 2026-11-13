"""Pytest-compatible metric tests, also runnable directly without pytest."""

import math
from pathlib import Path
import sys

# Prevent direct execution from creating bytecode files in the repository.
sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np

from cachekd import metrics


def test_normalize_answer() -> None:
    assert metrics.normalize_answer("  The, CAT! ate an apple; a pear.  ") == "cat ate apple pear"
    assert metrics.normalize_answer("another theatre A an THE") == "another theatre"
    assert metrics.normalize_answer("x-y can't\tstop\nnow") == "xy cant stop now"


def test_rouge_l() -> None:
    assert math.isclose(metrics.rouge_l("The red fox!", ["red fox"]), 100.0)
    assert metrics.rouge_l("red fox", ["blue whale"]) == 0.0
    assert math.isclose(metrics.rouge_l("red fox", ["blue whale", "red fox"]), 100.0)
    assert math.isclose(metrics.rouge_l("cats", ["cat"]), 100.0)
    assert metrics.rouge_l("", ["fox"]) == 0.0
    assert metrics.rouge_l("the!", ["fox"]) == 0.0
    assert metrics.rouge_l("fox", ["", "the!"]) == 0.0
    assert metrics.rouge_l("fox", []) == 0.0


def test_corpus_rouge_l() -> None:
    result = metrics.corpus_rouge_l(["red fox", "blue whale"], [["red fox"], ["green bird"]])
    assert math.isclose(result, 50.0)
    for predictions, references in [([], []), (["fox"], []), ([], [["fox"]])]:
        try:
            metrics.corpus_rouge_l(predictions, references)
        except ValueError:
            pass
        else:
            raise AssertionError("Expected ValueError for empty or mismatched corpus")


def test_distinct_n() -> None:
    texts = ["x y x y", "x y z"]
    assert math.isclose(metrics.distinct_n(texts, 1), 3 / 7)
    assert math.isclose(metrics.distinct_n(texts, 2), 3 / 5)
    assert metrics.distinct_n(["x", "y"], 2) == 0.0
    assert metrics.distinct_n(["the a an"], 1) == 0.0
    assert metrics.distinct_n([], 1) == 0.0
    assert metrics.distinct_n(texts, 5) == 0.0


def test_distinct_n_per_prompt() -> None:
    samples = [["x y x y", "x y z"], ["q r"], [], ["the"]]
    assert math.isclose(metrics.distinct_n_per_prompt(samples, 1), (3 / 7 + 1) / 2)
    assert math.isclose(metrics.distinct_n_per_prompt(samples, 2), (3 / 5 + 1) / 2)
    assert metrics.distinct_n_per_prompt([["x"], ["the"], []], 2) == 0.0
    assert metrics.distinct_n_per_prompt([], 1) == 0.0


def test_invalid_ngram_sizes() -> None:
    for n in [0, -1, 1.5, True]:
        for function in [metrics.distinct_n, metrics.distinct_n_per_prompt]:
            try:
                function([], n)
            except ValueError:
                pass
            else:
                raise AssertionError("Expected ValueError for invalid n")


def test_self_bleu() -> None:
    sentence = "red fox jumps over green logs"
    assert math.isclose(metrics.self_bleu([sentence] * 3), 100.0)
    assert metrics.self_bleu(["red fox jumps high", "blue whale swims deep"]) < 5.0
    assert math.isnan(metrics.self_bleu([sentence]))
    assert math.isnan(metrics.self_bleu([]))
    assert math.isnan(metrics.self_bleu([sentence, "the!", ""]))
    assert math.isclose(metrics.self_bleu([sentence, "", sentence, "a an"]), 100.0)
    # Uniform BLEU-4 weights still apply to samples shorter than four tokens.
    assert 0 < metrics.self_bleu(["fox", "fox"]) < 100


def test_mean_self_bleu() -> None:
    same = ["red fox jumps high"] * 2
    disjoint = ["red fox jumps high", "blue whale swims deep"]
    expected = (metrics.self_bleu(same) + metrics.self_bleu(disjoint)) / 2
    assert math.isclose(metrics.mean_self_bleu([same, [], disjoint, ["single"]]), expected)
    assert math.isnan(metrics.mean_self_bleu([[], ["single"]]))
    assert math.isnan(metrics.mean_self_bleu([]))


def test_semantic_diversity() -> None:
    assert math.isclose(metrics.semantic_diversity(np.array([[2, 0], [8, 0]])), 0, abs_tol=1e-12)
    assert math.isclose(metrics.semantic_diversity(np.eye(2)), 1)
    assert math.isclose(metrics.semantic_diversity(np.array([[3, 0], [-2, 0]])), 2)
    assert math.isclose(metrics.semantic_diversity(np.array([[0, 0], [2, 0], [0, 4]])), 1)
    rows = np.array([[1, 0], [0, 1], [-1, 0]])
    assert math.isclose(metrics.semantic_diversity(rows), 4 / 3)
    assert math.isnan(metrics.semantic_diversity(np.zeros((3, 2))))
    assert math.isnan(metrics.semantic_diversity(np.array([[1, 0], [0, 0]])))
    assert math.isnan(metrics.semantic_diversity(np.empty((0, 2))))
    assert math.isnan(metrics.semantic_diversity(np.empty((2, 0))))
    assert math.isclose(metrics.semantic_diversity(np.array([[1e300, 0], [0, 1e-300]])), 1)


def test_mean_semantic_diversity() -> None:
    opposite = np.array([[1, 0], [-1, 0]])
    assert math.isclose(metrics.mean_semantic_diversity([np.eye(2), np.zeros((2, 2)), opposite]), 1.5)
    assert math.isnan(metrics.mean_semantic_diversity([np.zeros((2, 2))]))
    assert math.isnan(metrics.mean_semantic_diversity([]))


def test_expected_calibration_error() -> None:
    ece = metrics.expected_calibration_error
    assert ece(np.array([0, 0.5, 0.5, 1]), np.array([False, False, True, True])) == 0
    assert ece(np.ones(3), np.zeros(3)) == 1
    # Bin 0: accuracy 1/2, confidence 1/4; bin 1: accuracy 1/2, confidence 3/4.
    assert math.isclose(ece(np.array([0.1, 0.4, 0.6, 0.9]), np.array([0, 1, 1, 0]), 2), 0.25)
    # Zero and the upper boundary 0.5 share bin 0: 2/3 * 1/4 + 1/3 * 0.
    assert math.isclose(ece(np.array([0, 0.5, 1]), np.array([0, 1, 1]), 2), 1 / 6)
    assert math.isclose(ece(np.array([0, 1]), np.array([1, 0]), 1), 0)
    boundary = 1 / 3
    above = np.nextafter(boundary, 1.0)
    expected = (boundary + 1 - above) / 2
    assert math.isclose(ece(np.array([boundary, above]), np.array([0, 1]), 3), expected)


def test_invalid_calibration_inputs() -> None:
    cases = [
        ([], [], 2),
        ([0.5], [], 2),
        ([[0.5]], [1], 2),
        ([0.5], [[1]], 2),
        ([-0.1], [0], 2),
        ([1.1], [1], 2),
        ([float("nan")], [1], 2),
        ([float("inf")], [1], 2),
        ([0.5], [2], 2),
        ([0.5], [float("nan")], 2),
        ([0.5], [1], 0),
        ([0.5], [1], 1.5),
    ]
    for confidence, correct, n_bins in cases:
        try:
            metrics.expected_calibration_error(np.array(confidence), np.array(correct), n_bins)
        except ValueError:
            pass
        else:
            raise AssertionError("Expected ValueError for invalid calibration input")


def test_summarize_seeds() -> None:
    mean, std, n = metrics.summarize_seeds([1, float("nan"), 2, 3])
    assert mean == 2 and std == 1 and n == 3
    mean, std, n = metrics.summarize_seeds([float("nan"), 7])
    assert mean == 7 and math.isnan(std) and n == 1
    for values in [[], [float("nan")]]:
        mean, std, n = metrics.summarize_seeds(values)
        assert math.isnan(mean) and math.isnan(std) and n == 0


if __name__ == "__main__":
    for name, test in sorted(list(globals().items())):
        if name.startswith("test_") and callable(test):
            test()
    print("ALL METRIC TESTS PASSED")
