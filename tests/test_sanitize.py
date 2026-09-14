"""请求头脱敏测试

这是目前项目里唯一的脱敏能力（消息体不做内容级过滤，见 SECURITY.md），
所以它必须有测试锁死 —— 脱敏器是「静默失效」类模块：漏掉一种形态时，
用户不会收到任何报错，只会在某天发现 token 明文躺在 raw.jsonl 里。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from proxy import SENSITIVE_HEADERS, sanitize_headers_for_storage  # noqa: E402


def test_长请求头被截断为前10字符加星号():
    v = "sk-ant-api03-" + "A" * 80
    out = sanitize_headers_for_storage({"x-api-key": v})
    assert out["x-api-key"] == v[:10] + "***"
    # 关键断言：完整值不得出现在结果里的任何位置
    assert v not in out["x-api-key"]


def test_短请求头整体替换():
    out = sanitize_headers_for_storage({"authorization": "short"})
    assert out["authorization"] == "***"


def test_三个敏感头都覆盖():
    headers = {k: "Bearer " + "x" * 40 for k in SENSITIVE_HEADERS}
    out = sanitize_headers_for_storage(headers)
    for k in SENSITIVE_HEADERS:
        assert out[k].endswith("***"), f"{k} 未脱敏"
        assert len(out[k]) <= 13


def test_大小写不敏感():
    """真实请求里头名大小写不固定，匹配必须按小写比较。"""
    for name in ("X-API-Key", "AUTHORIZATION", "Proxy-Authorization"):
        out = sanitize_headers_for_storage({name: "Bearer " + "y" * 40})
        assert out[name].endswith("***"), f"{name} 未脱敏（大小写导致漏匹配）"


def test_非敏感头原样保留():
    out = sanitize_headers_for_storage({"content-type": "application/json"})
    assert out["content-type"] == "application/json"


def test_空字典不报错():
    assert sanitize_headers_for_storage({}) == {}


def test_不修改入参():
    src = {"x-api-key": "sk-" + "z" * 50}
    original = dict(src)
    sanitize_headers_for_storage(src)
    assert src == original, "脱敏函数不应就地修改调用方的 headers"
