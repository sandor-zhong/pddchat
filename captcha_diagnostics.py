"""Per-attempt captcha records; no browser profiles or full-page dumps."""
from __future__ import annotations

import json
import os
import re
import uuid
import zipfile
from datetime import datetime
from pathlib import Path

SESSION_ID = f"{datetime.now():%Y%m%d-%H%M%S}-{os.getpid()}-{uuid.uuid4().hex[:8]}"


def save_attempt(root: Path, record: dict, artifacts: dict[str, bytes]) -> None:
    folder = root / SESSION_ID / record["id"]
    folder.mkdir(parents=True, exist_ok=True)
    # Save only failures; success statistics do not need image/HTML retention.
    saved = []
    if not record["passed"]:
        for name, data in artifacts.items():
            (folder / name).write_bytes(data)
            saved.append(name)
    record = dict(record, session=SESSION_ID, artifacts=saved)
    pending = folder / "record.tmp"
    pending.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    pending.replace(folder / "record.json")


def export_bundle(root: Path, destination: str) -> dict:
    summary = {"attempts": 0, "passed": 0, "failed": 0, "by_shop_and_type": {}}
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
        for record_file in root.glob("*/*/record.json"):
            record = json.loads(record_file.read_text(encoding="utf-8"))
            summary["attempts"] += 1
            status = "passed" if record["passed"] else "failed"
            summary[status] += 1
            key = f"{record['shop']} / {record['type']}"
            group = summary["by_shop_and_type"].setdefault(key, {"attempts": 0, "passed": 0, "failed": 0})
            group["attempts"] += 1
            group[status] += 1
            archive.write(record_file, record_file.relative_to(root).as_posix())
            for name in record.get("artifacts", []):
                if name not in {"captcha.png", "image.png", "element.html"} and not re.fullmatch(r"refresh_[12]_(before|after)\.(png|html)", name):
                    continue
                artifact = record_file.parent / name
                if artifact.is_file():
                    archive.write(artifact, artifact.relative_to(root).as_posix())
        for group in [summary, *summary["by_shop_and_type"].values()]:
            group["pass_rate"] = group["passed"] / group["attempts"] if group["attempts"] else None
        archive.writestr("summary.json", json.dumps(summary, ensure_ascii=False, indent=2))
        archive.writestr("README.txt", "字符/点选验证码诊断包\n统计口径：每次自动处理尝试；通过=处理后验证码消失，失败包括接口、识别、输入或点击异常。不是订单成功率，也不统计人工通过。\n按 shop/type 汇总；session 为工作进程，challenge_id 关联同一次验证码处理的多次尝试。\n失败样本保存处理前的 captcha.png、image.png、element.html（静态元素，非可直接联网重放的网页）。record.json 包含时间、耗时、题型、诊断过程及采集错误。刷新后截图不作为本次识别原图。\n仅完整落盘的记录参与导出；运行中未结束的尝试不在包内。\n")
    return summary
