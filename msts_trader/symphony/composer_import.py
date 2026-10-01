"""Import a Composer.trade symphony (EDN as shown in their editor, or JSON).

Composer's format is not formally documented; this maps the node shapes seen
in exported symphonies:

  {:step :root :name "..." :rebalance :daily :children [...]}
  {:step :wt-cash-equal | :wt-cash-specified | :wt-inverse-vol :window-days "20" ...}
  {:step :asset :ticker "SPY"}   {:step :group :name "..."}   {:step :empty}
  {:step :filter :sort-by-fn :cumulative-return :sort-by-window-days "10"
                 :select-fn :top :select-n "2" :children [...]}
  {:step :if :children [{:step :if-child :is-else-condition? false
                         :lhs-fn :relative-strength-index :lhs-window-days "10" :lhs-val "SPY"
                         :comparator :gt :rhs-fixed-value? true :rhs-val "80" :children [...]}
                        {:step :if-child :is-else-condition? true :children [...]}]}

Newer exports put the test in a structured `condition` instead (binary,
multi-ticker "binary-compound" with a "%" placeholder, and any/all
"compound"); it takes precedence over any legacy lhs-/rhs- fields.

Anything unrecognised is reported with its path — never silently dropped,
since a dropped branch changes what the strategy buys.
"""

from __future__ import annotations

import json
import re
from typing import Any

from .model import INDICATORS, Symphony, slugify


class ComposerImportError(ValueError):
    pass


# ── minimal EDN reader ───────────────────────────────────────────────────
_TOKEN = re.compile(
    r"""\s*(?:
        (?P<open>[\[{(]|\#\{) |
        (?P<close>[\]})]) |
        (?P<str>"(?:\\.|[^"\\])*") |
        (?P<atom>[^\s,\[\]{}()"]+)
    )""",
    re.VERBOSE,
)


def parse_edn(text: str) -> Any:
    """Parse the EDN subset Composer emits: maps, vectors, lists, sets,
    keywords (-> "name" strings), strings, numbers, booleans, nil."""
    text = re.sub(r";[^\n]*", "", text)  # comments
    tokens = []
    pos = 0
    text = text.replace(",", " ")
    while pos < len(text):
        if text[pos:].strip() == "":
            break
        m = _TOKEN.match(text, pos)
        if not m or m.end() == pos:
            raise ComposerImportError(f"EDN syntax error near: {text[pos : pos + 30]!r}")
        pos = m.end()
        kind = m.lastgroup
        tokens.append((kind, m.group(kind)))

    i = 0

    def read():
        nonlocal i
        if i >= len(tokens):
            raise ComposerImportError("unexpected end of EDN")
        kind, val = tokens[i]
        i += 1
        if kind == "open":
            closer = {"[": "]", "(": ")", "{": "}", "#{": "}"}[val]
            items = []
            while True:
                if i >= len(tokens):
                    raise ComposerImportError(f"unclosed {val!r}")
                if tokens[i] == ("close", closer):
                    i += 1
                    break
                items.append(read())
            if val == "{":
                if len(items) % 2:
                    raise ComposerImportError("map with odd number of forms")
                return {str(items[k]): items[k + 1] for k in range(0, len(items), 2)}
            return items
        if kind == "close":
            raise ComposerImportError(f"unexpected {val!r}")
        if kind == "str":
            return json.loads(val)
        # atom
        if val.startswith(":"):
            return val[1:]
        if val == "nil":
            return None
        if val in ("true", "false"):
            return val == "true"
        if val.startswith("#"):
            raise ComposerImportError(f"unsupported EDN tag {val!r}")
        try:
            return int(val)
        except ValueError:
            pass
        try:
            return float(val.rstrip("M"))
        except ValueError:
            return val  # bare symbol

    value = read()
    if i != len(tokens):
        raise ComposerImportError("trailing content after the symphony")
    return value


# ── Composer node -> our node ────────────────────────────────────────────
def _num(v, what: str, path: str, errors: list[str]) -> float | None:
    if isinstance(v, dict) and "num" in v and "den" in v:
        try:
            return float(v["num"]) / float(v["den"])
        except (TypeError, ValueError, ZeroDivisionError):
            pass
    try:
        return float(v)
    except (TypeError, ValueError):
        errors.append(f"{path}: {what} {v!r} is not a number")
        return None


def _window(node: dict, prefix: str, path: str, errors: list[str], default: int = 1) -> int:
    for key in (f"{prefix}window-days", f"{prefix}window"):
        if key in node and node[key] not in (None, ""):
            v = _num(node[key], key, path, errors)
            return int(v) if v is not None else default
    params = node.get(f"{prefix}fn-params")
    if isinstance(params, dict) and params.get("window") not in (None, ""):
        v = _num(params["window"], "window", path, errors)
        return int(v) if v is not None else default
    return default


def _fn(name, path: str, errors: list[str]) -> str | None:
    name = str(name or "")
    if name not in INDICATORS:
        errors.append(f"{path}: unsupported indicator {name!r}")
        return None
    return name


