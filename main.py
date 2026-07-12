"""旧启动路径兼容层；新代码使用 ``kangel.main``。"""

from pathlib import Path
import sys


# 在尚未执行 editable install 的旧部署中继续支持 ``python main.py``。
_SRC = str(Path(__file__).resolve().parent / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from kangel.app.bootstrap import create_app  # noqa: E402
from kangel.main import app, run  # noqa: E402


if __name__ == "__main__":
    run()
