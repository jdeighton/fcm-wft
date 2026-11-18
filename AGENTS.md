# AGENTS.md

## Agent skills

### Issue tracker

Issues are tracked in GitHub Issues on jdeighton/fcm-wft via the `gh` CLI. See `docs/agents/issue-tracker.md`.

### Triage labels

Default labels: needs-triage, needs-info, ready-for-agent, ready-for-human, wontfix. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: one `CONTEXT.md` and `docs/adr/` at the repo root. See `docs/agents/domain.md`.

## Development

- Python ≥ 3.11. Create a venv and install `requirements-dev.txt` (from Artifactory on firm machines).
- Run tests: `python -m pytest`
- Run locally: put a `uat.env` (see `.env.example`) in `%USERPROFILE%\.wft\` (or the folder named by `WFT_HOME`), create `meta/environment` containing `uat` in the share folder (the path must match `config/environments.yaml`), publish definitions with `python scripts/publish_definitions.py --env uat`, then `python -m wft --env uat` and open the printed link. To try unpublished definitions, set `WFT_DEFINITIONS_DIR` in the env file to the repo's `definitions` folder.
- Validate definitions: `python scripts/validate.py` (add `--env uat` to also check versions were bumped against the share). The definition format is `wft/schemas/workflow.schema.json`; expressions are in `wft/expressions.py`.
- The engine (`wft/engine.py`) must stay pure: no I/O, clocks or id generation. Storage goes through the `Repository` interface.
