"""Check WFT's TT connection with your own key: authenticate, list accounts and users, optionally fetch one.

    python scripts/tt_smoke.py --env uat
    python scripts/tt_smoke.py --env uat --account ACC1

Prints counts and a few names only; never prints your key, secret or token.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wft.adapters import AdapterUnavailable, NotFoundInSystem  # noqa: E402
from wft.adapters.tt import ENDPOINTS, TTAdapter  # noqa: E402
from wft.config import ENVIRONMENTS, ConfigError, load_config  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--env", choices=ENVIRONMENTS, required=True)
    parser.add_argument("--account", help="also fetch this account and its limits")
    args = parser.parse_args(argv)
    try:
        config = load_config(args.env)
    except ConfigError as e:
        print(f"tt_smoke: {e}", file=sys.stderr)
        return 2
    if not config.tt:
        print("tt_smoke: TT isn't configured in this env file (WFT_TT_BASE_URL etc.)", file=sys.stderr)
        return 2
    tt = TTAdapter(config.tt)
    print(f"Checking {config.tt.label} at {config.tt.base_url}")
    ok = True
    for op in ("accounts", "users"):
        try:
            items = tt.list(op)
            print(f"  {op:<16} OK  {len(items)} found, e.g. {', '.join(e.name for e in items[:3]) or '-'}")
        except AdapterUnavailable as e:
            ok = False
            print(f"  {op:<16} FAILED  {e}   [{' '.join(ENDPOINTS[op])}]")
    if args.account:
        for op in ("account", "account_limits"):
            try:
                fetched = tt.fetch(op, args.account)
                keys = sorted(fetched.values)[:12] if isinstance(fetched.values, dict) else type(fetched.values).__name__
                print(f"  {op:<16} OK  fields: {json.dumps(keys)}")
            except (AdapterUnavailable, NotFoundInSystem) as e:
                ok = False
                print(f"  {op:<16} FAILED  {e}   [{' '.join(ENDPOINTS[op])}]")
    print("All good." if ok else "Some calls failed: compare ENDPOINTS in wft/adapters/tt.py with the TT API docs.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
