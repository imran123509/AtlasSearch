from __future__ import annotations

import pytest

from atlas_indexer.analysis import (
    Analyzer,
    AnalyzerConfig,
    AnalyzerMismatch,
    FoldPolicy,
    Script,
    TokenKind,
    casefold,
    character_bigrams,
    detect_script,
    find_protected,
    fold_diacritics,
    fold_policy,
    normalise,
    stem_word,
    terms_by_position,
)
from atlas_indexer.analysis.tokens import Token


@pytest.fixture
def analyzer():
    return Analyzer()


def terms(analyzer, text, lang=None):
    return set(analyzer.analyze(text, lang).terms)


# ---------------------------------------------------------------------------
# The one rule that matters
# ---------------------------------------------------------------------------

class TestSymmetry:
    """The query must be tokenised exactly like the document.

    Asymmetry produces terms that can never match, and it fails silently —
    documents simply do not appear, with no error anywhere.
    """

    @pytest.mark.parametrize(
        "text,lang",
        [
            ("The Block-Max WAND algorithm", "en"),
            ("Fixing 0x80070643 in C++", "en"),
            ("schön und Bär", "de"),
            ("año nuevo café", "es"),
            ("日本語のテキスト", "ja"),
            ("universities running better", "en"),
            ("MiXeD CaSe ÜMLÄUTS", None),
        ],
    )
    def test_document_and_query_produce_identical_terms(self, analyzer, text, lang):
        doc = analyzer.analyze_document(text, lang)
        qry = analyzer.analyze_query(text, lang)
        assert doc.terms == qry.terms
        assert doc.texts() == qry.texts()

    def test_query_side_flag_never_reaches_the_pipeline(self, analyzer):
        """A flag is a weak guarantee, so the structure enforces it: query-side
        work is layered on top of `_analyze_core`, which takes no such argument."""
        import inspect

        assert "query_side" not in inspect.signature(analyzer._analyze_core).parameters

    def test_query_expansion_is_additive_only(self, analyzer):
        """Expansion may add terms at an existing position. It may never remove
        or alter one already produced."""
        base = analyzer.analyze_document("block max wand", "en")
        expanded = analyzer.analyze_query("block max wand", "en")
        assert set(base.terms) <= set(expanded.terms)
        for term, positions in base.terms.items():
            assert set(positions) <= set(expanded.terms[term])


class TestAnalyzerVersioning:
    def test_version_is_stable_for_identical_config(self):
        assert Analyzer().version == Analyzer().version

    def test_version_changes_with_config(self):
        a = Analyzer(AnalyzerConfig(stem=True))
        b = Analyzer(AnalyzerConfig(stem=False))
        assert a.version != b.version

    def test_matching_version_is_accepted(self, analyzer):
        analyzer.check_compatible(analyzer.version)

    def test_mismatched_version_is_refused(self, analyzer):
        """A mismatched analyzer produces query terms the index cannot contain,
        so every result set is quietly wrong. Fail loudly instead."""
        with pytest.raises(AnalyzerMismatch, match="rebuild"):
            analyzer.check_compatible("v1-deadbeefdeadbeef")


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------

class TestNormalisation:
    def test_nfkc_folds_ligatures(self):
        """Without NFKC, `ﬁle` and `file` are different terms and a user
        searching for one never finds the other."""
        assert normalise("ﬁle") == "file"

    def test_nfkc_folds_fullwidth(self):
        assert normalise("ｆｕｌｌ") == "full"

    def test_nfkc_folds_circled_digits(self):
        assert normalise("①") == "1"

    def test_nfc_would_not_be_enough(self):
        import unicodedata

        assert unicodedata.normalize("NFC", "ﬁle") != "file"
        assert normalise("ﬁle") == "file"

    def test_casefold_handles_sharp_s(self):
        """`.lower()` leaves ß alone; full case folding maps it to ss."""
        assert casefold("straße") == "strasse"
        assert "straße".lower() != "strasse"

    def test_turkish_dotted_and_dotless_i(self):
        assert casefold("I", "tr") == "ı"
        assert casefold("İ", "tr") == "i"
        assert casefold("I", "en") == "i"

    def test_analyzer_matches_across_ligature_and_ascii(self, analyzer):
        assert terms(analyzer, "ﬁle", "en") & terms(analyzer, "file", "en")


# ---------------------------------------------------------------------------
# Diacritics — the language-dependent part
# ---------------------------------------------------------------------------

