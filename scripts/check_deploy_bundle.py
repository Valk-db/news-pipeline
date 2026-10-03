"""Fail a build whose Vercel bundle cannot import, render, or resolve a dependency.

The dev suite cannot see a deploy break. Every file is present in the working tree,
so a module that `.vercelignore` drops is still importable locally and every test
passes. This gate closes that gap: it derives the deploy file set from the real
`.vercelignore`, copies it somewhere the dev checkout is not importable, and
imports the real entry point from that copy alone.

It reads the repository and nothing else. No database, no network, no credentials,
no `.env.local`, so it is safe to run in CI and safe to run on a laptop on a plane.

    python -m scripts.check_deploy_bundle --tree .

Exit code is 0 when the bundle is sound and 1 when it is not, so it drops straight
into a CI step. The logic lives in `scripts/deploy_bundle.py`; this file is only the
command line around it.
"""

from __future__ import annotations

import argparse
import pathlib
import sys

from scripts import deploy_bundle as db


def _rule(title: str) -> None:
    print("\n%s\n%s" % (title, "-" * len(title)))


def _fmt_list(items, limit: int = 12) -> str:
    items = list(items)
    if not items:
        return "none"
    head = ", ".join(items[:limit])
    return head if len(items) <= limit else "%s (+%d more)" % (head, len(items) - limit)


