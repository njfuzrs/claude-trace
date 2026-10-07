"""消息体密钥脱敏测试

脱敏器是「静默失效」类模块：漏掉一种形态时没人会报错，只会在某天发现
token 明文躺在 raw.jsonl 里。所以每种规则都要有一条「完整值不得出现」的断言。
"""

import json

import pytest

from scrub import mask_secret, maybe_scrub, maybe_scrub_jsonl_line, scrub_obj, scrub_text

# 用拼接构造假密钥，避免 gitleaks 扫描测试源码时命中
ANT = "sk-" + "ant-api03-" + "A1b2C3d4E5f6G7h8I9j0" * 4
GHP = "ghp" + "_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"
AWS = "AKIA" + "Z7Q2W3E4R5T6Y7U8"
JWT = "eyJ" + "hbGciOiJIUzI1NiJ9" + ".eyJ" + "zdWIiOiIxMjM0NTY3ODkwIn0" + ".SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV"


@pytest.fixture(autouse=True)
def _enable(monkeypatch):
    monkeypatch.delenv("TRAJ_SCRUB_SECRETS", raising=False)


def test_保留头尾去掉中间():
    out = mask_secret(ANT)
    assert out.startswith(ANT[:8]) and out.endswith(ANT[-4:])
    assert "***" in out and ANT not in out


def test_短值至少抹掉一半():
    out = mask_secret("abcdefghij")
    visible = len(out) - 3
    assert visible <= 5


@pytest.mark.parametrize("secret", [
    ANT,
    "sk-" + "proj-" + "x9Y8z7W6v5U4t3S2r1Q0p9O8",
    GHP,
    AWS,
    "AIza" + "SyA1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q",
    "xoxb-" + "123456789012-abcdefABCDEF",
    "hf" + "_" + "AbCdEfGhIjKlMnOpQrStUvWxYz0123456789",
    "glpat-" + "a1B2c3D4e5F6g7H8i9J0",
    JWT,
])
def test_常见密钥形态都被脱敏(secret):
    text = f"我的 key 是 {secret} 请帮我调一下"
    out = scrub_text(text)
    assert secret not in out
    assert "***" in out
    assert out.startswith("我的 key 是 ") and out.endswith(" 请帮我调一下")


def test_bearer_头只脱值():
    tok = "abcDEF1234567890ghiJKL0987"
    out = scrub_text(f'curl -H "Authorization: Bearer {tok}" https://x')
    assert tok not in out and "Bearer " in out


def test_连接串密码():
    out = scrub_text("postgres://admin:S3cr3tP4ssw0rd@db.local:5432/app")
    assert "S3cr3tP4ssw0rd" not in out
    assert out.startswith("postgres://admin:") and "@db.local:5432/app" in out


def test_env_赋值():
    v = "q8W7e6R5t4Y3u2I1o0P9"
    out = scrub_text(f"OPENAI_API_KEY={v}\nDEBUG=true")
    assert v not in out and "DEBUG=true" in out
    out = scrub_text(f'{{"client_secret": "{v}"}}')
    assert v not in out


def test_代码不误伤():
    code = "token = get_access_token(user)\npassword_hash = hashlib.sha256(pw)\nkey = 'name'"
    assert scrub_text(code) == code
    # 纯字母的长标识符不是密钥
    s = "secret_manager_client_factory = SecretManagerClientFactoryImpl"
    assert scrub_text(s) == s


def test_前缀在单词中间不命中():
    s = "task-abcdefghijklmnopqrstuvwxyz0123"
    assert scrub_text(s) == s


def test_pem_私钥正文被抹掉():
    body = "MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQC7"
    pem = "-----BEGIN " + "PRIVATE KEY-----\n" + body + "\n-----END PRIVATE KEY-----"
    out = scrub_text("内容:\n" + pem)
    assert body not in out
    assert "BEGIN PRIVATE KEY" in out and "END PRIVATE KEY" in out


def test_递归脱敏且不修改入参():
    src = {
        "messages": [{"role": "user", "content": [{"type": "text", "text": f"key {ANT}"}]}],
        "n": 3,
    }
    snapshot = json.dumps(src)
    out = scrub_obj(src)
    assert json.dumps(src) == snapshot, "不应就地修改（内存对象还要做增量哈希）"
    assert ANT not in json.dumps(out)
    assert out["n"] == 3


def test_signature_与图片不动():
    sig = "sk-" + "x" * 40
    blk = {"type": "thinking", "thinking": "t", "signature": sig}
    assert scrub_obj(blk)["signature"] == sig
    img = {"type": "base64", "media_type": "image/png", "data": "sk-" + "y" * 40}
    assert scrub_obj(img) == img


def test_工具调用输入也会脱敏():
    blk = {"type": "tool_use", "name": "Bash", "input": {"command": f"export GH={GHP}"}}
    assert GHP not in json.dumps(scrub_obj(blk))


@pytest.mark.parametrize("v", ["false", "0", "off", "NO"])
def test_可关闭(monkeypatch, v):
    monkeypatch.setenv("TRAJ_SCRUB_SECRETS", v)
    obj = {"t": ANT}
    assert maybe_scrub(obj) is obj
    line = json.dumps(obj) + "\n"
    assert maybe_scrub_jsonl_line(line) == line


def test_默认开启():
    assert ANT not in json.dumps(maybe_scrub({"t": ANT}))


def test_jsonl_行():
    line = json.dumps({"tool_response": f"AWS={AWS}"}) + "\n"
    out = maybe_scrub_jsonl_line(line)
    assert out.endswith("\n") and AWS not in out
    assert json.loads(out)
    # 坏行退化为文本脱敏
    assert AWS not in maybe_scrub_jsonl_line(f"not json {AWS}\n")


def test_幂等():
    once = scrub_text(f"k={ANT} {GHP}")
    assert scrub_text(once) == once
