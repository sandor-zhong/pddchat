from __future__ import annotations

import json
import random
import re
from datetime import datetime
from pathlib import Path

from playwright.sync_api import (
    Page,
    sync_playwright,
)
from playwright.sync_api import (
    TimeoutError as PlaywrightTimeoutError,
)

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
from paths import (
    DEFAULT_ORDER_NO,
    EVIDENCE_DIR,
    OUTPUT_DIR,
    PROFILE_DIR,
    START_URL,
)
from screenshots import safe_filename, save_error_screenshot, to_relative_evidence_path
from stealth import (
    create_stealth_context,
    human_click,
    human_delay,
    human_scroll,
    human_type,
    jitter_wait,
)

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
        ).dispose()

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

    # 标签页会被后续订单复用，所以输入框里可能还保留上一笔订单号。先按
    # 真人操作全选并删除，再检查 DOM 中的实际 value；React 受控输入框
    # 偶尔漏掉快捷键事件时，用 fill("") 作为清空兜底。
    human_click(page, order_input)
    human_delay(120, 320)
    order_input.press("Control+A")
    human_delay(80, 220)
    order_input.press("Backspace")
    human_delay(100, 260)

    if (order_input.input_value() or "").strip():
        order_input.fill("")
    if (order_input.input_value() or "").strip():
        raise RuntimeError("聊天记录订单编号输入框清空失败，已停止本单查询。")

    # 清空校验通过后再逐键输入；click_first=False 避免重复点击导致焦点或
    # 光标位置发生变化。输入完成后仍要验证实际值，正确才允许点击查询。
    human_type(page, order_input, order_no, click_first=False)

    current_value = (order_input.input_value() or "").strip()

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
    """只记录查询点击后的结果内容变化，忽略滚动和样式变化。"""
    page.evaluate("""() => {
        window.__pddChatObserver?.disconnect();
        window.__pddChatMutationCount = 0;
        window.__pddChatLastMutation = 0;
        window.__pddChatQueryClicked = false;
        window.__pddChatObserver = new MutationObserver(records => {
            if (!window.__pddChatQueryClicked) return;
            const relevant = records.some(record => {
                const element = record.target.nodeType === 1
                    ? record.target : record.target.parentElement;
                return element?.closest('.search-order-sn-msg-table, .new-message-list')
                    || Array.from(record.addedNodes).some(node => node.nodeType === 1 &&
                        (node.matches('.search-order-sn-msg-table, .new-message-list') ||
                         node.querySelector('.search-order-sn-msg-table, .new-message-list')));
            });
            if (relevant) {
                window.__pddChatMutationCount++;
                window.__pddChatLastMutation = performance.now();
            }
        });
        window.__pddChatObserver.observe(document.body, {
            childList: true, subtree: true, characterData: true
        });
    }""")
    get_query_button(page).evaluate("""button => {
        button.addEventListener('click', () => {
            window.__pddChatQueryClicked = true;
            window.__pddChatMutationCount = 0;
            window.__pddChatLastMutation = performance.now();
        }, {capture: true, once: true});
    }""")


def wait_for_query_result(page: Page, old_signature: str, timeout: int = 30_000) -> None:
    """等待本次查询产生可见且稳定的结果；超时禁止采集旧内容。"""
    try:
        page.wait_for_function("""oldText => {
            const visible = el => !!el && el.getClientRects().length > 0
                && getComputedStyle(el).visibility !== 'hidden';
            const panel = document.querySelector('.search-order-sn-msg-table');
            const messages = Array.from(document.querySelectorAll(
                '.new-message-list .message-item')).some(visible);
            const empty = Array.from(document.querySelectorAll(
                '.search-order-sn-msg-table .table-blank-text')).some(el =>
                    visible(el) && el.textContent.includes('暂无聊天记录'));
            const loading = Array.from(document.querySelectorAll(
                '[aria-busy="true"], [data-testid="beast-core-loading"], .ant-spin-spinning'
            )).some(visible);
            return window.__pddChatQueryClicked
                && window.__pddChatMutationCount > 0
                && performance.now() - window.__pddChatLastMutation >= 1000
                && !loading && (messages || empty);
        }""", arg=old_signature, timeout=timeout).dispose()
    except PlaywrightTimeoutError as exc:
        clicked = page.evaluate('Boolean(window.__pddChatQueryClicked)')
        raise RuntimeError(
            f"聊天查询未确认新结果（查询按钮点击事件={clicked}），已停止本单采集，避免读取旧聊天记录。"
        ) from exc
    finally:
        page.evaluate('() => window.__pddChatObserver?.disconnect()')


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


