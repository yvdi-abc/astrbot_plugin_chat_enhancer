import re
import asyncio
import time
from typing import List, Tuple

from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api import logger, AstrBotConfig
from astrbot.api.star import Context, Star
from astrbot.api.message_components import Plain, BaseMessageComponent, Node, Nodes
from astrbot.api.provider import LLMResponse

# 兜底：剥离模型可能输出的 <quote .../> <mention .../> <refuse/> 等控制标签，
# 防止被当作普通文本发送出去（配合 enhance_mode/proactive_reply 的引用标签方案）。
CONTROL_TAG_RE = re.compile(r"</?(?:quote|mention|refuse)\b[^>]*>", re.IGNORECASE)


class ChatEnhancerPlugin(Star):
    """聊天增强器插件。

    功能：
    - 消息智能分段发送（模拟真人打字节奏）
    - Markdown 格式消除
    - 智能合并转发（长回答打包为 QQ 合并转发消息）
    - 平台自适应（非 QQ 平台自动降级为分段发送）
    - 多模式触发策略（长度/分段数/关键词）
    - 状态查看与功能开关指令
    """

    # 支持合并转发的平台（qq_official 官方适配器不支持 Nodes 组件，会静默丢弃，故不包含）
    FORWARD_SUPPORTED_PLATFORMS = ("aiocqhttp",)

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config

    async def initialize(self):
        """插件初始化"""
        logger.info("聊天增强器插件已加载")
        logger.info(f"消息分段: {self.config.get('enable_split', True)}")
        logger.info(f"MD格式消除: {self.config.get('remove_markdown', True)}")
        logger.info(f"智能合并转发: {self.config.get('enable_forward', True)}")

    # ------------------------------------------------------------------ #
    # Markdown 处理                                                       #
    # ------------------------------------------------------------------ #

    def _remove_markdown(self, text: str) -> str:
        """移除 Markdown 格式"""
        if not self.config.get("remove_markdown", True):
            return text

        # 保存代码块
        code_blocks = []
        keep_code = self.config.get("keep_code_blocks", True)

        if keep_code:
            # 提取代码块
            code_pattern = r'```[\s\S]*?```'
            code_blocks = re.findall(code_pattern, text)
            # 用占位符替换
            text = re.sub(
                code_pattern,
                lambda m: f"__CODE_BLOCK_{len(code_blocks) - code_blocks[::-1].index(m.group()) - 1}__",
                text,
            )

        # 移除 MD 格式
        # 粗体 **text**（__text__ 容易误伤，仅处理 **text**）
        text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)

        # 斜体 *text*
        text = re.sub(r'(?<!\*)\*([^*\n]+)\*(?!\*)', r'\1', text)

        # 删除线 ~~text~~
        text = re.sub(r'~~(.+?)~~', r'\1', text)

        # 行内代码 `code`
        text = re.sub(r'`([^`]+?)`', r'\1', text)

        # 标题 # ## ###（支持行首或前有空格/标点）
        text = re.sub(r'(?:^|(?<=[\s，,、。；;]))#{1,6}\s+', '', text, flags=re.MULTILINE)

        # 链接 [text](url)
        text = re.sub(r'\[([^\]]+)\]\([^\)]+\)', r'\1', text)

        # 图片 ![alt](url)
        text = re.sub(r'!\[([^\]]*)\]\([^\)]+\)', r'[图片: \1]', text)

        # 表格 | a | b | 转为文本
        text = re.sub(r'\|', ' ', text)
        text = re.sub(r'^\s*-{3,}\s*$', '', text, flags=re.MULTILINE)

        # 列表标记 - * +
        text = re.sub(r'^[\s]*[-\*\+]\s+', '• ', text, flags=re.MULTILINE)

        # 数字列表 1. 2. 3.
        text = re.sub(r'^[\s]*\d+\.\s+', '', text, flags=re.MULTILINE)

        # 引用 >
        text = re.sub(r'^>\s+', '', text, flags=re.MULTILINE)

        # 水平线 --- *** ___
        text = re.sub(r'^[\s]*[-\*_]{3,}[\s]*$', '', text, flags=re.MULTILINE)

        # 恢复代码块
        if keep_code and code_blocks:
            for i, block in enumerate(code_blocks):
                text = text.replace(f"__CODE_BLOCK_{i}__", block)

        # 清理多余空行（最多保留两个连续换行）
        text = re.sub(r'\n{3,}', '\n\n', text)

        return text.strip()

    # ------------------------------------------------------------------ #
    # 分段处理                                                           #
    # ------------------------------------------------------------------ #

    def _split_message(self, text: str, remove_md: bool = False) -> List[str]:
        """智能分段消息（不合并，返回所有原始分段）。

        Args:
            text: 要分段的文本
            remove_md: 是否对每段移除 MD 格式
        """
        if not self.config.get("enable_split", True):
            result = [text]
            if remove_md:
                result = [self._remove_markdown(seg) for seg in result]
            return [seg for seg in result if seg.strip()]

        split_chars = self.config.get(
            "split_chars", ["。", "！", "？", "?", "!", "\n"]
        )
        # 钳制为合法范围，防止负值导致死循环
        min_len = max(1, int(self.config.get("min_segment_len", 10) or 10))
        max_seg_len = max(1, int(self.config.get("max_segment_len", 200) or 200))

        segments = []
        current = ""

        for char in text:
            current += char
            if char in split_chars:
                # 遇到切分符：若达到最小长度则切分
                if len(current.strip()) >= min_len:
                    segments.append(current.strip())
                    current = ""
                # 若当前只积累了空白（段间空行/换行），也清空，避免空行被吞进下一段
                elif not current.strip():
                    current = ""
                # 否则继续累积（内容不足一段，等下一个切分符）

        # 添加剩余内容
        if current.strip():
            segments.append(current.strip())

        # 超长段落二次切分
        final_segments = []
        for seg in segments:
            if len(seg) > max_seg_len:
                while len(seg) > max_seg_len:
                    final_segments.append(seg[:max_seg_len].strip())
                    seg = seg[max_seg_len:]
                if seg.strip():
                    final_segments.append(seg.strip())
            else:
                final_segments.append(seg)

        # 折叠段内多余空行（\n\n\n → \n），并去掉首尾空白
        final_segments = [
            re.sub(r"\n{2,}", "\n", seg).strip() for seg in final_segments
        ]

        # 如果需要，对每段移除 MD 格式
        if remove_md and self.config.get("remove_markdown", True):
            final_segments = [self._remove_markdown(seg) for seg in final_segments]

        return [seg for seg in final_segments if seg.strip()] or ([text] if text.strip() else [])

    def _merge_segments(self, segments: List[str], max_count: int) -> List[str]:
        """将分段合并到不超过 max_count 段。"""
        if len(segments) <= max_count:
            return segments
        merged = []
        temp = ""
        for i, seg in enumerate(segments):
            if len(merged) < max_count - 1:
                merged.append(seg)
            else:
                temp += seg
        if temp:
            merged.append(temp)
        return [seg for seg in merged if seg.strip()]

    # ------------------------------------------------------------------ #
    # 合并转发判断                                                       #
    # ------------------------------------------------------------------ #

    def _is_forward_platform(self, event: AstrMessageEvent) -> bool:
        """判断当前平台是否支持合并转发。"""
        platform = event.get_platform_name()
        return platform in self.FORWARD_SUPPORTED_PLATFORMS

    def _should_use_forward(self, text: str, user_message: str) -> bool:
        """判断是否应该使用合并转发。

        触发条件（任一满足）：
        1. 文本长度 > forward_threshold
        2. 分段数 > max_segments
        3. 用户消息包含 forward_keywords 中的关键词
        """
        if not self.config.get("enable_forward", True):
            return False

        # 长度阈值
        threshold = int(self.config.get("forward_threshold", 500) or 500)
        max_segments = int(self.config.get("max_segments", 5) or 5)

        # 原始分段数（不合并）
        segments = self._split_message(text, remove_md=False)
        raw_count = len(segments)

        # 条件 1: 长度超阈值
        if len(text) > threshold:
            logger.info(f"[聊天增强器] 长度 {len(text)} > 阈值 {threshold}，触发合并转发")
            return True

        # 条件 2: 分段数超限
        if raw_count > max_segments:
            logger.info(
                f"[聊天增强器] 分段数 {raw_count} > 上限 {max_segments}，触发合并转发"
            )
            return True

        # 条件 3: 关键词
        keywords = self.config.get("forward_keywords", [])
        for keyword in keywords:
            if keyword in user_message:
                logger.info(f"[聊天增强器] 检测到关键词 '{keyword}'，触发合并转发")
                return True

        return False

    # ------------------------------------------------------------------ #
    # 发送逻辑                                                           #
    # ------------------------------------------------------------------ #

    async def _send_forward(self, event: AstrMessageEvent, text: str):
        """发送合并转发消息"""
        bot_name = self.config.get("bot_name", "AI助手")
        # 先分段，再对每段去除 MD 格式
        segments = self._split_message(text, remove_md=True)
        # 合并转发节点数过多时合并（QQ 单条转发消息上限约 100 节点）
        if len(segments) > 50:
            segments = self._merge_segments(segments, 50)

        # 创建节点列表
        nodes = []
        self_id = event.get_self_id() or "0"
        for segment in segments:
            if segment.strip():
                node = Node(
                    name=bot_name,
                    uin=self_id,
                    content=[Plain(segment)]
                )
                nodes.append(node)

        if not nodes:
            logger.warning("[聊天增强器] 合并转发内容为空，跳过")
            return

        # 发送合并转发
        try:
            result_chain = MessageChain()
            result_chain.chain = [Nodes(nodes=nodes)]
            await event.send(result_chain)
            logger.info(f"[聊天增强器] 已发送合并转发消息，共 {len(nodes)} 段")
        except Exception as e:
            logger.error(f"[聊天增强器] 合并转发失败: {e}，回退到普通分段发送")
            await self._send_segments(event, segments)

    async def _send_segments(self, event: AstrMessageEvent, segments: List[str]):
        """发送分段消息"""
        speed = self.config.get("send_speed", "自然")

        # 计算延迟
        if speed == "快速":
            delay = 0.3
        elif speed == "慢速":
            delay = 2.5
        else:  # 自然
            delay = 0.8

        for i, segment in enumerate(segments):
            if segment.strip():
                await event.send(event.plain_result(segment))

                # 最后一段不延迟
                if i < len(segments) - 1:
                    await asyncio.sleep(delay)

    async def _handle_result(
        self,
        event: AstrMessageEvent,
        text: str,
        should_forward: bool,
    ):
        """统一的发送处理逻辑：合并转发 / 分段发送 / 直接发送。"""
        # 兜底剥离控制标签，避免 <quote/> 等被当正文发出
        text = CONTROL_TAG_RE.sub("", text or "").strip()
        if not text:
            return
        if should_forward and self._is_forward_platform(event):
            # 合并转发
            await self._send_forward(event, text)
            return

        # 分段发送
        if self.config.get("enable_split", True):
            segments = self._split_message(text)
            if len(segments) > 1:
                await self._send_segments(event, segments)
                return

        # 直接发送（无分段或分段未开启）
        await event.send(event.plain_result(text))

    # ------------------------------------------------------------------ #
    # LLM 事件钩子                                                       #
    # ------------------------------------------------------------------ #

    @filter.on_llm_response()
    async def on_llm_response(self, event: AstrMessageEvent, resp: LLMResponse):
        """在 LLM 响应后处理消息（仅主对话链路触发）"""
        # 获取原始响应文本
        original_text = resp.completion_text

        if not original_text or not original_text.strip():
            return

        # 1. 移除 Markdown 格式
        processed_text = self._remove_markdown(original_text)

        # 2. 标记已处理内容，供装饰阶段使用
        event._chat_enhancer_text = processed_text

    @filter.on_decorating_result()
    async def on_decorating_result(self, event: AstrMessageEvent):
        """在发送消息前进行最终处理。

        只处理 LLM 产生的结果（主对话 AI 回复），避免误伤其他插件
        （如 link_resolver 解析结果、图片/视频等富媒体）直接发出的内容。
        """
        result = event.get_result()
        if result is None or not result.chain:
            return

        # 仅处理 LLM 结果（主对话 AI 回复）或已被 on_llm_response 标记的内容；
        # 其他插件 yield 的普通结果不处理，避免拦截富媒体/特殊内容
        is_llm = False
        try:
            is_llm = result.is_llm_result() or result.is_model_result()
        except Exception:
            is_llm = False
        if not is_llm and not hasattr(event, "_chat_enhancer_text"):
            return

        # 如果消息链中已有 Nodes/Node（其他插件已做合并转发），跳过
        if any(isinstance(c, (Node, Nodes)) for c in result.chain):
            return

        # 提取纯文本内容
        text = "".join(
            comp.text for comp in result.chain if isinstance(comp, Plain)
        ).strip()
        if not text:
            return

        # 如果 on_llm_response 已处理过（主对话链路），使用其已去 MD 的文本
        if hasattr(event, "_chat_enhancer_text"):
            text = event._chat_enhancer_text
        else:
            # LLM 结果统一去 MD
            text = self._remove_markdown(text)

        # 兜底剥离 <quote/> <mention/> <refuse/> 等控制标签（enhance/proactive 引用方案残留）
        text = CONTROL_TAG_RE.sub("", text).strip()
        if not text:
            return

        user_message = event.message_str
        should_forward = self._should_use_forward(text, user_message)

        try:
            if should_forward and self._is_forward_platform(event):
                # 清空原始消息链，通过合并转发发送
                result.chain = []
                await self._send_forward(event, text)
            else:
                # 使用分段发送
                if self.config.get("enable_split", True):
                    segments = self._split_message(text)
                    if len(segments) > 1:
                        # 清空原始消息链
                        result.chain = []
                        await self._send_segments(event, segments)
                    else:
                        # 单段：写回去 MD 后的文本（修复短消息 MD 不生效）
                        result.chain = [Plain(text)]
                else:
                    # 未开启分段：写回去 MD 后的文本
                    result.chain = [Plain(text)]
        except Exception as e:
            logger.error(f"[聊天增强器] 处理消息失败: {e}", exc_info=True)
            # 失败兜底：回填原始文本，避免用户收不到消息
            try:
                result.chain = [Plain(text)]
            except Exception:
                pass

    # ------------------------------------------------------------------ #
    # 指令                                                               #
    # ------------------------------------------------------------------ #

    @filter.command("增强")
    async def cmd_enhancer_status(self, event: AstrMessageEvent):
        """查看增强器状态"""
        platform = event.get_platform_name()
        forward_support = "支持" if self._is_forward_platform(event) else "不支持（自动降级为分段发送）"
        status = f"""聊天增强器状态：

✅ 消息分段: {'开启' if self.config.get('enable_split') else '关闭'}
✅ MD格式消除: {'开启' if self.config.get('remove_markdown') else '关闭'}
✅ 智能合并转发: {'开启' if self.config.get('enable_forward') else '关闭'}

⚙️ 最大分段数: {self.config.get('max_segments', 5)}
⚙️ 转发阈值: {self.config.get('forward_threshold', 500)} 字符
⚙️ 发送速度: {self.config.get('send_speed', '自然')}
⚙️ Bot名称: {self.config.get('bot_name', 'AI助手')}
📱 当前平台: {platform}（合并转发: {forward_support}）"""

        yield event.plain_result(status)

    @filter.command("增强开关")
    async def cmd_toggle_feature(self, event: AstrMessageEvent):
        """切换功能开关"""
        msg = re.sub(r'\[MSG_ID:\d+\]', '', event.message_str).strip()
        parts = msg.split()

        if len(parts) < 2:
            yield event.plain_result("用法: /增强开关 <分段|MD消除|合并转发>")
            return

        feature = parts[1]

        if feature == "分段":
            current = self.config.get("enable_split", True)
            self.config["enable_split"] = not current
            self.config.save_config()
            yield event.plain_result(f"✅ 消息分段已{'开启' if not current else '关闭'}")
        elif feature == "MD消除":
            current = self.config.get("remove_markdown", True)
            self.config["remove_markdown"] = not current
            self.config.save_config()
            yield event.plain_result(f"✅ MD格式消除已{'开启' if not current else '关闭'}")
        elif feature == "合并转发":
            current = self.config.get("enable_forward", True)
            self.config["enable_forward"] = not current
            self.config.save_config()
            yield event.plain_result(f"✅ 智能合并转发已{'开启' if not current else '关闭'}")
        else:
            yield event.plain_result("❌ 未知功能，可用选项: 分段、MD消除、合并转发")

    @filter.command("增强测试")
    async def cmd_test_forward(self, event: AstrMessageEvent):
        """测试合并转发功能"""
        if not self.config.get("enable_forward", True):
            yield event.plain_result("❌ 合并转发功能未开启，请先使用 /增强开关 合并转发 开启")
            return

        if not self._is_forward_platform(event):
            yield event.plain_result(f"❌ 当前平台 ({event.get_platform_name()}) 不支持合并转发，自动使用分段发送")
            await self._send_segments(
                event,
                ["这是一条测试分段消息 1", "这是一条测试分段消息 2", "这是一条测试分段消息 3"],
            )
            return

        # 生成测试内容
        test_text = (
            "这是聊天增强器的合并转发测试消息。\n\n"
            "第一段：合并转发可以将长文本打包成一条可展开的聊天记录，方便阅读。\n\n"
            "第二段：本插件支持自动分段、Markdown 消除、智能合并转发等功能。\n\n"
            "第三段：如果你能看到这条消息并且可以展开查看多段内容，说明合并转发功能工作正常。\n\n"
            "第四段：测试完成，感谢使用聊天增强器插件！"
        )

        yield event.plain_result("🧪 正在测试合并转发功能，请查看收到的合并转发消息...")
        await self._send_forward(event, test_text)
