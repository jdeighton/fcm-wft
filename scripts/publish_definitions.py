"""Validate, then publish workflow definitions to an environment's share.

    python scripts/publish_definitions.py --env uat
    python scripts/publish_definitions.py --env prod

Publishing to prod requires the definitions folder to be committed in git.
Nothing is published if validation reports any error.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wft.config import ENVIRONMENTS, REPO_ROOT, ConfigError, check_share_marker, load_config  # noqa: E402
from wft.publishing import PublishError, git_state, publish  # noqa: E402
from wft.repository import default_identity  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--env", choices=ENVIRONMENTS, required=True)
    parser.add_argument("--definitions", type=Path, default=REPO_ROOT / "definitions")
    args = parser.parse_args(argv)

    try:
        config = load_config(args.env)
        check_share_marker(config)
    except ConfigError as e:
        print(f"publish: {e}", file=sys.stderr)
        return 2

    commit, dirty = git_state(REPO_ROOT)
    if args.env == "prod" and (commit is None or dirty):
        print("publish: refusing to publish to prod: definitions/ has uncommitted changes (or this isn't a git checkout).",
              file=sys.stderr)
        return 2
    if dirty:
        print("publish: note: definitions/ has uncommitted changes; the manifest will record that.")

    user, host = default_identity()
    try:
        result = publish(args.definitions, config.share_root, f"{user}@{host}", commit=commit, dirty=dirty)
    except PublishError as e:
        if e.report:
            for issue in e.report.issues:
                print(issue, file=sys.stderr)
        print(f"publish: {e}", file=sys.stderr)
        return 1

    for issue in result.report.issues:
        print(issue)
    for key in result.published:
        print(f"published  {key}")
    for key in result.unchanged:
        print(f"unchanged  {key}")
    print(f"\n{len(result.published)} published, {len(result.unchanged)} already up to date on {args.env}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
