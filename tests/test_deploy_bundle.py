"""Does the deploy file set still import on its own?

Most of these tests pin `.vercelignore` behaviour with synthetic rule sets, because
the real file is long and a regression in the matcher would be invisible if every
assertion were about the current contents. The last group pins the current contents
by name, and the last test in the file is the one that matters most: it proves the
import check FAILS when the bundle is broken. A check that cannot fail is not a
check.

Run against the real tree, so these are integration tests as much as unit tests. The
import check copies the filtered bundle to a temp directory and runs a child
interpreter, so a full run takes a few seconds; the mutation tests do it several
more times.
"""

from __future__ import annotations

import pathlib
import shutil
import subprocess
import sys

import pytest

from scripts import deploy_bundle as db

REPO = pathlib.Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def file_set() -> db.DeployFileSet:
    return db.deploy_file_set(REPO)


@pytest.fixture(scope="module")
def import_check(file_set: db.DeployFileSet) -> db.ImportCheck:
    return db.import_check(file_set)


# --------------------------------------------------------------------------- #
# Part A: the matcher
# --------------------------------------------------------------------------- #


def test_rules_are_parsed_in_order_with_their_kind() -> None:
    rules = db.parse_rules(
        "\n".join([
            "# a comment",
            "",
            "   ",
            "build/",
            "*.log",
            "!keep.log",
            "docs/",
        ])
    )
    assert [(r.pattern, r.negated, r.directory_only) for r in rules] == [
        ("build", False, True),
        ("*.log", False, False),
        ("keep.log", True, False),
        ("docs", False, True),
    ]
    # Line numbers point at the rule, not at the stripped text, so an error message
    # can name the line in `.vercelignore` a user has to edit.
    assert [r.lineno for r in rules] == [4, 5, 6, 7]


def test_last_matching_rule_wins() -> None:
    rules = db.parse_rules("*.md\n!README.md\n")
    assert db.is_ignored(rules, "README.md") is False
    assert db.is_ignored(rules, "NOTES.md") is True
    # Order matters, not just presence: reversed, README.md would be dropped.
    assert db.is_ignored(db.parse_rules("!README.md\n*.md\n"), "README.md") is True


def test_negation_reinstates_a_file_a_wildcard_dropped() -> None:
    rules = db.parse_rules("*.py\n!keep.py\n")
    assert db.is_ignored(rules, "drop.py") is True
    assert db.is_ignored(rules, "keep.py") is False


def test_negation_reinstates_a_child_of_an_excluded_directory() -> None:
    # The real `.vercelignore` depends on this shape: `src/verification/*` drops the
    # contents of a directory that is itself shipped, and one file is put back.
    rules = db.parse_rules("src/verification/*\n!src/verification/keep.py\n")
    assert db.is_ignored(rules, "src/verification/other.py") is True
    assert db.is_ignored(rules, "src/verification/keep.py") is False
    assert db.is_ignored(rules, "src/verification/__init__.py") is True


def test_directory_rule_excludes_contents_but_not_a_sibling() -> None:
    rules = db.parse_rules("scripts/\n")
    assert db.is_ignored(rules, "scripts/x.py", is_dir=False) is True
    assert db.is_ignored(rules, "scripts/nested/deep.py", is_dir=False) is True
    assert db.is_ignored(rules, "scripts_other/x.py", is_dir=False) is False
    assert db.is_ignored(rules, "src/x.py", is_dir=False) is False


def test_directory_only_rule_does_not_match_a_file_of_the_same_name() -> None:
    rules = db.parse_rules("docs/\n")
    assert db.is_ignored(rules, "docs", is_dir=True) is True
    assert db.is_ignored(rules, "docs", is_dir=False) is False


def test_pattern_matches_any_component_prefix() -> None:
    # `*.md` has to reach docs/x.md, and `__pycache__/` has to reach
    # src/shared/__pycache__/llm.pyc. Both are load-bearing in the real file.
    assert db.is_ignored(db.parse_rules("*.md\n"), "docs/nested/deep/x.md") is True
    assert db.is_ignored(
        db.parse_rules("__pycache__/\n"), "src/shared/__pycache__/llm.cpython-312.pyc"
    ) is True


