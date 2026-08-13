from __future__ import annotations

import json
import re
from pathlib import Path

from datetime import datetime

from playwright.sync_api import (
    Page,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)

from paths import (
    DEFAULT_ORDER_NO,
    EVIDENCE_DIR,
    OUTPUT_DIR,
    PROFILE_DIR,
)
from screenshots import safe_filename, save_error_screenshot, to_relative_evidence_path
from blockers import (
    clear_blockers,
    safe_click,
)
from login import (
    get_order_input,
    get_order_search_radio,
    get_query_button,
    wait_for_login,
)
from stealth import create_stealth_context, human_delay, human_type, jitter_wait


# ============================================================
# 查询方式和订单输入
# ============================================================

def select_order_search_mode(page: Page) -> None:
    """选择“按订单/违规会话编号查询”方式。"""

    radio = get_order_search_radio(page)

    radio.wait_for(
        state="visible",
        timeout=30_000,
    )

    if radio.count() != 1:
        raise RuntimeError(
            "“按订单/违规会话编号查询”选项数量异常："
            f"{radio.count()}"
        )

    checked = radio.get_attribute("data-checked")

    if checked != "true":
        safe_click(
            page=page,
            locator=radio,
            description="按订单/违规会话编号查询",
        )

    try:
        page.wait_for_function(
            """
            () => {
                const labels = Array.from(
                    document.querySelectorAll(
                        'label[data-testid="beast-core-radio"]'
                    )
                );

                const target = labels.find((label) => {
                    return label.textContent
                        ?.includes("按订单/违规会话编号查询");
                });

                return (
                    target?.getAttribute("data-checked")
                    === "true"
                );
            }
            """,
            timeout=10_000,
        )

    except PlaywrightTimeoutError:
        # 某些页面状态更新较慢，但输入框可能已经显示。
        print("未检测到单选框状态变化，继续检查订单输入框。")

    order_input = get_order_input(page)

    order_input.wait_for(
        state="visible",
        timeout=20_000,
    )

    print("已选择：按订单/违规会话编号查询")


def fill_order_number(
    page: Page,
    order_no: str,
) -> None:
    """填写订单编号（使用拟人键盘输入，避免瞬时 .fill() 被风控检测）。"""

    order_no = order_no.strip()

    if not order_no:
        raise ValueError("订单编号不能为空。")

    order_input = get_order_input(page)

    order_input.wait_for(
        state="visible",
        timeout=20_000,
    )

    # 用模拟真人逐键输入替代 order_input.fill(order_no)
    # .fill() 是瞬间完成的，不产生 keydown/keyup 事件，极易被风控识别
    human_type(page, order_input, order_no)

    current_value = order_input.input_value()

    if current_value != order_no:
        raise RuntimeError(
            "订单编号填写失败："
            f"期望值={order_no}，实际值={current_value}"
        )

    print(f"已输入订单编号：{order_no}")


# ============================================================
# 查询结果等待
# ============================================================

def get_result_signature(page: Page) -> str:
    """
    获取当前查询结果区域的文本签名。

    用于判断点击查询后，结果是否发生变化。
    """

    result_panel = page.locator(
        ".search-order-sn-msg-table"
    )

    if result_panel.count() == 0:
        return ""

    try:
        return result_panel.inner_text(
            timeout=3_000,
        ).strip()

    except PlaywrightTimeoutError:
        return ""


def start_result_observer(page: Page) -> None:
    """监听查询结果区域的 DOM 变化。"""

    page.evaluate(
        """
        () => {
            if (window.__pddChatObserver) {
                window.__pddChatObserver.disconnect();
            }

            window.__pddChatMutationCount = 0;

            const resultPanel =
                document.querySelector(
                    ".search-order-sn-msg-table"
                );

            const target =
                resultPanel
                || document.querySelector(".search-content")
                || document.body;

            window.__pddChatObserver =
                new MutationObserver(() => {
                    window.__pddChatMutationCount += 1;
                });

            window.__pddChatObserver.observe(
                target,
                {
                    childList: true,
                    subtree: true,
                    characterData: true,
                    attributes: true,
                }
            );
        }
        """
    )


