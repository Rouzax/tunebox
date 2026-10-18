import sys

from tunebox.app import main
from tunebox.cli import run

sys.exit(run(sys.argv[1:]) if sys.argv[1:] else main())
