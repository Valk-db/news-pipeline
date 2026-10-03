"""What actually ships to Vercel, and whether that file set imports on its own.

A green dev environment cannot see a deploy break. Every file is present locally,
so a module that `.vercelignore` drops is still importable from the working tree
and the test suite passes anyway. The company has already paid for that lesson
once: a batch was live-verified against dev Supabase, the suite was green, ruff
was clean, and the deploy still broke, because `.vercelignore` removed a file the
serverless bundle imports. Nothing in dev shows it.

So this module answers two questions mechanically, with no hand-written file list:

  1. Which files ship? `deploy_file_set()` applies the real `.vercelignore` rules
     to the tracked tree, in order, last match wins.
  2. Do those files import? `import_check()` copies the filtered set to a scratch
     directory, puts that directory first on `sys.path`, removes the dev tree's
     editable-install hook so it cannot quietly supply a missing module, and then
     imports the real entry point in a fresh process.

Both are importable functions rather than a script you run once, because
`scripts/check_deploy_bundle.py` (the CI gate) and `tests/test_deploy_bundle.py`
(the matcher unit tests) are both consumers of the same logic.

`.vercelignore` semantics honoured here, which are the ones the real file depends
on:
  - rules are ordered and the LAST match wins;
  - `!` re-includes whatever an earlier rule ignored;
  - a trailing `/` makes a rule directory-only;
  - a pattern matches any component-prefix of a path, so `scripts/` covers
    `scripts/foo.py` and `*.md` covers `docs/x.md`;
  - a directory-only rule may re-include a child (`src/verification/*` ignores the
    contents while `!src/verification/event_identity.py` puts one file back),
    because `src/verification/` itself was never ignored.

The file set is derived from `git ls-files`, so untracked and gitignored files
(the local `.env.local`, `__pycache__`, `var/`) are never treated as shipped.
That is the deterministic set, and it is what a Vercel git deploy uploads.
"""

from __future__ import annotations

import ast
import dataclasses
import fnmatch
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterable, Iterator, Sequence

# Top-level packages that make up the deployed surface. `api/index.py` is three
# lines of substance and imports `curation_ui.main`, so these three roots (plus
# whatever they import) are everything Vercel serves.
FIRST_PARTY_ROOTS: tuple[str, ...] = ("src", "curation_ui", "api")

# Paths a Vercel deployment reads. Both crons rewrite to /api/index like every
# other request, so the cron handlers are routes on the same app; these are the
# paths to prove exist.
CRON_PATHS: tuple[str, ...] = ("/api/cron/checkpoint", "/api/cron/checkpoint/watchdog")

# Modules present in `sys.modules` because the interpreter started, not because
# the application imported them. Kept in step with `_STARTUP_NOISE` in the child
# program below; neither list is a dependency of the deploy.
_STARTUP_NOISE: frozenset[str] = frozenset({"sitecustomize", "_distutils_hack"})


# --------------------------------------------------------------------------- #
# The .vercelignore matcher
# --------------------------------------------------------------------------- #


@dataclasses.dataclass(frozen=True)
class Rule:
    """One parsed `.vercelignore` line."""

    pattern: str
    negated: bool
    directory_only: bool
    lineno: int
    raw: str

    def __str__(self) -> str:  # pragma: no cover - display only
        return ("!" if self.negated else "") + self.pattern + ("/" if self.directory_only else "")


def parse_rules(text: str) -> list[Rule]:
    """Parse `.vercelignore` contents into ordered rules.

    Blank lines and `#` comments are dropped. `!` prefix sets `negated`; a
    trailing `/` sets `directory_only`. Order is preserved because last match
    wins.
    """
    rules: list[Rule] = []
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        negated = line.startswith("!")
        if negated:
            line = line[1:].strip()
        if not line:
            continue
        directory_only = line.endswith("/")
        if directory_only:
            line = line[:-1].strip()
        rules.append(Rule(pattern=line, negated=negated, directory_only=directory_only,
                          lineno=lineno, raw=raw.strip()))
    return rules


def _prefixes(relpath: str) -> Iterator[tuple[str, bool]]:
    """Yield `(path, is_final)` for each component-prefix of `relpath`.

    `a/b/c.py` yields `a` (a directory), `a/b` (a directory), `a/b/c.py` (the file
    itself). A directory-only rule may match any of the directory prefixes, and a
    plain rule may match any of them.
    """
    parts = relpath.split("/")
    for i in range(1, len(parts) + 1):
        yield "/".join(parts[:i]), i == len(parts)


def rule_matches(rule: Rule, relpath: str, *, is_dir: bool) -> bool:
    """Whether `rule` matches `relpath`.

    Matching is per component-prefix so `scripts/` covers `scripts/foo.py` and
    `*.md` covers `docs/x.md`, which is what gitignore-family ignore files do.

    A pattern is also tested against each prefix's own basename, because a pattern
    with no slash matches at any depth: `__pycache__/` has to catch
    `src/shared/__pycache__/llm.cpython-312.pyc`, and testing only the joined prefix
    path misses it. The `*.py[cod]` rule happens to catch that file today, which is
    why the bug survived a correct-looking file count, but `__pycache__/`,
    `.mypy_cache/` and `htmlcov/` are all in the real file and all nested.
    """
    for prefix, is_final in _prefixes(relpath):
        if rule.directory_only and is_final and not is_dir:
            # A trailing-slash rule names directories; the leaf file it would
            # name is not a directory, so it does not match.
            continue
        if fnmatch.fnmatchcase(prefix, rule.pattern):
            return True
        if not is_final and fnmatch.fnmatchcase(prefix.rsplit("/", 1)[-1], rule.pattern):
            return True
    return False


