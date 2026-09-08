"""astrbot_plugin_chat_enhancer 插件测试脚本。

验证：
1. 合并转发触发判断（长度/分段数/关键词三条件）
2. 平台自适应（QQ 平台用合并转发，其他平台降级）
3. MD 格式消除正确性
4. 分段逻辑（最小/最大长度、二次切分）
5. 合并转发 Node 构造（关键修复：get_self_id 而非 self_id）
6. 错误回退
"""
import asyncio
import json
import os
import sys
import tempfile
import types

sys.path.insert(0, "/root/.local/share/uv/tools/astrbot")

import importlib.util

spec = importlib.util.spec_from_file_location(
    "chat_enhancer",
    "/root/dsh_projects/astrbot_plugin_chat_enhancer/main.py",
)
plugin_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plugin_mod)

from astrbot.api import AstrBotConfig
from astrbot.core.message.components import Nodes

SCHEMA = json.load(
    open("/root/dsh_projects/astrbot_plugin_chat_enhancer/_conf_schema.json")
)


def make_config(overrides=None):
    fd, path = tempfile.mkstemp(suffix=".json")
    d = {k: v.get("default") for k, v in SCHEMA.items()}
    if overrides:
        d.update(overrides)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False)
    return AstrBotConfig(config_path=path, schema=SCHEMA)


class MockEvent:
    def __init__(self, platform="aiocqhttp", message_str=""):
        self._platform = platform
        self.message_str = message_str
        self.unified_msg_origin = "test:GroupMessage:1"
        self.sent = []
        self._result_chain = []

    def get_platform_name(self):
        return self._platform

    def get_self_id(self):
        return "10001"

    def plain_result(self, text):
        return types.SimpleNamespace(chain=[types.SimpleNamespace(text=text)], text=text)

    async def send(self, message):
        self.sent.append(message)

    def get_result(self):
        return types.SimpleNamespace(chain=self._result_chain)


def collect_agen(agen):
    out = []

    async def runner():
        async for item in agen:
            out.append(item)

    asyncio.get_event_loop().run_until_complete(runner())
    return out


# ---------- 测试 1: MD 消除 ----------
def test_md_removal():
    plugin = plugin_mod.ChatEnhancerPlugin(None, make_config())
    text = "这是**粗体**，这是*斜体*，`代码`，[链接](https://x.com)，# 标题\n- 列表项\n> 引用\n~~删除~~"
    result = plugin._remove_markdown(text)
    assert "**" not in result, f"粗体未消除: {result}"
    assert "*斜体*" not in result, f"斜体未消除: {result}"
    assert "`代码`" not in result, f"代码未消除: {result}"
    assert "https://x.com" not in result, f"链接未消除: {result}"
    assert "# 标题" not in result, f"标题未消除: {result}"
    assert "列表项" in result
    assert "引用" in result
    assert "删除" in result
    print("PASS 测试1: MD 格式消除正确")


# ---------- 测试 2: 代码块保留 ----------
def test_code_block_keep():
    plugin = plugin_mod.ChatEnhancerPlugin(None, make_config({"keep_code_blocks": True}))
    text = "说明：\n```python\nprint('hello')\n```\n结束"
    result = plugin._remove_markdown(text)
    assert "print('hello')" in result, f"代码块未保留: {result}"
    print("PASS 测试2: 代码块保留")


# ---------- 测试 3: 分段逻辑 ----------
def test_split():
    plugin = plugin_mod.ChatEnhancerPlugin(None, make_config())
    text = "第一句。第二句！第三句？第四句。第五句。第六句。第七句！第八句？第九句。第十句。第十一句。第十二句。"
    segs = plugin._split_message(text)
    assert len(segs) >= 3, f"分段数不足: {segs}"
    assert all(s.strip() for s in segs), "存在空段"
    print(f"PASS 测试3: 分段逻辑正确（{len(segs)} 段）")


# ---------- 测试 4: 超长段二次切分 ----------
def test_long_segment_split():
    plugin = plugin_mod.ChatEnhancerPlugin(
        None, make_config({"max_segment_len": 50, "enable_split": True})
    )
    long_text = "这是一个非常长的段落" * 30  # 无标点，600 字符
    segs = plugin._split_message(long_text)
    assert all(len(s) <= 55 for s in segs), f"存在超长段: {[len(s) for s in segs]}"
    assert len(segs) > 1, "长段未切分"
    print(f"PASS 测试4: 超长段二次切分（{len(segs)} 段，最大 {max(len(s) for s in segs)} 字符）")


# ---------- 测试 5: 合并转发触发 - 长度阈值 ----------
def test_forward_by_length():
    plugin = plugin_mod.ChatEnhancerPlugin(
        None,
        make_config(
            {
                "enable_forward": True,
                "forward_threshold": 100,
                "forward_keywords": [],  # 关闭关键词，只测长度
            }
        ),
    )
    long_text = "这是一段很长的回答内容。" * 15  # 约 165 字符
    assert plugin._should_use_forward(long_text, "随便问问"), "长度超阈值应触发"
    short_text = "短回答"
    assert not plugin._should_use_forward(short_text, "短回答"), "短文本不应触发"
    print("PASS 测试5: 长度阈值触发合并转发")


