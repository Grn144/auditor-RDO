import sys
from pathlib import Path

# Garante que `import app` e `import auditar_relatorio` funcionem
# independente de como o pytest for invocado.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
