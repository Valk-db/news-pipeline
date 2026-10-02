"""Parse FIPS 10-4 table from Wikipedia wikitext into code->name JSON.

Source: https://en.wikipedia.org/wiki/List_of_FIPS_country_codes (fetched 2026-10-02).
Wikitext rows are two lines:
  | {{mono|AA}}
  | ''{{flag|Aruba|size=40px}}''
"""
import json
import re

lines = open("/tmp/fips_raw.txt", encoding="utf-8").read().splitlines()
rows = {}
i = 0
while i < len(lines):
    m = re.match(r"^\|\s*\{\{mono\|([A-Z]{2})\}\}", lines[i].strip())
    if m and i + 1 < len(lines):
        code = m.group(1)
        nxt = lines[i + 1].strip()
        n2 = re.match(r"^\|\s*(.*)$", nxt)
        if n2:
            name = n2.group(1)
            fm = re.search(r"\{\{flag\|([^}|]+)", name)
            if fm:
                name = fm.group(1)
            else:
                fm2 = re.search(r"\{\{flagicon\|([^}|]+)", name)
                if fm2:
                    name = fm2.group(1)
            m_ital = re.search(r"''\[\[([^\]|]+)", n2.group(1))
            if m_ital and "flagicon" in n2.group(1):
                name = m_ital.group(1)
            name = re.sub(r"<ref.*", "", name).replace("''", "").strip()
            # strip any leftover template braces
            name = re.sub(r"\{\{|\}\}", "", name).strip()
            rows[code] = name
            i += 2
            continue
    i += 1
print(f"parsed {len(rows)} codes")
checks = {"US":{"United States"},"UP":{"Ukraine"},"RS":{"Russia"},
          "CH":{"People's Republic of China","China"},"UK":{"United Kingdom"},
          "IS":{"Israel"},"IR":{"Iran"},"NI":{"Nigeria"},"SF":{"South Africa"},
          "BR":{"Brazil"},"IN":{"India"},"PK":{"Pakistan"},
          "TU":{"Turkey","T\u00fcrkiye"},"EZ":{"Czechia","Czech Republic"},
          "BM":{"Burma","Myanmar"},"TC":{"United Arab Emirates"},"MU":{"Oman"},
          "AE":{"United Arab Emirates"}}
for c, wants in checks.items():
    got = rows.get(c)
    assert got in wants, f"{c}: got {got!r} want one of {wants!r}"
print("sanity checks passed")
json.dump(rows, open("/home/hatch/workspace/wt-blindspot/analysis/blindspot_gdelt/fips10_4.json","w"),
          indent=1, sort_keys=True)
print("wrote fips10_4.json")
