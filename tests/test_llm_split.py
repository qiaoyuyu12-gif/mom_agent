"""
单测:`<think>` 段流式切分与同步切分。

这些是纯逻辑测试,不依赖 Redis/PG/LLM,可单独运行。
"""

from app.core.llm import StreamThinkSplitter, split_thinking


# --------- 同步切分 ---------

def test_split_thinking_with_block():
    text = "<think>先做规划</think>正式答复"
    thought, answer = split_thinking(text)
    assert thought == "先做规划"
    assert answer == "正式答复"


def test_split_thinking_no_block():
    thought, answer = split_thinking("普通答复")
    assert thought == ""
    assert answer == "普通答复"


def test_split_thinking_multiple_blocks():
    text = "<think>步骤1</think>中段<think>步骤2</think>结论"
    thought, answer = split_thinking(text)
    assert "步骤1" in thought and "步骤2" in thought
    assert answer == "中段结论"


# --------- 流式切分 ---------

def _drain(splitter: StreamThinkSplitter, chunks):
    out = []
    for c in chunks:
        out.extend(list(splitter.feed(c)))
    out.extend(list(splitter.flush()))
    return out


def test_stream_simple_answer_only():
    s = StreamThinkSplitter()
    events = _drain(s, ["你好", "世界"])
    assert all(ch == "answer" for ch, _ in events)
    assert "".join(seg for _, seg in events) == "你好世界"


def test_stream_with_think_block():
    s = StreamThinkSplitter()
    events = _drain(s, ["<think>", "思考A", "</think>", "答复B"])
    thought = "".join(seg for ch, seg in events if ch == "thought")
    answer = "".join(seg for ch, seg in events if ch == "answer")
    assert thought == "思考A"
    assert answer == "答复B"


def test_stream_tag_split_across_chunks():
    """开/闭标签可能横跨多个 chunk,切分器要正确缓冲。"""
    s = StreamThinkSplitter()
    # "<thi" / "nk>thought</thi" / "nk>final"
    events = _drain(s, ["<thi", "nk>thought</thi", "nk>final"])
    thought = "".join(seg for ch, seg in events if ch == "thought")
    answer = "".join(seg for ch, seg in events if ch == "answer")
    assert thought == "thought"
    assert answer == "final"


def test_stream_no_close_tag_flushes_as_thought():
    """流意外结束时,残留缓冲按当前状态吐尽。"""
    s = StreamThinkSplitter()
    events = _drain(s, ["<think>未关闭"])
    thought = "".join(seg for ch, seg in events if ch == "thought")
    answer = "".join(seg for ch, seg in events if ch == "answer")
    assert thought == "未关闭"
    assert answer == ""
