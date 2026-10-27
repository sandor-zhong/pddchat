from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

from paths import OUTPUT_DIR
from appeal_style import evidence_style

# ============================================================
# 基础配置
# ============================================================
#
# OpenAI 客户端做懒加载：模块顶层不再直接实例化，避免只跑 Phase 1/3
# 也强制要求 DEEPSEEK_API_KEY。首次调用 get_client() 时才读环境变量、
# 校验缺失并抛错。

_client: OpenAI | None = None


def get_client() -> OpenAI:
    """懒加载 DeepSeek 客户端；缺 key 时给出明确错误。"""
    global _client
    if _client is not None:
        return _client

    load_dotenv()
    api_key = os.getenv("DEEPSEEK_API_KEY")
    if not api_key:
        raise RuntimeError(
            "缺少环境变量 DEEPSEEK_API_KEY。请在项目根目录的 .env 文件中配置："
            "\n  DEEPSEEK_API_KEY=<你的 key>"
        )

    _client = OpenAI(
        api_key=api_key,
        base_url="https://api.deepseek.com",
    )
    return _client

# 申诉凭证类型：AI 判定结果只允许是下面 3 种之一。
APPEAL_EVIDENCE_OPTIONS = [
    "消费者所述问题经核验不存在",
    "消费者误解商品信息",
    "消费者未表达商品问题",
]

# 本次查询不到聊天记录时（不代表其他渠道没有投诉或凭证），
# 不经过大模型判断，直接固定使用"消费者未表达商品问题"和固定申诉文案。
NO_CHAT_RECORD_LABEL = APPEAL_EVIDENCE_OPTIONS[2]

NO_CHAT_RECORD_APPEAL_TEXTS = (
    "这单的具体沟通情况还不清楚，我们对这笔负向体验判定有异议。",
    "这单买家具体反馈了什么还不清楚，我们对这笔负向体验判定有异议。",
    "这单具体是怎么沟通处理的还不清楚，我们对这笔负向体验判定有异议。",
)
# 保留旧常量名，供售后申诉等没有订单号的兼容调用使用。
NO_CHAT_RECORD_APPEAL_TEXT = NO_CHAT_RECORD_APPEAL_TEXTS[0]


def get_no_chat_appeal_text(order_no: str = "") -> str:
    """按订单号稳定选择自然话术；同一订单重复运行不会来回变化。"""
    if not order_no:
        return NO_CHAT_RECORD_APPEAL_TEXT
    digest = hashlib.sha256(order_no.encode("utf-8")).digest()
    return NO_CHAT_RECORD_APPEAL_TEXTS[digest[0] % len(NO_CHAT_RECORD_APPEAL_TEXTS)]

SYSTEM_PROMPT = f"""你是拼多多商家客服的助手，负责根据买家和客服的聊天记录，
判断这笔订单在提交差评/纠纷申诉时应该选择哪一种"申诉凭证"。

聊天记录会以压缩后的 JSON 格式给出，"m" 数组里每一项是 [角色, 内容]：
- B：买家（消费者）
- S：商家客服
- SYS：系统消息

重要原则：商家申诉时一律不承认商品本身存在质量问题。请从下面 3 个
选项中选出最符合聊天记录内容的一个：

1. {APPEAL_EVIDENCE_OPTIONS[0]}
   —— 买家反馈过具体问题，但没有提供照片/视频等有效凭证证明问题
   真实存在，问题始终没有被证实；或者商家已经要求买家提供凭证、
   买家未能提供，或商家已经主动给出处理方案（无论买家是否明确
   接受），整体上买家所反馈的问题并没有被实际验证坐实。

2. {APPEAL_EVIDENCE_OPTIONS[1]}
   —— 买家对商品信息、功能、适用范围或使用方式存在误解或理解错误，
   商品本身按详情页描述是正常的，买家反馈的"问题"其实是理解有误
   导致的。

3. {APPEAL_EVIDENCE_OPTIONS[2]}
   —— 买家全程只是咨询、提问或使用求助，没有明确反馈/描述商品存在
   问题（例如只问怎么用、是否兼容某型号、能不能用某配件等）。

只输出选中选项的完整文字，不要输出编号、解释或其他任何内容。
"""

