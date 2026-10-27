"""复议和售后文案：参考成功样本的简洁表达，不依赖外部样本文件。"""

import json

from appeal_classifier import get_client
from appeal_style import evidence_style


EVIDENCE_STYLE = evidence_style()

RECONSIDERATION_DESCRIPTION_PROMPT = """你根据本订单聊天和选定原因撰写复议描述。
本次针对负向体验处罚，不是货款或运费申诉。有依据时解释规格或使用条件的理解分歧；
仅有咨询时说明咨询内容；故障原因不明就不下结论，不强行声称商品无问题。
从具体沟通里找到能支持商家立场的事实，把我们的理由讲清楚后直接结束，
不加请平台处理或重新审核的套话。不超过200字。
""" + EVIDENCE_STYLE

AFTERSALE_DESCRIPTION_PROMPT = """你根据本订单聊天、申诉项和选定原因撰写售后申诉描述。
货款：口语化说明与扣款争议相关的具体事实，突出商家有依据的解释和实际处理，
不把同意退款直接当作商品无问题，也不固定附加撤销、退款或复核请求。
运费：有买家明确误拍、不需要等证据时，说明个人退货原因及费用争议；不能仅因缺少
视频、客服引导选择退款原因或提出免退方案就认定无理由退货。不能确定退货原因时，
只写沟通中明确的事情，不添加责任无法划分等收尾。不能凭空宣称不支持七天无理由。
组合申诉：围绕实际申诉项和选定原因，不添加未选择的费用或请求。不超过300字。
""" + EVIDENCE_STYLE


def generate_reconsideration_description(compact_chat: dict, reason: str) -> str:
    """从原始聊天生成复议文案，不把首次申诉文案当事实改写。"""
    if not compact_chat.get("m"):
        return (
            "这单的具体沟通情况还不清楚，我们对这笔负向体验处罚有异议。"
        )
    response = get_client().chat.completions.create(
        model="deepseek-v4-flash",
        messages=[
            {"role": "system", "content": RECONSIDERATION_DESCRIPTION_PROMPT},
            {"role": "user", "content": json.dumps(
                {"chat": compact_chat, "复议原因": reason}, ensure_ascii=False
            )},
        ],
        temperature=0.2,
        max_tokens=600,
        extra_body={"thinking": {"type": "disabled"}},
        timeout=30,
    )
    text = (response.choices[0].message.content or "").strip()
    if not text or len(text) > 200:
        raise RuntimeError("复议文案为空或超过200字，请重新生成后提交。")
    return text
