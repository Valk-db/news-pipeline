"""Vercel bundle import guard.

Ensures no pipeline-only imports leak into the Vercel-deployed curation UI bundle.
Pure filesystem test - no app import, no DB, no fixtures.
"""
import ast
from pathlib import Path


FORBIDDEN_MODULES = {
    "numpy",
    "sentence_transformers",
    "spacy",
    "trafilatura",
    "bs4",
    "feedparser",
    "datasketch",
    "src.enrichment",
    "src.reliability",
    "src.verification",
    "src.ingestion",
}


def find_python_files(directory: Path) -> list[Path]:
    """Find all .py files in a directory."""
    return list(directory.rglob("*.py"))


def get_imported_modules(file_path: Path) -> set[str]:
    """Extract all imported module names from a Python file using AST."""
    try:
        content = file_path.read_text(encoding="utf-8")
        tree = ast.parse(content)
    except (SyntaxError, UnicodeDecodeError):
        return set()

    imports = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imports.add(node.module.split(".")[0])
    return imports


def test_vercel_bundle_imports():
    """Test that Vercel-deployed files don't import pipeline-only modules."""
    repo_root = Path(__file__).parent.parent

    # Files that go into the Vercel bundle:
    # - All .py under curation_ui/
    # - src/schema/models.py
    # - All .py directly in src/shared/
    check_paths = []

    curation_ui_dir = repo_root / "curation_ui"
    if curation_ui_dir.exists():
        check_paths.extend(find_python_files(curation_ui_dir))

    models_file = repo_root / "src" / "schema" / "models.py"
    if models_file.exists():
        check_paths.append(models_file)

    shared_dir = repo_root / "src" / "shared"
    if shared_dir.exists():
        check_paths.extend(shared_dir.glob("*.py"))

    violations = []
    for file_path in check_paths:
        imported = get_imported_modules(file_path)
        for forbidden in FORBIDDEN_MODULES:
            if forbidden in imported:
                violations.append(f"{file_path.relative_to(repo_root)}: {forbidden}")

    if violations:
        raise AssertionError(
            "Vercel bundle imports forbidden pipeline-only modules:\n"
            + "\n".join(violations)
        )