def _metric(node: dict, side: str, path: str, errors: list[str]) -> dict | None:
    fn = _fn(node.get(f"{side}-fn"), path, errors)
    ticker = node.get(f"{side}-val")
    if fn is None:
        return None
    if not isinstance(ticker, str) or not ticker:
        errors.append(f"{path}: {side} indicator has no ticker")
        return None
    return {"fn": fn, "ticker": ticker, "window": _window(node, f"{side}-", path, errors)}


_CMP = {"gt": "gt", "gte": "gte", "lt": "lt", "lte": "lte", ">": "gt", ">=": "gte", "<": "lt", "<=": "lte"}


def _children(node: dict, path: str, errors: list[str]) -> list[dict]:
    out = []
    for k, c in enumerate(node.get("children") or []):
        conv = _convert(c, f"{path}/{k}", errors)
        if conv is not None:
            out.append(conv)
    return out


def _cmetric(spec, ticker_override: str | None, path: str, errors: list[str]) -> dict | None:
    """Structured-condition operand: {"fn", "ticker", "params": {"window"}}."""
    if not isinstance(spec, dict):
        errors.append(f"{path}: condition operand is not a map")
        return None
    fn = _fn(spec.get("fn"), path, errors)
    ticker = spec.get("ticker")
    if ticker == "%":
        ticker = ticker_override
    if fn is None:
        return None
    if not isinstance(ticker, str) or not ticker:
        errors.append(f"{path}: condition operand has no ticker")
        return None
    params = spec.get("params") or {}
    win = params.get("window") if isinstance(params, dict) else None
    w = _num(win, "window", path, errors) if win not in (None, "") else 1
    return {"fn": fn, "ticker": ticker, "window": int(w or 1)}


def _binary(c: dict, ticker: str | None, path: str, errors: list[str]) -> dict | None:
    lhs = _cmetric(c.get("lhs"), ticker, path, errors)
    cmp_ = _CMP.get(str(c.get("comparator") or ""))
    if cmp_ is None:
        errors.append(f"{path}: unsupported comparator {c.get('comparator')!r}")
    rhs = c.get("rhs")
    out: dict = {"lhs": lhs, "comparator": cmp_}
    if isinstance(rhs, dict) and "constant" in rhs:
        out["rhs_value"] = _num(rhs.get("constant"), "rhs constant", path, errors)
    elif isinstance(rhs, dict) and "fn" in rhs:
        out["rhs"] = _cmetric(rhs, ticker, path, errors)
    else:
        errors.append(f"{path}: unsupported right-hand side {rhs!r}")
        return None
    if lhs is None or cmp_ is None or (out.get("rhs") is None and out.get("rhs_value") is None):
        return None
    return out


def _structured(c, path: str, errors: list[str]) -> dict | None:
    """Composer's structured condition (newer editor):
    binary          lhs <cmp> rhs
    binary-compound the same test over `tickers` ("%" = each ticker), any/all
    compound        any/all of nested conditions
    """
    if not isinstance(c, dict):
        errors.append(f"{path}: condition is not a map")
        return None
    ctype = str(c.get("condition-type") or "binary")
    op = str(c.get("operator") or "any")
    if op not in ("any", "all"):
        errors.append(f"{path}: unsupported condition operator {op!r}")
        return None
    if ctype == "compound":
        parts = [_structured(x, f"{path}/{i}", errors) for i, x in enumerate(c.get("conditions") or [])]
    elif ctype == "binary-compound":
        tickers = [t for t in (c.get("tickers") or []) if isinstance(t, str) and t]
        if not tickers:
            errors.append(f"{path}: multi-ticker condition lists no tickers")
            return None
        parts = [_binary(c, t, path, errors) for t in tickers]
    elif ctype == "binary":
        return _binary(c, None, path, errors)
    else:
        errors.append(f"{path}: unsupported condition type {ctype!r}")
        return None
    parts = [p for p in parts if p is not None]
    if not parts:
        return None
    return parts[0] if len(parts) == 1 else {op: parts}


def _condition(ch: dict, path: str, errors: list[str]) -> dict | None:
    # Newer Composer exports carry a structured `condition`; nodes edited in
    # the new editor keep stale legacy lhs-/rhs- fields alongside it, so the
    # structured one wins whenever it is present.
    if isinstance(ch.get("condition"), dict):
        return _structured(ch["condition"], path, errors)
    lhs = _metric(ch, "lhs", path, errors)
    cmp_ = _CMP.get(str(ch.get("comparator") or ""))
    if cmp_ is None:
        errors.append(f"{path}: unsupported comparator {ch.get('comparator')!r}")
    cond: dict = {"lhs": lhs, "comparator": cmp_}
    if ch.get("rhs-fixed-value?") or ch.get("rhs-fn") in (None, ""):
        cond["rhs_value"] = _num(ch.get("rhs-val"), "rhs value", path, errors)
    else:
        cond["rhs"] = _metric(ch, "rhs", path, errors)
    if lhs is None or cmp_ is None:
        return None
    return cond


