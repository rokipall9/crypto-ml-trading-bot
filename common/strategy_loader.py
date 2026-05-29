"""
strategy_loader.py — Declarative strategy definitions via YAML.

Read /home/ubuntu/common/strategies/*.yaml. Each file defines a
strategy's static config: scoring weights, thresholds, RR gates,
stop/TP rules, etc. The bot's scoring engine consumes these dicts
instead of hardcoded constants.

Why: operators add/tune strategies without touching Python code. Also
makes A/B testing trivial — copy a YAML, tweak params, register as
shadow strategy.

Stdlib YAML parser is intentionally simple — only supports flat dicts +
lists + scalars. No anchors, no tags, no flow style. If you need
something fancier, install PyYAML.

Usage:
    from strategy_loader import load_all, load
    strategies = load_all()                 # → {"BREAKOUT_4H": {...}, ...}
    cfg = load("BREAKOUT_4H")               # → single dict
    cfg = load("/path/to/file.yaml")        # → from explicit path
"""
from __future__ import annotations

import json
import os
import re
import sys
from typing import Any, Dict, List, Optional

sys.path.insert(0, "/home/ubuntu/common")
from srs_logger import get_logger

log = get_logger("strategy_loader")

STRATEGIES_DIR = "/home/ubuntu/common/strategies"


# ─── Minimal YAML parser (stdlib only) ───
_SCALAR_RE = re.compile(r"^([+-]?\d+\.?\d*|true|false|null|none|~)$",
                        re.IGNORECASE)


def _parse_scalar(s: str) -> Any:
    s = s.strip()
    if not s:
        return None
    # Quoted strings
    if (s.startswith('"') and s.endswith('"')
            or s.startswith("'") and s.endswith("'")):
        return s[1:-1]
    low = s.lower()
    if low in ("true", "yes"):  return True
    if low in ("false", "no"):  return False
    if low in ("null", "none", "~", ""):  return None
    if _SCALAR_RE.match(s):
        try:
            if "." in s: return float(s)
            return int(s)
        except (ValueError, TypeError):
            pass
    return s   # bare string


def _parse_yaml(text: str) -> Dict[str, Any]:
    """Very simple YAML: 2-space indented dicts, '- ' list items, '#' comments."""
    lines = []
    for raw in text.splitlines():
        # strip comments (simple — doesn't handle '#' inside strings)
        if "#" in raw:
            in_str = False
            quote = None
            cut = -1
            for i, c in enumerate(raw):
                if c in ('"', "'"):
                    if not in_str:
                        in_str = True; quote = c
                    elif quote == c:
                        in_str = False
                elif c == "#" and not in_str:
                    cut = i; break
            if cut >= 0:
                raw = raw[:cut]
        if raw.rstrip():
            lines.append(raw.rstrip())

    # Build into a stack-based structure
    root: Dict[str, Any] = {}
    stack: List = [(0, root)]   # (indent, container)

    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.lstrip(" ")
        indent = len(line) - len(stripped)

        # Pop stack to current indent
        while stack and stack[-1][0] > indent:
            stack.pop()
        container = stack[-1][1]

        if stripped.startswith("- "):
            # List item
            val = stripped[2:].strip()
            if not isinstance(container, list):
                # Replace last entry of parent with [] if needed
                # This means parent dict has a key that should become a list
                # We expect previous line was 'key:'
                raise ValueError(f"unexpected list item at line {i+1}: {line}")
            if ":" in val and not (val.startswith('"') or val.startswith("'")):
                # inline dict on list item
                k, v = val.split(":", 1)
                d = {k.strip(): _parse_scalar(v)}
                container.append(d)
            else:
                container.append(_parse_scalar(val))
        elif ":" in stripped:
            key, _, rest = stripped.partition(":")
            key = key.strip()
            rest = rest.strip()
            if rest:
                container[key] = _parse_scalar(rest)
            else:
                # peek next line to decide list or dict
                nxt = lines[i + 1] if i + 1 < len(lines) else ""
                nxt_stripped = nxt.lstrip(" ")
                nxt_indent = len(nxt) - len(nxt_stripped)
                if nxt_indent > indent and nxt_stripped.startswith("- "):
                    container[key] = []
                    stack.append((indent + 1, container[key]))
                elif nxt_indent > indent:
                    container[key] = {}
                    stack.append((indent + 1, container[key]))
                else:
                    container[key] = None
        i += 1

    return root


# ─── Public API ───
def list_files() -> List[str]:
    if not os.path.isdir(STRATEGIES_DIR):
        return []
    return sorted([os.path.join(STRATEGIES_DIR, f)
                   for f in os.listdir(STRATEGIES_DIR)
                   if f.endswith((".yaml", ".yml"))])


def load(name_or_path: str) -> Optional[Dict[str, Any]]:
    """Load a single strategy by name (looks under STRATEGIES_DIR) or absolute path."""
    if os.path.isabs(name_or_path):
        path = name_or_path
    else:
        # try .yaml then .yml
        for ext in (".yaml", ".yml"):
            cand = os.path.join(STRATEGIES_DIR, name_or_path + ext)
            if os.path.exists(cand):
                path = cand
                break
        else:
            return None
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return _parse_yaml(f.read())
    except Exception as e:
        log.error("yaml_parse_failed", path=path, err=str(e))
        return None


def load_all() -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for path in list_files():
        cfg = load(path)
        if not cfg:
            continue
        name = cfg.get("name") or os.path.splitext(os.path.basename(path))[0]
        out[name] = cfg
    return out


def validate(cfg: Dict[str, Any]) -> List[str]:
    """Return list of validation errors (empty = valid)."""
    errors = []
    required = ["name", "score_threshold", "rr_gate_min"]
    for k in required:
        if k not in cfg:
            errors.append(f"missing_required_key: {k}")
    if "score_threshold" in cfg:
        v = cfg["score_threshold"]
        if not isinstance(v, (int, float)) or v < 0 or v > 10:
            errors.append(f"score_threshold must be 0-10, got {v}")
    if "rr_gate_min" in cfg:
        v = cfg["rr_gate_min"]
        if not isinstance(v, (int, float)) or v < 1:
            errors.append(f"rr_gate_min must be ≥ 1, got {v}")
    return errors


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "list":
        for p in list_files():
            print(f"  {p}")
    elif len(sys.argv) > 1 and sys.argv[1] == "show":
        if len(sys.argv) < 3:
            print(json.dumps(load_all(), indent=2, default=str))
        else:
            cfg = load(sys.argv[2])
            print(json.dumps(cfg, indent=2, default=str))
    elif len(sys.argv) > 1 and sys.argv[1] == "validate":
        if len(sys.argv) < 3:
            print("Usage: strategy_loader.py validate <name>"); sys.exit(1)
        cfg = load(sys.argv[2])
        if not cfg:
            print(f"not found: {sys.argv[2]}"); sys.exit(1)
        errs = validate(cfg)
        if not errs:
            print(f"✅ {sys.argv[2]} is valid")
        else:
            print(f"❌ {sys.argv[2]} has {len(errs)} errors:")
            for e in errs: print(f"  - {e}")
            sys.exit(1)
    else:
        print("Usage:")
        print("  strategy_loader.py list")
        print("  strategy_loader.py show [name]")
        print("  strategy_loader.py validate <name>")
