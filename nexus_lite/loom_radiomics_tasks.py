"""Radiomics tasks for the loom multi-agent example (pluggable via `--task NAME`).

Four tasks, each with 3-4 independent flat modules, IBSI-flavoured with unambiguous edge cases:
  radiomics-firstorder  discretisation, ROI masking + re-segmentation, first-order features
  radiomics-texture     2D GLCM + features, GLRLM + features (mask-aware)
  radiomics-stats       ICC(3,1), Benjamini-Hochberg q-values, train-only z-score, Harrell's C-index
  radiomics-suv         IBSI-SUV v3.1 body normalisation, decay/t_ave, unit conversion, IPP z-stacking

Every task object exposes NAME, SPEC, FILES, GOALS, DEFAULT_SPLIT, MOCK_FILES {file: (flawed, good)},
HIDDEN_TESTS {test_file: src}, HIDDEN_TOTAL. Every edge case the hidden tests check is stated in SPEC.
The hidden tests are written only to a directory outside the workers' sandbox and are never put in a
prompt. FLAWED = GOOD with two deliberately broken modules, so the mock review -> rework loop runs.
Stdlib only: images are nested python lists (2D or 3D), masks are 0/1 nested lists.
"""
from __future__ import annotations

from types import SimpleNamespace

_M = "# hidden-suite-marker-7f3a91\n"
_MATRIX_HELPERS = '''

def flat(m):
    return [x for r in m for x in r]


def close(a, b):
    return len(a) == len(b) and all(abs(x - y) < 1e-9 for x, y in zip(a, b))
'''


def _task(name, spec, goals, templates, flaws, hidden):
    """templates {file: src with @@TOKEN@@}, flaws {file: (token, good, bad)}: GOOD fills the good value,
    FLAWED fills the bad one for exactly the files named in `flaws`."""
    files = list(goals)
    assert set(templates) == set(files) and set(hidden) == {"test_h_" + f for f in files}
    mock = {}
    for f in files:
        good = templates[f]
        bad = good
        if f in flaws:
            tok, g, b = flaws[f]
            assert tok in good and g != b
            bad = good.replace(tok, b)
            good = good.replace(tok, g)
        assert "@@" not in good + bad
        mock[f] = (bad, good)
    tests = {k: _M + v for k, v in hidden.items()}
    return SimpleNamespace(
        NAME=name, SPEC=spec, FILES=files, GOALS=goals,
        DEFAULT_SPLIT=[{"file": f, "goal": goals[f]} for f in files],
        MOCK_FILES=mock, HIDDEN_TESTS=tests,
        HIDDEN_TOTAL=sum(src.count("\ndef test_") for src in tests.values()))


