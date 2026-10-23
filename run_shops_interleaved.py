"""多店铺逐单跨店随机调度器（子进程常驻 + 指令驱动 + 固定并发上限）。

跟 run_shops.py 的区别在调度粒度：run_shops.py 是"整店一整轮"——一家店的
全部订单处理完才换下一家；这个脚本调度到"单个订单"粒度，而且**同时最多
只开 N 家店铺的浏览器**（默认 4，`--max-concurrent` 可调）：谁的订单先
处理完，就关掉它的浏览器，从还没处理过的店铺里挑一个顶上来补位，直到
这一轮所有店铺都处理完。

调度规则：
- 每一轮开始前，先查 db.py 的 shop_daily_status 表——今天（本地日期）已经
  跑完（status='done'）的店铺直接跳过，连浏览器都不开；只有失败过
  （status='failed'，比如登录失效）或者今天还没跑过的才会真的处理。
  这样重复运行（无论是同一个调度器进程的下一轮，还是隔一段时间重新起
  一次这个脚本）都不会白白重新采集一遍已经处理完的店铺；
- 同时最多 N 家店铺的浏览器处于打开状态（N = --max-concurrent，默认 4），
  不管这次发现了多少家店铺，并发数量都不会超过这个上限；
- 这 N 家里，同一时刻只有一家在真正操作（发指令、等回应严格串行，不并发，
  补位新开浏览器这个动作本身也是串行的，不会两家同时 init）；
- 某家店铺订单处理完（收到 queue_empty）：标记今天 done、关掉它的浏览器
  子进程、让出名额；如果还有没处理过的店铺在排队，立刻开一个新的顶上来
  （同样先 init 采集再加入轮转）；排队的店铺都用完了，名额就空着，直到
  这一轮结束；
- 一轮跑完（这次要处理的店铺都处理过一遍）自动重新开始下一轮，无限循环，
  直到手动 Ctrl+C 或 --once 停止；如果这一轮所有店铺都是 done（没有需要
  处理的），会原地等待、定期重新检查，不会自己退出（除非 --once）。

实际调度逻辑在 scheduler_core.py（Scheduler 类）——这个文件只是它的 CLI
外壳：解析命令行参数、把状态变化事件（SchedulerEvent）还原成终端打印，
本身不含任何调度算法。之所以拆开，是因为 scheduler_core.py 要同时给这个
CLI 和 gui/ 子项目（独立的旧版本 Python venv）复用，不能依赖任何 UI 框架。

协议细节见 main.py 的 run_serve_mode() 文档字符串（stdin/stdout 上的
JSON Lines 指令：init/next/stop → ready/result/queue_empty/error）。

用法：
  python run_shops_interleaved.py                              无限循环，同时最多开 4 家店铺
  python run_shops_interleaved.py --max-concurrent 6             同时最多开 6 家店铺
  python run_shops_interleaved.py --once                        只跑一轮（所有店铺都处理完一遍）就退出
  python run_shops_interleaved.py --confirm                     真实提交申诉（透传每家店）
  python run_shops_interleaved.py --confirm --manual-captcha    真提交 + 人工滑验证码（透传，会放宽响应超时）
  python run_shops_interleaved.py --reanalyze                   强制重新分析已分析过的订单（透传）
"""

from __future__ import annotations

import argparse

from scheduler_core import (
    DEFAULT_MAX_CONCURRENT,
    DEFAULT_MAX_CONSECUTIVE_ERRORS,
    DEFAULT_RESPONSE_TIMEOUT,
    MANUAL_CAPTCHA_RESPONSE_TIMEOUT,
    PROFILES_DIR,
    Scheduler,
    SchedulerEvent,
    discover_shops,
)


def _print_event(event: SchedulerEvent) -> None:
    """把调度器事件原样打印——event.line 是预先格式化好的完整文本，
    忠实对应重构前那一行/那几行 print()，逐字保持终端输出不变。"""
    if event.line:
        print(event.line)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "多店铺逐单跨店随机调度器：同时最多开 N 家店铺，"
            "谁先做完谁换还没处理过的店铺补位"
        ),
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="只跑一轮（所有店铺都处理完一遍）就退出（便于调试）",
    )
    parser.add_argument(
        "--max-concurrent",
        type=int,
        default=DEFAULT_MAX_CONCURRENT,
        help=(
            f"同时最多打开几家店铺的浏览器（默认 {DEFAULT_MAX_CONCURRENT}）；"
            "一家处理完就关掉，换还没处理过的店铺顶上来，不管这次发现多少"
            "家店铺，并发数都不会超过这个值"
        ),
    )
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="透传给每家店：填完表单后真实提交申诉（默认点取消）",
    )
    parser.add_argument(
        "--manual-captcha",
        action="store_true",
        help="透传给每家店：关闭自动破解验证码，等待人工手动滑动（同时放宽调度器的响应超时）",
    )
    parser.add_argument(
        "--reanalyze",
        action="store_true",
        help="透传给每家店：强制重新分析数据库里已有结果的订单",
    )
    parser.add_argument(
        "--max-consecutive-errors",
        type=int,
        default=DEFAULT_MAX_CONSECUTIVE_ERRORS,
        help=(
            f"某店铺连续失败几单就判定它状态异常（比如登录失效），"
            f"关闭并让位给排队里还没处理的店铺（默认 {DEFAULT_MAX_CONSECUTIVE_ERRORS}；"
            "期间只要有一单不是失败，计数就清零重新数）"
        ),
    )
    args = parser.parse_args()

    extra: list[str] = []
    if args.confirm:
        extra.append("--confirm")
    if args.manual_captcha:
        extra.append("--manual-captcha")
    if args.reanalyze:
        extra.append("--reanalyze")

    response_timeout = (
        MANUAL_CAPTCHA_RESPONSE_TIMEOUT if args.manual_captcha else DEFAULT_RESPONSE_TIMEOUT
    )
    pool_size = max(1, args.max_concurrent)
    max_consecutive_errors = max(1, args.max_consecutive_errors)

    shops = discover_shops()
    if not shops:
        print(f"[调度器] 未在 {PROFILES_DIR} 下发现任何店铺目录，退出。")
        return

    print(f"[调度器] 发现 {len(shops)} 家店铺: {', '.join(shops)}")
    print(f"[调度器] 并发上限: 同时最多 {pool_size} 家店铺打开浏览器")
    print(f"[调度器] 容错阈值: 某店铺连续失败 {max_consecutive_errors} 单就判定异常、关闭让位")
    if extra:
        print(f"[调度器] 透传参数: {' '.join(extra)}")
    if args.once:
        print("[调度器] --once 模式：本轮所有店铺处理完毕后退出。")
    else:
        print("[调度器] 无限循环模式：Ctrl+C 停止。")

    scheduler = Scheduler(
        shops=shops,
        extra=extra,
        pool_size=pool_size,
        max_consecutive_errors=max_consecutive_errors,
        response_timeout=response_timeout,
        on_event=_print_event,
    )

    try:
        if args.once:
            scheduler.run_once()
        else:
            scheduler.run_forever()
    except KeyboardInterrupt:
        print("\n[调度器] 收到 Ctrl+C，停止调度。")
        scheduler.request_stop()


if __name__ == "__main__":
    main()