def wait_for_query_result(
    page: Page,
    old_signature: str,
) -> None:
    """等待查询完成并等待聊天记录渲染。"""

    try:
        page.wait_for_function(
            """
            (oldText) => {
                const mutationCount =
                    window.__pddChatMutationCount || 0;

                const panel =
                    document.querySelector(
                        ".search-order-sn-msg-table"
                    );

                const currentText =
                    panel?.innerText?.trim() || "";

                const hasMessages =
                    document.querySelectorAll(
                        ".new-message-list .message-item"
                    ).length > 0;

                const hasResultTitle =
                    Array.from(
                        document.querySelectorAll("*")
                    ).some((element) => {
                        return (
                            element.children.length === 0
                            && element.textContent?.trim()
                            === "查询结果"
                        );
                    });

                return (
                    mutationCount > 0
                    || currentText !== oldText
                    || hasMessages
                    || hasResultTitle
                );
            }
            """,
            arg=old_signature,
            timeout=30_000,
        )

    except PlaywrightTimeoutError:
        # 超时很可能是验证码/弹窗把请求拦住了。这里如果闷头继续，
        # 就会拿着一张陈旧的页面去采集，产出错的数据——比直接报错更糟。
        # 所以先清障，清掉了就再等一轮真正的结果。
        print("未检测到结果变化，检查是否有弹窗遮挡……")

        clear_blockers(page)

        try:
            page.wait_for_function(
                """
                () => {
                    const mutationCount =
                        window.__pddChatMutationCount || 0;

                    const hasMessages =
                        document.querySelectorAll(
                            ".new-message-list .message-item"
                        ).length > 0;

                    const hasBlankText =
                        document.querySelector(
                            ".search-order-sn-msg-table .table-blank-text"
                        ) !== null;

                    return (
                        mutationCount > 0
                        || hasMessages
                        || hasBlankText
                    );
                }
                """,
                timeout=30_000,
            )

        except PlaywrightTimeoutError:
            print(
                "清障后仍未检测到结果变化，"
                "继续读取当前页面结果。"
            )

    # 等待前端最后一批 DOM 渲染
    jitter_wait(page, 1_000)


# ============================================================
# 聊天记录提取
# ============================================================

def is_chat_records_empty(page: Page) -> bool:
    """
    判断查询结果是否为"暂无聊天记录"（订单确实没有聊天记录）。

    拼多多页面在这种情况下，聊天列表容器（.new-message-list）
    仍然存在于 DOM 中，但父级 .result-col-body 会被设置成
    display:none，导致该容器永远不会变成"可见"状态；如果照常
    等待它 visible 会白等 20 秒后抛出超时异常。真正代表"没有
    聊天记录"的标志，是与结果表格同级的 .table-blank-text 提示。

    调用前应确保 wait_for_query_result() 已经执行完毕，
    页面已经渲染稳定。
    """

    blank_text = page.locator(
        ".search-order-sn-msg-table .table-blank-text"
    ).filter(has_text="暂无聊天记录")

    if blank_text.count() == 0:
        return False

    try:
        return blank_text.first.is_visible()
    except Exception:
        return False


def save_no_chat_record_screenshot(
    page: Page,
    order_no: str,
    run_id: str,
) -> Path:
    """
    订单查询结果为"暂无聊天记录"时，保存整页截图作为凭证。

    即使没有聊天内容，也需要留存"确实查询过、确实没有记录"
    的证据，因此这里截取整个页面（包含订单号、查询结果标题、
    "暂无聊天记录"提示），而不是跳过截图。
    """

    screenshot_dir = (
        EVIDENCE_DIR
        / safe_filename(order_no)
        / run_id
    )

    screenshot_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    screenshot_path = (
        screenshot_dir / "no_chat_record.png"
    )

    result_table = page.locator(
        ".search-order-sn-msg-table"
    )

    try:
        result_table.wait_for(
            state="visible",
            timeout=10_000,
        )
    except PlaywrightTimeoutError:
        print("查询结果区域 10 秒内未显示，仍尝试截图。")

    page.screenshot(
        path=str(screenshot_path),
        full_page=True,
        animations="disabled",
        scale="css",
    )

    print(f'"暂无聊天记录"凭证截图：{screenshot_path}')

    return screenshot_path


