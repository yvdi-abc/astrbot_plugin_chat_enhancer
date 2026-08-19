# AstrBot 聊天增强器插件

[![Version](https://img.shields.io/badge/version-v1.0.0-blue.svg)](https://github.com/yvdi-abc/astrbot_plugin_chat_enhancer)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![AstrBot](https://img.shields.io/badge/AstrBot-Plugin-orange.svg)](https://github.com/Soulter/AstrBot)

集成消息分段发送、Markdown格式消除、智能长文本合并转发等功能，让 AI 回复更自然，模拟真实聊天体验。

## ✨ 功能特性

### 🎯 核心功能

1. **消息智能分段** - 将长消息分段发送，模拟真实聊天节奏
2. **Markdown 格式消除** - 自动移除 `**粗体**`、`*斜体*`、`` `代码` `` 等格式
3. **智能合并转发** - 检测长篇回答，自动使用聊天记录形式发送
4. **可选开关** - 所有功能都可以独立开关控制

### 📋 适用场景

- ✅ **日常聊天** - 自动分段，避免一次性发送大段文字
- ✅ **知识解答** - 长篇解释自动转为合并转发，更易阅读
- ✅ **QQ 群聊** - 消除 MD 格式，让 AI 回复在 QQ 中正常显示
- ✅ **教程说明** - 检测关键词，智能判断是否需要合并转发

## 📦 安装

### 方法一：通过 AstrBot 插件商店（推荐）

1. 在 AstrBot 控制面板中打开插件商店
2. 搜索 "聊天增强器"
3. 点击安装并重启 AstrBot

### 方法二：手动安装

```bash
git clone https://github.com/yvdi-abc/astrbot_plugin_chat_enhancer.git
```

将 `astrbot_plugin_chat_enhancer` 文件夹复制到 AstrBot 的 `data/plugins` 目录，然后重启 AstrBot。

## 🚀 使用方法

### 可用指令

| 指令 | 说明 | 示例 |
|------|------|------|
| `/增强` | 查看增强器状态 | `/增强` |
| `/增强开关 <功能>` | 切换功能开关 | `/增强开关 分段` |

### 功能开关选项

- `分段` - 消息分段发送
- `MD消除` - Markdown 格式消除
- `合并转发` - 智能合并转发

## ⚙️ 配置说明

在 AstrBot 控制面板的插件配置中，你可以设置以下参数：

### 基础配置

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `enable_split` | bool | `true` | 是否启用消息分段发送 |
| `max_segments` | int | `5` | 最大分段数（超过则触发合并转发） |
| `split_chars` | list | `["。", "！", "？", ...]` | 分段符号列表 |

### Markdown 消除

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `remove_markdown` | bool | `true` | 是否移除 MD 格式 |
| `keep_code_blocks` | bool | `true` | 保留代码块（即使开启 MD 消除） |

### 智能合并转发

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `enable_forward` | bool | `true` | 是否启用智能合并转发 |
| `forward_threshold` | int | `500` | 触发合并转发的字符数阈值 |
| `forward_keywords` | list | `["解释", "说明", ...]` | 触发合并转发的关键词 |
| `bot_name` | string | `"AI助手"` | 合并转发中显示的 Bot 名称 |

### 发送控制

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `send_speed` | string | `"自然"` | 发送速度：快速/自然/慢速 |

## 💡 使用示例

### 示例 1: 普通对话（自动分段）

```
用户: 你好

Bot: 你好！
Bot: 我是AI助手，有什么可以帮助你的吗？
```

### 示例 2: 长文本（合并转发）

```
用户: 请详细解释一下什么是机器学习

Bot: [合并转发消息]
    AI助手: 机器学习是人工智能的一个分支...
    AI助手: 它主要包括三种类型...
    AI助手: 监督学习是指...
    AI助手: 无监督学习则是...
    AI助手: 强化学习通过...
```

### 示例 3: Markdown 格式消除

**原始输出：**
```
这是**粗体**文字，这是*斜体*文字，这是`代码`。
```

**消除后输出：**
```
这是粗体文字，这是斜体文字，这是代码。
```

### 示例 4: 切换功能

```
用户: /增强
Bot: 聊天增强器状态：
     ✅ 消息分段: 开启
     ✅ MD格式消除: 开启
     ✅ 智能合并转发: 开启

用户: /增强开关 MD消除
Bot: ✅ MD格式消除已关闭
```

## 🔧 工作原理

### 1. 消息分段流程

```
LLM响应 → MD格式消除 → 智能分段 → 判断是否合并转发 → 发送
```

### 2. 合并转发触发条件

满足以下**任一**条件时触发合并转发：

1. 消息长度 > `forward_threshold` **且** 用户消息包含触发关键词
2. 分段数 > `max_segments`

### 3. Markdown 格式消除

支持消除以下 MD 格式：

- **粗体**: `**text**` 或 `__text__`
- *斜体*: `*text*` 或 `_text_`
- ~~删除线~~: `~~text~~`
- `行内代码`: `` `code` ``
- 标题: `# ## ###`
- 链接: `[text](url)`
- 图片: `![alt](url)`
- 列表: `- * +` 和数字列表
- 引用: `>`
- 水平线: `--- *** ___`

**注意**: 代码块 ` ```code``` ` 默认保留（可配置）

## 📝 注意事项

- 插件通过 `@filter.on_llm_response()` 钩子处理 AI 回复
- 合并转发功能需要平台支持（主要是 QQ）
- 如果合并转发失败，会自动回退到普通分段发送
- 分段发送会有延迟，可通过 `send_speed` 调整

## 🔄 更新日志

### v1.0.0
- 初始版本发布
- 支持消息智能分段发送
- 支持 Markdown 格式消除
- 支持智能合并转发
- 支持功能独立开关

## 🤝 贡献

欢迎提交 Issue 和 Pull Request！

## 📄 许可证

本项目采用 MIT 许可证 - 详见 [LICENSE](LICENSE) 文件

## 👤 作者

**yvdi-abc**

## 🙏 致谢

- [AstrBot](https://github.com/Soulter/AstrBot) - 优秀的聊天机器人框架
- [astrbot_plugin_splitter](https://github.com/nuomicici/astrbot_plugin_splitter) - 消息分段功能参考

---

如果这个插件对你有帮助，欢迎给个 ⭐ Star！
