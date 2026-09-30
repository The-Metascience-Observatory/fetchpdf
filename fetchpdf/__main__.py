"""Allow `python -m fetchpdf` as an alias for the `fetchpdf` console script."""

import sys

from fetchpdf.fetchpdf import main

if __name__ == "__main__":
    sys.exit(main())
