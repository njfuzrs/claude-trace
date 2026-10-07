"""
scrub.py — 消息体内容级密钥脱敏（落盘前统一过一遍）

只作用于「落盘副本」：转发给上游的请求体永远是客户端原始 bytes（见 CLAUDE.md），
内存里用于增量哈希的 request_body 也不动 —— scrub_obj 总是返回新对象。

脱敏方式：保留头尾、去掉中间，如 sk-ant-api03-abcd…wxyz → sk-ant-a***wxyz。
头尾保留是为了事后还能辨认「是哪把 key」以便轮换，同时中间部分不可恢复。

开关：环境变量 TRAJ_SCRUB_SECRETS，默认开启；设为 false / 0 / off / no 关闭。
注意 collector.py 由 Claude Code 拉起，读不到 launchd plist 的环境变量，
要在 hook 侧也关闭，需在 ~/.claude/settings.json 的 env 里同样设置。

本文件同时是 collector.py 的同目录依赖（部署到 ~/.claude/hooks/），只用标准库。
"""

import functools
import os
import re
from typing import Any, Callable, List, Tuple

MASK = "***"

# 不参与脱敏的字段：thinking 的 signature 是上游签名，改了回放会 400；
# 它是随机 base64，本来也不会是用户粘贴的密钥。
_SKIP_KEYS = frozenset({"signature"})


def scrub_enabled() -> bool:
    """每次调用时读环境变量，便于测试与运行期切换"""
    v = os.environ.get("TRAJ_SCRUB_SECRETS", "true").strip().lower()
    return v not in ("false", "0", "off", "no")


