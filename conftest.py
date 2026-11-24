"""确保仓库根目录在 sys.path 中。

这样无论用 ``python -m unittest discover -s tests``、``pytest`` 还是
``python -m pytest`` 调用，tests 都能导入 ``washstation`` 与 ``scripts``。
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
