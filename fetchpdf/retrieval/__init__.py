"""Format-prioritized retrieval.

The default retrieval path in ``fetch_pdf`` is PDF-shaped: it walks a
fixed list of sources and accepts the first thing with ``%PDF`` magic bytes.
That is the right default for a human who wants a paper to read.

It is the wrong default for feeding a table-extraction pipeline, because what
breaks there is not character accuracy but row/column/header association -- and
that association only survives losslessly in markup. This package inverts the
loop: format tiers outer, sources inner, so a JATS copy from a worse-ranked
source beats a PDF from a better-ranked one.

This package also holds the second, independent opt-in feature that grew out of
the same machinery: ``supplementary``, reached by ``--pull-supplementary``, which
fetches everything the authors deposited *alongside* the paper. It is not a tier.
The tier walk is winner-take-all by design -- one best representation of the full
text -- and "what else is there" is a set, not a winner, so it runs as a pass
after retrieval rather than as a rung inside it. It shares this package's HTTP
client, rate limiter and identifier resolver, which is why it lives here.

Nothing in here runs unless one of the opt-in flags is set. With none of them,
the existing chain executes byte-for-byte as before.
"""

from .tiers import Tier, Ladder, load_ladder
from .identifiers import IdentifierSet, ResolutionChain
from .artifact import Artifact

__all__ = [
    "Tier",
    "Ladder",
    "load_ladder",
    "IdentifierSet",
    "ResolutionChain",
    "Artifact",
]