def last_matching_rule(rules: Sequence[Rule], relpath: str, *, is_dir: bool) -> Rule | None:
    """The last rule that matches `relpath`, or None if no rule does."""
    match = None
    for rule in rules:
        if rule_matches(rule, relpath, is_dir=is_dir):
            match = rule
    return match


def is_ignored(rules: Sequence[Rule], relpath: str, *, is_dir: bool = False) -> bool:
    """Whether `.vercelignore` drops `relpath`. Last match wins; `!` re-includes."""
    rule = last_matching_rule(rules, relpath, is_dir=is_dir)
    return rule is not None and not rule.negated


# --------------------------------------------------------------------------- #
# Part A: the filtered file set
# --------------------------------------------------------------------------- #


def tracked_files(tree: pathlib.Path) -> list[str]:
    """Repo-relative posix paths of git-tracked files, sorted.

    Tracked rather than "everything on disk" so the set is deterministic and
    untracked/gitignored files (`.env.local`, `__pycache__`, `var/`) are never
    counted as shipped.
    """
    out = subprocess.run(
        ["git", "-C", str(tree), "ls-files", "-z"],
        check=True, capture_output=True, text=True,
    ).stdout
    return sorted(p for p in out.split("\0") if p)


def untracked_not_ignored(tree: pathlib.Path) -> list[str]:
    """Untracked files that are not gitignored, i.e. things a dirty-tree deploy
    would additionally upload. Reported as a warning, never as shipped."""
    out = subprocess.run(
        ["git", "-C", str(tree), "ls-files", "-z", "--others", "--exclude-standard"],
        check=True, capture_output=True, text=True,
    ).stdout
    return sorted(p for p in out.split("\0") if p)


def _all_dirs(files: Iterable[str]) -> set[str]:
    dirs: set[str] = set()
    for path in files:
        parts = path.split("/")
        for i in range(1, len(parts)):
            dirs.add("/".join(parts[:i]))
    return dirs


@dataclasses.dataclass
class DeployFileSet:
    """The result of applying `.vercelignore` to a tree."""

    tree: pathlib.Path
    rules: list[Rule]
    included: list[str]
    excluded: list[str]
    included_dirs: set[str]
    excluded_dirs: set[str]

    def summary_by_top_level(self) -> dict[str, dict[str, int]]:
        """Files and directories excluded per top-level entry."""
        all_dirs = _all_dirs(self.included + self.excluded)
        out: dict[str, dict[str, int]] = {}

        def bucket(relpath: str) -> str:
            top = relpath.split("/")[0]
            return top

        for path in self.excluded:
            key = bucket(path)
            entry = out.setdefault(key, {"excluded_files": 0, "excluded_dirs": 0,
                                         "included_files": 0})
            entry["excluded_files"] += 1
        for path in self.included:
            key = bucket(path)
            entry = out.setdefault(key, {"excluded_files": 0, "excluded_dirs": 0,
                                         "included_files": 0})
            entry["included_files"] += 1
        for path in sorted(all_dirs):
            if path in self.excluded_dirs:
                entry = out.setdefault(bucket(path), {"excluded_files": 0,
                                                      "excluded_dirs": 0,
                                                      "included_files": 0})
                entry["excluded_dirs"] += 1
        return dict(sorted(out.items()))


def load_rules(tree: pathlib.Path, vercelignore: pathlib.Path | None = None) -> list[Rule]:
    """Parse the tree's `.vercelignore`."""
    path = vercelignore if vercelignore is not None else tree / ".vercelignore"
    return parse_rules(path.read_text(encoding="utf-8"))


def deploy_file_set(tree: pathlib.Path, rules: Sequence[Rule] | None = None) -> DeployFileSet:
    """Apply `.vercelignore` to the tracked tree and return what ships."""
    tree = pathlib.Path(tree)
    if rules is None:
        rules = load_rules(tree)

    everything = tracked_files(tree)
    included: list[str] = []
    excluded: list[str] = []
    for path in everything:
        if is_ignored(rules, path):
            excluded.append(path)
        else:
            included.append(path)

    all_dirs = _all_dirs(everything)
    included_dirs = {d for d in all_dirs if not is_ignored(rules, d, is_dir=True)}
    excluded_dirs = all_dirs - included_dirs

    return DeployFileSet(
        tree=tree,
        rules=list(rules),
        included=included,
        excluded=excluded,
        included_dirs=included_dirs,
        excluded_dirs=excluded_dirs,
    )


# --------------------------------------------------------------------------- #
# Part B: import-check the filtered tree in isolation
# --------------------------------------------------------------------------- #


def materialise(files: Sequence[str], tree: pathlib.Path, dest: pathlib.Path) -> int:
    """Copy `files` from `tree` into `dest`, creating parents. Returns the count."""
    dest.mkdir(parents=True, exist_ok=True)
    count = 0
    for relpath in files:
        src = tree / relpath
        if not src.is_file():
            continue
        target = dest / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, target)
        count += 1
    return count


