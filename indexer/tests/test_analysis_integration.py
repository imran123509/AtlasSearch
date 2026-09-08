"""End to end: parse -> analyze -> index -> query.

This is where the symmetry rule stops being a unit-test property and starts
being the thing that decides whether a document is findable at all.
"""

from __future__ import annotations

import pytest

from atlas_indexer.analysis import Analyzer, AnalyzerConfig, AnalyzerMismatch
from atlas_indexer.analysis import to_input_document
from atlas_indexer.index import Index
from atlas_indexer.pipeline import Parser

HTML = """<!DOCTYPE html>
<html lang="en">
<head><title>Fixing error 0x80070643 in .NET</title></head>
<body>
  <nav><a href="/">Home</a></nav>
  <article>
    <h1>Fixing error 0x80070643 in .NET</h1>
    <p>The installer reports error 0x80070643 when the C++ redistributable is
       missing. Running the repair tool on 192.168.1.1 resolves it in most
       cases, and the universities we surveyed confirmed the behaviour.</p>
  </article>
  <footer>&copy; 2026</footer>
</body></html>"""


@pytest.fixture
def analyzer():
    return Analyzer()


@pytest.fixture
def indexed(tmp_path, analyzer):
    parsed = Parser().parse(HTML.encode(), url="https://example.com/kb/80070643")
    doc = to_input_document(parsed, analyzer, static_rank=0.8)

    filler = []
    for i in range(60):
        from atlas_indexer.index import InputDocument

        text = f"Unrelated article number {i} about gardening and cookery topics."
        analyzed = analyzer.analyze_document(text, "en")
        filler.append(
            InputDocument(f"filler-{i}", analyzed.terms, 0.1, analyzed.length)
        )

    index = Index(tmp_path / "idx")
    index.add_documents([doc, *filler], analyzer_version=analyzer.version)
    return index, analyzer, doc


class TestRoundTrip:
    def test_exact_error_code_is_findable(self, indexed):
        """The case the whole protected-identifier machinery exists for:
        somebody pastes an error code."""
        index, analyzer, doc = indexed
        query = analyzer.analyze_query("0x80070643", "en")
        hits = index.search(list(query.terms), k=5)
        assert hits and hits[0].external_id == doc.external_id

    @pytest.mark.parametrize("query", [".NET", "C++", "192.168.1.1"])
    def test_other_identifiers_are_findable(self, indexed, query):
        index, analyzer, doc = indexed
        terms = list(analyzer.analyze_query(query, "en").terms)
        hits = index.search(terms, k=5)
        assert hits and hits[0].external_id == doc.external_id

    def test_stemmed_query_matches_unstemmed_document(self, indexed):
        """The document says "universities"; the query says "university"."""
        index, analyzer, doc = indexed
        terms = list(analyzer.analyze_query("university", "en").terms)
        hits = index.search(terms, k=5)
        assert doc.external_id in {h.external_id for h in hits}

    def test_case_and_punctuation_do_not_matter(self, indexed):
        index, analyzer, doc = indexed
        a = index.search(list(analyzer.analyze_query("INSTALLER", "en").terms), k=5)
        b = index.search(list(analyzer.analyze_query("installer", "en").terms), k=5)
        assert [h.external_id for h in a] == [h.external_id for h in b]
        assert a and a[0].external_id == doc.external_id

    def test_title_terms_are_searchable(self, indexed):
        index, analyzer, doc = indexed
        hits = index.search(list(analyzer.analyze_query("fixing", "en").terms), k=5)
        assert doc.external_id in {h.external_id for h in hits}

    def test_boilerplate_is_absent(self, indexed):
        """Nav and footer were stripped at parse time, so they are not terms."""
        _index, _analyzer, doc = indexed
        assert "home" not in doc.terms


class TestFieldGaps:
    def test_phrases_cannot_straddle_a_field_boundary(self, tmp_path, analyzer):
        """Without a gap, a title ending "New York" followed by a body starting
        "Times" would match the phrase "york times" the document never had."""
        parsed = Parser().parse(
            b"<html lang='en'><head><title>New York</title></head>"
            b"<body><article><p>Times reported that the quarterly figures were "
            b"revised upward again this year.</p></article></body></html>",
            url="https://example.com/x",
        )
        doc = to_input_document(parsed, analyzer)
        york = max(doc.terms["york"])
        times = min(doc.terms["times"])
        assert times - york > 1, "no gap between fields"


class TestAnalyzerVersionGate:
    def test_version_is_recorded_in_the_segment(self, indexed):
        index, analyzer, _doc = indexed
        assert index.segments[0].manifest.analyzer_version == analyzer.version

    def test_matching_analyzer_passes_the_gate(self, indexed):
        index, analyzer, _doc = indexed
        analyzer.check_compatible(index.segments[0].manifest.analyzer_version)

    def test_a_different_analyzer_is_refused(self, indexed):
        """Changing the analyzer requires a full rebuild. Serving across the
        change is a silent, total quality failure."""
        index, _analyzer, _doc = indexed
        other = Analyzer(AnalyzerConfig(stem=False))
        with pytest.raises(AnalyzerMismatch, match="rebuild"):
            other.check_compatible(index.segments[0].manifest.analyzer_version)

    def test_the_mismatch_is_not_theoretical(self, tmp_path):
        """Demonstrate what the gate prevents: an index built with stemming,
        queried by an analyzer without it, silently misses the document."""
        stemming = Analyzer(AnalyzerConfig(stem=True))
        from atlas_indexer.index import InputDocument

        analyzed = stemming.analyze_document("the universities were running", "en")
        index = Index(tmp_path / "idx")
        index.add_documents(
            [InputDocument("d0", analyzed.terms, 1.0, analyzed.length)],
            analyzer_version=stemming.version,
        )

        # "university" reaches the document only via the stem.
        good = index.search(list(stemming.analyze_query("university", "en").terms), k=5)
        assert good, "stemmed query should have matched"

        no_stem = Analyzer(AnalyzerConfig(stem=False))
        bad = index.search(list(no_stem.analyze_query("university", "en").terms), k=5)
        assert not bad, "expected the mismatched analyzer to miss — that is the point"


class TestDocumentLength:
    def test_length_is_not_inflated_by_alternatives(self, indexed):
        """A document must not look longer purely because we indexed its stems;
        BM25 length normalisation would penalise it for our choice."""
        _index, _analyzer, doc = indexed
        distinct_positions = len({p for ps in doc.terms.values() for p in ps})
        total_postings = sum(len(ps) for ps in doc.terms.values())
        assert total_postings > distinct_positions  # alternatives exist
        assert doc.length < total_postings          # but length ignores them
