"""Dump every user-visible string on `/welcome` + `/settings` to a CSV (V3 P5a).

    .venv/bin/python tools/export_copy.py            # -> dev-notes/copy/onboarding_settings.csv

Static analysis only (nothing is imported or run): starting from the page
functions, it follows calls through `app.py` and the UI modules it uses, and
collects the string literals that can reach the screen. F-strings keep their
`{expressions}` as written. Fill in `new_text` for the rows to change; rows with
an empty `new_text` stay as they are. Rows are matched back on `text` (+ `file`),
not on `line`, so the CSV survives code edits made meanwhile.
"""

from __future__ import annotations

import ast
import csv
import re
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "src" / "sortandpost"
OUT = ROOT / "dev-notes" / "copy" / "onboarding_settings.csv"

# Modules whose code is followed (others are back-end; their strings never reach these pages
# except as error text).
MODULES = [
    "app",
    "onboarding",
    "achievements_ui",
    "posts_ui",
    "achievements",
    "themes",
    "prefs",
]
# (module, function name) the walk starts from, with the page each belongs to.
ROOTS = [
    ("app", "settings_page", "/settings"),
    ("app", "welcome_page", "/welcome"),
    ("app", "_setup_welcome", "/welcome"),
]

# Calls whose string arguments are never shown: styling, wiring, lookups, logging.
_SKIP_METHODS = {
    "classes",
    "props",
    "style",
    "on",
    "run_javascript",
    "add_head_html",
    "add_body_html",
    "add_css",
    "get",
    "getattr",
    "setattr",
    "hasattr",
    "startswith",
    "endswith",
    "split",
    "rsplit",
    "strip",
    "rstrip",
    "lstrip",
    "replace",
    "join",
    "sub",
    "match",
    "search",
    "fullmatch",
    "compile",
    "open",
    "glob",
    "rglob",
    "with_suffix",
    "with_name",
    "joinpath",
    "debug",
    "info",
    "warning",
    "error",
    "exception",
    "critical",
    "icon",
    "element",
    "encode",
    "decode",
    "format",
    "isoformat",
    "strftime",
    "strptime",
    "environ",
    "getenv",
    "pop",
    "setdefault",
    "index",
    "count",
    "find",
    "loads",
    "dumps",
    "Path",
    "page",
    "timer",
    "mark",
    "download",
    "navigate",
    "open_url",
    "storage",
    "add_static_files",
    "fromisoformat",
    "lower",
    "upper",
    "partition",
    "removeprefix",
    "removesuffix",
    "_settings_section",
    "dict",
    "keys",
    "cast",
    "isinstance",
    "_helper_action_js",
    "_helper_action_js_multi",
    "ValueError",
    "RuntimeError",
    "TypeError",
    "KeyError",
}
_SKIP_KWARGS = {
    "icon",
    "color",
    "text_color",
    "on_click",
    "on_change",
    "classes",
    "props",
    "style",
    "target",
    "mode",
    "align",
    "position",
    "type",
    "key",
    "sep",
    "timespec",
    "encoding",
    "suffix",
    "prefix",
    "language",
    "columns",
    "rows",
    "side",
    "anchor",
    "self",
    "sort",
    "group",
    "size",
    "format",
    "name",
    "id",
    "validation",
    "spacing",
    "direction",
    "how",
    "kind",
}
# Shared chrome (app bar, status bar) belongs to every page, not these two.
_SKIP_FUNCS = {"_header", "_status_bar", "_nav"}
# Positional arguments that are names / icons / URLs rather than text.
_SKIP_POSITIONS = {"step": {0}, "_settings_card": {1}, "link": {1}, "tab": {0}, "tab_panel": {0}}
# Calls that put a lone lowercase word on screen ("on", "none"); elsewhere such a word is a key.
_DISPLAY_CALLS = {
    "label",
    "notify",
    "button",
    "set_text",
    "tooltip",
    "checkbox",
    "switch",
    "markdown",
    "chip",
    "expansion",
    "link",
    "_settings_card",
    "_restart_app",
    "append",
}
_DISPLAY_KWARGS = {"label", "placeholder", "caption", "hint", "title", "text", "message", "button"}
_ICONISH = re.compile(r"^[a-z][a-z0-9]*(_[a-z0-9]+)+$")  # material icon / identifier
_CSSISH = re.compile(r"^[-!a-z0-9:/\[\]#.%()_]+( [-!a-z0-9:/\[\]#.%()_]+)+$")  # "text-xs sp-faint"
_CODEISH = re.compile(r"^[#/.$]|^https?://|^[a-z0-9_.\-]+\.(py|json|yaml|db|svg|png|md|ini)$")


@dataclass
class Row:
    page: str
    file: str
    line: int
    function: str
    context: str
    text: str


def _call_name(func: ast.expr) -> str:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        base = _call_name(func.value)
        return f"{base}.{func.attr}" if base else func.attr
    if isinstance(func, ast.Call):
        return _call_name(func.func)
    return ""


