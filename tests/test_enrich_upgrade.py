"""Tests for metadata enrichment and preprint upgrade features."""

import json
from unittest.mock import MagicMock, patch

import pytest

from zotero_mcp import ads_client
from zotero_mcp.citation_import import csl_json_to_zotero
from zotero_mcp.tools.write import (
    _ads_doc_to_enrich_fields,
    _find_published_version,
    _parse_bibcode_from_extra,
    _title_similarity,
    _upgrade_single_preprint,
    _enrich_single_item,
)


# --------------------------------------------------------------------------- #
# ads_client.doc_to_csl_json — bibstem + pubdate extraction
# --------------------------------------------------------------------------- #

class TestDocToCslJsonBibstemPubdate:
    def test_bibstem_mapped_to_container_title_short(self):
        doc = {
            "bibcode": "2023ApJ...945L..15A",
            "title": "Test Title",
            "bibstem": "ApJ",
            "pub": "The Astrophysical Journal",
        }
        csl = ads_client.doc_to_csl_json(doc)
        assert csl["container-title"] == "The Astrophysical Journal"
        assert csl["container-title-short"] == "ApJ"

    def test_pubdate_full_precision_over_year(self):
        doc = {
            "bibcode": "2023ApJ...945L..15A",
            "title": "Test",
            "year": "2023",
            "pubdate": "2023-02-15",
        }
        csl = ads_client.doc_to_csl_json(doc)
        # date-parts should be [2023, 2, 15]
        assert csl["issued"]["date-parts"] == [[2023, 2, 15]]

    def test_pubdate_strips_trailing_zero_day(self):
        doc = {
            "bibcode": "2023ApJ...945L..15A",
            "title": "Test",
            "pubdate": "2023-02-00",
        }
        csl = ads_client.doc_to_csl_json(doc)
        # -00 day stripped → [2023, 2]
        assert csl["issued"]["date-parts"] == [[2023, 2]]

    def test_year_only_when_no_pubdate(self):
        doc = {
            "bibcode": "2023ApJ...945L..15A",
            "title": "Test",
            "year": "2023",
        }
        csl = ads_client.doc_to_csl_json(doc)
        assert csl["issued"]["date-parts"] == [[2023]]

    def test_no_bibstem_no_container_title_short(self):
        doc = {"bibcode": "2023ApJ...945L..15A", "title": "Test"}
        csl = ads_client.doc_to_csl_json(doc)
        assert "container-title-short" not in csl


# --------------------------------------------------------------------------- #
# citation_import.csl_json_to_zotero — journalAbbreviation mapping
# --------------------------------------------------------------------------- #

class TestCslJournalAbbreviation:
    # Realistic journalArticle template with all writable fields.
    _JA_TEMPLATE = {
        "itemType": "journalArticle",
        "title": "",
        "publicationTitle": "",
        "journalAbbreviation": "",
        "volume": "",
        "issue": "",
        "pages": "",
        "date": "",
        "DOI": "",
        "abstractNote": "",
        "url": "",
        "language": "",
        "ISSN": "",
        "shortTitle": "",
        "creators": [],
    }

    def test_container_title_short_maps_to_journal_abbreviation(self):
        csl = {
            "type": "article-journal",
            "title": "Test Paper",
            "container-title": "Monthly Notices of the Royal Astronomical Society",
            "container-title-short": "MNRAS",
        }
        item = csl_json_to_zotero(csl, lambda t: dict(self._JA_TEMPLATE))
        assert item["itemType"] == "journalArticle"
        assert item["publicationTitle"] == "Monthly Notices of the Royal Astronomical Society"
        assert item["journalAbbreviation"] == "MNRAS"

    def test_no_container_title_short_leaves_field_empty(self):
        csl = {
            "type": "article-journal",
            "title": "Test",
            "container-title": "Nature",
        }
        item = csl_json_to_zotero(csl, lambda t: dict(self._JA_TEMPLATE))
        assert item.get("journalAbbreviation", "") == ""

    def test_book_does_not_get_journal_abbreviation(self):
        csl = {
            "type": "book",
            "title": "My Book",
            "container-title-short": "MB",
        }
        book_template = {"itemType": "book", "title": "", "publisher": ""}
        item = csl_json_to_zotero(csl, lambda t: dict(book_template))
        assert "journalAbbreviation" not in item or item.get("journalAbbreviation") == ""


# --------------------------------------------------------------------------- #
# Helper functions
# --------------------------------------------------------------------------- #

class TestParseBibcodeFromExtra:
    def test_extracts_bibcode(self):
        extra = "arXiv:2401.12345\nbibcode: 2024ApJ...961L..10X"
        assert _parse_bibcode_from_extra(extra) == "2024ApJ...961L..10X"

    def test_returns_none_when_no_bibcode(self):
        assert _parse_bibcode_from_extra("arXiv:2401.12345") is None

    def test_returns_none_for_empty(self):
        assert _parse_bibcode_from_extra(None) is None
        assert _parse_bibcode_from_extra("") is None


