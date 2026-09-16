"""Reject quality debt patterns that previously hid malformed Python source."""

from __future__ import annotations

import ast
import re
import sys
import tokenize
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCANNED_ROOTS = (ROOT / "src", ROOT / "tests", ROOT / "scripts")
BROAD_NOQA = re.compile(r"#\s*(?:ruff:\s*)?noqa\s*$", re.IGNORECASE)


def python_files() -> list[Path]:
    return sorted(path for root in SCANNED_ROOTS for path in root.rglob("*.py"))


def local_import_graph(files: list[Path]) -> dict[str, set[str]]:
    package_root = ROOT / "src" / "narang_rider"
    modules = {
        "narang_rider"
        if path == package_root / "__init__.py"
        else "narang_rider." + ".".join(path.relative_to(package_root).with_suffix("").parts)
        for path in files
        if path.is_relative_to(package_root)
    }
    graph = {module: set() for module in modules}
    for path in files:
        if not path.is_relative_to(package_root):
            continue
        module = (
            "narang_rider"
            if path == package_root / "__init__.py"
            else "narang_rider." + ".".join(path.relative_to(package_root).with_suffix("").parts)
        )
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                graph[module].update(alias.name for alias in node.names if alias.name in modules)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported = node.module if node.level == 0 else f"narang_rider.{node.module}"
                if imported in modules:
                    graph[module].add(imported)
    return graph


def cycles(graph: dict[str, set[str]]) -> list[str]:
    found: set[str] = set()
    visiting: list[str] = []
    visited: set[str] = set()

    def visit(module: str) -> None:
        if module in visiting:
            cycle = visiting[visiting.index(module) :] + [module]
            found.add(" -> ".join(cycle))
            return
        if module in visited:
            return
        visiting.append(module)
        for dependency in sorted(graph[module]):
            visit(dependency)
        visiting.pop()
        visited.add(module)

    for module in sorted(graph):
        visit(module)
    return sorted(found)


def main() -> int:
    files = python_files()
    failures: list[str] = []
    for path in files:
        relative = path.relative_to(ROOT)
        source = path.read_text(encoding="utf-8")
        try:
            compile(source, str(relative), "exec", dont_inherit=True)
        except SyntaxError as error:
            failures.append(f"{relative}:{error.lineno}: syntax error: {error.msg}")
            continue
        for number, line in enumerate(source.splitlines(), start=1):
            if BROAD_NOQA.search(line):
                failures.append(f"{relative}:{number}: broad noqa is forbidden")
        with path.open("rb") as stream:
            for token in tokenize.tokenize(stream.readline):
                if token.type == tokenize.OP and token.string == ";":
                    failures.append(
                        f"{relative}:{token.start[0]}: compressed semicolon statement is forbidden"
                    )

    failures.extend(f"import cycle: {cycle}" for cycle in cycles(local_import_graph(files)))
    if failures:
        print("\n".join(failures), file=sys.stderr)
        return 1
    print(f"Python quality gate passed for {len(files)} files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
