# What-If Simulation Results

**Snapshot Header**
- DB: Production PostgreSQL (DATABASE_URL from env)
- ReportingUnit count: 2,930
- RawArticle count: 2,941
- Max ReportingUnit.created_at: 2026-09-29 03:00:24.012825+00:00
- Embedding model: sentence-transformers/all-MiniLM-L6-v2 (local)
- Commit SHA: f98c541 (main)
- Simulation script: scripts/what_if_sim.py (commit a4ec4f5)

---

## Top-N Analysis (Jaccard ≥ 0.4 merge rule)

### top_n_entities = 3
- Tier-1 units: 1,662
- Cross-owner pairs at TF-IDF cos ≥ 0.80: 67
- Cross-owner pairs at TF-IDF cos ≥ 0.90: 25
- Pairs already merged at Jaccard ≥ 0.4: 72
- Newly merged pairs (Jaccard ≥ 0.4): 1,261
- False merge risk (embedding cos < 0.6): 1,232 (97.7%)
- Union-growth flips: 70

**Embedding Cosine Histogram (0.1 bins) for Newly-Merged Pairs:**
| Bin | Count | Bar |
|-----|-------|-----|
| [0.0-0.1) | 750 | ######################################## |
| [0.1-0.2) | 228 | ######################################## |
| [0.2-0.3) | 123 | ######################### |
| [0.3-0.4) | 66 | ############## |
| [0.4-0.5) | 50 | ########### |
| [0.5-0.6) | 15 | #### |
| [0.6-0.7) | 12 | ### |
| [0.7-0.8) | 7 | ## |
| [0.8-0.9) | 8 | ## |
| [0.9-1.0) | 1 | # |

### top_n_entities = 5
- Tier-1 units: 1,662
- Cross-owner pairs at TF-IDF cos ≥ 0.80: 67
- Cross-owner pairs at TF-IDF cos ≥ 0.90: 25
- Pairs already merged at Jaccard ≥ 0.4: 86
- Newly merged pairs (Jaccard ≥ 0.4): 1,039
- False merge risk (embedding cos < 0.6): 1,008 (97.0%)
- Union-growth flips: 62

**Embedding Cosine Histogram (0.1 bins) for Newly-Merged Pairs:**
| Bin | Count | Bar |
|-----|-------|-----|
| [0.0-0.1) | 495 | ######################################## |
| [0.1-0.2) | 240 | ######################################## |
| [0.2-0.3) | 137 | ############################ |
| [0.3-0.4) | 74 | ############### |
| [0.4-0.5) | 39 | ######## |
| [0.5-0.6) | 23 | ##### |
| [0.6-0.7) | 9 | ## |
| [0.7-0.8) | 14 | ### |
| [0.8-0.9) | 7 | ## |
| [0.9-1.0) | 1 | # |

### top_n_entities = 6
- Tier-1 units: 1,662
- Cross-owner pairs at TF-IDF cos ≥ 0.80: 67
- Cross-owner pairs at TF-IDF cos ≥ 0.90: 25
- Pairs already merged at Jaccard ≥ 0.4: 78
- Newly merged pairs (Jaccard ≥ 0.4): 442
- False merge risk (embedding cos < 0.6): 427 (96.6%)
- Union-growth flips: 73

**Embedding Cosine Histogram (0.1 bins) for Newly-Merged Pairs:**
| Bin | Count | Bar |
|-----|-------|-----|
| [0.0-0.1) | 231 | ######################################## |
| [0.1-0.2) | 92 | ######################################## |
| [0.2-0.3) | 48 | ############################ |
| [0.3-0.4) | 29 | ############### |
| [0.4-0.5) | 15 | ######## |
| [0.5-0.6) | 12 | #### |
| [0.6-0.7) | 8 | ## |
| [0.7-0.8) | 5 | # |
| [0.8-0.9) | 11 | ### |
| [0.9-1.0) | 1 | # |

---

## Why Cross cos≥0.80 is Top-N Independent

The TF-IDF cosine matrix (`cos_sim`) is computed **once** from article titles (line 74-77) before the top_n loop. The top_n parameter only affects the entity sets used for Jaccard similarity (line 56-68), not the TF-IDF vectors. Therefore `cross_owner_cos_80` and `cross_owner_cos_90` are identical across all three top_n values.

---

## Jaccard Threshold Sweep at top_n=3

| Jaccard ≥ | Newly Merged | Already Merged | cos≥0.80 | cos≥0.90 | False Merge Risk (cos<0.6) |
|-----------|--------------|----------------|----------|----------|----------------------------|
| 0.3 | 1,435 | 72 | 10 | 2 | 1,405 (97.9%) |
| 0.25 | 2,099 | 72 | 10 | 2 | 2,063 (98.3%) |
| 0.2 | 25,094 | 152 | 24 | 7 | 24,992 (99.6%) |

---

## Determinism Verification

Back-to-back runs on the same DB with no ingest between produced **identical outputs** (verified by diff).