APPEAL_TEXT_SYSTEM_PROMPT = """你根据本订单聊天和申诉凭证类型撰写负向申诉描述。
聊天为压缩JSON，m数组每项为[角色, 内容]，B=买家，S=客服，SYS=系统消息。
本次针对负向体验判定，不是货款或运费申诉，也不要称为复议。
有依据时解释规格或使用条件的理解分歧；仅有咨询时说明咨询内容；无法核实故障时
只写已有的沟通，不强行声称商品无问题。申诉凭证类型仅为分类参考，不是核验结论。
优先抓住能支撑商家立场的具体事实，说明我们为什么这样回应或处理，而非复述聊天流水。
讲清理由后直接结束，不加请平台处理、复核或划分责任的固定结尾。
信息很少时宁可简短，不为凑字数重复表达。正文含标点不超过120字。
""" + evidence_style(50, 110)


# ============================================================
# 人工粘贴的聊天记录 -> 压缩格式
# ============================================================
#
# 拼多多开放平台没有聊天记录接口，浏览器自动化又会触发风控，
# 所以聊天记录这一段改由人工提供：你在浏览器里正常看、正常复制，
# 粘贴给这个工具即可。下面负责把粘贴的纯文本转成分类需要的结构。

# 判断发言人是买家还是客服的关键词
BUYER_PREFIXES = ("买家", "消费者", "客户", "顾客", "B")
SELLER_PREFIXES = ("客服", "商家", "卖家", "我", "S")
SYSTEM_PREFIXES = ("系统", "系统消息", "SYS")

# 常见的"发言人:内容"分隔符，中英文冒号都要认
ROLE_SEPARATORS = ("：", ":")


def guess_role(speaker: str) -> str:
    """
    根据发言人名字猜角色。

    优先按关键词判断；都不匹配时退回原爬虫的规则——拼多多对买家
    昵称做隐私打码（例如 1***），客服/店铺名不会带这种星号。
    """

    name = speaker.strip()

    for prefix in SYSTEM_PREFIXES:
        if name.startswith(prefix):
            return "SYS"

    for prefix in BUYER_PREFIXES:
        if name.startswith(prefix):
            return "B"

    for prefix in SELLER_PREFIXES:
        if name.startswith(prefix):
            return "S"

    if "*" in name:
        return "B"

    return "S"


def parse_chat_text(text: str, order_no: str = "") -> dict:
    """
    把人工粘贴的聊天记录文本转成 compact 格式。

    支持的写法（每行一条，冒号中英文都行）：
        买家：这个线接触不良
        客服：亲亲可以提供一个小视频吗
        系统：机器人识别到消费者可能反馈商品质量问题

    没有冒号的行会被当作上一条发言的续行，接到上一条后面——
    聊天记录里换行的长句很常见，不能当成新发言丢掉角色。
    """

    compact_messages: list[list[str]] = []

    for raw_line in text.splitlines():
        line = raw_line.strip()

        if not line:
            continue

        speaker = ""
        content = ""

        for separator in ROLE_SEPARATORS:
            if separator in line:
                head, _, tail = line.partition(separator)

                # 冒号出现在很靠后的位置，多半是正文里的冒号，
                # 不是"发言人:内容"的分隔符
                if len(head.strip()) <= 12:
                    speaker = head.strip()
                    content = tail.strip()
                break

        if not speaker:
            # 续行：接到上一条后面
            if compact_messages:
                compact_messages[-1][1] += " " + line
            continue

        if not content:
            continue

        compact_messages.append([guess_role(speaker), content])

    return {
        "o": order_no,
        "m": compact_messages,
    }