def get_current_page_signature(page: Page) -> str:
    """
    获取当前聊天记录页的特征。

    用于判断翻页后聊天内容是否发生变化，
    避免重复采集同一页或陷入死循环。
    """

    message_items = page.locator(
        ".new-message-list .message-item"
    )

    return message_items.evaluate_all(
        """
        (items) => {
            const records = items.map((item) => {
                const sender =
                    item.querySelector(".message-name")
                    ?.textContent?.trim() || "";

                const time =
                    item.querySelector(".message-time")
                    ?.textContent?.trim() || "";

                const content =
                    item.querySelector(".message-body")
                    ?.innerText?.trim() || "";

                const images = Array.from(
                    item.querySelectorAll(".message-body img")
                ).map((image) => {
                    return image.getAttribute("src") || "";
                });

                return {
                    sender,
                    time,
                    content,
                    images,
                };
            });

            return JSON.stringify(records);
        }
        """
    )


def extract_current_page_records(
    page: Page,
    page_number: int,
) -> list[dict]:
    """提取当前这一页的聊天记录。"""

    message_items = page.locator(
        ".new-message-list .message-item"
    )

    if message_items.count() == 0:
        return []

    records = message_items.evaluate_all(
        """
        (items) => {
            const cleanText = (value) => {
                return (value || "")
                    .replace(/\\u00a0/g, " ")
                    .trim();
            };

            return items.map((item, index) => {
                const senderElement =
                    item.querySelector(".message-name");

                const timeElement =
                    item.querySelector(".message-time");

                const bodyElement =
                    item.querySelector(".message-body");

                const avatarElement =
                    item.querySelector(".message-avatar");

                const textElements = Array.from(
                    item.querySelectorAll(".message-text")
                );

                let content = textElements
                    .map((element) => {
                        return cleanText(
                            element.innerText
                            || element.textContent
                        );
                    })
                    .filter(Boolean)
                    .join("\\n");

                if (!content && bodyElement) {
                    content = cleanText(
                        bodyElement.innerText
                        || bodyElement.textContent
                    );
                }

                const imageUrls = Array.from(
                    item.querySelectorAll(
                        ".message-body img"
                    )
                )
                    .map((image) => {
                        return image.getAttribute("src") || "";
                    })
                    .filter(Boolean);

                return {
                    page_index: index + 1,

                    sender: cleanText(
                        senderElement?.innerText
                        || senderElement?.textContent
                    ),

                    time: cleanText(
                        timeElement?.innerText
                        || timeElement?.textContent
                    ),

                    content,

                    is_system_message:
                        item.classList.contains(
                            "system-message"
                        ),

                    avatar_url:
                        avatarElement?.getAttribute("src")
                        || "",

                    image_urls: imageUrls,
                };
            });
        }
        """
    )

    for record in records:
        record["page_number"] = page_number

    return records


def get_expected_message_total(page: Page) -> int | None:
    """
    从“共有 123 条”中读取理论消息总数。

    读取不到时返回 None。
    """

    pagination = page.locator(
        '[data-testid="beast-core-pagination"]'
    )

    if pagination.count() == 0:
        return None

    try:
        pagination_text = pagination.inner_text(
            timeout=3_000,
        )
    except PlaywrightTimeoutError:
        return None

    matched = re.search(
        r"共有\s*(\d+)\s*条",
        pagination_text,
    )

    if not matched:
        return None

    return int(matched.group(1))


