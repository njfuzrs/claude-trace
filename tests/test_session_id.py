"""session_id 路径遍历防护测试

session_id 来自外部（HTTP 请求头 / hook 事件），并被直接用于拼接落盘目录
（`sessions/{session_id}/`）。不校验就等于把目录写入位置交给调用方决定，
所以这层防护值得用测试锁死。
"""

from pathlib import Path

from proxy import _sanitize_session_id

# 合法 id 原样通过：字母、数字、下划线、连字符
VALID = [
    "abc123",
    "a1b2-c3d4",
    "with_underscore",
    "0192b4f1-3c2d-7a8e-9f01-23456789abcd",  # Claude Code 的 UUID 形式
]

# 必须被替换掉的构造
MALICIOUS = [
    "../../etc/passwd",
    "..",
    "a/../../b",
    "foo/bar",          # 分隔符本身就不该出现在单段目录名里
    "foo\\bar",
    "with space",
    "with.dot",         # 点号不在白名单内，`.` / `..` 都要挡住
    "semi;colon",
    "null\x00byte",
    "",                 # 空值
    "新建会话",          # 非 ASCII
]


def test_合法id原样返回():
    for sid in VALID:
        assert _sanitize_session_id(sid) == sid, f"合法 id 被错误替换: {sid}"


def test_恶意id被替换():
    for sid in MALICIOUS:
        out = _sanitize_session_id(sid)
        assert out != sid, f"未被替换: {sid!r}"


def test_替换结果本身是安全的():
    """替换后的值会直接用于建目录，它自己必须也是单段安全名。"""
    for sid in MALICIOUS:
        out = _sanitize_session_id(sid)
        assert "/" not in out and "\\" not in out
        assert out not in (".", "..")
        assert out and out.strip() == out


def test_替换结果不逃出根目录():
    root = Path("/tmp/trajectories/sessions").resolve()
    for sid in MALICIOUS:
        resolved = (root / _sanitize_session_id(sid)).resolve()
        assert resolved.parent == root, f"逃出根目录: {sid!r} → {resolved}"