def _render(node: ast.expr) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        out = []
        for part in node.values:
            if isinstance(part, ast.Constant):
                out.append(str(part.value))
            elif isinstance(part, ast.FormattedValue):
                spec = ""
                if part.format_spec is not None:
                    spec = ":" + (_render(part.format_spec) or "")
                out.append("{" + ast.unparse(part.value) + spec + "}")
        return "".join(out)
    return None


def _looks_visible(text: str) -> bool:
    t = text.strip()
    if not re.search(r"[A-Za-z]", re.sub(r"\{[^}]*\}", "", t)):
        return False  # punctuation / numbers / only placeholders
    if re.match(r"^\{_[A-Z_]+\}/", t) or t.startswith(("file:", "SELECT ", "INSERT ", "UPDATE ")):
        return False  # URL built from a route constant, SQL
    if re.match(r"^(text|bg|border|sp|q)-[a-z0-9-]+$", t):
        return False  # one CSS class
    if _ICONISH.match(t) or _CODEISH.match(t):
        return False
    if _CSSISH.match(t) and not re.search(r"[A-Z]", t):
        # all-lowercase multi-token strings are CSS classes unless they read like words
        words = t.split()
        if any(re.search(r"[-:\[\]/!]", w) for w in words) or all(len(w) <= 3 for w in words):
            return False
    return not (t.startswith(("<", "sp-", "q-", "--")) or "=>" in t or "function(" in t)


