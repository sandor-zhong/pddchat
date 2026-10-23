"""Feature names shared by navigation, execution controls and task history.

The existing persisted mode 'collect' remains the identifier for this feature,
so old jobs continue to display correctly without rewriting historical data.
New features must use a distinct mode and provide their own page/worker logic.
"""
from dataclasses import dataclass


APP_NAME = "拼多多店铺数据"


@dataclass(frozen=True)
class Feature:
    mode: str
    name: str
    description: str


NEGATIVE_APPEAL_BACKUP = Feature(
    mode="collect",
    name="负面申诉数据备份",
    description="筛选处理状态为“申诉通过”的记录，逐页采集并按店铺保存到本地数据库。",
)
SHOP_ONLINE_STATUS = Feature(
    mode="service_status",
    name="店铺在线状态查询",
    description="逐店查询客服账号的电脑/手机在线状态、回复指标，并接着查询最近90天违规信息。",
)
CUSTOMER_SERVICE_DATA = Feature(
    mode="customer_service",
    name="客服数据",
    description="逐店查询近1天客服数据，保存统计时间、消费者服务体验分、咨询及人工回复指标。",
)
APPEAL_AMOUNT = Feature(
    mode="appeal_amount",
    name="申诉金额",
    description="逐店查询近30日申诉成功金额和近30天负向申诉成功金额，并成套保存。",
)
AFTER_SALES_WORK_ORDER = Feature(
    mode="work_order_screenshot",
    name="售后工单截图",
    description="逐店筛选售后工单，截取指定内容区域，并保存“查询结果”中的工单数量供 Excel 导出。",
)
FEATURES = (NEGATIVE_APPEAL_BACKUP, SHOP_ONLINE_STATUS, CUSTOMER_SERVICE_DATA,
            APPEAL_AMOUNT, AFTER_SALES_WORK_ORDER)


def task_name(mode: str) -> str:
    if mode == "account_login":
        return "账号批量登录"
    if mode == "login":
        return "登录验证"
    if mode == "login_check":
        return "登录状态检查"
    return next((feature.name for feature in FEATURES if feature.mode == mode), f"未知功能（{mode}）")