# Runs inside the child interpreter. It must be self-contained: it is exec'd with
# `python -c`, so it cannot import anything from this module.
_CHILD = r'''
import builtins, importlib, json, os, sys, traceback, types

scratch = os.path.abspath(sys.argv[1])
repo = os.path.abspath(sys.argv[2])
probe = sys.argv[3] if len(sys.argv) > 3 else ""

# 1. Put the filtered tree first and the dev tree nowhere.
sys.path = [scratch] + [p for p in sys.path if os.path.abspath(p or ".") != repo]

# 2. Drop the editable-install hooks. The dev venv installs this project as
#    editable (`__editable__.news_pipeline.pth`), and its finder maps `src`
#    onto a checkout of this repo. Left in place it would quietly supply any
#    module .vercelignore dropped, and this check would pass while proving
#    nothing. Matched by both module name and class name: the generated finder
#    class lives in a module called __editable___<dist>_finder and is named
#    _EditableFinder / _EditableNamespaceFinder.
dropped = []
for finder in list(sys.meta_path):
    # The generated editable finder is appended to sys.meta_path as a CLASS
    # (`sys.meta_path.append(_EditableFinder)`), not an instance, so its type is
    # plain `type` and type(finder).__name__ is the string "type". Inspect the
    # object itself. Getting this wrong is silent: the finder then keeps serving
    # the dev checkout and the check passes while proving nothing.
    obj_name = getattr(finder, "__name__", None) or type(finder).__name__
    obj_mod = getattr(finder, "__module__", "") or ""
    if obj_mod.startswith("__editable__") or "Editable" in obj_name:
        sys.meta_path.remove(finder)
        dropped.append(obj_mod + "." + obj_name)
for hook in list(getattr(sys, "path_hooks", [])):
    fmod = getattr(hook, "__module__", "") or ""
    fname = getattr(hook, "__name__", "") or ""
    if fmod.startswith("__editable__") or "Editable" in fname:
        sys.path_hooks.remove(hook)
        dropped.append("path_hook:" + fmod + "." + fname)

# The generated editable install also appends a PATH_PLACEHOLDER string to
# sys.path and arms a path hook for it. Dropping the hook is not enough on its
# own: sys.path_importer_cache still holds a finder bound to the placeholder, so
# the dev checkout keeps resolving through it. Measured on this machine: with the
# hook removed but the placeholder left in place, `src.utils.ingest_stats` still
# resolved to /home/hatch/workspace/repos/news-pipeline, i.e. a DIFFERENT checkout
# of this repo, and the check would have passed while proving nothing.
sys.path[:] = [
    p for p in sys.path
    if "__editable__" not in str(p) and "__path_hook__" not in str(p)
]
sys.path_importer_cache.clear()

# 3. Guard: any first-party module that resolves outside the scratch tree is a
#    leak, and we raise at the import so the traceback IS the import chain.
def _origin_outside_scratch(mod, name):
    f = getattr(mod, "__file__", None)
    if not f:
        return False
    f = os.path.abspath(f)
    return not (f == scratch or f.startswith(scratch + os.sep))

_real_import = builtins.__import__

def _guarded(name, globals=None, locals=None, fromlist=(), level=0):
    before = set(sys.modules)
    try:
        mod = _real_import(name, globals, locals, fromlist, level)
    except ImportError as exc:
        # Only report it as a finding if the missing module is first-party or if
        # the chain started inside the bundle. Third-party ImportErrors are the
        # dependency check's business (Part D), not the filter's.
        top = name.split(".")[0]
        if top in ("src", "curation_ui", "api"):
            missing = getattr(exc, "name", None) or name
            raise type(exc)(
                "MISSING-FROM-BUNDLE: %s (%s)" % (missing, exc)
            ).with_traceback(exc.__traceback__) from None
        raise
    if level == 0 and isinstance(mod, types.ModuleType) and mod.__name__.split(".")[0] in (
        "src", "curation_ui", "api"
    ):
        if _origin_outside_scratch(mod, name):
            raise RuntimeError(
                "LEAKED-OUTSIDE-BUNDLE: %s resolved to %s" % (mod.__name__, mod.__file__)
            )
    return mod

builtins.__import__ = _guarded

result = {"ok": True, "dropped_editable_hooks": sorted(set(dropped)), "errors": []}

def fail(kind, exc, chain):
    result["ok"] = False
    result["errors"].append({
        "kind": kind,
        "error": "%s: %s" % (type(exc).__name__, exc),
        "chain": chain,
    })

def chain_of():
    return [l for l in traceback.format_stack() if "importlib" not in l][-14:]

# 4. PROVE the isolation instead of assuming it. `probe` names a module that
#    exists in the dev tree and that .vercelignore drops. If it imports here,
#    something is still serving the dev tree and every result below is
#    meaningless -- so this fails the check rather than quietly flattering it.
if probe:
    result["isolation_probe"] = probe
    pre_probe = set(sys.modules)
    try:
        __import__(probe)
    except ImportError:
        result["isolation_verified"] = True
    except Exception as exc:
        result["isolation_verified"] = False
        fail("isolation", exc,
             ["probe %s raised %s instead of ImportError; the dev tree is "
              "partly reachable" % (probe, type(exc).__name__)])
    else:
        result["isolation_verified"] = False
        fail("isolation", RuntimeError(
            "BROKEN ISOLATION: %s imported successfully from the isolated tree, "
            "so a dropped module would be supplied by the dev checkout and this "
            "check proves nothing" % probe),
            ["probe: import %s" % probe])
    # Undo the probe completely. It fails partway through a real import chain, so
    # everything it dragged in (bs4, lxml, ...) is still in sys.modules and would
    # otherwise be reported as a dependency of the app. Measured: the probe alone
    # puts beautifulsoup4, lxml, chardet and soupsieve in the loaded set.
    for mod in [m for m in list(sys.modules) if m not in pre_probe]:
        del sys.modules[mod]

try:
    import curation_ui.main as M
except Exception as exc:
    fail("import", exc, chain_of())
    print("@@JSON@@" + json.dumps(result))
    sys.exit(1)

app = getattr(M, "app", None)
result["has_app"] = app is not None

routes = []
if app is not None:
    def iter_routes(seq, seen=None):
        # FastAPI >= 0.14x keeps an included router as a nested _IncludedRouter
        # on app.routes instead of splicing its routes into the parent, so
        # iterating app.routes directly silently misses every real route. Mirrors
        # tests/test_route_table.py:iter_routes.
        seen = set() if seen is None else seen
        for route in seq:
            if id(route) in seen:
                continue
            seen.add(id(route))
            nested = getattr(route, "routes", None)
            if not isinstance(nested, list):
                original = getattr(route, "original_router", None)
                nested = getattr(original, "routes", None) if original is not None else None
            if isinstance(nested, list):
                for r in iter_routes(nested, seen):
                    yield r
                continue
            yield route

    for r in iter_routes(list(getattr(app, "routes", []))):
        methods = sorted(getattr(r, "methods", None) or [])
        path = getattr(r, "path", None)
        if not path or not methods:
            continue
        ep = getattr(r, "endpoint", None)
        routes.append({
            "path": path,
            "methods": methods,
            "endpoint": getattr(ep, "__name__", repr(ep)),
            "module": getattr(ep, "__module__", None),
        })
    routes.sort(key=lambda x: (x["path"], x["methods"]))
result["routes"] = routes

# Cron targets. Every request rewrites to /api/index, so these are routes on this
# same app; prove the path exists and name the handler that serves it.
cron = {}
for want in ("/api/cron/checkpoint", "/api/cron/checkpoint/watchdog"):
    hit = [x for x in routes if x["path"] == want]
    cron[want] = hit[0] if hit else None
result["cron"] = cron

# Templates are a data dependency the import graph cannot see. `Jinja2Templates`
# resolves its directory lazily and only touches a template when a page renders, so
# a bundle missing curation_ui/templates/ imports perfectly and then 500s on every
# HTML route. Verified by mutation: adding `curation_ui/templates/` to
# .vercelignore leaves the import check green. Compile each shipped template here
# instead, which is what the first request would do.
templates = {}
tpl_dir = os.path.join(scratch, "curation_ui", "templates")
# Use the app's own Environment, not a fresh one: curation_ui/app_state.py
# registers a custom `is_safe_url` filter on it, and a bare Environment rejects
# every template that uses it. Compiling against the wrong env would report three
# false failures and train the reader to ignore this check.
try:
    from curation_ui.app_state import templates as app_templates
    env = app_templates.env
except Exception as exc:
    env = None
    fail("template", exc, ["could not reach the app's Jinja environment"])

if os.path.isdir(tpl_dir):
    if env is not None:
        for name in sorted(os.listdir(tpl_dir)):
            if not name.endswith(".html"):
                continue
            try:
                source = env.loader.get_source(env, name)[0]
                env.parse(source, name=name)
            except Exception as exc:
                templates[name] = "ERROR %s: %s" % (type(exc).__name__, exc)
                fail("template", exc, ["jinja2 could not compile %s" % name])
            else:
                templates[name] = "ok"
else:
    # No templates at all is not automatically wrong (an API-only bundle), but this
    # app serves six HTML routes, so report it rather than pass quietly.
    templates["<directory>"] = "MISSING"
    if any(not r["path"].startswith(("/api/", "/healthz", "/docs", "/redoc",
                                     "/openapi.json"))
           for r in routes):
        fail("template", FileNotFoundError(tpl_dir),
             ["no templates directory, but HTML routes are served"])
result["templates"] = templates

# Every template a shipped file names must exist. Compiling the templates that are
# present cannot catch one that is absent: the loop above only walks the directory,
# so deleting map.html left the check green while /map and /api/map/* 500 on
# TemplateNotFound. Found by mutation. Names are read from TemplateResponse /
# get_template call arguments rather than from the route table, because a template
# can be named from a helper several frames below the handler.
import ast as _ast
referenced = {}
for root, _dirs, names in os.walk(os.path.join(scratch, "curation_ui")):
    if "__pycache__" in root:
        continue
    for fn in names:
        if not fn.endswith(".py"):
            continue
        full = os.path.join(root, fn)
        rel = os.path.relpath(full, scratch)
        try:
            parsed = _ast.parse(open(full, encoding="utf-8").read())
        except (SyntaxError, UnicodeDecodeError, OSError):
            continue
        for node in _ast.walk(parsed):
            if not isinstance(node, _ast.Call):
                continue
            fnname = getattr(node.func, "attr", None)
            if fnname not in ("TemplateResponse", "get_template"):
                continue
            # Every string argument, not just the first: the app's own helper is
            # `templates.TemplateResponse(request, "index.html", {...})`, so the
            # name is the second positional argument.
            for arg in list(node.args) + [k.value for k in node.keywords]:
                if isinstance(arg, _ast.Constant) and isinstance(arg.value, str) \
                        and arg.value.endswith((".html", ".xml", ".txt")):
                    referenced.setdefault(arg.value, []).append(
                        "%s:%d" % (rel, node.lineno))
result["templates_referenced"] = {
    name: ("ok" if os.path.exists(os.path.join(tpl_dir, name)) else "MISSING")
    for name in sorted(referenced)
}
for name, where in sorted(referenced.items()):
    if not os.path.exists(os.path.join(tpl_dir, name)):
        fail("template", FileNotFoundError(name),
             ["template %s referenced by %s is not in the bundle"
              % (name, ", ".join(where))])

# First-party modules resolved, with provenance checked against the scratch tree.
resolved = {}
for name, mod in sorted(sys.modules.items()):
    if not isinstance(mod, types.ModuleType):
        continue
    top = name.split(".")[0]
    if top not in ("src", "curation_ui", "api"):
        continue
    f = getattr(mod, "__file__", None)
    resolved[name] = {
        "file": os.path.relpath(os.path.abspath(f), scratch) if f else None,
        "in_bundle": bool(f) and not _origin_outside_scratch(mod, name),
    }
result["first_party_modules"] = resolved

# Third-party modules actually LOADED, which is stronger than a static scan of
# import statements: it catches what arrives transitively (jinja2 via
# fastapi.templating, asyncpg via SQLAlchemy's dialect) and cannot be fooled by an
# import statement inside a branch that never runs.
stdlib = set(getattr(sys, "stdlib_module_names", set()))
loaded = set()
for name, mod in list(sys.modules.items()):
    if not isinstance(mod, types.ModuleType):
        continue
    top = name.split(".")[0]
    if top in ("src", "curation_ui", "api") or top in stdlib or top == "__future__":
        continue
    if getattr(mod, "__file__", None) is None and top not in sys.builtin_module_names:
        continue  # namespace packages and synthesised modules, not a distribution
    loaded.add(top)

# Interpreter startup leaves modules in sys.modules that no application import
# caused. They are not dependencies of the deploy and must not be reported as
# missing from requirements.txt:
#   __editable__*  the dev venv's editable-install shim, executed by the .pth at
#                  interpreter start, before this script ran. Removed from
#                  sys.meta_path above; still listed in sys.modules.
#   _distutils_hack, sitecustomize, _sysconfigdata_*  setuptools' own startup
#                  hooks, present in any venv including Vercel's.
_STARTUP_NOISE = {"sitecustomize", "_distutils_hack"}
result["loaded_third_party"] = sorted(loaded)
result["loaded_startup"] = sorted(m for m in loaded if m in _STARTUP_NOISE)

result["sys_path_head"] = sys.path[:3]
result["repo_importable"] = any(
    os.path.abspath(p or ".") == repo for p in sys.path
)

print("@@JSON@@" + json.dumps(result))
'''


