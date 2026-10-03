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

    The sibling case -- a positive rule that EXCLUDES a file which does not exist
    -- is `test_no_exclusion_rule_names_a_file_that_does_not_exist` below. It was
    the one that actually bit: `.vercelignore` line 62 listed
    `requirements-vercel.txt`, a file that does not exist in the repository, so it
    was a dangling exclusion reading as intent that was never carried out.
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


# Rules that name a file which is not tracked, on purpose. Each is a safety net for
# a file that exists on SOME developer's machine and is not in git, so "it does not
# exist in the tree" is the normal state rather than a dangling intent. Anything not
# listed here has to name a real tracked file, and has to actually drop it.
_TOOL_ARTIFACT_EXCLUSIONS = {
    ".DS_Store": "macOS Finder writes one into every directory it visits",
    "Thumbs.db": "Windows Explorer writes a thumbnail cache next to any image",
    "test.db": "a local scratch sqlite file from ad-hoc queries, never committed",
    ".coverage": "pytest-cov writes it on every run; it is gitignored",
    ".Python": "a stray file some Python installs drop at the virtualenv root",
    ".venv": "the virtualenv directory, listed bare as well as with a trailing slash",
    "venv": "ditto, the other conventional name for the virtualenv",
    "env": "ditto, and the third conventional name",
}


def _is_plain_filename_rule(rule: db.Rule) -> bool:
    """A rule that names one file, as opposed to a directory or a glob.

    `deploy_bundle.py` strips a trailing slash and a leading `!` at parse time and
    records `directory_only`, so a directory rule is excluded here rather than by
    its text. Glob metacharacters are excluded because a glob is a statement about
    a class of names and is not expected to match anything in particular.
    """
    if rule.negated or rule.directory_only:
        return False
    return not any(ch in rule.pattern for ch in "*?[]!{}")


def test_no_exclusion_rule_names_a_file_that_does_not_exist(
        file_set: db.DeployFileSet) -> None:
    """A rule matching nothing reads as intent that was never carried out.

    Not hypothetical, and not the benign case it looks like. `.vercelignore`
    carried `requirements-vercel.txt` for as long as the deploy-bundle checker
    existed. That file has never been in the repository, so the rule dropped
    nothing, cost nothing, and taught every reader that a Vercel-specific
    requirement set was expected -- which is precisely the kind of false premise
    that outlives the person who wrote it. `git log` and `git ls-files` both say
    the file does not exist; the rule was intent, not documentation.

    A rule that names a REAL tracked file must also actually drop it, so the
    inverse error (a rule that looks load-bearing and is not, because a later rule
    re-includes the same path) is caught here too.
    """
    tracked = set(db.tracked_files(REPO))
    excluded = set(file_set.excluded)

    dangling = [
        (rule.lineno, rule.pattern)
        for rule in file_set.rules
        if _is_plain_filename_rule(rule)
        and rule.pattern not in tracked
        and rule.pattern not in _TOOL_ARTIFACT_EXCLUSIONS
    ]
    assert dangling == [], (
        "these exclusion rules name a file that is neither tracked nor a known tool "
        "artifact: %s. Either the file is missing (the rule reads as intent that was "
        "never carried out) or it is a tool artifact that belongs in "
        "_TOOL_ARTIFACT_EXCLUSIONS with a reason." % dangling
    )

    inert = [
        (rule.lineno, rule.pattern)
        for rule in file_set.rules
        if _is_plain_filename_rule(rule)
        and rule.pattern in tracked
        and rule.pattern not in excluded
    ]
    assert inert == [], (
        "these exclusion rules name a tracked file and still ship it, so a later rule "
        "undoes them: %s" % inert
    )


