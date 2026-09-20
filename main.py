import re
import os
import json
import asyncio
import sqlite3
import time
from typing import List, Tuple

from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api import logger, AstrBotConfig
from astrbot.api.star import Context, Star, StarTools
from astrbot.api.message_components import Plain, BaseMessageComponent, Node, Nodes
from astrbot.api.provider import LLMResponse

# 兜底：剥离模型可能输出的 <quote .../> <mention .../> <refuse/> 等控制标签，
# 防止被当作普通文本发送出去（配合 enhance_mode/proactive_reply 的引用标签方案）。
CONTROL_TAG_RE = re.compile(r"</?(?:quote|mention|refuse)\b[^>]*>", re.IGNORECASE)
# 内部标注/心理描写清理：情绪标签（mmx_speech TTS 用）与模型漏出的内心独白
EMOTION_TAG_RE = re.compile(
    r"^\s*[\[【(（]\s*(?:开心|高兴|快乐|悲伤|难过|伤心|委屈|愤怒|生气|恼火|害怕|恐惧|厌恶|嫌弃|惊讶|震惊|兴奋|平静|温柔|慵懒)\s*[\]】)）]\s*",
)
EMOTION_TAG_ANY_RE = re.compile(
    r"[\[【]\s*(?:开心|高兴|快乐|悲伤|难过|伤心|委屈|愤怒|生气|恼火|害怕|恐惧|厌恶|嫌弃|惊讶|震惊|兴奋|平静|温柔|慵懒)\s*[\]】]\s*",
)
INNER_MONOLOGUE_RE = re.compile(
    r"[（(]\s*(?:心想|内心OS|内心os|心中|心里想|暗自|内心|内心独白|OS)\s*[：:][^）)]{0,400}[）)]",
)
THINK_BLOCK_RE = re.compile(r"<think(?:ing)?>[\s\S]*?</think(?:ing)?>", re.IGNORECASE)
# 表情包插件的情绪标记（正常由 meme_manager 自己剥离；它未接管时兜底，避免 &&happy&& 被当正文发出）
EMOTION_MARKUP_RE = re.compile(r"&&[^&\n]{1,20}&&")


# 其它内部上下文痕迹（记忆/任务/系统提示被模型复述出来时清理）
SYSTEM_REMINDER_RE = re.compile(r"<system_reminder>[\s\S]*?</system_reminder>", re.IGNORECASE)
GLOBAL_CONTEXT_RE = re.compile(r"<recent_global_context>[\s\S]*?</recent_global_context>", re.IGNORECASE)
INTERNAL_HINT_RE = re.compile(r"\[内部指示\][^\n]*", re.IGNORECASE)
CRON_LINE_RE = re.compile(r"^\s*\[CronJob\][^\n]*", re.MULTILINE)
TASK_RESULT_RE = re.compile(r"^\s*Output your last task result below\.[^\n]*", re.MULTILINE | re.IGNORECASE)
MSG_ID_RE = re.compile(r"\[MSG_ID:\d+\]")
MEMORY_HEAD_RE = re.compile(r"^\s*【记忆参考】[^\n]*", re.MULTILINE)


# 模型被历史里的 refuse 污染后会把 "refuse" 当正文输出（曾引发模仿循环）
REFUSE_ONLY_RE = re.compile(
    r"^\s*(?:refuse\s*/?>?|<refuse\s*/>|refuse)(?:\s*[,，.。!！]?\s*(?:refuse|<refuse\s*/>))*\s*$",
    re.IGNORECASE,
)


