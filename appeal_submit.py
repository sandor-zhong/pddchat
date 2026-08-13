from __future__ import annotations

import json
from pathlib import Path

from playwright.sync_api import (
    BrowserContext,
    Locator,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)

from stealth import (
    create_stealth_context,
    human_delay,
    human_type,
    jitter_wait,
    stealth_safe_click,
)

from pdd_chat import (
    BASE_DIR,
    DEBUG_DIR,
    EVIDENCE_DIR,
    OUTPUT_DIR,
    PROFILE_DIR,
    clear_blockers,
    ensure_no_captcha,
    get_query_button,
    is_captcha_visible,
    safe_click,
    safe_filename,
    save_error_screenshot,
    wait_for_login,
)


# ============================================================
# 基础配置
# ============================================================

APPEAL_SEARCH_URL = (
    "https://mms.pinduoduo.com/aftersales/customer_complain_appeal"
)

# 默认验证订单号（用户提供的真实示例，本身处于"可申诉"状态）
DEFAULT_TEST_ORDER_NO = "260803-561460027021919"

# 大模型判定结果 -> 页面上对应 radio 按钮的文本
#
# 页面上的 5 个 radio 选项：
#   商品有问题，但已与消费者达成一致
#   商品有问题，已补偿消费者
#   消费者未表达商品问题
#   商品无问题，消费者误解
#   其他
#
# "商品有问题…"两个选项会承认商品本身存在质量问题，与
# appeal_classifier.py 里"一律不承认商品本身存在质量问题"的
# 申诉原则直接冲突，不允许被自动化选中；"其他"未验证是否会
# 展开新的必填输入框，本次也不用。所以下表只映射到剩下两个：
# "商品无问题，消费者误解" 和 "消费者未表达商品问题"。
APPEAL_REASON_MAP = {
    "消费者所述问题经核验不存在": "商品无问题，消费者误解",
    "消费者误解商品信息": "商品无问题，消费者误解",
    "消费者未表达商品问题": "消费者未表达商品问题",
}

# 申诉详情页处理状态块（div.detail_status__2shmG）有三种形态，
# 点里面的"我要申诉"弹出的东西完全不同，必须先做区分：
#
# 状态 A：detail_statusText 里出现"当前平台未采取实际处理措施"，
#         点"我要申诉"直接弹出 header 为"发起申诉"的表单模态框。
#         表单使用 radio + textarea + 图片上传（有示例缩略图）。
# 状态 B：detail_statusName 里额外套 detail_confront_new_status 类，
#         但"我要申诉"是 <a> 链接（在 detail_status__2shmG 里）；
#         点"我要申诉"先弹出"确认要发起申诉吗"的二次确认框，
#         确认后再弹出表单模态框（radio 渐进渲染）。
# 状态 C：detail_statusName 里也有 detail_confront_new_status 类（同 B），
#         但"我要申诉"是 <button>（在 detail_statusText__2tLhY 里）；
#         点"我要申诉"打开一个新页面（非模态框），表单使用下拉
#         select + textarea + 图片上传，选了下拉才渐进渲染后续字段。
STATUS_A_TEXT_MARKER = "当前平台未采取实际处理措施"
STATUS_B_CLASS_MARKER = "detail_confront_new_status"
STATUS_ALREADY_APPEALED_MARKER = "已申诉"
STATUS_C_BUTTON_SELECTOR = (
    'div.detail_statusText__2tLhY '
    'button[data-testid="beast-core-button"]'
)

# 商品问题 A2 变体：申诉表单模态框里 #appealReasonCode 只有 2 个 radio
# （"商品没有问题" / "其他"），固定选"其他"；选中"其他"后
# #appealNote（申诉说明）与 #appealEvidenceImages（申诉凭证）才渐进
# 渲染出现。详情页状态块无法区分 A2 与状态 A（两者共用 #appealReasonCode
# 锚点、都是直接弹表单），只能在模态框打开后按 radio 数量+文字识别。
A2_APPEAL_REASON_GOODS_NO_ISSUE = "商品没有问题"
A2_APPEAL_REASON_OTHER = "其他"

# 申诉相关模态框识别用（仅识别，不用作点击 locator）
APPEAL_FORM_HEADER_TEXT = "发起申诉"
APPEAL_CONFIRM_MODAL_TITLE_FRAGMENT = "确认要发起申诉吗"

# 状态 B 二次确认框里"继续申诉"的按钮文字（用户明确要求一律走这条）
APPEAL_CONFIRM_CONTINUE_BUTTON_TEXT = "不认可，仍要申诉"

# 状态 B 表单里 3 个额外必选 radio 的目标文字
# —— 页面默认值就是这几个，代码里显式选中一遍是为了：
#    1) 万一 pdd 改默认，代码能第一时间发现（读回 data-checked 校验）；
#    2) 保持"填表这件事完全由代码控制"的语义清晰。
STATUS_B_ADMIT_PROBLEM_TEXT = "不认可商品问题"
STATUS_B_RECEIVE_GOOD_TEXT = "已收到退货商品"
STATUS_B_CAN_PROOF_TEXT = "可以提供凭证"

# 图片上传容器 id：三种状态各用了不同的 form-item id
APPEAL_IMAGE_CONTAINER_A = "appealEvidenceImages"
APPEAL_IMAGE_CONTAINER_B = "appealProof"
APPEAL_IMAGE_CONTAINER_C = "voucherImages"

# 取消按钮文字
APPEAL_CANCEL_BUTTON_TEXT = "取消"

# 提交按钮文字
APPEAL_SUBMIT_BUTTON_TEXT = "提交"

# "申诉须知"二次确认弹窗按钮文字
APPEAL_NOTICE_CONFIRM_TEXT = "确认并提交"


# ============================================================
# 搜索页：进入页面、填写订单编号、点击查询
# ============================================================

def open_appeal_search_page(page: Page) -> None:
    """进入拼多多客诉申诉搜索页（按订单编号搜索的入口）。"""

    print("正在进入客诉申诉搜索页……")

    try:
        page.goto(
            APPEAL_SEARCH_URL,
            wait_until="domcontentloaded",
            timeout=60_000,
        )

    except PlaywrightTimeoutError:
        # 拼多多后台可能存在接口长时间加载的情况。
        # goto 超时不一定表示页面完全打不开。
        print("页面导航超过 60 秒，继续检查页面内容。")

    # 给风控弹窗留出注入时间再检查
    jitter_wait(page, 1_500)

    clear_blockers(page)

    get_order_sn_input(page).wait_for(
        state="visible",
        timeout=30_000,
    )

    print("客诉申诉搜索页已就绪。")


def get_order_sn_input(page: Page) -> Locator:
    """获取订单编号搜索框。"""

    return page.get_by_placeholder("请输入完整订单编号")


def fill_appeal_search_order_no(
    page: Page,
    order_no: str,
) -> None:
    """在订单编号搜索框里填写订单编号。"""

    order_no = order_no.strip()

    if not order_no:
        raise ValueError("订单编号不能为空。")

    order_input = get_order_sn_input(page)

    order_input.wait_for(
        state="visible",
        timeout=20_000,
    )

    # 用模拟真人逐键输入替代 order_input.fill(order_no)
    # .fill() 是瞬间完成的，不产生 keydown/keyup 事件，易被风控识别
    human_type(page, order_input, order_no)

    current_value = order_input.input_value()

    if current_value != order_no:
        raise RuntimeError(
            "订单编号填写失败："
            f"期望值={order_no}，实际值={current_value}"
        )

    print(f"已输入订单编号：{order_no}")


# ============================================================
# 搜索结果等待
# ============================================================

def get_search_result_signature(page: Page) -> str:
    """
    获取当前搜索结果表格的特征（每行订单编号拼接）。

    用于判断点击查询后，结果是否真的发生了变化。
    """

    rows = page.locator(
        'tbody[data-testid="beast-core-table-middle-tbody"] tr'
    )

    if rows.count() == 0:
        return ""

    signatures = rows.evaluate_all(
        """
        (trs) => {
            return trs.map((tr) => {
                const firstCell = tr.querySelector(
                    'td[data-testid="beast-core-table-td"]'
                );

                return (firstCell?.textContent || "").trim();
            });
        }
        """
    )

    return "|".join(signatures)


def wait_for_search_result_updated(
    page: Page,
    previous_signature: str,
) -> bool:
    """等待搜索结果表格内容相对 previous_signature 发生变化。"""

    try:
        page.wait_for_function(
            """
            (previousSignature) => {
                const rows = Array.from(
                    document.querySelectorAll(
                        'tbody[data-testid="beast-core-table-middle-tbody"] tr'
                    )
                );

                if (rows.length === 0) {
                    return false;
                }

                const currentSignature = rows
                    .map((tr) => {
                        const firstCell = tr.querySelector(
                            'td[data-testid="beast-core-table-td"]'
                        );

                        return (firstCell?.textContent || "").trim();
                    })
                    .join("|");

                return currentSignature !== previousSignature;
            }
            """,
            arg=previous_signature,
            timeout=20_000,
        )

        jitter_wait(page, 500)

        return True

    except PlaywrightTimeoutError:
        return False