def click_next_page(page: Page) -> dict:
    """
    点击分页组件中的下一页。

    因为目前没有多页情况下的完整分页 HTML，
    这里同时支持：
    - aria-label 包含下一页或 next
    - title 包含下一页
    - data-testid 包含 next
    - class 中包含 next、right、arrow-right
    - 分页组件最后一个可操作项
    """

    result = page.evaluate(
        """
        () => {
            const root = document.querySelector(
                '[data-testid="beast-core-pagination"]'
            );

            if (!root) {
                return {
                    clicked: false,
                    reason: "没有找到分页组件"
                };
            }

            const rootStyle = window.getComputedStyle(root);

            if (
                rootStyle.display === "none"
                || rootStyle.visibility === "hidden"
            ) {
                return {
                    clicked: false,
                    reason: "分页组件未显示，当前可能只有一页"
                };
            }

            const normalize = (value) => {
                return String(value || "")
                    .replace(/\\s+/g, " ")
                    .trim()
                    .toLowerCase();
            };

            const getDescription = (element) => {
                return normalize([
                    element.textContent,
                    element.getAttribute("aria-label"),
                    element.getAttribute("title"),
                    element.getAttribute("data-testid"),
                    element.getAttribute("class"),
                    element.querySelector("[data-testid]")
                        ?.getAttribute("data-testid"),
                    element.querySelector("[class]")
                        ?.getAttribute("class"),
                ].join(" "));
            };

            const isDisabled = (element) => {
                if (!element) {
                    return true;
                }

                const description = getDescription(element);

                return (
                    element.disabled === true
                    || element.getAttribute("disabled") !== null
                    || element.getAttribute("aria-disabled") === "true"
                    || element.getAttribute("data-disabled") === "true"
                    || /disabled|disable/.test(description)
                );
            };

            const elements = Array.from(
                root.querySelectorAll(
                    "button, a, li"
                )
            );

            const scored = elements
                .map((element) => {
                    const description =
                        getDescription(element);

                    let score = 0;

                    if (description.includes("下一页")) {
                        score += 100;
                    }

                    if (
                        /(^|\\s|_|-)next($|\\s|_|-)/.test(
                            description
                        )
                    ) {
                        score += 80;
                    }

                    if (
                        /arrow.?right|chevron.?right|page.?right/.test(
                            description
                        )
                    ) {
                        score += 60;
                    }

                    if (
                        /(^|\\s|_|-)right($|\\s|_|-)/.test(
                            description
                        )
                    ) {
                        score += 30;
                    }

                    if (
                        description.includes("上一页")
                        || /prev|previous|arrow.?left/.test(
                            description
                        )
                    ) {
                        score -= 200;
                    }

                    if (
                        description.includes("共有")
                        || description.includes("total")
                    ) {
                        score -= 200;
                    }

                    return {
                        element,
                        score,
                        description,
                    };
                })
                .filter((item) => item.score > 0)
                .sort((a, b) => b.score - a.score);

            let target = scored[0]?.element || null;

            /*
             * 如果无法根据属性识别下一页，
             * 尝试取分页组件的最后一个直接子元素。
             */
            if (!target) {
                const directChildren = Array.from(
                    root.children
                ).filter((element) => {
                    return element.matches(
                        "li, button, a"
                    );
                });

                if (directChildren.length >= 3) {
                    target = directChildren[
                        directChildren.length - 1
                    ];
                }
            }

            if (!target) {
                return {
                    clicked: false,
                    reason: "没有识别到下一页按钮"
                };
            }

            const clickable = target.matches(
                "button, a"
            )
                ? target
                : (
                    target.querySelector("button, a")
                    || target
                );

            if (
                isDisabled(target)
                || isDisabled(clickable)
            ) {
                return {
                    clicked: false,
                    reason: "已经到最后一页"
                };
            }

            clickable.scrollIntoView({
                block: "center",
                inline: "center",
            });

            clickable.click();

            return {
                clicked: true,
                reason: "已点击下一页",
                description:
                    getDescription(clickable),
            };
        }
        """
    )

    return result


def wait_for_next_page_loaded(
    page: Page,
    previous_signature: str,
) -> bool:
    """等待点击下一页后聊天记录发生变化。"""

    try:
        page.wait_for_function(
            """
            (previousSignature) => {
                const items = Array.from(
                    document.querySelectorAll(
                        ".new-message-list .message-item"
                    )
                );

                if (items.length === 0) {
                    return false;
                }

                const records = items.map((item) => {
                    const sender =
                        item.querySelector(".message-name")
                        ?.textContent?.trim() || "";

                    const time =
                        item.querySelector(".message-time")
                        ?.textContent?.trim() || "";

                    const content =
                        item.querySelector(".message-body")
                        ?.innerText?.trim() || "";

                    const images = Array.from(
                        item.querySelectorAll(
                            ".message-body img"
                        )
                    ).map((image) => {
                        return image.getAttribute("src") || "";
                    });

                    return {
                        sender,
                        time,
                        content,
                        images,
                    };
                });

                const currentSignature =
                    JSON.stringify(records);

                return (
                    currentSignature
                    !== previousSignature
                );
            }
            """,
            arg=previous_signature,
            timeout=20_000,
        )

        # 等待当前页剩余内容渲染完成
        jitter_wait(page, 800)

        return True

    except PlaywrightTimeoutError:
        return False