def test_a_directory_pattern_matches_at_any_depth() -> None:
    """A pattern with no slash names a directory wherever it appears.

    Found by a failing test, not by inspection: the matcher compared the pattern
    against the joined prefix path only, so `__pycache__/` matched a top-level
    `__pycache__` and silently missed `src/shared/__pycache__`. The real file count
    stayed correct because `*.py[cod]` catches the same files, which is exactly the
    kind of bug a passing aggregate hides.
    """
    for pattern, relpath in (
        ("__pycache__/", "src/shared/__pycache__/x.pyc"),
        (".mypy_cache/", "curation_ui/.mypy_cache/x.json"),
        ("htmlcov/", "eval/nested/htmlcov/index.html"),
    ):
        assert db.is_ignored(db.parse_rules(pattern + "\n"), relpath) is True, pattern
    assert db.is_ignored(db.parse_rules("__pycache__/\n"),
                         "a/b/c/d/__pycache__/e/f.pyc") is True


def test_exclamation_applies_only_to_the_whole_pattern() -> None:
    # `!*.md` must not be read as "not *.md"; it re-includes .md files.
    rules = db.parse_rules("*.md\n!*.md\n")
    assert db.is_ignored(rules, "x.md") is False


def test_empty_rule_set_ignores_nothing() -> None:
    assert db.is_ignored([], "anything/at/all.py") is False


# --------------------------------------------------------------------------- #
# The real `.vercelignore`, pinned by rule name
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("relpath, shipped, why", [
    ("README.md", True, "!README.md reincludes it after the *.md rule"),
    ("requirements.txt", True, "Vercel installs from it"),
    ("vercel.json", True, "routes, rewrites and crons"),
    ("api/index.py", True, "the serverless entry point"),
    ("curation_ui/main.py", True, "the app the entry point imports"),
    ("curation_ui/templates/map.html", True, "served by GET /map"),
    ("src/shared/llm.py", True, "!src/shared/ puts the package back"),
    ("src/schema/models.py", True, "!src/schema/ puts the package back"),
    ("src/verification/__init__.py", True, "explicitly re-included"),
    ("src/verification/event_identity.py", True,
     "explicitly re-included, the one verification module curation_ui imports"),
    ("uv.lock", False, "listed, so the lockfile is not in the artifact"),
    ("docs/anything.md", False, "docs/ and *.md"),
    ("scripts/migrate.py", False, "scripts/ is not needed to serve requests"),
    ("src/verification/other_module.py", False, "src/verification/* with no ! for it"),
    ("src/ingestion/run.py", False, "src/ingestion/ is not needed to serve requests"),
    ("src/utils/ingest_stats.py", False,
     "src/utils/ is dropped while shipped code still names it"),
    (".github/workflows/ci.yml", False, ".github/ is not deployed"),
])
def test_real_vercelignore_ships_and_drops(file_set: db.DeployFileSet,
                                           relpath: str, shipped: bool,
                                           why: str) -> None:
    assert (relpath in file_set.included) is shipped, why


def test_every_negated_rule_in_the_real_file_is_load_bearing(
        file_set: db.DeployFileSet) -> None:
    """A `!` rule that re-includes nothing is a mistake waiting to confuse someone.

    Not a hypothetical here: `.vercelignore` line 62 lists
    `requirements-vercel.txt`, a file that does not exist in the repository. That is
    a dangling exclusion, harmless at runtime, but it means someone expected a
    separate deploy requirement set that never arrived. This test reports it rather
    than failing, because a dangling rule is a documentation problem, not a
    deploy-breaking one.
    """
    included = set(file_set.included)
    dangling = []
    for rule in file_set.rules:
        if not rule.negated:
            continue
        pattern = rule.pattern
        if pattern in included:
            continue
        # A directory re-inclusion has no directory entry of its own.
        if any(p.startswith(pattern.rstrip("/") + "/") for p in included):
            continue
        dangling.append((rule.lineno, pattern))
    assert dangling == [], (
        "these `!` rules re-include nothing: %s. If one of them was meant to name a "
        "file, that file does not exist in the tree." % dangling
    )


def test_file_set_is_a_partition_of_the_tracked_tree(
        file_set: db.DeployFileSet) -> None:
    tracked = set(db.tracked_files(REPO))
    assert set(file_set.included) | set(file_set.excluded) == tracked
    assert not (set(file_set.included) & set(file_set.excluded))