def search_appeal_order(
    page: Page,
    order_no: str,
) -> None:
    """按订单编号搜索，等待搜索结果表格更新完成。"""

    fill_appeal_search_order_no(page, order_no)

    query_button = get_query_button(page)

    query_button.wait_for(
        state="visible",
        timeout=20_000,
    )

    if not query_button.is_enabled():
        raise RuntimeError("查询按钮当前不可用。")

    previous_signature = get_search_result_signature(page)

    print(f"正在搜索订单：{order_no}")

    safe_click(
        page=page,
        locator=query_button,
        description="查询按钮",
    )

    # 点击查询会触发后端请求，是验证码高发时机
    clear_blockers(page)

    if not wait_for_search_result_updated(
        page=page,
        previous_signature=previous_signature,
    ):
        print(
            "点击查询后 20 秒内没有检测到结果变化，"
            "继续尝试读取当前页面结果。"
        )


# ============================================================
# 查看详情：定位链接、捕获新开页面
# ============================================================

def get_view_detail_link(
    page: Page,
    order_no: str,
) -> Locator:
    """
    获取指定订单那一行里的"查看详情"链接。

    先按订单编号定位到对应的 <tr>，再在这一行内查找"查看详情"，
    因为同一行里"问题类型"列还有一个"查看历史整改建议"链接，
    两者选择器完全相同（data-testid="beast-core-button-link"），
    必须靠文字区分，否则可能点错。
    """

    row = page.locator(
        'tbody[data-testid="beast-core-table-middle-tbody"] tr'
    ).filter(has_text=order_no)

    return row.locator(
        'a[data-testid="beast-core-button-link"]'
    ).filter(has_text="查看详情")


def _click_view_appealable_orders(page: Page) -> bool:
    """点击搜索页统计区的"查看可申诉订单"，等待表格数据刷新。

    场景：按订单编号查询后没找到"查看详情"（表格显示"暂无申诉记录"），
    页面顶部统计区会有"可申诉订单数 N 单"和"查看可申诉订单"入口，
    点击后会在当前页切换/刷新出可申诉订单列表，再从中定位该订单的
    "查看详情"。

    返回 True 表示已点击并等表格刷新；False 表示页面里根本没有该入口。
    """

    link = page.locator(
        'a[data-testid="beast-core-button-link"]'
    ).filter(
        has_text="查看可申诉订单"
    )

    try:
        link.wait_for(
            state="visible",
            timeout=8_000,
        )
    except PlaywrightTimeoutError:
        print("当前页面没有「查看可申诉订单」入口。")
        return False

    print("正在点击「查看可申诉订单」……")

    # 点击前记下当前表格签名，点击后等待它变化
    before_signature = get_search_result_signature(page)

    clear_blockers(page)

    safe_click(
        page=page,
        locator=link,
        description="查看可申诉订单",
    )

    wait_for_search_result_updated(
        page=page,
        previous_signature=before_signature,
    )

    jitter_wait(page, 500)
    return True


def open_appeal_detail_page(
    context: BrowserContext,
    page: Page,
    order_no: str,
) -> Page | None:
    """点击搜索结果中的"查看详情"，捕获随之新开的页面并返回。

    若查询后没找到"查看详情"（表格显示"暂无申诉记录"），会先点击
    "查看可申诉订单"再找一次；仍找不到则返回 None，表示该订单
    没有可申诉记录（由调用方决定跳过，不抛异常）。
    """

    detail_link = get_view_detail_link(page, order_no)

    try:
        detail_link.wait_for(
            state="visible",
            timeout=5_000,
        )
    except PlaywrightTimeoutError:
        print(
            f"订单 {order_no} 查询后未找到「查看详情」"
            "（表格可能显示暂无申诉记录），尝试点击「查看可申诉订单」……"
        )

        if not _click_view_appealable_orders(page):
            print(f"订单 {order_no} 视为找不到申诉记录。")
            return None

        try:
            detail_link.wait_for(
                state="visible",
                timeout=5_000,
            )
        except PlaywrightTimeoutError:
            print(
                f"订单 {order_no} 点击「查看可申诉订单」后仍无「查看详情」，"
                "视为找不到申诉记录。"
            )
            return None

    if detail_link.count() != 1:
        raise RuntimeError(
            f"订单 {order_no} 对应的「查看详情」链接数量异常："
            f"{detail_link.count()}"
        )

    print(f"正在点击订单 {order_no} 的查看详情……")

    # 这里的点击不能用 safe_click：它包在 expect_page 上下文里，
    # 重试会破坏上下文管理器的语义（可能捕获到错误的新页面）。
    # 改成进入 with 之前先把障碍清干净，块内保持一次普通点击。
    clear_blockers(page)

    with context.expect_page() as new_page_info:
        # 点击前加入微小的拟人反应延迟
        human_delay(80, 250)
        detail_link.click()

    detail_page = new_page_info.value

    detail_page.wait_for_load_state()

    print(f"已打开新页面：{detail_page.url}")

    # 实测这个新开的详情页正是验证码高发的位置，
    # 而且要检查的是新页面本身，不是原来的搜索页。
    jitter_wait(detail_page, 1_500)

    clear_blockers(detail_page)

    return detail_page


def search_and_open_appeal_detail(
    context: BrowserContext,
    page: Page,
    order_no: str,
) -> Page | None:
    """
    按订单编号搜索并打开对应的申诉详情页（新开的页面）。

    若查询后没找到"查看详情"（表格显示"暂无申诉记录"），会先点击
    "查看可申诉订单"再找一次；仍找不到则返回 None，表示该订单
    没有可申诉记录（由调用方决定跳过）。

    这是对外的主入口：未来 run_appeal_batch.py 批量处理已分析订单
    时，会对每个订单号依次调用这个函数。
    """

    search_appeal_order(page, order_no)

    return open_appeal_detail_page(
        context=context,
        page=page,
        order_no=order_no,
    )


# ============================================================
# 申诉模态框：点击"我要申诉" → 填写表单 → 上传凭证
# ============================================================

