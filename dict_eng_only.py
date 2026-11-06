r"""GoldenDict-NG program: English entry and pronunciation (no Persian translation).

Command line for a program dictionary (type Html):
    python "C:\Tools\goldendict-anki\dict_eng_only.py" %GDWORD%
"""

import sys

from dict_core import run

if __name__ == "__main__":
    sys.exit(run(translate=False))