@dataclasses.dataclass
class ImportCheck:
    """Outcome of importing the bundle in isolation."""

    ok: bool
    copied_files: int
    filtered_files: int
    payload: dict
    stderr: str = ""

    @property
    def errors(self) -> list[dict]:
        return self.payload.get("errors", [])

    @property
    def modules(self) -> dict[str, dict]:
        return self.payload.get("first_party_modules", {})

    @property
    def routes(self) -> list[dict]:
        return self.payload.get("routes", [])

    @property
    def cron(self) -> dict:
        return self.payload.get("cron", {})

    @property
    def templates(self) -> dict[str, str]:
        """Per-template compile result, e.g. `{"index.html": "ok"}`."""
        return self.payload.get("templates", {})

    @property
    def templates_referenced(self) -> dict[str, str]:
        """Per-referenced-template presence, e.g. `{"map.html": "ok"}`."""
        return self.payload.get("templates_referenced", {})

    @property
    def loaded_third_party(self) -> list[str]:
        """Top-level non-stdlib modules the isolated import actually loaded.

        Stronger than a static scan of import statements: it catches what arrives
        transitively (jinja2 via fastapi.templating) and cannot be fooled by an
        import inside a branch that never runs. Interpreter startup modules and the
        dev venv's own editable-install shim are excluded; see `_STARTUP_NOISE` in
        the child program for why each one is not an application dependency.
        """
        noise = set(self.payload.get("loaded_startup", ())) | _STARTUP_NOISE
        return [m for m in self.payload.get("loaded_third_party", [])
                if m not in noise and not m.startswith("__editable__")
                and not m.startswith("_sysconfigdata")]

    @property
    def runtime_files(self) -> list[str]:
        """Shipped files the entry point actually reached, as tree-relative paths.

        This is the closure that matters for the dependency check. The bundle also
        contains `tests/`, `eval/` and `analysis/`, which upload to Vercel but are
        never imported by `curation_ui.main`; a library only `tests/` needs is not
        a deploy dependency, and treating it as one buries the real answer.
        """
        return sorted({
            info["file"] for info in self.modules.values()
            if info.get("in_bundle") and info.get("file")
        })