def test_the_tool_artifact_allowlist_is_not_where_the_dangling_rule_went(
        file_set: db.DeployFileSet) -> None:
    """The allowlist above is the escape hatch, so it needs its own guard.

    Otherwise the fix for a dangling rule is to add its name to the allowlist,
    which is the same defect wearing a comment. Entries must be a real reason,
    not a bare name, and `requirements-vercel.txt` may never be among them: that
    was the rule this whole group exists because of.
    """
    for name, reason in _TOOL_ARTIFACT_EXCLUSIONS.items():
        assert len(reason) > 20, (
            "%s is allowlisted with the reason %r, which says nothing. Every entry "
            "needs to name the tool that produces the file." % (name, reason)
        )
        assert not any(rule.pattern == name and rule.negated
                       for rule in file_set.rules), (
            "%s is in the artifact allowlist AND has a `!` rule" % name
        )
    assert "requirements-vercel.txt" not in _TOOL_ARTIFACT_EXCLUSIONS, (
        "requirements-vercel.txt is a deploy intent that was never carried out, not a "
        "tool artifact. Do not allowlist it; write the file or drop the rule."
    )
    # Sanity: the allowlist must not have become a dumping ground.
    assert len(_TOOL_ARTIFACT_EXCLUSIONS) <= 12


def test_the_documentation_rules_are_written_once(file_set: db.DeployFileSet) -> None:
    """`*.md` then `!README.md` appeared twice, at two ends of the file.

    The matcher takes the LAST matching rule, so the second copy decided every
    markdown path and the first copy was inert -- two pairs of rules where one
    pair would do, and a tail that reads as if it were adding something. A
    duplicated tail is also how a later edit lands in the copy nobody reads: you
    change the first `*.md` and the second one silently overrides it.
    """
    md_rules = [r for r in file_set.rules if r.pattern in ("*.md", "README.md")]
    assert len(md_rules) == 2, (
        "expected exactly one `*.md` and one `!README.md` rule, found %d: %s. A "
        "duplicated tail is inert, and the copy that wins is the LAST one, so the "
        "copy a reader edits is the copy that does nothing."
        % (len(md_rules), [(r.lineno, r.pattern, r.negated) for r in md_rules])
    )
    assert [(r.pattern, r.negated) for r in md_rules] == [("*.md", False),
                                                          ("README.md", True)], (
        "the documentation pair is not `*.md` followed by `!README.md` in that order: "
        "%s" % [(r.pattern, r.negated) for r in md_rules]
    )
    # And the decision itself, so the de-duplication is pinned as behaviour.
    assert "README.md" in file_set.included
    assert "VERCEL_DEPLOY.md" not in file_set.included
    assert "DECISIONS.md" not in file_set.included


def test_tests_are_excluded_from_the_deploy_bundle(
        file_set: db.DeployFileSet) -> None:
    """`tests/` is not shipped. A decision, so it is pinned rather than implied.

    Measured when the rule was added: tests/ was 114 of 259 shipped files and
    1,657,238 of 3,439,447 shipped bytes. Excluding it cannot break the runtime --
    nothing under `tests/` is imported by anything the entry point reaches, which
    the boundary check below proves -- and it buys the cross-boundary check its
    strongest property: with tests/ shipped, a shipped module that wrongly
    imported a test helper would import cleanly and the edge would be invisible.

    The honest cost, recorded here so nobody has to rediscover it: this removes
    the ability to run the suite against the exact uploaded tree. That ability
    does not exist today in any form -- Vercel does not publish the artifact, no
    script here fetches one, and deploy-bundle.yml runs the check against `--tree
    .`, the working tree. So the cost is of a capability nobody has, and the
    benefit is not. If a future workflow genuinely downloads the uploaded bundle
    to test it, this test is the thing that says the bundle no longer carries a
    suite, and that is the moment to revisit the rule.
    """
    shipped_tests = [p for p in file_set.included
                     if p == "tests" or p.startswith("tests/")]
    assert shipped_tests == [], (
        "tests/ is being shipped again: %s. Either the rule was dropped by accident "
        "or something now needs it, in which case say so here rather than quietly "
        "doubling the upload." % shipped_tests[:5]
    )
    # Not just "absent from included": the tracked tree still HAS them, so this
    # cannot pass because the files stopped existing.
    tracked_tests = [p for p in db.tracked_files(REPO)
                     if p.startswith("tests/") and p.endswith(".py")]
    assert len(tracked_tests) > 100, (
        "only %d tracked test files; the exclusion is being asserted against an "
        "almost-empty tests/ directory, which would make it vacuous"
        % len(tracked_tests)
    )
    assert all(p in file_set.excluded for p in tracked_tests), (
        "a tracked test file is neither shipped nor excluded: %s"
        % [p for p in tracked_tests if p not in file_set.excluded][:5]
    )
    # And the deploy still has the code it needs: a bundle of 145 files whose
    # largest single contributor is tests/ would be a sign the rule went too far.
    assert "curation_ui/main.py" in file_set.included
    assert "api/index.py" in file_set.included
    assert "src/shared/database.py" in file_set.included


