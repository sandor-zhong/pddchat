from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
import re


class CollectionError(RuntimeError):
    """An actionable error; never silently report an incomplete crawl as complete."""


class Cancelled(Exception):
    pass


@dataclass(frozen=True)
class Shop:
    key: str
    name: str
    account: str
    avatar_url: str = ""


@dataclass(frozen=True)
class Appeal:
    order_sn: str
    goods_id: str
    goods_name: str
    goods_image_url: str
    issue_type: str
    compensation_cents: int | None
    compensation_text: str
    deduction_progress: str
    created_at: str
    status: str
    operation: str
    detail_url: str
    raw_columns: dict

    @property
    def identity(self):
        # The list does not expose a complaint ID. Do not deduplicate by order alone.
        return (self.order_sn, self.goods_id, self.issue_type, self.created_at)

    def to_dict(self):
        return asdict(self)


def parse_amount(text: str) -> int | None:
    cleaned = re.sub(r"[¥￥元,\s]", "", text)
    if cleaned in ("", "-", "--", "—"):
        return None
    try:
        value = Decimal(cleaned) * 100
        if not value.is_finite() or value != value.to_integral_value():
            raise ValueError
        if abs(value) > 10**15:
            raise ValueError
        return int(value)
    except (InvalidOperation, ValueError):
        raise CollectionError(f"无法识别补偿金额：{text!r}，本页未入库。") from None


def parse_rows(snapshot: dict) -> list[Appeal]:
    headers = snapshot["headers"]
    required = ("订单编号", "商品信息", "问题类型", "补偿金额（元）", "扣款进度", "创建时间", "处理状态", "操作")
    normalized = [re.sub(r"\s+", "", h).replace("(", "（").replace(")", "）") for h in headers]
    if any(h not in normalized for h in required):
        raise CollectionError(f"列表表头发生变化，无法安全采集：{headers}")
    indexes = {h: normalized.index(h) for h in required}
    result = []
    for row in snapshot["rows"]:
        cells = row["cells"]
        if len(cells) != len(headers):
            raise CollectionError("列表列数不一致，本页未入库。")
        values = {h: cells[i].strip() for h, i in indexes.items()}
        if values["处理状态"] != "申诉通过":
            raise CollectionError(f"查询结果含非“申诉通过”记录：{values['订单编号']}。请重新查询。")
        order = values["订单编号"]
        created = values["创建时间"]
        if not re.fullmatch(r"\d{6}-\d+", order):
            raise CollectionError(f"无法识别订单编号：{order!r}")
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}", created):
            raise CollectionError(f"无法识别创建时间：{created!r}")
        goods = row.get("goods", {})
        goods_id = goods.get("id", "")
        if not goods_id:
            match = re.search(r"ID\s*[:：]\s*(\d+)", values["商品信息"], re.I)
            goods_id = match.group(1) if match else ""
        if not goods_id:
            raise CollectionError(f"订单 {order} 缺少商品 ID，本页未入库。")
        goods_name = goods.get("name", "") or re.sub(r"ID\s*[:：]\s*\d+", "", values["商品信息"], flags=re.I).strip()
        issue = row.get("issue_type", "") or values["问题类型"].splitlines()[0]
        result.append(Appeal(
            order, goods_id, goods_name, goods.get("image", ""), issue,
            parse_amount(values["补偿金额（元）"]), values["补偿金额（元）"],
            values["扣款进度"], created, values["处理状态"], values["操作"],
            row.get("detail_url", ""), dict(zip(headers, cells)),
        ))
    return result
