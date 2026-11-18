"""Validation of workflow definitions in the repo, including a dry run of every path."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

import jsonschema
import yaml

from wft import __version__, engine
from wft.definitions import (MANIFEST_FILE, PUBLISHED_DIR, DefinitionError, WorkflowDef, parse_checklist_md,
                             parse_version, parse_workflow, referenced_files)
from wft.events import Event
from wft.adapters import known_fetch_operation, known_list_operation
from wft.artefacts import mapping_errors
from wft.expressions import ExpressionError, is_template, parse
from wft.configfiles import CONFIG_FILES, REPO_CONFIG_DIR

WORKFLOW_SCHEMA = json.loads((Path(__file__).parent / "schemas" / "workflow.schema.json").read_text(encoding="utf-8"))
_SCHEMA_VALIDATOR = jsonschema.Draft202012Validator(WORKFLOW_SCHEMA)
MAX_PATHS = 500
MAX_ACTIONS_PER_PATH = 200


@dataclass
class Issue:
    source: str
    message: str
    level: str = "error"

    def __str__(self) -> str:
        return f"{self.level.upper():7} {self.source}: {self.message}"


@dataclass
class Report:
    issues: list[Issue] = field(default_factory=list)
    checked: list[str] = field(default_factory=list)

    @property
    def errors(self) -> list[Issue]:
        return [i for i in self.issues if i.level == "error"]

    @property
    def ok(self) -> bool:
        return not self.errors


# --- the bundle of files that make up one workflow version ----------------

def bundle_files(workflow_file: Path, definitions_dir: Path, data: dict) -> dict[str, bytes]:
    """Published path -> content for a workflow and every file it references."""
    files = {"workflow.yaml": workflow_file.read_bytes()}
    for ref in referenced_files(data):
        path = definitions_dir / ref
        if path.is_file():
            files[ref] = path.read_bytes()
    return files


def fingerprint(files: dict[str, bytes]) -> dict[str, str]:
    return {name: hashlib.sha256(content).hexdigest() for name, content in sorted(files.items())}


def published_manifest(share_root: Path, workflow_id: str, version: int) -> dict | None:
    path = share_root / PUBLISHED_DIR / workflow_id / str(version) / MANIFEST_FILE
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None


def published_versions(share_root: Path, workflow_id: str) -> list[int]:
    folder = share_root / PUBLISHED_DIR / workflow_id
    return sorted(int(d.name) for d in folder.glob("*") if d.name.isdigit() and (d / MANIFEST_FILE).is_file())


# --- whole-repo validation -------------------------------------------------

def validate_repo(definitions_dir: Path, share_root: Path | None = None,
                  app_version: str = __version__) -> Report:
    report = Report()
    loaded: dict[str, tuple[Path, dict]] = {}
    for path in sorted((definitions_dir / "workflows").glob("*.yaml")):
        source = path.relative_to(definitions_dir).as_posix()
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as e:
            report.issues.append(Issue(source, f"not valid YAML: {e}"))
            continue
        if not isinstance(data, dict):
            report.issues.append(Issue(source, "must be a YAML mapping"))
            continue
        wid = data.get("id")
        if wid in loaded:
            report.issues.append(Issue(source, f"duplicate workflow id {wid!r} (also in {loaded[wid][0].name})"))
            continue
        if isinstance(wid, str) and path.stem != wid:
            report.issues.append(Issue(source, f"file name must match the workflow id ({wid}.yaml)"))
        loaded[wid] = (path, data)

    for name, check in CONFIG_FILES.items():
        path = definitions_dir / REPO_CONFIG_DIR / name
        source = (REPO_CONFIG_DIR / name).as_posix()
        if path.is_file():
            try:
                report.issues += [Issue(source, m) for m in check(yaml.safe_load(path.read_text(encoding="utf-8")))]
            except yaml.YAMLError as e:
                report.issues.append(Issue(source, f"not valid YAML: {e}"))

    known_ids = {k for k in loaded if isinstance(k, str)}
    # Sub-workflows mustn't start each other in a loop (A starts B starts A).
    calls = {wid: [st.get("workflow") for st in (data.get("steps") or []) if isinstance(st, dict)
                   and st.get("type") == "subworkflow" and st.get("workflow") in loaded]
             for wid, (_, data) in loaded.items() if isinstance(wid, str)}
    loop = _find_cycle(calls)
    if loop:
        report.issues.append(Issue("workflows", f"sub-workflows start each other in a loop: {' -> '.join(loop)}"))
    for wid, (path, data) in loaded.items():
        source = path.relative_to(definitions_dir).as_posix()
        report.checked.append(source)
        issues = check_definition(data, definitions_dir, known_ids, app_version)
        report.issues += [Issue(source, m, lvl) for lvl, m in issues]
        if any(lvl == "error" for lvl, _ in issues):
            continue
        defn = parse_workflow(_choose_decisions_by_hand(data), definitions_dir)
        report.issues += [Issue(source, m) for m in dry_run(defn, load_samples(definitions_dir, wid) or {})]
        if share_root is not None:
            report.issues += [Issue(source, m) for m in check_version_bump(path, definitions_dir, data, share_root)]
    return report


def check_version_bump(path: Path, definitions_dir: Path, data: dict, share_root: Path) -> list[str]:
    wid, version = data["id"], data["version"]
    versions = published_versions(share_root, wid)
    if versions and version < versions[-1]:
        return [f"version {version} is older than the published version {versions[-1]}"]
    manifest = published_manifest(share_root, wid, version)
    if manifest is None:
        return []
    current = fingerprint(bundle_files(path, definitions_dir, data))
    if current != manifest.get("files"):
        changed = sorted(set(current) ^ set(manifest["files"]) |
                         {k for k in current if manifest["files"].get(k) != current[k]})
        return [f"changed since version {version} was published ({', '.join(changed)}); bump the version"]
    return []


# --- one definition --------------------------------------------------------

def check_definition(data: dict, base: Path, known_ids: set[str],
                     app_version: str = __version__) -> list[tuple[str, str]]:
    """Static checks. Returns (level, message) pairs."""
    schema_errors = sorted(_SCHEMA_VALIDATOR.iter_errors(data), key=lambda e: list(e.absolute_path))
    if schema_errors:
        # When a step fails its per-type branch, "unevaluated property" errors for the same step are just noise.
        branch_failed = {tuple(e.absolute_path) for e in schema_errors if e.validator == "oneOf"}
        return [("error", f"{_where(e.absolute_path)}{msg}")
                for e in schema_errors
                if not (e.validator == "unevaluatedProperties" and tuple(e.absolute_path) in branch_failed)
                for msg in _schema_messages(e)]

    out: list[tuple[str, str]] = []
    err = lambda m: out.append(("error", m))  # noqa: E731
    steps = data["steps"]
    ids = [s["id"] for s in steps]
    step_ids = set(ids)

    for dup in sorted({i for i in ids if ids.count(i) > 1}):
        err(f"step id {dup!r} is used more than once")

    unsupported = sorted({s["type"] for s in steps} - engine.SUPPORTED_STEP_TYPES)
    if unsupported:
        err(f"step type(s) {', '.join(unsupported)} not supported by app version {app_version} "
            f"(supported: {', '.join(sorted(engine.SUPPORTED_STEP_TYPES))})")

    if data.get("min_app_version") and parse_version(data["min_app_version"]) > parse_version(app_version):
        out.append(("warning", f"requires app {data['min_app_version']}; this checkout is {app_version}"))

    # Referenced files
    inputs_schema = None
    for ref in referenced_files(data):
        path = base / ref
        if not path.is_file():
            err(f"referenced file not found: {ref}")
            continue
        try:
            if ref.startswith("schemas/"):
                schema = json.loads(path.read_text(encoding="utf-8"))
                jsonschema.Draft202012Validator.check_schema(schema)
                if any(s.get("schema") and f"schemas/{s['schema']}" == ref for s in data["steps"]) \
                        and schema.get("type") != "object":
                    err(f"{ref}: a form schema must have \"type\": \"object\"")
                if ref == f"schemas/{data.get('inputs_schema')}":
                    inputs_schema = schema
            elif ref.startswith("checklists/"):
                if not parse_checklist_md(path.read_text(encoding="utf-8")):
                    err(f"checklist {ref} has no '- item' lines")
            elif ref.startswith("mappings/"):
                for problem in mapping_errors(yaml.safe_load(path.read_text(encoding="utf-8"))):
                    err(f"{ref}: {problem}")
        except (ValueError, jsonschema.SchemaError, yaml.YAMLError) as e:
            err(f"{ref} is not valid: {str(e).splitlines()[0]}")

    # Trading-system operations
    for s in steps:
        if s["type"] == "verify" and not known_fetch_operation(s["adapter"]):
            err(f"step {s['id']!r}: unknown adapter operation {s['adapter']!r}")
        prefill = s.get("prefill")
        if prefill and not known_fetch_operation(prefill["operation"]):
            err(f"step {s['id']!r}: unknown prefill operation {prefill['operation']!r}")
    for s in steps:
        if s.get("schema") and (base / "schemas" / s["schema"]).is_file():
            try:
                form_schema = json.loads((base / "schemas" / s["schema"]).read_text(encoding="utf-8"))
            except ValueError:
                continue
            for where, name in _picklists(form_schema):
                if not known_list_operation(name):
                    err(f"schemas/{s['schema']}: {where}: unknown picklist {name!r}")

    # Dependencies and routing
    for s in steps:
        for dep in s.get("depends_on", ()):
            if dep == s["id"]:
                err(f"step {s['id']!r} depends on itself")
            elif dep not in step_ids:
                err(f"step {s['id']!r} depends on unknown step {dep!r}")
        for target, what in _routes(s):
            if target not in step_ids:
                err(f"step {s['id']!r}: {what} refers to unknown step {target!r}")
        for wf, what in ((s.get("workflow"), "sub-workflow"), ((s.get("compensation") or {}).get("workflow"), "compensation workflow")):
            if wf and wf not in known_ids:
                err(f"step {s['id']!r}: {what} {wf!r} is not a known workflow")
    deps = {s["id"]: [d for d in s.get("depends_on", ()) if d in step_ids] for s in steps}
    by_id = {s["id"]: s for s in steps}
    for s in steps:
        if s["type"] != "outcome":
            continue
        externals = [d for d in s.get("depends_on", ()) if by_id.get(d, {}).get("type") == "external_request"]
        for name, o in s["outcomes"].items():
            if o.get("next") in step_ids and s["id"] not in deps.get(o["next"], ()):
                err(f"outcome step {s['id']!r}: {name!r} goes to {o['next']!r}, which must depend on the outcome step")
            if "amend_and_resubmit" in (o.get("choices") or ()) and not externals:
                err(f"outcome step {s['id']!r}: {name!r} offers amend_and_resubmit, so it must follow an "
                    "external_request step (to know where to go back to)")
            cap = o.get("capture")
            source = cap if isinstance(cap, str) else (cap or {}).get("from")
            if source and inputs_schema is not None and not _schema_has(inputs_schema, source.split(".")):
                err(f"outcome step {s['id']!r}: {name!r} captures {source!r}, which is not in {data['inputs_schema']}")
            if source and "inputs_schema" not in data:
                err(f"outcome step {s['id']!r}: {name!r} captures {source!r} but the workflow has no inputs_schema")
    for s in steps:
        if s["type"] != "decision":
            continue
        targets = [b["next"] for b in s.get("branches", ())] + ([s["default"]] if s.get("default") else [])
        for t in targets:
            if t in step_ids and s["id"] not in deps.get(t, ()):
                err(f"decision {s['id']!r}: option {t!r} must depend on the decision step")
    for s in steps:
        target = (s.get("on_amend") or {}).get("route_to")
        if target in step_ids and target not in _ancestors(deps, s["id"]):
            err(f"step {s['id']!r}: on_amend.route_to {target!r} must be a step it depends on (directly or not)")
    cycle = _find_cycle(deps)
    if cycle:
        err(f"dependency cycle: {' -> '.join(cycle)}")
    else:
        for unreachable in _unreachable(steps):
            err(f"step {unreachable!r} can never be reached")

    # Fields consumed from the inputs
    consumed = [(s["id"], f) for s in steps for f in s.get("consumes", ())]
    if consumed and "inputs_schema" not in data:
        err("steps declare `consumes` but the workflow has no inputs_schema")
    elif inputs_schema is not None:
        for sid, f in consumed:
            if not _schema_has(inputs_schema, f.split(".")):
                err(f"step {sid!r} consumes {f!r}, which is not in {data['inputs_schema']}")

    # Expressions
    for where, text in _templates(data):
        try:
            template = parse(text)
        except ExpressionError as e:
            err(f"{where}: {e}")
            continue
        for path in template.paths:
            root, rest = path.parts[0], list(path.parts[1:])
            if root == "steps" and rest and rest[0] not in step_ids:
                err(f"{where}: refers to unknown step {rest[0]!r}")
            elif root == "inputs" and inputs_schema is not None and rest and not _schema_has(inputs_schema, rest):
                err(f"{where}: inputs.{'.'.join(rest)} is not in {data['inputs_schema']}")
            elif root == "item" and not any(s.get("for_each") for s in steps):
                err(f"{where}: `item` is only available in a for_each sub-workflow step")

    # Sample data for the dry run: samples/<id>.yaml maps each form step id to sample form data.
    form_steps = [s for s in steps if s["type"] == "form"]
    sample_path = f"samples/{data['id']}.yaml"
    samples = load_samples(base, data["id"])
    if form_steps and samples is None:
        err(f"form steps need sample data at {sample_path} for the dry run")
    elif samples is not None:
        if not isinstance(samples, dict):
            err(f"{sample_path} must map form step ids to sample data")
            samples = {}
        for extra in sorted(set(samples) - {s["id"] for s in form_steps}):
            err(f"{sample_path}: {extra!r} is not a form step")
        for s in form_steps:
            if s["id"] not in samples:
                err(f"{sample_path}: no sample for form step {s['id']!r}")
                continue
            schema_path = base / "schemas" / s["schema"]
            if schema_path.is_file():
                schema = json.loads(schema_path.read_text(encoding="utf-8"))
                for e in jsonschema.Draft202012Validator(schema).iter_errors(samples[s["id"]]):
                    err(f"{sample_path}: {s['id']}: {_where(e.absolute_path)}{e.message}")
        if inputs_schema is not None:
            merged = {}
            for s in form_steps:
                if not s.get("depends_on") and isinstance(samples.get(s["id"]), dict):
                    merged.update(samples[s["id"]])
            for e in jsonschema.Draft202012Validator(inputs_schema).iter_errors(merged):
                err(f"{sample_path}: inputs from the initial form(s) don't match {data['inputs_schema']}: "
                    f"{_where(e.absolute_path)}{e.message}")
    if inputs_schema is not None and not any(not s.get("depends_on") for s in form_steps):
        err("inputs_schema is set but there is no initial form step (a form with no depends_on) to capture inputs")

    try:
        parse_workflow(data, base)
    except DefinitionError as e:
        err(str(e))
    return out


def _choose_decisions_by_hand(data: dict) -> dict:
    """A copy whose decisions wait for a choice, so the dry run tries every option rather than the one the samples pick."""
    return {**data, "steps": [{**s, "_choose_by_hand": True} if s.get("type") == "decision" else s
                              for s in data["steps"]]}


def load_samples(base: Path, workflow_id: str) -> Any:
    path = base / "samples" / f"{workflow_id}.yaml"
    return yaml.safe_load(path.read_text(encoding="utf-8")) if path.is_file() else None


def _routes(step: dict) -> Iterator[tuple[str, str]]:
    if (step.get("on_amend") or {}).get("route_to"):
        yield step["on_amend"]["route_to"], "on_amend.route_to"
    for name, outcome in (step.get("outcomes") or {}).items():
        if outcome.get("next"):
            yield outcome["next"], f"outcome {name!r}"
    for i, branch in enumerate(step.get("branches") or ()):
        yield branch["next"], f"branch {i + 1}"
    if step.get("default"):
        yield step["default"], "default branch"


def _picklists(schema: Any, where: str = "") -> Iterator[tuple[str, str]]:
    """(field path, picklist name) for every `x-picklist` in a JSON Schema."""
    if isinstance(schema, dict):
        if isinstance(schema.get("x-picklist"), str):
            yield where or "(root)", schema["x-picklist"]
        for key, sub in (schema.get("properties") or {}).items():
            yield from _picklists(sub, f"{where}.{key}" if where else key)
        if isinstance(schema.get("items"), dict):
            yield from _picklists(schema["items"], f"{where}[]")


def _ancestors(deps: dict[str, list[str]], node: str) -> set[str]:
    seen: set[str] = set()
    todo = list(deps.get(node, ()))
    while todo:
        n = todo.pop()
        if n not in seen:
            seen.add(n)
            todo.extend(deps.get(n, ()))
    return seen


def _find_cycle(graph: dict[str, list[str]]) -> list[str] | None:
    WHITE, GREY, BLACK = 0, 1, 2
    colour = dict.fromkeys(graph, WHITE)
    stack: list[str] = []

    def visit(node: str) -> list[str] | None:
        colour[node] = GREY
        stack.append(node)
        for nxt in graph[node]:
            if colour[nxt] == GREY:
                return stack[stack.index(nxt):] + [nxt]
            if colour[nxt] == WHITE and (found := visit(nxt)):
                return found
        stack.pop()
        colour[node] = BLACK
        return None

    for node in graph:
        if colour[node] == WHITE and (found := visit(node)):
            return found
    return None


def _unreachable(steps: list[dict]) -> list[str]:
    """Steps that no start step leads to, following dependencies and routing."""
    edges: dict[str, set[str]] = {s["id"]: set() for s in steps}
    for s in steps:
        for dep in s.get("depends_on", ()):
            edges.setdefault(dep, set()).add(s["id"])
        for target, what in _routes(s):
            if not what.startswith("on_amend"):  # amend routes go backwards; they don't make a step reachable
                edges[s["id"]].add(target)
    seen = set()
    todo = [s["id"] for s in steps if not s.get("depends_on")]
    while todo:
        node = todo.pop()
        if node in seen:
            continue
        seen.add(node)
        todo.extend(edges.get(node, ()))
    return [s["id"] for s in steps if s["id"] not in seen]


def _schema_has(schema: dict, parts: list[str]) -> bool:
    """Whether a JSON Schema (object properties, array items) has the dotted field path. `*` matches any child."""
    node = schema
    for part in parts:
        while node.get("type") == "array" and isinstance(node.get("items"), dict):
            node = node["items"]
        if part == "*":
            return node.get("type") == "object" or "properties" in node
        props = node.get("properties") or {}
        if part not in props:
            return False
        node = props[part]
    return True


def _templates(data: dict) -> Iterator[tuple[str, str]]:
    """Every templated string in a definition, with where it is."""
    def walk(value: Any, where: str) -> Iterator[tuple[str, str]]:
        if isinstance(value, dict):
            for k, v in value.items():
                yield from walk(v, f"{where}.{k}")
        elif isinstance(value, list):
            for i, v in enumerate(value):
                yield from walk(v, f"{where}[{i}]")
        elif is_template(value) or (isinstance(value, str) and "}}" in value):
            yield where, value
    for s in data["steps"]:
        yield from walk({k: v for k, v in s.items() if k != "id"}, f"step {s['id']!r}")


def _where(path) -> str:
    parts = list(path)
    if not parts:
        return ""
    out = ""
    for p in parts:
        out += f"[{p}]" if isinstance(p, int) else (f".{p}" if out else p)
    return f"{out}: "


def _schema_messages(e: jsonschema.ValidationError) -> list[str]:
    if e.validator == "oneOf" and isinstance(e.instance, dict) and "type" in e.instance:
        # Report the errors from the branch for this step's own type, not every branch.
        mine = [c for c in e.context or ()
                if (e.validator_value[c.relative_schema_path[0]].get("properties", {})
                    .get("type", {}).get("const")) == e.instance["type"]]
        if mine:
            return [f"{e.instance['type']} step: {_where(c.relative_path)}{c.message}" for c in mine]
    return [e.message]


# --- dry run -----------------------------------------------------------------

def dry_run(defn: WorkflowDef, samples: dict | None = None) -> list[str]:
    """Walk every path through the workflow with the real engine; report dead ends and steps never reached."""
    problems: list[str] = []
    counter = iter(range(10**9))

    def stamp(request_id: str, new: engine.NewEvent) -> Event:
        n = next(counter)
        return Event(event_id=f"dry{n:09d}", request_id=request_id, seq=new.seq, type=new.type,
                     actor="dry-run", host="dry-run", app_version=__version__, definition=defn.key,
                     ts=f"2000-01-01T00:00:00.{n % 1000:03d}+00:00", payload=new.payload, prev=new.prev)

    start = engine.apply(None, stamp("dry", engine.create_request(defn, "Dry run")), defn)
    done_steps: set[str] = set()
    paths = 0
    todo: list[tuple[engine.RequestState, list[str]]] = [(start, [])]
    while todo:
        state, labels = todo.pop()
        options = engine.actions(state, defn, samples)
        if not options:
            if state.status in (engine.ACTIVE, engine.DRAFT):
                stuck = [s.id for s in state.steps if s.status != engine.DONE]
                problems.append(f"dry run: path [{', '.join(labels) or 'start'}] gets stuck with "
                                f"steps {', '.join(stuck)} unfinished")
            done_steps |= {s.id for s in state.steps if s.status == engine.DONE}
            paths += 1
            if paths >= MAX_PATHS:
                problems.append(f"dry run: more than {MAX_PATHS} paths; stopped exploring")
                break
            continue
        if len(labels) >= MAX_ACTIONS_PER_PATH:
            problems.append(f"dry run: path [{', '.join(labels[:5])}, ...] is longer than {MAX_ACTIONS_PER_PATH} actions (loop?)")
            continue
        for label, commands in options:
            if "amend_and_resubmit" in label and label in labels:
                continue  # one resubmission per path is enough to prove the route; more would loop forever
            s = state
            try:
                for command in commands:
                    s = engine.apply(s, stamp("dry", command(s)), defn)
            except engine.CommandError as e:
                problems.append(f"dry run: {label!r} after [{', '.join(labels) or 'start'}] failed: {e}")
                continue
            todo.append((s, labels + [label]))
    never = [s.id for s in defn.steps if s.id not in done_steps]
    if never and not problems:
        problems.append(f"dry run: step(s) {', '.join(never)} are never completed on any path")
    return problems
