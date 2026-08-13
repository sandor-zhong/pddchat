"""项目路径与外链常量集中定义。

从 pdd_chat.py 抽出：这些常量与聊天页无关，多个模块都会用，
放在一起便于并行改造时统一切换（例如按 shop_id 分目录）。
"""

from __future__ import annotations

from pathlib import Path

# 聊天记录查询页 URL
START_URL = (
    "https://mms.pinduoduo.com/"
    "mms-chat/search?msfrom=mms_sidenav"
)

# 项目根目录 —— 所有派生目录都以此为基准。
BASE_DIR = Path(__file__).resolve().parent

# 浏览器登录状态保存位置（Playwright user_data_dir）
PROFILE_DIR = BASE_DIR / "state" / "pdd_chat_profile"

# 聊天记录及采集结果的输出目录
OUTPUT_DIR = BASE_DIR / "output"

# 查询失败截图、验证码调试信息保存位置
DEBUG_DIR = BASE_DIR / "debug"

# 逐页聊天记录凭证截图
EVIDENCE_DIR = BASE_DIR / "evidence"

# 单模块调试时使用的默认订单号
DEFAULT_ORDER_NO = "260430-189593086262417"