class TestDiacritics:
    def test_english_folds_accents(self):
        assert fold_policy("en") is FoldPolicy.FOLD
        assert "cafe" in fold_diacritics("café", "en")

    def test_german_transliterates_rather_than_strips(self):
        """schön != schon. German's own convention is ä->ae, not ä->a."""
        assert fold_policy("de") is FoldPolicy.TRANSLITERATE
        forms = fold_diacritics("schön", "de")
        assert "schoen" in forms
        assert "schon" not in forms

    @pytest.mark.parametrize("lang", ["sv", "fi", "da", "no", "tr", "et", "hu"])
    def test_scandinavian_and_turkic_keep_their_letters(self, lang):
        """å ä ö are distinct letters at the end of the alphabet, not decorated
        vowels."""
        assert fold_policy(lang) is FoldPolicy.KEEP
        assert fold_diacritics("för", lang) == ["för"]

    def test_spanish_folds_accents_but_protects_enye(self):
        """año != ano, and one of them is embarrassing."""
        assert "ano" not in fold_diacritics("año", "es")
        assert "cafe" in fold_diacritics("café", "es")

    def test_unknown_language_indexes_both_forms(self):
        """Guessing wrong is silent in both directions, so do not guess."""
        assert fold_policy(None) is FoldPolicy.BOTH
        forms = fold_diacritics("café", None)
        assert "café" in forms and "cafe" in forms

    def test_german_schoen_and_schon_stay_distinct_as_surface_forms(self, analyzer):
        a = analyzer.analyze("schön", "de")
        b = analyzer.analyze("schon", "de")
        surfaces_a = {t.text for t in a.tokens if t.kind is TokenKind.SURFACE}
        surfaces_b = {t.text for t in b.tokens if t.kind is TokenKind.SURFACE}
        assert surfaces_a != surfaces_b

    def test_german_stemmer_folds_umlauts_and_that_is_survivable(self, analyzer):
        """Snowball's German stemmer strips umlauts as part of its algorithm, so
        `schön` stems to `schon` no matter what the fold policy says.

        The diacritic policy is therefore partly defeated by the stemmer — worth
        knowing. It stays acceptable because the surface form is also indexed: an
        exact query for `schön` matches surface + transliteration + stem (three
        terms) while a query for `schon` matches fewer, so exact still ranks
        higher. Precision from the surface, recall from the stem.
        """
        assert stem_word("schön", "de") == "schon"
        schoen = analyzer.analyze("schön", "de").terms
        assert "schön" in schoen and "schoen" in schoen and "schon" in schoen


# ---------------------------------------------------------------------------
# Identifiers that must not be split
# ---------------------------------------------------------------------------

class TestProtectedIdentifiers:
    @pytest.mark.parametrize(
        "text,expected",
        [
            ("C++", "c++"),
            (".NET", ".net"),
            ("0x80070643", "0x80070643"),
            ("192.168.1.1", "192.168.1.1"),
            ("COVID-19", "covid-19"),
            ("admin@example.com", "admin@example.com"),
            ("v1.2.3", "v1.2.3"),
            ("node.js", "node.js"),
        ],
    )
    def test_identifier_survives_intact(self, analyzer, text, expected):
        """Somebody pastes an error code and gets nothing — the most visible
        quality failure a search engine can have."""
        assert expected in terms(analyzer, text, "en")

    def test_c_plus_plus_does_not_leak_a_bare_c(self, analyzer):
        """Emitting "c" would make every C++ document match a query for "c"."""
        assert "c" not in terms(analyzer, "C++ programming", "en")

    def test_covid_19_matches_both_whole_and_parts(self, analyzer):
        got = terms(analyzer, "COVID-19", "en")
        assert {"covid-19", "covid", "19"} <= got

    def test_parts_take_consecutive_positions_for_phrases(self, analyzer):
        result = analyzer.analyze("COVID-19", "en")
        assert result.terms["covid"] == [0]
        assert result.terms["19"] == [1]
        assert result.terms["covid-19"] == [0]

    def test_ip_address_does_not_emit_noise_octets(self, analyzer):
        """"192" and "1" as standalone terms are pure noise; an IP is only ever
        searched whole."""
        got = terms(analyzer, "server at 192.168.1.1 responded", "en")
        assert "192.168.1.1" in got
        assert "192" not in got and "1" not in got

    def test_email_does_emit_parts(self, analyzer):
        got = terms(analyzer, "admin@example.com", "en")
        assert {"admin@example.com", "admin", "example", "com"} <= got

    def test_surrounding_text_still_tokenised(self, analyzer):
        got = terms(analyzer, "Fixing error 0x80070643 today", "en")
        assert {"fixing", "error", "0x80070643", "today"} <= got

    def test_find_protected_returns_ordered_non_overlapping_spans(self):
        spans = find_protected("see 0x1234ABCD and COVID-19 at admin@x.com")
        assert [s.start for s in spans] == sorted(s.start for s in spans)
        for a, b in zip(spans, spans[1:]):
            assert a.end <= b.start

    def test_disabling_protection_is_possible(self):
        plain = Analyzer(AnalyzerConfig(protect_identifiers=False))
        assert "0x80070643" not in set(plain.analyze("0x80070643", "en").terms)


