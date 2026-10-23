"""Account login with a bounded, human-operated verification step."""
import math
from time import monotonic
from urllib.parse import urlparse

from .models import Cancelled

LOGIN_URL = "https://mms.pinduoduo.com/login/"


class LoginError(RuntimeError):
    pass


class VerificationTimeout(LoginError):
    pass


def account_login(page, username, password, stop_event, waiting=lambda message: None,
                  verification_timeout=300, login_timeout=45):
    """Never emit Playwright exception text: fill() call logs can contain a password."""
    def check():
        if stop_event.is_set():
            raise Cancelled()
        if page.is_closed():
            raise LoginError("登录窗口已关闭，请重试这家店铺。")

    def visible(selector):
        elements = page.locator(selector)
        return any(elements.nth(index).is_visible() for index in range(elements.count()))

    try:
        check()
        # Reauthenticate only this shop's dedicated profile. Do not affect the user's browser.
        page.context.clear_cookies()
        page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=45000)
        if urlparse(page.url).hostname != "mms.pinduoduo.com":
            raise LoginError("登录页地址异常，未输入账号密码。")
        page.evaluate("() => { localStorage.clear(); sessionStorage.clear(); }")
        page.context.clear_cookies()
        page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=45000)
        check()
        if urlparse(page.url).hostname != "mms.pinduoduo.com":
            raise LoginError("登录页地址异常，未输入账号密码。")
        page.bring_to_front()
        page.locator('[data-tracking-click-viewid="password_log"]').click()
        user_input = page.locator("#usernameId")
        password_input = page.locator("#passwordId")
        user_input.fill("")
        password_input.fill("")
        check()
        user_input.fill(username)
        password_input.fill(password)
        check()
        page.locator('button[data-tracking-click-viewid="account_login_confirmation"]').click()
        deadline = monotonic() + login_timeout
        verification_deadlines = {}
        last_verification = None
        last_notice = None
        while True:
            check()
            # A visible challenge takes precedence over a background header or redirect.
            if visible('#slide-captcha-dialog, .captcha-wrapper__slider'):
                verification = "slider"
            elif visible('.verify-phone-container, input[placeholder="请输入短信验证码"]'):
                verification = "sms"
            else:
                verification = None
            # A redirect alone isn't proof of authentication; the worker checks the protected page next.
            location = urlparse(page.url)
            if verification is None:
                if location.hostname == "mms.pinduoduo.com" and not location.path.startswith("/login"):
                    return
                if visible('.mms-header__user-info'):
                    return
                # Only known phrases; never echo arbitrary page text or input values.
                error = page.get_by_text("账号或密码错误", exact=False)
                if any(error.nth(i).is_visible() for i in range(error.count())):
                    raise LoginError("账号或密码错误，请编辑该店铺的账号密码后重试。")
            if verification:
                if verification not in verification_deadlines:
                    verification_deadlines[verification] = monotonic() + verification_timeout
                if verification != last_verification:
                    page.bring_to_front()
                last_verification = verification
            if last_verification is not None:
                # Reappearing/closed challenges do not reset their deadline or prove login success.
                remaining = math.ceil(verification_deadlines[last_verification] - monotonic())
                label = "滑块拼图验证" if last_verification == "slider" else "短信验证"
                if remaining <= 0:
                    raise VerificationTimeout(f"{label}等待超时（最多 5 分钟），已跳过；可在完成准备后重试登录。")
                notice = (verification, remaining)
                if notice != last_notice and (last_notice is None or verification != last_notice[0] or remaining % 5 == 0):
                    if verification == "slider":
                        message = f"等待人工滑块拼图验证，剩余 {remaining} 秒；请在浏览器中向右拖动滑块完成拼图。"
                    elif verification == "sms":
                        message = f"等待人工短信验证，剩余 {remaining} 秒；请在浏览器输入验证码并点击“确认”。"
                    else:
                        message = f"等待{label}后的登录结果，剩余 {remaining} 秒；如关闭了验证弹窗，请在浏览器按页面提示继续登录。"
                    waiting(message)
                    last_notice = notice
            else:
                if monotonic() >= deadline:
                    raise LoginError("登录未完成，请检查账号密码或在单店手动登录中处理页面提示后重试。")
            page.wait_for_timeout(150)
    except (Cancelled, LoginError):
        raise
    except Exception:
        check()
        raise LoginError("账号登录操作未完成，请检查网络、登录页面或账号信息后重试。") from None
