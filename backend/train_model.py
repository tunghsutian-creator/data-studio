"""Train the optional local fallback classifier from a reviewed CSV manifest.

The manifest must contain ``path`` and ``label`` columns.  A ``group_id``
column is strongly recommended so files from the same acquisition package,
exact-duplicate group, or derived-data family cannot cross the validation
boundary.

Example::

    python -m backend.train_model labels.csv model.joblib --root "C:\\Research Data"

scikit-learn and joblib are optional runtime dependencies: importing and using
the rule classifier does not import either package.
"""

from __future__ import annotations

import argparse
import csv
from datetime import UTC, datetime
import json
from pathlib import Path
from typing import Any, Sequence

from .classifier import MODEL_FEATURE_VERSION, extract_model_features
from .taxonomy import Modality, normalize_modality


def _load_manifest(
    manifest_path: Path, root: Path | None,
) -> tuple[list[Path], list[str], list[str | None]]:
    paths: list[Path] = []
    labels: list[str] = []
    groups: list[str | None] = []
    with manifest_path.open("r", encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames is None or not {"path", "label"}.issubset(reader.fieldnames):
            raise ValueError("Manifest must contain path and label columns")
        for row_number, row in enumerate(reader, start=2):
            raw_path = (row.get("path") or "").strip()
            raw_label = (row.get("label") or "").strip()
            if not raw_path or not raw_label:
                raise ValueError(f"Manifest row {row_number} has an empty path or label")
            path = Path(raw_path)
            if not path.is_absolute() and root is not None:
                path = root / path
            if not path.is_file():
                raise FileNotFoundError(f"Manifest row {row_number} does not exist: {path}")
            label = normalize_modality(raw_label)
            if label is Modality.UNKNOWN:
                raise ValueError(f"Manifest row {row_number} uses UNKNOWN as a training label")
            paths.append(path)
            labels.append(label.value)
            raw_group = (row.get("group_id") or "").strip()
            groups.append(raw_group or None)
    if len(paths) < 2 or len(set(labels)) < 2:
        raise ValueError("Training requires at least two rows and two distinct labels")
    return paths, labels, groups


def _optional_dependencies() -> tuple[Any, Any, Any, Any, Any]:
    try:
        import joblib  # type: ignore[import-not-found]
        from sklearn.feature_extraction import DictVectorizer  # type: ignore[import-not-found]
        from sklearn.linear_model import LogisticRegression  # type: ignore[import-not-found]
        from sklearn.metrics import classification_report  # type: ignore[import-not-found]
        from sklearn.model_selection import GroupShuffleSplit  # type: ignore[import-not-found]
        from sklearn.pipeline import Pipeline  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise RuntimeError(
            "Training requires the optional packages scikit-learn and joblib"
        ) from exc
    return joblib, DictVectorizer, LogisticRegression, classification_report, (GroupShuffleSplit, Pipeline)


def _build_pipeline(dict_vectorizer: Any, logistic_regression: Any, pipeline_type: Any) -> Any:
    return pipeline_type(
        [
            ("vectorizer", dict_vectorizer(sparse=True)),
            (
                "classifier",
                logistic_regression(
                    class_weight="balanced",
                    max_iter=2_000,
                    random_state=42,
                ),
            ),
        ]
    )


def train_from_manifest(
    manifest_path: Path,
    output_path: Path,
    *,
    root: Path | None = None,
    evaluate: bool = True,
) -> dict[str, Any]:
    """Train and persist a path-label-free local model artifact."""

    joblib, dict_vectorizer, logistic_regression, classification_report, extras = _optional_dependencies()
    group_shuffle_split, pipeline_type = extras
    paths, labels, groups = _load_manifest(manifest_path, root)
    features = [extract_model_features(path) for path in paths]
    report: dict[str, Any] = {
        "rows": len(paths),
        "classes": sorted(set(labels)),
        "feature_version": MODEL_FEATURE_VERSION,
        "validation": None,
    }

    usable_groups = all(group is not None for group in groups) and len(set(groups)) >= 2
    if evaluate and usable_groups and len(paths) >= 5:
        splitter = group_shuffle_split(n_splits=1, test_size=0.2, random_state=42)
        train_indices, test_indices = next(splitter.split(features, labels, groups))
        train_labels = [labels[index] for index in train_indices]
        if len(set(train_labels)) >= 2:
            validation_model = _build_pipeline(dict_vectorizer, logistic_regression, pipeline_type)
            validation_model.fit([features[index] for index in train_indices], train_labels)
            test_labels = [labels[index] for index in test_indices]
            predictions = validation_model.predict([features[index] for index in test_indices])
            report["validation"] = {
                "strategy": "group-shuffle",
                "train_rows": len(train_indices),
                "test_rows": len(test_indices),
                "metrics": classification_report(
                    test_labels,
                    predictions,
                    output_dict=True,
                    zero_division=0,
                ),
            }
        else:
            report["validation"] = {
                "strategy": "skipped",
                "reason": "group split left fewer than two classes in training",
            }
    elif evaluate:
        report["validation"] = {
            "strategy": "skipped",
            "reason": "safe validation requires group_id for every row and at least five rows",
        }

    model = _build_pipeline(dict_vectorizer, logistic_regression, pipeline_type)
    model.fit(features, labels)
    artifact = {
        "schema_version": 1,
        "feature_version": MODEL_FEATURE_VERSION,
        "name": output_path.stem,
        "trained_at": datetime.now(UTC).isoformat(),
        "training_rows": len(paths),
        "classes": list(model.classes_),
        "model": model,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(artifact, output_path)
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path, help="Reviewed CSV with path,label[,group_id]")
    parser.add_argument("output", type=Path, help="Destination .joblib artifact")
    parser.add_argument("--root", type=Path, help="Resolve relative manifest paths under this root")
    parser.add_argument("--no-evaluate", action="store_true", help="Skip group-aware validation")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report = train_from_manifest(
        args.manifest,
        args.output,
        root=args.root,
        evaluate=not args.no_evaluate,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