# ---------- 测试 6: 合并转发触发 - 关键词 ----------
def test_forward_by_keyword():
    plugin = plugin_mod.ChatEnhancerPlugin(
        None,
        make_config(
            {
                "enable_forward": True,
                "forward_threshold": 10000,  # 长度阈值设很大，只测关键词
                "forward_keywords": ["解释", "说明"],
            }
        ),
    )
    text = "中等长度的回答，分段数不超过限制。"
    assert plugin._should_use_forward(text, "请解释一下"), "含关键词应触发"
    assert not plugin._should_use_forward(text, "你好呀"), "不含关键词不应触发"
    print("PASS 测试6: 关键词触发合并转发")


# ---------- 测试 7: 合并转发触发 - 分段数 ----------
def test_forward_by_segment_count():
    plugin = plugin_mod.ChatEnhancerPlugin(
        None,
        make_config(
            {
                "enable_forward": True,
                "forward_threshold": 10000,  # 长度阈值设很大
                "max_segments": 3,
            }
        ),
    )
    # 10 段文本
    text = "第一段。第二段！第三段？第四段。第五段。第六段。第七段。第八段。第九段。第十段。"
    assert plugin._should_use_forward(text, "普通消息"), "分段数超限应触发"
    print("PASS 测试7: 分段数超限触发合并转发")


# ---------- 测试 8: 合并转发 Node 构造（关键修复） ----------
def test_forward_node_construction():
    plugin = plugin_mod.ChatEnhancerPlugin(None, make_config())
    event = MockEvent(platform="aiocqhttp")
    text = "第一段内容。第二段内容！第三段内容？"
    segments = plugin._split_message(text, remove_md=True)

    nodes = []
    self_id = event.get_self_id() or "0"
    for segment in segments:
        node = plugin_mod.Node(
            name="芙芙",
            uin=self_id,
            content=[plugin_mod.Plain(segment)],
        )
        nodes.append(node)
    assert len(nodes) > 1, "节点数不足"
    assert nodes[0].uin == "10001", f"uin 错误: {nodes[0].uin}"
    print(f"PASS 测试8: 合并转发 Node 构造正确（{len(nodes)} 节点，uin={nodes[0].uin}）")


# ---------- 测试 9: 平台自适应 ----------
def test_platform_adapt():
    plugin = plugin_mod.ChatEnhancerPlugin(None, make_config())
    qq_event = MockEvent(platform="aiocqhttp")
    tg_event = MockEvent(platform="telegram")
    assert plugin._is_forward_platform(qq_event), "QQ 平台应支持合并转发"
    assert not plugin._is_forward_platform(tg_event), "Telegram 不应支持合并转发"
    print("PASS 测试9: 平台自适应判断")


# ---------- 测试 10: 合并转发发送（异步验证 get_self_id 修复） ----------
def test_forward_send():
    plugin = plugin_mod.ChatEnhancerPlugin(None, make_config({"bot_name": "测试Bot"}))
    event = MockEvent(platform="aiocqhttp")
    text = "这是第一段内容，讲述基础知识。这是第二段内容，深入分析！这是第三段内容，总结要点。"

    asyncio.get_event_loop().run_until_complete(plugin._send_forward(event, text))
    assert len(event.sent) == 1, f"应发送 1 条合并转发，实际 {len(event.sent)}"
    sent = event.sent[0]
    assert any(isinstance(c, Nodes) for c in sent.chain), "消息链应包含 Nodes 组件"
    nodes_comp = [c for c in sent.chain if isinstance(c, Nodes)][0]
    assert len(nodes_comp.nodes) > 1, "合并转发应包含多个节点"
    print(f"PASS 测试10: 合并转发发送成功（{len(nodes_comp.nodes)} 节点）")


# ---------- 测试 11: 合并转发失败回退 ----------
def test_forward_fallback():
    class FailOnceEvent(MockEvent):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.send_calls = 0

        async def send(self, message):
            self.send_calls += 1
            if self.send_calls == 1:
                # 第一次（合并转发）失败
                raise Exception("send timeout")
            # 后续（回退分段发送）成功
            self.sent.append(message)

    plugin = plugin_mod.ChatEnhancerPlugin(None, make_config({"send_speed": "快速"}))
    event = FailOnceEvent(platform="aiocqhttp")
    text = "第一段。第二段！第三段？第四段。第五段。第六段。"
    asyncio.get_event_loop().run_until_complete(plugin._send_forward(event, text))
    assert event.send_calls > 1, "合并转发失败后应回退到分段发送"
    assert len(event.sent) > 0, "回退发送应有消息产出"
    print(f"PASS 测试11: 合并转发失败后正确回退到分段发送（共 {event.send_calls} 次发送）")


# ---------- 测试 12: 状态指令 ----------
def test_status_cmd():
    plugin = plugin_mod.ChatEnhancerPlugin(None, make_config())
    event = MockEvent(platform="aiocqhttp", message_str="/增强")
    results = collect_agen(plugin.cmd_enhancer_status(event))
    assert len(results) == 1, "应产出 1 条结果"
    text = results[0].text
    assert "聊天增强器状态" in text, f"状态内容错误: {text}"
    assert "合并转发" in text
    assert "aiocqhttp" in text
    print("PASS 测试12: 状态指令正常")


if __name__ == "__main__":
    test_md_removal()
    test_code_block_keep()
    test_split()
    test_long_segment_split()
    test_forward_by_length()
    test_forward_by_keyword()
    test_forward_by_segment_count()
    test_forward_node_construction()
    test_platform_adapt()
    test_forward_send()
    test_forward_fallback()
    test_status_cmd()
    print("\n✅ 全部 12 项测试通过")
