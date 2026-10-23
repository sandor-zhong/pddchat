"""项目路径与外链常量集中定义。

从 pdd_chat.py 抽出：这些常量与聊天页无关，多个模块都会用。

多店铺分片：读环境变量 PDDCHAT_SHOP，
- 有值：所有目录追加 <shop_slug> 子层，多店终端并跑互不干扰。
- 无值：走原来的单店路径（state/pdd_chat_profile、output/、debug/、
        evidence/），已有单店会话零迁移。

**入口脚本（main.py / run_appeal_batch.py / appeal_submit.py）必须在
`from paths import ...` 之前 从 sys.argv 抓 --shop 并写入
os.environ["PDDCHAT_SHOP"]**——paths.py 在 import 时就固化路径，
argparse 里再传就晚了。
"""

from __future__ import annotations

import os
import re
from pathlib import Path

# 聊天记录查询页 URL
START_URL = (
    "https://mms.pinduoduo.com/"
    "mms-chat/search?msfrom=mms_sidenav"
)

# 项目根目录 —— 所有派生目录都以此为基准。
BASE_DIR = Path(__file__).resolve().parent

# 打包安装版会把 Chromium 浏览器二进制随包带走，放在跟这份源码同级的
# browsers\ 目录下（见 packaging\build_runtime.ps1 用 PLAYWRIGHT_BROWSERS_
# PATH 把浏览器下载到这个位置、packaging\pddchat.iss 把它一起打进安装包），
# 这样同事装完之后双击就能用，不需要联网单独装一次浏览器。
# Playwright 找浏览器二进制靠 PLAYWRIGHT_BROWSERS_PATH 这个环境变量
# （官方支持的机制，不是自定义 hack）——这里只在这个目录确实存在时才设置，
# 且用 setdefault 不覆盖：开发环境没有这个目录，或者哪天开发者自己显式
# 设置过这个变量，都保持原来的行为（走 Playwright 默认的每用户缓存目录
# %LOCALAPPDATA%\ms-playwright\）不受影响。是否要真的读这个环境变量是
# Playwright 自己的驱动进程在真正打开浏览器时才做的事，这里只负责"提前
# 把值摆好"，不需要关心调用时机早晚。
_BUNDLED_BROWSERS_DIR = BASE_DIR / "browsers"
if _BUNDLED_BROWSERS_DIR.is_dir():
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(_BUNDLED_BROWSERS_DIR))


def _slugify_shop(raw: str) -> str:
    """把用户传入的 shop id 变成文件系统安全的目录名。

    保留字母/数字/下划线/连字符；其余全部换成下划线；
    去掉首尾多余下划线；退化为空则回落 default。
    """
    slug = re.sub(r"[^\w-]", "_", raw).strip("_")
    return slug or "default"


_shop_raw = os.environ.get("PDDCHAT_SHOP")
if _shop_raw:
    SHOP_ID: str | None = _slugify_shop(_shop_raw)
    PROFILE_DIR = BASE_DIR / "state" / "profiles" / SHOP_ID
    OUTPUT_DIR = BASE_DIR / "output" / SHOP_ID
    DEBUG_DIR = BASE_DIR / "debug" / SHOP_ID
    EVIDENCE_DIR = BASE_DIR / "evidence" / SHOP_ID
else:
    SHOP_ID = None
    # 老单店：保持向后兼容
    PROFILE_DIR = BASE_DIR / "state" / "pdd_chat_profile"
    OUTPUT_DIR = BASE_DIR / "output"
    DEBUG_DIR = BASE_DIR / "debug"
    EVIDENCE_DIR = BASE_DIR / "evidence"

# 单模块调试时使用的默认订单号
DEFAULT_ORDER_NO = "260430-189593086262417"
