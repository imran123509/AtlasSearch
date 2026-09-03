"""AtlasSearch parse stage — implements features/HTML-PARSER.md.

Consumes `pages.fetched`, produces `pages.parsed`, `links.extracted` and
`urls.discovered`.

Two things to know before changing anything here:

  Silently wrong text extraction is the worst bug this code can have. A document
  indexes fine, queries succeed, and the terms are garbage. Nothing alerts. The
  diagnostics on ParsedDocument (`retained_ratio`, `warnings`) exist to make that
  class of failure visible; do not drop them to slim the payload.

  Anchor text is NOT extracted here. It is discovered on source pages and
  delivered to target pages during index build — a parser cannot know a
  document's anchors because they live on other documents.
"""

from .boilerplate import TemplateLearner, extract
from .charset import decode, detect
from .config import Config
from .models import (
    CharsetResult,
    ExtractedLink,
    LanguageResult,
    LinkRel,
    ParsedDocument,
    RenderDecision,
    TextBlock,
)
from .parse import ParseRejected, safe_parse
from .pipeline import Parser, doc_id

__all__ = [
    "CharsetResult",
    "Config",
    "ExtractedLink",
    "LanguageResult",
    "LinkRel",
    "ParseRejected",
    "ParsedDocument",
    "Parser",
    "RenderDecision",
    "TemplateLearner",
    "TextBlock",
    "decode",
    "detect",
    "doc_id",
    "extract",
    "safe_parse",
]
__version__ = "0.1.0"
