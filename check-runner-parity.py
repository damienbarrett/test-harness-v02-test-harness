#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["pyyaml"]
# ///
"""Check parity between Taskfile.yml and justfile pairs.

For each directory containing both files, verifies (and fails on):
  1. The set of logical recipe names matches.
  2. The dependency set per recipe matches.

Command-body content differences are reported as warnings only. The two DSLs
have legitimately different shell-script forms, so byte-equivalence is not a
useful check. Use the warnings to spot-check intent.

Naming normalisation: Taskfile allows ":" in task names; justfile does not.
This script treats "container:build" (Taskfile) and "container-build"
(justfile) as the same logical recipe.

Aggregator normalisation: Taskfile entries of the form "cmds: [{task: X}]"
are treated as dependencies for comparison, since that is what they are
semantically and what justfile expresses with "recipe: X".

Exit code 0 = all parity (warnings allowed). Non-zero = missing recipes or
divergent deps.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
SKIP_PARTS = {
    ".task",
    "node_modules",
    ".venv",
    "target",
    ".git",
    "transpiled",
    "bindings",
}
SKIP_TARGETS = {"default"}


def canonical_name(name: str) -> str:
    """Map both "container:build" and "container-build" to the same key."""
    return name.replace(":", "-")


def normalize_command(text: str) -> str:
    text = text.replace("\\\n", " ")
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(
        r"\{\{\s*\.?([A-Za-z_][A-Za-z0-9_]*)\s*\}\}",
        lambda m: "{{" + m.group(1).upper() + "}}",
        text,
    )
    text = re.sub(r"\s+#[^\"']*$", "", text).strip()
    return text


def _parse_taskfile_direct(path: Path) -> dict[str, dict]:
    """Parse only the directly-defined tasks in a Taskfile."""
    with open(path) as fh:
        data = yaml.safe_load(fh) or {}
    out: dict[str, dict] = {}
    for raw_name, body in (data.get("tasks") or {}).items():
        if raw_name in SKIP_TARGETS:
            continue
        name = canonical_name(raw_name)
        deps: list[str] = []
        cmds: list[str] = []
        if isinstance(body, dict):
            for dep in body.get("deps") or []:
                deps.append(canonical_name(dep if isinstance(dep, str) else dep.get("task", "")))
            for cmd in body.get("cmds") or []:
                if isinstance(cmd, dict) and "task" in cmd:
                    deps.append(canonical_name(cmd["task"]))
                elif isinstance(cmd, str):
                    cmds.append(normalize_command(cmd))
        out[name] = {"deps": sorted(deps), "cmds": cmds}
    return out


def parse_taskfile(path: Path) -> dict[str, dict]:
    """Parse a Taskfile and enumerate one level of include children.

    Convention: each Taskfile/justfile pair exposes its own tasks plus one
    level of nested children. Taskfile gets nested names for free via
    "includes:"; justfile needs an explicit wrapper recipe per child task.
    We do not recurse into nested includes.
    """
    out = _parse_taskfile_direct(path)
    with open(path) as fh:
        data = yaml.safe_load(fh) or {}
    for prefix, inc in (data.get("includes") or {}).items():
        inc_path = inc.get("taskfile") if isinstance(inc, dict) else inc
        if not inc_path:
            continue
        child_path = (path.parent / inc_path).resolve()
        if not child_path.exists():
            continue
        for child_name in _parse_taskfile_direct(child_path):
            out[canonical_name(f"{prefix}:{child_name}")] = {"deps": [], "cmds": []}
    return out


def parse_justfile(path: Path) -> dict[str, dict]:
    out: dict[str, dict] = {}
    current: tuple[str, list[str]] | None = None
    body: list[str] = []

    def flush() -> None:
        nonlocal current, body
        if current is None:
            return
        name, deps = current
        joined = "\n".join(line for line in body if line.strip())
        cmds = [normalize_command(line) for line in joined.split("\n") if line.strip()]
        out[name] = {"deps": sorted(deps), "cmds": cmds}
        current = None
        body = []

    with open(path) as fh:
        for raw in fh:
            line = raw.rstrip("\n")
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            if line[0] in (" ", "\t"):
                if current is not None:
                    body.append(line.strip())
                continue
            if ":=" in line or line.startswith(("export ", "alias ", "set ", "import ")):
                continue
            if ":" not in line:
                continue
            flush()
            head, _, after = line.partition(":")
            head_parts = head.split()
            if not head_parts:
                continue
            name = head_parts[0]
            if not re.match(r"^[a-zA-Z_][a-zA-Z0-9_-]*$", name):
                continue
            if name in SKIP_TARGETS:
                current = None
                continue
            deps = [canonical_name(d) for d in after.strip().split()]
            current = (canonical_name(name), deps)
    flush()
    return out


def compare(tf: dict, jf: dict) -> tuple[list[str], list[str]]:
    """Return (failures, warnings) for one directory."""
    failures: list[str] = []
    warnings: list[str] = []
    only_task = sorted(set(tf) - set(jf))
    only_just = sorted(set(jf) - set(tf))
    if only_task:
        failures.append(f"  only in Taskfile.yml: {only_task}")
    if only_just:
        failures.append(f"  only in justfile:     {only_just}")
    for name in sorted(set(tf) & set(jf)):
        t, j = tf[name], jf[name]
        if t["deps"] != j["deps"]:
            failures.append(f"  {name}: deps differ - Taskfile {t['deps']} vs justfile {j['deps']}")
        if t["cmds"] != j["cmds"]:
            warnings.append(
                f"  {name}: command bodies differ "
                f"({len(t['cmds'])} vs {len(j['cmds'])} normalised commands)"
            )
    return failures, warnings


def main() -> int:
    failures = 0
    warnings = 0
    pairs_checked = 0
    for taskfile in sorted(ROOT.rglob("Taskfile.yml")):
        if any(part in SKIP_PARTS for part in taskfile.parts):
            continue
        justfile = taskfile.parent / "justfile"
        if not justfile.exists():
            continue
        pairs_checked += 1
        rel = taskfile.parent.relative_to(ROOT) or Path(".")
        try:
            tf = parse_taskfile(taskfile)
            jf = parse_justfile(justfile)
        except Exception as exc:  # noqa: BLE001
            print(f"FAIL {rel}/: parse error: {exc}", file=sys.stderr)
            failures += 1
            continue
        f_lines, w_lines = compare(tf, jf)
        if f_lines:
            print(f"FAIL {rel}/")
            for line in f_lines:
                print(line)
            failures += 1
        if w_lines:
            for line in w_lines:
                print(f"warn {rel}/{line.lstrip()}")
            warnings += len(w_lines)

    print()
    if failures == 0:
        msg = f"OK: {pairs_checked} Taskfile.yml/justfile pair(s) in parity"
        if warnings:
            msg += f" ({warnings} command-body warning(s))"
        print(msg + ".")
        return 0
    print(f"{failures} location(s) with drift.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
