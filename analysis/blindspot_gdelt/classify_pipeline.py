#!/usr/bin/env python3
"""Attribute pipeline articles to (country, topic family).

Country: map article entities' GPE names to FIPS 10-4 countries
  (fips10_4.json, sourced from Wikipedia's List of FIPS country codes).
  Most-frequent mapped GPE wins; articles with no mapped GPE are UNATTRIBUTED
  (attribution rate is reported, not hidden).

Topic family: priority-ordered keyword classifier on title + entity text,
  mapped to the same CAMEO-root families used for the GDELT side so the two
  distributions are directly comparable. Priority order is most-specific
  first; the first family with any keyword hit wins. This is a heuristic and
  is documented as such; a 25-article eyeball sample is printed for a
  precision sanity check.

Usage: python classify_pipeline.py
"""
import json
import os
import random
import re
import sys
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")

# ---------------------------------------------------------------- countries

FIPS = json.load(open(os.path.join(HERE, "fips10_4.json"), encoding="utf-8"))

# lowercase alias -> FIPS code, hand-verified against the FIPS table above.
ALIASES = {
    "usa": "US", "u.s.": "US", "u.s.a.": "US", "america": "US",
    "united states of america": "US",
    "uk": "UK", "u.k.": "UK", "britain": "UK", "great britain": "UK",
    "england": "UK", "scotland": "UK", "wales": "UK", "northern ireland": "UK",
    "uae": "TC", "u.a.e.": "TC",
    "south korea": "KS", "republic of korea": "KS", "rok": "KS",
    "korea, south": "KS",
    "north korea": "KN", "dprk": "KN", "korea, north": "KN",
    "democratic people's republic of korea": "KN",
    "russian federation": "RS",
    "prc": "CH",
    "islamic republic of iran": "IR",
    "czech republic": "EZ",
    "turkiye": "TU",
    "ivory coast": "IV", "cote divoire": "IV", "cote d'ivoire": "IV",
    "bosnia": "BK", "bosnia-herzegovina": "BK",
    "macedonia": "MK",
    "republic of the congo": "CF", "congo-brazzaville": "CF",
    "congo republic": "CF",
    "democratic republic of the congo": "CG", "drc": "CG",
    "congo-kinshasa": "CG", "dr congo": "CG",
    "eu": "EE", "european union": "EE",
    "vatican": "VT", "holy see": "VT",
    "eswatini": "WZ", "swaziland": "WZ",
    "myanmar": "BM",
    "east timor": "TT",
    "saudi arabia": "SA", "ksa": "SA", "kingdom of saudi arabia": "SA",
    "state of qatar": "QA", "state of kuwait": "KU",
    "sultanate of oman": "MU",
    "gaza": "GZ", "gaza strip": "GZ",
    "korea": "KS",  # bare "Korea" in news almost always means South Korea
}


def build_gpe_index():
    """Map normalized place-name string -> FIPS code."""
    idx = {}
    for code, name in FIPS.items():
        variants = {name.lower()}
        # "Korea, South" -> "south korea"; "Congo (Brazzaville)" handled by alias
        if "," in name:
            a, b = [p.strip() for p in name.split(",", 1)]
            variants.add(f"{b} {a}".lower())
        # strip parenthetical: "Congo (Brazzaville)" -> "congo"
        variants.add(re.sub(r"\s*\(.*?\)", "", name).strip().lower())
        variants.add(re.sub(r"^the\s+", "", name).strip().lower())
        for v in variants:
            if v and v not in idx:
                idx[v] = code
    for alias, code in ALIASES.items():
        idx[alias] = code
    return idx


GPE_INDEX = build_gpe_index()

# ---------------------------------------------------------------- families

# (family, keywords) in priority order: most specific first, first hit wins.
FAMILY_KEYWORDS = [
    ("PROTEST", ["protest", "demonstration", "rally", "march against", "sit-in",
                 "picket", "general strike", "uprising"]),
    ("ARMED_CONFLICT", ["airstrike", "air strike", "missile", "invasion",
                        "offensive", "ceasefire", "shelling", "battlefield",
                        "troops", "war in", "war,", "destroyed",
                        "devastation", "siege"]),
    ("MASS_VIOLENCE", ["terrorist", "terrorism", "massacre", "hostage",
                       "suicide attack", "suicide bombing"]),
    ("VIOLENCE", ["killed", "killing", "shot dead", "shooting", "stabbing",
                  "clash", "bombing", "explosion", "attack", "assault",
                  "dead", "wounded", "gunman"]),
    ("COERCION", ["sanction", "embargo", "military drill", "warship",
                  "no-fly", "deploy troops", "troop deployment"]),
    ("THREAT", ["threaten", "ultimatum", "warns of war", "threat of"]),
    ("INVESTIGATE", ["investigation", "probe", "inquiry", "arrest", "court",
                     "trial", "lawsuit", "indict", "sentence", "charged with",
                     "plead"]),
    ("AID", ["humanitarian aid", "relief effort", "aid package", "food aid",
              "disaster relief", "donation", " aid ", "humanitarian",
              "military aid", "aid worker", "foreign aid"]),
    ("DISAPPROVE", ["condemn", "criticize", "criticise", "denounce",
                    "rejects", "demands"]),
    ("COOPERATION", ["summit", "treaty", "signs deal", "trade deal",
                     "cooperation", "alliance", "partnership", "accord",
                     "minister", "ministers", "talks", "diplomat",
                     "embassy", "delegation", "bilateral", "envoy",
                     "foreign ministry", "state visit", "peace talks",
                     "negotiat"]),
    ("ECONOMY", ["economy", "market", "stock", "inflation", "gdp", "tariff",
                  "trade", "recession", "central bank", "interest rate",
                  "unemployment", "debt", "price", "prices", "nasdaq",
                  "dow jones"]),
    ("HEALTH", ["health", "virus", "vaccine", "disease", "hospital",
                "pandemic", "outbreak", "ebola", "measles"]),
    ("ENVIRONMENT", ["climate", "earthquake", "flood", "hurricane",
                     "wildfire", "forest fire", "bushfire", "disaster",
                     "drought", "storm", "tsunami",
                     "tornado", "landslide", "volcano", "magnitude",
                     "el nino", "la nina", "heatwave", "heat wave"]),
    ("TECHNOLOGY", ["artificial intelligence", "semiconductor", "quantum",
                    "cyber", "blockchain", "startup", "big tech"]),
    ("US_POLITICS", ["congress", "senate", "white house", "supreme court",
                     "democrat", "republican", "midterm", "campaign"]),
    ("SPORTS", ["championship", "olympic", "fifa", "world cup", "league",
                "tournament", "grand slam"]),
    ("CULTURE", ["film", "movie", "festival", " grammy", "oscar", "novel",
                 "exhibition"]),
    ("VERBAL", ["says", "said", "announces", "statement", "vows", "pledges",
                 "urges", "calls for", "warns", "hails"]),
]


