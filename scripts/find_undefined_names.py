"""Find names a module uses as globals but never binds (the `NameError` class of
production bug: `reply`, `MSG_DB_ERROR`...). Uses stdlib symtable so locals,
arguments and comprehension targets are handled for us."""

import ast
import builtins
import glob
import symtable
import sys


def module_bindings(tree):
    names = set()

    def walk(statements):
        for node in statements:
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                for alias in node.names:
                    names.add((alias.asname or alias.name).split(".")[0])
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(node.name)
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    for sub in ast.walk(target):
                        if isinstance(sub, ast.Name):
                            names.add(sub.id)
            elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
                for sub in ast.walk(node.target):
                    if isinstance(sub, ast.Name):
                        names.add(sub.id)
            elif isinstance(node, ast.Try):
                for inner in node.handlers:
                    if inner.name:
                        names.add(inner.name)
                walk(node.body)
                walk(node.orelse)
                walk(node.finalbody)
                for inner in node.handlers:
                    walk(inner.body)
            elif isinstance(node, (ast.If, ast.For, ast.AsyncFor, ast.While, ast.With,
                                   ast.AsyncWith)):
                walk(node.body)
                walk(getattr(node, "orelse", []))

    walk(tree.body)
    return names


def free_names(table):
    """Globals referenced anywhere below this symbol table."""
    found = set()
    if table.get_type() == "function":
        for symbol in table.get_symbols():
            if symbol.is_global() and not symbol.is_assigned():
                found.add(symbol.get_name())
    for child in table.get_children():
        found |= free_names(child)
    return found


def report(path):
    source = open(path, encoding="utf-8").read()
    tree = ast.parse(source)
    defined = module_bindings(tree) | set(dir(builtins)) | {"__name__", "__file__"}
    table = symtable.symtable(source, path, "exec")
    missing = sorted(free_names(table) - defined)
    return [name for name in missing if not name.startswith("__")]


if __name__ == "__main__":
    targets = sys.argv[1:] or glob.glob("Thunder/**/*.py", recursive=True)
    bad = 0
    for path in sorted(targets):
        for name in report(path):
            bad += 1
            print(f"{path}: undefined global name {name!r}")
    print(f"--- {bad} undefined name(s) found")
    sys.exit(1 if bad else 0)
