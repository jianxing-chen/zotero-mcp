"""Tests for metadata enrichment and preprint upgrade features."""

from unittest.mock import MagicMock, patch

from zotero_mcp import ads_client
from zotero_mcp.citation_import import csl_json_to_zotero
from zotero_mcp.tools.write import (
    _ads_doc_to_enrich_fields,
    _clean_title_for_ads,
    _enrich_single_item,
    _find_published_version,
    _parse_arxiv_id_from_extra,
    _parse_bibcode_from_extra,
    _title_similarity,
    _upgrade_single_preprint,
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


class TestParseArxivIdFromExtra:
    def test_extracts_new_style_arxiv_id(self):
        extra = "arXiv:2401.12345 [astro-ph]"
        assert _parse_arxiv_id_from_extra(extra) == "2401.12345"

    def test_extracts_with_emoji_prefix(self):
        extra = "⭐⭐arXiv:1605.01665 [astro-ph, physics:nucl-th]"
        assert _parse_arxiv_id_from_extra(extra) == "1605.01665"

    def test_extracts_old_style_arxiv_id(self):
        extra = "arXiv:astro-ph/0501001"
        assert _parse_arxiv_id_from_extra(extra) == "astro-ph/0501001"

    def test_returns_none_when_no_arxiv(self):
        assert _parse_arxiv_id_from_extra("bibcode: 2024ApJ...961L..10X") is None

    def test_returns_none_for_empty(self):
        assert _parse_arxiv_id_from_extra(None) is None
        assert _parse_arxiv_id_from_extra("") is None
        assert _parse_arxiv_id_from_extra("🌟") is None


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

    def test_jabbr_from_bibcode_not_bibstem(self):
        """Journal abbreviation extracted from bibcode, not bibstem.

        bibstem returns 'ApJL' for L-page papers, but the bibcode encodes
        the journal as 'ApJ' — the 'L' is part of the page number (L47).
        """
        doc = {"bibcode": "2000ApJ...545L..47G", "bibstem": ["ApJL", "ApJL..545"]}
        result = _ads_doc_to_enrich_fields(doc, {"journal_abbreviation"})
        assert result["journal_abbreviation"] == "ApJ"

    def test_jabbr_normal_page_bibcode(self):
        """Non-L-page bibcode: bibcode extraction matches bibstem."""
        doc = {"bibcode": "2013ApJ...778..104R", "bibstem": ["ApJ", "ApJ...778"]}
        result = _ads_doc_to_enrich_fields(doc, {"journal_abbreviation"})
        assert result["journal_abbreviation"] == "ApJ"

    def test_jabbr_falls_back_to_bibstem_without_bibcode(self):
        """No bibcode → fall back to bibstem."""
        doc = {"bibstem": ["MNRAS", "MNRAS.491"]}
        result = _ads_doc_to_enrich_fields(doc, {"journal_abbreviation"})
        assert result["journal_abbreviation"] == "MNRAS"

    def test_jabbr_handles_ampersand(self):
        """A&A (Astronomy & Astrophysics) bibcode."""
        doc = {"bibcode": "2014A&A...571A..56G", "bibstem": ["A&A", "A&A...571"]}
        result = _ads_doc_to_enrich_fields(doc, {"journal_abbreviation"})
        assert result["journal_abbreviation"] == "A&A"


# --------------------------------------------------------------------------- #
# _enrich_single_item
# --------------------------------------------------------------------------- #

class TestEnrichSingleItem:
    def test_skips_when_fields_already_present_and_bibcode_exists(self):
        """All wanted fields present AND bibcode already in Extra → skip."""
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
                "extra": "bibcode: 2023ApJ...945L..15A",
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
                "bibcode": "2013ApJ...762...36X",
                "pubdate": "2013-01-10",
                "bibstem": "ApJ",
                "year": "2013",
                "pub": "The Astrophysical Journal",
            }]
            result = _enrich_single_item(write_zot, "ABC12345",
                                         {"date", "journal_abbreviation"}, force=False)
        assert result["status"] == "enriched"
        assert "date" in result["filled"]
        assert "journal_abbreviation" in result["filled"]
        assert "bibcode" in result["filled"]
        assert "url" in result["filled"]
        write_zot.update_item.assert_called_once()
        # Check the patched data has the right fields.
        patched = write_zot.update_item.call_args[0][0]
        assert patched["date"] == "2013-01-10"
        assert patched["journalAbbreviation"] == "ApJ"
        assert "bibcode: 2013ApJ...762...36X" in patched["extra"]
        assert patched["url"] == "https://ui.adsabs.harvard.edu/abs/2013ApJ...762...36X"

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

    # --- Gap 1: bibcode auto-write to Extra ---

    def test_bibcode_written_to_empty_extra(self):
        """ADS doc has a bibcode; item Extra is empty → bibcode appended."""
        write_zot = MagicMock()
        write_zot.item.return_value = {
            "key": "GAP10001",
            "data": {
                "key": "GAP10001", "itemType": "journalArticle",
                "title": "Test", "date": "2023", "journalAbbreviation": "ApJ",
                "DOI": "10.1088/x", "extra": "",
            },
        }
        with patch("zotero_mcp.tools.write._ads_client") as mock_ads:
            mock_ads._FULL_FIELDS = ads_client._FULL_FIELDS
            mock_ads.fetch_record.return_value = None
            mock_ads.search.return_value = [{"bibcode": "2023ApJ...945L..15A"}]
            result = _enrich_single_item(write_zot, "GAP10001",
                                         {"date", "journal_abbreviation"}, force=False)
        assert result["status"] == "enriched"
        assert "bibcode" in result["filled"]
        patched = write_zot.update_item.call_args[0][0]
        assert "bibcode: 2023ApJ...945L..15A" in patched["extra"]

    def test_bibcode_not_duplicated_when_already_present(self):
        """Extra already has a bibcode line → not written again."""
        write_zot = MagicMock()
        write_zot.item.return_value = {
            "key": "GAP10002",
            "data": {
                "key": "GAP10002", "itemType": "journalArticle",
                "title": "Test", "date": "", "journalAbbreviation": "",
                "DOI": "10.1088/x", "extra": "bibcode: 2023ApJ...945L..15A",
            },
        }
        with patch("zotero_mcp.tools.write._ads_client") as mock_ads:
            mock_ads._FULL_FIELDS = ads_client._FULL_FIELDS
            mock_ads.fetch_record.return_value = {
                "bibcode": "2023ApJ...945L..15A", "pubdate": "2023-01",
                "bibstem": "ApJ", "year": "2023",
            }
            result = _enrich_single_item(write_zot, "GAP10002",
                                         {"date", "journal_abbreviation"}, force=False)
        # bibcode was already present → only date/journalAbbr filled
        assert "bibcode" not in result["filled"]
        patched = write_zot.update_item.call_args[0][0]
        # Extra unchanged (no duplicate bibcode line)
        assert patched["extra"] == "bibcode: 2023ApJ...945L..15A"

    def test_bibcode_appended_preserving_existing_extra(self):
        """Extra has other content (e.g. arXiv) → bibcode appended, not clobbered."""
        write_zot = MagicMock()
        write_zot.item.return_value = {
            "key": "GAP10003",
            "data": {
                "key": "GAP10003", "itemType": "journalArticle",
                "title": "Test", "date": "2023", "journalAbbreviation": "ApJ",
                "DOI": "10.1088/x", "extra": "arXiv:2401.12345 [astro-ph]",
            },
        }
        with patch("zotero_mcp.tools.write._ads_client") as mock_ads:
            mock_ads._FULL_FIELDS = ads_client._FULL_FIELDS
            mock_ads.fetch_record.return_value = None
            mock_ads.search.return_value = [{"bibcode": "2024ApJ...961L..10X"}]
            result = _enrich_single_item(write_zot, "GAP10003",
                                         {"date", "journal_abbreviation"}, force=False)
        assert "bibcode" in result["filled"]
        patched = write_zot.update_item.call_args[0][0]
        assert "arXiv:2401.12345" in patched["extra"]
        assert "bibcode: 2024ApJ...961L..10X" in patched["extra"]

    def test_ads_url_written_when_url_empty(self):
        """ADS abstract URL written to the url field when it's empty."""
        write_zot = MagicMock()
        write_zot.item.return_value = {
            "key": "GAP10004",
            "data": {
                "key": "GAP10004", "itemType": "journalArticle",
                "title": "Test", "date": "2023", "journalAbbreviation": "ApJ",
                "DOI": "10.1088/x", "url": "", "extra": "",
            },
        }
        with patch("zotero_mcp.tools.write._ads_client") as mock_ads:
            mock_ads._FULL_FIELDS = ads_client._FULL_FIELDS
            mock_ads.fetch_record.return_value = None
            mock_ads.search.return_value = [{"bibcode": "2024ApJ...961L..10X"}]
            result = _enrich_single_item(write_zot, "GAP10004",
                                         {"date", "journal_abbreviation"}, force=False)
        assert "url" in result["filled"]
        patched = write_zot.update_item.call_args[0][0]
        assert patched["url"] == "https://ui.adsabs.harvard.edu/abs/2024ApJ...961L..10X"

    def test_url_not_overwritten_when_already_present(self):
        """Existing url (e.g. arXiv abstract page) is preserved, not replaced."""
        write_zot = MagicMock()
        write_zot.item.return_value = {
            "key": "GAP10005",
            "data": {
                "key": "GAP10005", "itemType": "journalArticle",
                "title": "Test", "date": "2023", "journalAbbreviation": "ApJ",
                "DOI": "10.1088/x",
                "url": "https://arxiv.org/abs/2401.12345",
                "extra": "",
            },
        }
        with patch("zotero_mcp.tools.write._ads_client") as mock_ads:
            mock_ads._FULL_FIELDS = ads_client._FULL_FIELDS
            mock_ads.fetch_record.return_value = None
            mock_ads.search.return_value = [{"bibcode": "2024ApJ...961L..10X"}]
            result = _enrich_single_item(write_zot, "GAP10005",
                                         {"date", "journal_abbreviation"}, force=False)
        # bibcode still written, but url unchanged
        assert "bibcode" in result["filled"]
        assert "url" not in result["filled"]
        patched = write_zot.update_item.call_args[0][0]
        assert patched["url"] == "https://arxiv.org/abs/2401.12345"

    # --- Gap 2: title fallback ---

    def test_title_fallback_finds_record_with_greek_and_latex(self):
        """No bibcode/DOI/arXiv → title search finds ADS record.
        Title contains Greek letters (σ) and LaTeX ($\\lambda$)."""
        write_zot = MagicMock()
        write_zot.item.return_value = {
            "key": "GAP20001",
            "data": {
                "key": "GAP20001", "itemType": "journalArticle",
                "title": "Spectral analysis of \u03c3 Ori and $\\lambda$ Orionis",
                "date": "", "journalAbbreviation": "",
                "DOI": "", "extra": "",
            },
        }
        with patch("zotero_mcp.tools.write._ads_client") as mock_ads:
            mock_ads._FULL_FIELDS = ads_client._FULL_FIELDS
            mock_ads.fetch_record.return_value = None
            # The title search should be called with cleaned words.
            mock_ads.search.return_value = [{
                "bibcode": "2018A&A...618A..50S",
                "title": ["Spectral analysis of sigma Ori and lambda Orionis"],
                "pubdate": "2018-11", "bibstem": "A&A", "year": "2018",
            }]
            result = _enrich_single_item(write_zot, "GAP20001",
                                         {"date", "journal_abbreviation"}, force=False)
        assert result["status"] == "enriched"
        # Verify the ADS search was called with a title: query
        search_call = mock_ads.search.call_args
        assert search_call is not None
        query = search_call[0][0] if search_call[0] else search_call[1].get("query", "")
        assert 'title:"' in query

    def test_title_too_short_skips_title_search(self):
        """Title with < 3 words → _find_by_title returns None → not_found."""
        write_zot = MagicMock()
        write_zot.item.return_value = {
            "key": "GAP20002",
            "data": {
                "key": "GAP20002", "itemType": "journalArticle",
                "title": "Dark Matter",
                "date": "", "journalAbbreviation": "",
                "DOI": "", "extra": "",
            },
        }
        with patch("zotero_mcp.tools.write._ads_client") as mock_ads:
            mock_ads._FULL_FIELDS = ads_client._FULL_FIELDS
            result = _enrich_single_item(write_zot, "GAP20002",
                                         {"date", "journal_abbreviation"}, force=False)
        # "Dark Matter" → 2 words → _find_by_title returns None before calling ADS
        assert result["status"] == "not_found"
        # ADS search was NOT called for title (no identifier path either)
        mock_ads.search.assert_not_called()

    def test_title_with_english_greek_spelling(self):
        """Title with English-spelled Greek (omega Cen) → normal search."""
        write_zot = MagicMock()
        write_zot.item.return_value = {
            "key": "GAP20003",
            "data": {
                "key": "GAP20003", "itemType": "journalArticle",
                "title": "Variable stars in omega Cen survey observations",
                "date": "", "journalAbbreviation": "",
                "DOI": "", "extra": "",
            },
        }
        with patch("zotero_mcp.tools.write._ads_client") as mock_ads:
            mock_ads._FULL_FIELDS = ads_client._FULL_FIELDS
            mock_ads.search.return_value = [{
                "bibcode": "2020ApJ...890...50N",
                "title": ["Variable stars in omega Cen survey observations"],
                "pubdate": "2020-02", "bibstem": "ApJ", "year": "2020",
            }]
            result = _enrich_single_item(write_zot, "GAP20003",
                                         {"date", "journal_abbreviation"}, force=False)
        assert result["status"] == "enriched"
        assert "date" in result["filled"]
        assert "bibcode" in result["filled"]