def _convert(node, path: str, errors: list[str]) -> dict | None:
    if not isinstance(node, dict):
        errors.append(f"{path}: expected a node map, got {type(node).__name__}")
        return None
    step = str(node.get("step") or "")
    out: dict | None = None
    if step == "asset":
        out = {"step": "asset", "ticker": str(node.get("ticker") or "")}
        if node.get("name"):
            out["name"] = str(node["name"])
    elif step == "empty":
        return None  # cash
    elif step == "group":
        out = {"step": "group", "name": str(node.get("name") or "Group"), "children": _children(node, path, errors)}
    elif step == "wt-cash-equal":
        out = {"step": "wt-cash-equal", "children": _children(node, path, errors)}
    elif step == "wt-cash-specified":
        out = {"step": "wt-cash-specified", "children": _children(node, path, errors)}
    elif step == "wt-inverse-vol":
        out = {
            "step": "wt-inverse-vol",
            "window": max(_window(node, "", path, errors, default=20), 2),
            "children": _children(node, path, errors),
        }
    elif step == "filter":
        fn = _fn(node.get("sort-by-fn"), path, errors)
        n = _num(node.get("select-n", 1), "select-n", path, errors)
        sel = str(node.get("select-fn") or "top")
        if sel not in ("top", "bottom"):
            errors.append(f"{path}: unsupported select-fn {sel!r}")
        out = {
            "step": "filter",
            "sort_fn": fn or "cumulative-return",
            "window": _window(node, "sort-by-", path, errors, default=10),
            "select": sel if sel in ("top", "bottom") else "top",
            "n": int(n or 1),
            "children": _children(node, path, errors),
        }
    elif step == "if":
        conds, else_children = [], []
        for k, ch in enumerate(node.get("children") or []):
            p = f"{path}/{k}"
            if not isinstance(ch, dict) or ch.get("step") != "if-child":
                errors.append(f"{p}: expected an if-child")
                continue
            kids = _children(ch, p, errors)
            if ch.get("is-else-condition?"):
                else_children = kids
            else:
                conds.append((_condition(ch, p, errors), kids))
        if not conds:
            errors.append(f"{path}: if block has no condition")
            return None
        # if / else-if chain -> nested ifs, innermost holds the else.
        tail = else_children
        for cond, kids in reversed(conds):
            tail = [{"step": "if", "condition": cond, "then": kids, "else": tail}]
        out = tail[0]
    else:
        errors.append(f"{path}: unsupported block {step!r}")
        return None
    if "weight" in node and node["weight"] is not None:
        w = _num(node["weight"], "weight", path, errors)
        if w is not None:
            out["weight"] = w
    return out


_KW = re.compile(r":[a-z][a-z0-9?-]*")


def _unkeyword(x):
    """JSON exports sometimes keep EDN keywords as ':name' strings (keys too)."""
    if isinstance(x, dict):
        return {(k[1:] if isinstance(k, str) and _KW.fullmatch(k) else k): _unkeyword(v) for k, v in x.items()}
    if isinstance(x, list):
        return [_unkeyword(v) for v in x]
    if isinstance(x, str) and _KW.fullmatch(x):
        return x[1:]
    return x


def convert(data: Any, *, id: str | None = None) -> tuple[Symphony, list[str]]:
    """Composer structure (already parsed) -> (Symphony, warnings)."""
    data = _unkeyword(data)
    if not isinstance(data, dict):
        raise ComposerImportError("expected a symphony map at the top level")
    # Some exports wrap the tree: {"symphony": {...}} / {"latest_version": {"score": ...}}
    for key in ("symphony", "score"):
        if isinstance(data.get(key), dict) and "step" not in data:
            data = data[key]
    errors: list[str] = []
    warnings: list[str] = []
    if data.get("step") not in (None, "root"):
        # A bare block: wrap it.
        data = {"step": "root", "name": "Imported", "children": [data]}
    name = str(data.get("name") or "Imported symphony")
    reb = str(data.get("rebalance") or "daily")
    if reb not in ("daily", "weekly", "monthly", "quarterly", "yearly"):
        warnings.append(
            f"rebalance {reb!r} is not supported (threshold/corridor rebalancing) — using daily; "
            f"the deploy drift threshold still limits turnover"
        )
        reb = "daily"
    children = _children(data, "root", errors)
    if errors:
        raise ComposerImportError("could not import:\n  " + "\n  ".join(errors))
    try:
        sym = Symphony.model_validate(
            {
                "id": id or slugify(name),
                "name": name,
                "description": str(data.get("description") or ""),
                "rebalance": reb,
                "children": children,
            }
        )
    except Exception as e:
        raise ComposerImportError(f"imported tree failed validation: {e}") from e
    return sym, warnings


def import_text(text: str, *, id: str | None = None) -> tuple[Symphony, list[str]]:
    """Accept Composer EDN or JSON (Composer-shaped or our own format)."""
    text = text.strip().lstrip("﻿")
    if not text:
        raise ComposerImportError("empty input")
    if text[0] in "{[":
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            data = parse_edn(text)
    else:
        data = parse_edn(text)
    # Our own native format round-trips too.
    if isinstance(data, dict) and "step" not in data and "children" in data and "id" in data:
        sym = Symphony.model_validate(data if id is None else {**data, "id": id})
        return sym, []
    return convert(data, id=id)
