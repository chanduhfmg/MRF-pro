import sys
from pathlib import Path

# make `import app.*` work no matter where pytest is invoked from
sys.path.insert(0, str(Path(__file__).resolve().parent))
