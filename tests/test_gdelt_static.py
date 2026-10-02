"""Tests for GDELT 2.0 static file parsing.

All fixtures are tiny hand written TSV rows in the real column layout, so the
tests run offline and never touch the network.
"""

import os
import zipfile

import pytest

from src.ingestion import gdelt_static
from src.ingestion.gdelt_static import (
    build_event_index,
    clean_gkg_title,
    event_geo,
    parse_gkg_file,
    parse_gkg_locations,
    parse_gkg_tone,
    parse_lastupdate,
)


# --------------------------------------------------------------- fixtures

# A 61 column export row. Column order follows the verified map:
# 0 id, 1 sqldate, 25 is_root, 26 event_code, 27 base, 28 root, 29 quad,
# 30 goldstein, 31 mentions, 32 sources, 33 articles, 34 tone,
# 36 action_geo_name, 37 action_geo_country, 40 lat, 41 lon,
# 44 actor1_name, 48 lat, 49 lon, 52 actor2_name, 56 lat, 57 lon,
# 59 date_added, 60 source_url
def make_events_row(**overrides):
    row = [""] * 61
    row[0] = "7001"
    row[1] = "20260930"
    row[25] = "1"
    row[26] = "0420"
    row[27] = "042"
    row[28] = "04"
    row[29] = "1"
    row[30] = "-3.2"
    row[31] = "12"
    row[32] = "5"
    row[33] = "9"
    row[34] = "-1.75"
    row[36] = "Kyiv"
    row[37] = "UP"
    row[40] = "50.4501"
    row[41] = "30.5234"
    row[44] = "Russia"
    row[48] = "55.7558"
    row[49] = "37.6173"
    row[52] = "Ukraine"
    row[56] = "49.0"
    row[57] = "31.0"
    row[59] = "20260930120000"
    row[60] = "https://example.com/story-one"
    for idx, value in overrides.items():
        row[int(idx.lstrip("c"))] = value
    return "\t".join(row)


def make_gkg_row(url="https://example.com/story-one", locations=None, title="<PAGE_TITLE>Story one headline</PAGE_TITLE>"):
    row = [""] * 27
    row[0] = "20260930110000"
    row[1] = "20260930110000"
    row[2] = "1"
    row[3] = "example.com"
    row[4] = url
    row[7] = "THEME_GENERAL_GOVERNMENT;THEME_CONFLICT"
    row[9] = locations if locations is not None else (
        "3#Kyiv#UP#UA#50.45#30.52#1234"
    )
    row[11] = "Volodymyr Zelenskyy"
    row[13] = "Ministry of Defence"
    row[15] = "-2.5,1.0,0.5"
    row[26] = title
    return "\t".join(row)


def write_zip(tmp_path, name, text):
    path = os.path.join(tmp_path, name)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(name.replace(".zip", ""), text)
    return path


# ------------------------------------------------------------ lastupdate


class TestParseLastupdate:
    def test_extracts_timestamps_newest_first(self):
        text = (
            "size (bytes) md5\n"
            "1234 abc 20260930114500.export.CSV.zip\n"
            "1235 abc 20260930113000.export.CSV.zip\n"
            "1236 abc 20260930114500.gkg.csv.zip\n"
        )
        stamps = parse_lastupdate(text)
        assert stamps == ["20260930114500", "20260930113000"]

    def test_ignores_header_and_non_zip_lines(self):
        text = "size (bytes) md5\nnotatimestamp.export.CSV.zip\n"
        assert parse_lastupdate(text) == []

    def test_empty_text(self):
        assert parse_lastupdate("") == []


# ------------------------------------------------------------ title/loc