def expand_chat_scroll_area(page: Page) -> dict:
    """
    找到聊天记录内部的滚动容器，并临时展开全部内容。

    所有被修改的行内样式都会保存在浏览器中，
    截图完成后由 restore_chat_scroll_area() 恢复。
    """

    return page.evaluate(
        """
        () => {
            const messageList = document.querySelector(
                ".new-message-list"
            );

            if (!messageList) {
                throw new Error(
                    "没有找到 .new-message-list"
                );
            }

            const isScrollable = (element) => {
                const style = window.getComputedStyle(element);

                const overflowY = style.overflowY;

                return (
                    ["auto", "scroll", "overlay"]
                        .includes(overflowY)
                    && element.scrollHeight
                        > element.clientHeight + 2
                );
            };

            /*
             * 从聊天列表开始向上查找真正拥有滚动条的父元素。
             */
            let scrollContainer = messageList;

            while (
                scrollContainer
                && scrollContainer !== document.body
            ) {
                if (isScrollable(scrollContainer)) {
                    break;
                }

                scrollContainer =
                    scrollContainer.parentElement;
            }

            /*
             * 没识别到明显滚动容器时，
             * 仍然从聊天列表自身开始展开。
             */
            if (
                !scrollContainer
                || scrollContainer === document.body
            ) {
                scrollContainer = messageList;
            }

            const beforeInfo = {
                tag: scrollContainer.tagName,
                className:
                    scrollContainer.className || "",
                clientHeight:
                    scrollContainer.clientHeight,
                scrollHeight:
                    scrollContainer.scrollHeight,
                scrollTop:
                    scrollContainer.scrollTop,
            };

            /*
             * 防止上次异常退出后残留备份。
             */
            if (window.__pddEvidenceStyleBackups) {
                window.__pddEvidenceStyleBackups
                    .forEach((backup) => {
                        if (backup.originalStyle === null) {
                            backup.element.removeAttribute(
                                "style"
                            );
                        } else {
                            backup.element.setAttribute(
                                "style",
                                backup.originalStyle
                            );
                        }
                    });
            }

            const backups = [];

            const rememberElement = (element) => {
                if (
                    backups.some(
                        (item) => item.element === element
                    )
                ) {
                    return;
                }

                backups.push({
                    element,
                    originalStyle:
                        element.getAttribute("style"),
                });
            };

            /*
             * 从滚动容器一直修改到聊天页面主体。
             * 这样可以避免父元素的 height、max-height、
             * overflow 再次裁剪聊天记录。
             */
            const stopElement =
                messageList.closest(
                    "#pdd-app-skeleton-main-content"
                )
                || messageList.closest(".search-content")
                || document.body;

            let current = scrollContainer;

            while (current) {
                rememberElement(current);

                current.style.setProperty(
                    "height",
                    "auto",
                    "important"
                );

                current.style.setProperty(
                    "max-height",
                    "none",
                    "important"
                );

                current.style.setProperty(
                    "overflow",
                    "visible",
                    "important"
                );

                current.style.setProperty(
                    "overflow-y",
                    "visible",
                    "important"
                );

                current.style.setProperty(
                    "contain",
                    "none",
                    "important"
                );

                if (
                    current === stopElement
                    || current === document.body
                ) {
                    break;
                }

                current = current.parentElement;
            }

            /*
             * 聊天列表自己也可能带 max-height。
             */
            rememberElement(messageList);

            messageList.style.setProperty(
                "height",
                "auto",
                "important"
            );

            messageList.style.setProperty(
                "max-height",
                "none",
                "important"
            );

            messageList.style.setProperty(
                "overflow",
                "visible",
                "important"
            );

            /*
             * 从第一条聊天记录开始截图。
             */
            scrollContainer.scrollTop = 0;

            window.__pddEvidenceStyleBackups = backups;
            window.__pddEvidenceScrollContainer =
                scrollContainer;

            const afterInfo = {
                clientHeight:
                    scrollContainer.clientHeight,
                scrollHeight:
                    scrollContainer.scrollHeight,
            };

            return {
                found: true,
                before: beforeInfo,
                after: afterInfo,
                modifiedElementCount: backups.length,
            };
        }
        """
    )


