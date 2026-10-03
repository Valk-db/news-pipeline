"""The RLS backfill migration's stated reason for NOT forcing RLS was wrong.

`20261002220000_row_level_security_backfill.sql` justified omitting
`FORCE ROW LEVEL SECURITY` by claiming the application connects as
`service_role` "rather than as the owner". Measured on dev, the connecting role
is `postgres`, which is BOTH the owner and BYPASSRLS. The conclusion (do not
force) was right and the reasoning was not, which is the dangerous shape: a
comment that reaches the right answer for the wrong reason is one edit away from
justifying the opposite, and nobody re-measures a comment.

A comment is exactly as rot-prone as code and this one had already rotted, so
the reasoning is pinned the way a threshold would be. Both halves are asserted:

  * the corrected reason is present and names BYPASSRLS as what makes FORCE
    inert, and says the connecting role is the owner;
  * the retracted claim is gone, in the form it took.

The numbers in the comment were measured on dev (`news-pipeline-dev`) through the
local pg tunnel, not copied from anywhere:

    rolname                 rolbypassrls
    postgres                true
    service_role            true
    anon                    false
    authenticated           false
    transparency_signer     false

    public tables: 35 total, 35 with RLS enabled, 0 forced
    owners:       postgres owns all 35
    policies:     6, all naming transparency_signer, 0 naming anon/authenticated
    (rls, forced, policy_count): (true,false,0) x31, (true,false,1) x2, (true,false,2) x2

This file asserts on the migration TEXT, on purpose. It cannot assert on the dev
catalogue -- a test that needs a database is a test that gets skipped in the one
CI job that would otherwise notice -- but it CAN notice the claim regressing,
which is the failure this file exists to prevent.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
MIGRATION = (
    REPO_ROOT / "supabase" / "migrations"
    / "20261002220000_row_level_security_backfill.sql"
)


def _text() -> str:
    assert MIGRATION.is_file(), f"expected {MIGRATION}"
    return MIGRATION.read_text(encoding="utf-8")


def _comments() -> str:
    """Just the comment block, so an assertion cannot be satisfied by SQL text."""
    lines = [ln for ln in _text().splitlines() if ln.lstrip().startswith("--")]
    return "\n".join(ln.lstrip()[2:].strip() for ln in lines)


class TestTheCorrectedReasonIsStated:
    def test_it_names_bypassrls_as_the_thing_that_makes_force_inert(self) -> None:
        text = _comments().lower()
        assert "force" in text, "the migration no longer discusses FORCE at all"
        assert "bypassrls" in text, (
            "the reason FORCE is not applied must name BYPASSRLS, since that is what "
            "the dev catalogue actually shows"
        )
        # BYPASSRLS outranks FORCE. Saying only "the role has BYPASSRLS" would leave
        # the original gap open: someone could still read FORCE as a partial fix.
        assert re.search(r"bypassrls.{0,120}(outrank|both enable and force)", text), (
            "the comment must say BYPASSRLS outranks ENABLE and FORCE, not merely "
            "that the role has it"
        )

    def test_it_says_the_connecting_role_is_the_owner(self) -> None:
        """The retracted claim was "connects as service_role, NOT as the owner"."""
        text = _comments()
        assert "postgres" in text, (
            "the comment must name the role the app actually connects as, which is "
            "postgres, not service_role"
        )
        assert re.search(r"postgres.{0,160}owner", text, re.IGNORECASE), (
            "the comment must state that postgres is the table owner; that is the "
            "half of the old claim that was wrong"
        )

    def test_it_attributes_the_blanket_deny_to_enable_with_no_policies(self) -> None:
        """The deny comes from ENABLE + zero policies, not from FORCE and not from
        any policy existing."""
        text = _comments().lower()
        assert "no policy" in text or "zero policies" in text, (
            "the comment must say the blanket deny is RLS with no policy"
        )
        assert "transparency_signer" in text, (
            "the four policies that do exist are signer-scoped; naming that is what "
            "distinguishes 'deny by having no policy' from 'deny by having no "
            "permissive anon policy'"
        )


class TestTheRetractedClaimIsGone:
    def test_service_role_is_not_described_as_the_connecting_role(self) -> None:
        """The specific false sentence, in every shape it was written in."""
        text = _comments()
        for pattern in (
            r"connects as\s+service_role",
            r"reaches this database as service_role",
            r"only ever reaches this database as\s+`?service_role",
            r"not as the owner",
        ):
            assert not re.search(pattern, text, re.IGNORECASE), (
                f"the retracted claim is back in the comment as /{pattern}/. The "
                "connecting role is postgres, which IS the owner and also carries "
                "BYPASSRLS."
            )

    def test_service_role_is_not_mentioned_as_a_premise_at_all(self) -> None:
        """Stronger than the sentence check: the premise may not survive as a clause.

        `service_role` is allowed to appear exactly once, in the sentence that
        records that the old reasoning was wrong, so a reader who greps for the
        role is pointed at the retraction rather than at the premise.
        """
        text = _comments()
        assert text.count("service_role") <= 1, (
            "service_role appears %d times in the migration comments; it is a real "
            "BYPASSRLS role but it is not the one the application connects as, and "
            "repeating it as a premise is how the wrong claim came back"
            % text.count("service_role")
        )


class TestTheMigrationItselfIsUnchanged:
    def test_the_statement_block_is_untouched(self) -> None:
        """The brief for this fix was comment-only. Prove the SQL did not move."""
        statements = [ln for ln in _text().splitlines()
                      if ln.strip() and not ln.lstrip().startswith("--")]
        body = "\n".join(statements)
        assert "ENABLE ROW LEVEL SECURITY" in body, "the migration lost its statement"
        assert "FORCE ROW LEVEL SECURITY" not in body, (
            "FORCE ROW LEVEL SECURITY appeared in the executable body. This fix was "
            "scoped to the comment and enabling FORCE is a schema change, not a fix"
        )
        assert "ALTER TABLE %I ENABLE ROW LEVEL SECURITY" in body, (
            "the loop's ALTER statement was rewritten; the class-level loop is the "
            "part that has to stay"
        )
