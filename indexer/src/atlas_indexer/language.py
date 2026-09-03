"""Language identification.

Two rules from features/HTML-PARSER.md, both load-bearing:

  Run on the extracted main text, not the raw HTML. Markup skews n-gram
  statistics badly — `<div class="container">` looks like English to a
  character-n-gram model regardless of what the page says.

  Use the `lang` attribute as a prior, never as truth. It is wrong often enough
  to matter, and tokenisation branches on this, so an error here corrupts the
  document's terms permanently.
"""

from __future__ import annotations

import re
import threading

import structlog

from .config import LanguageConfig
from .models import LanguageResult

log = structlog.get_logger(__name__)

_identifier = None
_lock = threading.Lock()

_LANG_ATTR = re.compile(r"^([a-zA-Z]{2,3})(?:[-_].*)?$")


def _get_identifier():  # noqa: ANN202
    """Load the model once; it is ~1 MB and thread-safe after construction."""
    global _identifier
    if _identifier is None:
        with _lock:
            if _identifier is None:
                from py3langid.langid import MODEL_FILE, LanguageIdentifier

                _identifier = LanguageIdentifier.from_model_file(MODEL_FILE, norm_probs=True)
    return _identifier


def normalise_lang_attr(value: str | None) -> str | None:
    """`en-GB` -> `en`. Returns None for junk like `lang=""` or `lang="{{lang}}"`."""
    if not value:
        return None
    m = _LANG_ATTR.match(value.strip())
    return m.group(1).lower() if m else None


def identify(text: str, cfg: LanguageConfig, *, lang_attr: str | None = None) -> LanguageResult:
    """Identify the language of `text`, using `lang_attr` only as a tie-breaker."""
    prior = normalise_lang_attr(lang_attr)
    sample = " ".join(text.split())

    if len(sample) < cfg.min_chars:
        # Too little signal for the model. Fall back to the declared attribute
        # rather than guessing — but record that we did.
        if prior:
            return LanguageResult(prior, cfg.attribute_prior_weight, "attribute", prior)
        return LanguageResult("und", 0.0, "fallback", prior)

    try:
        code, confidence = _get_identifier().classify(sample[:10_000])
    except Exception as exc:  # noqa: BLE001 - never fail a parse over language ID
        log.warning("language.classify_failed", error=str(exc))
        return LanguageResult(prior or "und", 0.0, "fallback", prior)

    confidence = float(confidence)
    disagreed = bool(prior and prior != code)

    if confidence >= cfg.min_confidence:
        # The model is sure. It wins even against a declared attribute, because
        # a confident n-gram match on real prose beats a copy-pasted template.
        return LanguageResult(code, confidence, "model", prior, disagreed)

    # The model is unsure. Now the declared attribute is worth something.
    if prior:
        return LanguageResult(prior, cfg.attribute_prior_weight, "attribute", prior, disagreed)

    return LanguageResult(code, confidence, "model", prior, disagreed)