def click_next_page(page: Page, *, root_selector: str | None = None) -> dict:
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
        (scopeSelector) => {
            document.querySelectorAll("[data-pdd-chat-next-target]")
                .forEach(element => element.removeAttribute(
                    "data-pdd-chat-next-target"
                ));
            const scope = scopeSelector ? document.querySelector(scopeSelector) : document;
            const root = scope?.querySelector(
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

            // JavaScript 只负责识别目标。真正点击在 Python 侧通过鼠标
            // 移动、按下、松开完成，不再调用 element.click()。
            clickable.setAttribute(
                "data-pdd-chat-next-target",
                "true"
            );

            return {
                clicked: false,
                targetFound: true,
                reason: "已识别下一页按钮",
                description:
                    getDescription(clickable),
            };
        }
        """,
        arg=root_selector,
    )

    if not result.get("targetFound"):
        return result

    target = page.locator('[data-pdd-chat-next-target="true"]')
    try:
        human_click(page, target)
    finally:
        try:
            target.evaluate(
                "element => element.removeAttribute('data-pdd-chat-next-target')"
            )
        except Exception:
            pass

    result["clicked"] = True
    result["reason"] = "已用鼠标点击下一页"
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
        ).dispose()

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
                ).filter((image) => {
                    const rect = image.getBoundingClientRect();
                    return rect.bottom > 0 && rect.top < window.innerHeight;
                });

                return images.every((image) => {
                    return (
                        image.complete
                        && image.naturalWidth > 0
                    );
                });
            }
            """,
            timeout=10_000,
        ).dispose()

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
) -> list[dict]:
    """
    像人工查看聊天一样逐屏滚动，只截聊天面板的当前可见区域。

    不展开 DOM、不截整页，也不做图片拼接。每张原始截图会记录它覆盖的
    页内消息序号及完整可见范围，供后续优先选择一张、最多两张连续原图。
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

    message_list = page.locator(
        ".new-message-list"
    )

    message_list.wait_for(
        state="visible",
        timeout=20_000,
    )

    message_list.evaluate(
        """
        (list) => {
            const scrollable = (node) => {
                const style = getComputedStyle(node);
                return ["auto", "scroll", "overlay"].includes(style.overflowY)
                    && node.scrollHeight > node.clientHeight + 2;
            };
            let node = list;
            while (node && node !== document.body && !scrollable(node)) {
                node = node.parentElement;
            }
            if (!node || node === document.body) node = list;
            node.setAttribute("data-pdd-natural-chat-scroller", "true");
            window.__pddNaturalChatScroller = node;
            return {
                clientHeight: node.clientHeight,
                scrollHeight: node.scrollHeight,
                scrollTop: node.scrollTop,
            };
        }
        """,
    )

    scroll_region = page.locator('[data-pdd-natural-chat-scroller="true"]')
    box = scroll_region.bounding_box()
    if not box:
        raise RuntimeError("无法获取聊天区域的屏幕位置。")

    viewport = page.viewport_size or {
        "width": int(page.evaluate("window.innerWidth")),
        "height": int(page.evaluate("window.innerHeight")),
    }
    center_x = box["x"] + box["width"] / 2
    center_y = box["y"] + box["height"] / 2
    page.mouse.move(center_x, center_y)

    # 从当前所在位置像真人一样滚回开头，不直接改 scrollTop。
    safety = 0
    while safety < 30:
        top = page.evaluate("window.__pddNaturalChatScroller.scrollTop")
        if top <= 1:
            break
        human_scroll(page, -min(max(int(top), 240), 720), 3, 7)
        human_delay(120, 360)
        safety += 1

    captures: list[dict] = []
    seen_ranges: set[tuple[int, int]] = set()
    screen_number = 1
    attempts = 0
    stalled = 0

    while screen_number <= 80 and attempts < 120:
        attempts += 1
        wait_for_chat_images(page)
        human_delay(350, 1100)

        visible_info = page.locator(
            ".new-message-list .message-item"
        ).evaluate_all(
            """
            (items) => {
                const scroller = window.__pddNaturalChatScroller;
                const view = scroller.getBoundingClientRect();
                const visibleItems = items.map((item, index) => ({
                    item,
                    index: index + 1,
                    rect: item.getBoundingClientRect(),
                })).filter(({rect}) => (
                    rect.bottom > view.top + 2
                    && rect.top < view.bottom - 2
                ));

                // 保留发言人及时间，不能只保留有利气泡而丢失来源信息。
                const contentRects = visibleItems.flatMap(({item}) => {
                    const nodes = item.querySelectorAll(
                        ".message-avatar, .message-name, .message-body, .message-time"
                    );
                    return Array.from(nodes).map(node => (
                        node.getBoundingClientRect()
                    )).filter(rect => (
                        rect.width > 0 && rect.height > 0
                        && rect.bottom > view.top
                        && rect.top < view.bottom
                    ));
                });

                if (!visibleItems.length || !contentRects.length) {
                    return {indices: [], bounds: null};
                }
                return {
                    indices: visibleItems.map(({index}) => index),
                    fullIndices: visibleItems.filter(({rect}) => (
                        rect.top >= Math.max(0, view.top)
                        && rect.bottom <= Math.min(window.innerHeight, view.bottom)
                        && rect.left >= Math.max(0, view.left)
                        && rect.right <= Math.min(window.innerWidth, view.right)
                    )).map(({index}) => index),
                    bounds: {
                        left: Math.max(
                            view.left,
                            Math.min(...contentRects.map(rect => rect.left))
                        ),
                        top: Math.max(
                            view.top,
                            Math.min(...contentRects.map(rect => rect.top))
                        ),
                        right: Math.min(
                            view.right,
                            Math.max(...contentRects.map(rect => rect.right))
                        ),
                        bottom: Math.min(
                            view.bottom,
                            Math.max(...contentRects.map(rect => rect.bottom))
                        ),
                    },
                };
            }
            """
        )
        visible = visible_info["indices"]
        if not visible:
            raise RuntimeError("聊天区域可见，但没有识别到当前屏幕中的消息。")

        message_range = (min(visible), max(visible))
        if message_range not in seen_ranges:
            seen_ranges.add(message_range)
            # 按当前屏幕实际气泡的外接范围框选，而不是复用固定面板尺寸。
            # 四边余量彼此独立，让短消息、长消息、左右对话自然得到不同
            # 宽高；只增加空白，不侵入气泡，所以不会为了“不工整”截字。
            bounds = visible_info["bounds"]
            left = max(0, bounds["left"] - random.randint(8, 30))
            top = max(0, bounds["top"] - random.randint(4, 28))
            right = min(
                viewport["width"],
                bounds["right"] + random.randint(5, 22),
            )
            bottom = min(
                viewport["height"],
                bounds["bottom"] + random.randint(8, 35),
            )
            screenshot_path = screenshot_dir / (
                f"page_{page_number:03d}_screen_{screen_number:03d}_"
                f"msg_{message_range[0]:03d}-{message_range[1]:03d}.png"
            )
            page.screenshot(
                path=str(screenshot_path),
                clip={
                    "x": left,
                    "y": top,
                    "width": max(1, right - left),
                    "height": max(1, bottom - top),
                },
                animations="disabled",
                scale="css",
            )
            captures.append({
                "path": screenshot_path,
                "start": message_range[0],
                "end": message_range[1],
                "full_indices": visible_info.get("fullIndices", []),
                "includes_time": True,
            })
            print(
                f"第 {page_number} 页聊天区域截图 {screen_number}："
                f"消息 {message_range[0]}-{message_range[1]}，{screenshot_path}"
            )
            screen_number += 1

        state = page.evaluate(
            """
            () => {
                const node = window.__pddNaturalChatScroller;
                return {
                    top: node.scrollTop,
                    max: Math.max(0, node.scrollHeight - node.clientHeight),
                    height: node.clientHeight,
                };
            }
            """
        )
        if state["top"] >= state["max"] - 2:
            break

        distance = int(state["height"] * random.uniform(0.65, 0.84))
        previous_top = state["top"]
        human_scroll(page, max(180, distance), 4, 9)
        if random.random() < 0.12:
            human_delay(100, 260)
            human_scroll(page, -random.randint(35, 90), 2, 4)
        human_delay(450, 1350)
        new_top = page.evaluate("window.__pddNaturalChatScroller.scrollTop")
        stalled = stalled + 1 if new_top <= previous_top + 1 else 0
        if stalled >= 3:
            print("聊天区域连续三次没有滚动，停止当前页截图。")
            break

    page.evaluate(
        """
        () => {
            window.__pddNaturalChatScroller?.removeAttribute(
                "data-pdd-natural-chat-scroller"
            );
            delete window.__pddNaturalChatScroller;
        }
        """
    )
    return captures

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
        screenshot_captures = (
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
        for record in page_records:
            page_index = int(record.get("page_index") or 0)
            matching = next(
                (
                    capture for capture in screenshot_captures
                    if capture["start"] <= page_index <= capture["end"]
                ),
                screenshot_captures[0] if screenshot_captures else None,
            )
            if matching:
                record["evidence_screenshot"] = to_relative_evidence_path(
                    matching["path"]
                )
        if page_records:
            page_records[0]["page_evidence_details"] = [
                {
                    "path": to_relative_evidence_path(capture["path"]),
                    "page": current_page,
                    "full_indices": capture.get("full_indices", []),
                    "includes_time": capture.get("includes_time", False),
                }
                for capture in screenshot_captures
            ]
            page_records[0]["page_evidence_screenshots"] = [
                to_relative_evidence_path(capture["path"])
                for capture in screenshot_captures
            ]

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

        # 翻页前先清障；按钮由 DOM 属性识别，实际动作使用鼠标移动和点击。
        clear_blockers(page)

        next_result = click_next_page(page)

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

    # 每单从新文档查询，清除上一单内容及分页/滚动状态。
    page.goto(START_URL, wait_until="domcontentloaded", timeout=30_000)
    clear_blockers(page)

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

    clear_blockers(page)
    query_button.scroll_into_view_if_needed(timeout=8000)
    # Locator.click 会检查遮挡、稳定性和实际可点击性，失败直接停止。
    query_button.click(timeout=8000)

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


def build_chat_record_dict(
    order_no: str,
    records: list[dict],
    no_chat_record_screenshot: Path | None = None,
    issue_type: str = "",
) -> dict:
    """把聊天记录拼成 save_chat_records() 原来写进 JSON 文件的那个 dict。

    纯内存计算，不碰磁盘——从 save_chat_records() 里抽出来，供它自己（继续
    写 chat_<订单号>.json，独立调试工具在用）和 run_appeal_batch.py 的
    process_single_order()（改成直接写数据库，不落这份 JSON）共用，避免
    两处重复拼 evidence_screenshots 列表。

    no_chat_record_screenshot：订单查询结果为"暂无聊天记录"时的
    凭证截图路径；订单确实有聊天记录时传 None。
    issue_type：订单问题类型（"商品问题"/"服务问题"），从申诉列表表格采集。
    """
    evidence_screenshots: list[str] = []
    for record in records:
        evidence_screenshots.extend(record.get("page_evidence_screenshots") or [])
        if record.get("evidence_screenshot"):
            evidence_screenshots.append(record["evidence_screenshot"])
    evidence_screenshots = list(dict.fromkeys(evidence_screenshots))

    if no_chat_record_screenshot is not None:
        evidence_screenshots.append(
            to_relative_evidence_path(no_chat_record_screenshot)
        )

    return {
        "order_no": order_no,
        "issue_type": issue_type,
        "message_count": len(records),
        "evidence_screenshots": evidence_screenshots,
        "messages": records,
    }


def save_chat_records(
    order_no: str,
    records: list[dict],
    no_chat_record_screenshot: Path | None = None,
    issue_type: str = "",
) -> Path:
    """
    将聊天记录保存成 JSON 文件。

    只剩独立调试工具（本文件自己的 main()、appeal_classifier.py 的
    batch_from_output_dir() 兼容兜底）在用；main.py 批量流水线已经改成
    直接写数据库（见 run_appeal_batch.py 的 process_single_order()），
    不再落这份 chat_<订单号>.json。
    """

    OUTPUT_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    filename = (
        f"chat_{safe_filename(order_no)}.json"
    )

    output_file = OUTPUT_DIR / filename

    result = build_chat_record_dict(
        order_no, records, no_chat_record_screenshot, issue_type,
    )

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
