"""The LLM roster: the one ordered list of every rung the fallback chain can try.

`LLMClient` used to carry the provider order three times over -- once in
`generate_caption`, once in `classify_relevance`, once in `chat_completion` -- as
`if self.groq_client: ... if self.cerebras_client: ...`. Adding a third provider to
that shape means editing the same if/else three times, and the three copies had already
drifted (Cerebras never went through the budget, and a 429 on one rung raised
"No LLM provider available" instead of trying the next one). The roster is the fix for
the drift, not just the duplication: a rung is a row here, and everything else -- the
walk, the budgets, the preflight, the staleness check -- reads this list.

Two things about a rung are worth stating plainly, because both were measured rather
than assumed:

**A ":free" model id is a perishable fact.** OpenRouter retires free tiers, and
`openai/gpt-oss-20b:free` -- the backup this batch was asked to wire up -- is gone from
the entire 466-model catalogue, not merely from the free subset. A hard-coded id is
therefore a silent 404 waiting to happen, so every free id lives here once, is
overridable from settings without a code change, and is checkable with
`scripts/check_free_models.py`. See `stale_free_ids`.

**Being in the catalogue does not mean being servable.** `google/gemma-4-26b-a4b-it:free`
is listed, but on 2026-10-03 every chat completion against it returned HTTP 429 with
`limit_source: upstream_provider_shared_pool` -- 3/3 attempts, while
`nvidia/nemotron-3-super-120b-a12b:free` answered 200 on the first try from the same
account, same minute. The free pool is shared across every OpenRouter user, so it
rate-limits on a schedule none of us control. That is why there are two free rungs and
not one, and why the chain demotes a rung that is locked out instead of re-paying for
it on every article (see `LLMClient._demote`).

Order note: Groq stays first. It is the only rung whose credential this pipeline is
known to hold in CI, and the backlog's proposal to make a free OpenRouter model the
*primary* is a decision-needed item, not a settled one -- see the report.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from typing import Any, Iterable

import httpx
from sqlalchemy import text

from src.shared.budget import (
    CEREBRAS_REQUESTS,
    GROQ_REQUESTS,
    OPENROUTER_GEMMA_REQUESTS,
    OPENROUTER_NEMOTRON_REQUESTS,
    today,
    token_counter_name,
    unpriced_calls_counter_name,
)
from src.shared.config import get_settings
from src.shared.database import _get_engine

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LLMRung:
    """One row of the roster: a model, how to reach it, and what it is allowed to spend.

    `key_attr`/`client_attr` are attribute *names*, not values, and that is the whole
    trick behind testability. The tests build a bare `LLMClient()` and assign
    `client.groq_client = mock` without configuring any key; the walk asks
    `getattr(self, rung.client_attr)` at call time, so an injected mock is a live rung
    and an unconfigured setting is not. Reading the key instead would have made the
    existing fallback tests unreachable.
    """

    name: str
    """Stable id, also the ingest-stats source key (`STATS.record(rung.name, ...)`)."""

    method: str
    """Which transport: "groq", "cerebras", or "openrouter"."""

    key_attr: str
    """Settings field holding the credential. The preflight asks about this, not the client,
    because a preflight that probes a client someone injected in a test is a preflight
    that reports on the wrong thing."""

    client_attr: str
    """LLMClient attribute that must be truthy for this rung to be live."""

    budget_name: str
    """`budget_counters.name` row. Per rung, never per provider: see budget.py."""

    model_attr: str
    """Settings field that overrides the model id. Empty means "use `model` below"."""

    model: str = ""
    """The pinned model id. For a free tier this is the value the staleness check watches."""

    daily_cap_attr: str = ""
    minute_cap_attr: str = ""

    daily_token_cap_attr: str = ""
    """Settings field holding this rung's daily allowance in TOKENS.

    Separate from `daily_cap_attr` because the provider enforces two different
    limits with two different numbers, and capping only one of them is not a
    budget. Groq's free tier for openai/gpt-oss-20b publishes RPD 1,000 and TPD
    200,000 (https://console.groq.com/docs/rate-limits, checked 2026-10-03, with
    the TPD figure confirmed by a live 429 body rather than trusted from the
    docs): at 1,000 requests/day and a measured 1,742-4,962 tokens per call,
    requests run out first on paper and tokens run out first in practice. Which
    one binds is a measurement, not an assumption -- on 2026-10-03 dev's
    `groq_requests` read 28 while the day's 200,000 tokens were fully spent.
    """

    free_tier: bool = False

    label: str = ""
    """Human name for logs and annotations. Defaults to `name`."""


# Order is the whole point of this list, so it is written out once, in one place, in
# order. The rationale per rung:
#
#   groq          the only rung with a credential this pipeline is known to hold in CI
#   openrouter:...  free tiers, one rung per model so their outages stay independent
#   cerebras      paid/free secondary, kept last so its behaviour is unchanged for
#                 anyone relying on the old two-rung order
ROSTER: tuple[LLMRung, ...] = (
    LLMRung(
        name="groq",
        method="groq",
        key_attr="groq_api_key",
        client_attr="groq_client",
        budget_name=GROQ_REQUESTS,
        model_attr="groq_model",
        model="openai/gpt-oss-20b",
        daily_cap_attr="groq_daily_request_budget",
        daily_token_cap_attr="groq_daily_token_cap",
        minute_cap_attr="groq_requests_per_minute",
        label="Groq",
    ),
    LLMRung(
        name="openrouter:gemma",
        method="openrouter",
        key_attr="openrouter_api_key",
        client_attr="_openrouter_key",
        budget_name=OPENROUTER_GEMMA_REQUESTS,
        model_attr="openrouter_gemma_model",
        model="google/gemma-4-26b-a4b-it:free",
        daily_cap_attr="openrouter_gemma_daily_request_budget",
        daily_token_cap_attr="openrouter_gemma_daily_token_cap",
        minute_cap_attr="openrouter_requests_per_minute",
        free_tier=True,
        label="OpenRouter gemma",
    ),
    LLMRung(
        name="openrouter:nemotron",
        method="openrouter",
        key_attr="openrouter_api_key",
        client_attr="_openrouter_key",
        budget_name=OPENROUTER_NEMOTRON_REQUESTS,
        model_attr="openrouter_nemotron_model",
        # Substituted for openai/gpt-oss-20b:free, which is retired: absent from the
        # whole catalogue. Chosen because it is the largest free model that accepts
        # `response_format` (which classify_relevance sends) and is not a reasoning
        # model, which at small max_tokens spends the budget on reasoning tokens and
        # returns empty content -- the failure the groq-translate batch already hit.
        model="nvidia/nemotron-3-super-120b-a12b:free",
        daily_cap_attr="openrouter_nemotron_daily_request_budget",
        daily_token_cap_attr="openrouter_nemotron_daily_token_cap",
        minute_cap_attr="openrouter_requests_per_minute",
        free_tier=True,
        label="OpenRouter nemotron",
    ),
    LLMRung(
        name="cerebras",
        method="cerebras",
        key_attr="cerebras_api_key",
        client_attr="cerebras_client",
        budget_name=CEREBRAS_REQUESTS,
        model_attr="cerebras_model",
        model="gpt-oss-120b",
        daily_cap_attr="cerebras_daily_request_budget",
        daily_token_cap_attr="cerebras_daily_token_cap",
        minute_cap_attr="cerebras_requests_per_minute",
        label="Cerebras",
    ),
)


def _setting(settings: Any, attr: str) -> Any:
    """A settings value, or None if it is absent or the wrong type.

    The type check is not paranoia about the schema -- it is about the preflight tests,
    which pass a MagicMock as the settings object. A MagicMock invents a truthy
    attribute for any name you ask it, so a plain `getattr` would report every
    OpenRouter rung as configured in a run that configured none of them.
    """
    if not attr:
        return None
    value = getattr(settings, attr, None)
    return value if value is not None and type(value) in (str, int, float) else None


def model_for(rung: LLMRung, settings: Any) -> str:
    """The model id this rung will actually call: the setting if set, else the pinned id."""
    override = _setting(settings, rung.model_attr)
    if isinstance(override, str) and override.strip():
        return override.strip()
    return rung.model


def configured_free_ids(settings: Any) -> list[str]:
    """Every free model id the roster would call right now, after settings overrides."""
    return [model_for(r, settings) for r in ROSTER if r.free_tier]


def stale_free_ids(configured: Iterable[str], listed: Iterable[str]) -> list[str]:
    """The configured free ids that the catalogue no longer offers, in roster order.

    A rename or a retirement is the failure mode this exists for. Without it the only
    symptom is a 404 on every request to that rung, which -- with a fallback chain --
    looks exactly like "the free pool is busy today", so the rung gets demoted for the
    run and the real cause is never seen.
    """
    available = set(listed)
    return [model_id for model_id in configured if model_id not in available]


async def fetch_free_model_ids(
    *,
    api_key: str,
    base_url: str = "https://openrouter.ai/api/v1",
    timeout: float = 30.0,
) -> set[str]:
    """Every `:free` model the catalogue currently lists.

    Reads the whole catalogue rather than trusting a filtered query parameter, because
    the interesting failure is a model that has silently *left* the free tier -- a
    filtered list cannot show you a model that is no longer in it.
    """
    async with httpx.AsyncClient(timeout=timeout) as http:
        response = await http.get(
            f"{base_url.rstrip('/')}/models",
            headers={"Authorization": f"Bearer {api_key}"},
        )
    response.raise_for_status()
    return {m["id"] for m in response.json().get("data", []) if m.get("id")}


@dataclass(frozen=True)
class RosterCheck:
    ok: bool
    configured: tuple[str, ...]
    listed: int
    stale: tuple[str, ...]
    error: str = ""

    def render(self) -> str:
        lines = [f"Free-model roster: {len(self.configured)} configured, {self.listed} listed"]
        if self.error:
            lines.append(f"  CHECK FAILED: {self.error}")
        for model_id in self.configured:
            mark = "STALE" if model_id in self.stale else "ok"
            lines.append(f"  {model_id:<44} {mark}")
        if self.stale:
            lines.append(
                "  Stale ids are retired or renamed upstream. Override the id in settings"
                " (e.g. OPENROUTER_GEMMA_MODEL) or pick a replacement in llm_roster.ROSTER."
            )
        return "\n".join(lines)


async def check_free_model_roster(settings: Any) -> RosterCheck:
    """Compare the roster's free ids against the live catalogue.

    Only meaningful when an OpenRouter key is configured; without one there is nothing
    to check and `ok` is True, so an unkeyed deployment does not get a red mark for a
    problem it does not have.
    """
    configured = tuple(configured_free_ids(settings))
    api_key = _setting(settings, "openrouter_api_key")
    if not api_key:
        return RosterCheck(ok=True, configured=configured, listed=0, stale=(), error="")

    try:
        listed = await fetch_free_model_ids(
            api_key=api_key,
            base_url=str(_setting(settings, "openrouter_base_url") or "https://openrouter.ai/api/v1"),
            timeout=float(_setting(settings, "openrouter_timeout") or 30.0),
        )
    except Exception as exc:  # noqa: BLE001 - a check that cannot run is not a pass
        return RosterCheck(
            ok=False,
            configured=configured,
            listed=0,
            stale=(),
            error=f"catalogue unreachable ({type(exc).__name__}: {exc})",
        )

    stale = tuple(stale_free_ids(configured, listed))
    return RosterCheck(ok=not stale, configured=configured, listed=len(listed), stale=stale)


def live_rungs(client: Any) -> list[LLMRung]:
    """The roster, in order, narrowed to rungs this client can actually call.

    Read at call time rather than cached, because "live" is a property of the client's
    attributes and those change: the fallback tests assign `client.groq_client` after
    construction, and a key added to the environment later should not require a restart
    of a long-lived process to be seen.
    """
    return [r for r in ROSTER if getattr(client, r.client_attr, None)]


def configured_rungs(settings: Any) -> list[LLMRung]:
    """The roster, narrowed to rungs whose credential is set. The preflight's view.

    Deliberately not `live_rungs`: preflight exists to answer "is this key usable", and
    answering that from an object someone injected by hand would be answering a
    different question.
    """
    out: list[LLMRung] = []
    for rung in ROSTER:
        key = _setting(settings, rung.key_attr)
        if isinstance(key, str) and key.strip():
            out.append(rung)
    return out


def roster_summary(settings: Any) -> str:
    """One line per rung, for a log line that shows the order actually in force."""
    lines = []
    for rung in ROSTER:
        cap = _setting(settings, rung.daily_cap_attr)
        tcap = _setting(settings, rung.daily_token_cap_attr)
        rpm = _setting(settings, rung.minute_cap_attr)
        lines.append(
            f"  {rung.name:<22} {model_for(rung, settings):<44}"
            f" budget={rung.budget_name} cap={cap} rpm={rpm} token_budget={token_counter_name(rung.budget_name)} token_cap={tcap}"
        )
    return "\n".join(lines)


# --------------------------------------------------------------------- daily cost

# Every row for the day, not a name filter. budget_counters holds a handful of rows
# per day, and a `name LIKE` filter here would be wrong twice over: LIKE has no
# alternation (so a `%a|%b` pattern is a literal), and `_` in a LIKE pattern is a
# single-character wildcard, so `%_requests` also matches `groqXrequests`. The
# interesting part of this query is which rows are NOT the roster's, so the
# selection belongs in Python where it can be read.
_COST_QUERY = text("SELECT name, used FROM budget_counters WHERE day = :day")


@dataclass(frozen=True)
class RungCost:
    """What one rung cost on one day. `None` means the counter could not be read."""

    rung: str
    requests: int | None
    request_cap: int
    tokens: int | None
    token_cap: int
    unpriced_calls: int | None

    @property
    def spend_is_lower_bound(self) -> bool:
        """True when part of this rung's cost is not in the token figure.

        A day's token count with unpriced calls on it is a floor, not a total.
        Reporting it as a total is how a cap ends up looking respected while the
        real spend is unknown.
        """
        return bool(self.unpriced_calls)


@dataclass(frozen=True)
class DailyRosterCost:
    day: str
    rungs: tuple[RungCost, ...]
    unknown_counters: tuple[str, ...]
    """Counter names present for this day that belong to no rung in the roster."""

    def render(self) -> str:
        head = f"Roster cost for {self.day}"
        if self.unknown_counters:
            head += f" (also on this day, not a roster rung: {', '.join(self.unknown_counters)})"
        lines = [head]
        for rung in self.rungs:
            lines.append(
                f"  {rung.rung:<22} requests {_fmt(rung.requests):>10} / {rung.request_cap:<8}"
                f" tokens {_fmt(rung.tokens):>10} / {rung.token_cap:<8}"
            )
            if rung.unpriced_calls:
                # Printed, not summarised: a token figure that is a lower bound has
                # to announce itself wherever it is read.
                lines.append(
                    f"  {'':<22} ^ {rung.unpriced_calls} call(s) reported no usable usage;"
                    f" the token figure is a LOWER BOUND, not the real cost"
                )
        return "\n".join(lines)


def _fmt(value: int | None) -> str:
    return "unreadable" if value is None else f"{value:,}"


async def daily_roster_cost(day: date | None = None, settings: Any = None) -> DailyRosterCost:
    """What the roster cost on one day: tokens, requests, per rung.

    `budget_counters` already answers this, so this is a query and not a schema
    change and not a reporting module: one SELECT over the day's rows, folded
    against ROSTER in Python. Everything the answer needs is already a column --
    the reason a per-rung breakdown is possible at all is that batch-freemodel
    stopped sharing one request row between rungs.

    Unreadable counters are reported as None and never as 0. 0 would answer "this
    rung spent nothing", which is a claim about a provider this function did not
    reach; None answers "we do not know", which is what a failed query means.

    `unknown_counters` is the converse check: rows on the day that belong to no
    rung here (translation, Phase 2, MyMemory) are named rather than silently
    dropped, so a reader asking "what did the roster cost" cannot mistake "the
    roster cost nothing" for "the roster is not the only thing that spent".
    """
    settings = settings if settings is not None else get_settings()
    target = day or today()
    engine = _get_engine()
    rows: dict[str, int] = {}
    readable = True
    try:
        # engine is None -> AttributeError -> caught below as unreadable, which is
        # the answer an unconfigured database deserves. Not an exception the caller
        # has to distinguish from "the day cost nothing".
        async with engine.connect() as conn:
            for name, used_amount in (await conn.execute(
                _COST_QUERY, {"day": target}
            )).all():
                rows[str(name)] = int(used_amount)
    except Exception as exc:  # noqa: BLE001 - every counter becomes unreadable, not zero
        logger.warning("daily_roster_cost unreadable: %s", type(exc).__name__)
        rows, readable = {}, False

    def read(name: str) -> int | None:
        # A row that does not exist for the day is a real 0 -- nothing was spent
        # against that counter. A counter that could not be READ is None. Folding
        # those two together is the mistake this function exists to avoid: a
        # failed query would otherwise report a confident, entirely fictional
        # "the roster spent 0 tokens today".
        return rows.get(name, 0) if readable else None

    rungs = tuple(
        RungCost(
            rung=rung.name,
            requests=read(rung.budget_name),
            request_cap=int(_setting(settings, rung.daily_cap_attr) or 0),
            tokens=read(token_counter_name(rung.budget_name)),
            token_cap=int(_setting(settings, rung.daily_token_cap_attr) or 0),
            unpriced_calls=read(unpriced_calls_counter_name(token_counter_name(rung.budget_name))),
        )
        for rung in ROSTER
    )
    mine = {r.budget_name for r in ROSTER}
    mine |= {token_counter_name(r.budget_name) for r in ROSTER}
    mine |= {unpriced_calls_counter_name(token_counter_name(r.budget_name)) for r in ROSTER}
    return DailyRosterCost(
        day=target.isoformat(),
        rungs=rungs,
        unknown_counters=tuple(sorted(n for n in rows if n not in mine)),
    )
