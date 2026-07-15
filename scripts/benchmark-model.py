"""Run the Phase 1 local-AI feasibility benchmark.

Reports contain data-derived identifiers and therefore must be written outside
the Git repository. The source catalog is opened immutable/read-only and raw
files are never modified.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from backend.ai.benchmark import (  # noqa: E402
    BenchmarkProfile,
    LlamaCppClient,
    assert_report_path_is_outside_repository,
    build_evidence_pack,
    run_benchmark,
    select_diagnostic_datasets,
)
from backend.ai.model_lock import load_model_lock  # noqa: E402
from backend.ai.provider import ProviderIdentity  # noqa: E402
from backend.paths import RootMapper  # noqa: E402


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _write_report(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def _parser() -> argparse.ArgumentParser:
    data_root = Path(r"C:\research data")
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, default=data_root / "catalog" / "academic_vault.sqlite3")
    parser.add_argument("--reference-root", type=Path, default=data_root / "data ref")
    parser.add_argument("--inbox-root", type=Path, default=data_root / "inbox")
    parser.add_argument("--vault-root", type=Path, default=data_root / "vault")
    parser.add_argument(
        "--profile",
        type=Path,
        default=REPOSITORY_ROOT / "profiles" / "windows-rtx5080.json",
    )
    parser.add_argument(
        "--model-lock",
        type=Path,
        default=REPOSITORY_ROOT / "profiles" / "windows-model-lock.json",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=data_root / "benchmark-results" / f"qwen3vl-q8-{timestamp}.json",
    )
    parser.add_argument("--limit", type=int, default=30)
    parser.add_argument("--repeat-probe", type=int, default=3)
    parser.add_argument(
        "--prepare-only",
        action="store_true",
        help="Build bounded evidence in memory and write only a local preparation summary; do not call a model.",
    )
    return parser


def main() -> int:
    args = _parser().parse_args()
    output = assert_report_path_is_outside_repository(args.output, REPOSITORY_ROOT)
    roots = {
        "reference": args.reference_root.resolve(strict=True),
        "inbox": args.inbox_root.resolve(strict=False),
        "vault": args.vault_root.resolve(strict=False),
    }
    for name, root in roots.items():
        if _is_within(output, root):
            raise ValueError(f"benchmark output may not be written inside the {name} root")

    profile = BenchmarkProfile.load(args.profile)
    model_lock = load_model_lock(args.model_lock)
    if model_lock.profile_id != profile.profile_id:
        raise ValueError("model lock profile_id does not match benchmark profile")
    mapper = RootMapper(roots)
    diagnostics = select_diagnostic_datasets(args.catalog, args.limit)
    packs = [build_evidence_pack(args.catalog, item, mapper, profile) for item in diagnostics]
    if args.prepare_only:
        payload = {
            "report_schema_version": 1,
            "kind": "preparation-summary",
            "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "profile_id": profile.profile_id,
            "model_lock": model_lock.public_summary(),
            "catalog_open_mode": "immutable-read-only",
            "case_count": len(packs),
            "cases": [
                {
                    "dataset_id": pack.dataset.dataset_id,
                    "expected_modality": pack.dataset.expected_modality,
                    "rule_confidence": pack.dataset.rule_confidence,
                    "asset_count": pack.asset_count,
                    "bounded_image_count": len(pack.image_data_urls),
                }
                for pack in packs
            ],
        }
    else:
        identity = ProviderIdentity(
            provider=profile.provider,
            profile_id=profile.profile_id,
            model_id=profile.model_id,
            quantization=profile.quantization,
            device=profile.device,
            model_revision=model_lock.model.revision,
            runtime_release=model_lock.runtime.release,
            runtime_commit=model_lock.runtime.commit,
        )
        client = LlamaCppClient(profile, identity=identity)
        try:
            health = client.health()
            payload = run_benchmark(packs, client, profile, repeat_probe=args.repeat_probe)
            payload["model_lock"] = model_lock.public_summary()
            payload["server_health"] = health
        finally:
            client.close()
    _write_report(output, payload)
    print(json.dumps({"output": str(output), "case_count": len(packs)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
