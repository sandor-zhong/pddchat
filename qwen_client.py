"""可独立复制的千问图片识别调用。依赖：pip install openai httpx

用法：
    with create_client(api_key="你的 API Key") as client:
        result = recognize_captcha(client, "captcha.png", enable_thinking=False)

不自动加载 .env、不打印日志、不压缩图片。API 异常直接交给调用方处理。
"""

import base64
import json
import math
import os
from pathlib import Path

import httpx
from openai import OpenAI

MODEL = "qwen3.8-max"
BASE_URL = "https://ws-p5co90b56o051vy2.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"
REQUEST_TIMEOUT = httpx.Timeout(connect=10, read=60, write=30, pool=10)


def image_to_data_uri(image_path: str | Path) -> str:
    """将本地图片转换为 Base64 Data URI。"""
    path = Path(image_path)
    if not path.is_file():
        raise FileNotFoundError(f"图片不存在：{path.resolve()}")

    suffix_to_mime = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
        ".bmp": "image/bmp",
    }
    mime_type = suffix_to_mime.get(path.suffix.lower())
    if not mime_type:
        raise ValueError(f"不支持的图片格式：{path.suffix}")

    image_base64 = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime_type};base64,{image_base64}"


def create_client(
    api_key: str | None = None,
    *,
    base_url: str = BASE_URL,
    timeout: float | httpx.Timeout = REQUEST_TIMEOUT,
) -> OpenAI:
    """创建客户端；未传密钥时优先读取 QWEN_API_KEY，不自动重试。

    ARK_API_KEY 仅作为旧版兼容回退；新配置应使用 QWEN_API_KEY，避免把
    火山方舟密钥误传给阿里千问接口。
    """
    if api_key is None:
        api_key = os.getenv("QWEN_API_KEY") or os.getenv("ARK_API_KEY", "")
    api_key = api_key.strip()
    if not api_key:
        raise RuntimeError("请传入 api_key 或设置 QWEN_API_KEY 环境变量")
    if not api_key.isascii():
        raise RuntimeError("QWEN_API_KEY 包含非 ASCII 字符，请填写有效的 API Key")
    return OpenAI(
        base_url=base_url,
        api_key=api_key,
        timeout=timeout,
        max_retries=0,
    )


def recognize_captcha(
    client: OpenAI,
    image_path: str | Path,
    *,
    model: str = MODEL,
    enable_thinking: bool | None = None,
) -> str:
    """识别单张本地图片，返回保留大小写的字符或 UNKNOWN。

    enable_thinking 未传时读取 CAPTCHA_ENABLE_THINKING，默认关闭。
    文件、API 和返回格式错误会抛出异常。
    """
    return recognize_captcha_data_uri(
        client,
        image_to_data_uri(image_path),
        model=model,
        enable_thinking=enable_thinking,
    )


def recognize_captcha_data_uri(
    client: OpenAI,
    data_uri: str,
    *,
    model: str = MODEL,
    enable_thinking: bool | None = None,
) -> str:
    """识别 Data URI 图片，调用参数和结果规则与 recognize_captcha 一致。"""
    if not isinstance(data_uri, str) or not data_uri.startswith("data:image/"):
        raise ValueError("验证码图片必须是 data:image/... 格式的 Data URI")
    if enable_thinking is None:
        enable_thinking = os.getenv("CAPTCHA_ENABLE_THINKING", "false").strip().lower() in {
            "1", "true", "yes", "on"
        }
    response = client.chat.completions.create(
        model=model,
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": data_uri}},
                    {"type": "text", "text": (
                        "请识别图片中的验证码。先阅读标题或说明，确定字符选择规则。"
                        "如果要求选择打勾或打对号的字符，仅输出对应勾选字符；"
                        "如果指定其他规则，按说明执行；否则输出主体中从左到右的全部字符。"
                        "保留字母大小写，不要把标题、说明、勾号、背景或干扰线作为字符。"
                        "只输出字母和数字，不要输出空格、标点、Markdown 或解释。"
                        "无法可靠识别时只输出 UNKNOWN，不要猜测。"
                    )},
                ],
            }
        ],
        extra_body={"enable_thinking": enable_thinking},
        **({} if enable_thinking else {"max_tokens": 32}),
    )
    if not response.choices:
        raise RuntimeError("API 未返回识别结果 choices")
    raw_result = response.choices[0].message.content
    if not isinstance(raw_result, str) or not raw_result.strip():
        raise RuntimeError("API 未返回有效的识别文本")
    result = raw_result.strip()
    if not result.isascii() or not result.isalnum():
        raise RuntimeError(f"识别结果格式异常：{raw_result!r}")
    return result