def collect_evidence_files(order_no: str) -> list[Path]:
    """
    收集指定订单的申诉凭证截图。

    搜索策略（按优先级）：
    1. output/chat_<safe_order_no>.json 里的 evidence_screenshots
       字段——这是 pdd_chat.py 抓聊天记录时写入的权威截图清单，
       路径按项目根 BASE_DIR 相对存放。
    2. 回退：evidence/<safe_order_no>/ 子目录下所有 .png。
    3. 回退：evidence/<safe_order_no>*.png 平铺文件。

    找不到任何图片时返回空列表；由调用方（fill_appeal_modal）
    在打开 modal 之前就判断空列表并抛错。
    """

    name = safe_filename(order_no)

    # 1) chat json 里的 evidence_screenshots
    json_path = OUTPUT_DIR / f"chat_{name}.json"
    if json_path.exists():
        try:
            data = json.loads(json_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            data = {}

        screenshots = data.get("evidence_screenshots") or []
        files = [BASE_DIR / rel_path for rel_path in screenshots]
        files = [p for p in files if p.is_file()]
        if files:
            return files

    # 2) evidence/<name>/ 子目录
    evidence_dir = EVIDENCE_DIR / name
    if evidence_dir.is_dir():
        files = sorted(evidence_dir.glob("*.png"))
        if files:
            return files

    # 3) 平铺文件
    return sorted([
        p for p in EVIDENCE_DIR.glob(f"{name}*.png")
        if p.is_file()
    ])


def detect_detail_page_status(detail_page: Page) -> str:
    """判断当前申诉详情页处理状态属于 "A"、"B"、"C" 还是 "ALREADY_APPEALED"。

    通过多处锚点交叉判断：

    - detail_statusText 里是否出现"当前平台未采取实际处理措施"
      （只有状态 A 会出现）；
    - detail_statusName 的 class 列表里是否包含
      detail_confront_new_status（状态 B 和 C 都有）；
    - B 和 C 的进一步区分：如果"我要申诉"是 <button> 且在
      detail_statusText__2tLhY 里，则为 C；如果是 <a> 且在
      detail_status__2shmG 里，则为 B；
    - 若 class 锚点均不匹配但文本中出现"我要申诉"（如带倒计时
      的可申诉判定页，CSS 哈希可能不同），则通过 DOM 结构回退
      判断 B/C，不直接抛错。

    三处锚点交叉校验仍冲突时抛 RuntimeError，绝不静默猜测。
    """

    status_block = detail_page.locator("div.detail_status__2shmG")
    status_block.wait_for(
        state="visible",
        timeout=15_000,
    )

    text_block = status_block.locator("div.detail_statusText__2tLhY")
    text_content = (
        text_block.inner_text()
        if text_block.count() > 0
        else ""
    )

    name_block = status_block.locator("div.detail_statusName__VnpUu")
    class_list = ""
    if name_block.count() > 0:
        class_list = name_block.first.get_attribute("class") or ""

    # 读取整个 status_block 的文本（含 statusName 区域），用于检测"已申诉"
    full_status_text = (
        status_block.inner_text() if status_block.count() > 0 else ""
    )

    has_a_marker = STATUS_A_TEXT_MARKER in text_content
    has_b_marker = STATUS_B_CLASS_MARKER in class_list

    # "已申诉"状态（详情页显示"已申诉"+"申诉详情"按钮，无需再模拟）
    if STATUS_ALREADY_APPEALED_MARKER in full_status_text:
        return "ALREADY_APPEALED"

    if has_a_marker and not has_b_marker:
        return "A"

    if has_b_marker and not has_a_marker:
        # B 和 C 都有 detail_confront_new_status，需要进一步区分：
        # C 的"我要申诉"是一个 <button>，嵌在 detail_statusText 里
        c_button = detail_page.locator(STATUS_C_BUTTON_SELECTOR).filter(
            has_text="我要申诉",
        )
        if c_button.count() > 0:
            return "C"
        return "B"

    # 锚点标记都不匹配，但文本中含有"我要申诉"（可能是 class 哈希变化
    # 导致的漏判，比如带倒计时的可申诉判定页），通过 DOM 结构回退判断
    if "我要申诉" in text_content:
        c_button = detail_page.locator(STATUS_C_BUTTON_SELECTOR).filter(
            has_text="我要申诉",
        )
        if c_button.count() > 0:
            return "C"
        # 无 button 形态的"我要申诉"则按 B 处理（模态框弹窗）
        return "B"

    raise RuntimeError(
        "无法判断申诉详情页的处理状态：\n"
        f"  detail_statusName class = {class_list!r}\n"
        f"  detail_statusText = {text_content!r}"
    )


def get_appeal_button(page: Page) -> Locator:
    """详情页处理状态块里的"我要申诉"链接按钮。

    限定在 div.detail_status__2shmG 块内，避免误击页面其它
    带"申诉"字样的链接（例如页脚说明、其它模块的申诉入口等）。
    """

    return page.locator(
        'div.detail_status__2shmG a[data-testid="beast-core-button-link"]',
    ).filter(
        has_text="我要申诉",
    )


def click_appeal_button(page: Page) -> None:
    """在详情页上点击"我要申诉"，打开申诉相关模态框。

    A/B 两种状态都会走这个函数，函数本身不做状态判断——
    是否要接着处理二次确认框、进哪个表单，由 fill_appeal_modal
    顶层根据 detect_detail_page_status 的结果决定。

    此时 modal 还没打开，用 safe_click 是安全的：它内部的
    clear_blockers 只会关掉页面上真正的通知型弹窗。
    """

    button = get_appeal_button(page)

    button.wait_for(
        state="visible",
        timeout=20_000,
    )

    safe_click(
        page=page,
        locator=button,
        description="我要申诉按钮",
    )

    print("已点击「我要申诉」按钮。")


# ============================================================
# 状态 C：按钮定位与点击（打开新页面）
# ============================================================

def get_appeal_button_c(page: Page) -> Locator:
    """状态 C 详情页里的"我要申诉"按钮（<button> 类型，在 detail_statusText 里）。

    与 A/B 不同，C 的按钮不在 detail_status__2shmG 块里，而是在
    detail_statusText__2tLhY 里作为 <button data-testid="beast-core-button">。
    """
    return page.locator(STATUS_C_BUTTON_SELECTOR).filter(
        has_text="我要申诉",
    )


def click_appeal_button_c(context: BrowserContext, page: Page) -> Page:
    """状态 C 场景：点击"我要申诉"并捕获新打开的申诉表单页面。

    与 A/B 不同，C 点击不是弹出模态框，而是打开一个全新的独立页面。
    通过 context.expect_page() 捕获这个新页面并返回。

    返回：新打开的申诉表单 page（非模态框）。
    """
    button = get_appeal_button_c(page)

    button.wait_for(
        state="visible",
        timeout=20_000,
    )

    if button.count() != 1:
        raise RuntimeError(
            f"状态 C「我要申诉」按钮定位异常：匹配数量={button.count()}"
        )

    print("状态 C：正在点击「我要申诉」（预期打开新页面）……")

    clear_blockers(page)
    # 微小的拟人随机延迟（不在 expect_page 上下文内做 safe_click）
    human_delay(100, 300)

    with context.expect_page() as new_page_info:
        button.click()

    form_page = new_page_info.value

    form_page.wait_for_load_state()

    print(f"状态 C 申诉表单页面已打开：{form_page.url}")

    jitter_wait(form_page, 1_500)
    clear_blockers(form_page)

    return form_page


# ============================================================
# 状态 C：下拉 select 选择 + 渐进渲染等待
# ============================================================

def wait_for_appeal_form_page(form_page: Page) -> None:
    """等待状态 C 的独立申诉表单页面渲染完成。

    锚点是 #appealReason（下拉 select），因为这是 C 表单最顶部
    的 form-item，在页面加载完成后最先出现。
    """
    form_page.locator("#appealReason").wait_for(
        state="visible",
        timeout=30_000,
    )
    print("状态 C 申诉表单页面已就绪。")


def select_appeal_reason_dropdown(
    page: Page,
    classification: str,
) -> None:
    """状态 C 场景：在 #appealReason 下拉 select 中选择申诉原因。

    C 的 appealReason 是 beast-core-select 组件（非 radio），操作分四步：
    1. 点击 select header 展开下拉面板
    2. 等待下拉面板出现
    3. 点击对应 <li role="option"> 选项（用 has_text 匹配，兼容"消费者未表达
       商品问题（含售后详情内未描述问题等）"这种带括号后缀的长文本）
    4. 等待渐进渲染：选择后 #voucherImages 和 #appealDescription 才会出现

    点击完选项后立即等渐进渲染完成，把两次 wait 合并到一个函数里，
    让调用方拿到后可以直接往下填。
    """
    radio_text = APPEAL_REASON_MAP.get(classification)
    if radio_text is None:
        raise ValueError(
            f"未知的申诉分类：{classification!r}，"
            f"允许的值为：{list(APPEAL_REASON_MAP)}"
        )

    # 1. 打开下拉面板
    select_header = page.locator(
        '#appealReason [data-testid="beast-core-select-header"]'
    )
    select_header.wait_for(state="visible", timeout=10_000)

    # modal 还没打开，这里点击可以用 safe_click
    safe_click(
        page=page,
        locator=select_header,
        description="申诉原因下拉框",
    )

    # 2. 等待下拉面板展开
    dropdown_panel = page.locator(
        'ul[role="listbox"].ST_dropdownPanel_5-193-0'
    )
    dropdown_panel.wait_for(
        state="visible",
        timeout=5_000,
    )

    # 3. 点击选项（用 has_text 模糊匹配，不要求完全一致）
    target_option = dropdown_panel.locator(
        'li[role="option"]'
    ).filter(has_text=radio_text)

    if target_option.count() != 1:
        raise RuntimeError(
            f"状态 C 下拉选项定位异常："
            f"radio_text={radio_text!r}，"
            f"匹配数量={target_option.count()}"
        )

    stealth_safe_click(
        page=page,
        locator=target_option,
        description=f"下拉选项：{radio_text}",
    )

    # 校验是否选中（检查 active 类或 checked 状态）
    checked = target_option.get_attribute("data-checked")
    if checked != "true":
        raise RuntimeError(
            f"状态 C 下拉选项点击后未选中："
            f"radio_text={radio_text!r}，"
            f"data-checked={checked!r}"
        )

    print(f"状态 C 已选择申诉原因：{radio_text}")

    # 4. 等待渐进渲染：选了下拉后，#voucherImages 和 #appealDescription 才会出现
    page.locator(f"#{APPEAL_IMAGE_CONTAINER_C}").wait_for(
        state="visible",
        timeout=10_000,
    )
    page.locator("#appealDescription").wait_for(
        state="visible",
        timeout=5_000,
    )
    print("状态 C 渐进渲染完成：凭证上传区和申诉描述区已出现。")


# ============================================================
# 商品问题 A2 变体：模态框识别
# ============================================================

def is_status_a2_modal(modal: Locator) -> bool:
    """判断模态框是否为"商品问题 A2 变体"。

    商品问题还有一种状态：点"我要申诉"直接弹出表单模态框（同状态 A），
    但 #appealReasonCode 里只有 2 个 radio——"商品没有问题"和"其他"，
    且必须选"其他"后 #appealNote（申诉说明）与 #appealEvidenceImages
    （申诉凭证）才渐进渲染出现。详情页状态块无法区分 A2 与状态 A
    （两者共用 #appealReasonCode 锚点），所以只能在模态框打开后用
    radio 数量 + 文字识别，避免把状态 A（5 个 radio）误判。

    返回 True 表示是 A2 变体。
    """

    radios = modal.locator(
        '#appealReasonCode label[data-testid="beast-core-radio"]'
    )

    if radios.count() != 2:
        return False

    texts = [r.inner_text().strip() for r in radios.all()]
    return texts == [
        A2_APPEAL_REASON_GOODS_NO_ISSUE,
        A2_APPEAL_REASON_OTHER,
    ]


# ============================================================
# 状态 B：二次确认框处理
# ============================================================

def handle_appeal_confirm_modal(page: Page) -> bool:
    """状态 B 场景：处理"确认要发起申诉吗？"二次确认框。

    点"我要申诉"之后，状态 B 会先弹出 AppealConfirmModal（无
    MDL_header，body 里标题片段固定含 "确认要发起申诉吗"）。
    用户明确要求一律选"不认可，仍要申诉"进入正式的申诉表单。

    必须等这个 confirm modal 完全消失再返回——否则紧接着的
    wait_for_appeal_modal 里的"检测到二次确认框就抛错"防呆
    会被残留元素误触发。

    返回 True 表示正常处理了二次确认框；返回 False 表示等不到
    二次确认框但检测到商品问题 A2 表单（详情页被误判为 B），
    调用方应降级按 A2 流程处理。
    """

    confirm_modal = page.locator(
        '[data-testid="beast-core-modal-innerWrapper"]',
    ).filter(
        has_text=APPEAL_CONFIRM_MODAL_TITLE_FRAGMENT,
    )

    try:
        confirm_modal.wait_for(
            state="visible",
            timeout=15_000,
        )
    except PlaywrightTimeoutError:
        # 兜底：商品问题 A2 变体没有"确认要发起申诉吗"二次确认框，
        # 点"我要申诉"直接弹出表单。若详情页状态块被误判为 B
        # （detail_confront_new_status 类），这里会等不到二次确认框——
        # 检测到 A2 表单（2 个 radio）就降级为 A2 流程，避免整个批次中断。
        a2_modal = page.locator(
            '[data-testid="beast-core-modal-innerWrapper"]:visible'
        ).first
        if is_status_a2_modal(a2_modal):
            print(
                "未等到二次确认框，但检测到商品问题 A2 表单"
                "（申诉原因仅'商品没有问题/其他'），按 A2 变体继续。"
            )
            return False
        raise

    print("二次确认框已弹出，准备点击「不认可，仍要申诉」……")

    button = confirm_modal.locator("button").filter(
        has_text=APPEAL_CONFIRM_CONTINUE_BUTTON_TEXT,
    )

    if button.count() != 1:
        raise RuntimeError(
            f"二次确认框里\"{APPEAL_CONFIRM_CONTINUE_BUTTON_TEXT}\""
            f"按钮定位异常：匹配数量={button.count()}"
        )

    # modal 已经打开，不能用 safe_click（clear_blockers 会把
    # 这个 modal 本身当通知型弹窗关掉）——用 stealth_safe_click，
    # 只带拟人延迟 + 重试，不做 clear_blockers。
    stealth_safe_click(
        page=page,
        locator=button,
        description=f"二次确认框：{APPEAL_CONFIRM_CONTINUE_BUTTON_TEXT}",
    )

    # 关键：等 confirm modal 完全消失。用较长的超时，因为动画
    # 结束 + 后端请求返回都可能耗时。
    confirm_modal.wait_for(
        state="hidden",
        timeout=15_000,
    )

    print("二次确认框已关闭，等待发起申诉表单出现……")
    return True


def wait_for_appeal_modal(page: Page, status: str) -> Locator:
    """等待"发起申诉"表单模态框渲染完成，返回 modal locator。

    锚点根据状态选择——这是因为**状态 B 的表单是渐进渲染的**：
    刚打开时只有 #admitGoodProblem 一个 form-item 可见，其它
    form-item 是随着用户依次选完前一个才依次出现。所以：
    - 状态 A：用 #appealReasonCode 作为锚点（A 表单一次性渲染，
      appealReasonCode 是表单里最上面的 form-item）；
    - 状态 B：用 #admitGoodProblem 作为锚点（B 的阶段 1 就存在，
      这也是 B 表单最上面的 form-item）。

    另做一层显式防呆：如果冒出**可见的**二次确认框，说明状态
    检测漏了或页面出了什么意外，及早抛错，不要让后续填表函数
    去撞不存在的字段。判断"是否有二次确认框"必须看**可见**
    元素，不能只看 DOM——pdd 关闭模态框可能是 display:none
    而不是移除 DOM，B 场景走完 handle_appeal_confirm_modal 后
    confirm modal 的 DOM 可能残留，用 :visible 伪类过滤。
    """

    if status == "A":
        anchor_selector = "#appealReasonCode"
    elif status == "B":
        anchor_selector = "#admitGoodProblem"
    else:
        raise ValueError(
            f"wait_for_appeal_modal 未知 status: {status!r}"
        )

    modal = page.locator(
        '[data-testid="beast-core-modal-innerWrapper"]',
    ).filter(
        has=page.locator(anchor_selector),
    )

    modal.wait_for(
        state="visible",
        timeout=15_000,
    )

    visible_confirm_count = page.locator(
        '[data-testid="beast-core-modal-innerWrapper"]:visible',
    ).filter(
        has_text=APPEAL_CONFIRM_MODAL_TITLE_FRAGMENT,
    ).count()

    if visible_confirm_count > 0:
        raise RuntimeError(
            "检测到可见的二次确认框（\"确认要发起申诉吗\"），"
            "而不是预期的发起申诉表单。订单可能处于状态 B 但"
            "未走 handle_appeal_confirm_modal，或页面 DOM 已变化，"
            "请检查。"
        )

    jitter_wait(page, 800)
    print(f"发起申诉表单模态框已弹出（锚点={anchor_selector}）。")

    return modal


def select_form_radio(
    page: Page,
    modal: Locator,
    form_item_id: str,
    radio_text: str,
) -> None:
    """在 modal 的某个 form-item 里，按文字选中 radio。

    这是一个通用工具：状态 A 的 appealReasonCode、状态 B 的
    admitGoodProblem / receiveGood / appealReasonCode / canProof
    都通过它来选。

    行为：**永远显式点击**（stealth_safe_click），点完读回
    data-checked 校验。

    为什么不做"已默认选中就跳过"的幂等优化：状态 B 表单是渐进
    渲染的（`#admitGoodProblem` 选完才展开 `#receiveGood`，
    `#receiveGood` 选完才展开后面的字段），即使 pdd 前端默认给
    了目标值，也**必须显式点击一次**来触发下一阶段的渲染，跳过
    点击 → 后续 form-item 永远不出现。状态 A 的 radio 本来就都
    未选中，点击也没有语义差异。所以对 A/B 都统一"永远点击"。

    模态框打开后所有点击都必须用 stealth_safe_click，不能用
    safe_click——后者内部的 clear_blockers 会把当前 modal 本身
    当作"通知型弹窗"关掉（modal 也是 data-testid="beast-core-modal"、
    右上角也有 modal-icon-close）。
    """

    radio = modal.locator(
        f'#{form_item_id} label[data-testid="beast-core-radio"]',
    ).filter(has_text=radio_text)

    # 先等 radio 可见——B 场景的渐进渲染下，后续 form-item 是
    # 前一次点击触发展开的，wait_for 兜住这个时序。
    radio.wait_for(
        state="visible",
        timeout=10_000,
    )

    if radio.count() != 1:
        raise RuntimeError(
            f"radio 定位异常：form-item=#{form_item_id}，"
            f"文字={radio_text!r}，匹配数量={radio.count()}"
        )

    stealth_safe_click(
        page=page,
        locator=radio,
        description=f"#{form_item_id} 单选：{radio_text}",
    )

    checked = radio.get_attribute("data-checked")
    if checked != "true":
        raise RuntimeError(
            f"radio 点击后未选中：form-item=#{form_item_id}，"
            f"文字={radio_text!r}，data-checked={checked!r}"
        )

    print(f"已选择 #{form_item_id}：{radio_text}")


def select_appeal_reason(
    page: Page,
    modal: Locator,
    classification: str,
) -> None:
    """状态 A 场景：按分类查 APPEAL_REASON_MAP 映射到 radio 文字后选中。

    状态 A 的 appealReasonCode 有 5 个 radio 选项，需要通过
    APPEAL_REASON_MAP 把 classifier 的 3 个分类映射到其中 2 个 radio；
    状态 B 的 appealReasonCode 有 3 个 radio 选项，文字完全等于
    classifier 的 3 个分类字面，不走本函数，直接用 select_form_radio
    传 classification 原文。
    """

    radio_text = APPEAL_REASON_MAP.get(classification)

    if radio_text is None:
        raise ValueError(
            f"未知的申诉分类：{classification!r}，"
            f"允许的值为：{list(APPEAL_REASON_MAP)}"
        )

    select_form_radio(
        page=page,
        modal=modal,
        form_item_id="appealReasonCode",
        radio_text=radio_text,
    )


def fill_appeal_note(
    page: Page,
    modal: Locator | Page,
    text: str,
    form_item_id: str = "appealNote",
    use_human_type: bool = True,
) -> None:
    """在申诉说明 textarea 里填入申诉文案。

    以 form_item_id 为根定位 textarea，避免匹配到页面其它组件
    的同类元素。填完读回 input_value 校验。

    - A/B 场景：form_item_id 为 "appealNote"（模态框内），用拟人打字
    - C 场景：form_item_id 为 "appealDescription"（独立页面内），
      用 fill() 直接写入（避免 keyboard.type 触发页面事件导致异常）

    参数:
        use_human_type: True 拟人逐字打字，False 用 fill() 直接写入
    """

    textarea = modal.locator(
        f'#{form_item_id} textarea[data-testid="beast-core-textArea-htmlInput"]',
    )

    textarea.wait_for(
        state="visible",
        timeout=10_000,
    )

    if use_human_type:
        human_type(page, textarea, text)
    else:
        # C 场景：独立页面的 textarea 可能有 input 事件处理器，
        # keyboard.type() 逐字触发事件可能导致意外导航/弹窗。
        # fill() 直接设置值，只触发一次 change 事件，更安全。
        textarea.click()
        human_delay(100, 300)
        textarea.fill(text)
        human_delay(100, 300)

    current = textarea.input_value()
    if current != text:
        raise RuntimeError(
            "申诉说明填写内容与预期不一致：\n"
            f"  期望：{text!r}\n"
            f"  实际：{current!r}"
        )

    print(f"已填写申诉说明（{len(text)} 字）")


def upload_evidence_images(
    page: Page,
    modal: Locator | Page,
    file_paths: list[Path],
    container_id: str = APPEAL_IMAGE_CONTAINER_A,
    example_thumbnail_count: int | None = 1,
) -> None:
    """上传申诉凭证截图（三种场景共用）。

    三种场景的凭证上传结构类似，差异用参数区分：
    - 上传容器 form-item id：A=appealEvidenceImages，B=appealProof，C=voucherImages
    - 示例缩略图：A 有 1 张占位，B 没有（0），C 走 MmsUiMmsUpload 组件
      没有 beast-core-thumbnail，传 None 跳过缩略图等待。

    定位真正接受用户文件的 input：容器里可能存在多个
    input[data-testid="beast-core-upload-input"]（装饰/示例
    对应的没有 accept 属性），只有真正上传用的那个带
    accept=".jpg,.jpeg,.png,.bmp,.jps,.webp"，用 accept 属性
    作为选择器精确锁定。
    input[data-testid="beast-core-upload-input"]（装饰/示例
    对应的没有 accept 属性），只有真正上传用的那个带
    accept=".jpg,.jpeg,.png,.bmp,.jps,.webp"，用 accept 属性
    作为选择器精确锁定。

    上传上限 3 张，超过取前 3 张并打印警告。

    副作用（重要）：set_input_files 会立刻触发前端把文件上传到
    拼多多的图片服务器（commimg.pddpic.com/upload/aftersales/...
    这类 URL），即使后续不点"提交"，文件也已经在 pdd 服务器上。
    这不等于"提交申诉"——最终提交仍需人工在浏览器里点"提交"。
    """

    if not file_paths:
        raise RuntimeError(
            "申诉凭证图片列表为空，无法上传。"
            "申诉表单里凭证是必填项，请先确认已生成聊天记录截图。"
        )

    file_input = modal.locator(
        f'#{container_id} '
        'input[data-testid="beast-core-upload-input"]'
        '[accept*=".jpg"]',
    )

    # 状态 B 里 #appealProof 是渐进渲染的（选完 #receiveGood 才
    # 出现），走到 upload 时按调用顺序它应该已经在，但显式 wait
    # 更稳。用 state="attached" 而不是 "visible"，因为真正的
    # file input 有 style="display: none;"，永远不会 visible。
    file_input.wait_for(
        state="attached",
        timeout=10_000,
    )

    if file_input.count() != 1:
        raise RuntimeError(
            f"申诉凭证上传 input 定位异常：容器=#{container_id}，"
            f"匹配数量={file_input.count()}"
        )

    paths = file_paths[:3]
    if len(file_paths) > 3:
        print(
            f"警告：申诉凭证图片共 {len(file_paths)} 张，"
            "超过上限 3 张，只取前 3 张。"
        )

    paths_str = [str(p.resolve()) for p in paths]

    # 上传 input 可能是 non-multiple（不带 multiple 属性），此时
    # set_input_files 一次只能接受 1 个文件。PDD 上传组件是"逐个选择
    # 追加"模式：每次 change 事件把新文件追加进已选列表并清空 input
    # value，因此对每个文件分别 set_input_files 即可传多张图。
    multiple = file_input.get_attribute("multiple")
    if multiple is not None:
        file_input.set_input_files(paths_str)
    else:
        for single in paths_str:
            file_input.set_input_files(single)

    # 等容器里出现对应数量的缩略图作为上传完成信号。
    # A：容器里本来有 1 个"示例"占位 thumbnail，期望 uploaded+1；
    # B：没有占位，期望 uploaded；
    # C（example_thumbnail_count=None）：MmsUiMmsUpload 组件没有
    #   beast-core-thumbnail，跳过等待。
    # 拼多多图片上传是异步走 HTTP，一般几秒内完成；超时不硬失败，
    # 打个警告让人工在页面上确认。
    if example_thumbnail_count is not None:
        try:
            page.wait_for_function(
                """
                ({expected, containerId, exampleCount}) => {
                    const items = document.querySelectorAll(
                        '#' + containerId
                        + ' [data-testid="beast-core-thumbnail"]'
                    );
                    return items.length >= expected + exampleCount;
                }
                """,
                arg={
                    "expected": len(paths),
                    "containerId": container_id,
                    "exampleCount": example_thumbnail_count,
                },
                timeout=30_000,
            )
        except PlaywrightTimeoutError:
            print(
                f"警告：等待 {len(paths)} 张凭证上传完成超时"
                f"（容器=#{container_id}）。"
                "上传可能仍在进行，请人工在页面上确认。"
            )

    print(
        f"已提交 {len(paths)} 张申诉凭证截图待上传"
        f"（容器=#{container_id}）。"
    )


def fill_appeal_form_status_a(
    page: Page,
    modal: Locator,
    classification: str,
    appeal_text: str,
    evidence_files: list[Path],
) -> None:
    """状态 A 表单填写：单一 appealReasonCode radio + note + 上传。

    A 场景的 appealReasonCode 有 5 个选项，通过 APPEAL_REASON_MAP
    映射到其中 2 个。上传容器是 #appealEvidenceImages，有 1 张
    "示例"占位缩略图。
    """

    select_appeal_reason(page, modal, classification)
    fill_appeal_note(page, modal, appeal_text)
    upload_evidence_images(
        page=page,
        modal=modal,
        file_paths=evidence_files,
        container_id=APPEAL_IMAGE_CONTAINER_A,
        example_thumbnail_count=1,
    )


def fill_appeal_form_status_a2(
    page: Page,
    modal: Locator,
    appeal_text: str,
    evidence_files: list[Path],
) -> None:
    """商品问题 A2 变体表单填写：固定选"其他" → 说明 → 上传。

    商品问题 A2 变体的表单与状态 A 共用 #appealReasonCode 锚点，
    但 radio 只有 2 个选项（"商品没有问题" / "其他"），用户要求
    固定选"其他"。且申诉说明（#appealNote）与申诉凭证
    （#appealEvidenceImages）是选中"其他"之后才渐进渲染出现的，
    所以流程与状态 A 不同：

      1. 先固定选"其他"（触发渐进渲染）
      2. 再填 #appealNote、上传 #appealEvidenceImages

    上传容器与状态 A 相同（#appealEvidenceImages），但该变体没有
    "示例"占位缩略图，所以 example_thumbnail_count=0。
    """

    select_form_radio(
        page=page,
        modal=modal,
        form_item_id="appealReasonCode",
        radio_text=A2_APPEAL_REASON_OTHER,
    )
    # 选"其他"后 #appealNote / #appealEvidenceImages 渐进渲染，
    # fill_appeal_note / upload_evidence_images 内部 wait 会兜住时序。
    fill_appeal_note(page, modal, appeal_text)
    upload_evidence_images(
        page=page,
        modal=modal,
        file_paths=evidence_files,
        container_id=APPEAL_IMAGE_CONTAINER_A,
        example_thumbnail_count=0,
    )


def fill_appeal_form_status_b(
    page: Page,
    modal: Locator,
    classification: str,
    appeal_text: str,
    evidence_files: list[Path],
) -> None:
    """状态 B 表单填写：3 个额外 radio + appealReasonCode + note + 上传。

    B 场景的 appealReasonCode 的 3 个 radio 文字与 classifier 的
    3 个分类字面完全一致，不走 APPEAL_REASON_MAP，直接用
    classification 原文作为 radio_text。

    额外必选 radio（页面默认值就是这些，select_form_radio 里
    默认已选中就跳过点击，避免多此一举）：
      admitGoodProblem = 不认可商品问题
      receiveGood      = 已收到退货商品
      canProof         = 可以提供凭证

    上传容器是 #appealProof，没有"示例"占位缩略图。
    """

    select_form_radio(
        page=page,
        modal=modal,
        form_item_id="admitGoodProblem",
        radio_text=STATUS_B_ADMIT_PROBLEM_TEXT,
    )
    select_form_radio(
        page=page,
        modal=modal,
        form_item_id="receiveGood",
        radio_text=STATUS_B_RECEIVE_GOOD_TEXT,
    )
    select_form_radio(
        page=page,
        modal=modal,
        form_item_id="appealReasonCode",
        radio_text=classification,
    )
    fill_appeal_note(page, modal, appeal_text)
    select_form_radio(
        page=page,
        modal=modal,
        form_item_id="canProof",
        radio_text=STATUS_B_CAN_PROOF_TEXT,
    )
    upload_evidence_images(
        page=page,
        modal=modal,
        file_paths=evidence_files,
        container_id=APPEAL_IMAGE_CONTAINER_B,
        example_thumbnail_count=0,
    )


# ============================================================
# 服务问题：模态框表单填写
# ============================================================

# 服务问题申诉原因选项
SERVICE_APPEAL_REASON = "其他"


def fill_appeal_form_service(
    page: Page,
    modal: Locator,
    appeal_text: str,
    evidence_files: list[Path],
) -> None:
    """服务问题申诉表单填写：选择"其他" → 申诉说明 → 上传凭证。

    表单结构（模态框内）：
      #appealReasonCode    → radio 按钮组（渐进渲染触发点）
      #appealNote          → textarea（选了"其他"后才出现）
      #appealEvidenceImages → 图片上传区（选了"其他"后才出现）

    选中"其他" radio 触发渐进渲染后，textarea 和上传区才可见可操作。
    """

    # 1. 选"其他" radio——触发渐进渲染
    select_form_radio(
        page=page,
        modal=modal,
        form_item_id="appealReasonCode",
        radio_text=SERVICE_APPEAL_REASON,
    )

    # 2. 填写申诉说明（渐进渲染后 textarea 才可见，fill_appeal_note
    #    内部的 wait_for_selector 会兜住这个时序）
    fill_appeal_note(page, modal, appeal_text)

    # 3. 上传凭证图片（容器 id 同状态 A，都是 appealEvidenceImages；
    #    服务问题无示例占位缩略图）
    upload_evidence_images(
        page=page,
        modal=modal,
        file_paths=evidence_files,
        container_id=APPEAL_IMAGE_CONTAINER_A,
        example_thumbnail_count=0,
    )


# ============================================================
# 状态 C：独立页面表单填写
# ============================================================

def fill_appeal_form_status_c(
    page: Page,
    classification: str,
    appeal_text: str,
    evidence_files: list[Path],
) -> None:
    """状态 C 场景：在新打开的独立申诉表单页面上填写所有字段。

    C 与 A/B 的核心差异：
    1. 不是模态框，而是一个完整的独立页面
    2. 申诉原因是下拉 select（非 radio）
    3. 选中下拉后 #voucherImages + #appealDescription 才渐进渲染
    4. 图片上传用 MmsUiMmsUpload 组件，没有 beast-core-thumbnail

    调用本函数前需确保已经：
    - wait_for_appeal_form_page() 通过
    - select_appeal_reason_dropdown() 已选择完成
    """
    submit_button = page.locator(
        'button[data-testid="beast-core-button"][type="submit"]'
    )

    print("状态 C：开始填写申诉表单……")

    # 1. 填写申诉描述
    print("状态 C：正在填写申诉描述……")
    fill_appeal_note(
        page,
        page,
        appeal_text,
        form_item_id="appealDescription",
        use_human_type=False,
    )

    # 2. 上传凭证截图（C 没有缩略图，跳过等待）
    print(f"状态 C：正在上传 {len(evidence_files)} 张凭证……")
    upload_evidence_images(
        page=page,
        modal=page,
        file_paths=evidence_files,
        container_id=APPEAL_IMAGE_CONTAINER_C,
        example_thumbnail_count=None,
    )

    jitter_wait(page, 1_000)

    # 校验底部"提交申请"按钮是否可见（确认表单完整渲染 + 选择已生效）
    if submit_button.count() != 1 or not submit_button.is_visible():
        raise RuntimeError(
            "状态 C 表单提交按钮异常："
            f"数量={submit_button.count()}，"
            f"可见={submit_button.is_visible() if submit_button.count() > 0 else 'N/A'}"
        )

    print("状态 C 申诉表单填写完成（未提交）。")


def fill_appeal_modal(
    page: Page,
    order_no: str,
    classification: str,
    appeal_text: str,
    context: BrowserContext | None = None,
    issue_type: str = "",
) -> Page | None:
    """打开申诉入口并填写所有字段（不点提交）。

    调用方需要在调用本函数前已处在订单的申诉详情页上。
    issue_type: "商品问题"/"服务问题"，控制表单处理分支（当前仅支持商品问题）。

    顶层负责分流（含 A/B/C 三种状态 + 商品问题 A2 变体）：

    A/B（模态框）：
    1. 前置校验分类合法、凭证图片存在；
    2. 检测详情页状态；
    3. 点"我要申诉"（A/B 共用同一个 <a> 按钮）；
    4. B 多一步：处理二次确认框；
    5. 等发起申诉表单模态框出现；
    6. 分流到 fill_appeal_form_status_a / fill_appeal_form_status_b；
    7. 截图存档 + 打印汇总，返回 None。

    C（独立页面）：
    1. 前置校验同 A/B；
    2. 检测详情页状态为 C；
    3. 用 context.expect_page() 点 <button> 捕获新页面；
    4. 在新页面选下拉 → 等渐进渲染 → 填说明 → 上传凭证；
    5. 截图存档 + 打印汇总，返回新 Page 给调用方切换引用。

    Returns:
        C 场景返回新的申诉表单 Page；A/B 返回 None。
    Raises:
        ValueError: context 为 None 但进入了 C 分支。
    """

    # 前置校验 1：申诉分类必须在映射表里（仅商品问题需要）。
    if issue_type != "服务问题" and classification not in APPEAL_REASON_MAP:
        raise ValueError(
            f"未知的申诉分类：{classification!r}，"
            f"允许的值为：{list(APPEAL_REASON_MAP)}"
        )

    # 前置校验 2：凭证图片必须存在。
    evidence_files = collect_evidence_files(order_no)
    if not evidence_files:
        raise RuntimeError(
            f"未找到订单 {order_no} 的申诉凭证截图。"
            "申诉表单要求必填凭证，请先运行 pdd_chat.py 抓聊天记录。"
        )

    # ──── 服务问题分支 ────
    if issue_type == "服务问题":
        print(f"订单 {order_no} 为服务问题，走服务问题申诉流程。")

        # 服务问题详情页可能不用同一个 detail_status__2shmG 容器，
        # 先用正常的 click_appeal_button，失败则用更宽松的选择器兜底
        try:
            click_appeal_button(page)
        except Exception:
            print("  标准选择器未找到「我要申诉」，改用页面级文本匹配...")
            fallback_btn = page.get_by_text("我要申诉", exact=True).first
            fallback_btn.wait_for(state="visible", timeout=20_000)
            safe_click(
                page=page,
                locator=fallback_btn,
                description="我要申诉（服务问题兜底）",
            )

        # 服务问题模态框同 A：初期可见的锚点是 #appealReasonCode
        modal = wait_for_appeal_modal(page, "A")
        fill_appeal_form_service(
            page=page,
            modal=modal,
            appeal_text=appeal_text,
            evidence_files=evidence_files,
        )

        # 截图存档
        DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        screenshot_path = (
            DEBUG_DIR
            / f"appeal_form_filled_{safe_filename(order_no)}.png"
        )
        try:
            page.screenshot(
                path=str(screenshot_path),
                full_page=True,
            )
            print(f"填表截图已存档：{screenshot_path}")
        except Exception as error:
            print(f"填表截图失败：{error}")

        print()
        print("申诉表单填写完成（服务问题），未点击提交。")
        print(f"  申诉原因：{SERVICE_APPEAL_REASON}")
        print(f"  申诉说明：{appeal_text}")
        print(f"  上传凭证：{len(evidence_files)} 张")
        print()

        return None

    # ──── 以下为商品问题分支 ────

    # 检测状态
    status = detect_detail_page_status(page)
    if status not in ("A", "B", "C"):
        raise RuntimeError(
            f"detect_detail_page_status 返回了未知状态：{status!r}"
        )

    # ──── 按状态分流：独立页面 vs 模态框 ────
    if status == "C":
        if context is None:
            raise ValueError(
                "状态 C 需要 context 参数来捕获新页面（context.expect_page），"
                "调用 fill_appeal_modal 时请传入 context= 参数。"
            )

        form_page = click_appeal_button_c(context, page)
        form_page.wait_for_load_state()
        clear_blockers(form_page)

        wait_for_appeal_form_page(form_page)
        select_appeal_reason_dropdown(form_page, classification)
        fill_appeal_form_status_c(
            page=form_page,
            classification=classification,
            appeal_text=appeal_text,
            evidence_files=evidence_files,
        )

        # 截图存档
        DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        screenshot_path = (
            DEBUG_DIR
            / f"appeal_form_filled_{safe_filename(order_no)}.png"
        )
        try:
            form_page.screenshot(
                path=str(screenshot_path),
                full_page=True,
            )
            print(f"填表截图已存档：{screenshot_path}")
        except Exception as error:
            print(f"填表截图失败：{error}")

        print()
        print(f"申诉表单填写完成（状态 C），未点击提交。")
        print(f"  申诉原因：{APPEAL_REASON_MAP.get(classification)}")
        print(f"  申诉说明：{appeal_text}")
        print(f"  上传凭证：{len(evidence_files)} 张")
        print()

        return form_page

    # ──── 状态 A/B 模态框分支 ────
    if status not in ("A", "B"):
        raise RuntimeError(
            f"内部错误：程序意外进入 A/B 分支，但 status={status!r}。"
            "C 分支应在前面通过 return 退出。请检查 C 分支是否有未捕获的异常。"
        )

    click_appeal_button(page)

    if status == "B":
        is_real_b = handle_appeal_confirm_modal(page)
        if not is_real_b:
            # 详情页被误判为 B：没等来二次确认框，但检测到商品问题
            # A2 表单，降级按 A2 变体处理（固定选"其他"）。
            status = "A2"

    # A2 与状态 A 共用 #appealReasonCode 锚点，wait 阶段按 A 处理
    modal = wait_for_appeal_modal(page, "A" if status == "A2" else status)

    if status == "A2":
        print(
            "商品问题 A2 变体：申诉原因仅'商品没有问题/其他'，"
            "固定选'其他'，选完后申诉说明/申诉凭证渐进渲染。"
        )
        fill_appeal_form_status_a2(
            page=page,
            modal=modal,
            appeal_text=appeal_text,
            evidence_files=evidence_files,
        )
    elif status == "A":
        if is_status_a2_modal(modal):
            print(
                "商品问题 A2 变体：申诉原因仅'商品没有问题/其他'，"
                "固定选'其他'。"
            )
            fill_appeal_form_status_a2(
                page=page,
                modal=modal,
                appeal_text=appeal_text,
                evidence_files=evidence_files,
            )
        else:
            fill_appeal_form_status_a(
                page=page,
                modal=modal,
                classification=classification,
                appeal_text=appeal_text,
                evidence_files=evidence_files,
            )
    else:   # status == "B"
        fill_appeal_form_status_b(
            page=page,
            modal=modal,
            classification=classification,
            appeal_text=appeal_text,
            evidence_files=evidence_files,
        )

    # 填完截图存档，便于事后回看/排错
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    screenshot_path = (
        DEBUG_DIR
        / f"appeal_form_filled_{safe_filename(order_no)}.png"
    )
    try:
        page.screenshot(
            path=str(screenshot_path),
            full_page=True,
        )
        print(f"填表截图已存档：{screenshot_path}")
    except Exception as error:
        print(f"填表截图失败：{error}")

    # 汇总里区分 A/B/A2——不同表单的可见字段不一样
    print()
    print(f"申诉表单填写完成（状态 {status}），未点击提交。")
    if status == "A2":
        print(f"  申诉原因：{A2_APPEAL_REASON_OTHER}（固定选择）")
    elif status == "A":
        print(f"  申诉原因：{APPEAL_REASON_MAP.get(classification)}")
    else:
        print(f"  是否认可商品问题：{STATUS_B_ADMIT_PROBLEM_TEXT}")
        print(f"  是否收到退货商品：{STATUS_B_RECEIVE_GOOD_TEXT}")
        print(f"  申诉原因：{classification}")
        print(f"  能否提供凭证：{STATUS_B_CAN_PROOF_TEXT}")
    print(f"  申诉说明：{appeal_text}")
    print(f"  上传凭证：{len(evidence_files)} 张")
    print()

    return None


def load_order_classification(order_no: str) -> tuple[str, str, str]:
    """
    从 output/chat_<safe_order_no>.json 读取大模型分类结果。

    返回 (appeal_evidence_classification, appeal_text, issue_type)。
    如果分类结果文件不存在，抛出 FileNotFoundError。
    """

    json_path = (
        OUTPUT_DIR / f"chat_{safe_filename(order_no)}.json"
    )

    if not json_path.exists():
        raise FileNotFoundError(
            f"未找到订单 {order_no} 的分类结果文件：{json_path}\n"
            "请先运行 run_appeal_batch.py 采集聊天记录并分类。"
        )

    data = json.loads(
        json_path.read_text(encoding="utf-8")
    )

    classification = data.get("appeal_evidence_classification", "")
    appeal_text = data.get("appeal_text", "")
    issue_type = data.get("issue_type", "")

    if not classification or not appeal_text:
        raise ValueError(
            f"订单 {order_no} 的分类结果不完整：\n"
            f"  appeal_evidence_classification={classification!r}\n"
            f"  appeal_text={appeal_text!r}"
        )

    return classification, appeal_text, issue_type


# ============================================================
# 取消按钮：填完表单后点击"取消"关闭（不提交）
# ============================================================

def click_appeal_form_cancel(page: Page, status: str) -> None:
    """填完申诉表单后，点击"取消"按钮关闭表单（不点提交）。

    A/B 场景：表单在模态框里，需要在可见 modal 内部找到"取消"按钮，
    点击后等 modal hidden。
    C 场景：表单在独立页面里，在整个页面范围找"取消"按钮（排除
    type="submit"），点击后等页面反应。

    参数:
        page:   当前表单所在的 page（A/B 为详情页，C 为表单页）
        status: "A" / "B" / "C"
    """

    text = APPEAL_CANCEL_BUTTON_TEXT

    if status in ("A", "B"):
        # ── 模态框内取消 ──
        modal = page.locator(
            '[data-testid="beast-core-modal-innerWrapper"]:visible',
        )
        # 先尝试用 button 标签文本匹配
        cancel_button = modal.locator("button").filter(has_text=text)

        if cancel_button.count() == 0:
            # 回退：beast-core-button 组件
            cancel_button = modal.locator(
                'button[data-testid="beast-core-button"]',
            ).filter(has_text=text)

        if cancel_button.count() != 1:
            raise RuntimeError(
                f"状态 {status} 模态框内取消按钮定位异常："
                f"匹配数量={cancel_button.count()}，"
                f"按钮文字='{text}'"
            )

        stealth_safe_click(
            page=page,
            locator=cancel_button,
            description=f"状态 {status} 模态框取消按钮",
        )

        # 等 modal 消失（带动画+可能的网络请求）
        try:
            modal.wait_for(state="hidden", timeout=10_000)
            print(f"状态 {status} 申诉表单已取消（模态框已关闭）。")
        except PlaywrightTimeoutError:
            print(
                f"状态 {status} 等待模态框关闭超时，"
                "继续执行后续步骤。"
            )

    elif status == "C":
        # ── 独立页面取消 ──
        # C 的 form-page 底部有"提交申请"（type="submit"）和"取消"两个按钮
        cancel_button = (
            page.locator('button[data-testid="beast-core-button"]')
            .filter(has_text=text)
        )

        if cancel_button.count() == 0:
            # 回退：不限 beast-core 组件
            cancel_button = page.locator("button").filter(has_text=text)

        # 如果有多个匹配，排除 type="submit"
        if cancel_button.count() > 1:
            cancel_button = cancel_button.locator(
                ":not([type='submit'])"
            )

        if cancel_button.count() != 1:
            raise RuntimeError(
                f"状态 C 取消按钮定位异常："
                f"匹配数量={cancel_button.count()}，"
                f"按钮文字='{text}'"
            )

        stealth_safe_click(
            page=page,
            locator=cancel_button,
            description="状态 C 表单取消按钮",
        )

        jitter_wait(page, 1_000)
        print("状态 C 申诉表单已取消。")

    else:
        raise ValueError(f"未知 status: {status!r}")


import time as _time


def _dismiss_appeal_notice_modal(page: Page) -> bool:
    """点击提交后若弹出'申诉须知'二次确认框，自动点击'确认并提交'。

    该弹窗是 popover 组件（data-testid="beast-core-portal"，PP_popoverWithConfirm），
    不是 modal 组件（beast-core-modal-innerWrapper）。结构：
       div[data-testid="beast-core-portal"]:visible
         ├─ span 申诉须知（标题）
         └─ footer 里两个按钮：
              button[data-testid="beast-core-button"] > span 确认并提交
              button[data-testid="beast-core-button"] > span 不是以上情况

    返回 True 表示处理过弹窗，False 表示未检测到。
    """
    notice_modal = page.locator(
        '[data-testid="beast-core-portal"]:visible'
    ).filter(has_text="申诉须知")

    try:
        notice_modal.wait_for(state="visible", timeout=5_000)
    except PlaywrightTimeoutError:
        return False

    confirm_btn = notice_modal.locator(
        'button[data-testid="beast-core-button"]',
    ).filter(has_text=APPEAL_NOTICE_CONFIRM_TEXT)

    if confirm_btn.count() != 1:
        confirm_btn = notice_modal.locator("button").filter(
            has_text=APPEAL_NOTICE_CONFIRM_TEXT,
        )

    if confirm_btn.count() == 1:
        stealth_safe_click(
            page=page,
            locator=confirm_btn,
            description="申诉须知-确认并提交",
        )
        print("已自动点击'申诉须知'弹窗中的'确认并提交'。")
        return True

    print("检测到'申诉须知'弹窗，但未定位到'确认并提交'按钮。")
    return False


def _wait_for_submit_captcha(page: Page, appear_wait_sec: int = 10) -> None:
    """点击提交按钮后，等待验证码出现并交给人工处理。

    提交后验证码通常立刻弹出，但也有网络延迟；
    appear_wait_sec 内轮询检测，一出现就调用 ensure_no_captcha 阻塞等待人工滑完，
    5 分钟超时。
    """
    print("等待验证码出现...")
    deadline = _time.time() + appear_wait_sec
    captcha_appeared = False

    while _time.time() < deadline:
        if is_captcha_visible(page):
            captcha_appeared = True
            break
        _time.sleep(0.5)

    if captcha_appeared:
        print("检测到验证码，先尝试自动破解，失败再等待人工处理...")
        ensure_no_captcha(page)
    else:
        print("未检测到验证码，提交可能已直接完成。")


def click_appeal_form_submit(page: Page, status: str) -> None:
    """填完申诉表单后，点击"提交"按钮真正提交申诉。

    A/B 场景：表单在模态框里，找可见 modal 内的提交按钮，
    点击后等 modal 关闭（提交成功通常表现为模态框消失或跳转）。
    C 场景：表单在独立页面里，找 type="submit" 的"提交申请"按钮，
    点击后等页面跳转/关标签。

    参数:
        page:   当前表单所在的 page（A/B 为详情页，C 为表单页）
        status: "A" / "B" / "C"
    """

    if status in ("A", "B"):
        # ── 模态框内提交 ──
        modal = page.locator(
            '[data-testid="beast-core-modal-innerWrapper"]:visible',
        )
        # 优先 type="submit"
        submit_button = modal.locator('button[type="submit"]')

        if submit_button.count() == 0:
            # 回退：按文本匹配
            submit_button = modal.locator("button").filter(
                has_text=APPEAL_SUBMIT_BUTTON_TEXT,
            )

        if submit_button.count() == 0:
            # 再回退：beast-core-button 组件
            submit_button = modal.locator(
                'button[data-testid="beast-core-button"]',
            ).filter(
                has_text=APPEAL_SUBMIT_BUTTON_TEXT,
            )

        if submit_button.count() != 1:
            raise RuntimeError(
                f"状态 {status} 模态框内提交按钮定位异常："
                f"匹配数量={submit_button.count()}"
            )

        stealth_safe_click(
            page=page,
            locator=submit_button,
            description=f"状态 {status} 模态框提交按钮",
        )

        # 处理可能弹出的"申诉须知"二次确认弹窗
        _dismiss_appeal_notice_modal(page)

        # 提交后通常立刻弹出验证码，等待一小段时间后检测
        _wait_for_submit_captcha(page)

        # 等待提交结果（modal 消失 或 页面跳转）
        try:
            modal.wait_for(state="hidden", timeout=15_000)
            print(f"状态 {status} 申诉已提交（模态框已关闭）。")
        except PlaywrightTimeoutError:
            # 可能提交后有结果页或跳转，模态框未必消失
            print(
                f"状态 {status} 提交后模态框未消失，"
                "可能跳转到结果页，继续执行。"
            )

    elif status == "C":
        # ── 独立页面提交 ──
        submit_button = page.locator(
            'button[data-testid="beast-core-button"][type="submit"]',
        )

        if submit_button.count() == 0:
            # 回退：不限 beast-core
            submit_button = page.locator('button[type="submit"]')

        if submit_button.count() != 1:
            raise RuntimeError(
                f"状态 C 提交按钮定位异常："
                f"匹配数量={submit_button.count()}"
            )

        stealth_safe_click(
            page=page,
            locator=submit_button,
            description="状态 C 表单提交按钮",
        )

        # 处理可能弹出的"申诉须知"二次确认弹窗
        _dismiss_appeal_notice_modal(page)

        # 提交后通常立刻弹出验证码，等待一小段时间后检测
        _wait_for_submit_captcha(page)

        # 等待提交结果（跳转或弹提示）
        try:
            page.wait_for_load_state("networkidle", timeout=15_000)
        except PlaywrightTimeoutError:
            pass
        jitter_wait(page, 2_000)
        print("状态 C 申诉已提交。")

    else:
        raise ValueError(f"未知 status: {status!r}")


# ============================================================
# 程序入口（单订单验证）
# ============================================================

def main() -> None:
    """
    独立可运行的验证入口：单订单全流程验证。

    流程：
    1. 搜索页 → 填写订单编号 → 点击查询
    2. 点击"查看详情"，打开新申诉详情页
    3. 截图存档
    4. 读取大模型分类结果
    5. 根据详情页状态 A/B/C 分流：
       - A/B：点击"我要申诉" → 弹出模态框 → 填写
       - C ：点击"我要申诉" → 打开独立表单页面 → 填写
    6. 填入申诉原因、申诉说明、上传申诉凭证
    7. 停留在当前页面，不点提交
    """

    PROFILE_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    order_no = input(
        "请输入订单编号，直接回车使用默认值 "
        f"{DEFAULT_TEST_ORDER_NO}："
    ).strip()

    if not order_no:
        order_no = DEFAULT_TEST_ORDER_NO

    with sync_playwright() as playwright:
        context = create_stealth_context(
            playwright,
            user_data_dir=str(PROFILE_DIR),
        )
        
        page: Page | None = None

        try:
            page = (
                context.pages[0]
                if context.pages
                else context.new_page()
            )

            page = wait_for_login(
                context=context,
                page=page,
            )

            open_appeal_search_page(page)

            detail_page = search_and_open_appeal_detail(
                context=context,
                page=page,
                order_no=order_no,
            )

            if detail_page is None:
                print(f"订单 {order_no} 找不到申诉记录，跳过验证。")
                input("按回车关闭浏览器：")
                return

            DEBUG_DIR.mkdir(
                parents=True,
                exist_ok=True,
            )

            screenshot_path = (
                DEBUG_DIR
                / f"appeal_detail_{safe_filename(order_no)}.png"
            )

            # 等待新页面完成渲染，避免截图截到空白/加载中的状态
            jitter_wait(detail_page, 1_000)

            detail_page.screenshot(
                path=str(screenshot_path),
                full_page=True,
            )

            print()
            print(
                f"订单 {order_no} 的申诉详情页地址："
                f"{detail_page.url}"
            )
            print(f"验证截图已保存到：{screenshot_path}")

            # --------------------------------------------------
            # 填写申诉表单（不点提交）
            # --------------------------------------------------
            print()
            print("正在读取大模型分类结果……")

            classification, appeal_text, issue_type = load_order_classification(
                order_no
            )

            print(f"  订单问题类型：{issue_type or '未记录'}")
            print(f"  申诉凭证分类：{classification}")
            print(f"  申诉文案：{appeal_text}")
            print()

            form_page = fill_appeal_modal(
                page=detail_page,
                order_no=order_no,
                classification=classification,
                appeal_text=appeal_text,
                context=context,
                issue_type=issue_type,
            )

            # 状态 C 返回新的独立表单页面，切换到新引用
            if form_page is not None:
                detail_page = form_page

            print("请在浏览器中检查表单内容是否正确。")
            print("确认无误后可以手动点击「提交」。")

            input("操作完成，按回车关闭浏览器：")

        except Exception as error:
            print()
            print("=" * 60)
            print("程序执行失败")
            print(f"错误类型：{type(error).__name__}")
            print(f"错误内容：{error}")
            print("=" * 60)

            if page is not None and not page.is_closed():
                screenshot_path = save_error_screenshot(
                    page=page,
                    order_no=order_no,
                )

                if screenshot_path:
                    print(
                        "错误页面截图已保存到："
                        f"{screenshot_path}"
                    )

            input("按回车关闭浏览器：")

        finally:
            context.close()


if __name__ == "__main__":
    main()