# ====================================================================== radiomics-firstorder
_FO_SPEC = (
    "Build four small, independent python modules, all flat in one directory (no packages, stdlib only). "
    "Images and masks are nested python lists (2D or 3D); masks hold only 0 and 1.\n"
    "1) discretise.py: fixed_bin_width(values, width, minimum) returns the 1-based bin index of every value; "
    "bin k covers [minimum + (k-1)*width, minimum + k*width), lower edge inclusive and upper edge exclusive, so the index is "
    "floor((x - minimum) / width) + 1 (a value exactly on an edge belongs to the higher bin). width <= 0 raises "
    "ValueError, and so does any value below minimum; an empty list returns []. fixed_bin_number(values, n_bins) uses "
    "the range of the given values: lo = min, hi = max, index = floor(n_bins * (x - lo) / (hi - lo)) + 1, except that the "
    "maximum value goes into the LAST bin n_bins (never n_bins + 1). If hi == lo every value gets bin 1. n_bins < 1 "
    "or empty values raise ValueError. bin_edges(minimum, width, n_bins) returns the n_bins + 1 edges "
    "[minimum + i*width for i in 0..n_bins]; width <= 0 or n_bins < 1 raise ValueError.\n"
    "2) roi.py: shape_of(nested) returns the tuple of dimensions (a scalar gives (), a 3D list (2, 2, 2) etc.); an empty list "
    "or a ragged list raises ValueError. extract_roi(image, mask) returns the image values where mask == 1, flattened in "
    "row-major (C) order, as a list. Different shapes (any dimension, including 2D vs 3D) raise ValueError, a mask value that "
    "is not the int 0 or 1 (bools and floats included) raises ValueError, and a mask with no 1 (empty ROI) raises ValueError. resegment(values, lo=None, hi=None) "
    "keeps the values with lo <= v <= hi, BOTH ends inclusive, preserving order; None means that side is unbounded; "
    "lo > hi raises ValueError; an empty result is fine (returns []).\n"
    "3) moments.py: functions over a non-empty list of numbers; an empty list raises ValueError in every one. "
    "mean(values); variance(values) is the POPULATION variance (divide by n); skewness(values) = m3 / m2**1.5 and "
    "kurtosis_excess(values) = m4 / m2**2 - 3, where mk is the k-th central moment divided by n; if the variance is 0 "
    "both skewness and kurtosis_excess return 0.0; energy(values) = sum of x**2 (no offset).\n"
    "4) distribution.py: entropy(bins) takes a list of integer bin indices and returns -sum(p * log2(p)) over the occupied "
    "bins, p = count / n (a single occupied bin gives 0.0); uniformity(bins) = sum(p**2); both raise ValueError on an empty "
    "list. percentile(values, p) uses linear interpolation on the sorted values: rank = p * (n - 1) / 100, interpolate "
    "between the two neighbouring sorted values (n == 1 returns that value); p outside [0, 100] or empty values raise "
    "ValueError. robust_mad(values) is the robust mean absolute deviation: keep the values x with P10 <= x <= P90 "
    "(both ends inclusive, percentile as defined above), then return the mean of |x - m| over the kept values where m "
    "is the mean of the kept values; empty values raise ValueError."
)
_FO_GOALS = {
    "discretise.py": "fixed_bin_width, fixed_bin_number (max value in the last bin), bin_edges",
    "roi.py": "shape_of, extract_roi (row-major, ValueError rules), resegment (inclusive range)",
    "moments.py": "mean, population variance, skewness, kurtosis_excess, energy (zero-variance rule)",
    "distribution.py": "entropy (log2), uniformity, percentile (linear), robust_mad (P10-P90 inclusive)",
}
_FO_TEMPLATES = {
    "discretise.py": '''import math


def fixed_bin_width(values, width, minimum):
    if not width > 0:
        raise ValueError("width must be > 0")
    out = []
    for x in values:
        if x < minimum:
            raise ValueError("value below minimum")
        out.append(int(math.floor((x - minimum) / width)) + 1)
    return out


def fixed_bin_number(values, n_bins):
    if n_bins < 1:
        raise ValueError("n_bins < 1")
    if len(values) == 0:
        raise ValueError("empty values")
    lo, hi = min(values), max(values)
    if hi == lo:
        return [1] * len(values)
    return [@@CLAMP@@ for x in values]


def bin_edges(minimum, width, n_bins):
    if not width > 0 or n_bins < 1:
        raise ValueError("bad width or n_bins")
    return [minimum + i * width for i in range(n_bins + 1)]
''',
    "roi.py": '''def shape_of(nested):
    if isinstance(nested, (list, tuple)):
        if len(nested) == 0:
            raise ValueError("empty")
        subs = [shape_of(x) for x in nested]
        if any(s != subs[0] for s in subs):
            raise ValueError("ragged")
        return (len(nested),) + subs[0]
    return ()


def _flat(nested):
    if isinstance(nested, (list, tuple)):
        out = []
        for x in nested:
            out.extend(_flat(x))
        return out
    return [nested]


def extract_roi(image, mask):
    if shape_of(image) != shape_of(mask):
        raise ValueError("shape mismatch")
    vals, m = _flat(image), _flat(mask)
    if any(not isinstance(k, int) or isinstance(k, bool) or k not in (0, 1) for k in m):
        raise ValueError("mask must hold only the ints 0 and 1")
    roi = [v for v, k in zip(vals, m) if k == 1]
    if not roi:
        raise ValueError("empty ROI")
    return roi


def resegment(values, lo=None, hi=None):
    if lo is not None and hi is not None and lo > hi:
        raise ValueError("lo > hi")
    return [v for v in values if (lo is None or v >= lo) and (hi is None or v <= hi)]
''',
    "moments.py": '''def _need(values):
    if len(values) == 0:
        raise ValueError("empty values")


def mean(values):
    _need(values)
    return sum(values) / len(values)


def variance(values):
    _need(values)
    mu = sum(values) / len(values)
    return sum((x - mu) ** 2 for x in values) / @@DIV@@


def _central(values, k):
    mu = sum(values) / len(values)
    return sum((x - mu) ** k for x in values) / len(values)


def skewness(values):
    _need(values)
    m2 = _central(values, 2)
    if m2 == 0:
        return 0.0
    return _central(values, 3) / m2 ** 1.5


def kurtosis_excess(values):
    _need(values)
    m2 = _central(values, 2)
    if m2 == 0:
        return 0.0
    return _central(values, 4) / m2 ** 2 - 3


def energy(values):
    _need(values)
    return sum(x * x for x in values)
''',
    "distribution.py": '''import math
from collections import Counter


def _probs(bins):
    if len(bins) == 0:
        raise ValueError("empty bins")
    n = len(bins)
    return [c / n for c in Counter(bins).values()]


def entropy(bins):
    return -sum(p * math.log2(p) for p in _probs(bins)) + 0.0


def uniformity(bins):
    return sum(p * p for p in _probs(bins))


def percentile(values, p):
    if len(values) == 0 or not 0 <= p <= 100:
        raise ValueError("bad values or p")
    s = sorted(values)
    r = p * (len(s) - 1) / 100
    lo = int(math.floor(r))
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (r - lo)


def robust_mad(values):
    p10, p90 = percentile(values, 10), percentile(values, 90)
    kept = [x for x in values if p10 <= x <= p90]
    m = sum(kept) / len(kept)
    return sum(abs(x - m) for x in kept) / len(kept)
''',
}
_FO_FLAWS = {
    "discretise.py": ("@@CLAMP@@",
                      "min(n_bins, int(math.floor(n_bins * (x - lo) / (hi - lo))) + 1)",
                      "int(math.floor(n_bins * (x - lo) / (hi - lo))) + 1"),
    "moments.py": ("@@DIV@@", "len(values)", "(len(values) - 1) or 1"),
}
_FO_HIDDEN = {
    "test_h_discretise.py": '''import pytest
from discretise import fixed_bin_width, fixed_bin_number, bin_edges


def test_width_edges_are_lower_inclusive():
    assert fixed_bin_width([-100, -76, -75, 0, 124, 125], 25, -100) == [1, 1, 2, 5, 9, 10]


def test_width_fractional():
    assert fixed_bin_width([0, 0.25, 0.5, 1.0, 1.49], 0.5, 0) == [1, 1, 2, 3, 3]
    assert fixed_bin_width([], 2, 0) == []


def test_width_errors():
    for width in (0, -1):
        with pytest.raises(ValueError):
            fixed_bin_width([1, 2], width, 0)
    with pytest.raises(ValueError):
        fixed_bin_width([5, -1], 2, 0)


def test_number_max_goes_to_last_bin():
    assert fixed_bin_number([1, 2, 3, 4, 5], 4) == [1, 2, 3, 4, 4]
    assert fixed_bin_number([1, 2, 3, 4, 5], 2) == [1, 1, 2, 2, 2]


def test_number_uses_value_range():
    assert fixed_bin_number([10, 20, 30], 3) == [1, 2, 3]
    assert fixed_bin_number([-4, 0, 4], 4) == [1, 3, 4]


def test_number_constant_values():
    assert fixed_bin_number([7, 7, 7], 5) == [1, 1, 1]


def test_number_errors():
    with pytest.raises(ValueError):
        fixed_bin_number([1, 2], 0)
    with pytest.raises(ValueError):
        fixed_bin_number([], 3)


def test_bin_edges():
    assert bin_edges(0, 0.5, 3) == [0, 0.5, 1.0, 1.5]
    assert bin_edges(-10, 5, 2) == [-10, -5, 0]
    for args in ((0, 0, 3), (0, 1, 0), (0, -1, 2)):
        with pytest.raises(ValueError):
            bin_edges(*args)
''',
    "test_h_roi.py": '''import pytest
from roi import shape_of, extract_roi, resegment


def test_extract_2d_row_major():
    assert extract_roi([[1, 2, 3], [4, 5, 6]], [[1, 0, 1], [0, 1, 0]]) == [1, 3, 5]


def test_extract_3d_row_major():
    img = [[[1, 2], [3, 4]], [[5, 6], [7, 8]]]
    msk = [[[0, 1], [0, 0]], [[1, 0], [0, 1]]]
    assert extract_roi(img, msk) == [2, 5, 8]


def test_shape_mismatch():
    with pytest.raises(ValueError):
        extract_roi([[1, 2, 3], [4, 5, 6]], [[1, 1], [1, 1]])
    with pytest.raises(ValueError):
        extract_roi([[1, 2], [3, 4]], [[[1, 1], [1, 1]]])


def test_empty_roi():
    with pytest.raises(ValueError):
        extract_roi([[1, 2], [3, 4]], [[0, 0], [0, 0]])


def test_mask_must_be_binary():
    with pytest.raises(ValueError):
        extract_roi([[1, 2], [3, 4]], [[1, 2], [0, 1]])


def test_mask_rejects_bools_and_floats():
    for bad in ([[True, False], [False, True]], [[1.0, 0.0], [0.0, 1.0]]):
        with pytest.raises(ValueError):
            extract_roi([[1, 2], [3, 4]], bad)


def test_shape_of():
    assert shape_of([[[1, 2], [3, 4]], [[5, 6], [7, 8]]]) == (2, 2, 2)
    assert shape_of([[1, 2, 3], [4, 5, 6]]) == (2, 3) and shape_of(5) == ()
    for bad in ([], [[1, 2], [3]], [[1], 2]):
        with pytest.raises(ValueError):
            shape_of(bad)


def test_resegment_both_ends_inclusive():
    assert resegment([1, 2, 3, 4, 5], 2, 4) == [2, 3, 4]
    assert resegment([5, 1, 4, 2, 3], 2, 4) == [4, 2, 3]


def test_resegment_open_bounds_empty_and_error():
    assert resegment([1, 2, 3, 4, 5], lo=3) == [3, 4, 5]
    assert resegment([1, 2, 3, 4, 5], hi=2) == [1, 2]
    assert resegment([1, 2, 3], 10, 20) == [] and resegment([1, 2]) == [1, 2]
    with pytest.raises(ValueError):
        resegment([1, 2], 3, 2)
''',
    "test_h_moments.py": '''import pytest
from moments import mean, variance, skewness, kurtosis_excess, energy


def test_mean():
    assert mean([1, 2, 3, 4, 10]) == 4.0 and mean([-2, 2]) == 0.0


def test_variance_is_population():
    assert variance([1, 2, 3, 4, 10]) == pytest.approx(10.0)
    assert variance([5]) == 0.0


def test_skewness():
    assert skewness([1, 2, 3, 4, 10]) == pytest.approx(36 / 10 ** 1.5)
    assert skewness([1, 2, 3]) == pytest.approx(0.0, abs=1e-12)


def test_kurtosis_excess():
    assert kurtosis_excess([1, 2, 3, 4, 10]) == pytest.approx(278.8 / 100 - 3)
    assert kurtosis_excess([-1, 1]) == pytest.approx(-2.0)


def test_zero_variance_rule():
    assert skewness([2, 2, 2, 2]) == 0.0 and kurtosis_excess([2, 2, 2, 2]) == 0.0
    assert skewness([9]) == 0.0 and kurtosis_excess([9]) == 0.0


def test_energy():
    assert energy([1, 2, 3, 4, 10]) == 130 and energy([-3, 4]) == 25


def test_empty_raises_everywhere():
    for fn in (mean, variance, skewness, kurtosis_excess, energy):
        with pytest.raises(ValueError):
            fn([])
''',
    "test_h_distribution.py": '''import pytest
from distribution import entropy, uniformity, percentile, robust_mad


def test_entropy_uniform_bins():
    assert entropy([1, 1, 2, 2]) == pytest.approx(1.0)
    assert entropy([1, 2, 3, 4]) == pytest.approx(2.0)


def test_entropy_skewed_and_single_bin():
    assert entropy([1, 1, 1, 2]) == pytest.approx(0.8112781244591328)
    assert entropy([3, 3, 3]) == 0.0


def test_uniformity():
    assert uniformity([1, 1, 2, 2]) == pytest.approx(0.5)
    assert uniformity([1, 2, 3, 4]) == pytest.approx(0.25)
    assert uniformity([5, 5]) == pytest.approx(1.0)


def test_entropy_empty():
    for fn in (entropy, uniformity):
        with pytest.raises(ValueError):
            fn([])


def test_percentile_linear():
    assert percentile([40, 10, 30, 20], 50) == pytest.approx(25.0)
    assert percentile([10, 20, 30, 40], 0) == 10 and percentile([10, 20, 30, 40], 100) == 40
    assert percentile([10, 20, 30, 40], 10) == pytest.approx(13.0)
    assert percentile([7], 33) == 7


def test_percentile_errors():
    for args in (([], 50), ([1, 2], -1), ([1, 2], 100.5)):
        with pytest.raises(ValueError):
            percentile(*args)


def test_robust_mad_inclusive_percentile_bounds():
    assert robust_mad(list(range(11))) == pytest.approx(20 / 9)


def test_robust_mad_drops_outliers():
    assert robust_mad([1, 2, 3, 4, 5, 6, 7, 8, 9, 1000]) == pytest.approx(2.0)


def test_robust_mad_constant_and_empty():
    assert robust_mad([5, 5, 5]) == 0.0
    with pytest.raises(ValueError):
        robust_mad([])
''',
}
FIRSTORDER = _task("radiomics-firstorder", _FO_SPEC, _FO_GOALS, _FO_TEMPLATES, _FO_FLAWS, _FO_HIDDEN)