def isolation_probe(file_set: DeployFileSet) -> str:
    """A dotted module name that exists in the dev tree but is dropped.

    Used as the child's proof of isolation (see `_CHILD`). Prefers a `.py` under
    a dropped directory, then any dropped first-party module.
    """
    everything = tracked_files(file_set.tree)
    dropped = set(file_set.excluded)
    candidates: list[tuple[int, str]] = []
    for path in everything:
        if path in dropped or not path.startswith("src/") or not path.endswith(".py"):
            continue
        if path.endswith("/__init__.py"):
            continue
        parts = path[:-3].split("/")
        if parts[0] != "src" or len(parts) < 3:
            continue
        candidates.append((len(parts), ".".join(parts)))
    if not candidates:
        return ""
    return sorted(candidates)[0][1]


def import_check(file_set: DeployFileSet, *, python: str | None = None,
                 scratch_root: pathlib.Path | None = None) -> ImportCheck:
    """Import `curation_ui.main` from the filtered tree alone.

    The filtered set is copied to a scratch directory outside the repo. The child
    process gets that directory first on `sys.path` with the repo root removed and
    its editable-install hook deleted, so a module `.vercelignore` dropped raises
    `ModuleNotFoundError` instead of being quietly supplied from the working tree.
    The child then proves that isolation held by trying to import a known-dropped
    module and requiring it to fail. No database, no network, no credentials.
    """
    python = python or sys.executable
    scratch_root_arg = scratch_root
    if scratch_root_arg is not None:
        scratch_root_arg.mkdir(parents=True, exist_ok=True)
        scratch = pathlib.Path(tempfile.mkdtemp(prefix="bundle-", dir=str(scratch_root_arg)))
    else:
        scratch = pathlib.Path(tempfile.mkdtemp(prefix="deploycheck-"))
    try:
        copied = materialise(file_set.included, file_set.tree, scratch)
        probe = isolation_probe(file_set)
        env = {
            k: v for k, v in os.environ.items()
            if k not in ("PYTHONPATH", "PYTHONSTARTUP")
        }
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        proc = subprocess.run(
            [python, "-c", _CHILD, str(scratch), str(file_set.tree), probe],
            capture_output=True, text=True, cwd=str(scratch), env=env, timeout=300,
        )
        payload: dict = {}
        for line in proc.stdout.splitlines():
            if line.startswith("@@JSON@@"):
                import json
                payload = json.loads(line[len("@@JSON@@"):])
        if not payload:
            return ImportCheck(
                ok=False, copied_files=copied, filtered_files=len(file_set.included),
                payload={"ok": False, "errors": [{
                    "kind": "child-crash",
                    "error": "child produced no result (exit %d)" % proc.returncode,
                    "chain": (proc.stderr or "").strip().splitlines()[-25:],
                }]},
                stderr=proc.stderr,
            )
        return ImportCheck(
            ok=bool(payload.get("ok")) and proc.returncode == 0,
            copied_files=copied,
            filtered_files=len(file_set.included),
            payload=payload,
            stderr=proc.stderr,
        )
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


