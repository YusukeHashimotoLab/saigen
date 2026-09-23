#!/usr/bin/env python
"""Precision / accuracy statistics for a dispensing run.

Reads the CSV artefacts of one run folder (``logs/<date>/<name>_<timestamp>/``)
written by :class:`~src.flow.experiment_logger.ExperimentLogger` and
:class:`~src.flow.accuracy_logger.DispenseAccuracyLogger`, and reports the
statistics used in the paper: mean, standard deviation, variance, CV
(repeatability) and the bias against the nominal dispensed mass (trueness).

Two sources, in this order:

1. ``dispense_accuracy.csv`` - one row per ``dispense`` paired with the
   ``measure_weight`` that follows it; the nominal mass is taken from the file
   (``target_volume_mL`` x ``density_g_per_mL``) unless overridden.
2. ``measurements.csv`` - every step of the run; the ``weight_g`` column of the
   ``measure_weight`` rows is used. The nominal mass must then come from
   ``--nominal`` or ``--volume``/``--density``.

The balance is tared before each dispense, so each weight is the net mass of
that dispense.

Repeatability is only meaningful between repeats of the *same* nominal volume, so
the statistics are computed **per target volume** (``target_volume_mL`` of
dispense_accuracy.csv): a run that dispenses 1, 3 and 5 mL yields three reports,
never one pooled CV. measurements.csv carries no volume, so it forms one group.
With fewer than two measurements in a group the sample standard deviation,
variance and CV are *undefined* and reported as such (not as 0.0).

Usage:
    python -m src.gui.analyze_dispense_log logs/2026-09-08/zif8_..._120000
    python -m src.gui.analyze_dispense_log <run folder> --volume 5 --density 0.998
    python -m src.gui.analyze_dispense_log <run folder>/measurements.csv --nominal 4.99

The previous version of this script scraped weights out of the GUI's text log;
runs now always produce these CSVs (from the GUI and from the CLI alike), so
the log-parsing path is gone.
"""
import argparse
import csv
import os
import statistics
import sys

ACCURACY_CSV = "dispense_accuracy.csv"
MEASUREMENTS_CSV = "measurements.csv"


def _read_csv(path):
    # ExperimentLogger writes utf-8-sig so Excel opens the files cleanly.
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def _to_float(value):
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def read_accuracy_csv(path):
    """Return ``(weights, nominal_from_file)`` from a dispense_accuracy.csv."""
    weights, nominals = [], []
    for row in _read_csv(path):
        weight = _to_float(row.get("measured_weight_g"))
        if weight is None:
            continue
        weights.append(weight)
        expected = _to_float(row.get("expected_weight_g"))
        if expected is None:
            volume = _to_float(row.get("target_volume_mL"))
            density = _to_float(row.get("density_g_per_mL"))
            expected = volume * density if (volume and density) else None
        if expected is not None:
            nominals.append(expected)
    nominal = statistics.fmean(nominals) if nominals else None
    return weights, nominal


def read_accuracy_groups(path):
    """Group a dispense_accuracy.csv by target volume.

    Returns:
        list of ``{"volume": float|None, "nominal": float|None, "values": [...]}``
        in order of first appearance.
    """
    groups = {}
    for row in _read_csv(path):
        weight = _to_float(row.get("measured_weight_g"))
        if weight is None:
            continue
        volume = _to_float(row.get("target_volume_mL"))
        key = None if volume is None else round(volume, 4)
        group = groups.setdefault(key, {"volume": key, "nominals": [], "values": []})
        group["values"].append(weight)
        expected = _to_float(row.get("expected_weight_g"))
        if expected is None:
            density = _to_float(row.get("density_g_per_mL"))
            expected = volume * density if (volume and density) else None
        if expected is not None:
            group["nominals"].append(expected)
    out = []
    for g in groups.values():
        nominals = g.pop("nominals")
        g["nominal"] = statistics.fmean(nominals) if nominals else None
        out.append(g)
    return out


def load_groups(target):
    """Resolve a run folder or CSV path to ``(groups, source)``.

    ``groups`` is the output of :func:`read_accuracy_groups`; for a
    measurements.csv it is a single group with ``volume`` and ``nominal`` None.
    """
    path = target
    if os.path.isdir(target):
        accuracy = os.path.join(target, ACCURACY_CSV)
        if os.path.exists(accuracy):
            groups = read_accuracy_groups(accuracy)
            if groups:
                return groups, accuracy
        path = os.path.join(target, MEASUREMENTS_CSV)
        if not os.path.exists(path):
            raise FileNotFoundError(
                f"Neither {ACCURACY_CSV} nor {MEASUREMENTS_CSV} found in {target}"
            )
    elif os.path.basename(target) == ACCURACY_CSV:
        return read_accuracy_groups(target), target
    values = read_measurements_csv(path)
    return ([{"volume": None, "nominal": None, "values": values}] if values else []), path


def read_measurements_csv(path):
    """Return the ``weight_g`` values of the measure_weight rows, in order."""
    weights = []
    for row in _read_csv(path):
        if row.get("status") not in (None, "", "ok"):
            continue
        weight = _to_float(row.get("weight_g"))
        if weight is not None:
            weights.append(weight)
    return weights


