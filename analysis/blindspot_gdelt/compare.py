#!/usr/bin/env python3
"""Compare GDELT event census vs pipeline article coverage.

Inputs (data/):
  gdelt_events.jsonl      deduped GDELT events for the 24h window
  pipeline_attributed.jsonl  pipeline articles with (country_fips, family)

Outputs (data/):
  compare_report.json  all rollups
  compare_tables.md    markdown tables for the human report

Country on the GDELT side is ActionGeo FIPS 10-4; on the pipeline side it is
the attributed FIPS from entities (GEO.country, else GPE mapping). Family on
both sides is the CAMEO-root family set (pipeline articles carry extra
non-CAMEO families: ECONOMY, HEALTH, ENVIRONMENT, TECHNOLOGY, US_POLITICS,
SPORTS, CULTURE, which are reported separately).
"""
import json
import os
import sys
from collections import Counter, defaultdict
from urllib.parse import urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
FIPS = json.load(open(os.path.join(HERE, "fips10_4.json"), encoding="utf-8"))

CAMEO_FAMILIES = ["VERBAL", "COOPERATION", "AID", "INVESTIGATE", "DISAPPROVE",
                  "THREAT", "PROTEST", "COERCION", "VIOLENCE",
                  "ARMED_CONFLICT", "MASS_VIOLENCE"]


def domain_of(url):
    try:
        h = (urlparse(url).hostname or "").lower()
        return h[4:] if h.startswith("www.") else h
    except Exception:
        return ""