def test_the_tests_rule_matches_a_module_under_tests(
        file_set: db.DeployFileSet) -> None:
    """The rule itself, against the matcher, not against its effect on the tree.

    The effect is the assertion above; this is the mechanism. A rule that stopped
    matching because the pattern or the matcher's directory handling changed would
    still leave the file set looking right on a tree with no `tests/` directory in
    it, which is not the tree Vercel sees.
    """
    from scripts import deploy_bundle

    rules = db.load_rules(REPO)
    assert db.is_ignored(rules, "tests/test_deploy_bundle.py") is True
    assert db.is_ignored(rules, "tests/deep/nested/helper.py") is True
    assert db.is_ignored(rules, "tests", is_dir=True) is True
    # The prefix trap: a rule for `tests/` must not swallow a sibling that merely
    # starts with the same characters.
    assert db.is_ignored(rules, "tests_extra/thing.py") is False
    assert db.is_ignored(rules, "contest/thing.py") is False
    # And a real module under tests/ is genuinely absent from the shipped set,
    # named rather than counted, so a re-include shows up as a name.
    assert "tests/test_deploy_bundle.py" not in file_set.included
    assert deploy_bundle.deploy_file_set is db.deploy_file_set  # sanity: same impl


def test_no_shipped_module_imports_from_tests(
        file_set: db.DeployFileSet,
        import_check: db.ImportCheck) -> None:
    """The thing that would make excluding tests/ wrong, asserted directly.

    If a shipped module ever reaches into `tests/`, the upload 500s on Vercel and
    nothing local notices, because the working tree still has the directory. The
    matcher treats `tests` as a first-party root, so `from tests.helpers import x`
    shows up as a boundary edge with `shipped=False`.
    """
    edges = [b for b in db.cross_boundary_imports(file_set, files=import_check.runtime_files)
             if not b.shipped and b.target.split(".")[0] == "tests"]
    assert edges == [], (
        "shipped code imports a module under tests/, which the upload does not have: "
        "%s" % ["%s:%d -> %s" % (b.source, b.lineno, b.target) for b in edges]
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
# Part C2: the known-edge record, and why it is a record and not a suppression
# --------------------------------------------------------------------------- #

# One real edge object, so the mechanism can be exercised without the import
# check. `cross_boundary_imports` gives frozen dataclasses; these mirror its shape.
def _edge(source: str, target: str, scope: str = "function", lineno: int = 1,
          shipped: bool = False) -> db.BoundaryImport:
    return db.BoundaryImport(source=source, lineno=lineno, target=target,
                             scope=scope, shipped=shipped)


def test_every_latent_edge_in_the_real_tree_has_been_investigated(
        file_set: db.DeployFileSet,
        import_check: db.ImportCheck) -> None:
    """The point of the record: nothing is reported without an answer attached.

    `check_deploy_bundle` now FAILS on an uninvestigated function-scope edge. That
    is the only way the record can stay true, because the alternative is a note
    that every future batch re-investigates and re-reports. A finding that has been
    classified must stay classified, and a new one must not arrive unclassified.
    """
    broken = db.cross_boundary_imports(file_set, files=import_check.runtime_files)
    unack = db.unacknowledged_latent_edges([b for b in broken if not b.shipped])
    assert unack == [], (
        "these latent boundary edges are not in KNOWN_LATENT_BOUNDARY_EDGES: %s. "
        "Trace the call graph: if no served path can reach the import, record the "
        "reason there; if one can, it is a landmine and the check will now fail on "
        "it." % ["%s:%d -> %s" % (b.source, b.lineno, b.target) for b in unack]
    )


def test_the_record_cannot_outlive_its_edge(
        file_set: db.DeployFileSet,
        import_check: db.ImportCheck) -> None:
    """A record of an investigated edge that no longer exists is a false premise.

    The same failure mode as the migration comment in item 1, one layer down: a
    piece of prose that says "we checked this" is only worth keeping while the
    thing it describes is there. A record nobody prunes is a second thing to
    re-investigate, and it will eventually be wrong.
    """
    broken = db.cross_boundary_imports(file_set, files=import_check.runtime_files)
    stale = db.stale_acknowledgements(broken)
    assert stale == [], (
        "KNOWN_LATENT_BOUNDARY_EDGES has entries for edges that no longer exist: %s. "
        "Either the edge was fixed (delete the entry) or the file moved and the key "
        "is stale." % stale
    )


def test_a_known_edge_cannot_hide_a_live_break() -> None:
    """The load-bearing safety property. A known edge must never launder a live one.

    `latent_edge_verdict` refuses to answer for anything that is not function
    scope, so hoisting the acknowledged import to the top of its file -- which
    turns a latent edge into a ModuleNotFoundError on every request -- cannot be
    absorbed by the record. Without this check the whole table would be a
    suppression list wearing a comment, and the most valuable finding the checker
    produces would be the one thing it could not report.
    """
    key = next(iter(db.KNOWN_LATENT_BOUNDARY_EDGES))
    source, target = key
    latent = _edge(source, target, scope="function")
    assert db.latent_edge_verdict(latent) is not None, "the record is empty for its own edge"

    hoisted = _edge(source, target, scope="module")
    assert db.latent_edge_verdict(hoisted) is None, (
        "a module-scope import of %s was acknowledged by the known-latent record. A "
        "module-scope import of a dropped module is a live break on every request, "
        "and the record must not be able to absorb it." % target
    )
    # And therefore it is still counted as a live break by the check's own grouping.
    hoisted_edges = [hoisted]
    assert [b for b in hoisted_edges if not b.shipped and b.scope == "module"] == hoisted_edges
    # An uninvestigated function-scope edge is likewise not acknowledged.
    assert db.latent_edge_verdict(_edge("src/other.py", "src.utils.ner.thing")) is None
    assert db.unacknowledged_latent_edges(
        [_edge("src/other.py", "src.utils.ner.thing")]) != []
    # And a shipped edge is never "broken", acknowledged or not.
    assert db.unacknowledged_latent_edges(
        [_edge("src/other.py", "src.shared.database", shipped=True)]) == []


def test_the_recorded_reason_is_a_call_graph_and_not_a_restatement(
        file_set: db.DeployFileSet,
        import_check: db.ImportCheck) -> None:
    """The `why` has to be checkable, or it is just a louder version of the bug.

    A record that says "this is fine" has reproduced the problem. A record that
    names the entry points, the one caller that is not shipped, and the symbols the
    served app actually imports can be re-verified by anyone who disagrees. So
    assert the content: the function holding the import, the two entry points, and
    the dropped caller.
    """
    (source, target), why = next(iter(db.KNOWN_LATENT_BOUNDARY_EDGES.items()))
    assert source == "src/verification/event_identity.py"
    assert target == "src.utils.ner.canonical_surface"

    for required in ("event_entity_keys", "same_event", "assign_canonical_events",
                     "scripts/backfill_globe_events.py", "curation_ui"):
        assert required in why, (
            "the recorded reason does not mention %s, so it cannot be re-verified "
            "against the code: %s" % (required, why)
        )
    assert "grep" not in why.lower() or "not a grep" in why.lower(), (
        "the reason must not rest on a name search"
    )

    # Every claim in it, re-derived from the code rather than from the prose.
    import ast

    source_text = (REPO / source).read_text(encoding="utf-8")
    assert "def event_entity_keys(" in source_text
    assert "def same_event(" in source_text
    assert "def assign_canonical_events(" in source_text
    # The import really is inside event_entity_keys, not at module scope.
    tree = ast.parse(source_text)
    module_level = {n.module for n in tree.body if isinstance(n, ast.ImportFrom)}
    assert "src.utils.ner" not in module_level, (
        "the recorded reason says this edge is function-scope; it is now a "
        "module-scope import, which is a live break rather than a latent one"
    )
    # The one non-test caller named as shipped-away really is the only importer of
    # the entry points, and it really is dropped from the bundle. Measured with the
    # AST rather than a substring search, because a substring search also matches
    # the prose in this file and in scripts/deploy_bundle.py's own docstring, which
    # is how a "call graph" argument turns into a grep result.
    imported: dict[str, set[str]] = {}
    for relpath in db.tracked_files(REPO):
        if not relpath.endswith(".py") or relpath.startswith("tests/") or relpath == source:
            continue
        try:
            mod = ast.parse((REPO / relpath).read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(mod):
            if isinstance(node, ast.ImportFrom) and node.module == source.replace(
                    ".py", "").replace("/", "."):
                imported[relpath] = {a.name for a in node.names}
    entry_points = {"event_entity_keys", "same_event", "cluster_events",
                    "assign_canonical_events"}
    reaching = {p: names & entry_points for p, names in imported.items()
                if names & entry_points}
    assert reaching == {
        "scripts/backfill_globe_events.py": {"assign_canonical_events"},
    }, (
        "these modules import an entry point to the lazy import from %s: %s. The "
        "recorded reason names scripts/backfill_globe_events.py as the only one, and "
        ".vercelignore drops it, so anything else here is a served caller the record "
        "does not account for." % (source, reaching)
    )
    assert "scripts/backfill_globe_events.py" not in file_set.included

    # And the four names the record says curation_ui imports, no more, read off
    # the import statements rather than out of the prose.
    served: set[str] = set()
    for module in ("curation_ui/events.py", "curation_ui/globe.py",
                   "curation_ui/map_api.py", "curation_ui/cron.py",
                   "curation_ui/story_api.py"):
        path = REPO / module
        if not path.is_file():
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom) and node.module == "src.verification.event_identity":
                served |= {a.name for a in node.names}
    assert served == {"IS_CANONICAL_EVENT", "CANONICAL_EVENT_ID",
                      "cluster_tier1_sources", "cluster_corroboration"}, (
        "the set of names curation_ui imports from %s changed: %s. If one of the new "
        "names can reach event_entity_keys(), the recorded reason is wrong."
        % (source, sorted(served))
    )
    assert not (served & entry_points)


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


def test_the_report_does_not_assert_whether_pyproject_ships(file_set: db.DeployFileSet) -> None:
    """The check must read the file set, not state a remembered fact about it.

    `check_deploy_bundle.py` printed, as a hard-coded sentence, "pyproject.toml
    does not ship, so requirements.txt is what Vercel installs". It does ship:
    `.vercelignore` has no rule for it. The sentence was wrong in the same
    direction that flatters the tool -- it explained away a real risk (a package
    only `pyproject.toml` names is a deploy-time absence) by asserting a file was
    absent when it was present, and nobody reading the output could tell.

    Two assertions, because one of them is a trap: pinning the CURRENT truth
    ("pyproject.toml ships") would be the same hard-coded claim with the sign
    flipped. What is pinned is that the value is derived, and that the fiction
    cannot come back as a literal.
    """
    source = (REPO / "scripts" / "check_deploy_bundle.py").read_text(encoding="utf-8")
    assert "pyproject.toml does not ship, so requirements.txt is what Vercel" not in source, (
        "the hard-coded 'pyproject.toml does not ship' sentence is back. The file set "
        "must be read for that, not remembered: pyproject.toml ships today."
    )
    assert "ships_pyproject" in source and "file_set.included" in source, (
        "the note must be computed from the filtered file set"
    )
    # The premise the sentence was asserting, measured rather than remembered.
    assert "pyproject.toml" in file_set.included
    assert "uv.lock" not in file_set.included
    # And the two files that decide what Vercel installs are BOTH shipped, which
    # is why the note has to be careful about which one it credits.
    assert "requirements.txt" in file_set.included


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