class _Module:
    def __init__(self, name: str) -> None:
        self.name = name
        self.path = PKG / f"{name}.py"
        self.tree = ast.parse(self.path.read_text(encoding="utf-8"))
        # every function by name (top-level and nested); nested names shadow nothing we care about
        self.funcs: dict[str, list[ast.FunctionDef | ast.AsyncFunctionDef]] = {}
        for node in ast.walk(self.tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self.funcs.setdefault(node.name, []).append(node)
        self.consts: dict[str, ast.AST] = {}
        self.imports: dict[str, tuple[str, str]] = {}  # local name -> (module, attr or "")
        for node in self.tree.body:
            if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for t in targets:
                    if isinstance(t, ast.Name):
                        self.consts[t.id] = node.value
            elif isinstance(node, ast.ImportFrom) and node.module:
                for alias in node.names:
                    local = alias.asname or alias.name
                    if node.module == "sortandpost":
                        self.imports[local] = (alias.name, "")
                    elif node.module.startswith("sortandpost."):
                        self.imports[local] = (node.module.split(".", 1)[1], alias.name)


class Extractor:
    def __init__(self) -> None:
        self.mods = {m: _Module(m) for m in MODULES}
        self.rows: list[Row] = []
        self.seen_funcs: set[tuple[str, str, int]] = set()
        self.seen_consts: set[tuple[str, str, str]] = set()
        self.seen_rows: dict[tuple[str, int, int], Row] = {}
        self.scopes: dict[tuple[str, str, int], tuple[dict[str, ast.AST], ...]] = {}
        self.cur: tuple[str, str, int] = ("", "", 0)

    def run(self) -> list[Row]:
        for mod, fname, page in ROOTS:
            m = self.mods[mod]
            for top in m.tree.body:  # page functions are nested in build_ui / run_setup
                if not isinstance(top, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                siblings: dict[str, ast.AST] = {
                    n.name: n
                    for n in ast.walk(top)
                    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n is not top
                }
                if fname in siblings:
                    self._visit_func(m, siblings[fname], page, (siblings,))
        return self.rows

    # -- walking -------------------------------------------------------------------------

    def _visit_func(
        self, mod: _Module, fn: ast.AST, page: str, scope: tuple[dict[str, ast.AST], ...] = ()
    ) -> None:
        key = (page, mod.name, id(fn))
        name = getattr(fn, "name", "?")
        if key in self.seen_funcs or name in _SKIP_FUNCS:
            return
        self.seen_funcs.add(key)
        local: dict[str, ast.AST] = {}
        stack = list(getattr(fn, "body", []))
        while stack:  # defs nested directly in this function (through if/with/for blocks)
            n = stack.pop()
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                local[n.name] = n
            elif not isinstance(n, ast.ClassDef):
                stack.extend(c for c in ast.iter_child_nodes(n) if isinstance(c, ast.stmt))
        self.scopes[key] = (*scope, local)
        self.cur = key
        body = list(getattr(fn, "body", []))
        if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
            body = body[1:]  # docstring
        for stmt in body:
            self.cur = key
            self._walk(mod, stmt, page, name, "", skip=False)

    def _follow(self, mod: _Module, func: ast.expr, page: str) -> None:
        if isinstance(func, ast.Name):
            scope = self.scopes.get(self.cur, ())
            for local in reversed(scope):
                if func.id in local:
                    self._visit_func(mod, local[func.id], page, scope)
                    return
            top = {
                n.name: n
                for n in mod.tree.body
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            }
            if func.id in top:
                self._visit_func(mod, top[func.id], page)
            elif func.id in mod.imports:
                target, attr = mod.imports[func.id]
                if target in self.mods and attr:
                    for fn in self.mods[target].funcs.get(attr, []):
                        self._visit_func(self.mods[target], fn, page)
        elif isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
            tmod, tattr = mod.imports.get(func.value.id, ("", ""))
            if tmod in self.mods and not tattr:
                other = self.mods[tmod]
                for fn in other.funcs.get(func.attr, []):
                    self._visit_func(other, fn, page)

    def _const(self, mod: _Module, name: str, page: str) -> None:
        if name in mod.consts and (page, mod.name, name) not in self.seen_consts:
            self.seen_consts.add((page, mod.name, name))
            self._walk(mod, mod.consts[name], page, f"<{name}>", "constant", skip=False)
        elif name in mod.imports:
            target, attr = mod.imports[name]
            if target in self.mods and attr in self.mods[target].consts:
                self._const(self.mods[target], attr, page)

    def _emit(
        self, mod: _Module, node: ast.expr, page: str, func: str, ctx: str, *, shown: bool
    ) -> None:
        text = _render(node)
        if text is None or not _looks_visible(text):
            return
        if not shown and re.fullmatch(r"[a-z]+", text):
            return  # a lone lowercase word outside a display call is a key, not copy
        key = (mod.name, node.lineno, node.col_offset)
        if key in self.seen_rows:
            row = self.seen_rows[key]
            if page not in row.page.split(" "):
                row.page += f" {page}"
            return
        rel = str(mod.path.relative_to(ROOT))
        self.seen_rows[key] = Row(page, rel, node.lineno, func, ctx, text)
        self.rows.append(self.seen_rows[key])

    def _walk(
        self,
        mod: _Module,
        node: ast.AST,
        page: str,
        func: str,
        ctx: str,
        *,
        skip: bool,
        shown: bool = False,
    ) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            here = self.cur
            self._visit_func(mod, node, page, self.scopes.get(here, ()))  # nested handler
            self.cur = here
            return
        if isinstance(node, (ast.Constant, ast.JoinedStr)):
            if not skip:
                self._emit(mod, node, page, func, ctx, shown=shown)
            return
        if isinstance(node, ast.IfExp):
            self._walk(mod, node.test, page, func, ctx, skip=True)
            for branch in (node.body, node.orelse):
                self._walk(mod, branch, page, func, ctx, skip=skip, shown=True)
            return
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            self._const(mod, node.id, page)
            return
        if isinstance(node, ast.Call):
            name = _call_name(node.func)
            last = name.rsplit(".", 1)[-1]
            here = self.cur
            self._follow(mod, node.func, page)
            self.cur = here
            if isinstance(node.func, ast.Attribute):  # "sep".join(...) / ui.label(...).classes(...)
                base_const = isinstance(node.func.value, (ast.Constant, ast.JoinedStr))
                self._walk(mod, node.func.value, page, func, ctx, skip=skip or base_const)
            skip_args = skip or last in _SKIP_METHODS or name.startswith(("log.", "logger."))
            no_pos = _SKIP_POSITIONS.get(last, set())
            for i, a in enumerate(node.args):
                self._walk(
                    mod,
                    a,
                    page,
                    func,
                    name,
                    skip=skip_args or i in no_pos,
                    shown=last in _DISPLAY_CALLS,
                )
            for kw in node.keywords:
                self._walk(
                    mod,
                    kw.value,
                    page,
                    func,
                    f"{name}({kw.arg}=)",
                    skip=skip_args or (kw.arg in _SKIP_KWARGS),
                    shown=kw.arg in _DISPLAY_KWARGS,
                )
            return
        if isinstance(node, ast.Dict):
            for k, v in zip(node.keys, node.values, strict=True):
                if k is not None:
                    self._walk(mod, k, page, func, ctx, skip=True)  # option values / keys
                self._walk(mod, v, page, func, ctx or "dict value", skip=skip)
            return
        if isinstance(node, ast.Compare):
            for sub in [node.left, *node.comparators]:
                self._walk(mod, sub, page, func, ctx, skip=True)
            return
        if isinstance(node, ast.Subscript):
            self._walk(mod, node.value, page, func, ctx, skip=skip)
            self._walk(mod, node.slice, page, func, ctx, skip=True)
            return
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            return  # stray docstring / comment string
        if isinstance(node, ast.BinOp):  # "a" + "b", "%s" % x
            for sub in (node.left, node.right):
                self._walk(mod, sub, page, func, ctx, skip=skip, shown=shown)
            return
        for child in ast.iter_child_nodes(node):
            self._walk(mod, child, page, func, ctx, skip=skip)


def main() -> int:
    rows = Extractor().run()
    rows.sort(key=lambda r: (not r.page.startswith("/welcome"), r.file, r.line))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(["id", "page", "file", "line", "function", "context", "text", "new_text"])
        for i, r in enumerate(rows, 1):
            w.writerow([i, r.page, r.file, r.line, r.function, r.context, r.text, ""])
    print(f"{len(rows)} strings -> {OUT.relative_to(ROOT)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
