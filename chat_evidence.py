"""从连续聊天原图中优先选择一张、最多两张；不拼接、不删消息。"""
from pathlib import Path


DISPUTE_TERMS = ("问题", "质量", "损坏", "破损", "错发", "退款", "退货", "不符", "漏发", "投诉")
SUPPORT_TERMS = ("我买错", "我拍错", "看错", "误会", "已解决", "可以用了", "弄错", "型号", "规格", "接口", "兼容")


def select_chat_evidence(files: list[Path], order: dict | None) -> list[Path]:
    """优先完整问答及后续反馈，相关性排序只影响选屏，不判定商家无责。

    一张不能完整覆盖时选两张相邻原图；仍不足时保留相关覆盖最多的两张，
    明确记录局部覆盖，不因上下文过长跳过订单。话术仍使用完整聊天记录。
    """
    if not files:
        return []
    messages = (order or {}).get("messages") or []
    if not messages:
        empty = next(([p] for p in files if p.name == "no_chat_record.png"), None)
        if empty:
            return empty
    records = sorted(messages, key=lambda m: (int(m.get("page_number") or 0), int(m.get("page_index") or 0)))
    turns = []
    for record in records:
        if record.get("is_system_message"):
            continue
        role = "B" if "*" in str(record.get("sender") or "") else "S"
        if turns and turns[-1][0] == role:
            turns[-1][1].append(record)
        else:
            turns.append((role, [record]))
    windows = []
    reason = str((order or {}).get("appeal_text") or "")
    hints = {reason[i:i + 2] for i in range(len(reason) - 1)
             if all("\u4e00" <= c <= "\u9fff" for c in reason[i:i + 2])}
    for index, (role, turn) in enumerate(turns):
        if role != "B":
            continue
        # 买家完整发言 + 客服完整回应 + 紧随其后的买家反馈。
        # 首条可见消息之前若有客服说明，也保留，避免截成孤立的“同意”。
        first = max(0, index - 2)
        last = min(len(turns), index + 3)
        context = [m for _, group in turns[first:last] for m in group]
        content = " ".join(str(m.get("content") or "") for m in context)
        score = sum(4 for term in DISPUTE_TERMS if term in content)
        score += sum(3 for term in SUPPORT_TERMS if term in content)
        score += min(6, sum(1 for hint in hints if hint in content))
        windows.append((context, score))
    captures = []
    seen = set()
    paths = {str(p).replace("\\", "/"): p for p in files}
    for record in records:
        for capture in record.get("page_evidence_details") or []:
            if not capture.get("includes_time"):
                continue
            relative = str(capture.get("path") or "").replace("\\", "/")
            path = next((p for name, p in paths.items() if name == relative or name.endswith("/" + relative)), None)
            if path is None:
                continue
            if path in seen:
                continue
            seen.add(path)
            full = {(capture.get("page"), index) for index in capture.get("full_indices") or []}
            captures.append((path, full, capture.get("page") or 0))
    captures.sort(key=lambda item: (item[2], item[0].name))

    def key(message):
        return message.get("page_number"), message.get("page_index")

    def evaluate(group):
        covered = set().union(*(item[1] for item in group))
        complete = []
        partial = []
        for context, score in windows:
            required = {key(m) for m in context}
            # 同时保留两端之间的系统消息，不把有缺口的覆盖视为完整。
            positions = [i for i, m in enumerate(records) if key(m) in required]
            if positions:
                required.update(key(m) for m in records[min(positions):max(positions) + 1])
            overlap = len(required & covered)
            if required and required <= covered:
                complete.append((score, len(required)))
            partial.append((overlap, score))
        return max(complete, default=None), max(partial, default=(0, 0))

    singles = [(evaluate([item]), [item]) for item in captures]
    complete_singles = [(metrics[0], group) for metrics, group in singles if metrics[0] is not None]
    if complete_singles:
        _, selected = max(complete_singles, key=lambda entry: entry[0])
        status = "完整上下文"
    else:
        # 仅选同一采集批次里相邻原图，不拼接跨批次画面。
        pairs = [[left, right] for left, right in zip(captures, captures[1:])
                 if left[0].parent == right[0].parent]
        scored_pairs = [(evaluate(group), group) for group in pairs]
        complete_pairs = [(metrics[0], group) for metrics, group in scored_pairs if metrics[0] is not None]
        if complete_pairs:
            _, selected = max(complete_pairs, key=lambda entry: entry[0])
            status = "两张共同覆盖完整上下文"
        elif scored_pairs or singles:
            _, selected = max(scored_pairs or singles, key=lambda entry: entry[0][1])
            status = "上下文未完整覆盖，按最多两张保留相关连续原图；继续处理，分析使用完整聊天"
        else:
            # 旧截图缺少覆盖元数据时保持原始顺序，最多取同目录连续两张。
            unique = list(dict.fromkeys(files))
            selected = [(p, set(), 0) for p in unique if p.parent == unique[0].parent][:2]
            status = "旧截图缺少覆盖元数据，无法确认完整上下文；保留原图并继续处理"
    result = [item[0] for item in selected]
    print(f"[聊天凭证] 选择{len(result)}张：{'、'.join(p.name for p in result)}；{status}。")
    return result