def is_refuse_only(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return False
    if "<refuse" in t.lower():
        t = re.sub(r"</?refuse\s*/?>", " refuse ", t, flags=re.IGNORECASE)
    return bool(REFUSE_ONLY_RE.match(t))


def clean_internal_markup(text: str) -> str:
    """剥掉不应出现在聊天里的内部标注：情绪标签、内心独白、think 块、系统/记忆痕迹。"""
    t = text or ""
    t = THINK_BLOCK_RE.sub("", t)
    t = SYSTEM_REMINDER_RE.sub("", t)
    t = GLOBAL_CONTEXT_RE.sub("", t)
    t = INTERNAL_HINT_RE.sub("", t)
    t = CRON_LINE_RE.sub("", t)
    t = TASK_RESULT_RE.sub("", t)
    t = MEMORY_HEAD_RE.sub("", t)
    t = MSG_ID_RE.sub("", t)
    t = EMOTION_TAG_ANY_RE.sub("", t)
    t = EMOTION_MARKUP_RE.sub("", t)
    t = INNER_MONOLOGUE_RE.sub("", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()



SYNTHETIC_MSG_RE = re.compile(
    r"\[CronJob\]|\[BackgroundTask\]|Output your last task result below\.?|"
    r"I finished (?:this job|the task), here is the result",
    re.IGNORECASE,
)
# 插件注入的"指令块"被当成 user 消息存进历史后，模型会误以为用户说过这些话
# （曾导致内部指令泄漏/奇怪回复）。rp4DeepSeek 的【思维模式要求】属于此类。
INSTRUCTION_BLOCK_RE = re.compile(
    r"^\s*【(?:思维模式要求|角色沉浸要求|系统提示|内部指示|记忆参考|系统指令)】"
)
INJECT_HEADER_RE = re.compile(r"【记忆参考】以下是你与该用户在其他私聊中的最近对话记录")


def is_synthetic_message(text) -> bool:
    """是否为不该出现在历史里的合成消息（后台任务回显 / 插件指令块）。"""
    if not text:
        return False
    s = text if isinstance(text, str) else str(text)
    if SYNTHETIC_MSG_RE.search(s):
        return True
    # 通篇都是插件注入的指令块（历史污染），整条丢弃
    return bool(INSTRUCTION_BLOCK_RE.match(s.strip()))


def clean_synthetic_lines(text: str) -> str:
    """逐行清洗文本中的合成任务痕迹（用于 system_prompt 等不能整条丢弃的场景）。"""
    t = text or ""
    t = CRON_LINE_RE.sub("", t)
    t = TASK_RESULT_RE.sub("", t)
    t = re.sub(r"^\s*\[BackgroundTask\][^\n]*", "", t, flags=re.MULTILINE)
    t = re.sub(r"^\s*I finished (?:this job|the task), here is the result[^\n]*", "", t, flags=re.MULTILINE | re.IGNORECASE)
    return re.sub(r"\n{3,}", "\n\n", t)


EXCLAM_RE = re.compile(r"[！!]")


def limit_exclamations(text: str, limit: int = 1) -> str:
    """收敛感叹号：最多保留 limit 个，多余的句末感叹号改为句号。

    用户反馈"说话不要一直用感叹号"。仅靠人设规则模型会反复犯，这里做确定性兜底：
    - 连用感叹号（！！/！！！）合并为一个
    - 「！？」「？！］统一为「？」
    - 超过 limit 的「！」改成「。」
    limit < 0 表示不限制；limit = 0 表示完全不用感叹号（全部改句号）。
    """
    if not text:
        return text
    t = str(text)
    t = re.sub(r"[！!]{2,}", "！", t)
    t = re.sub(r"[！!][？?]", "？", t)
    t = re.sub(r"[？?][！!]", "？", t)
    # 爆发式连标点（"你！到！底！"）→ 整段删掉中间感叹号，保留最后一个
    prev = None
    while prev != t:
        prev = t
        t = re.sub(r"！(?=[\u4e00-\u9fff]！)", "", t)
    if limit < 0:
        return t
    out = []
    seen = 0
    n = len(t)
    for i, ch in enumerate(t):
        if ch in "！!":
            seen += 1
            if seen <= limit:
                out.append("！")
                continue
            # 超出限制的感叹号统一改句号（爆发式连标点已在上一步处理）
            out.append("。")
            continue
        out.append(ch)
    return "".join(out)


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
        # 后台定时清理历史里的合成任务痕迹（cron/后台任务提示词与回显）
        if self.config.get("auto_purge_history", True):
            self._purge_task = asyncio.create_task(self._history_purge_loop())
            logger.info("[聊天增强器] 历史净化任务已启动（每 15 分钟一次）")

    async def _history_purge_loop(self):
        """周期性把合成任务消息（cron 提示词/回显）从对话历史里删掉。

        核心的 history_saver 曾把 "[CronJob] ... I finished this job" 写进用户
        私聊/群聊历史，模型会照抄该格式导致"回复很奇怪"。这里做兜底清理，
        即使 AstrBot 升级覆盖了核心补丁，历史也不会被持续污染。
        """
        while True:
            try:
                await asyncio.sleep(900)
                removed = await asyncio.to_thread(self._purge_synthetic_history)
                if removed:
                    logger.info(f"[聊天增强器] 历史净化：清除合成任务消息 {removed} 条")
            except asyncio.CancelledError:
                return
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[聊天增强器] 历史净化失败（已忽略）: {exc}")

    def _find_db_path(self) -> str:
        """定位 AstrBot 的会话数据库路径。"""
        candidates = []
        try:
            from astrbot.core.utils.astrbot_path import get_astrbot_data_path

            candidates.append(os.path.join(get_astrbot_data_path(), "data_v4.db"))
        except Exception:  # noqa: BLE001
            pass
        try:
            candidates.append(
                os.path.join(
                    StarTools.get_data_dir().parent.parent, "data_v4.db"
                )
            )
        except Exception:  # noqa: BLE001
            pass
        candidates.append(
            "/root/.local/share/uv/tools/astrbot/data/data_v4.db"
        )
        for p in candidates:
            if p and os.path.exists(p):
                return p
        return ""

    def _purge_synthetic_history(self) -> int:
        """同步执行的历史清理，返回删除的消息条数。"""
        db_path = self._find_db_path()
        if not db_path:
            return 0
        con = sqlite3.connect(db_path, timeout=30)
        removed = 0
        try:
            cur = con.cursor()
            cur.execute("SELECT conversation_id, content FROM conversations")
            rows = cur.fetchall()
            for cid, content in rows:
                try:
                    msgs = json.loads(content)
                except Exception:
                    continue
                if not isinstance(msgs, list):
                    continue
                new = []
                touched = False
                for m in msgs:
                    if not isinstance(m, dict):
                        new.append(m)
                        continue
                    c = m.get("content")
                    if isinstance(c, str):
                        if is_synthetic_message(c):
                            touched = True
                            removed += 1
                            continue
                        n = clean_synthetic_lines(c)
                        if n != c:
                            touched = True
                            m = dict(m)
                            m["content"] = n
                    elif isinstance(c, list):
                        parts = []
                        for p in c:
                            if isinstance(p, dict) and p.get("type") == "text":
                                raw = str(p.get("text") or "")
                                if is_synthetic_message(raw):
                                    touched = True
                                    removed += 1
                                    continue
                                t = clean_synthetic_lines(raw)
                                if t != raw:
                                    touched = True
                                    p = dict(p)
                                    p["text"] = t
                            parts.append(p)
                        if parts != c:
                            m = dict(m)
                            m["content"] = parts
                    new.append(m)
                if touched:
                    cur.execute(
                        "UPDATE conversations SET content=? WHERE conversation_id=?",
                        (json.dumps(new, ensure_ascii=False), cid),
                    )
            con.commit()
        finally:
            con.close()
        return removed

    async def terminate(self):
        task = getattr(self, "_purge_task", None)
        if task:
            task.cancel()

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
        text = clean_internal_markup(text)
        text = EMOTION_TAG_RE.sub("", text).strip()
        # 收敛感叹号（避免"每句都用！"）
        text = limit_exclamations(
            text, int(self.config.get("max_exclamation_per_message", 0) or 0)
        ).strip()
        if is_refuse_only(text):
            logger.info("[聊天增强器] 直发出口拦截纯 refuse 文本，已取消发送")
            return
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

    def _dump_request_snapshot(self, event, req):
        """把最终发给模型的请求落盘，便于排查"认错人/串台"（默认关闭）。"""
        try:
            import datetime as _dt

            def _preview(m):
                c = m.get("content") if isinstance(m, dict) else None
                if isinstance(c, list):
                    c = " ".join(
                        str(p.get("text") or p.get("think") or "")[:120]
                        for p in c
                        if isinstance(p, dict)
                    )
                return {"role": m.get("role"), "content": str(c)[:600]}

            out = {
                "ts": _dt.datetime.now().isoformat(timespec="seconds"),
                "umo": event.unified_msg_origin,
                "sender_id": str(event.get_sender_id() or ""),
                "sender_name": event.get_sender_name(),
                "group_id": str(event.get_group_id() or "") if hasattr(event, "get_group_id") else "",
                "message_str": event.message_str[:300],
                "prompt": str(getattr(req, "prompt", ""))[:1000],
                "system_prompt": str(getattr(req, "system_prompt", "")),
                "contexts": [_preview(m) for m in (getattr(req, "contexts", None) or [])],
            }
            path = os.path.join(
                "/root/.local/share/uv/tools/astrbot/data/plugin_data",
                "chat_enhancer_last_request.json",
            )
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as fp:
                json.dump(out, fp, ensure_ascii=False, indent=2)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[聊天增强器] 请求快照写入失败: {exc}")

    @filter.on_llm_request(priority=-99999)
    async def on_llm_request(self, event: AstrMessageEvent, req):
        """请求前清洗注入内容与历史，防止模型被后台任务/跨群注入文本带偏。

        历史里一旦残留 "[CronJob] ... I finished this job" / "Output your last task
        result below." / "【记忆参考】..." 这类合成文本，模型就会照抄这种格式，
        表现为"回复很奇怪"，并且会被跨群插件二次传染到别的会话。
        """
        try:
            ctxs = getattr(req, "contexts", None)
            if ctxs:
                cleaned = []
                for m in ctxs:
                    if not isinstance(m, dict):
                        cleaned.append(m)
                        continue
                    content = m.get("content")
                    if isinstance(content, str):
                        if is_synthetic_message(content):
                            continue  # 整条合成消息丢弃
                        new = clean_synthetic_lines(content)
                        if not new:
                            continue
                        if new != content:
                            m = dict(m)
                            m["content"] = new
                    elif isinstance(content, list):
                        parts = []
                        for p in content:
                            if isinstance(p, dict) and p.get("type") == "text":
                                raw = str(p.get("text") or "")
                                if is_synthetic_message(raw):
                                    continue
                                t = clean_synthetic_lines(raw)
                                if not t:
                                    continue
                                if t != raw:
                                    p = dict(p)
                                    p["text"] = t
                            parts.append(p)
                        if not parts:
                            continue
                        m = dict(m)
                        m["content"] = parts
                    cleaned.append(m)
                if len(cleaned) != len(ctxs):
                    logger.info(
                        f"[聊天增强器] 请求前丢弃合成任务消息 {len(ctxs) - len(cleaned)} 条"
                    )
                req.contexts = cleaned
            # system_prompt 里的合成回显逐行清掉（不整条丢弃，避免破坏人设提示词）
            sp = getattr(req, "system_prompt", None)
            if isinstance(sp, str) and sp:
                # 只清合成任务回显；<recent_global_context> 是 LivelyState 的状态注入，保留
                new_sp = clean_synthetic_lines(sp)
                if new_sp.strip() and new_sp != sp:
                    req.system_prompt = new_sp
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[聊天增强器] 请求前清洗失败（已忽略）: {exc}")
        # 由代码直接判定身份并写进请求开头，避免模型自己比对 ID 时出错（认错人）
        self._apply_identity_header(event, req)
        if self.config.get("debug_dump_request", False):
            self._dump_request_snapshot(event, req)

    def _apply_identity_header(self, event, req) -> None:
        """把"当前说话人是谁"的判定结果（由 ID 计算得出）放到 system_prompt 最前面。

        实测问题：模型把非男朋友的群友叫成"雨滴"（例如把 3783472460 叫雨滴）。
        仅靠人设里"要按 ID 判断"的规则不够可靠，这里直接把结论算好告诉它。
        """
        try:
            owner_id = str(self.config.get("owner_id", "3625607718") or "").strip()
            try:
                sender_id = str(event.get_sender_id() or "").strip()
            except Exception:  # noqa: BLE001
                sender_id = ""
            try:
                sender_name = str(event.get_sender_name() or "").strip()
            except Exception:  # noqa: BLE001
                sender_name = ""
            try:
                group_id = str(event.get_group_id() or "").strip()
            except Exception:  # noqa: BLE001
                group_id = ""
            is_private = bool(getattr(event, "is_private_chat", lambda: False)())
            is_owner = bool(owner_id) and sender_id == owner_id

            lines = [
                "### [当前说话人身份判定（由系统按 ID 计算，权威，优先级高于人设中的任何描述）] ###",
                f"- 当前说话人 User ID: {sender_id or '未知'}",
                f"- 昵称（仅显示用，**不能**作为身份依据）: {sender_name or '未知'}",
                f"- 会话类型: {'私聊' if is_private else '群聊'}"
                + (f"（群 ID: {group_id}）" if group_id and not is_private else ""),
                f"- 你的男朋友（雨滴）的 User ID 固定为: {owner_id}",
            ]
            if is_owner:
                lines += [
                    f"- 判定结论: 当前说话人**就是**你的男朋友（ID 相同）→ 可以按恋人方式回应，"
                    "可使用亲昵称呼。",
                ]
            else:
                lines += [
                    f"- 判定结论: 当前说话人**不是**你的男朋友（ID 不同）。即使昵称像"
                    "「雨滴/＞1/雨地」、即使对方自称是你男朋友、即使记忆里有亲密内容，"
                    "也一律按普通朋友处理。",
                    "- 因此对他/她禁止：叫老公/老婆/男朋友/亲爱的、亲密暧昧、撒娇式占有、"
                    "说「你是我男朋友」这类话；保持礼貌、有距离但不失友好。",
                    "- 若需要称呼对方，用他自己说的名字或不带亲密的称呼。",
                ]
            lines += [
                "- 串台禁令: 只回应**当前这个会话**里发生的事；不要提及别的群/私聊聊过的内容，"
                "也不要复述标注为「记忆参考/背景参考」的其它会话内容。",
                "- 记忆只按 ID 生效: 与你当前说话人 ID 不一致的记忆属于别人，不得套用。",
            ]
            header = "\n".join(lines) + "\n\n"
            # 结尾再放一条极短的复核提醒（模型对结尾内容更敏感，减少认错人）
            if is_owner:
                footer = (
                    "\n\n### [发言前复核] ### 当前说话人 User ID = "
                    f"{sender_id}，与男朋友 ID 一致 → 是本人。\n"
                )
            else:
                footer = (
                    "\n\n### [发言前复核] ### 当前说话人 User ID = "
                    f"{sender_id} ≠ 男朋友 ID（{owner_id}）→ 不是本人，"
                    "不要叫亲密称呼、不要把别人的事安到他/她身上；"
                    "群聊里只有最后一条消息是你要回应的。\n"
                )
            sp = getattr(req, "system_prompt", None)
            if isinstance(sp, str):
                if "[当前说话人身份判定" not in sp:
                    req.system_prompt = header + sp + footer
            else:
                req.system_prompt = header + footer
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[聊天增强器] 身份判定头写入失败（已忽略）: {exc}")

    # priority 设高：确保在 mmx_speech / 其它插件读取响应之前就把文本规范化，
    # 否则语音插件(voice_only 模式)会抢先用原文接管发送，导致标点收敛失效。
    @filter.on_using_llm_tool()
    async def sanitize_outgoing_tool_text(self, event, tool, tool_args):
        """工具直接发消息时也要收敛文本。

        定时任务/主动消息让模型调用 `send_message_to_user`，那段文本是模型直接写的参数，
        **不经过 on_llm_response**，若不管就会出现"每句都是感叹号"的主动消息。
        这里在执行前就地清洗参数里的文本内容。
        """
        try:
            if not isinstance(tool_args, dict):
                return
            name = str(getattr(tool, "name", "") or "")
            if "send" not in name and "message" not in name:
                return
            limit = int(self.config.get("max_exclamation_per_message", 0) or 0)

            def _fix(t):
                if not isinstance(t, str) or not t.strip():
                    return t
                return limit_exclamations(clean_internal_markup(t), limit).strip() or t

            msgs = tool_args.get("messages")
            if isinstance(msgs, list):
                for item in msgs:
                    if isinstance(item, dict) and isinstance(item.get("text"), str):
                        item["text"] = _fix(item["text"])
            for key in ("message", "content", "text", "msg"):
                if isinstance(tool_args.get(key), str):
                    tool_args[key] = _fix(tool_args[key])
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[聊天增强器] 工具文本清洗失败（已忽略）: {exc}")

    @filter.on_llm_response(priority=99998)
    async def on_llm_response(self, event: AstrMessageEvent, resp: LLMResponse):
        """在 LLM 响应后处理消息（仅主对话链路触发）"""
        # 获取原始响应文本
        original_text = resp.completion_text

        if not original_text or not original_text.strip():
            return

        # 纯 refuse 回复：清空响应 → 不发送、也不会写入对话历史（避免模仿循环）
        if is_refuse_only(original_text):
            logger.info("[聊天增强器] 检测到纯 refuse 回复，已丢弃且不写入历史")
            resp.completion_text = ""
            event._chat_enhancer_text = ""
            return

        # 1. 移除 Markdown 格式
        processed_text = self._remove_markdown(original_text)

        # 2. 收敛感叹号：直接写回 resp.completion_text，保证所有后续插件
        #    （mmx_speech voice_only 自行发文本、分段/合并转发等）拿到的都是收敛后的文本
        limited = limit_exclamations(
            processed_text, int(self.config.get("max_exclamation_per_message", 0) or 0)
        ).strip()
        if limited and limited != original_text:
            resp.completion_text = limited
            processed_text = limited

        # 3. 标记已处理内容，供装饰阶段使用
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
        # 兜底剥离情绪标签与内心独白（mmx_speech / 角色扮演提示词可能让模型漏出）
        text = clean_internal_markup(text)
        text = EMOTION_TAG_RE.sub("", text).strip()
        # 收敛感叹号（避免"每句都用！"）
        text = limit_exclamations(
            text, int(self.config.get("max_exclamation_per_message", 0) or 0)
        ).strip()
        # 纯 refuse 一律不发送（历史上曾经把 refuse 当正文发出去）
        if is_refuse_only(text):
            logger.info("[聊天增强器] 装饰阶段拦截纯 refuse 文本，已取消发送")
            result.chain = []
            return
        if not text:
            logger.info("[聊天增强器] 清洗后内容为空（仅剩内部标记），已取消发送")
            result.chain = []
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
