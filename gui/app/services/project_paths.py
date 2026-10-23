"""解析/持久化"主项目在哪里"这件事：pddchat_root（仓库根目录）和
pddchat_python（主项目 3.12 venv 的 python.exe）。

这个模块是 GUI 跟主项目之间唯一的"路径粘合层"：
- 读 config.json 拿到这两个路径（相对路径按 gui/ 目录本身解析，方便仓库内
  开发时直接用默认值 "../"/"../.venv/Scripts/python.exe"，不用手改）；
- 把 pddchat_root 加进 sys.path，这样才能 `import db` / `import
  scheduler_core`（这两个模块本身没有任何第三方依赖，纯文件级 import，
  见它们各自模块文档的说明）；
- pddchat_python 只用来拼 subprocess 命令行（起 main.py --serve/
  --login-only），GUI 自己的解释器永远不会去跑主项目那些依赖 playwright
  的代码。

没有 config.json（比如第一次打开、还没执行"复制 config.example.json"
这一步）时用内置默认值兜底，不直接崩溃——降级到"假设 gui/ 就在仓库内部
标准位置"这个最常见的开发布局。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

GUI_DIR = Path(__file__).resolve().parent.parent.parent  # gui/
CONFIG_PATH = GUI_DIR / "config.json"

_DEFAULT_CONFIG = {
    "pddchat_root": "..",
    "pddchat_python": "../.venv/Scripts/python.exe",
}


def _load_raw_config() -> dict:
    if CONFIG_PATH.is_file():
        try:
            return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as error:
            print(f"[project_paths] 读取 {CONFIG_PATH} 失败（{error}），改用内置默认值。")
    return dict(_DEFAULT_CONFIG)


def _resolve(raw: str) -> Path:
    """相对路径按 gui/ 目录（而不是当前工作目录）解析——GUI 可能是
    双击图标、也可能是从任意目录用命令行启动的，不能依赖 cwd。"""
    p = Path(raw)
    return p if p.is_absolute() else (GUI_DIR / p).resolve()


class ProjectPaths:
    """单例式的路径解析结果，供其它模块 import 使用。"""

    def __init__(self) -> None:
        raw = _load_raw_config()
        self.root: Path = _resolve(raw.get("pddchat_root", _DEFAULT_CONFIG["pddchat_root"]))
        self.python: Path = _resolve(
            raw.get("pddchat_python", _DEFAULT_CONFIG["pddchat_python"])
        )
        self.main_py: Path = self.root / "main.py"

    def validate(self) -> list[str]:
        """检查关键路径是否存在，返回问题描述列表（空列表=一切正常）。
        调用方（app/main.py 启动时）应该把非空结果展示给用户，而不是让
        后续操作因为路径不对而报出难懂的异常。"""
        problems = []
        if not self.root.is_dir():
            problems.append(f"pddchat_root 不存在或不是目录：{self.root}")
        if not (self.root / "db.py").is_file():
            problems.append(f"pddchat_root 下没有找到 db.py：{self.root}")
        if not self.main_py.is_file():
            problems.append(f"pddchat_root 下没有找到 main.py：{self.main_py}")
        if not self.python.is_file():
            problems.append(f"pddchat_python 指向的解释器不存在：{self.python}")
        return problems


PROJECT_PATHS = ProjectPaths()

# 让 `import db` / `import scheduler_core` 能找到主项目根目录下的这两个
# 纯标准库模块——只加这一层 sys.path，绝不 import 任何依赖 playwright/
# paths.py 固化路径的模块（login.py/captcha.py/blockers.py/main.py 等），
# 见模块文档顶部的解耦说明。
if str(PROJECT_PATHS.root) not in sys.path:
    sys.path.insert(0, str(PROJECT_PATHS.root))
