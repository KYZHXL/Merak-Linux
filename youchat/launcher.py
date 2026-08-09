"""启动分发器：读 settings.json 的 ui.mode，按形态启动 Web / Desktop / TUI。

用法：
    python -m youchat                 # 按 settings.json 的 ui.mode
    python -m youchat --mode web      # 覆盖为 web / desktop / tui
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .console.controller import AppController, VALID_MODES


def main(argv=None):
    parser = argparse.ArgumentParser(description="天璇 Merak 启动器")
    parser.add_argument("--mode", default=None, choices=list(VALID_MODES),
                        help="界面形态（覆盖 settings.json）")
    args = parser.parse_args(argv)

    controller = AppController()
    mode = args.mode or controller.get_settings().get("ui", {}).get("mode", "web")

    if mode == "tui":
        from .console import tui
        tui.main(controller)
    elif mode == "desktop":
        from .console import desktop
        desktop.main(controller)
    else:
        from .console import web
        web.main(controller)


if __name__ == "__main__":
    main()
