"""独立测试 .env 中配置的 GPT-6 接口，不切换正式申诉模型。"""
from pathlib import Path

from dotenv import dotenv_values
from openai import OpenAI, APIError


def main() -> int:
    config = dotenv_values(Path(__file__).resolve().parent / ".env")
    required = ("GPT6_API_KEY", "GPT6_BASE_URL", "GPT6_MODEL")
    missing = [name for name in required if not config.get(name)]
    if missing:
        print("缺少配置：" + ", ".join(missing))
        return 1
    try:
        with OpenAI(
            api_key=config["GPT6_API_KEY"],
            base_url=config["GPT6_BASE_URL"],
            timeout=60.0,
            max_retries=0,
        ) as client:
            response = client.chat.completions.create(
                model=config["GPT6_MODEL"],
                messages=[{"role": "user", "content": "这是接口连通测试，请只回复：连接成功"}],
                max_completion_tokens=512,
            )
    except APIError as exc:
        # 不输出服务端错误正文，避免第三方接口回显认证信息。
        print(f"接口调用失败：{type(exc).__name__}，状态码：{getattr(exc, 'status_code', None)}")
        return 1
    content = response.choices[0].message.content if response.choices else None
    if not content or not content.strip():
        print("接口已响应，但没有返回正文。")
        return 1
    # 不打印未经处理的服务端内容，防止回显密钥。
    for name in required:
        if name.endswith("API_KEY") and config[name]:
            content = content.replace(config[name], "[REDACTED]")
    print("请求模型：" + config["GPT6_MODEL"])
    print("响应模型：" + str(response.model))
    print("模型回复：" + content)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
