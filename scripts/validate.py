"""Validate the workflow definitions in the repo.

    python scripts/validate.py              # structure, references, expressions, dry run
    python scripts/validate.py --env uat    # ... plus check changed versions are bumped vs the share

Exit code 0 when there are no errors (warnings are allowed).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wft.config import ENVIRONMENTS, REPO_ROOT, ConfigError, check_share_marker, load_config  # noqa: E402
from wft.validation import validate_repo  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--definitions", type=Path, default=REPO_ROOT / "definitions")
    parser.add_argument("--env", choices=ENVIRONMENTS, help="also compare with what's published on this share")
    args = parser.parse_args(argv)

    share_root = None
    if args.env:
        try:
            config = load_config(args.env)
            check_share_marker(config)
        except ConfigError as e:
            print(f"validate: {e}", file=sys.stderr)
            return 2
        share_root = config.share_root

    report = validate_repo(args.definitions, share_root)
    for issue in report.issues:
        print(issue)
    print(f"\nChecked {len(report.checked)} workflow(s): {len(report.errors)} error(s), "
          f"{len(report.issues) - len(report.errors)} warning(s)"
          + ("" if share_root else " (version bumps not checked; pass --env to compare with a share)"))
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
