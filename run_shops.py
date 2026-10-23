"""多店铺串行随机调度器。

自动扫描 state/profiles/ 下已登录的店铺目录，随机打乱顺序后以子进程方式
逐店运行 main.py 的完整全流程（采集订单号 + 逐单分析并立刻提交申诉）。

调度规则：
- 同一时刻只操作一家店铺（subprocess.run 阻塞等待，天然串行）；
- 一家店做完后随机跳到下一家（每轮 random.shuffle 洗牌）；
- 一轮跑完自动重新洗牌开始下一轮，无限循环直到手动 Ctrl+C 停止；
- 某店铺子进程非零退出码时记录异常并继续下一家，不中断整轮。

调度粒度说明：本调度器换店铺的粒度是"一整家店的一整轮"——它调用
`main.py --shop <shop>` 时不带任何阶段参数，因此每家店内部会走 main.py
的默认"逐单模式"（分析完一单立刻提交，再处理下一单），但仍然是这家店的
订单全部处理完才轮到下一家店，不是"提交完一单就跨店铺切换"。真正的
"提交完一单随机换店铺"需要在一个进程里同时保持多个店铺的浏览器会话，
是尚未实现的后续工作，不要与这里的店铺级调度混淆。

用法：
  python run_shops.py                             无限循环串行调度
  python run_shops.py --once                      只跑一轮就退出（调试用）
  python run_shops.py --confirm                   真实提交申诉（透传每家店）
  python run_shops.py --confirm --manual-captcha  真提交 + 人工滑验证码（透传）
"""

from __future__ import annotations

import argparse
import random
import subprocess
import sys
import time
from pathlib import Path

# 调度器自身不 import paths（避免与店铺路径固化耦合），项目根目录直接取自
# 本文件位置，与 paths.BASE_DIR 指向同一目录。
BASE_DIR = Path(__file__).resolve().parent
MAIN_PY = BASE_DIR / "main.py"
PROFILES_DIR = BASE_DIR / "state" / "profiles"


def discover_shops() -> list[str]:
    """扫描 state/profiles/ 下的一级子目录，返回店铺 slug 列表。

    过滤以 . 开头的隐藏目录；目录不存在或为空时返回空列表。
    目录名可直接作为 --shop 回传：paths._slugify_shop() 幂等，
    slug 化后仍指向同一目录。
    """
    if not PROFILES_DIR.is_dir():
        return []

    return sorted(
        d.name
        for d in PROFILES_DIR.iterdir()
        if d.is_dir() and not d.name.startswith(".")
    )


def run_one_shop(shop: str, extra: list[str]) -> int:
    """为单个店铺启动 main.py 子进程并阻塞等待其完成。

    子进程由 main.py 顶部 _bootstrap_shop_env() 自行从 argv 抓取 --shop，
    无需调度器手动设置环境变量。返回子进程退出码。
    """
    cmd = [sys.executable, str(MAIN_PY), "--shop", shop, *extra]

    print()
    print(">" * 70)
    print(f"开始操作店铺 [{shop}]")
    print(f"子进程: {Path(sys.executable).name} main.py --shop {shop}" + (" " + " ".join(extra) if extra else ""))
    print("<" * 70)

    try:
        proc = subprocess.run(cmd, cwd=str(BASE_DIR))
        return proc.returncode
    except FileNotFoundError:
        print(f"[调度器] 找不到入口脚本 {MAIN_PY}，跳过该店铺。")
        return -1


def main() -> None:
    parser = argparse.ArgumentParser(
        description="多店铺串行随机调度器：随机轮换店铺，一次只操作一家",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="只跑一轮就退出（便于调试）",
    )
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="透传给每家店：填完表单后真实提交申诉（默认点取消）",
    )
    parser.add_argument(
        "--manual-captcha",
        action="store_true",
        help="透传给每家店：关闭自动破解验证码，等待人工手动滑动",
    )
    args = parser.parse_args()

    extra: list[str] = []
    if args.confirm:
        extra.append("--confirm")
    if args.manual_captcha:
        extra.append("--manual-captcha")

    shops = discover_shops()
    if not shops:
        print(f"[调度器] 未在 {PROFILES_DIR} 下发现任何店铺目录，退出。")
        return

    print(f"[调度器] 发现 {len(shops)} 家店铺: {', '.join(shops)}")
    if extra:
        print(f"[调度器] 透传参数: {' '.join(extra)}")
    if args.once:
        print("[调度器] --once 模式：本轮结束后退出。")
    else:
        print("[调度器] 无限循环模式：Ctrl+C 停止。")

    round_no = 0
    try:
        while True:
            round_no += 1

            # 每轮重新洗牌，实现"做完一家随机跳下一家"
            order = shops[:]
            random.shuffle(order)

            print()
            print("=" * 70)
            print(f"[调度器] 第 {round_no} 轮开始")
            print(f"[调度器] 本轮顺序: {', '.join(order)}")
            print("=" * 70)

            for shop in order:
                returncode = run_one_shop(shop, extra)

                if returncode != 0:
                    print(
                        f"[调度器] 店铺 [{shop}] 异常退出（退出码 {returncode}），"
                        "记录后继续下一家。"
                    )
                else:
                    print(f"[调度器] 店铺 [{shop}] 本轮完成 ✓")

                # 店铺之间随机停顿 1~3 秒，避免节奏过于机械
                time.sleep(random.uniform(1.0, 3.0))

            if args.once:
                print(f"\n[调度器] --once 模式：本轮全部店铺处理完毕，退出。")
                return

    except KeyboardInterrupt:
        print("\n[调度器] 收到 Ctrl+C，停止调度。")
        return


if __name__ == "__main__":
    main()