def restore_chat_scroll_area(page: Page) -> None:
    """恢复截图前临时修改的网页样式。"""

    page.evaluate(
        """
        () => {
            const backups =
                window.__pddEvidenceStyleBackups || [];

            /*
             * 从外向内倒序恢复，避免恢复过程中布局互相影响。
             */
            [...backups]
                .reverse()
                .forEach((backup) => {
                    if (!backup.element) {
                        return;
                    }

                    if (backup.originalStyle === null) {
                        backup.element.removeAttribute(
                            "style"
                        );
                    } else {
                        backup.element.setAttribute(
                            "style",
                            backup.originalStyle
                        );
                    }
                });

            delete window.__pddEvidenceStyleBackups;
            delete window.__pddEvidenceScrollContainer;
        }
        """
    )


def wait_for_chat_images(page: Page) -> None:
    """
    等待当前页聊天记录中的图片加载完成。

    避免截图中出现空白图片或图片加载到一半。
    """

    try:
        page.wait_for_function(
            """
            () => {
                const images = Array.from(
                    document.querySelectorAll(
                        ".new-message-list img"
                    )
                );

                return images.every((image) => {
                    return (
                        image.complete
                        && image.naturalWidth > 0
                    );
                });
            }
            """,
            timeout=10_000,
        )

    except PlaywrightTimeoutError:
        print(
            "部分聊天图片在 10 秒内未加载完成，"
            "继续保存截图。"
        )


def save_page_evidence_screenshot(
    page: Page,
    order_no: str,
    page_number: int,
    run_id: str,
) -> Path:
    """
    保存当前分页的完整聊天记录截图。

    截图前会临时展开聊天列表内部的滚动区域，
    因此能够截取滚动条下面的全部聊天记录。
    """

    screenshot_dir = (
        EVIDENCE_DIR
        / safe_filename(order_no)
        / run_id
    )

    screenshot_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    screenshot_path = (
        screenshot_dir
        / f"page_{page_number:03d}.png"
    )

    message_list = page.locator(
        ".new-message-list"
    )

    message_list.wait_for(
        state="visible",
        timeout=20_000,
    )

    wait_for_chat_images(page)

    try:
        expand_info = expand_chat_scroll_area(page)

        before = expand_info.get("before", {})
        after = expand_info.get("after", {})

        print(
            "聊天滚动区域："
            f'可见高度 {before.get("clientHeight", 0)}，'
            f'内容总高度 {before.get("scrollHeight", 0)}'
        )

        print(
            "展开后的区域："
            f'可见高度 {after.get("clientHeight", 0)}，'
            f'内容总高度 {after.get("scrollHeight", 0)}'
        )

        # 等待网页重新排版稳定
        jitter_wait(page, 800)

        page.screenshot(
            path=str(screenshot_path),
            full_page=True,
            animations="disabled",
            scale="css",
        )

        print(
            f"第 {page_number} 页完整凭证截图："
            f"{screenshot_path}"
        )

        return screenshot_path

    finally:
        # 即使截图失败也要恢复网页，否则会影响翻页
        try:
            restore_chat_scroll_area(page)
            jitter_wait(page, 300)

        except Exception as restore_error:
            print(
                "恢复聊天区域样式失败："
                f"{restore_error}"
            )