# --------------------------------------------------------------------------- #
# Cross-boundary imports: kept module reaching into a dropped one
# --------------------------------------------------------------------------- #


@dataclasses.dataclass(frozen=True)
class BoundaryImport:
    """An import in a shipped file whose target is not shipped."""

    source: str
    lineno: int
    target: str
    scope: str  # "module" or "function"
    shipped: bool


# The latent boundary edges that have been investigated, found unreachable, and are
# therefore expected rather than outstanding. Keyed by (source, target) so the
# acknowledgement survives a line moving, which it will: an unrelated edit above
# the import shifts every lineno in the file.
#
# This exists because a reported finding that nobody has classified gets
# re-investigated by every batch that sees it, and the answer is the same each
# time. `event_identity.py:196` has been reported by the bundle check since the
# check was written. Here is the answer, once, next to the finding.
#
# `why` is the call-graph argument, not a grep. `curation_ui` imports exactly four
# names from that module -- IS_CANONICAL_EVENT, CANONICAL_EVENT_ID,
# cluster_tier1_sources and cluster_corroboration -- and the first three are
# SQLAlchemy expressions while the fourth is one grouped SELECT. The only entry
# points to `event_entity_keys` are `same_event` (via `cluster_events`) and
# `assign_canonical_events`, and the sole non-test caller of either is
# `scripts/backfill_globe_events.py`, which `.vercelignore` drops. Nothing on a
# served route can reach the import, so it cannot execute on Vercel.
#
# Deliberately NOT a suppression list. `latent_edge_verdict` only ever labels a
# FUNCTION-scope edge; a module-scope edge, which is a live break, is never
# labelled and never consults this table. `test_a_known_latent_edge_cannot_hide_a_live_break`
# is the mutation that proves it.
KNOWN_LATENT_BOUNDARY_EDGES: dict[tuple[str, str], str] = {
    (
        "src/verification/event_identity.py",
        "src.utils.ner.canonical_surface",
    ): (
        "unreachable from every served route: the lazy import sits in "
        "event_entity_keys(), whose only entry points are same_event() and "
        "assign_canonical_events(), and the sole non-test caller of either is "
        "scripts/backfill_globe_events.py, which .vercelignore drops. curation_ui "
        "imports only IS_CANONICAL_EVENT, CANONICAL_EVENT_ID, cluster_tier1_sources "
        "and cluster_corroboration from this module, none of which call it. It "
        "breaks the first time a shipped code path reaches event_entity_keys(), and "
        "cross_boundary_imports() will say so again if that happens."
    ),
}


def latent_edge_verdict(edge: BoundaryImport) -> str | None:
    """The reason this known edge is unreachable, or None if it is not a known one.

    Returns None for anything at module scope, deliberately: an acknowledgement
    recorded while an edge was function-scope must not be able to launder the same
    import once someone hoists it to the top of the file, which would turn a
    latent edge into a live one.
    """
    if edge.scope != "function":
        return None
    return KNOWN_LATENT_BOUNDARY_EDGES.get((edge.source, edge.target))


def unacknowledged_latent_edges(edges: Sequence[BoundaryImport]) -> list[BoundaryImport]:
    """Latent edges nobody has investigated. The ones that still need a human."""
    return [e for e in edges
            if e.scope == "function" and not e.shipped
            and latent_edge_verdict(e) is None]


def stale_acknowledgements(edges: Sequence[BoundaryImport]) -> list[tuple[str, str]]:
    """Entries for edges that no longer exist, so the table cannot accumulate lies.

    A record of "we checked this and it is fine" is only worth keeping while the
    thing it describes exists. One that outlives its edge is a second false
    premise, which is the exact failure this table was added to stop.
    """
    present = {(e.source, e.target) for e in edges
               if e.scope == "function" and not e.shipped}
    return sorted(key for key in KNOWN_LATENT_BOUNDARY_EDGES if key not in present)


def _iter_import_nodes(tree: ast.Module):
    """Yield `(node, scope)` for every Import/ImportFrom, function bodies included.

    A function-local import still has to resolve on Vercel if the code path runs,
    so lazy imports are reported too. That is how the known `src.utils` edges were
    found: `src/verification/event_identity.py` imports it inside a function and
    says so in a comment, which is an accurate description and not a guard.
    """
    def walk(node, scope: str):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.Import, ast.ImportFrom)):
                yield child, scope
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                yield from walk(child, "function")
            else:
                yield from walk(child, scope)
    yield from walk(tree, scope="module")