def load_weights(target):
    """Resolve a run folder or a CSV path to ``(weights, nominal, source)``."""
    if os.path.isdir(target):
        accuracy = os.path.join(target, ACCURACY_CSV)
        if os.path.exists(accuracy):
            weights, nominal = read_accuracy_csv(accuracy)
            if weights:
                return weights, nominal, accuracy
        measurements = os.path.join(target, MEASUREMENTS_CSV)
        if os.path.exists(measurements):
            return read_measurements_csv(measurements), None, measurements
        raise FileNotFoundError(
            f"Neither {ACCURACY_CSV} nor {MEASUREMENTS_CSV} found in {target}"
        )

    if os.path.basename(target) == ACCURACY_CSV:
        weights, nominal = read_accuracy_csv(target)
        return weights, nominal, target
    return read_measurements_csv(target), None, target


def summarize(values, nominal):
    """Statistics of one group (one target volume).

    ``stdev``, ``variance`` and ``cv`` are sample statistics (n-1) and are
    ``None`` - undefined - when there are fewer than two values.
    """
    n = len(values)
    mean = statistics.fmean(values)
    stdev = statistics.stdev(values) if n >= 2 else None
    variance = statistics.variance(values) if n >= 2 else None
    if stdev is None:
        cv = None
    else:
        cv = (stdev / mean * 100) if mean else float("nan")
    return {
        "n": n,
        "mean": mean,
        "median": statistics.median(values),
        "stdev": stdev,
        "variance": variance,
        "cv": cv,
        "min": min(values),
        "max": max(values),
        "range": max(values) - min(values),
        "nominal": nominal,
        "bias": mean - nominal,
        "bias_pct": (mean - nominal) / nominal * 100 if nominal else float("nan"),
    }


UNDEFINED = "undefined (n < 2)"


def _fmt(value, spec, unit=""):
    return UNDEFINED if value is None else f"{value:{spec}}{unit}"


def format_report(values, source, s, volume=None) -> str:
    line = "=" * 60
    title = f"Dispensing accuracy report  (source: {source})"
    out = [line, title]
    if volume is not None:
        out.append(f"target volume: {volume:g} mL")
    out += [line, "",
           "[ measurements ]", f"{'#':>3}  {'mass (g)':>10}  {'vs nominal':>11}"]
    for i, v in enumerate(values, 1):
        out.append(f"{i:>3}  {v:>10.3f}  {v - s['nominal']:>+11.3f}")
    out += [
        "",
        "[ statistics ]",
        f"  n                 : {s['n']}",
        f"  mean              : {s['mean']:.4f} g",
        f"  median            : {s['median']:.4f} g",
        f"  std. deviation    : {_fmt(s['stdev'], '.4f', ' g')}   (sample, n-1)",
        f"  variance          : {_fmt(s['variance'], '.5f', ' g^2')}",
        f"  CV                : {_fmt(s['cv'], '.2f', ' %')}      <- repeatability (precision)",
        f"  min / max         : {s['min']:.3f} / {s['max']:.3f} g",
        f"  range             : {s['range']:.3f} g",
        "",
        "[ against the nominal mass ]",
        f"  nominal           : {s['nominal']:.4f} g",
        f"  bias              : {s['bias']:+.4f} g ({s['bias_pct']:+.2f} %)"
        f"  <- trueness (accuracy)",
        line,
    ]
    return "\n".join(out)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Precision/accuracy statistics for a dispensing run"
    )
    p.add_argument(
        "run",
        help=f"run folder (logs/<date>/<name>_<timestamp>/) or a "
             f"{ACCURACY_CSV} / {MEASUREMENTS_CSV} path",
    )
    p.add_argument("--nominal", type=float, default=None,
                   help="nominal dispensed mass in g (overrides --volume/--density "
                        "and the value stored in dispense_accuracy.csv)")
    p.add_argument("--volume", type=float, default=5.0,
                   help="nominal dispensed volume in mL (default 5.0)")
    p.add_argument("--density", type=float, default=0.998,
                   help="liquid density in g/mL (default 0.998 = water at ~22 C)")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)

    try:
        groups, source = load_groups(args.run)
    except (FileNotFoundError, NotADirectoryError, OSError) as e:
        print(f"Could not read the run: {e}", file=sys.stderr)
        return 2

    groups = [g for g in groups if g["values"]]
    if not groups:
        print("No weight measurements found. Does the run contain measure_weight "
              "steps that followed a dispense?", file=sys.stderr)
        return 1
    if args.nominal is not None and len(groups) > 1:
        print("Note: --nominal applies to every target volume of this run; "
              "use --nominal only for a single-volume run.", file=sys.stderr)

    reports = []
    for g in groups:
        if args.nominal is not None:
            nominal = args.nominal
        elif g["nominal"] is not None:
            nominal = g["nominal"]
        else:
            nominal = (g["volume"] if g["volume"] is not None else args.volume) * args.density
        reports.append(format_report(g["values"], source,
                                     summarize(g["values"], nominal), volume=g["volume"]))
    print("\n\n".join(reports))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