SENSOR_DOMAINS = {"earthquake.usgs.gov", "gdacs.org"}


def classify_family(text: str, source_domain: str = ""):
    if source_domain in SENSOR_DOMAINS:
        return "ENVIRONMENT", "sensor_domain"
    t = " " + text.lower() + " "
    for family, kws in FAMILY_KEYWORDS:
        for kw in kws:
            if kw in t:
                return family, kw
    if re.search(r"\bm\s?\d+\.\d\b", t):  # USGS-style "M 1.8" magnitude title
        return "ENVIRONMENT", "magnitude_pattern"
    return "UNMATCHED", ""


US_STATE_CODES = {"ak", "hv", "nc", "ci", "pr", "tx", "nn", "us", "uw",
                  "av", "ok", "uu", "nm", "se", "ne"}


def attribute_country(article) -> tuple:
    """Return (fips_code_or_None, method).

    Priority: entities.GEO.country (already resolved by the pipeline's
    geocoder/GDELT join) -> GPE name mapping -> None. USGS rows carry US
    regional network codes (ak, hv, nc, ...) which are all United States.
    """
    ent = article.get("entities") or {}
    g = ent.get("GEO")
    if isinstance(g, dict) and g.get("country"):
        c = str(g["country"]).strip()
        if article.get("source_domain") == "earthquake.usgs.gov" or \
                c.lower() in US_STATE_CODES:
            return "US", "geo_usgs"
        if c.upper() in FIPS:
            return c.upper(), "geo_fips"
        code = GPE_INDEX.get(c.lower())
        if code:
            return code, "geo_name"
    gpes = ent.get("GPE") or []
    counts = Counter()
    for gp in gpes:
        name = str(gp).strip().lower()
        name = re.sub(r"[\u2019']s$", "", name)  # "China's"/"China\u2019s" -> "china"
        code = GPE_INDEX.get(name)
        if code:
            counts[code] += 1
    if counts:
        return counts.most_common(1)[0][0], "gpe"
    return None, "none"


def main() -> int:
    infile = os.path.join(DATA, "pipeline_articles.jsonl")
    articles = [json.loads(line) for line in open(infile, encoding="utf-8")]
    out_rows = []
    fam_counts = Counter()
    country_counts = Counter()
    unattributed = 0
    for a in articles:
        ent = a.get("entities") or {}
        text = (a.get("title") or "") + " " + " ".join(
            str(x) for k in ("PERSON", "ORG", "GPE") for x in (ent.get(k) or []))
        family, kw = classify_family(text, a.get("source_domain") or "")
        code, method = attribute_country(a)
        country = FIPS.get(code, code) if code else None
        if code is None:
            unattributed += 1
        else:
            country_counts[(code, country)] += 1
        fam_counts[family] += 1
        out_rows.append({
            "id": a["id"], "title": a.get("title"),
            "source_domain": a.get("source_domain"),
            "detected_language": a.get("detected_language"),
            "country_fips": code, "country": country,
            "family": family, "family_kw": kw,
        })
    with open(os.path.join(DATA, "pipeline_attributed.jsonl"), "w",
              encoding="utf-8") as f:
        for r in out_rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    n = len(articles)
    print(f"articles={n} country_attributed={n-unattributed} "
          f"({(n-unattributed)/n*100:.1f}%) unattributed={unattributed}")
    print("families:", dict(fam_counts.most_common()))
    print("top countries:", [(c, k[1], v) for k, v in
                             ((k, v) for k, v in country_counts.most_common(15))
                             for c in [k[0]]])

    # eyeball precision sample
    random.seed(20261002)
    print("\n--- eyeball sample (title | country | family) ---")
    for r in random.sample(out_rows, min(25, len(out_rows))):
        print(f"{(r['title'] or '')[:90]!r} | {r['country']} | {r['family']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
