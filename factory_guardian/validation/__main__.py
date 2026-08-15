"""``python -m factory_guardian.validation`` 進入點。

刻意不掛進 `cli.py`：外部驗證是**離線的一次性研究工作**，不是 Demo 流程的一部分，
掛進主 CLI 會讓「這個數字是不是 Demo 產生的」變得模糊。
"""

from __future__ import annotations

import sys

from .runner import main

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
