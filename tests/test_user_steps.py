"""S2-1 user step + test_runs 测试

user step 只进 trajectory / history 索引，SFT convert 必须跳过它 —— 旧夹具的
convert 输出 hash 不变是 §8.2 约束 4 的硬门。
"""

import hashlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

from builder import (
    SessionMetadata,
    apply_hook_events_to_metadata,
    build_trajectory,
    clean_user_text,
    detect_test_runner,
)

ROOT = Path(__file__).resolve().parent.parent


def _load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "tools" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


convert_trajs = _load("convert_trajs")
filter_trajs = _load("filter_trajs")

REMINDER = "<system-reminder>\nCLAUDE.md 内容……\n</system-reminder>"


def _pair(ts, request_msgs, response_content, stop="tool_use", new_messages=None, tools=True):
    return SimpleNamespace(
        timestamp=ts,
        request_body={"messages": request_msgs, "system": "sys",
                      "tools": [{"name": "Bash"}] if tools else None},
        response_body={
            "content": response_content,
            "stop_reason": stop,
            "usage": {"input_tokens": 1, "output_tokens": 1},
        },
        new_messages=new_messages or [],
        is_partial=False,
        stop_reason=stop,
    )


def _bash(tid, cmd):
    return {"type": "tool_use", "id": tid, "name": "Bash", "input": {"command": cmd}}


def _result(tid, text, is_error=False):
    return {"type": "tool_result", "tool_use_id": tid, "content": text, "is_error": is_error}


def _session(cmd="ls", result="a.py", prompt="帮我看看目录"):
    """一轮 prompt → Bash → 结果 → final_answer"""
    first_user = {"role": "user", "content": [
        {"type": "text", "text": REMINDER},
        {"type": "text", "text": prompt},
    ]}
    p1 = _pair("2026-10-03T10:00:00", [first_user],
               [{"type": "text", "text": "先看一下"}, _bash("t1", cmd)])
    tool_msg = {"role": "user", "content": [_result("t1", result)]}
    p2 = _pair("2026-10-03T10:00:05",
               [first_user, {"role": "assistant", "content": []}, tool_msg],
               [{"type": "text", "text": "完成"}], stop="end_turn",
               new_messages=[tool_msg])
    meta = SessionMetadata(session_id="s1", model="claude-opus-5")
    apply_hook_events_to_metadata(meta, [
        {"event": "UserPromptSubmit", "prompt": prompt, "timestamp": "t"},
    ])
    return build_trajectory("s1", [p1, p2], meta)


# ── 去噪 ─────────────────────────────────────────────


def test_去掉system_reminder保留正文():
    clean, kinds = clean_user_text(f"{REMINDER}\n修一下 bug")
    assert clean == "修一下 bug"
    assert kinds == ["system_reminder"]


def test_slash命令保留用户敲的那一行():
    text = ("<command-name>/review</command-name>\n<command-message>review</command-message>\n"
            "<command-args>PR 12</command-args>")
    clean, _ = clean_user_text(text)
    assert clean == "/review PR 12"


def test_本地命令回显整条算噪声():
    text = "<local-command-stdout>ok</local-command-stdout>"
    clean, kinds = clean_user_text(text)
    assert clean == ""
    assert "local_command" in kinds


def test_compaction摘要算噪声():
    clean, kinds = clean_user_text(
        "This session is being continued from a previous conversation that ran out of context.")
    assert clean == ""
    assert kinds == ["compaction_summary"]


# ── user step 位置与字段 ────────────────────────────


def test_user_step插在action之前且时间有序():
    traj = _session()
    types = [s["message_type"] for s in traj["trajectory"]]
    assert types == ["user", "action", "observation", "action"]
    u = traj["trajectory"][0]
    assert u["content_clean"] == "帮我看看目录"
    assert REMINDER in u["content"]          # 原文不删
    assert u["is_system_noise"] is False
    assert u["prompt_id"] == 0               # 对上 metadata.user_prompts[0]
    dq = traj["info"]["data_quality"]
    assert dq["user_steps"] == 1 and dq["user_steps_prompt_matched"] == 1


