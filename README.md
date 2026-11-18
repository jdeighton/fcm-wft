# WFT: trading environment workflow tracker

WFT guides operations users through setting up and maintaining the electronic trading
environment (clients, accounts, users, limits) and tracks each request from creation to
completion. It replaces paper-based tracking with structured forms, step checklists, an
append-only history and timing data.

Each user runs their own WFT backend on their Windows desktop. It serves the web UI on
`127.0.0.1` only. All shared data lives as files on a shared drive, so no shared server or
database is needed. See the [PRD](docs/PRD.md) for the full design.

> [!IMPORTANT]
> **Before production use, populate [`definitions/config/roles.yaml`](definitions/config/roles.yaml).**
>
> The file is committed with an empty `users` list. Until a roles file with at least one
> admin has been published to an environment, **anyone** who can run the publish script
> can publish definitions and team configuration to that environment. That's deliberate:
> it lets the first admin set things up. But a production share must not stay in that state.
>
> 1. List every team member's Windows username, with a display name.
> 2. Give the 1–2 people who will publish definitions `roles: [admin]`.
> 3. Make sure **you** are one of the admins: the publish script refuses a roles file
>    that would lock the publisher out.
> 4. Commit it, then publish with `python scripts/publish_definitions.py --env prod`.
>
> ```yaml
> users:
>   - username: jsmith        # Windows username (case doesn't matter)
>     name: Jane Smith        # shown in the UI
>     roles: [admin]          # can publish definitions and change configuration
>   - username: akhan
>     name: Ali Khan          # everyone listed (or not) is an Operator
> ```

## Before go-live checklist

| What | Where | Why |
|---|---|---|
| **Team and admins** | `definitions/config/roles.yaml` | See above: until it's published with an admin, publishing is open to anyone. |
| Real share paths (and later TT URLs) | `config/environments.yaml` | The committed patterns are placeholders (`\prod\` / `\uat\` path segments). They stop a UAT profile pointing at production. |
| Environment marker on each share | `<share>\meta\environment` | A file containing `prod` or `uat`. WFT refuses to start if it doesn't match `--env`. |
| Per-user env files | `%USERPROFILE%\.wft\prod.env`, `uat.env` | Copy from [`.env.example`](.env.example). Never commit real env files. |
| Published definitions | `python scripts/publish_definitions.py --env prod` | WFT only runs published workflows. Publishing to prod requires `definitions/` to be committed. |
| Python packages | Artifactory | Install `requirements.txt`. `jsonschema` pulls in `rpds-py`, which needs a Windows wheel. |

## Running locally

```bash
python -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
python -m pytest
```

1. Create a UAT share folder whose path matches `config/environments.yaml` (e.g. `...\wft\uat`),
   containing `meta\environment` with the text `uat`.
2. Put a `uat.env` in `%USERPROFILE%\.wft\` (or the folder named by `WFT_HOME`):
   `WFT_SHARE_ROOT=<that folder>`.
3. Publish the definitions with `python scripts/publish_definitions.py --env uat`.
4. Start WFT with `wft.bat --env uat` (or `python -m wft --env uat`). It opens the browser
   with a one-time sign-in link, which is also printed in the console.

## Working on workflows

- Definitions live in [`definitions/`](definitions): `workflows/<id>.yaml`, plus `schemas/`,
  `checklists/`, `mappings/`, `samples/` and `config/`. The format is
  [`wft/schemas/workflow.schema.json`](wft/schemas/workflow.schema.json).
- `python scripts/validate.py` checks structure, references and expressions, and dry-runs every
  path. Add `--env uat` to also check that changed workflows had their version bumped.
- Each published version is immutable. Running requests stay on the version they started on.

See [AGENTS.md](AGENTS.md) for development conventions and [docs/PRD.md](docs/PRD.md) for the design.
