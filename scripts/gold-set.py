"""Prepare or validate a blinded Phase 3 local gold set outside Git."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from backend.ai.benchmark import assert_report_path_is_outside_repository  # noqa: E402
from backend.ai.gold_set import (  # noqa: E402
    build_gold_candidate_bundle,
    validate_gold_review,
    write_gold_candidate_bundle,
)


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _outside_data_sources(path: Path, data_root: Path) -> None:
    protected = {
        "reference": data_root / "data ref",
        "inbox": data_root / "inbox",
        "vault": data_root / "vault",
        "catalog": data_root / "catalog",
        "models": data_root / "models",
        "backups": data_root / "backups",
        "exports": data_root / "exports",
    }
    for name, root in protected.items():
        if _is_within(path, root.resolve(strict=False)):
            raise ValueError(f"gold-set artifacts may not be written inside the {name} root")


def _parser() -> argparse.ArgumentParser:
    data_root = Path(r"C:\research data")
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare = subparsers.add_parser("prepare", help="Create a blinded candidate bundle")
    prepare.add_argument(
        "--catalog",
        type=Path,
        default=data_root / "catalog" / "academic_vault.sqlite3",
    )
    prepare.add_argument(
        "--output-dir",
        type=Path,
        default=data_root / "evaluation" / "gold-set-phase3-v1",
    )
    prepare.add_argument("--target", type=int, default=180)
    prepare.add_argument("--seed", default="phase3-v1")
    prepare.add_argument("--source", default="reference", choices=("reference", "inbox"))

    validate = subparsers.add_parser("validate", help="Validate completed human labels")
    validate.add_argument("--review", type=Path, required=True)
    validate.add_argument("--audit", type=Path, required=True)
    validate.add_argument("--minimum-included", type=int, default=150)
    return parser


def main() -> int:
    args = _parser().parse_args()
    data_root = Path(r"C:\research data").resolve(strict=False)
    if args.command == "prepare":
        if not 150 <= args.target <= 200:
            raise ValueError("Phase 3 gold-set target must be between 150 and 200")
        output = assert_report_path_is_outside_repository(
            args.output_dir,
            REPOSITORY_ROOT,
        )
        _outside_data_sources(output, data_root)
        bundle = build_gold_candidate_bundle(
            args.catalog,
            target=args.target,
            seed=args.seed,
            source_kind=args.source,
        )
        paths = write_gold_candidate_bundle(
            output,
            bundle,
            repository_root=REPOSITORY_ROOT,
        )
        print(
            json.dumps(
                {
                    **paths,
                    "candidate_count": len(bundle["review_rows"]),
                    "selection_manifest_sha256": bundle["audit"]["selection_manifest_sha256"],
                    "human_labels_prefilled": 0,
                },
                ensure_ascii=False,
            )
        )
        return 0

    review = assert_report_path_is_outside_repository(args.review, REPOSITORY_ROOT)
    audit = assert_report_path_is_outside_repository(args.audit, REPOSITORY_ROOT)
    report = validate_gold_review(
        review,
        audit,
        minimum_included=args.minimum_included,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(
            json.dumps(
                {"status": "invalid", "error": str(exc)},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        raise SystemExit(2) from None