# --------------------------------------------------------------------------- #
# _clean_title_for_ads — Greek letters + LaTeX normalization
# --------------------------------------------------------------------------- #

class TestCleanTitleForAds:
    def test_greek_unicode_transliterated(self):
        r"""σ → s, α → a, ω → o (via unidecode), output is lowercase."""
        cleaned = _clean_title_for_ads("Spectral analysis of \u03c3 Ori")
        assert "s ori" in cleaned
        assert "\u03c3" not in cleaned

    def test_latex_commands_unbackslashed(self):
        r"""\gamma → gamma, \lambda → lambda, all lowercase."""
        cleaned = _clean_title_for_ads(r"Flux in $\gamma$-ray and $\lambda$ Ori")
        assert "gamma" in cleaned
        assert "lambda" in cleaned
        assert "$" not in cleaned
        assert "\\" not in cleaned

    def test_mixed_greek_and_latex(self):
        r"""Title with both LaTeX symbols and Unicode Greek."""
        title = "Mass loss in $\\alpha$ and \u03c9 Cen systems"
        cleaned = _clean_title_for_ads(title)
        assert "alpha" in cleaned
        # \u03c9 (omega) → unidecode → "o"; Cen → lowercased → "cen"
        assert "o cen" in cleaned or "omega" in cleaned
        assert "$" not in cleaned

    def test_english_greek_spelling_unchanged(self):
        """'omega Cen' (already English) → lowercased to 'omega cen'."""
        cleaned = _clean_title_for_ads("Variable stars in omega Cen survey")
        assert "omega cen" in cleaned

    def test_punctuation_and_whitespace_collapsed(self):
        cleaned = _clean_title_for_ads("Title:  With  (extra)  [punctuation]!!")
        assert "  " not in cleaned
        assert ":" not in cleaned
        assert "!" not in cleaned

    def test_output_is_lowercase(self):
        """ADS title:phrase search is case-sensitive — output must be lowercase."""
        cleaned = _clean_title_for_ads("ON THE WHITE DWARF COOLING SEQUENCE")
        assert cleaned == "on the white dwarf cooling sequence"


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