class TestTitleSimilarity:
    def test_identical_titles(self):
        assert _title_similarity("A B C", "A B C") == 1.0

    def test_completely_different(self):
        assert _title_similarity("A B C", "X Y Z") == 0.0

    def test_partial_overlap(self):
        sim = _title_similarity("A B C D", "A B E F")
        assert 0.0 < sim < 1.0


# --------------------------------------------------------------------------- #
# _ads_doc_to_enrich_fields
# --------------------------------------------------------------------------- #

class TestAdsDocToEnrichFields:
    def test_extracts_date_and_abbreviation(self):
        doc = {"pubdate": "2023-05-15", "bibstem": "ApJ", "year": "2023"}
        result = _ads_doc_to_enrich_fields(doc, {"date", "journal_abbreviation"})
        assert result["date"] == "2023-05-15"
        assert result["journal_abbreviation"] == "ApJ"

    def test_only_date_when_no_bibstem(self):
        doc = {"pubdate": "2023-05-15", "year": "2023"}
        result = _ads_doc_to_enrich_fields(doc, {"date", "journal_abbreviation"})
        assert "date" in result
        assert "journal_abbreviation" not in result

    def test_strips_zero_day(self):
        doc = {"pubdate": "2023-05-00", "bibstem": "MNRAS"}
        result = _ads_doc_to_enrich_fields(doc, {"date", "journal_abbreviation"})
        assert result["date"] == "2023-05"

    def test_falls_back_to_year(self):
        doc = {"year": "2022", "bibstem": "A&A"}
        result = _ads_doc_to_enrich_fields(doc, {"date", "journal_abbreviation"})
        assert result["date"] == "2022"


# --------------------------------------------------------------------------- #
# _enrich_single_item
# --------------------------------------------------------------------------- #

class TestEnrichSingleItem:
    def test_skips_when_fields_already_present(self):
        write_zot = MagicMock()
        write_zot.item.return_value = {
            "key": "ABC12345",
            "data": {
                "key": "ABC12345",
                "itemType": "journalArticle",
                "title": "Test",
                "date": "2023",
                "journalAbbreviation": "ApJ",
                "DOI": "10.1088/0004-637X/762/1/36",
                "extra": "",
            },
        }
        result = _enrich_single_item(write_zot, "ABC12345", {"date", "journal_abbreviation"}, force=False)
        assert result["status"] == "skipped_existing"
        write_zot.update_item.assert_not_called()

    def test_enriches_missing_fields_from_ads(self):
        write_zot = MagicMock()
        write_zot.item.return_value = {
            "key": "ABC12345",
            "data": {
                "key": "ABC12345",
                "itemType": "journalArticle",
                "title": "Test Paper",
                "date": "",
                "journalAbbreviation": "",
                "DOI": "10.1088/0004-637X/762/1/36",
                "extra": "",
            },
        }
        with patch("zotero_mcp.tools.write._ads_client") as mock_ads:
            mock_ads._FULL_FIELDS = ads_client._FULL_FIELDS
            mock_ads.fetch_record.return_value = None
            mock_ads.search.return_value = [{
                "pubdate": "2013-01-10",
                "bibstem": "ApJ",
                "year": "2013",
                "pub": "The Astrophysical Journal",
            }]
            result = _enrich_single_item(write_zot, "ABC12345",
                                         {"date", "journal_abbreviation"}, force=False)
        assert result["status"] == "enriched"
        assert "date" in result["filled"]
        assert "journalAbbreviation" in result["filled"]
        write_zot.update_item.assert_called_once()
        # Check the patched data has the right fields.
        patched = write_zot.update_item.call_args[0][0]
        assert patched["date"] == "2013-01-10"
        assert patched["journalAbbreviation"] == "ApJ"

    def test_no_identifier_returns_not_found(self):
        write_zot = MagicMock()
        write_zot.item.return_value = {
            "key": "XYZ12345",
            "data": {
                "key": "XYZ12345",
                "itemType": "journalArticle",
                "title": "No ID",
                "date": "",
                "journalAbbreviation": "",
                "DOI": "",
                "extra": "",
            },
        }
        result = _enrich_single_item(write_zot, "XYZ12345", {"date", "journal_abbreviation"}, force=False)
        assert result["status"] == "not_found"


# --------------------------------------------------------------------------- #
# _find_published_version
# --------------------------------------------------------------------------- #