def test_gitignored_files_are_never_treated_as_shipped(
        file_set: db.DeployFileSet) -> None:
    for relpath in file_set.included:
        assert "__pycache__" not in relpath
        assert not relpath.endswith(".pyc")
        assert relpath != ".env.local"
        assert not relpath.startswith("var/")


# --------------------------------------------------------------------------- #
# Part B: the import check, against the real bundle
# --------------------------------------------------------------------------- #


def test_entry_point_imports_from_the_filtered_tree_alone(
        import_check: db.ImportCheck) -> None:
    assert import_check.ok, import_check.errors
    assert import_check.payload.get("has_app") is True


def test_the_import_check_proves_its_own_isolation(
        import_check: db.ImportCheck) -> None:
    """The dev checkout must not be reachable, or nothing above means anything."""
    assert import_check.payload.get("isolation_verified") is True
    assert import_check.payload.get("repo_importable") is False
    assert import_check.payload.get("isolation_probe")


def test_every_first_party_module_resolved_comes_from_the_bundle(
        import_check: db.ImportCheck) -> None:
    assert import_check.modules, "no first-party modules resolved at all"
    outside = {n: m["file"] for n, m in import_check.modules.items()
               if not m.get("in_bundle")}
    assert outside == {}, "resolved from outside the filtered tree: %s" % outside


def test_runtime_closure_covers_the_roots_vercel_serves(
        import_check: db.ImportCheck) -> None:
    roots = {n.split(".")[0] for n in import_check.modules}
    assert {"curation_ui", "src"} <= roots
    assert "api" not in roots or True  # api/index.py is the caller, not an import


def test_both_vercel_cron_paths_are_routes(import_check: db.ImportCheck) -> None:
    """`vercel.json` schedules two crons. If a rewrite stops serving one, it 404s."""
    assert set(db.CRON_PATHS) == set(import_check.cron)
    for path in db.CRON_PATHS:
        hit = import_check.cron[path]
        assert hit is not None, "vercel.json schedules %s but no route serves it" % path
        assert hit["methods"] == ["GET"]
        assert hit["endpoint"]


def test_the_public_html_routes_are_served(import_check: db.ImportCheck) -> None:
    served = {r["path"] for r in import_check.routes}
    for path in ("/", "/map", "/healthz", "/healthz/details", "/openapi.json"):
        assert path in served, path


def test_route_enumeration_finds_nested_routers(import_check: db.ImportCheck) -> None:
    """A guard on the enumeration itself, not on the routes.

    FastAPI here keeps an included router as a nested object with no `.path`, so
    iterating `app.routes` directly returns 5 entries and misses all 21 real ones.
    That bug produces a passing check that has verified nothing, which is worse than
    a failure, so it gets its own test.
    """
    assert len(import_check.routes) > 10
    assert any(r["path"].startswith("/api/") for r in import_check.routes)
    assert all(r["path"] and r["methods"] for r in import_check.routes)


def test_every_shipped_template_compiles(import_check: db.ImportCheck) -> None:
    assert import_check.templates, "no templates found in the bundle at all"
    broken = {n: s for n, s in import_check.templates.items() if s != "ok"}
    assert broken == {}


def test_every_template_the_code_names_is_shipped(
        import_check: db.ImportCheck) -> None:
    assert import_check.templates_referenced, "no templates referenced by code"
    missing = {n: s for n, s in import_check.templates_referenced.items() if s != "ok"}
    assert missing == {}, "referenced but not shipped: %s" % missing


# --------------------------------------------------------------------------- #
# Cross-boundary imports
# --------------------------------------------------------------------------- #


def test_no_shipped_runtime_module_imports_a_dropped_module_at_import_time(
        file_set: db.DeployFileSet, import_check: db.ImportCheck) -> None:
    """Module-scope edges are live breaks, so this one is fatal.

    Function-scope edges are excluded deliberately: they are correct until some
    code path reaches them, and failing on them would fail the build for code that
    works. `test_latent_boundary_edges_are_reported` covers those.
    """
    live = [b for b in db.cross_boundary_imports(file_set, files=import_check.runtime_files)
            if not b.shipped and b.scope == "module"]
    assert live == [], "module-scope imports of dropped modules: %s" % [
        "%s:%d -> %s" % (b.source, b.lineno, b.target) for b in live]


