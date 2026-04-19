#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["wasmtime==43.0.0"]
# ///
"""Unified WASM contract test harness.

Discovers test fixtures in ``common/functions/`` and WASM implementations
across language directories, then runs every test case against every
implementation.

Convention-based discovery (no metadata required in test files):

  Directory structure::

      common/functions/{interface}/{function}.test.json
      {lang}/component/{world}.wasm

  The WIT world name is derived from the WIT file in ``common/wit/``.
  The interface name is the directory name under ``common/functions/``.
  The function name is the file stem (before ``.test.json``).

  Implementations are discovered by scanning for ``*/component/``
  directories.  Any directory matching that pattern is expected to
  contain a ``{world}.wasm`` file for every world defined in the WIT.

Exit code 0 = all pass.  Non-zero = at least one failure.
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import make_dataclass
from pathlib import Path

from wasmtime import Engine, Store, WasiConfig
from wasmtime import component as wt_component

ROOT = Path(__file__).resolve().parent.parent

# Directories to skip when scanning for implementation dirs.
SKIP_DIRS = {
    "common",
    "container",
    "test-harness",
    ".git",
    ".task",
    "node_modules",
}

# WIT package namespace and package name.
WIT_NAMESPACE = "common"
WIT_PACKAGE = "tasks"


# ---------------------------------------------------------------------------
# WIT parsing (minimal — extracts world names only)
# ---------------------------------------------------------------------------

_WORLD_RE = re.compile(r"^\s*world\s+([\w-]+)\s*\{", re.MULTILINE)


def discover_worlds() -> list[str]:
    """Extract world names from WIT files under ``common/wit/``."""
    wit_dir = ROOT / "common" / "wit"
    worlds: list[str] = []
    if not wit_dir.is_dir():
        return worlds
    for wit_file in sorted(wit_dir.glob("*.wit")):
        text = wit_file.read_text()
        worlds.extend(_WORLD_RE.findall(text))
    return worlds


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def discover_test_suites() -> list[dict]:
    """Find ``*.test.json`` files; derive interface and function from path."""
    suites: list[dict] = []
    functions_dir = ROOT / "common" / "functions"
    if not functions_dir.is_dir():
        return suites
    for path in sorted(functions_dir.rglob("*.test.json")):
        with open(path) as fh:
            data = json.load(fh)
        # Convention: parent dir = interface, file stem = function
        interface = path.parent.name
        function = path.name.removesuffix(".test.json")
        data["_path"] = path
        data["_interface"] = interface
        data["_function"] = function
        suites.append(data)
    return suites


def discover_implementations() -> list[str]:
    """Find language directories that contain a ``component/`` sub-dir."""
    langs: list[str] = []
    for child in sorted(ROOT.iterdir()):
        if not child.is_dir():
            continue
        if child.name in SKIP_DIRS:
            continue
        if (child / "component").is_dir():
            langs.append(child.name)
    return langs


# ---------------------------------------------------------------------------
# WASM execution
# ---------------------------------------------------------------------------


def _make_record_class(data: dict) -> object:
    """Convert a dict to a dataclass instance for wasmtime record passing."""
    cls = make_dataclass("Record", list(data.keys()))
    return cls(**data)


def prepare_args(raw_input: dict) -> list:
    """Convert JSON input to positional args with record conversion.

    Each top-level value in the input dict becomes a positional argument
    (matching the Component Model calling convention).  Dicts nested inside
    lists are converted to dataclass instances so wasmtime can marshal
    them as WIT records.
    """
    args = []
    for val in raw_input.values():
        if isinstance(val, list):
            args.append([_make_record_class(item) if isinstance(item, dict) else item for item in val])
        elif isinstance(val, dict):
            args.append(_make_record_class(val))
        else:
            args.append(val)
    return args


def instantiate_component(engine: Engine, wasm_path: Path):
    """Instantiate a WASM component with WASI support.

    Returns ``(store, instance)`` on success or raises on failure.
    """
    store = Store(engine)
    store.set_wasi(WasiConfig())

    comp = wt_component.Component.from_file(engine, str(wasm_path))
    linker = wt_component.Linker(engine)
    linker.add_wasip2()

    try:
        instance = linker.instantiate(store, comp)
        return store, instance
    except Exception:
        # Some components (e.g. componentize-js) import wasi:http which
        # wasmtime-py does not provide.  Fall back to trapping unknown
        # imports — this works only if the tested function doesn't
        # actually call into those imports at runtime.
        store = Store(engine)
        store.set_wasi(WasiConfig())
        linker = wt_component.Linker(engine)
        linker.define_unknown_imports_as_traps(comp)
        linker.allow_shadowing = True
        linker.add_wasip2()
        instance = linker.instantiate(store, comp)
        return store, instance


def call_function(store, instance, interface_export: str, function_name: str, args: list):
    """Resolve and call a single exported function on an instance."""
    iface_idx = instance.get_export_index(store, interface_export)
    if iface_idx is None:
        raise RuntimeError(f"export '{interface_export}' not found")

    func_idx = instance.get_export_index(store, function_name, iface_idx)
    if func_idx is None:
        raise RuntimeError(f"function '{function_name}' not found in '{interface_export}'")

    func = instance.get_func(store, func_idx)
    if func is None:
        raise RuntimeError(f"could not load function '{function_name}'")

    result = func(store, *args)
    func.post_return(store)
    return result


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    worlds = discover_worlds()
    if not worlds:
        print("FAIL: no worlds found in common/wit/", file=sys.stderr)
        return 1

    suites = discover_test_suites()
    if not suites:
        print("FAIL: no test suites found under common/functions/", file=sys.stderr)
        return 1

    langs = discover_implementations()
    if not langs:
        print("FAIL: no implementation directories found", file=sys.stderr)
        return 1

    total = 0
    passed = 0
    failed = 0

    print(f"Discovered {len(worlds)} world(s): {', '.join(worlds)}")
    print(f"Discovered {len(suites)} test suite(s)")
    print(f"Discovered {len(langs)} implementation(s): {', '.join(langs)}")
    print()

    engine = Engine()

    for world in worlds:
        for suite in suites:
            interface = suite["_interface"]
            function = suite["_function"]
            tests = suite["tests"]
            suite_rel = suite["_path"].relative_to(ROOT)
            interface_export = f"{WIT_NAMESPACE}:{WIT_PACKAGE}/{interface}"

            print(f"--- {suite_rel}")
            print(f"    world={world}  interface={interface}  function={function}")
            print(f"    {len(tests)} test case(s) x {len(langs)} implementation(s)")
            print()

            for lang in langs:
                wasm_path = ROOT / lang / "component" / f"{world}.wasm"

                if not wasm_path.exists():
                    print(f"  FAIL [{lang}] {wasm_path.relative_to(ROOT)}: WASM file not found")
                    failed += len(tests)
                    total += len(tests)
                    continue

                # Try to instantiate
                try:
                    store, instance = instantiate_component(engine, wasm_path)
                except Exception as exc:
                    first_line = str(exc).splitlines()[0]
                    print(f"  FAIL [{lang}] could not instantiate: {first_line}")
                    failed += len(tests)
                    total += len(tests)
                    continue

                lang_passed = 0
                lang_failed = 0

                for case in tests:
                    total += 1
                    description = case["description"]
                    expected = case["expected"]

                    try:
                        args = prepare_args(case["input"])
                        actual = call_function(
                            store, instance, interface_export, function, args,
                        )
                        if actual == expected:
                            lang_passed += 1
                            passed += 1
                        else:
                            lang_failed += 1
                            failed += 1
                            print(
                                f"  FAIL [{lang}] {description}: "
                                f"expected {expected}, got {actual}"
                            )
                    except Exception as exc:
                        lang_failed += 1
                        failed += 1
                        first_line = str(exc).splitlines()[0]
                        print(f"  FAIL [{lang}] {description}: {first_line}")

                if lang_failed == 0:
                    print(f"  OK   [{lang}] {lang_passed}/{lang_passed} passed")
                else:
                    print(
                        f"  FAIL [{lang}] {lang_failed}/{lang_passed + lang_failed} failed"
                    )

            print()

    # Summary
    print("=" * 60)
    if failed == 0:
        print(f"OK: {passed}/{total} tests passed across {len(langs)} implementation(s).")
        return 0

    print(f"{failed}/{total} test(s) failed.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