def analyze_chat_text(text: str, order_no: str = "") -> tuple[str, str]:
    """
    对外主入口：粘贴的聊天记录文本 -> (申诉凭证类型, 申诉文案)。

    没有有效内容时按"无聊天记录"处理，返回固定文案，不调用大模型。
    """

    compact_data = parse_chat_text(text, order_no=order_no)

    if not compact_data["m"]:
        return NO_CHAT_RECORD_LABEL, get_no_chat_appeal_text(order_no)

    label = classify_appeal_evidence(compact_data)
    appeal_text = generate_appeal_text(compact_data, label)

    return label, appeal_text


# ============================================================
# 聊天记录压缩
# ============================================================

def classify_role(record: dict) -> str:
    """判断一条消息的角色：买家 / 客服 / 系统消息。"""

    if record["is_system_message"]:
        return "SYS"

    # 拼多多对买家昵称做了隐私打码（例如 "1***"），
    # 客服/店铺名称不会包含这种打码符号。
    if "*" in record["sender"]:
        return "B"

    return "S"


def simplify_content(record: dict) -> str:
    """精简单条消息内容，省 token 的同时保留分类需要的信号。"""

    content = record["content"].strip()

    if content.startswith("http://") or content.startswith("https://"):
        return "[链接]"

    if content:
        return content

    if record["image_urls"]:
        return "[图片]"

    return ""


def compact_chat_json(data: dict) -> dict:
    """把完整聊天记录转换成节省 token 的压缩格式。"""

    compact_messages = []

    for record in data["messages"]:
        text = simplify_content(record)

        if not text:
            continue

        compact_messages.append([
            classify_role(record),
            text,
        ])

    return {
        "o": data["order_no"],
        "m": compact_messages,
    }


# ============================================================
# 大模型分类
# ============================================================

def classify_appeal_evidence(compact_data: dict) -> str:
    """调用 DeepSeek，根据压缩后的聊天记录判断申诉凭证类型。"""

    user_content = json.dumps(
        compact_data,
        ensure_ascii=False,
    )

    resp = get_client().chat.completions.create(
        model="deepseek-v4-flash",
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
        temperature=0,
        max_tokens=60,
        extra_body={
            "thinking": {"type": "disabled"}
        },
        timeout=30,
    )

    usage = resp.usage
    label = resp.choices[0].message.content.strip()

    print(
        f"【Token消耗】输入:{usage.prompt_tokens} "
        f"输出:{usage.completion_tokens}"
    )

    if label not in APPEAL_EVIDENCE_OPTIONS:
        print(
            "警告：模型返回内容不在预设的 3 个选项中："
            f"{label!r}"
        )

    return label