def mask_secret(value: str) -> str:
    """保留头尾、中间替换为 ***

    保留长度随密钥长度伸缩，最多头 8 尾 4，且头尾合计不超过原长的一半，
    短值也至少抹掉一半，保证不可还原。
    """
    n = len(value)
    if n <= 8:
        return value[:1] + MASK + value[-1:] if n >= 4 else MASK
    head = min(8, n // 4)
    tail = min(4, n // 4)
    return value[:head] + MASK + value[-tail:]


# 左边界：前一个字符不能是字母数字，避免把 task-xxx 里的 sk- 当成 key 前缀
_LB = r"(?<![A-Za-z0-9])"
# 右边界：同理，防止只截到长串的一部分
_RB = r"(?![A-Za-z0-9])"

# (正则, 需要脱敏的分组号)。分组 0 表示整段命中都是密钥。
# 规则与 gitleaks 默认规则集的高置信度部分对齐；宁可漏报也不误伤代码正文。
_TOKEN_RULES: List[Tuple["re.Pattern[str]", int]] = [
    # Anthropic / OpenAI / 各类中转站的 sk- 密钥（含 sk-ant- / sk-proj- 等）
    (re.compile(_LB + r"sk-[A-Za-z0-9_\-]{20,}"), 0),
    # GitHub token
    (re.compile(_LB + r"gh[pousr]_[A-Za-z0-9]{30,}" + _RB), 0),
    (re.compile(_LB + r"github_pat_[A-Za-z0-9_]{20,}" + _RB), 0),
    # GitLab
    (re.compile(_LB + r"glpat-[A-Za-z0-9_\-]{20,}"), 0),
    # AWS Access Key ID
    (re.compile(_LB + r"(?:AKIA|ASIA|AGPA|AIDA|AROA)[0-9A-Z]{16}" + _RB), 0),
    # Google API key
    (re.compile(_LB + r"AIza[0-9A-Za-z_\-]{35}"), 0),
    # Slack
    (re.compile(_LB + r"xox[abprs]-[A-Za-z0-9\-]{10,}"), 0),
    # Stripe
    (re.compile(_LB + r"(?:sk|rk|pk)_(?:live|test)_[A-Za-z0-9]{16,}" + _RB), 0),
    # HuggingFace
    (re.compile(_LB + r"hf_[A-Za-z0-9]{30,}" + _RB), 0),
    # JWT（三段 base64url，前两段以 eyJ 开头）
    (re.compile(_LB + r"eyJ[A-Za-z0-9_\-]{10,}\.eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"), 0),
    # Authorization: Bearer xxx（出现在 curl 命令、日志里）
    (re.compile(r"(?i)\bbearer\s+([A-Za-z0-9._~+/=\-]{20,})"), 1),
    # 连接串里的密码：scheme://user:PASSWORD@host
    (re.compile(
        r"(?i)\b(?:postgres(?:ql)?|mysql|mariadb|mongodb(?:\+srv)?|redis|rediss|amqps?|mssql)"
        r"://[^\s:/@\"']+:([^\s@/\"']{3,})@"
    ), 1),
]

# 键值形式的通用凭据：API_KEY=xxx / "secret": "xxx" / password: xxx
# 值必须同时含字母和数字且 ≥16 位，以免把 token = get_token() 这类代码误伤。
_ASSIGN_RE = re.compile(
    r"(?i)((?:api[_\-]?key|secret|token|passw(?:or)?d|access[_\-]?key|auth[_\-]?key|private[_\-]?key)"
    r"[A-Za-z0-9_\-]*[\"']?\s*[:=]\s*[\"']?)"
    r"([A-Za-z0-9_\-+/=]{16,})"
)

# PEM 私钥：保留 BEGIN/END 行，正文整段抹掉（头尾几个字符对私钥没有辨识意义）
_PEM_RE = re.compile(
    r"(-----BEGIN [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----)[\s\S]*?(-----END [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----)"
)

# 快速预检：字符串里一个触发词都没有就整段跳过，省掉十几次正则扫描。
# 大小写不敏感地找，覆盖上面所有规则的必要子串。
_TRIGGER_RE = re.compile(
    r"(?i)sk-|gh[pousr]_|github_pat_|glpat-|akia|asia|agpa|aida|aroa|aiza|xox|_live_|_test_|hf_|eyj|bearer"
    r"|://|key|secret|token|passw|private"
)


def _has_letter_and_digit(s: str) -> bool:
    return any(c.isdigit() for c in s) and any(c.isalpha() for c in s)


def _group_masker(group: int) -> Callable[["re.Match[str]"], str]:
    def repl(m: "re.Match[str]") -> str:
        if group == 0:
            return mask_secret(m.group(0))
        start, end = m.span(group)
        base = m.start(0)
        s = m.group(0)
        return s[: start - base] + mask_secret(m.group(group)) + s[end - base:]
    return repl


def _assign_repl(m: "re.Match[str]") -> str:
    value = m.group(2)
    if MASK in value or not _has_letter_and_digit(value):
        return m.group(0)
    return m.group(1) + mask_secret(value)


_REPLACERS = [(p, _group_masker(g)) for p, g in _TOKEN_RULES]


def scrub_text(text: str) -> str:
    """对单个字符串做密钥脱敏，不命中则原样返回（同一对象）"""
    if len(text) < 16:
        return text
    return _scrub_text_cached(text)


# traj 每轮全量重写，且 system prompt / 工具定义 / 历史消息在各步里反复出现：
# 实测 45MB 的 traj 有 11 万个字符串但只有 1.7 万个不同值。缓存后每轮只需扫新增内容。
@functools.lru_cache(maxsize=16384)
def _scrub_text_cached(text: str) -> str:
    if not _TRIGGER_RE.search(text):
        return text
    out = _PEM_RE.sub(lambda m: m.group(1) + "\n" + MASK + "\n" + m.group(2), text)
    for pattern, repl in _REPLACERS:
        out = pattern.sub(repl, out)
    out = _ASSIGN_RE.sub(_assign_repl, out)
    return out


def _is_opaque_block(d: dict) -> bool:
    """不需要扫描的块：图片 / 文档的 base64 正文、redacted_thinking 的密文"""
    t = d.get("type")
    return t == "redacted_thinking" or t == "base64"


def scrub_obj(obj: Any) -> Any:
    """递归脱敏 JSON 兼容对象，返回新对象，绝不就地修改入参"""
    if isinstance(obj, str):
        return scrub_text(obj)
    if isinstance(obj, dict):
        if _is_opaque_block(obj):
            return obj
        return {
            k: (v if k in _SKIP_KEYS else scrub_obj(v))
            for k, v in obj.items()
        }
    if isinstance(obj, (list, tuple)):
        return [scrub_obj(v) for v in obj]
    return obj


def maybe_scrub(obj: Any) -> Any:
    """开关打开时脱敏，关闭时原样返回"""
    return scrub_obj(obj) if scrub_enabled() else obj


def maybe_scrub_jsonl_line(line: str) -> str:
    """对一行 JSONL 脱敏；解析失败时退化为整行做文本脱敏"""
    import json
    if not scrub_enabled() or not line.strip():
        return line
    try:
        obj = json.loads(line)
    except ValueError:
        return scrub_text(line)
    nl = "\n" if line.endswith("\n") else ""
    return json.dumps(scrub_obj(obj), ensure_ascii=False) + nl