def main() -> int:
    events = [json.loads(line) for line in
              open(os.path.join(DATA, "gdelt_events.jsonl"), encoding="utf-8")]
    arts = [json.loads(line) for line in
            open(os.path.join(DATA, "pipeline_attributed.jsonl"), encoding="utf-8")]

    n_events = len(events)
    no_geo = sum(1 for e in events if not e["geo_country_fips"])
    print(f"GDELT unique events={n_events} no_action_geo_country={no_geo} "
          f"({no_geo/n_events*100:.1f}%)")

    # ---- GDELT rollups (attention = sum of num_articles over deduped events)
    g_country_ev = Counter()
    g_country_att = Counter()
    g_family_ev = Counter()
    g_family_att = Counter()
    g_cell = defaultdict(list)  # (fips, family) -> events
    g_domains = Counter()
    for e in events:
        fips = e["geo_country_fips"] or "??"
        fam = e["family"]
        g_country_ev[fips] += 1
        g_country_att[fips] += e["num_articles"]
        g_family_ev[fam] += 1
        g_family_att[fam] += e["num_articles"]
        g_cell[(fips, fam)].append(e)
        d = domain_of(e["source_url"])
        if d:
            g_domains[d] += 1

    # ---- pipeline rollups (all articles, and news-only excluding sensors)
    SENSOR_DOMAINS = {"earthquake.usgs.gov", "gdacs.org"}
    p_country = Counter()
    p_family = Counter()
    p_cell = Counter()
    p_country_news = Counter()
    p_family_news = Counter()
    p_cell_news = Counter()
    n_news = 0
    p_langs = Counter()
    p_unattr_country = 0
    for a in open(os.path.join(DATA, "pipeline_articles.jsonl"), encoding="utf-8"):
        p_langs[json.loads(a).get("detected_language") or "null"] += 1
    for a in arts:
        fips = a["country_fips"] or "??"
        if a["country_fips"] is None:
            p_unattr_country += 1
        p_country[fips] += 1
        p_family[a["family"]] += 1
        p_cell[(fips, a["family"])] += 1
        if a["source_domain"] not in SENSOR_DOMAINS:
            n_news += 1
            p_country_news[fips] += 1
            p_family_news[a["family"]] += 1
            p_cell_news[(fips, a["family"])] += 1

    n_arts = len(arts)
    tot_g_att = sum(g_country_att.values())

    def cname(fips):
        return FIPS.get(fips, fips) if fips != "??" else "UNKNOWN_GEO"

    def country_table(p_c):
        rows = []
        denom = sum(p_c.values())
        for fips in set(list(g_country_att) + list(p_c)):
            ga = g_country_att.get(fips, 0)
            pa = p_c.get(fips, 0)
            gs = ga / tot_g_att if tot_g_att else 0
            ps = pa / denom if denom else 0
            rows.append({
                "fips": fips, "country": cname(fips),
                "gdelt_events": g_country_ev.get(fips, 0),
                "gdelt_attention": ga,
                "gdelt_attention_share": gs,
                "pipeline_articles": pa,
                "pipeline_share": ps,
                "coverage_ratio": (ps / gs) if gs > 0 else (float("inf") if ps > 0 else 0.0),
            })
        return sorted(rows, key=lambda r: -r["gdelt_attention"])

    def family_table(p_f):
        rows = []
        denom = sum(p_f.values())
        for fam in CAMEO_FAMILIES:
            ga = g_family_att.get(fam, 0)
            pa = p_f.get(fam, 0)
            gs = ga / tot_g_att if tot_g_att else 0
            ps = pa / denom if denom else 0
            rows.append({
                "family": fam,
                "gdelt_events": g_family_ev.get(fam, 0),
                "gdelt_attention": ga,
                "gdelt_attention_share": gs,
                "pipeline_articles": pa,
                "pipeline_share": ps,
                "coverage_ratio": (ps / gs) if gs > 0 else (float("inf") if ps > 0 else 0.0),
            })
        return sorted(rows, key=lambda r: -r["gdelt_attention"])

    # ---- country comparison table
    country_rows = country_table(p_country)
    country_rows_news = country_table(p_country_news)

    # ---- family comparison table (CAMEO families only for the head-to-head)
    family_rows = family_table(p_family)
    family_rows_news = family_table(p_family_news)

    # ---- zero-coverage cells (news-only pipeline cells: sensors can't cover news)
    zero_cells = []
    for (fips, fam), evs in g_cell.items():
        if fam not in CAMEO_FAMILIES:
            continue
        att = sum(e["num_articles"] for e in evs)
        pa = p_cell_news.get((fips, fam), 0)
        if pa == 0:
            evs_sorted = sorted(evs, key=lambda e: -e["num_articles"])
            zero_cells.append({
                "fips": fips, "country": cname(fips), "family": fam,
                "gdelt_events": len(evs), "gdelt_attention": att,
                "examples": [{
                    "event_code": e["event_code"],
                    "root_label": e["root_label"],
                    "geo_name": e["geo_name"],
                    "date_added": e["date_added"],
                    "num_articles": e["num_articles"],
                    "num_sources": e["num_sources"],
                    "source_domain": domain_of(e["source_url"]),
                } for e in evs_sorted[:3]],
            })
    zero_cells.sort(key=lambda r: -r["gdelt_attention"])
    zero_att = sum(c["gdelt_attention"] for c in zero_cells)

    report = {
        "n_gdelt_events": n_events,
        "n_gdelt_no_geo": no_geo,
        "n_pipeline_articles": n_arts,
        "n_pipeline_news_articles": n_news,
        "n_pipeline_unattributed_country": p_unattr_country,
        "total_gdelt_attention": tot_g_att,
        "zero_cell_attention": zero_att,
        "zero_cell_attention_share": zero_att / tot_g_att if tot_g_att else 0,
        "n_zero_cells": len(zero_cells),
        "countries": country_rows,
        "countries_news_only": country_rows_news,
        "families": family_rows,
        "families_news_only": family_rows_news,
        "pipeline_only_families": {f: p_family[f] for f in sorted(p_family)
                                   if f not in CAMEO_FAMILIES},
        "pipeline_only_families_news": {f: p_family_news[f] for f in sorted(p_family_news)
                                        if f not in CAMEO_FAMILIES},
        "pipeline_languages": dict(p_langs.most_common()),
        "gdelt_top_domains": g_domains.most_common(60),
        "zero_cells": zero_cells,
    }
    json.dump(report, open(os.path.join(DATA, "compare_report.json"), "w"),
              indent=1, ensure_ascii=False)

    # ---- markdown tables for the human report
    L = []
    L.append("# compare tables (generated)\n")
    L.append(f"GDELT unique events: {n_events} | pipeline articles: {n_arts} "
             f"(news-only excl. sensors: {n_news})\n")

    def country_md(rows, title):
        L.append(title)
        L.append("| country | FIPS | gdelt events | gdelt attention | att share | "
                 "pipeline arts | pipe share | coverage ratio |")
        L.append("|---|---|---|---|---|---|---|---|")
        for r in rows[:30]:
            cr = r["coverage_ratio"]
            crs = f"{cr:.2f}" if cr != float("inf") else "inf"
            L.append(f"| {r['country']} | {r['fips']} | {r['gdelt_events']} | "
                     f"{r['gdelt_attention']} | {r['gdelt_attention_share']*100:.2f}% | "
                     f"{r['pipeline_articles']} | {r['pipeline_share']*100:.2f}% | {crs} |")
        L.append("")

    def family_md(rows, title):
        L.append(title)
        L.append("| family | gdelt events | gdelt attention | att share | "
                 "pipeline arts | pipe share | coverage ratio |")
        L.append("|---|---|---|---|---|---|---|")
        for r in rows:
            cr = r["coverage_ratio"]
            crs = f"{cr:.2f}" if cr != float("inf") else "inf"
            L.append(f"| {r['family']} | {r['gdelt_events']} | {r['gdelt_attention']} | "
                     f"{r['gdelt_attention_share']*100:.2f}% | {r['pipeline_articles']} | "
                     f"{r['pipeline_share']*100:.2f}% | {crs} |")
        L.append("")

    country_md(report["countries"], "## countries by GDELT attention (all pipeline articles)")
    country_md(report["countries_news_only"], "## countries by GDELT attention (news-only, sensors excluded)")
    family_md(report["families"], "## CAMEO families (all pipeline articles)")
    family_md(report["families_news_only"], "## CAMEO families (news-only, sensors excluded)")
    L.append("\n## pipeline-only families (no CAMEO counterpart)")
    for f, c in report["pipeline_only_families"].items():
        L.append(f"- {f}: {c} articles ({c/n_arts*100:.1f}%)")
    L.append("\n## pipeline article languages")
    for lang, c in report["pipeline_languages"].items():
        L.append(f"- {lang}: {c} ({c/n_arts*100:.2f}%)")
    L.append("\n## GDELT top 60 primary-source domains (by deduped event count)")
    for d, c in report["gdelt_top_domains"]:
        L.append(f"- {d}: {c}")
    L.append(f"\n## zero-coverage cells: {len(zero_cells)} "
             f"({zero_att} attention = {zero_att/tot_g_att*100:.1f}% of total)")
    open(os.path.join(DATA, "compare_tables.md"), "w").write("\n".join(L) + "\n")
    print(f"wrote compare_report.json + compare_tables.md "
          f"({len(zero_cells)} zero cells)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