class TestFindPublishedVersion:
    def test_returns_none_for_already_article(self):
        doc = {"doctype": "article", "pub": "ApJ", "title": "Published Paper"}
        assert _find_published_version(doc) is None

    def test_finds_published_version_by_title(self):
        eprint_doc = {
            "doctype": "eprint",
            "title": "Discovery of a New Pulsar in Globular Cluster",
            "bibcode": "2024arXiv240112345A",
        }
        published_doc = {
            "doctype": "article",
            "pub": "The Astrophysical Journal",
            "title": "Discovery of a New Pulsar in Globular Cluster",
            "bibstem": "ApJ",
            "pubdate": "2024-06-15",
            "volume": "968",
            "page": "L12",
        }
        with patch("zotero_mcp.tools.write._ads_client") as mock_ads:
            mock_ads._FULL_FIELDS = ads_client._FULL_FIELDS
            mock_ads.search.return_value = [published_doc]
            result = _find_published_version(eprint_doc)
        assert result is not None
        assert result["pub"] == "The Astrophysical Journal"

    def test_returns_none_when_no_match(self):
        eprint_doc = {
            "doctype": "eprint",
            "title": "Some Unpublished Preprint About Stuff",
            "bibcode": "2024arXiv240199999X",
        }
        with patch("zotero_mcp.tools.write._ads_client") as mock_ads:
            mock_ads._FULL_FIELDS = ads_client._FULL_FIELDS
            mock_ads.search.return_value = []
            result = _find_published_version(eprint_doc)
        assert result is None


# --------------------------------------------------------------------------- #
# _upgrade_single_preprint
# --------------------------------------------------------------------------- #

class TestUpgradeSinglePreprint:
    def test_skips_non_preprint(self):
        write_zot = MagicMock()
        write_zot.item.return_value = {
            "key": "ABC12345",
            "data": {"itemType": "journalArticle", "title": "Already Journal"},
        }
        result = _upgrade_single_preprint(write_zot, "ABC12345")
        assert result["status"] == "already_article"

    def test_upgrades_preprint_to_journal_article(self):
        write_zot = MagicMock()
        write_zot.item.return_value = {
            "key": "PRE12345",
            "data": {
                "key": "PRE12345",
                "itemType": "preprint",
                "title": "Discovery of a New Pulsar in Globular Cluster",
                "DOI": "10.48550/arXiv.2401.12345",
                "extra": "arXiv:2401.12345",
            },
        }
        write_zot.item_template.return_value = {
            "itemType": "journalArticle",
            "title": "",
            "date": "",
            "publicationTitle": "",
            "journalAbbreviation": "",
            "volume": "",
            "issue": "",
            "pages": "",
            "DOI": "",
        }
        eprint_doc = {
            "doctype": "eprint",
            "title": "Discovery of a New Pulsar in Globular Cluster",
            "bibcode": "2024arXiv240112345A",
        }
        published_doc = {
            "doctype": "article",
            "pub": "The Astrophysical Journal",
            "title": "Discovery of a New Pulsar in Globular Cluster",
            "bibstem": "ApJ",
            "pubdate": "2024-06-15",
            "volume": "968",
            "page": "L12",
            "doi": ["10.3847/2041-8213/ad4f0a"],
            "bibcode": "2024ApJ...968L..12A",
        }
        with patch("zotero_mcp.tools.write._ads_client") as mock_ads:
            mock_ads._FULL_FIELDS = ads_client._FULL_FIELDS
            mock_ads.fetch_record.return_value = eprint_doc
            mock_ads.search.return_value = [published_doc]
            result = _upgrade_single_preprint(write_zot, "PRE12345")
        assert result["status"] == "upgraded"
        write_zot.update_item.assert_called_once()
        patched = write_zot.update_item.call_args[0][0]
        assert patched["itemType"] == "journalArticle"
        assert patched["publicationTitle"] == "The Astrophysical Journal"
        assert patched["journalAbbreviation"] == "ApJ"
        assert patched["date"] == "2024-06-15"
        assert patched["volume"] == "968"
        assert patched["pages"] == "L12"
        assert patched["DOI"] == "10.3847/2041-8213/ad4f0a"

    def test_not_published_when_no_published_version(self):
        write_zot = MagicMock()
        write_zot.item.return_value = {
            "key": "PRE99999",
            "data": {
                "key": "PRE99999",
                "itemType": "preprint",
                "title": "Very New Unpublished Work",
                "DOI": "10.48550/arXiv.2501.99999",
                "extra": "",
            },
        }
        eprint_doc = {
            "doctype": "eprint",
            "title": "Very New Unpublished Work",
            "bibcode": "2025arXiv250199999X",
        }
        with patch("zotero_mcp.tools.write._ads_client") as mock_ads:
            mock_ads._FULL_FIELDS = ads_client._FULL_FIELDS
            mock_ads.fetch_record.return_value = None  # no bibcode
            # DOI search returns the eprint doc.
            mock_ads.search.return_value = [eprint_doc]
            result = _upgrade_single_preprint(write_zot, "PRE99999")
        assert result["status"] == "not_published"
        write_zot.update_item.assert_not_called()