def cross_boundary_imports(file_set: DeployFileSet, *,
                           files: Sequence[str] | None = None) -> list[BoundaryImport]:
    """Imports in shipped files whose target module is not shipped.

    Defaults to the whole filtered bundle; pass `ImportCheck.runtime_files` to
    restrict it to the files the entry point actually reached. A `scope` of
    "module" means the import runs on any use of the file, so a dropped target is
    a live break. A `scope` of "function" means it only runs on a code path, which
    is a latent landmine: correct today, broken the first time that path runs.
    """
    targets = sorted(files) if files is not None else sorted(file_set.included)
    shipped = set(file_set.included)
    shipped_top = {p.split("/")[0] for p in shipped}
    known_roots = set(FIRST_PARTY_ROOTS) | {
        p.split("/")[0] for p in tracked_files(file_set.tree)
    }

    def shipped_module(dotted: str) -> bool:
        parts = dotted.split(".")
        if parts[0] not in FIRST_PARTY_ROOTS:
            return True  # not first-party; the dependency check owns it
        if parts[0] == "src":
            # src/<pkg>/<mod>.py -> src/<pkg>/__init__.py must also ship.
            base = "/".join(parts[:2])
            init = base + "/__init__.py"
            as_file = "/".join(parts) + ".py"
            return init in shipped or as_file in shipped
        return parts[0] in shipped_top

    out: list[BoundaryImport] = []
    for relpath in targets:
        if not relpath.endswith(".py"):
            continue
        try:
            tree = ast.parse((file_set.tree / relpath).read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        for node, scope in _iter_import_nodes(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.split(".")[0] not in known_roots:
                        continue
                    out.append(BoundaryImport(
                        source=relpath, lineno=node.lineno, target=alias.name,
                        scope=scope, shipped=shipped_module(alias.name)))
            else:
                if node.level:  # relative import, always intra-package
                    continue
                mod = node.module or ""
                if mod.split(".")[0] not in known_roots:
                    continue
                for alias in node.names:
                    full = "%s.%s" % (mod, alias.name)
                    if alias.name == "*":
                        full = mod
                    out.append(BoundaryImport(
                        source=relpath, lineno=node.lineno, target=full,
                        scope=scope, shipped=shipped_module(full) and shipped_module(mod)))
    out.sort(key=lambda b: (b.source, b.lineno, b.target))
    return out


# --------------------------------------------------------------------------- #
# Part D: dependency closure
# --------------------------------------------------------------------------- #


_IMPORT_RE = re.compile(
    r"^\s*(?:from\s+([A-Za-z_][\w.]*)\s+import\b|import\s+([A-Za-z_][\w.]*))",
    re.MULTILINE,
)


@dataclasses.dataclass(frozen=True)
class ThirdPartyImport:
    module: str
    dist: str | None
    satisfied_by: str  # "stdlib" | "requirements.txt" | "pyproject" | "MISSING"
    in_requirements: bool


def read_requirements(tree: pathlib.Path) -> dict[str, str]:
    """Parse `requirements.txt` into `{distribution_lower: pinned_specifier}`."""
    path = tree / "requirements.txt"
    if not path.exists():
        return {}
    out: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("-r") or line.startswith("--"):
            continue
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        name = re.split(r"[<>=!~\[;]", line, maxsplit=1)[0].strip()
        out[name.lower().replace("_", "-")] = line
    return out


def _pyproject_dependencies(tree: pathlib.Path) -> list[list[str]]:
    try:
        import tomllib
    except ModuleNotFoundError:  # pragma: no cover - py3.12 has tomllib
        return []
    data = tomllib.loads((tree / "pyproject.toml").read_text(encoding="utf-8"))
    project = data.get("project", {})
    groups = [list(project.get("dependencies", []))]
    for extra in project.get("optional-dependencies", {}).values():
        groups.append(list(extra))
    return groups


def _dist_map() -> dict[str, str]:
    """Top-level module -> distribution name, for the current environment.

    `packages_distributions()` is keyed by top-level module and each value is the
    list of distributions providing it (`{"jinja2": ["Jinja2"],
    "typing_extensions": ["typing_extensions", "mypy_extensions"]}`), so it is read
    in that direction. Distributions that share a module name are disambiguated by
    preferring the one whose normalised name matches the module; without that,
    `typing_extensions` resolves to whichever of the two comes first.
    """
    try:
        from importlib.metadata import packages_distributions
    except ImportError:  # pragma: no cover
        return {}
    out: dict[str, str] = {}
    for module, dists in packages_distributions().items():
        if not dists:
            continue
        mod_norm = module.lower().replace("_", "-")
        exact = [d for d in dists
                 if d.lower().replace("_", "-") == mod_norm]
        out[module] = exact[0] if exact else sorted(dists)[0]
    return out


def _requirement_extras(line: str) -> set[str]:
    """The extras a single `requirements.txt` line requests, e.g. `{asyncio}`."""
    match = re.search(r"\[([^\]]+)\]", line)
    if not match:
        return set()
    return {e.strip().lower() for e in match.group(1).split(",") if e.strip()}


def _requirement_roots(requirements: dict[str, str]) -> tuple[set[str], dict[str, set[str]]]:
    """(distributions named directly, extras requested per distribution)."""
    roots: set[str] = set()
    extras: dict[str, set[str]] = {}
    for norm, line in requirements.items():
        roots.add(norm)
        got = _requirement_extras(line)
        if got:
            extras[norm] = got
    return roots, extras


def _dependency_closure(roots: set[str], extras: dict[str, set[str]]) -> set[str]:
    """Every distribution `pip install -r requirements.txt` would end up with.

    Walks `importlib.metadata.requires()` from the pinned roots, following base
    requirements and only those extras the file actually asks for. This is the
    distinction the deploy cares about: a module missing from `requirements.txt`
    is fine when pip pulls it in as fastapi's dependency, and fatal when nothing
    reaches it. Reading `requirements.txt` alone cannot tell those apart.

    Uses the currently installed metadata, so it reports what the dev environment
    resolved. Version skew between dev and the pinned file is a second question,
    answered separately by comparing the two.
    """
    try:
        from importlib.metadata import requires as dist_requires
    except ImportError:  # pragma: no cover
        return set(roots)
    seen: set[str] = set()
    queue: list[tuple[str, set[str]]] = [(r, set(extras.get(r, ()))) for r in roots]
    while queue:
        name, wanted = queue.pop()
        if name in seen:
            continue
        seen.add(name)
        try:
            reqs = dist_requires(name) or []
        except Exception:
            continue
        for raw in reqs:
            dep = re.split(r"[<>=!~;\[\s]", raw, maxsplit=1)[0].strip()
            dep_norm = dep.lower().replace("_", "-")
            if not dep_norm or dep_norm in seen:
                continue
            # Follow the dependency; follow an extra's packages only if this
            # distribution was installed with that extra requested upstream.
            dep_extras: set[str] = set()
            marker = raw.split(";", 1)[1] if ";" in raw else ""
            m = re.search(r'extra\s*==\s*["\']?([\w.-]+)', marker)
            if m:
                if m.group(1).lower() not in wanted:
                    continue
                dep_extras = wanted
            queue.append((dep_norm, dep_extras))
    return seen


def third_party_imports(file_set: DeployFileSet, *,
                        files: Sequence[str] | None = None) -> list[ThirdPartyImport]:
    """Every non-stdlib top-level module imported by the given shipped files.

    Defaults to the whole filtered bundle. Pass `ImportCheck.runtime_files` to get
    the deploy's real dependency closure rather than the closure of everything that
    happens to upload.
    """
    targets = sorted(files) if files is not None else sorted(file_set.included)
    stdlib = set(getattr(sys, "stdlib_module_names", set()))
    dist_map = _dist_map()
    requirements = read_requirements(file_set.tree)
    pyproject = _pyproject_dependencies(file_set.tree)
    pyproject_names: set[str] = set()
    for group in pyproject:
        for spec in group:
            name = re.split(r"[<>=!~\[;@ ]", spec, maxsplit=1)[0].strip()
            if name:
                pyproject_names.add(name.lower().replace("_", "-"))

    found: dict[str, ThirdPartyImport] = {}
    for relpath in targets:
        if not relpath.endswith(".py"):
            continue
        try:
            text = (file_set.tree / relpath).read_text(encoding="utf-8")
            parsed = ast.parse(text)
        except (SyntaxError, UnicodeDecodeError):
            continue
        mods: set[str] = set()
        for node, _scope in _iter_import_nodes(parsed):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    mods.add(alias.name.split(".")[0])
            elif node.level == 0 and node.module:
                mods.add(node.module.split(".")[0])
        for mod in mods:
            if mod in FIRST_PARTY_ROOTS or mod in stdlib or mod == "__future__":
                continue
            if mod in found:
                continue
            dist = dist_map.get(mod)
            dist_norm = (dist or mod).lower().replace("_", "-")
            in_req = dist_norm in requirements
            if in_req:
                how = "requirements.txt"
            elif dist_norm in pyproject_names:
                how = "pyproject.toml"
            elif dist is None:
                how = "MISSING (not importable in this environment)"
            else:
                how = "MISSING"
            found[mod] = ThirdPartyImport(
                module=mod, dist=dist, satisfied_by=how, in_requirements=in_req)

    return [found[m] for m in sorted(found)]


def lockfile_covers_imports(file_set: DeployFileSet) -> tuple[bool, str]:
    """Whether `uv.lock` is worth checking for the bundle's imports.

    `uv.lock` is itself in `.vercelignore`, and Vercel installs from
    `requirements.txt`, so the lockfile is not in the deployed artifact at all. It
    still governs CI's environment, so a bundle import satisfied only by a lockfile
    entry that `requirements.txt` lacks would pass CI and fail the deploy. The
    answer is reported, not enforced, because the two files are maintained
    separately today.
    """
    return True, ("uv.lock present (not shipped: listed in .vercelignore) -- CI "
                  "resolves from it, Vercel resolves from requirements.txt, so a "
                  "dependency in the lock and not in requirements.txt passes CI and "
                  "fails the deploy")


def dependency_closure(tree: pathlib.Path, loaded: Sequence[str]) -> list[ThirdPartyImport]:
    """Every module the deploy actually loaded, and how Vercel would satisfy it.

    `loaded` comes from `sys.modules` after the isolated import, so this is the
    truth about the running bundle rather than a static guess: it catches what
    arrives transitively (jinja2 via fastapi.templating) and cannot be fooled by an
    import statement inside a branch that never runs.

    A module is only a problem when nothing in the `requirements.txt` closure
    provides it. Transitive dependencies are fine, because pip installs them, and
    `sqlalchemy[asyncio]` legitimately pulls greenlet. What no dev test can see is
    a module satisfied in dev by the full `pipeline` extra and on Vercel by nothing
    at all.
    """
    requirements = read_requirements(tree)
    dist_map = _dist_map()
    roots, extras = _requirement_roots(requirements)
    closure = _dependency_closure(roots, extras)

    out: list[ThirdPartyImport] = []
    for module in sorted(loaded):
        dist = dist_map.get(module)
        dist_norm = (dist or module).lower().replace("_", "-")
        in_requirements = dist_norm in requirements
        if in_requirements:
            how = "requirements.txt"
        elif dist_norm in closure:
            how = "transitive of requirements.txt"
        else:
            how = "UNSATISFIED on Vercel"
        out.append(ThirdPartyImport(
            module=module, dist=dist, satisfied_by=how, in_requirements=in_requirements))
    return out
    return True, "uv.lock present (not shipped: listed in .vercelignore)"