def collect_all_chat_records(
    page: Page,
    order_no: str,
    max_pages: int = 200,
) -> tuple[list[dict], Path | None]:
    """
    采集所有分页中的聊天记录，并为每一页保存凭证截图。

    max_pages 是安全限制，防止网页异常时无限翻页。

    返回 (聊天记录列表, "暂无聊天记录"凭证截图路径)。
    订单确实有聊天记录时，第二个值为 None。
    """

    # 本次运行的唯一编号，避免多次采集时截图相互覆盖
    run_id = datetime.now().strftime(
        "%Y%m%d_%H%M%S"
    )

    if is_chat_records_empty(page):
        print(
            f"订单 {order_no} 查询结果为"
            '"暂无聊天记录"，本订单没有聊天记录。'
        )

        no_chat_screenshot = save_no_chat_record_screenshot(
            page=page,
            order_no=order_no,
            run_id=run_id,
        )

        return [], no_chat_screenshot

    all_records: list[dict] = []
    visited_signatures: set[str] = set()

    expected_total = get_expected_message_total(
        page
    )

    if expected_total is not None:
        print(
            f"分页显示共有 {expected_total} 条聊天记录。"
        )

    current_page = 1

    while current_page <= max_pages:
        message_list = page.locator(
            ".new-message-list"
        )

        message_list.wait_for(
            state="visible",
            timeout=20_000,
        )

        # 确保当前页的消息已经渲染
        page.locator(
            ".new-message-list .message-item"
        ).first.wait_for(
            state="visible",
            timeout=20_000,
        )

        current_signature = (
            get_current_page_signature(page)
        )

        if not current_signature:
            print(
                f"第 {current_page} 页没有获取到有效内容，"
                "停止采集。"
            )
            break

        # 防止重复采集同一页
        if current_signature in visited_signatures:
            print(
                "检测到重复页面，停止翻页，"
                "避免重复采集或死循环。"
            )
            break

        visited_signatures.add(
            current_signature
        )

        # 先保存当前页截图
        screenshot_path = (
            save_page_evidence_screenshot(
                page=page,
                order_no=order_no,
                page_number=current_page,
                run_id=run_id,
            )
        )

        # 再提取当前页数据
        page_records = (
            extract_current_page_records(
                page=page,
                page_number=current_page,
            )
        )

        # JSON 中保存截图相对路径
        relative_screenshot_path = (
            to_relative_evidence_path(screenshot_path)
        )

        for record in page_records:
            record["evidence_screenshot"] = (
                relative_screenshot_path
            )

        print(
            f"第 {current_page} 页采集完成，"
            f"本页 {len(page_records)} 条。"
        )

        all_records.extend(page_records)

        # 如果已经达到页面显示的总数量，可以提前结束
        if (
            expected_total is not None
            and len(all_records) >= expected_total
        ):
            print(
                "已达到分页显示的聊天记录总数，"
                "停止继续翻页。"
            )
            break

        # 翻页的点击发生在 JS 内部，会绕过遮挡直接派发事件、不抛错，
        # 但后续的数据刷新仍可能被风控拦住，所以要先清障再翻页。
        clear_blockers(page)

        next_result = click_next_page(page)

        # JS 内部直接调 el.click()，没有触发 mouse 事件；
        # 翻页后加一段随机延迟，模拟用户翻页→等加载→继续阅读的自然节奏
        human_delay(600, 1500)

        if not next_result.get("clicked"):
            print(
                "停止翻页："
                f'{next_result.get("reason", "未知原因")}'
            )
            break

        changed = wait_for_next_page_loaded(
            page=page,
            previous_signature=current_signature,
        )

        if not changed:
            print(
                "点击下一页后聊天内容没有发生变化，"
                "停止翻页。"
            )
            break

        current_page += 1

    else:
        print(
            f"已经达到最大翻页限制 "
            f"{max_pages} 页，停止继续采集。"
        )

    # 生成所有消息的全局编号
    for global_index, record in enumerate(
        all_records,
        start=1,
    ):
        record["index"] = global_index

    print()
    print(
        f"所有分页采集结束，共采集 "
        f"{len(all_records)} 条聊天记录。"
    )

    evidence_folder = (
        EVIDENCE_DIR
        / safe_filename(order_no)
        / run_id
    )

    print(
        f"凭证截图保存目录："
        f"{evidence_folder}"
    )

    if (
        expected_total is not None
        and len(all_records) != expected_total
    ):
        print(
            "警告：实际采集数量与分页显示数量不一致。"
        )
        print(
            f"页面显示：{expected_total} 条，"
            f"实际采集：{len(all_records)} 条。"
        )

    return all_records, None


