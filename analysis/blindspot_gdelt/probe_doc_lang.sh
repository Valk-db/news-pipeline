#!/bin/bash
# DOC 2.0 artlist language probes. Throttle discipline per src/ingestion/gdelt.py:
# GDELT asks for >=5s between requests; a body-apology throttle means stop and
# report. Four broad topic queries, 30s apart, bounded to the analysis window.
# Uses curl (proven through this VM's egress proxy).
OUTDIR="$(dirname "$0")/data"
for q in government market health climate; do
  safe="doc_artlist_${q}.json"
  echo "=== query=$q -> $safe"
  curl -sL --max-time 60 -A "news-pipeline/0.1 (blind-spot analysis)" \
    "https://api.gdeltproject.org/api/v2/doc/doc?query=${q}&mode=artlist&format=json&maxrecords=250&sort=datedesc&STARTDATETIME=20261001000000&ENDDATETIME=20261002235959" \
    -o "${OUTDIR}/${safe}" -w "http=%{http_code} bytes=%{size_download}\n"
  head -c 120 "${OUTDIR}/${safe}"; echo
  sleep 30
done
echo DONE
