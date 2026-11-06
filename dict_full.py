r"""GoldenDict-NG program: Persian translation, English entry and pronunciation.

Command line for a program dictionary (type Html):
    python "C:\Tools\goldendict-anki\dict_full.py" %GDWORD%
"""

import sys

from dict_core import run

if __name__ == "__main__":
    sys.exit(run(translate=True))