# ---------------------------------------------------------------------------
# Stopwords
# ---------------------------------------------------------------------------

class TestStopwordsAreNotRemoved:
    """Removing them at index time was right in 1998 and is wrong now. IDF
    already discounts a term appearing in 90% of documents."""

    @pytest.mark.parametrize(
        "phrase", ["to be or not to be", "The Who", "let it be", "vitamin A"]
    )
    def test_phrase_survives(self, analyzer, phrase):
        got = terms(analyzer, phrase, "en")
        assert got, f"{phrase!r} was reduced to nothing"
        for word in phrase.lower().split():
            assert word in got or any(word in t for t in got), f"lost {word!r}"

    def test_the_who_keeps_both_words(self, analyzer):
        assert {"the", "who"} <= terms(analyzer, "The Who", "en")

    def test_config_has_stopword_removal_off(self):
        assert AnalyzerConfig().remove_stopwords is False


# ---------------------------------------------------------------------------
# Stemming
# ---------------------------------------------------------------------------

class TestStemming:
    def test_surface_and_stem_both_indexed(self, analyzer):
        """Recall from the stem, precision from the surface form."""
        got = terms(analyzer, "running", "en")
        assert {"running", "run"} <= got

    def test_they_share_a_position(self, analyzer):
        result = analyzer.analyze("running", "en")
        assert result.terms["running"] == result.terms["run"] == [0]

    def test_unchanged_stem_is_not_duplicated(self, analyzer):
        """Emitting an identical token twice at one position would double the
        term frequency and make BM25 over-score the document."""
        result = analyzer.analyze("run", "en")
        assert len([t for t in result.tokens if t.text == "run"]) == 1

    def test_snowball_collision_is_real(self):
        """The doc gives `university -> univers` as the collision example.
        Snowball actually produces `universiti` for that word — the collision is
        real but between `university` and `universities`."""
        assert stem_word("university", "en") == stem_word("universities", "en")
        assert stem_word("universe", "en") != stem_word("university", "en")

    def test_stemming_can_be_disabled(self):
        plain = Analyzer(AnalyzerConfig(stem=False))
        assert "run" not in set(plain.analyze("running", "en").terms)

    @pytest.mark.parametrize("lang", ["zh", "ja", "ko", "th", "vi"])
    def test_languages_without_suffix_morphology_are_not_stemmed(self, lang):
        assert stem_word("test", lang) is None

    def test_unknown_language_is_not_stemmed(self):
        assert stem_word("running", None) is None
        assert stem_word("running", "xx") is None


# ---------------------------------------------------------------------------
# Segmentation
# ---------------------------------------------------------------------------

class TestSegmentation:
    @pytest.mark.parametrize(
        "text,expected",
        [
            ("hello world", Script.LATIN),
            ("日本語のテキスト", Script.CJK),
            ("한국어 텍스트", Script.KOREAN),
            ("ราชอาณาจักร", Script.THAI),
            ("مرحبا بالعالم", Script.ARABIC),
        ],
    )
    def test_script_detection(self, text, expected):
        assert detect_script(text) is expected

    def test_cjk_produces_overlapping_bigrams(self, analyzer):
        got = terms(analyzer, "日本語", "ja")
        assert {"日本", "本語"} <= got

    def test_single_cjk_character_still_indexed(self):
        """Otherwise a one-character query could never match."""
        assert [w for w, _s, _e in character_bigrams("日")] == ["日"]

    def test_mixed_script_keeps_both(self, analyzer):
        """A Japanese page quoting an English product name must not lose either.

        This is majority-Latin by character count, so dispatching on the
        document's dominant script would leave the Japanese as one unmatched
        token. Segmentation runs per script run instead.
        """
        got = terms(analyzer, "日本語のテキスト and English", "ja")
        assert {"and", "english"} <= got
        assert "日本" in got

    def test_script_change_inside_a_word_run_is_split(self, analyzer):
        got = terms(analyzer, "iPhoneはいい", "ja")
        assert "iphone" in got
        assert "はい" in got

    def test_apostrophes_stay_inside_words(self, analyzer):
        """Splitting them produces fragments that make phrase queries fail."""
        assert "don't" in terms(analyzer, "don't stop", "en")

    def test_thai_falls_back_to_bigrams(self, analyzer):
        result = analyzer.analyze("ราชอาณาจักร", "th")
        assert result.tokens
        assert all(t.kind is TokenKind.NGRAM for t in result.tokens)

    def test_arabic_vowel_marks_stripped(self, analyzer):
        with_marks = terms(analyzer, "مَرْحَبا", "ar")
        without = terms(analyzer, "مرحبا", "ar")
        assert with_marks & without