# ============================================================
# 完整查询流程
# ============================================================

def query_chat_records(
    page: Page,
    order_no: str,
) -> tuple[list[dict], Path | None]:
    """
    根据订单编号查询聊天记录。

    返回 (聊天记录列表, "暂无聊天记录"凭证截图路径)。
    订单确实有聊天记录时，第二个值为 None。
    """

    order_no = order_no.strip()

    if not order_no:
        raise ValueError("订单编号不能为空。")

    select_order_search_mode(page)

    fill_order_number(
        page=page,
        order_no=order_no,
    )

    query_button = get_query_button(page)

    query_button.wait_for(
        state="visible",
        timeout=20_000,
    )

    if not query_button.is_enabled():
        raise RuntimeError("查询按钮当前不可用。")

    old_signature = get_result_signature(page)

    start_result_observer(page)

    print(f"正在查询订单：{order_no}")

    safe_click(
        page=page,
        locator=query_button,
        description="查询按钮",
    )

    # 点击查询会触发后端请求，是验证码高发时机
    clear_blockers(page)

    wait_for_query_result(
        page=page,
        old_signature=old_signature,
    )

    records, no_chat_screenshot = collect_all_chat_records(
        page=page,
        order_no=order_no,
        max_pages=200,
    )

    return records, no_chat_screenshot


# ============================================================
# 数据输出
# ============================================================

def print_chat_records(
    records: list[dict],
) -> None:
    """在控制台打印聊天记录。"""

    print()
    print("=" * 70)
    print("聊天记录")
    print("=" * 70)

    if not records:
        print("当前订单没有获取到聊天记录。")
        print("=" * 70)
        return

    for record in records:
        message_type = (
            "系统消息"
            if record["is_system_message"]
            else "普通消息"
        )

        print(
            f'[{record["index"]}] '
            f'{record["time"]} '
            f'{record["sender"]} '
            f'({message_type})'
        )

        if record["content"]:
            print(record["content"])
        elif record["image_urls"]:
            print("[图片消息]")
        else:
            print("[未识别的非文本消息]")

        if record["image_urls"]:
            for image_url in record["image_urls"]:
                print(f"图片：{image_url}")

        print("-" * 70)


def save_chat_records(
    order_no: str,
    records: list[dict],
    no_chat_record_screenshot: Path | None = None,
    issue_type: str = "",
) -> Path:
    """
    将聊天记录保存成 JSON 文件。

    no_chat_record_screenshot：订单查询结果为"暂无聊天记录"时的
    凭证截图路径；订单确实有聊天记录时传 None。
    issue_type：订单问题类型（"商品问题"/"服务问题"），从申诉列表表格采集。
    """

    OUTPUT_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    filename = (
        f"chat_{safe_filename(order_no)}.json"
    )

    output_file = OUTPUT_DIR / filename

    evidence_screenshots = list(
        dict.fromkeys(
            record.get("evidence_screenshot", "")
            for record in records
            if record.get("evidence_screenshot")
        )
    )

    if no_chat_record_screenshot is not None:
        evidence_screenshots.append(
            to_relative_evidence_path(no_chat_record_screenshot)
        )

    result = {
        "order_no": order_no,
        "issue_type": issue_type,
        "message_count": len(records),
        "evidence_screenshots": evidence_screenshots,
        "messages": records,
    }

    output_file.write_text(
        json.dumps(
            result,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    return output_file


# ============================================================
# 程序入口
# ============================================================

def main() -> None:
    PROFILE_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    order_no = input(
        "请输入订单编号，直接回车使用默认值 "
        f"{DEFAULT_ORDER_NO}："
    ).strip()

    if not order_no:
        order_no = DEFAULT_ORDER_NO

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

            records, no_chat_screenshot = query_chat_records(
                page=page,
                order_no=order_no,
            )

            print_chat_records(records)

            output_file = save_chat_records(
                order_no=order_no,
                records=records,
                no_chat_record_screenshot=no_chat_screenshot,
            )

            print()
            print(f"聊天记录已保存到：{output_file}")

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