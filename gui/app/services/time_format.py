"""把 db.py 里存的 UTC 时间字符串转成本地时区展示。

db.py 的 _now() 落库统一用 datetime.now(timezone.utc)（存 UTC 是正确的
设计——避免服务器/客户端时区不一致导致数据错乱），但直接把这种字符串
（如 "2026-08-15T04:28:46+00:00"）显示给用户看会跟他们的本地时钟对不
上（国内 UTC+8 会差 8 小时）。这个模块只管"展示层"的转换，不碰 db.py
本身的存储格式，也不影响任何用 UTC 做比较/筛选的业务逻辑（比如
reports_page.py 的时间范围筛选，那部分本来就是按 UTC-aware 方式正确
比较的，跟这里无关）。
"""

from __future__ import annotations

from datetime import datetime


def format_local(iso_str: str | None, fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    """把 ISO 8601 时间字符串（预期是 db.py 落库的 offset-aware UTC 格式）
    转成本地时区后按 fmt 格式化，不带时区后缀——风格上跟 CLI --status
    报表里"本地日期"的呈现方式一致，运营不需要自己心算时差。

    - iso_str 为 None/空字符串：返回空字符串（调用方决定是显示"—"还是
      直接留白，这里不替调用方做选择）。
    - 解析失败（格式不是预期的 ISO 8601）：原样返回输入值兜底，不能因为
      一条格式异常的时间就让整个表格/卡片渲染报错。
    """
    if not iso_str:
        return ""
    try:
        dt = datetime.fromisoformat(iso_str)
    except ValueError:
        return iso_str
    return dt.astimezone().strftime(fmt)