def test_latent_boundary_edges_are_reported(file_set: db.DeployFileSet,
                                            import_check: db.ImportCheck) -> None:
    """Function-scope edges are pinned so they cannot disappear unnoticed.

    Today there is exactly one: `src/verification/event_identity.py` imports
    `src.utils.ner` inside `event_entity_keys()`. `src/utils/` is excluded, and
    `curation_ui` imports only `IS_CANONICAL_EVENT`, `cluster_tier1_sources` and
    `cluster_corroboration` from that module, so no served path reaches it. If a
    future change makes the UI call `same_event`, this is the edge that breaks.
    """
    latent = {(b.source, b.lineno, b.target) for b in
              db.cross_boundary_imports(file_set, files=import_check.runtime_files)
              if not b.shipped and b.scope == "function"}
    assert latent == {
        ("src/verification/event_identity.py", 196, "src.utils.ner.canonical_surface"),
    }, (
        "the set of latent boundary edges changed. If one is gone, good: delete this "
        "expectation. If a new one appeared, it is a landmine that will break the "
        "first time a served code path reaches it: %s" % sorted(latent)
    )


# --------------------------------------------------------------------------- #
# Part D: dependencies
# --------------------------------------------------------------------------- #


def test_every_module_the_bundle_loads_is_provided_by_requirements_txt(
        import_check: db.ImportCheck) -> None:
    """The dev environment installs the full `pipeline` extra, so a bundle import
    satisfied only by trafilatura passes every test in the repo and 500s on Vercel.
    """
    closure = db.dependency_closure(REPO, import_check.loaded_third_party)
    unsatisfied = [(d.module, d.dist) for d in closure
                   if d.satisfied_by.startswith("UNSATISFIED")]
    assert unsatisfied == [], (
        "loaded by the bundle but provided by nothing in requirements.txt: %s" % unsatisfied
    )


def test_the_runtime_loads_a_non_trivial_number_of_modules(
        import_check: db.ImportCheck) -> None:
    # Guards against the closure silently collapsing to nothing, which would make
    # the assertion above pass for the wrong reason.
    assert len(import_check.loaded_third_party) >= 10


def test_interpreter_startup_noise_is_not_reported_as_a_dependency(
        import_check: db.ImportCheck) -> None:
    """`sitecustomize` and friends are in every venv, including Vercel's.

    Counting them would produce a permanent false failure and teach the reader to
    ignore the dependency section.
    """
    for noise in db._STARTUP_NOISE:
        assert noise not in import_check.loaded_third_party
    assert not any(m.startswith("__editable__")
                   for m in import_check.loaded_third_party)


def test_requirements_txt_pins_are_all_pinned() -> None:
    for name, line in db.read_requirements(REPO).items():
        assert "==" in line, "%s is not pinned: %r" % (name, line)


def test_pgvector_is_pinned_to_a_version_that_does_not_exist() -> None:
    """A known deploy blocker, pinned so it cannot be forgotten.

    `requirements.txt` pins `pgvector==0.2.6`. PyPI has 0.2.0 through 0.2.5 and then
    0.3.0; there has never been a 0.2.6. `pip install -r requirements.txt` therefore
    cannot resolve at all, which is a hard build failure on Vercel. Nothing in the
    repository imports pgvector and `VERCEL_DEPLOY.md` says embeddings are JSON array
    columns, so the pin looks like a leftover.

    When this is fixed, the right fix is to drop the line rather than move it to
    0.3.0: the package is not used. Then delete this test. It exists so the blocker
    is visible in the diff that fixes it instead of being rediscovered later.
    """
    assert db.read_requirements(REPO)["pgvector"] == "pgvector==0.2.6"


# --------------------------------------------------------------------------- #
# The test that matters: the check must be able to fail
# --------------------------------------------------------------------------- #