# ====================================================================== radiomics-texture
_TX_SPEC = (
    "Build four small, independent python modules, all flat in one directory (no packages, stdlib only). "
    "A 2D image is a list of equal-length rows of integer gray levels 1..levels (already discretised); a mask is a list of "
    "rows of 0/1 with the same shape; `mask=None` means every pixel is inside. Pixel (r, c) is row r, column c; scanning is "
    "row-major.\n"
    "1) glcm.py: offset_for(angle, distance) returns the (drow, dcol) offset: angle 0 -> (0, d), 45 -> (-d, d), 90 -> (-d, 0), "
    "135 -> (-d, -d) (row numbers decrease upwards); any other angle or distance < 1 raises ValueError. "
    "glcm(image, levels, offset, mask=None, symmetric=True, normalise=True) returns a levels x levels list of lists: for every "
    "pixel (r, c) in row-major order whose neighbour (r + drow, c + dcol) lies inside the image, if BOTH pixels have mask 1, "
    "count one pair into cell [gray(r, c) - 1][gray(neighbour) - 1]. Pairs touching a masked-out pixel are never counted. "
    "Only gray levels at mask == 1 pixels are validated: each must be an int in 1..levels, otherwise ValueError (values at "
    "masked-out pixels are ignored, e.g. 0 background). symmetric=True then replaces the matrix M by M + M transposed "
    "(each pair counted in both directions). normalise=True divides every cell by the matrix total; a matrix with total 0 "
    "(no pairs) stays all zeros, no error. Empty or ragged image, mask of a different shape, levels < 1 or offset (0, 0) "
    "raise ValueError.\n"
    "2) glcm_features.py: functions of a normalised GLCM P (square, non-empty, cells sum to 1 within 1e-9, otherwise ValueError); "
    "i and j are the 1-based gray levels (row index + 1, column index + 1). contrast(P) = sum (i-j)**2 * P; homogeneity(P) "
    "(inverse difference moment) = sum P / (1 + (i-j)**2); energy(P) (angular second moment) = sum P**2; "
    "correlation(P) = sum (i - mu_i) * (j - mu_j) * P / (sigma_i * sigma_j) with mu and sigma the mean and population "
    "standard deviation of the row marginal (i) and column marginal (j); if sigma_i * sigma_j <= 1e-12 (all mass in one row or "
    "one column) return 1.0. features(P) returns a dict with the keys 'contrast', 'homogeneity', 'energy', 'correlation'.\n"
    "3) glrlm.py: glrlm(image, levels, direction, mask=None) returns a levels x max(nrows, ncols) list of lists of integer "
    "counts: cell [g - 1][L - 1] is the number of runs of gray level g and length L. A run is a maximal sequence of "
    "consecutive pixels along the direction that all have the same gray level and all have mask 1; a masked-out pixel ends a "
    "run and belongs to none. Directions: 'h' = along a row (step (0, 1)), 'v' = down a column (step (1, 0)), 'd' = down-right "
    "diagonal (step (1, 1)), 'a' = down-left anti-diagonal (step (1, -1)); anything else raises ValueError. Validation as in "
    "glcm (only mask == 1 pixels checked, mask shape, levels < 1, empty or ragged image raise ValueError).\n"
    "4) glrlm_features.py: functions of a run-length matrix R (list of equal-length rows, ragged raises ValueError); Nr is the "
    "total number of runs, Nr == 0 raises ValueError; row index i -> gray level, column index j -> run length j + 1. "
    "sre(R) = sum R[i][j] / (j+1)**2 / Nr; lre(R) = sum R[i][j] * (j+1)**2 / Nr; gln(R) = sum over rows of (row sum)**2 / Nr; "
    "rln(R) = sum over columns of (column sum)**2 / Nr; run_percentage(R, n_voxels) = Nr / n_voxels (n_voxels < 1 raises "
    "ValueError)."
)
_TX_GOALS = {
    "glcm.py": "offset_for and mask-aware glcm (symmetric, normalised, distance, 4 angles)",
    "glcm_features.py": "contrast, homogeneity, energy, correlation (zero-variance rule), features dict",
    "glrlm.py": "run-length matrix in directions h/v/d/a, mask-aware, levels x max(nrows,ncols)",
    "glrlm_features.py": "sre, lre, gln, rln, run_percentage",
}
_TX_TEMPLATES = {
    "glcm.py": '''_DIRS = {0: (0, 1), 45: @@A45@@, 90: (-1, 0), 135: (-1, -1)}


def offset_for(angle, distance):
    if angle not in _DIRS:
        raise ValueError("angle must be 0, 45, 90 or 135")
    if distance < 1:
        raise ValueError("distance < 1")
    dr, dc = _DIRS[angle]
    return (dr * distance, dc * distance)


def _check(image, levels, mask):
    if levels < 1 or len(image) == 0:
        raise ValueError("bad levels or empty image")
    nr, nc = len(image), len(image[0])
    if nc == 0 or any(len(r) != nc for r in image):
        raise ValueError("ragged or empty image")
    if mask is None:
        mask = [[1] * nc for _ in range(nr)]
    elif len(mask) != nr or any(len(r) != nc for r in mask):
        raise ValueError("mask shape mismatch")
    for r in range(nr):
        for c in range(nc):
            if mask[r][c] == 1:
                v = image[r][c]
                if not isinstance(v, int) or isinstance(v, bool) or not 1 <= v <= levels:
                    raise ValueError("gray level out of range")
    return nr, nc, mask


def glcm(image, levels, offset, mask=None, symmetric=True, normalise=True):
    nr, nc, mask = _check(image, levels, mask)
    dr, dc = offset
    if (dr, dc) == (0, 0):
        raise ValueError("zero offset")
    m = [[0] * levels for _ in range(levels)]
    for r in range(nr):
        for c in range(nc):
            r2, c2 = r + dr, c + dc
            if 0 <= r2 < nr and 0 <= c2 < nc and mask[r][c] == 1 and mask[r2][c2] == 1:
                m[image[r][c] - 1][image[r2][c2] - 1] += 1
    if symmetric:
        m = [[m[i][j] + m[j][i] for j in range(levels)] for i in range(levels)]
    if normalise:
        total = sum(sum(row) for row in m)
        if total > 0:
            m = [[x / total for x in row] for row in m]
    return m
''',
    "glcm_features.py": '''import math


def _check(p):
    n = len(p)
    if n == 0 or any(len(r) != n for r in p):
        raise ValueError("matrix must be square and non-empty")
    if abs(sum(sum(r) for r in p) - 1.0) > 1e-9:
        raise ValueError("matrix must sum to 1")


def contrast(p):
    _check(p)
    return sum((i - j) ** 2 * x for i, r in enumerate(p) for j, x in enumerate(r))


def homogeneity(p):
    _check(p)
    return sum(x / (1 + (i - j) ** 2) for i, r in enumerate(p) for j, x in enumerate(r))


def energy(p):
    _check(p)
    return sum(x * x for r in p for x in r)


def correlation(p):
    _check(p)
    n = len(p)
    pi = [sum(p[i]) for i in range(n)]
    pj = [sum(p[i][j] for i in range(n)) for j in range(n)]
    mi = sum((i + 1) * pi[i] for i in range(n))
    mj = sum((j + 1) * pj[j] for j in range(n))
    si = math.sqrt(sum((i + 1 - mi) ** 2 * pi[i] for i in range(n)))
    sj = math.sqrt(sum((j + 1 - mj) ** 2 * pj[j] for j in range(n)))
    if si * sj <= 1e-12:
        return 1.0
    return sum((i + 1 - mi) * (j + 1 - mj) * x for i, r in enumerate(p) for j, x in enumerate(r)) / (si * sj)


def features(p):
    return {"contrast": contrast(p), "homogeneity": homogeneity(p), "energy": energy(p),
            "correlation": correlation(p)}
''',
    "glrlm.py": '''_STEP = {"h": (0, 1), "v": (1, 0), "d": (1, 1), "a": (1, -1)}


def glrlm(image, levels, direction, mask=None):
    if direction not in _STEP:
        raise ValueError("direction must be h, v, d or a")
    if levels < 1 or len(image) == 0:
        raise ValueError("bad levels or empty image")
    nr, nc = len(image), len(image[0])
    if nc == 0 or any(len(r) != nc for r in image):
        raise ValueError("ragged or empty image")
    if mask is None:
        mask = [[1] * nc for _ in range(nr)]
    elif len(mask) != nr or any(len(r) != nc for r in mask):
        raise ValueError("mask shape mismatch")
    for r in range(nr):
        for c in range(nc):
            if mask[r][c] == 1:
                v = image[r][c]
                if not isinstance(v, int) or isinstance(v, bool) or not 1 <= v <= levels:
                    raise ValueError("gray level out of range")
    dr, dc = _STEP[direction]

    def same(r, c, g):
        return 0 <= r < nr and 0 <= c < nc and mask[r][c] == 1 and image[r][c] == g

    out = [[0] * max(nr, nc) for _ in range(levels)]
    for r in range(nr):
        for c in range(nc):
            if mask[r][c] != 1:
                continue
            g = image[r][c]
            if same(r - dr, c - dc, g):
                continue
            n, r2, c2 = 1, r + dr, c + dc
            while same(r2, c2, g):
                n, r2, c2 = n + 1, r2 + dr, c2 + dc
            out[g - 1][n - 1] += 1
    return out
''',
    "glrlm_features.py": '''def _runs(r):
    if len(r) == 0 or any(len(row) != len(r[0]) for row in r):
        raise ValueError("ragged or empty matrix")
    n = sum(sum(row) for row in r)
    if n == 0:
        raise ValueError("no runs")
    return n


def sre(r):
    n = _runs(r)
    return sum(x / (j + 1) ** @@P@@ for row in r for j, x in enumerate(row)) / n


def lre(r):
    n = _runs(r)
    return sum(x * (j + 1) ** 2 for row in r for j, x in enumerate(row)) / n


def gln(r):
    n = _runs(r)
    return sum(sum(row) ** 2 for row in r) / n


def rln(r):
    n = _runs(r)
    return sum(sum(row[j] for row in r) ** 2 for j in range(len(r[0]))) / n


def run_percentage(r, n_voxels):
    n = _runs(r)
    if n_voxels < 1:
        raise ValueError("n_voxels < 1")
    return n / n_voxels
''',
}
_TX_FLAWS = {
    "glcm.py": ("@@A45@@", "(-1, 1)", "(1, 1)"),
    "glrlm_features.py": ("@@P@@", "2", "1"),
}
_TX_IMG = "[[1, 1, 2], [1, 2, 2], [3, 3, 1]]"
_TX_IMG2 = "[[1, 1, 1, 2], [2, 1, 2, 2], [2, 2, 1, 1]]"
_TX_HIDDEN = {
    "test_h_glcm.py": "import pytest\nfrom glcm import offset_for, glcm\n" + _MATRIX_HELPERS + '''

IMG = [[1, 1, 2], [1, 2, 2], [3, 3, 1]]


def test_offsets():
    assert offset_for(0, 1) == (0, 1) and offset_for(45, 1) == (-1, 1)
    assert offset_for(90, 1) == (-1, 0) and offset_for(135, 1) == (-1, -1)
    assert offset_for(45, 2) == (-2, 2) and offset_for(0, 3) == (0, 3)


def test_offset_errors():
    for args in ((30, 1), (0, 0), (90, -1)):
        with pytest.raises(ValueError):
            offset_for(*args)


def test_angle_0_symmetric_counts():
    m = glcm(IMG, 3, (0, 1), normalise=False)
    assert flat(m) == [2, 2, 1, 2, 2, 0, 1, 0, 2]


def test_angle_90_symmetric_counts():
    m = glcm(IMG, 3, offset_for(90, 1), normalise=False)
    assert flat(m) == [2, 2, 1, 2, 2, 1, 1, 1, 0]


def test_angle_45_and_135_differ():
    assert flat(glcm(IMG, 3, offset_for(45, 1), normalise=False)) == [2, 0, 0, 0, 2, 2, 0, 2, 0]
    assert flat(glcm(IMG, 3, offset_for(135, 1), normalise=False)) == [0, 3, 1, 3, 0, 0, 1, 0, 0]


def test_distance_two():
    m = glcm(IMG, 3, offset_for(0, 2), normalise=False)
    assert flat(m) == [0, 2, 1, 2, 0, 0, 1, 0, 0]


def test_normalised_and_asymmetric():
    m = glcm(IMG, 3, (0, 1))
    assert close(flat(m), [x / 12 for x in [2, 2, 1, 2, 2, 0, 1, 0, 2]])
    a = glcm(IMG, 3, (0, 1), symmetric=False, normalise=False)
    assert flat(a) == [1, 2, 0, 0, 1, 0, 1, 0, 1]
    n = glcm(IMG, 3, (0, 1), symmetric=False)
    assert close(flat(n), [x / 6 for x in [1, 2, 0, 0, 1, 0, 1, 0, 1]])


def test_mask_removes_pairs():
    hole = [[1, 1, 1], [1, 0, 1], [1, 1, 1]]
    a = glcm(IMG, 3, (0, 1), mask=hole, symmetric=False)
    assert close(flat(a), [0.25, 0.25, 0, 0, 0, 0, 0.25, 0, 0.25])
    top = [[1, 1, 1], [1, 1, 1], [0, 0, 0]]
    assert flat(glcm(IMG, 3, (0, 1), mask=top, normalise=False)) == [2, 2, 0, 2, 2, 0, 0, 0, 0]


def test_masked_out_values_ignored_masked_in_validated():
    m = glcm([[1, 0], [2, 2]], 2, (0, 1), mask=[[1, 0], [1, 1]])
    assert close(flat(m), [0, 0, 0, 1.0])
    for bad in ([[1, 5], [1, 1]], [[1, 1.5], [1, 1]], [[1, 0], [1, 1]]):
        with pytest.raises(ValueError):
            glcm(bad, 2, (0, 1))


def test_no_pairs_gives_zero_matrix():
    assert glcm([[1]], 2, (0, 1)) == [[0, 0], [0, 0]]
    assert glcm([[1, 1]], 2, (0, 1), mask=[[0, 0]]) == [[0, 0], [0, 0]]


def test_shape_errors():
    for kw in ({"mask": [[1, 1]]}, {"levels": 0}):
        args = dict(image=[[1, 1], [1, 1]], levels=2, offset=(0, 1))
        args.update(kw)
        with pytest.raises(ValueError):
            glcm(**args)
    with pytest.raises(ValueError):
        glcm([[1, 1], [1]], 2, (0, 1))
    with pytest.raises(ValueError):
        glcm([[1, 1], [1, 1]], 2, (0, 0))
''',
    "test_h_glcm_features.py": "import pytest\nfrom glcm_features import contrast, homogeneity, energy, correlation, features\n" + '''

P = [[x / 12 for x in row] for row in [[2, 2, 1], [2, 2, 0], [1, 0, 2]]]
DIAG = [[0.5, 0], [0, 0.5]]
ANTI = [[0, 0.5], [0.5, 0]]


def test_contrast():
    assert contrast(P) == pytest.approx(1.0)
    assert contrast(DIAG) == 0 and contrast(ANTI) == pytest.approx(1.0)


def test_homogeneity():
    assert homogeneity(P) == pytest.approx(0.7)
    assert homogeneity(DIAG) == pytest.approx(1.0) and homogeneity(ANTI) == pytest.approx(0.5)


def test_energy():
    assert energy(P) == pytest.approx(22 / 144)
    assert energy(DIAG) == pytest.approx(0.5)


def test_correlation_value():
    assert correlation(P) == pytest.approx(5 / 23)


def test_correlation_extremes():
    assert correlation(DIAG) == pytest.approx(1.0)
    assert correlation(ANTI) == pytest.approx(-1.0)


def test_correlation_zero_variance_rule():
    assert correlation([[1.0]]) == 1.0
    assert correlation([[0, 0], [0, 1.0]]) == 1.0
    assert correlation([[0.5, 0.5], [0, 0]]) == 1.0


def test_features_dict():
    f = features(P)
    assert set(f) == {"contrast", "homogeneity", "energy", "correlation"}
    assert f["correlation"] == pytest.approx(5 / 23) and f["contrast"] == pytest.approx(1.0)


def test_must_be_normalised_square_nonempty():
    bad = ([[2, 2], [2, 2]], [[0.5, 0.5, 0]], [], [[0.4, 0.4], [0.1, 0.0]])
    for fn in (contrast, homogeneity, energy, correlation, features):
        for m in bad:
            with pytest.raises(ValueError):
                fn(m)
''',
    "test_h_glrlm.py": "import pytest\nfrom glrlm import glrlm\n" + '''

IMG2 = [[1, 1, 1, 2], [2, 1, 2, 2], [2, 2, 1, 1]]
IMG3 = [[1, 1, 2], [1, 2, 2], [3, 3, 1]]


def test_horizontal():
    assert glrlm(IMG2, 2, "h") == [[1, 1, 1, 0], [2, 2, 0, 0]]
    assert glrlm(IMG3, 3, "h") == [[2, 1, 0], [1, 1, 0], [0, 1, 0]]


def test_vertical():
    assert glrlm(IMG2, 2, "v") == [[4, 1, 0, 0], [2, 2, 0, 0]]
    assert glrlm(IMG3, 3, "v") == [[2, 1, 0], [1, 1, 0], [2, 0, 0]]


def test_diagonal():
    assert glrlm(IMG2, 2, "d") == [[3, 0, 1, 0], [4, 1, 0, 0]]
    assert glrlm(IMG3, 3, "d") == [[4, 0, 0], [3, 0, 0], [2, 0, 0]]


def test_anti_diagonal():
    assert glrlm(IMG2, 2, "a") == [[4, 1, 0, 0], [3, 0, 1, 0]]


def test_mask_splits_runs():
    mask = [[1, 1, 0, 1], [1, 1, 1, 1], [1, 1, 1, 1]]
    assert glrlm(IMG2, 2, "h", mask=mask) == [[1, 2, 0, 0], [2, 2, 0, 0]]


def test_masked_out_pixels_are_ignored_and_break_runs():
    assert glrlm([[1, 0, 1]], 1, "h", mask=[[1, 0, 1]]) == [[2, 0, 0]]
    assert glrlm([[1, 1, 1]], 1, "h", mask=[[0, 0, 0]]) == [[0, 0, 0]]
    with pytest.raises(ValueError):
        glrlm([[1, 7, 1]], 2, "h")


def test_columns_are_max_dimension():
    assert len(glrlm(IMG2, 2, "h")[0]) == 4 and len(glrlm([[1], [1], [1]], 1, "v")[0]) == 3
    assert glrlm([[1], [1], [1]], 1, "v") == [[0, 0, 1]]


def test_errors():
    for kw in ({"direction": "x"}, {"levels": 0}, {"mask": [[1, 1]]}):
        args = dict(image=[[1, 1], [1, 1]], levels=2, direction="h")
        args.update(kw)
        with pytest.raises(ValueError):
            glrlm(**args)
    with pytest.raises(ValueError):
        glrlm([[1, 1], [1]], 2, "h")
''',
    "test_h_glrlm_features.py": "import pytest\nfrom glrlm_features import sre, lre, gln, rln, run_percentage\n" + '''

R = [[1, 1, 1, 0], [2, 2, 0, 0]]


def test_sre():
    assert sre(R) == pytest.approx((1 + 0.25 + 1 / 9 + 2 + 0.5) / 7)


def test_lre():
    assert lre(R) == pytest.approx(24 / 7)


def test_gln():
    assert gln(R) == pytest.approx(25 / 7)


def test_rln():
    assert rln(R) == pytest.approx(19 / 7)


def test_run_percentage():
    assert run_percentage(R, 12) == pytest.approx(7 / 12)


def test_all_runs_length_one():
    r = [[2, 0, 0], [0, 0, 0]]
    assert sre(r) == 1.0 and lre(r) == 1.0 and gln(r) == pytest.approx(2.0) and rln(r) == pytest.approx(2.0)


def test_errors():
    for fn in (sre, lre, gln, rln):
        for bad in ([[0, 0], [0, 0]], [[1, 2], [3]], []):
            with pytest.raises(ValueError):
                fn(bad)
    with pytest.raises(ValueError):
        run_percentage(R, 0)
    with pytest.raises(ValueError):
        run_percentage([[0]], 5)
''',
}
TEXTURE = _task("radiomics-texture", _TX_SPEC, _TX_GOALS, _TX_TEMPLATES, _TX_FLAWS, _TX_HIDDEN)

