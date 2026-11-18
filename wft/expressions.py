"""The restricted expression language used inside workflow definitions.

A template is plain text with `{{ ... }}` holes. Inside a hole:

    expr     := pipeline [ ("==" | "!=") operand ]
    pipeline := operand ( "|" filter )*
    operand  := path | 'string' | "string" | number | true | false | null
    path     := root ( "." name )*        root ∈ inputs, steps, item, request, row
    filter   := name [ "(" operand ( "," operand )* ")" ]

Filters: default(x), join(sep), upper, lower, length. Nothing else can be called,
so definitions can never run arbitrary code.

A template that is exactly one hole evaluates to the raw value (list, dict, ...);
anything else renders to a string.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable

ROOTS = ("inputs", "steps", "item", "request", "row")

_TOKEN = re.compile(r"""
    \s*(?:
      (?P<str>'[^']*'|"[^"]*")
    | (?P<num>-?\d+(?:\.\d+)?)
    | (?P<op>==|!=|[|().,])
    | (?P<name>[A-Za-z_][A-Za-z0-9_]*)
    )""", re.VERBOSE)
_HOLE = re.compile(r"\{\{(.*?)\}\}", re.DOTALL)


class ExpressionError(ValueError):
    pass


def _default(value, fallback):
    return fallback if value is None or value == "" else value


def _join(value, sep=", "):
    return sep.join(str(v) for v in value or ())


FILTERS: dict[str, Callable[..., Any]] = {
    "default": _default,
    "join": _join,
    "upper": lambda v: str(v).upper() if v is not None else None,
    "lower": lambda v: str(v).lower() if v is not None else None,
    "length": lambda v: len(v) if v is not None else 0,
}
_ARITY = {"default": (1, 1), "join": (0, 1), "upper": (0, 0), "lower": (0, 0), "length": (0, 0)}


@dataclass(frozen=True)
class Path:
    parts: tuple[str, ...]

    def resolve(self, context: dict) -> Any:
        value: Any = context
        for part in self.parts:
            if isinstance(value, dict):
                value = value.get(part)
            elif isinstance(value, list) and part.isdigit():
                value = value[int(part)] if int(part) < len(value) else None
            else:
                return None
        return value


@dataclass(frozen=True)
class Literal:
    value: Any

    def resolve(self, context: dict) -> Any:
        return self.value


Operand = Path | Literal


@dataclass(frozen=True)
class Expr:
    operand: Operand
    filters: tuple[tuple[str, tuple[Operand, ...]], ...]
    compare: tuple[str, Operand] | None

    def evaluate(self, context: dict) -> Any:
        value = self.operand.resolve(context)
        for name, args in self.filters:
            value = FILTERS[name](value, *(a.resolve(context) for a in args))
        if self.compare:
            op, other = self.compare
            equal = value == other.resolve(context)
            return equal if op == "==" else not equal
        return value

    @property
    def paths(self) -> list[Path]:
        operands = [self.operand, *(a for _, args in self.filters for a in args)]
        if self.compare:
            operands.append(self.compare[1])
        return [o for o in operands if isinstance(o, Path)]


@dataclass(frozen=True)
class Template:
    source: str
    parts: tuple[str | Expr, ...]

    def render(self, context: dict) -> Any:
        if len(self.parts) == 1 and isinstance(self.parts[0], Expr):
            return self.parts[0].evaluate(context)
        return "".join(p if isinstance(p, str) else _text(p.evaluate(context)) for p in self.parts)

    @property
    def paths(self) -> list[Path]:
        return [path for p in self.parts if isinstance(p, Expr) for path in p.paths]


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def parse(source: str) -> Template:
    parts: list[str | Expr] = []
    pos = 0
    for m in _HOLE.finditer(source):
        if m.start() > pos:
            parts.append(source[pos:m.start()])
        parts.append(_parse_expr(m.group(1), source))
        pos = m.end()
    tail = source[pos:]
    if "{{" in tail or "}}" in tail:
        raise ExpressionError(f"Unbalanced braces in {source!r}")
    if tail:
        parts.append(tail)
    return Template(source, tuple(parts))


def is_template(value: Any) -> bool:
    return isinstance(value, str) and "{{" in value


def _tokens(text: str, source: str) -> list[tuple[str, str]]:
    out, pos = [], 0
    text = text.rstrip()
    while pos < len(text):
        m = _TOKEN.match(text, pos)
        if not m or m.end() == pos:
            raise ExpressionError(f"Unexpected {text[pos:].strip()[:20]!r} in {source!r}")
        kind = m.lastgroup
        out.append((kind, m.group(kind)))
        pos = m.end()
    return out


def _parse_expr(text: str, source: str) -> Expr:
    toks = _tokens(text, source)
    i = 0

    def peek(value: str | None = None, kind: str | None = None) -> bool:
        if i >= len(toks):
            return False
        k, v = toks[i]
        return (value is None or v == value) and (kind is None or k == kind)

    def take(value: str | None = None, kind: str | None = None) -> str:
        nonlocal i
        if not peek(value, kind):
            found = toks[i][1] if i < len(toks) else "end of expression"
            raise ExpressionError(f"Expected {value or kind} but found {found!r} in {source!r}")
        i += 1
        return toks[i - 1][1]

    def operand() -> Operand:
        if peek(kind="str"):
            return Literal(take()[1:-1])
        if peek(kind="num"):
            raw = take()
            return Literal(float(raw) if "." in raw else int(raw))
        name = take(kind="name")
        if name in ("true", "false", "null"):
            return Literal({"true": True, "false": False, "null": None}[name])
        if name not in ROOTS:
            raise ExpressionError(f"Unknown name {name!r} in {source!r}; paths start with {', '.join(ROOTS)}")
        parts = [name]
        while peek("."):
            take(".")
            part = take(kind="name") if not peek(kind="num") else take(kind="num")
            if part.startswith("__"):
                raise ExpressionError(f"Unexpected name {part!r} in {source!r}")
            parts.append(part)
        return Path(tuple(parts))

    if not toks:
        raise ExpressionError(f"Empty expression in {source!r}")
    first = operand()
    filters = []
    while peek("|"):
        take("|")
        name = take(kind="name")
        if name not in FILTERS:
            raise ExpressionError(f"Unknown filter {name!r} in {source!r}; allowed: {', '.join(FILTERS)}")
        args: list[Operand] = []
        if peek("("):
            take("(")
            if not peek(")"):
                args.append(operand())
                while peek(","):
                    take(",")
                    args.append(operand())
            take(")")
        lo, hi = _ARITY[name]
        if not lo <= len(args) <= hi:
            raise ExpressionError(f"Filter {name!r} takes {lo}-{hi} argument(s) in {source!r}")
        filters.append((name, tuple(args)))
    compare = None
    if peek("==") or peek("!="):
        op = take()
        compare = (op, operand())
    if i != len(toks):
        raise ExpressionError(f"Unexpected {toks[i][1]!r} in {source!r}")
    return Expr(first, tuple(filters), compare)