class TestGkgFieldParsing:
    def test_clean_title_strips_page_title_tags(self):
        assert clean_gkg_title("<PAGE_TITLE>Real headline</PAGE_TITLE>") == "Real headline"

    def test_clean_title_handles_empty(self):
        assert clean_gkg_title("") == ""

    def test_clean_title_collapses_whitespace(self):
        assert clean_gkg_title("<PAGE_TITLE>a   b\n c</PAGE_TITLE>") == "a b c"

    def test_parse_locations_keeps_numeric_coords(self):
        out = parse_gkg_locations("3#Washington, District of Columbia#US#USDC#38.8951#-77.0364#531871")
        assert out == [{
            "name": "Washington, District of Columbia",
            "country": "US",
            "lat": 38.8951,
            "lon": -77.0364,
        }]

    def test_parse_locations_skips_entries_without_numbers(self):
        out = parse_gkg_locations("3#Somewhere##X#N/A#N/A#0;;3#Kyiv#UP#UA#50.45#30.52#1")
        assert len(out) == 1
        assert out[0]["name"] == "Kyiv"

    def test_parse_tone_averages(self):
        assert parse_gkg_tone("-2.5,1.0,0.5") == pytest.approx(-1.0 / 3.0)

    def test_parse_tone_empty_is_none(self):
        assert parse_gkg_tone("") is None
        assert parse_gkg_tone("garbage") is None


# ------------------------------------------------------------ event geo


class TestEventGeo:
    def test_prefers_action_geo(self):
        geo, label = event_geo(make_events_row().split("\t"))
        assert geo["name"] == "Kyiv"
        assert geo["lat"] == pytest.approx(50.4501)
        assert label == "action_geo"

    def test_falls_back_to_actor1_geo(self):
        row = make_events_row(c40="", c41="").split("\t")
        geo, label = event_geo(row)
        assert geo["name"] == "Russia"
        assert label == "actor1_geo"

    def test_falls_back_to_actor2_geo(self):
        row = make_events_row(c40="", c41="", c48="", c49="").split("\t")
        geo, label = event_geo(row)
        assert geo["name"] == "Ukraine"
        assert label == "actor2_geo"

    def test_out_of_range_lat_is_rejected(self):
        row = make_events_row(c40="999").split("\t")
        geo, label = event_geo(row)
        # ActionGeo is unusable so it falls through to Actor1Geo
        assert label == "actor1_geo"

    def test_no_geo_at_all(self):
        row = make_events_row(c40="", c41="", c48="", c49="", c56="", c57="").split("\t")
        geo, label = event_geo(row)
        assert geo is None
        assert label == ""


# ------------------------------------------------------------ full parse


class TestBuildEventIndex:
    def test_indexes_row_by_source_url(self, tmp_path):
        text = "\n".join([make_events_row()]) + "\n"
        path = write_zip(tmp_path, "20260930114500.export.CSV.zip", text)

        index, rows, no_geo = build_event_index(path)

        assert rows == 1
        entry = index["https://example.com/story-one"]
        assert entry["event_code"] == "0420"
        assert entry["avg_tone"] == pytest.approx(-1.75)
        assert entry["num_articles"] == 9
        assert entry["geo"]["name"] == "Kyiv"
        assert entry["date_added"].year == 2026

    def test_short_rows_are_skipped(self, tmp_path):
        text = "too\tshort\trow\n" + make_events_row() + "\n"
        path = write_zip(tmp_path, "20260930114500.export.CSV.zip", text)
        _, rows, _ = build_event_index(path)
        assert rows == 1