# ====================================================================== radiomics-stats
_ST_SPEC = (
    "Build four small, independent python modules, all flat in one directory (no packages, stdlib only). "
    "A feature table is a list of rows (one row per patient, one column per feature), all floats/ints.\n"
    "1) icc.py: icc_3_1(data) is the two-way mixed, single-measure, CONSISTENCY intraclass correlation ICC(3,1) for "
    "test-retest data: data is n subjects x k sessions (list of n rows of k numbers). With grand mean g, row means and column "
    "means: SSR = k * sum over subjects (row mean - g)**2, SSC = n * sum over sessions (column mean - g)**2, SST = sum of "
    "(x - g)**2 over all cells, SSE = SST - SSR - SSC, MSR = SSR / (n-1), MSE = SSE / ((n-1) * (k-1)); "
    "ICC = (MSR - MSE) / (MSR + (k-1) * MSE). It is NOT clamped (it may be negative), and a constant offset between sessions "
    "does not lower it. n < 2, k < 2 or rows of different lengths raise ValueError; if MSR + (k-1)*MSE == 0 (no "
    "between-subject variance and no residual, i.e. every subject has the same profile across sessions, e.g. all cells "
    "identical or [[1, 3], [1, 3]]) raise ValueError.\n"
    "2) fdr.py: bh_qvalues(pvalues) returns the Benjamini-Hochberg adjusted p-values (q-values) in the ORIGINAL order. With "
    "m p-values sorted ascending and ranks 1..m: q_(rank) = min over all ranks r >= rank of p_(r) * m / r, never above 1, so "
    "q-values are monotone non-decreasing in p; equal p-values get equal q-values; an empty list returns []. A p-value "
    "outside [0, 1] (or NaN) raises ValueError. significant(pvalues, alpha) returns a list of booleans q <= alpha (alpha "
    "inclusive); alpha outside [0, 1] raises ValueError.\n"
    "3) standardise.py: fit_zscore(train_rows) returns (means, stds) per column, std being the POPULATION standard deviation "
    "(divide by n); an empty train set, empty rows or rows of different lengths raise ValueError. apply_zscore(rows, means, stds) "
    "returns (x - mean) / std per cell using the GIVEN parameters (never refit on `rows`); any column whose std <= 1e-12 is "
    "constant: its output is 0.0 for every row; a row whose length differs from len(means) raises ValueError; no rows returns "
    "[]. standardise(train_rows, test_rows) fits on the train rows ONLY and returns (train_z, test_z).\n"
    "4) cindex.py: concordance_index(times, events, risks) is Harrell's C. events are 1 (event observed) or 0 (censored), "
    "anything else raises ValueError, as do lists of different lengths. A pair (i, j) is comparable only if i had an event "
    "and times[i] < times[j] STRICTLY (so a censored i is never the earlier member, and pairs with equal times are never "
    "comparable). A comparable pair scores 1 if risks[i] > risks[j] (higher risk = earlier event), 0.5 if the risks are "
    "equal, 0 otherwise. The index is the total score divided by the number of comparable pairs; no comparable pair raises "
    "ValueError."
)
_ST_GOALS = {
    "icc.py": "icc_3_1 consistency ICC from ANOVA mean squares (no clamping, ValueError rules)",
    "fdr.py": "bh_qvalues (monotone, ties, original order) and significant",
    "standardise.py": "fit_zscore (population std), apply_zscore (zero-std rule), standardise (train-only fit)",
    "cindex.py": "Harrell concordance_index with censoring, risk ties and time ties",
}
_ST_TEMPLATES = {
    "icc.py": '''def icc_3_1(data):
    n = len(data)
    if n < 2:
        raise ValueError("need at least 2 subjects")
    k = len(data[0])
    if k < 2 or any(len(r) != k for r in data):
        raise ValueError("need k >= 2 sessions in every row")
    g = sum(sum(r) for r in data) / (n * k)
    rows = [sum(r) / k for r in data]
    cols = [sum(data[i][j] for i in range(n)) / n for j in range(k)]
    ssr = k * sum((m - g) ** 2 for m in rows)
    ssc = n * sum((m - g) ** 2 for m in cols)
    sst = sum((x - g) ** 2 for r in data for x in r)
    sse = sst - ssr - ssc
    msr = ssr / (n - 1)
    mse = sse / ((n - 1) * (k - 1))
    den = @@DEN@@
    if msr + (k - 1) * mse == 0:
        raise ValueError("no variance")
    return (msr - mse) / den
''',
    "fdr.py": '''def bh_qvalues(pvalues):
    m = len(pvalues)
    if any(not (0 <= p <= 1) for p in pvalues):
        raise ValueError("p-values must lie in [0, 1]")
    order = sorted(range(m), key=lambda i: pvalues[i])
    q = [0.0] * m
    running = 1.0
    for rank in range(m, 0, -1):
        i = order[rank - 1]
        running = min(running, pvalues[i] * m / rank)
        q[i] = running
    return q


def significant(pvalues, alpha):
    if not 0 <= alpha <= 1:
        raise ValueError("alpha must lie in [0, 1]")
    return [q <= alpha for q in bh_qvalues(pvalues)]
''',
    "standardise.py": '''import math


def fit_zscore(train_rows):
    n = len(train_rows)
    if n == 0:
        raise ValueError("empty train set")
    k = len(train_rows[0])
    if k == 0 or any(len(r) != k for r in train_rows):
        raise ValueError("empty or ragged rows")
    means = [sum(r[j] for r in train_rows) / n for j in range(k)]
    stds = [math.sqrt(sum((r[j] - means[j]) ** 2 for r in train_rows) / n) for j in range(k)]
    return means, stds


def apply_zscore(rows, means, stds):
    out = []
    for r in rows:
        if len(r) != len(means):
            raise ValueError("row length differs from fitted columns")
        out.append([0.0 if s <= 1e-12 else (x - m) / s for x, m, s in zip(r, means, stds)])
    return out


def standardise(train_rows, test_rows):
    means, stds = fit_zscore(train_rows)
    return apply_zscore(train_rows, means, stds), apply_zscore(test_rows, means, stds)
''',
    "cindex.py": '''def concordance_index(times, events, risks):
    n = len(times)
    if len(events) != n or len(risks) != n:
        raise ValueError("length mismatch")
    if any(e not in (0, 1) for e in events):
        raise ValueError("events must be 0 or 1")
    score, pairs = 0.0, 0
    for i in range(n):
        if events[i] != 1:
            continue
        for j in range(n):
            if times[i] < times[j]:
                pairs += 1
                if risks[i] > risks[j]:
                    score += 1
                elif risks[i] == risks[j]:
                    score += @@TIE@@
    if pairs == 0:
        raise ValueError("no comparable pairs")
    return score / pairs
''',
}
_ST_FLAWS = {
    "icc.py": ("@@DEN@@", "msr + (k - 1) * mse", "msr or 1e-300"),
    "cindex.py": ("@@TIE@@", "0.5", "1.0"),
}
_ST_HIDDEN = {
    "test_h_icc.py": '''import pytest
from icc import icc_3_1

SF = [[9, 2, 5, 8], [6, 1, 3, 2], [8, 4, 6, 8], [7, 1, 2, 6], [10, 5, 6, 9], [6, 2, 4, 7]]


def test_shrout_fleiss_reference():
    assert icc_3_1(SF) == pytest.approx(0.7148, abs=5e-4)


def test_session_offset_does_not_lower_consistency():
    assert icc_3_1([[1, 2], [3, 4], [5, 6]]) == pytest.approx(1.0)
    assert icc_3_1([[1, 11], [3, 13], [5, 15]]) == pytest.approx(1.0)


def test_negative_icc_is_not_clamped():
    assert icc_3_1([[1, 3], [3, 1], [2, 2]]) == pytest.approx(-1.0)


def test_scale_and_shift_invariance():
    scaled = [[10 * x + 3 for x in r] for r in SF]
    assert icc_3_1(scaled) == pytest.approx(icc_3_1(SF))


def test_shape_errors():
    for bad in ([[1, 2]], [[1], [2], [3]], [[1, 2], [3]], []):
        with pytest.raises(ValueError):
            icc_3_1(bad)


def test_no_variance_raises():
    with pytest.raises(ValueError):
        icc_3_1([[5, 5], [5, 5]])


def test_no_variance_same_profile_raises():
    with pytest.raises(ValueError):
        icc_3_1([[1, 3], [1, 3]])
''',
    "test_h_fdr.py": '''import pytest
from fdr import bh_qvalues, significant


def test_basic_values_in_original_order():
    assert bh_qvalues([0.01, 0.04, 0.03, 0.005]) == pytest.approx([0.02, 0.04, 0.04, 0.02])


def test_monotone_enforced():
    assert bh_qvalues([0.01, 0.04, 0.045]) == pytest.approx([0.03, 0.045, 0.045])


def test_ties_get_equal_q():
    q = bh_qvalues([0.02, 0.02, 0.5, 0.01])
    assert q == pytest.approx([0.08 / 3, 0.08 / 3, 0.5, 0.08 / 3])
    assert q[0] == q[1]


def test_capped_at_one_and_empty_and_single():
    assert bh_qvalues([1.0, 1.0, 1.0]) == pytest.approx([1.0, 1.0, 1.0])
    assert bh_qvalues([]) == [] and bh_qvalues([0.3]) == pytest.approx([0.3])
    assert bh_qvalues([0.0, 0.5]) == pytest.approx([0.0, 0.5])


def test_p_value_range_errors():
    for bad in ([-0.1, 0.5], [0.2, 1.2], [float("nan"), 0.1]):
        with pytest.raises(ValueError):
            bh_qvalues(bad)


def test_significant_alpha_inclusive():
    assert significant([0.01, 0.04, 0.03, 0.005], 0.02) == [True, False, False, True]
    with pytest.raises(ValueError):
        significant([0.1], 1.5)
''',
    "test_h_standardise.py": '''import pytest
from standardise import fit_zscore, apply_zscore, standardise

TRAIN = [[1, 10, 5], [3, 20, 5], [5, 30, 5]]


def test_fit_population_std():
    means, stds = fit_zscore(TRAIN)
    assert means == pytest.approx([3, 20, 5])
    assert stds == pytest.approx([(8 / 3) ** 0.5, (200 / 3) ** 0.5, 0.0])


def test_apply_uses_given_parameters_and_zero_std_rule():
    means, stds = fit_zscore(TRAIN)
    z = apply_zscore([[4, 10, 7]], means, stds)
    assert z[0] == pytest.approx([1 / (8 / 3) ** 0.5, -10 / (200 / 3) ** 0.5, 0.0])
    assert apply_zscore([[100, 100, 100]], means, stds)[0][0] == pytest.approx(97 / (8 / 3) ** 0.5)


def test_near_constant_column_uses_tolerance():
    means, stds = fit_zscore([[0.1], [0.1], [0.1]])
    assert apply_zscore([[0.1], [0.7]], means, stds) == [[0.0], [0.0]]


def test_standardise_fits_on_train_only():
    tr, te = standardise(TRAIN, [[103, 20, 5], [3, 50, 9]])
    assert tr[0] == pytest.approx([-1.5 ** 0.5, -1.5 ** 0.5, 0.0])
    s = (8 / 3) ** 0.5
    assert te[0][0] == pytest.approx(100 / s) and te[1][0] == pytest.approx(0.0)
    assert te[0][2] == 0.0 and te[1][2] == 0.0
    assert [sum(r[0] for r in tr) / 3, sum(r[1] for r in tr) / 3] == pytest.approx([0.0, 0.0], abs=1e-12)
    assert sum(r[0] ** 2 for r in tr) / 3 == pytest.approx(1.0)


def test_empty_test_rows():
    assert standardise(TRAIN, [])[1] == []


def test_errors():
    for bad in ([], [[]], [[1, 2], [3]]):
        with pytest.raises(ValueError):
            fit_zscore(bad)
    means, stds = fit_zscore(TRAIN)
    with pytest.raises(ValueError):
        apply_zscore([[1, 2]], means, stds)
    with pytest.raises(ValueError):
        standardise(TRAIN, [[1, 2, 3, 4]])
''',
    "test_h_cindex.py": '''import pytest
from cindex import concordance_index as c


def test_perfect_and_reversed():
    assert c([1, 2, 3, 4], [1, 1, 1, 1], [4, 3, 2, 1]) == 1.0
    assert c([1, 2, 3, 4], [1, 1, 1, 1], [1, 2, 3, 4]) == 0.0


def test_mixed_without_ties():
    assert c([1, 2, 3, 4], [1, 1, 1, 1], [2, 3, 1, 4]) == pytest.approx(2 / 6)


def test_censoring_limits_comparable_pairs():
    assert c([1, 2, 3, 4], [1, 0, 1, 0], [4, 3, 2, 1]) == 1.0
    assert c([1, 2, 3, 4], [1, 0, 1, 0], [1, 5, 2, 3]) == 0.0


def test_censored_subject_is_never_the_earlier_member():
    assert c([1, 2, 3], [0, 1, 1], [9, 1, 2]) == 0.0


def test_risk_ties_count_half():
    assert c([1, 2], [1, 1], [5, 5]) == 0.5
    assert c([1, 2, 3], [1, 1, 1], [3, 3, 1]) == pytest.approx(2.5 / 3)


def test_equal_times_are_not_comparable():
    assert c([2, 2, 3], [1, 0, 0], [1, 5, 0]) == 1.0
    assert c([2, 2, 3], [1, 1, 1], [1, 2, 0]) == 1.0


def test_errors():
    for args in (([1, 2], [0, 0], [1, 2]), ([1], [1], [1]), ([3, 3], [1, 1], [1, 2]),
                 ([1, 2], [1], [1, 2]), ([1, 2], [1, 2], [1, 2])):
        with pytest.raises(ValueError):
            c(*args)
''',
}
STATS = _task("radiomics-stats", _ST_SPEC, _ST_GOALS, _ST_TEMPLATES, _ST_FLAWS, _ST_HIDDEN)

