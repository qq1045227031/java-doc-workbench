"""Portable verified TLS and credential-free network diagnostics."""
from __future__ import annotations

import socket
import ssl
import urllib.error
import urllib.request

import certifi


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def timeout_seconds(profile):
    try:
        seconds = int(profile.get("timeoutSeconds", 180))
    except (TypeError, ValueError):
        raise ValueError("超时秒数必须为 15～600 的整数") from None
    if not 15 <= seconds <= 600:
        raise ValueError("超时秒数必须为 15～600 的整数")
    return seconds


def opener(profile):
    # Keep OS/enterprise trust and add bundled Mozilla roots. Never disable verification.
    context = ssl.create_default_context()
    context.load_verify_locations(cafile=certifi.where())
    handlers = [NoRedirect, urllib.request.HTTPSHandler(context=context)]
    mode = profile.get("proxyMode", "system")
    if mode not in {"system", "direct"}:
        raise ValueError("代理方式必须为 system 或 direct")
    if mode == "direct":
        handlers.append(urllib.request.ProxyHandler({}))
    return urllib.request.build_opener(*handlers)


def network_error(exc, profile):
    reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
    proxy = "使用系统代理" if profile.get("proxyMode", "system") == "system" else "直接连接"
    if isinstance(reason, ssl.SSLCertVerificationError):
        return f"TLS 证书校验失败（{proxy}）。请检查系统时间、公司代理证书或证书链；程序不会跳过证书验证。"
    if isinstance(reason, ssl.SSLError):
        return f"TLS 加密连接失败（{proxy}），尚未获得 API 身份验证结果；请检查代理和服务地址。"
    if isinstance(reason, socket.gaierror):
        return f"DNS 域名解析失败（{proxy}）。请检查 API 域名、DNS 或代理地址。"
    if isinstance(reason, (TimeoutError, socket.timeout)) or getattr(reason, "winerror", None) == 10060:
        return f"连接或响应超时（{proxy}）。可先测试连接，或减小批次、增加超时秒数。"
    if isinstance(reason, ConnectionRefusedError) or getattr(reason, "winerror", None) == 10061:
        return f"连接被拒绝（{proxy}）。请检查代理服务是否启动，或切换连接方式后测试。"
    if isinstance(reason, ConnectionResetError):
        return f"连接被对端或代理重置（{proxy}），尚无法判断 Key 是否有效。"
    return f"网络连接未完成（{proxy}，{type(reason).__name__}）。请运行“测试连接”并导出脱敏诊断。"


def http_error(status):
    messages = {
        400: "服务已连接，但请求参数不被接受。请核对模型、服务类型及 JSON 输出设置。",
        401: "服务已连接，身份验证未通过。请核对 Key 所属平台与 API 地址是否对应。",
        402: "服务已连接，服务商要求检查账户余额或计费状态。",
        403: "服务已连接，但访问被拒绝。请检查权限、地区或网络访问策略。",
        404: "服务已连接，但接口路径或模型未找到。请核对 API 基础地址和模型名。",
        407: "代理要求身份验证，请在本机配置好代理后再测试。",
        429: "服务已连接，但受到速率或账户额度限制。请检查服务商控制台。",
    }
    return f"HTTP {status}：" + messages.get(status, "服务返回异常状态，请检查服务状态和 API 地址。")