def locate_geometry_captcha_data_uri(
    client: OpenAI,
    data_uri: str,
    question: str,
    *,
    model: str = MODEL,
    enable_thinking: bool = False,
) -> dict[str, object]:
    """根据点选题目定位图片中的目标，返回 0~1000 归一化坐标。

    返回值已做严格校验，点击点一定在目标框内且不会落到图片边界之外。
    坐标针对传入图片；调用方可按元素当前 bounding_box 等比例换算。
    """
    if not isinstance(data_uri, str) or not data_uri.startswith("data:image/"):
        raise ValueError("验证码图片必须是 data:image/... 格式的 Data URI")
    question = str(question or "").strip()
    if not question:
        raise ValueError("点选验证码题目不能为空")

    prompt = (
        "你是视觉点击定位器。坐标统一为0到1000，左上角(0,0)，"
        "右下角(1000,1000)。\n"
        "严格按照题目选择唯一要点击的目标。对于‘与A相同/相近/对应’，"
        "A是参照物，排除A本身，再选择其他目标。几何体不包括字母和数字；"
        "要求字母时选择字母。边界框紧贴目标，点击点为框中心且必须在框内。\n"
        "检查目标名称、理由、框和点击点指向同一物体。只输出合法JSON：\n"
        '{"target_name":"目标","target_bbox_2d":[0,0,1000,1000],'
        '"target_point_2d":[500,500],"confidence":0.9,'
        '"reasoning":"简短理由"}\n'
        f"题目：{question}"
    )
    response = client.chat.completions.create(
        model=model,
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": data_uri}},
                    {"type": "text", "text": prompt},
                ],
            }
        ],
        extra_body={"enable_thinking": enable_thinking},
        **({} if enable_thinking else {"max_tokens": 256}),
    )
    if not response.choices:
        raise RuntimeError("API 未返回点选定位结果 choices")
    raw = response.choices[0].message.content
    if not isinstance(raw, str) or not raw.strip():
        raise RuntimeError("API 未返回有效的点选定位文本")
    start = raw.find("{")
    if start < 0:
        raise ValueError("点选定位模型未返回 JSON 对象")
    try:
        obj, _ = json.JSONDecoder().raw_decode(raw[start:])
        point = obj["target_point_2d"]
        bbox = obj["target_bbox_2d"]
        if len(point) != 2 or len(bbox) != 4:
            raise ValueError("坐标或边界框长度错误")
        x, y = map(float, point)
        left, top, right, bottom = map(float, bbox)
        confidence = float(obj.get("confidence", 0))
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"点选定位结果格式异常：{exc}") from exc
    values = (x, y, left, top, right, bottom, confidence)
    if not all(math.isfinite(value) for value in values):
        raise ValueError("点选定位结果包含非有限数值")
    if not (
        0 <= left < right <= 1000
        and 0 <= top < bottom <= 1000
        and left <= x <= right
        and top <= y <= bottom
        and x < 1000
        and y < 1000
        and 0 <= confidence <= 1
    ):
        raise ValueError("点选定位坐标越界、点击点不在框内或置信度无效")
    return {
        "target_name": str(obj.get("target_name", "")),
        "target_bbox_2d": [left, top, right, bottom],
        "target_point_2d": [x, y],
        "confidence": confidence,
        "reasoning": str(obj.get("reasoning", "")),
    }