class TestParseGkgFile:
    def test_parses_article_with_event_join(self, tmp_path):
        events = write_zip(tmp_path, "20260930114500.export.CSV.zip", make_events_row() + "\n")
        index, _, _ = build_event_index(events)

        gkg = write_zip(tmp_path, "20260930114500.gkg.csv.zip", make_gkg_row() + "\n")
        articles, rows, joined, skipped = parse_gkg_file(gkg, index)

        assert rows == 1
        assert joined == 1
        assert skipped == 0
        assert len(articles) == 1
        art = articles[0]
        assert art.title == "Story one headline"
        assert art.source_domain == "example.com"
        assert art.published_at.year == 2026
        geo = art.entities["GEO"]
        # Event geography wins over GKG V1LOCATIONS when both are present.
        assert geo["lat"] == pytest.approx(50.4501)
        assert geo["lon"] == pytest.approx(30.5234)
        assert geo["source"] == "action_geo"
        assert art.url_hash

    def test_works_without_event_join(self, tmp_path):
        gkg = write_zip(tmp_path, "20260930114500.gkg.csv.zip", make_gkg_row() + "\n")
        articles, _, joined, _ = parse_gkg_file(gkg, {})
        assert joined == 0
        assert len(articles) == 1
        # GKG V1LOCATIONS supplies the geometry
        assert articles[0].entities["GEO"]["source"] == "gkg_v1locations"

    def test_row_without_any_geo_is_skipped_and_counted(self, tmp_path):
        gkg = write_zip(
            tmp_path,
            "20260930114500.gkg.csv.zip",
            make_gkg_row(locations="3#Nowhere##X#N/A#N/A#0") + "\n",
        )
        articles, _, _, skipped = parse_gkg_file(gkg, {})
        assert articles == []
        assert skipped == 1

    def test_row_without_title_is_skipped(self, tmp_path):
        gkg = write_zip(
            tmp_path,
            "20260930114500.gkg.csv.zip",
            make_gkg_row(title="") + "\n",
        )
        articles, rows, _, _ = parse_gkg_file(gkg, {})
        assert articles == []
        assert rows == 1

    def test_non_http_document_identifier_is_skipped(self, tmp_path):
        gkg = write_zip(
            tmp_path,
            "20260930114500.gkg.csv.zip",
            make_gkg_row(url="not-a-url") + "\n",
        )
        articles, _, _, _ = parse_gkg_file(gkg, {})
        assert articles == []

    def test_duplicate_urls_collapse(self, tmp_path):
        text = make_gkg_row() + "\n" + make_gkg_row() + "\n"
        gkg = write_zip(tmp_path, "20260930114500.gkg.csv.zip", text)
        articles, rows, _, _ = parse_gkg_file(gkg, {})
        assert rows == 2
        assert len(articles) == 1

    def test_known_url_hashes_are_filtered(self, tmp_path):
        gkg = write_zip(tmp_path, "20260930114500.gkg.csv.zip", make_gkg_row() + "\n")
        from src.utils.trafilatura_extract import compute_url_hash
        known = {compute_url_hash("https://example.com/story-one")}
        articles, _, _, _ = parse_gkg_file(gkg, {}, known_url_hashes=known)
        assert articles == []

    def test_max_articles_caps_output(self, tmp_path):
        rows = "\n".join(
            make_gkg_row(url=f"https://example.com/story-{i}") for i in range(5)
        )
        gkg = write_zip(tmp_path, "20260930114500.gkg.csv.zip", rows + "\n")
        articles, rows_seen, _, _ = parse_gkg_file(gkg, {}, max_articles=2)
        assert rows_seen == 5
        assert len(articles) == 2


class TestBodyText:
    def test_body_mentions_theme_person_org_and_location(self, tmp_path):
        gkg = write_zip(tmp_path, "20260930114500.gkg.csv.zip", make_gkg_row() + "\n")
        articles, _, _, _ = parse_gkg_file(gkg, {})
        body = articles[0].body_text
        assert "THEME_CONFLICT" in body
        assert "Volodymyr Zelenskyy" in body
        assert "Ministry of Defence" in body
        assert "Kyiv" in body


class TestZipErrors:
    def test_non_zip_raises(self, tmp_path):
        path = os.path.join(tmp_path, "bad.zip")
        with open(path, "w") as f:
            f.write("not a zip")
        with pytest.raises(zipfile.BadZipFile):
            list(gdelt_static.iter_tsv_rows(path))