def generate_appeal_text(compact_data: dict, evidence_label: str) -> str:
    """根据聊天记录和分类生成证据导向文案，最终限制在120字以内。"""

    user_payload = {
        "chat": compact_data,
        "申诉凭证类型": evidence_label,
    }

    user_content = json.dumps(
        user_payload,
        ensure_ascii=False,
    )

    resp = get_client().chat.completions.create(
        model="deepseek-v4-flash",
        messages=[
            {"role": "system", "content": APPEAL_TEXT_SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
        temperature=0.2,
        max_tokens=200,
        extra_body={
            "thinking": {"type": "disabled"}
        },
        timeout=30,
    )

    usage = resp.usage
    text = resp.choices[0].message.content.strip()

    print(
        f"【Token消耗】输入:{usage.prompt_tokens} "
        f"输出:{usage.completion_tokens}"
    )

    if len(text) > 120:
        print(
            f"警告：申诉文案超过120字（{len(text)}字），已自动截断："
            f"{text!r}"
        )
        candidate = text[:120]
        sentence_end = max(candidate.rfind(mark) for mark in "。！？")
        if sentence_end >= 70:
            text = candidate[:sentence_end + 1]
        else:
            text = candidate[:119].rstrip("，、；：") + "。"

    return text


# ============================================================
# 批处理入口
# ============================================================

def classify_order_file(input_file: Path) -> None:
    """读取单个订单的聊天记录文件，压缩后分类，并写回结果。"""

    data = json.loads(
        input_file.read_text(encoding="utf-8")
    )

    order_no = data["order_no"]

    if not data["messages"]:
        no_chat_text = get_no_chat_appeal_text(order_no)
        print(
            f"订单 {order_no} 没有聊天记录，"
            "不经过大模型判断，使用固定申诉文案。"
        )

        data["appeal_evidence_classification"] = NO_CHAT_RECORD_LABEL
        data["appeal_text"] = no_chat_text

        input_file.write_text(
            json.dumps(
                data,
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

        print(
            f"订单 {order_no} 分类结果：{NO_CHAT_RECORD_LABEL}"
            "（无聊天记录，未经过AI判断）"
        )
        print(f"订单 {order_no} 申诉文案：{no_chat_text}")
        print("-" * 70)
        return

    print(f"正在分类订单：{order_no}")

    compact_data = compact_chat_json(data)

    label = classify_appeal_evidence(compact_data)

    data["appeal_evidence_classification"] = label

    appeal_text = generate_appeal_text(compact_data, label)

    data["appeal_text"] = appeal_text

    input_file.write_text(
        json.dumps(
            data,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    print(f"订单 {order_no} 分类结果：{label}")
    print(f"订单 {order_no} 申诉文案：{appeal_text}")
    print("-" * 70)


def read_pasted_chat() -> str:
    """从控制台读入人工粘贴的聊天记录，空行两次或 Ctrl+Z/Ctrl+D 结束。"""

    print()
    print("=" * 70)
    print("请粘贴聊天记录，每行一条，格式示例：")
    print("    买家：这个线接触不良")
    print("    客服：亲亲可以提供一个小视频吗")
    print()
    print("粘贴完成后，连按两次回车开始分析。")
    print("=" * 70)

    lines: list[str] = []
    blank_count = 0

    while True:
        try:
            line = input()
        except EOFError:
            break

        if line.strip():
            blank_count = 0
            lines.append(line)
            continue

        blank_count += 1

        # 允许正文里出现单个空行，连续两个空行才认为输入结束
        if blank_count >= 2 and lines:
            break

        if lines:
            lines.append(line)

    return "\n".join(lines)


def batch_from_output_dir() -> None:
    """兼容旧流程：批量处理 output/ 里由爬虫产出的 JSON。"""

    input_files = sorted(
        OUTPUT_DIR.glob("chat_*.json")
    )

    if not input_files:
        print(f"没有在 {OUTPUT_DIR} 找到聊天记录文件。")
        return

    for input_file in input_files:
        classify_order_file(input_file)


def main() -> None:
    """
    默认走全自动流程：批量处理 output/ 目录里的聊天记录 JSON。

    带 --manual 参数时改走人工粘贴模式。这是给风控卡死、
    浏览器自动化跑不动时用的兜底，正常情况下用不到。
    """

    if "--manual" not in sys.argv:
        batch_from_output_dir()
        return

    order_no = input("订单编号（可留空，直接回车跳过）：").strip()

    text = read_pasted_chat()

    if not text.strip():
        print()
        print("没有读到聊天记录内容。")
        print(f"按无聊天记录处理：{NO_CHAT_RECORD_LABEL}")
        print(f"申诉文案：{NO_CHAT_RECORD_APPEAL_TEXT}")
        return

    print()
    print("正在分析……")

    label, appeal_text = analyze_chat_text(text, order_no=order_no)

    print()
    print("=" * 70)

    if order_no:
        print(f"订单编号：{order_no}")

    print(f"申诉凭证类型：{label}")
    print()
    print(f"申诉文案（{len(appeal_text)} 字）：")
    print(appeal_text)
    print("=" * 70)
    print()
    print("请复制上面的文案，粘贴到申诉表单中提交。")


if __name__ == "__main__":
    main()
