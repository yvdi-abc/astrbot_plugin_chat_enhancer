import re
import asyncio
from typing import List, Tuple
from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api import logger, AstrBotConfig
from astrbot.api.star import Context, Star
from astrbot.api.message_components import Plain, BaseMessageComponent, Node, Nodes
from astrbot.api.provider import LLMResponse


class ChatEnhancerPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config

    async def initialize(self):
        """插件初始化"""
        logger.info("聊天增强器插件已加载")
        logger.info(f"消息分段: {self.config.get('enable_split', True)}")
        logger.info(f"MD格式消除: {self.config.get('remove_markdown', True)}")
        logger.info(f"智能合并转发: {self.config.get('enable_forward', True)}")

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
            text = re.sub(code_pattern, lambda m: f"__CODE_BLOCK_{len(code_blocks) - code_blocks[::-1].index(m.group()) - 1}__", text)

        # 移除 MD 格式
        # 粗体 **text** 或 __text__
        text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)
        text = re.sub(r'__(.+?)__', r'\1', text)

        # 斜体 *text* 或 _text_
        text = re.sub(r'\*(.+?)\*', r'\1', text)
        text = re.sub(r'_(.+?)_', r'\1', text)

        # 删除线 ~~text~~
        text = re.sub(r'~~(.+?)~~', r'\1', text)

        # 行内代码 `code`
        text = re.sub(r'`([^`]+?)`', r'\1', text)

        # 标题 # ## ###
        text = re.sub(r'^#{1,6}\s+', '', text, flags=re.MULTILINE)

        # 链接 [text](url)
        text = re.sub(r'\[([^\]]+)\]\([^\)]+\)', r'\1', text)

        # 图片 ![alt](url)
        text = re.sub(r'!\[([^\]]*)\]\([^\)]+\)', r'[图片: \1]', text)

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

        return text

    def _split_message(self, text: str, remove_md: bool = False) -> List[str]:
        """智能分段消息

        Args:
            text: 要分段的文本
            remove_md: 是否对每段移除 MD 格式
        """
        if not self.config.get("enable_split", True):
            result = [text]
            if remove_md:
                result = [self._remove_markdown(seg) for seg in result]
            return result

        max_segments = self.config.get("max_segments", 5)
        split_chars = self.config.get("split_chars", ["。", "！", "？", "?", "!", "\n"])

        segments = []
        current = ""

        for char in text:
            current += char
            if char in split_chars:
                # 检查是否有足够的内容
                if len(current.strip()) > 10:
                    segments.append(current.strip())
                    current = ""

        # 添加剩余内容
        if current.strip():
            segments.append(current.strip())

        # 如果分段太多，合并一些
        if len(segments) > max_segments:
            merged = []
            temp = ""
            for seg in segments:
                if len(merged) < max_segments - 1:
                    merged.append(seg)
                else:
                    temp += seg
            if temp:
                merged.append(temp)
            segments = merged

        # 如果需要，对每段移除 MD 格式
        if remove_md and self.config.get("remove_markdown", True):
            segments = [self._remove_markdown(seg) for seg in segments]

        return segments if segments else [text]

    def _should_use_forward(self, text: str, user_message: str) -> bool:
        """判断是否应该使用合并转发"""
        if not self.config.get("enable_forward", True):
            return False

        # 检查字符数阈值
        threshold = self.config.get("forward_threshold", 500)
        if len(text) < threshold:
            return False

        # 检查用户消息中的关键词
        keywords = self.config.get("forward_keywords", [])
        for keyword in keywords:
            if keyword in user_message:
                logger.info(f"检测到关键词 '{keyword}'，触发合并转发")
                return True

        # 检查分段数（先分段再判断，不去除MD）
        segments = self._split_message(text, remove_md=False)
        max_segments = self.config.get("max_segments", 5)
        if len(segments) > max_segments:
            logger.info(f"分段数 {len(segments)} 超过阈值 {max_segments}，触发合并转发")
            return True

        return False

    async def _send_forward(self, event: AstrMessageEvent, text: str):
        """发送合并转发消息"""
        bot_name = self.config.get("bot_name", "AI助手")
        # 先分段，再对每段去除 MD 格式
        segments = self._split_message(text, remove_md=True)

        # 创建节点列表
        nodes = []
        for segment in segments:
            if segment.strip():
                node = Node(
                    name=bot_name,
                    uin=event.self_id,
                    content=[Plain(segment)]
                )
                nodes.append(node)

        # 发送合并转发
        try:
            result_chain = MessageChain()
            result_chain.chain = [Nodes(nodes=nodes)]
            await event.send(result_chain)
            logger.info(f"已发送合并转发消息，共 {len(nodes)} 段")
        except Exception as e:
            logger.error(f"合并转发失败: {e}，回退到普通分段发送")
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

    @filter.on_llm_response()
    async def on_llm_response(self, event: AstrMessageEvent, resp: LLMResponse):
        """在 LLM 响应后处理消息"""
        # 获取原始响应文本
        original_text = resp.completion_text

        if not original_text or not original_text.strip():
            return

        # 1. 移除 Markdown 格式
        processed_text = self._remove_markdown(original_text)

        # 2. 判断是否使用合并转发
        user_message = event.message_str
        should_forward = self._should_use_forward(processed_text, user_message)

        # 3. 修改响应内容
        resp.completion_text = processed_text

        # 4. 标记处理方式（通过添加标记供后续钩子使用）
        if not hasattr(event, '_chat_enhancer_forward'):
            event._chat_enhancer_forward = should_forward
            event._chat_enhancer_text = processed_text

    @filter.on_decorating_result()
    async def on_decorating_result(self, event: AstrMessageEvent):
        """在发送消息前进行最终处理"""
        result = event.get_result()

        # 检查是否需要特殊处理
        if not hasattr(event, '_chat_enhancer_forward'):
            return

        should_forward = event._chat_enhancer_forward
        text = event._chat_enhancer_text

        if should_forward:
            # 清空原始消息链，我们将通过合并转发发送
            result.chain = []
            # 异步发送合并转发（在后台执行）
            asyncio.create_task(self._send_forward(event, text))
        else:
            # 使用分段发送
            if self.config.get("enable_split", True):
                segments = self._split_message(text)
                if len(segments) > 1:
                    # 清空原始消息链
                    result.chain = []
                    # 异步发送分段消息
                    asyncio.create_task(self._send_segments(event, segments))

    @filter.command("增强")
    async def cmd_enhancer_status(self, event: AstrMessageEvent):
        """查看增强器状态"""
        status = f"""聊天增强器状态：

✅ 消息分段: {'开启' if self.config.get('enable_split') else '关闭'}
✅ MD格式消除: {'开启' if self.config.get('remove_markdown') else '关闭'}
✅ 智能合并转发: {'开启' if self.config.get('enable_forward') else '关闭'}

⚙️ 最大分段数: {self.config.get('max_segments', 5)}
⚙️ 转发阈值: {self.config.get('forward_threshold', 500)} 字符
⚙️ 发送速度: {self.config.get('send_speed', '自然')}
⚙️ Bot名称: {self.config.get('bot_name', 'AI助手')}"""

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
