# Data Sources License Risk Ledger

This file tracks the commercial-use licensing status of every ingestion source in the news-pipeline. Modeled on `gods-eye-view`'s `DATA_SOURCES.md` — see `GRAND_PLAN.md` §1c for why this matters given the "sell access later" plan.

**Purpose**: Catch a noncommercial-use restriction on an existing or new source *before* it's load-bearing for a paying customer, not after.

---

| Source | Used for | License / terms | Commercial-use risk | Attribution required | Cache/rate policy |
|--------|----------|-----------------|---------------------|---------------------|-------------------|
| **bbc.com** | Tier-1 RSS ingestion (world, UK, politics feeds) | BBC Terms of Use: RSS feeds for "personal, non-commercial use only" | **noncommercial — flag** | Yes (BBC branding) | RSS fetch every run; no auth |
| **theguardian.com** | Tier-1 RSS ingestion (world, UK, politics feeds) | Guardian Open Platform: free tier for non-commercial; commercial requires paid license | **noncommercial — flag** | Yes ("The Guardian") | RSS fetch every run; no auth |
| **npr.org** | Tier-1 RSS ingestion (news, world, politics feeds) | NPR Terms of Use: "non-commercial use only" for RSS feeds | **noncommercial — flag** | Yes ("NPR") | RSS fetch every run; no auth |
| **dw.com** | Tier-1 RSS ingestion (all, Europe feeds) | DW Terms: "free of charge for private, non-commercial use" | **noncommercial — flag** | Yes ("DW") | RSS fetch every run; no auth |
| **france24.com** | Tier-1 RSS ingestion (English feed) | France 24: "strictly personal and non-commercial use" | **noncommercial — flag** | Yes ("France 24") | RSS fetch every run; no auth |
| **aljazeera.com** | Tier-1 RSS ingestion (all.xml feed) | Al Jazeera Terms: "personal, non-commercial use only" | **noncommercial — flag** | Yes ("Al Jazeera") | RSS fetch every run; no auth |
| **euronews.com** | Tier-1 RSS ingestion (world theme feed) | Euronews Terms: "personal, non-commercial use" | **noncommercial — flag** | Yes ("Euronews") | RSS fetch every run; feed returns 404 |
| **pbs.org** | Tier-1 RSS ingestion (NewsHour feed) | PBS Terms: "personal, non-commercial use only" | **noncommercial — flag** | Yes ("PBS NewsHour") | RSS fetch every run; feed returns 404 |
| **apnews.com** | Tier-1 RSS ingestion (world, politics hubs) | AP Terms: commercial use requires paid license; RSS not officially provided | **noncommercial — flag** | Yes ("Associated Press") | RSS disabled (no working feed) |
| **reuters.com** | Tier-1 RSS ingestion (world, politics feeds) | Reuters Terms: commercial use requires license; RSS is HTML not RSS | **noncommercial — flag** | Yes ("Reuters") | RSS disabled (no working feed) |
| **nytimes.com** | Tier-2 RSS ingestion (world, politics) | NYT Terms: "personal, non-commercial use only" | **noncommercial — flag** | Yes ("The New York Times") | RSS fetch every run; no auth |
| **washingtonpost.com** | Tier-2 RSS ingestion (world, politics) | WaPo Terms: "personal, non-commercial use only" | **noncommercial — flag** | Yes ("The Washington Post") | RSS fetch every run; no auth |
| **wsj.com** | Tier-2 RSS ingestion (world, markets) | WSJ Terms: subscriber-only; commercial use requires license | **noncommercial — flag** | Yes ("The Wall Street Journal") | RSS fetch every run; paywalled |
| **ft.com** | Tier-2 RSS ingestion (UK, world) | FT Terms: "personal, non-commercial use only" | **noncommercial — flag** | Yes ("Financial Times") | RSS fetch every run; 403 on world |
| **economist.com** | Tier-2 RSS ingestion (international) | Economist Terms: "personal, non-commercial use only" | **noncommercial — flag** | Yes ("The Economist") | RSS fetch every run; no auth |
| **foreignpolicy.com** | Tier-2 RSS ingestion (main feed) | FP Terms: "personal, non-commercial use" | **noncommercial — flag** | Yes ("Foreign Policy") | RSS fetch every run; no auth |
| **foreignaffairs.com** | Tier-2 RSS ingestion (main feed) | FA Terms: "personal, non-commercial use" | **noncommercial — flag** | Yes ("Foreign Affairs") | RSS fetch every run; no auth |
| **csis.org** | Tier-2 RSS ingestion (main feed) | CSIS: "non-commercial use permitted with attribution" | **attribution-only** | Yes ("CSIS") | RSS fetch every run; no auth |
| **who.int** | Tier-2 RSS ingestion (news English) | WHO: CC BY-NC-SA 3.0 IGO (non-commercial) | **noncommercial — flag** | Yes ("WHO") | RSS fetch every run; no auth |
| **latimes.com** | Tier-2 RSS ingestion (world-nation) | LA Times Terms: "personal, non-commercial use only" | **noncommercial — flag** | Yes ("Los Angeles Times") | RSS fetch every run; 403 |
| **chicagotribune.com** | Tier-2 RSS ingestion (nation-world) | Tribune Terms: "personal, non-commercial use only" | **noncommercial — flag** | Yes ("Chicago Tribune") | RSS fetch every run; no auth |
| **bostonglobe.com** | Tier-2 RSS ingestion (nation) | Boston Globe Terms: "personal, non-commercial use only" | **noncommercial — flag** | Yes ("The Boston Globe") | RSS fetch every run; 404 |
| **reddit.com** | Tier-3 ingestion (public RSS from subreddits) | Reddit User Agreement: commercial use requires approval; public RSS not officially supported | **noncommercial — flag** | Yes ("Reddit") | RSS fetch with 3s delay; 403 blocked |
| **bsky.social** | Tier-3 source (configured, not ingested) | Bluesky: AT Protocol; commercial terms unclear | **unclear — needs review** | TBD | Not yet ingested |
| **substack.com** | Tier-4 source (configured, not ingested) | Substack: varies by newsletter; no blanket terms | **unclear — needs review** | TBD | Not yet ingested |
| **GDELT** (gdeltproject.org) | GDELT DOC API ingestion (bbc.com, theguardian.com, npr.org domains) | GDELT Terms: "commercial use permitted with citation" | **none** | Yes (cite GDELT) | API rate limited; circuit breaker at 3 failures |

---

### Summary of Risk Flags

**Noncommercial — flag (21 sources)**: All tier-1 and tier-2 RSS sources except CSIS carry explicit non-commercial restrictions in their terms of use. This is a significant risk for the "sell access later" plan — these sources would need to be replaced or licensed commercially before monetization.

**Attribution-only (1 source)**: CSIS permits non-commercial use with attribution — still a restriction, but less severe.

**Unclear — needs review (2 sources)**: Bluesky and Substack lack clear commercial terms for programmatic access.

**None (1 source)**: GDELT explicitly permits commercial use with citation.

---

### Action Required

Before any paid product launch, the following must be addressed:

1. **Replace non-commercial tier-1/2 sources** with commercially-licensed alternatives (e.g., paid wire services, licensed aggregators)
2. **Negotiate commercial licenses** for critical sources (AP, Reuters, BBC, Guardian, NPR)
3. **Verify Bluesky/Substack terms** before enabling those adapters
4. **Document all license negotiations** in this file with dates and terms