def test_无tools的旁路请求不产生user_step也不占prompt位():
    prompt = "帮我看看目录"
    title = _pair("2026-10-03T09:59:59",
                  [{"role": "user", "content": f"<session>\n{prompt}\n</session>\nWrite the title"}],
                  [{"type": "text", "text": "看目录"}], stop="end_turn", tools=False)
    main = _pair("2026-10-03T10:00:00", [{"role": "user", "content": prompt}],
                 [{"type": "text", "text": "好"}], stop="end_turn",
                 new_messages=[{"role": "user", "content": prompt}])
    meta = SessionMetadata(session_id="s1", model="m")
    apply_hook_events_to_metadata(meta, [{"event": "UserPromptSubmit", "prompt": prompt}])
    traj = build_trajectory("s1", [title, main], meta)
    users = [s for s in traj["trajectory"] if s["message_type"] == "user"]
    assert len(users) == 1
    assert users[0]["timestamp"] == "2026-10-03T10:00:00"
    assert users[0]["prompt_id"] == 0


def test_同一句重发不重复产生user_step():
    prompt = "写一个文档"
    m1 = {"role": "user", "content": [{"type": "text", "text": prompt,
                                       "cache_control": {"type": "ephemeral"}}]}
    m2 = {"role": "user", "content": prompt}   # cache_control 挪走后的同一句
    p1 = _pair("2026-10-03T10:00:00", [m1], [_bash("t1", "ls")], new_messages=[m1])
    tr = {"role": "user", "content": [_result("t1", "a")]}
    p2 = _pair("2026-10-03T10:00:05", [m2, tr], [{"type": "text", "text": "好"}],
               stop="end_turn", new_messages=[m2, tr])
    meta = SessionMetadata(session_id="s1", model="m")
    apply_hook_events_to_metadata(meta, [{"event": "UserPromptSubmit", "prompt": prompt}])
    traj = build_trajectory("s1", [p1, p2], meta)
    users = [s for s in traj["trajectory"] if s["message_type"] == "user"]
    assert len(users) == 1
    assert traj["info"]["data_quality"]["user_steps_resent_dropped"] == 1
    # history 仍保留两条原文，被丢的那条不指向任何 step
    hu = [h for h in traj["history"] if h.get("message_type") == "user"]
    assert [h["traj_step"] for h in hu] == [0, None]


def test_hook里提交两次的同一句都保留():
    prompt = "继续"
    m1 = {"role": "user", "content": prompt}
    p1 = _pair("2026-10-03T10:00:00", [m1], [{"type": "text", "text": "a"}],
               stop="end_turn", new_messages=[m1])
    a1 = {"role": "assistant", "content": [{"type": "text", "text": "a"}]}
    m2 = {"role": "user", "content": [{"type": "text", "text": prompt}]}
    p2 = _pair("2026-10-03T10:01:00", [m1, a1, m2], [{"type": "text", "text": "b"}],
               stop="end_turn", new_messages=[a1, m2])
    meta = SessionMetadata(session_id="s1", model="m")
    apply_hook_events_to_metadata(meta, [
        {"event": "UserPromptSubmit", "prompt": prompt},
        {"event": "UserPromptSubmit", "prompt": prompt},
    ])
    traj = build_trajectory("s1", [p1, p2], meta)
    users = [s for s in traj["trajectory"] if s["message_type"] == "user"]
    assert [u["prompt_id"] for u in users] == [0, 1]


