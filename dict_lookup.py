"""Kept so an existing GoldenDict entry that runs dict_lookup.py keeps working.

It behaves exactly like dict_full.py. New setups should use dict_full.py and dict_eng_only.py.
"""

import sys

from dict_core import run

if __name__ == "__main__":
    sys.exit(run(translate=True))
