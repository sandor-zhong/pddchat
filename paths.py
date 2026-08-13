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