def test_raw丢了首条输入时用hook补():
    prompt = "这个项目是做什么的"
    title = _pair("2026-10-03T09:59:59",
                  [{"role": "user", "content": f"<session>{prompt}</session>"}],
                  [{"type": "text", "text": "项目介绍"}], stop="end_turn", tools=False)
    main = _pair("2026-10-03T10:00:00", [], [{"type": "text", "text": "是采集器"}],
                 stop="end_turn", new_messages=[])
    meta = SessionMetadata(session_id="s1", model="m")
    apply_hook_events_to_metadata(meta, [{"event": "UserPromptSubmit", "prompt": prompt}])
    traj = build_trajectory("s1", [title, main], meta)
    # 标题生成 pair 本身照旧产出一条 final_answer（既有行为），补的 user step 紧贴主请求的 action 之前
    types = [s["message_type"] for s in traj["trajectory"]]
    assert types[-2:] == ["user", "action"]
    u = traj["trajectory"][-2]
    assert u["content"] == prompt and u["content_source"] == "hook" and u["prompt_id"] == 0
    assert traj["info"]["data_quality"]["user_steps_from_hook"] == 1


def test_纯tool_result不产生user_step():
    traj = _session()
    assert sum(s["message_type"] == "user" for s in traj["trajectory"]) == 1


def test_history带traj_step和timestamp():
    traj = _session()
    steps = traj["trajectory"]
    hist = [h for h in traj["history"] if h["role"] != "system"]
    user_h = hist[0]
    assert user_h["message_type"] == "user"
    assert user_h["timestamp"] == "2026-10-03T10:00:00"
    assert steps[user_h["traj_step"]]["message_type"] == "user"
    for h in hist:
        if h["role"] == "assistant":
            assert steps[h["traj_step"]]["message_type"] == "action"
        if h.get("message_type") == "observation":
            assert steps[h["traj_step"]]["tool_use_id"] == h["tool_call_ids"][0]


def test_元数据带collector_ver():
    meta = _session()["metadata"]
    assert meta["collector_ver"]
    assert meta["traj_schema"] == {"user_steps": True}


# ── test_runs ───────────────────────────────────────


def test_ls不算测试命令():
    traj = _session(cmd="ls -la")
    assert traj["metadata"]["test_runs"] == []
    assert not any(s.get("is_test_command") for s in traj["trajectory"])


def test_pytest记入test_runs():
    traj = _session(cmd="cd repo && python3 -m pytest -q tests/", result="Exit code 1\n1 failed")
    runs = traj["metadata"]["test_runs"]
    assert len(runs) == 1
    assert runs[0]["runner"] == "python -m pytest"
    assert runs[0]["source"] == "inferred_argv0"
    assert runs[0]["exit_code"] == 1
    obs = [s for s in traj["trajectory"] if s["message_type"] == "observation"][0]
    assert obs["is_test_command"] is True


def test_argv0表():
    hits = {
        "pytest -q": "pytest",
        "FOO=1 pytest": "pytest",
        "/usr/bin/python3.12 -m pytest": "python -m pytest",
        "npm test": "npm test",
        "npm run test -- --watch=false": "npm run test",
        "cargo test --all": "cargo test",
        "go test ./...": "go test",
        "uv sync && pytest | tail -3": "pytest",
    }
    for cmd, want in hits.items():
        assert detect_test_runner(cmd) == want, cmd
    for cmd in ("ls", "git log --grep test", "echo pytest", "cat test_x.py",
                "npm install", "grep -rn pytest .", ""):
        assert detect_test_runner(cmd) == "", cmd


# ── 下游口径不变 ────────────────────────────────────


def _hash(traj, style):
    record = convert_trajs.convert_traj(traj, style)
    blob = json.dumps(record["messages"], ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()


def test_convert三种style跳过user_step_hash不变():
    traj = _session()
    stripped = json.loads(json.dumps(traj))
    stripped["trajectory"] = [s for s in stripped["trajectory"] if s["message_type"] != "user"]
    for style in ("xml", "tool", "messages"):
        assert _hash(traj, style) == _hash(stripped, style), style


def test_filter步骤数不计user_step():
    traj = _session()
    m = filter_trajs.compute_quality_metrics(traj)
    assert m["step_count"] == 3


def test_只有user_step仍是空轨迹(tmp_path):
    from uploader import is_empty_trajectory
    (tmp_path / "session.traj").write_text(json.dumps({
        "trajectory": [{"message_type": "user", "content": "hi"}],
    }))
    assert is_empty_trajectory(tmp_path) is True