def report(tree: pathlib.Path, *, python: str | None = None,
           verbose: bool = False) -> tuple[bool, list[str]]:
    """Run every deploy check against `tree`. Returns `(ok, failures)`."""
    failures: list[str] = []
    tree = tree.resolve()

    file_set = db.deploy_file_set(tree)
    _rule("1. Deploy file set (.vercelignore applied to the tracked tree)")
    print("included files : %d" % len(file_set.included))
    print("excluded files : %d" % len(file_set.excluded))
    print("excluded dirs  : %s" % _fmt_list(sorted(file_set.excluded_dirs)))
    untracked = db.untracked_not_ignored(tree)
    if untracked:
        print("\nuntracked but not gitignored (NOT part of a git deploy, so not "
              "shipped either): %s" % _fmt_list(untracked))
    for rule in file_set.rules:
        if rule.negated:
            print("re-included by line %d: %s" % (rule.lineno, rule.pattern))
    if verbose:
        for top, counts in sorted(file_set.summary_by_top_level().items()):
            print("  %-34s in=%-4d out=%-4d dirs_out=%d"
                  % (top, counts["included"], counts["excluded"],
                     counts["excluded_dirs"]))

    _rule("2. Import check (curation_ui.main from the filtered tree alone)")
    check = db.import_check(file_set, python=python)
    print("files copied        : %d" % check.copied_files)
    print("entry point imports  : %s" % ("yes" if check.ok else "NO"))
    print("isolation verified  : %s (probe %s)"
          % (check.payload.get("isolation_verified"),
             check.payload.get("isolation_probe")))
    print("repo importable     : %s" % check.payload.get("repo_importable"))
    print("dev hooks removed   : %s"
          % _fmt_list(check.payload.get("dropped_editable_hooks", [])))
    print("first-party modules : %d, all from the bundle: %s"
          % (len(check.modules),
             all(m.get("in_bundle") for m in check.modules.values())))
    outside = sorted(n for n, m in check.modules.items() if not m.get("in_bundle"))
    if outside:
        failures.append("first-party modules resolved outside the bundle: %s"
                        % _fmt_list(outside))
    if not check.ok:
        for err in check.errors:
            print("\nFAIL [%s] %s" % (err.get("kind"), err.get("error")))
            for line in err.get("chain", [])[-8:]:
                print("      %s" % line)
        failures.append("the bundle does not import: %s"
                        % _fmt_list([str(e.get("error"))[:80] for e in check.errors], 3))
    elif not check.payload.get("isolation_verified"):
        failures.append("isolation could not be proven; every other result is void")

    _rule("3. Templates")
    templates = check.templates
    referenced = check.templates_referenced
    print("shipped templates   : %d" % len(templates))
    print("referenced by code  : %d" % len(referenced))
    for name, status in sorted(templates.items()):
        if status != "ok":
            print("  BROKEN  %-24s %s" % (name, status))
            failures.append("template %s does not compile: %s" % (name, status))
    for name, status in sorted(referenced.items()):
        if status != "ok":
            print("  MISSING %-24s referenced by shipped code" % name)
            failures.append("template %s is referenced but not shipped" % name)
    if not failures:
        print("all shipped templates compile and every referenced one is present")

    _rule("4. Routes and cron targets")
    print("routes served       : %d" % len(check.routes))
    for path in db.CRON_PATHS:
        hit = check.cron.get(path)
        if hit is None:
            print("  MISSING  %s" % path)
            failures.append("vercel.json schedules %s but no route serves it" % path)
        else:
            print("  ok       %-32s -> %s (%s)"
                  % (path, hit["endpoint"], hit["module"]))
    if verbose:
        for route in check.routes:
            print("  %-8s %-42s %s" % (",".join(route["methods"]), route["path"],
                                      route["endpoint"]))

    _rule("5. Cross-boundary imports (shipped code reaching into dropped code)")
    runtime = check.runtime_files
    boundaries = db.cross_boundary_imports(file_set, files=runtime)
    broken = [b for b in boundaries if not b.shipped]
    latent = [b for b in broken if b.scope == "function"]
    live = [b for b in broken if b.scope == "module"]
    print("runtime files       : %d" % len(runtime))
    print("boundary edges      : %d broken (%d at import time, %d inside a function)"
          % (len(broken), len(live), len(latent)))
    for edge in broken:
        print("  %-8s %-44s :%-4d -> %s"
              % (edge.scope, edge.source, edge.lineno, edge.target))
    if live:
        failures.append(
            "%d shipped module(s) import a dropped module at import time: %s"
            % (len(live), _fmt_list(["%s -> %s" % (b.source, b.target) for b in live], 5)))
    if latent:
        print("\nNOTE: the function-scope edges are latent, not live. They are "
              "correct today\n      because no served code path reaches them, and "
              "they break the first time one does.\n      They are reported, not "
              "failed: making them fatal would fail on code that works.")

    _rule("6. Dependency closure vs requirements.txt")
    closure = db.dependency_closure(tree, check.loaded_third_party)
    unsatisfied = [d for d in closure if d.satisfied_by.startswith("UNSATISFIED")]
    direct = [d for d in closure if d.satisfied_by == "requirements.txt"]
    transitive = [d for d in closure if d.satisfied_by.startswith("transitive")]
    print("modules loaded      : %d (%d direct, %d transitive)"
          % (len(closure), len(direct), len(transitive)))
    print("unsatisfied         : %d" % len(unsatisfied))
    for dep in unsatisfied:
        print("  UNSATISFIED %-26s dist=%s" % (dep.module, dep.dist))
        failures.append("bundle loads %s (%s) and nothing in requirements.txt "
                        "provides it" % (dep.module, dep.dist))
    covered, why = db.lockfile_covers_imports(file_set)
    print("uv.lock             : %s" % why)

    _rule("7. Requirement sets that disagree")
    requirements = db.read_requirements(tree)
    pyproject = db._pyproject_dependencies(tree)
    names: set[str] = set()
    for group in pyproject:
        for spec in group:
            import re as _re
            head = _re.split(r"[<>=!~\[;@ ]", spec, maxsplit=1)[0].strip()
            if head:
                names.add(head.lower().replace("_", "-"))
    only_requirements = sorted(set(requirements) - names)
    only_pyproject = sorted(names - set(requirements))
    print("in requirements.txt but in no pyproject dependency list: %s"
          % _fmt_list(only_requirements))
    print("in pyproject but not in requirements.txt               : %s"
          % _fmt_list(only_pyproject))
    print("note: pyproject.toml does not ship, so requirements.txt is what Vercel "
          "installs.\n      A package only pyproject names is a deploy-time absence.")

    _rule("RESULT")
    if failures:
        for item in failures:
            print("FAIL  %s" % item)
        print("\nDEPLOY BUNDLE CHECK FAILED (%d problem(s))" % len(failures))
        return False, failures
    print("DEPLOY BUNDLE CHECK PASSED")
    print("The filtered file set imports on its own, every template it serves "
          "compiles,\nboth vercel.json cron paths are routes, and every module it "
          "loads is\nreachable from requirements.txt. No deploy was performed and "
          "no credential was read.")
    return True, failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="check_deploy_bundle",
        description="Check that the .vercelignore-filtered file set imports, "
                    "renders and resolves its dependencies on its own.",
    )
    parser.add_argument("--tree", default=".", type=pathlib.Path,
                        help="path to the repository root (default: the cwd)")
    parser.add_argument("--python", default=None,
                        help="interpreter for the isolated child (default: this one)")
    parser.add_argument("--verbose", action="store_true",
                        help="list every shipped file by top-level directory and "
                             "every route")
    args = parser.parse_args(argv)

    if not (args.tree / ".vercelignore").exists():
        print("no .vercelignore in %s; nothing to check" % args.tree, file=sys.stderr)
        return 1
    ok, _failures = report(args.tree, python=args.python, verbose=args.verbose)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