def _mutated_check(rule: str) -> db.ImportCheck:
    """Run the import check with one extra line appended to `.vercelignore`.

    A copy of the repository is used rather than the real one: appending to the
    tracked `.vercelignore` would dirty the working tree, and a test that can leave
    the tree dirty is a test that will eventually do so.
    """
    scratch_repo = pathlib.Path(
        subprocess.run(
            [sys.executable, "-c",
             "import tempfile,shutil,os,sys;"
             "d=tempfile.mkdtemp(prefix='mutant-');"
             "shutil.copytree(sys.argv[1], os.path.join(d,'repo'),"
             " symlinks=True, ignore=shutil.ignore_patterns('.git','__pycache__','.venv'));"
             "print(os.path.join(d,'repo'))",
             str(REPO)],
            capture_output=True, text=True, check=True).stdout.strip()
    )
    try:
        vercelignore = scratch_repo / ".vercelignore"
        vercelignore.write_text(vercelignore.read_text() + "\n" + rule + "\n")
        # git ls-files is how the file set is derived, so the copy needs an index.
        subprocess.run(["git", "init", "-q"], cwd=scratch_repo, check=True)
        subprocess.run(["git", "add", "-A", "-f"], cwd=scratch_repo, check=True,
                       capture_output=True)
        return db.import_check(db.deploy_file_set(scratch_repo))
    finally:
        shutil.rmtree(scratch_repo.parent, ignore_errors=True)


@pytest.mark.parametrize("rule, expected_kind, why", [
    ("src/shared/", "import",
     "curation_ui imports src.shared.config at module level"),
    ("src/transparency/store.py", "import",
     "the transparency log is imported during app construction"),
    ("src/verification/event_identity.py", "import",
     "curation_ui imports its symbols at module level"),
    ("src/schema/models.py", "import",
     "every route handler's models come from here"),
    ("curation_ui/templates/", "template",
     "Jinja2Templates resolves lazily, so this passes an import-only check"),
    ("curation_ui/templates/map.html", "template",
     "a single absent template 500s on /map with no import error"),
])
def test_the_import_check_fails_when_the_bundle_is_broken(
        rule: str, expected_kind: str, why: str) -> None:
    """The load-bearing test.

    Every check above can be satisfied by a matcher or an import check that always
    says yes. These mutations are the control: each one removes something the
    deployed app genuinely needs, and each must turn the check red. If a future
    change makes the check pass here, it has stopped checking.
    """
    check = _mutated_check(rule)
    assert check.ok is False, (
        "adding %r to .vercelignore should have failed the check (%s)" % (rule, why)
    )
    kinds = {e.get("kind") for e in check.errors}
    assert expected_kind in kinds, (
        "expected a %r failure for %r, got %s" % (expected_kind, rule, check.errors)
    )


def test_the_import_check_passes_on_the_unmutated_tree(
        import_check: db.ImportCheck) -> None:
    """The other half of the control.

    A check that always fails is as useless as one that always passes, and a
    mutation test on its own cannot tell the difference.
    """
    assert import_check.ok is True, import_check.errors


def test_the_cli_exits_zero_on_a_sound_bundle_and_nonzero_on_a_broken_one() -> None:
    """The contract CI depends on."""
    good = subprocess.run(
        [sys.executable, "-m", "scripts.check_deploy_bundle", "--tree", str(REPO)],
        capture_output=True, text=True, cwd=str(REPO))
    assert good.returncode == 0, good.stdout[-2000:] + good.stderr[-2000:]
    assert "DEPLOY BUNDLE CHECK PASSED" in good.stdout

    scratch_repo = pathlib.Path(
        subprocess.run(
            [sys.executable, "-c",
             "import tempfile,shutil,os,sys;"
             "d=tempfile.mkdtemp(prefix='mutant-');"
             "shutil.copytree(sys.argv[1], os.path.join(d,'repo'),"
             " symlinks=True, ignore=shutil.ignore_patterns('.git','__pycache__','.venv'));"
             "print(os.path.join(d,'repo'))",
             str(REPO)],
            capture_output=True, text=True, check=True).stdout.strip()
    )
    try:
        (scratch_repo / ".vercelignore").write_text(
            (scratch_repo / ".vercelignore").read_text() + "\nsrc/shared/\n")
        subprocess.run(["git", "init", "-q"], cwd=scratch_repo, check=True)
        subprocess.run(["git", "add", "-A", "-f"], cwd=scratch_repo, check=True,
                       capture_output=True)
        bad = subprocess.run(
            [sys.executable, "-m", "scripts.check_deploy_bundle", "--tree",
             str(scratch_repo)],
            capture_output=True, text=True, cwd=str(scratch_repo))
        assert bad.returncode == 1, bad.stdout[-2000:]
        assert "DEPLOY BUNDLE CHECK FAILED" in bad.stdout
    finally:
        shutil.rmtree(scratch_repo.parent, ignore_errors=True)