# ====================================================================== radiomics-suv (IBSI-SUV v3.1)
_SUV_SPEC = (
    "Build four small, independent python modules implementing the IBSI-SUV v3.1 reference standard, "
    "all flat in one directory (no packages, stdlib only).\n"
    "1) suv_body.py: patient_weight_g(weight) converts patient weight to grams: weight <= 0 raises ValueError; "
    "if weight >= 1000.0 it is already in grams and returned as float(weight), otherwise float(weight) * 1000.0. "
    "patient_height_cm(height_m) converts height in metres to cm: height_m <= 0 raises ValueError, returns float(height_m) * 100.0. "
    "body_norm_factor_g(weight, height_m=None, sex=None, suv_type='BW') returns the IBSI-SUV v3.1 body-size factor in grams "
    "(or cm^2 for BSA): st = suv_type.strip().upper(); for st in ('', 'BW') only weight is needed and returns patient_weight_g(weight). "
    "All other types require height_m (missing/None or <= 0 raises ValueError) with h_cm = patient_height_cm(height_m) and w_kg = patient_weight_g(weight) / 1000.0. "
    "For st == 'BSA' return 0.007184 * (h_cm ** 0.725) * (w_kg ** 0.425) * 10000.0 (sex not required). "
    "For st in ('LBM', 'LBMJAMES128', 'LBMJANMA', 'IBW'), sex is required and sex.strip().upper() must be in ('M', 'F', 'O') else ValueError: "
    "LBM: m = 1.10*w_kg - 120.0*(w_kg/h_cm)**2, f = 1.07*w_kg - 148.0*(w_kg/h_cm)**2; "
    "LBMJAMES128: m = 1.10*w_kg - 128.0*(w_kg/h_cm)**2, f = 1.07*w_kg - 148.0*(w_kg/h_cm)**2; "
    "LBMJANMA: bmi = w_kg / ((h_cm * 0.01) ** 2), m = (9270.0*w_kg)/(6680.0 + 216.0*bmi), f = (9270.0*w_kg)/(8780.0 + 244.0*bmi); "
    "IBW: m = 48.0 + 1.06*(h_cm - 152.0), f = 45.5 + 0.91*(h_cm - 152.0); "
    "if sex is 'M' choose m, 'F' choose f, 'O' choose 0.5*(m + f), and return chosen * 1000.0. Any other suv_type raises ValueError.\n"
    "2) suv_decay.py: effective_dose_bq(dose) validates injected dose: dose <= 0 raises ValueError; if dose < 1e4 it is in MBq "
    "so return float(dose) * 1e6, else float(dose). frame_average_offset_s(frame_duration_ms, half_life_s) computes IBSI-SUV t_ave in seconds: "
    "frame_duration_ms <= 0 or half_life_s <= 0 raises ValueError; T = frame_duration_ms / 1000.0, lam = ln(2) / half_life_s, "
    "return (1.0 / lam) * ln((lam * T) / (1.0 - exp(-lam * T))). decayed_dose_bq(dose, elapsed_s, half_life_s) validates dose via effective_dose_bq "
    "and half_life_s > 0 (else ValueError) and returns effective_dose_bq(dose) * exp(-ln(2) * elapsed_s / half_life_s). "
    "resolve_elapsed_s(decay_correction, acq_s, admin_s, series_s=None, frame_ref_ms=None, frame_dur_ms=None, half_life_s=6588.0, manufacturer='') "
    "returns the decay time (t_ref - t_adm) in seconds: dc = decay_correction.strip().upper(); if dc == 'ADMIN' return 0.0; "
    "if dc == 'NONE' require frame_dur_ms > 0 and half_life_s > 0 (else ValueError) and t_ref = acq_s + frame_average_offset_s(frame_dur_ms, half_life_s); "
    "if dc == 'START': if series_s is not None and abs(acq_s - series_s) <= 1.0 then t_ref = acq_s; else require frame_ref_ms is not None and frame_ref_ms >= 0 "
    "(else ValueError), and if 'GE' in manufacturer.upper() then t_ref = acq_s - frame_ref_ms / 1000.0, else require frame_dur_ms > 0 and "
    "t_ref = acq_s + frame_average_offset_s(frame_dur_ms, half_life_s) - frame_ref_ms / 1000.0; any other dc raises ValueError. "
    "Finally diff = t_ref - admin_s; if diff < -3600.0 add 86400.0 (midnight crossing); if diff < -3600.0 or diff >= 2.0 * half_life_s raise ValueError; return diff.\n"
    "3) suv_convert.py: rescale_slice(raw_2d, slope, intercept) takes a non-empty rectangular 2D list of numbers and returns a 2D list of floats "
    "x * slope + intercept; empty/ragged raw_2d or slope == 0 raises ValueError. philips_cnts_to_suvbw(rescaled_2d, weight_g, decayed_dose, "
    "act_factor=0.0, suv_factor=0.0, manufacturer='PHILIPS') converts Philips CNTS: if 'PHILIPS' not in manufacturer.upper() or weight_g <= 0 or decayed_dose <= 0 "
    "raise ValueError; if act_factor > 0.0 scale every pixel by act_factor * (weight_g / decayed_dose); elif suv_factor > 0.0 scale every pixel by suv_factor; "
    "else raise ValueError.\n"
    "4) suv_qc.py: sort_slices_by_z(slices) takes a non-empty list of dicts each having 'image' (2D list) and optional 'ipp_z' (float/None) and 'slice_location' (float/None). "
    "For each slice use 'ipp_z' if not None, else 'slice_location' if not None, else raise ValueError (prioritising ImagePositionPatient[2] over SliceLocation). "
    "Duplicate z values or empty slices raise ValueError. Return [s['image'] for s in sorted_by_z_ascending]. "
    "roi_suv_metrics(vol_3d, mask_3d) extracts all voxel values where mask_3d == 1 (matching 3D shapes required, mask values must be int 0 or 1, at least one 1 required, else ValueError) "
    "and returns {'min': float, 'median': float, 'max': float, 'mean': float} where median is the average of the two middle values for even counts."
)
_SUV_GOALS = {
    "suv_body.py": "patient_weight_g (>=1000g rule), patient_height_cm, body_norm_factor_g (BW/LBM/LBMJAMES128/LBMJANMA/IBW/BSA)",
    "suv_decay.py": "effective_dose_bq (<1e4 MBq rule), frame_average_offset_s, decayed_dose_bq, resolve_elapsed_s (ADMIN/START/NONE)",
    "suv_convert.py": "rescale_slice (non-zero slope, rectangular 2D), philips_cnts_to_suvbw (act_factor priority over suv_factor)",
    "suv_qc.py": "sort_slices_by_z (ipp_z priority over slice_location), roi_suv_metrics (min/median/max/mean)",
}
_SUV_TEMPLATES = {
    "suv_body.py": '''def patient_weight_g(weight):
    if weight is None or weight <= 0:
        raise ValueError("weight must be > 0")
    w = float(weight)
    return w if w >= @@GRAM_THRESHOLD@@ else w * 1000.0


def patient_height_cm(height_m):
    if height_m is None or height_m <= 0:
        raise ValueError("height_m must be > 0")
    return float(height_m) * 100.0


def body_norm_factor_g(weight, height_m=None, sex=None, suv_type="BW"):
    st = (suv_type or "").strip().upper()
    w_g = patient_weight_g(weight)
    if st in ("", "BW"):
        return w_g
    h_cm = patient_height_cm(height_m)
    w_kg = w_g / 1000.0
    if st == "BSA":
        return 0.007184 * (h_cm ** 0.725) * (w_kg ** 0.425) * 10000.0
    if sex is None or str(sex).strip().upper() not in ("M", "F", "O"):
        raise ValueError("valid sex required")
    sx = str(sex).strip().upper()
    if st == "LBM":
        m = 1.10 * w_kg - 120.0 * ((w_kg / h_cm) ** 2)
        f = 1.07 * w_kg - 148.0 * ((w_kg / h_cm) ** 2)
    elif st == "LBMJAMES128":
        m = 1.10 * w_kg - 128.0 * ((w_kg / h_cm) ** 2)
        f = 1.07 * w_kg - 148.0 * ((w_kg / h_cm) ** 2)
    elif st == "LBMJANMA":
        bmi = w_kg / ((h_cm * 0.01) ** 2)
        m = (9270.0 * w_kg) / (6680.0 + 216.0 * bmi)
        f = (9270.0 * w_kg) / (8780.0 + 244.0 * bmi)
    elif st == "IBW":
        m = 48.0 + 1.06 * (h_cm - 152.0)
        f = 45.5 + 0.91 * (h_cm - 152.0)
    else:
        raise ValueError(f"unsupported suv_type: {st}")
    val = m if sx == "M" else (f if sx == "F" else 0.5 * (m + f))
    return val * 1000.0
''',
    "suv_decay.py": '''import math


def effective_dose_bq(dose):
    if dose is None or dose <= 0:
        raise ValueError("dose must be > 0")
    d = float(dose)
    return d * @@MBQ_SCALE@@ if d < 1e4 else d


def frame_average_offset_s(frame_duration_ms, half_life_s):
    if frame_duration_ms is None or frame_duration_ms <= 0 or half_life_s is None or half_life_s <= 0:
        raise ValueError("duration and half_life must be > 0")
    t_s = float(frame_duration_ms) / 1000.0
    lam = math.log(2.0) / float(half_life_s)
    return (1.0 / lam) * math.log((lam * t_s) / (1.0 - math.exp(-lam * t_s)))


def decayed_dose_bq(dose, elapsed_s, half_life_s):
    if half_life_s is None or half_life_s <= 0:
        raise ValueError("half_life_s must be > 0")
    d = effective_dose_bq(dose)
    return d * math.exp(-math.log(2.0) * float(elapsed_s) / float(half_life_s))


def resolve_elapsed_s(
    decay_correction,
    acq_s,
    admin_s,
    series_s=None,
    frame_ref_ms=None,
    frame_dur_ms=None,
    half_life_s=6588.0,
    manufacturer="",
):
    dc = (decay_correction or "").strip().upper()
    if dc == "ADMIN":
        return 0.0
    if half_life_s is None or half_life_s <= 0:
        raise ValueError("half_life_s must be > 0")
    if dc == "NONE":
        if frame_dur_ms is None or frame_dur_ms <= 0:
            raise ValueError("frame_dur_ms required for NONE")
        t_ref = float(acq_s) + frame_average_offset_s(frame_dur_ms, half_life_s)
    elif dc == "START":
        if series_s is not None and abs(float(acq_s) - float(series_s)) <= 1.0:
            t_ref = float(acq_s)
        else:
            if frame_ref_ms is None or frame_ref_ms < 0:
                raise ValueError("non-negative frame_ref_ms required")
            if "GE" in (manufacturer or "").upper():
                t_ref = float(acq_s) - float(frame_ref_ms) / 1000.0
            else:
                if frame_dur_ms is None or frame_dur_ms <= 0:
                    raise ValueError("frame_dur_ms required for non-GE START")
                t_ref = (
                    float(acq_s)
                    + frame_average_offset_s(frame_dur_ms, half_life_s)
                    - float(frame_ref_ms) / 1000.0
                )
    else:
        raise ValueError(f"invalid decay_correction: {dc}")
    diff = t_ref - float(admin_s)
    if diff < -3600.0:
        diff += 86400.0
    if diff < -3600.0 or diff >= 2.0 * float(half_life_s):
        raise ValueError("elapsed time out of IBSI-SUV bounds")
    return diff
''',
    "suv_convert.py": '''def rescale_slice(raw_2d, slope, intercept):
    if not isinstance(raw_2d, list) or not raw_2d or not isinstance(raw_2d[0], list) or not raw_2d[0]:
        raise ValueError("raw_2d must be non-empty 2D list")
    if slope is None or float(slope) == 0.0:
        raise ValueError("slope must be non-zero")
    cols = len(raw_2d[0])
    s = float(slope)
    b = float(intercept)
    out = []
    for row in raw_2d:
        if not isinstance(row, list) or len(row) != cols:
            raise ValueError("ragged 2D list")
        out.append([float(x) * s + b for x in row])
    return out


def philips_cnts_to_suvbw(
    rescaled_2d,
    weight_g,
    decayed_dose,
    act_factor=0.0,
    suv_factor=0.0,
    manufacturer="PHILIPS",
):
    if "PHILIPS" not in (manufacturer or "").upper():
        raise ValueError("CNTS only valid for Philips")
    if weight_g is None or weight_g <= 0 or decayed_dose is None or decayed_dose <= 0:
        raise ValueError("weight_g and decayed_dose must be > 0")
    af = float(act_factor or 0.0)
    sf = float(suv_factor or 0.0)
    if af > 0.0:
        scale = af * (float(weight_g) / float(decayed_dose))
    elif sf > 0.0:
        scale = sf
    else:
        raise ValueError("missing Philips scale factors")
    return rescale_slice(rescaled_2d, scale, 0.0)
''',
    "suv_qc.py": '''def sort_slices_by_z(slices):
    if not isinstance(slices, list) or not slices:
        raise ValueError("slices must be non-empty list")
    keyed = []
    seen_z = set()
    for s in slices:
        if not isinstance(s, dict) or "image" not in s:
            raise ValueError("invalid slice entry")
        z = s.get("ipp_z")
        if z is None:
            z = s.get("slice_location")
        if z is None:
            raise ValueError("missing z coordinate")
        zf = float(z)
        if zf in seen_z:
            raise ValueError("duplicate z coordinate")
        seen_z.add(zf)
        keyed.append((zf, s["image"]))
    keyed.sort(key=lambda item: item[0])
    return [img for _, img in keyed]


def roi_suv_metrics(vol_3d, mask_3d):
    if not isinstance(vol_3d, list) or not isinstance(mask_3d, list) or not vol_3d or len(vol_3d) != len(mask_3d):
        raise ValueError("invalid 3D inputs")
    vals = []
    for z_idx, (v_slice, m_slice) in enumerate(zip(vol_3d, mask_3d)):
        if not isinstance(v_slice, list) or not isinstance(m_slice, list) or not v_slice or len(v_slice) != len(m_slice):
            raise ValueError("slice shape mismatch")
        for v_row, m_row in zip(v_slice, m_slice):
            if not isinstance(v_row, list) or not isinstance(m_row, list) or not v_row or len(v_row) != len(m_row):
                raise ValueError("row shape mismatch")
            for v, m in zip(v_row, m_row):
                if type(m) is not int or m not in (0, 1):
                    raise ValueError("mask must be int 0 or 1")
                if m == 1:
                    vals.append(float(v))
    if not vals:
        raise ValueError("empty ROI")
    vals.sort()
    n = len(vals)
    med = vals[n // 2] if n % 2 == 1 else 0.5 * (vals[n // 2 - 1] + vals[n // 2])
    return {
        "min": vals[0],
        "median": med,
        "max": vals[-1],
        "mean": sum(vals) / n,
    }
''',
}
_SUV_FLAWS = {
    "suv_body.py": ("@@GRAM_THRESHOLD@@", "1000.0", "100000.0"),
    "suv_decay.py": ("@@MBQ_SCALE@@", "1e6", "1.0"),
}
_SUV_HIDDEN = {
    "test_h_suv_body.py": '''import pytest
from suv_body import patient_weight_g, patient_height_cm, body_norm_factor_g


def test_weight_kg_and_grams():
    assert patient_weight_g(75.0) == pytest.approx(75000.0)
    assert patient_weight_g(75000.0) == pytest.approx(75000.0)


def test_height_cm():
    assert patient_height_cm(1.75) == pytest.approx(175.0)


def test_bw_case_insensitive_and_blank():
    assert body_norm_factor_g(80.0, suv_type="bw") == pytest.approx(80000.0)
    assert body_norm_factor_g(80.0, suv_type="") == pytest.approx(80000.0)


def test_bw_and_bsa():
    assert body_norm_factor_g(75.0, suv_type="BW") == pytest.approx(75000.0)
    assert body_norm_factor_g(75.0, 1.75, suv_type="BSA") == pytest.approx(
        0.007184 * (175.0 ** 0.725) * (75.0 ** 0.425) * 10000.0
    )


def test_lbm_variants_and_ibw():
    assert body_norm_factor_g(75.0, 1.75, "M", "LBM") == pytest.approx(
        (1.10 * 75.0 - 120.0 * (75.0 / 175.0) ** 2) * 1000.0
    )
    assert body_norm_factor_g(75.0, 1.75, "F", "LBMJAMES128") == pytest.approx(
        (1.07 * 75.0 - 148.0 * (75.0 / 175.0) ** 2) * 1000.0
    )
    bmi = 75.0 / (1.75 ** 2)
    assert body_norm_factor_g(75.0, 1.75, "M", "LBMJANMA") == pytest.approx(
        ((9270.0 * 75.0) / (6680.0 + 216.0 * bmi)) * 1000.0
    )
    m_ibw = (48.0 + 1.06 * 23.0) * 1000.0
    f_ibw = (45.5 + 0.91 * 23.0) * 1000.0
    assert body_norm_factor_g(75.0, 1.75, "O", "IBW") == pytest.approx(0.5 * (m_ibw + f_ibw))


def test_body_errors():
    for bad_w in (0, -5, None):
        with pytest.raises(ValueError):
            patient_weight_g(bad_w)
    with pytest.raises(ValueError):
        body_norm_factor_g(75.0, None, "M", "LBM")
    with pytest.raises(ValueError):
        body_norm_factor_g(75.0, 1.75, "X", "LBM")
    with pytest.raises(ValueError):
        body_norm_factor_g(75.0, 1.75, "M", "UNKNOWN")
''',
    "test_h_suv_decay.py": '''import math
import pytest
from suv_decay import effective_dose_bq, frame_average_offset_s, decayed_dose_bq, resolve_elapsed_s


def test_effective_dose_mbq_and_bq():
    assert effective_dose_bq(300.0) == pytest.approx(300_000_000.0)
    assert effective_dose_bq(300_000_000.0) == pytest.approx(300_000_000.0)


def test_frame_average_offset():
    lam = math.log(2.0) / 6588.0
    expected = (1.0 / lam) * math.log((lam * 300.0) / (1.0 - math.exp(-lam * 300.0)))
    assert frame_average_offset_s(300000.0, 6588.0) == pytest.approx(expected)


def test_decayed_dose():
    assert decayed_dose_bq(300.0, 6588.0, 6588.0) == pytest.approx(150_000_000.0)


def test_resolve_elapsed_series_equals_acq():
    assert resolve_elapsed_s("START", 4200.5, 600.0, series_s=4200.0) == pytest.approx(3600.5)


def test_resolve_elapsed_admin_none_start():
    assert resolve_elapsed_s("ADMIN", 5000.0, 1000.0) == 0.0
    t_ave = frame_average_offset_s(300000.0, 6588.0)
    assert resolve_elapsed_s("NONE", 3600.0, 0.0, frame_dur_ms=300000.0, half_life_s=6588.0) == pytest.approx(3600.0 + t_ave)
    assert resolve_elapsed_s("START", 4200.0, 600.0, frame_ref_ms=600000.0, manufacturer="GE MEDICAL SYSTEMS") == pytest.approx(3000.0)
    assert resolve_elapsed_s("START", 4200.0, 600.0, frame_ref_ms=600000.0, frame_dur_ms=300000.0, manufacturer="SIEMENS") == pytest.approx(3000.0 + t_ave)


def test_midnight_crossing_and_errors():
    assert resolve_elapsed_s("START", 600.0, 86400.0 - 3000.0, series_s=600.0, half_life_s=6588.0) == pytest.approx(3600.0)
    with pytest.raises(ValueError):
        effective_dose_bq(0)
    with pytest.raises(ValueError):
        resolve_elapsed_s("START", 20000.0, 0.0, series_s=20000.0, half_life_s=6588.0)
''',
    "test_h_suv_convert.py": '''import pytest
from suv_convert import rescale_slice, philips_cnts_to_suvbw


def test_rescale_slice():
    assert rescale_slice([[1, 2], [3, 4]], 2.5, -1.0) == [[1.5, 4.0], [6.5, 9.0]]


def test_rescale_negative_slope():
    assert rescale_slice([[2.0, -4.0]], -0.5, 1.0) == [[0.0, 3.0]]


def test_philips_activity_factor_priority():
    out = philips_cnts_to_suvbw([[10.0, 20.0]], 75000.0, 150_000_000.0, act_factor=2.0, suv_factor=99.0)
    assert out[0] == pytest.approx([0.01, 0.02])


def test_philips_suv_factor_fallback():
    out = philips_cnts_to_suvbw([[10.0, 20.0]], 75000.0, 150_000_000.0, act_factor=0.0, suv_factor=0.005)
    assert out[0] == pytest.approx([0.05, 0.10])


def test_convert_errors():
    with pytest.raises(ValueError):
        rescale_slice([], 1.0, 0.0)
    with pytest.raises(ValueError):
        rescale_slice([[1, 2], [3]], 1.0, 0.0)
    with pytest.raises(ValueError):
        rescale_slice([[1, 2]], 0.0, 0.0)
    with pytest.raises(ValueError):
        philips_cnts_to_suvbw([[1.0]], 75000.0, 150_000_000.0, act_factor=2.0, manufacturer="SIEMENS")
    with pytest.raises(ValueError):
        philips_cnts_to_suvbw([[1.0]], 75000.0, 150_000_000.0, act_factor=0.0, suv_factor=0.0)
''',
    "test_h_suv_qc.py": '''import pytest
from suv_qc import sort_slices_by_z, roi_suv_metrics


def test_sort_slices_prioritizes_ipp_z_over_slice_location():
    slices = [
        {"image": [[2]], "ipp_z": 1406.7, "slice_location": -1406.7},
        {"image": [[1]], "ipp_z": 1400.0, "slice_location": -1400.0},
    ]
    assert sort_slices_by_z(slices) == [[[1]], [[2]]]


def test_sort_slices_falls_back_to_slice_location():
    slices = [
        {"image": [[20]], "ipp_z": None, "slice_location": 10.0},
        {"image": [[10]], "ipp_z": None, "slice_location": -5.0},
    ]
    assert sort_slices_by_z(slices) == [[[10]], [[20]]]


def test_roi_suv_metrics_dro_reference():
    vol = [[[[0.2, 1.0], [4.0, 99.0]]][0]]
    msk = [[[[1, 1], [1, 0]]][0]]
    res = roi_suv_metrics(vol, msk)
    assert res["min"] == pytest.approx(0.2)
    assert res["median"] == pytest.approx(1.0)
    assert res["max"] == pytest.approx(4.0)
    assert res["mean"] == pytest.approx((0.2 + 1.0 + 4.0) / 3.0)


def test_roi_suv_metrics_even_median():
    vol = [[[1.0, 2.0], [4.0, 9.0]]]
    msk = [[[1, 1], [1, 1]]]
    res = roi_suv_metrics(vol, msk)
    assert res["median"] == pytest.approx(3.0)


def test_qc_errors():
    with pytest.raises(ValueError):
        sort_slices_by_z([])
    with pytest.raises(ValueError):
        sort_slices_by_z([{"image": [[1]], "ipp_z": 1.0}, {"image": [[2]], "ipp_z": 1.0}])
    with pytest.raises(ValueError):
        roi_suv_metrics([[[1.0]]], [[[0]]])
    with pytest.raises(ValueError):
        roi_suv_metrics([[[1.0]]], [[[True]]])
''',
}
SUV = _task("radiomics-suv", _SUV_SPEC, _SUV_GOALS, _SUV_TEMPLATES, _SUV_FLAWS, _SUV_HIDDEN)

TASKS = {t.NAME: t for t in (FIRSTORDER, TEXTURE, STATS, SUV)}
DEFAULT_TASKS = ",".join(TASKS)
