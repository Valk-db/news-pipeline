#!/usr/bin/env python3
"""Language mix of GDELT's primary event sources vs the pipeline.

GDELT side: top-60 primary-source domains (by deduped event count) mapped to
their primary publishing language in DOMAIN_LANG below. A domain counts as
non-English when its primary edition is not English; multilingual outlets are
listed separately. This is a LOWER BOUND on non-English share: unmapped
long-tail domains are excluded from the denominator.

Pipeline side: detected_language distribution from raw_articles (measured).

The DOC 2.0 artlist `language` field would have been the direct instrument,
but api.gdeltproject.org served HTTP 429 + the plain-text throttle apology
("Please limit requests to one every 5 seconds...") on 2026-10-02 ~17:15 and
~18:45 UTC despite >=30s spacing, so no DOC language sample was taken.
"""
import json
import os
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")

# domain -> primary publishing language ("multi" = substantial non-English edition)
DOMAIN_LANG = {
    "dailymail.com": "en", "riotimesonline.com": "en", "nypost.com": "en",
    "aol.co.uk": "en", "timesofindia.indiatimes.com": "en",
    "economictimes.indiatimes.com": "en", "hindustantimes.com": "en",
    "indiankanoon.org": "en", "thehindu.com": "en", "allafrica.com": "en",
    "manilatimes.net": "en", "aninews.in": "en", "arabnews.com": "en",
    "punchng.com": "en", "bignewsnetwork.com": "en", "prokerala.com": "en",
    "moneycontrol.com": "en", "miragenews.com": "en", "ibtimes.co.uk": "en",
    "aol.com": "en", "aa.com.tr": "tr",
    "winnipegfreepress.com": "en", "cbc.ca": "en", "aljazeera.com": "en",
    "middleeasteye.net": "en", "trend.az": "en", "newsroomamerica.com": "en",
    "jpost.com": "en", "cubaheadlines.com": "en", "freepressjournal.in": "en",
    "theguardian.com": "en", "thenews.com.pk": "en",
    "theepochtimes.com": "en", "tribune.com.pk": "en", "thesun.ng": "en",
    "kyivpost.com": "en", "leadership.ng": "en", "abc.net.au": "en",
    "vanguardngr.com": "en", "jns.org": "en", "standardmedia.co.ke": "en",
    "tribuneonlineng.com": "en", "irishtimes.com": "en",
    "ynetnews.com": "en", "dunyanews.tv": "multi",
    "news.az": "en", "the-star.co.ke": "en", "thenationalnews.com": "en",
    "breitbart.com": "en", "en.apa.az": "en", "foxnews.com": "en",
    "blueprint.ng": "en", "newsweek.com": "en",
    "dominicanrepublicpost.com": "en", "news.webindia123.com": "en",
    "thestar.com.my": "en", "israelnationalnews.com": "en",
    "mirror.co.uk": "en", "thenationonlineng.net": "en",
    "philstar.com": "en",
}


def main() -> int:
    r = json.load(open(os.path.join(DATA, "compare_report.json")))
    top = r["gdelt_top_domains"]
    unmapped = [d for d, _ in top if d not in DOMAIN_LANG]
    assert not unmapped, f"unmapped domains: {unmapped}"

    lang_ev = Counter()
    mapped_events = 0
    for d, c in top:
        lang = DOMAIN_LANG[d]
        lang_ev[lang] += c
        mapped_events += c
    total_ev = r["n_gdelt_events"]
    print(f"top-60 domains cover {mapped_events}/{total_ev} events "
          f"({mapped_events/total_ev*100:.1f}%)")
    for lang, c in lang_ev.most_common():
        print(f"  {lang}: {c} events ({c/mapped_events*100:.2f}% of mapped)")

    non_en = sum(c for lang, c in lang_ev.items() if lang != "en")
    print(f"non-English primary-source share (lower bound): "
          f"{non_en}/{mapped_events} = {non_en/mapped_events*100:.2f}%")

    plangs = r["pipeline_languages"]
    n_pipe = r["n_pipeline_articles"]
    non_en_pipe = sum(c for lang, c in plangs.items() if lang != "en")
    print(f"pipeline non-English: {non_en_pipe}/{n_pipe} = "
          f"{non_en_pipe/n_pipe*100:.2f}%")

    json.dump({
        "gdelt_top60_mapped_events": mapped_events,
        "gdelt_top60_total_events": total_ev,
        "gdelt_lang_share": {k: v / mapped_events for k, v in lang_ev.items()},
        "gdelt_non_en_share_lower_bound": non_en / mapped_events,
        "pipeline_non_en_share": non_en_pipe / n_pipe,
        "domain_lang_table": DOMAIN_LANG,
        "doc_api_note": "DOC 2.0 artlist throttled (HTTP 429 + body apology) "
                        "2026-10-02 ~17:15 and ~18:45 UTC despite >=30s spacing; "
                        "no DOC language sample taken.",
    }, open(os.path.join(DATA, "language_report.json"), "w"), indent=1)
    print("wrote language_report.json")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