# ---------------------------------------------------------------------------
# Token bookkeeping
# ---------------------------------------------------------------------------

class TestTokenBookkeeping:
    def test_length_counts_positions_not_tokens(self, analyzer):
        """Counting alternatives would make a document look longer purely
        because we chose to index its stems, and BM25 would then penalise it."""
        result = analyzer.analyze("running quickly", "en")
        assert len(result.tokens) > result.length
        assert result.length == 2

    def test_duplicate_term_position_pairs_collapse(self):
        tokens = [
            Token("run", 0, kind=TokenKind.SURFACE),
            Token("run", 0, kind=TokenKind.STEM),
            Token("run", 1, kind=TokenKind.SURFACE),
        ]
        assert terms_by_position(tokens) == {"run": [0, 1]}

    def test_repeated_word_gets_distinct_positions(self, analyzer):
        assert analyzer.analyze("the cat the cat", "en").terms["cat"] == [1, 3]

    def test_empty_text(self, analyzer):
        result = analyzer.analyze("", "en")
        assert result.tokens == [] and result.length == 0

    def test_whitespace_and_punctuation_only(self, analyzer):
        assert analyzer.analyze("   ... !!! ", "en").tokens == []

    def test_absurdly_long_token_is_truncated(self):
        a = Analyzer(AnalyzerConfig(max_token_length=10))
        assert all(len(t) <= 10 for t in a.analyze("x" * 500, "en").terms)


# ---------------------------------------------------------------------------
# Clitics and particles — the agglutinative half of the segmentation table
# ---------------------------------------------------------------------------

class TestMorphology:
    """Whitespace splitting alone leaves an agglutinative word unmatchable by
    its own root. These are light stemmers (fixed affix lists), not analysers —
    the surface form is always kept, so a wrong strip costs one noisy term
    rather than making the original unfindable."""

    def test_korean_particle_stripped(self, analyzer):
        got = terms(analyzer, "텍스트를 읽습니다", "ko")
        assert "텍스트" in got, "object particle 를 was not stripped"

    def test_korean_surface_form_kept(self, analyzer):
        assert "텍스트를" in terms(analyzer, "텍스트를 읽습니다", "ko")

    @pytest.mark.parametrize(
        "word,root",
        [("텍스트를", "텍스트"), ("서울에서", "서울"), ("책은", "책"), ("학교에", "학교")],
    )
    def test_korean_particles(self, word, root):
        from atlas_indexer.analysis import light_stem

        assert light_stem(word, "ko") == root

    def test_arabic_clitics_stripped(self, analyzer):
        """والكتاب is wa+al+kitab — "and-the-book"."""
        got = terms(analyzer, "والكتاب", "ar")
        assert "كتاب" in got

    def test_arabic_surface_form_kept(self, analyzer):
        assert "والكتاب" in terms(analyzer, "والكتاب", "ar")

    def test_longest_prefix_wins(self):
        """و+ال must be tried before ال, or والكتاب strips to لكتاب."""
        from atlas_indexer.analysis import light_stem

        assert light_stem("والكتاب", "ar") == "كتاب"

    def test_short_words_are_not_over_stripped(self):
        from atlas_indexer.analysis import light_stem

        assert light_stem("ال", "ar") is None
        assert light_stem("는", "ko") is None

    def test_unchanged_word_returns_none(self):
        from atlas_indexer.analysis import light_stem

        assert light_stem("كتاب", "ar") is None

    def test_languages_without_clitics_are_untouched(self):
        from atlas_indexer.analysis import light_stem

        assert light_stem("running", "en") is None
        assert light_stem("laufen", "de") is None

    def test_can_be_disabled(self):
        plain = Analyzer(AnalyzerConfig(strip_clitics=False))
        assert "텍스트" not in set(plain.analyze("텍스트를", "ko").terms)

    def test_root_and_surface_share_a_position(self, analyzer):
        result = analyzer.analyze("텍스트를", "ko")
        assert result.terms["텍스트를"] == result.terms["텍스트"] == [0]


class TestEmailWithoutTld:
    def test_bare_host_email_is_protected(self, analyzer):
        """`user@host` is in the doc's must-not-split list. Requiring a dotted
        domain misses intranet and local addresses entirely."""
        got = terms(analyzer, "user@host", "en")
        assert "user@host" in got

    def test_dotted_email_still_works(self, analyzer):
        assert "admin@example.com" in terms(analyzer, "admin@example.com", "en")
