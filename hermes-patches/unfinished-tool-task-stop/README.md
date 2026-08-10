# Hermes unfinished tool-task continuation guard

这是 MyAIAgent V4 使用的 Hermes Agent 侧增强补丁。

## 解决的问题

Hermes 在执行多步骤工具任务时，模型有时会在任务仍未完成的情况下返回 `finish_reason=stop`，同时 assistant 自己又明确说明仍有步骤未完成或尚未通过验证。

旧行为可能把这种响应直接当作最终回答，导致任务做到一半停止，需要用户再次发送“继续”。

本补丁增加一个有边界的 Runtime continuation guard。

当用户明确要求真实执行工具任务、当前存在可调用工具，并且 assistant 自己明确表示仍有执行或验证工作没有完成且需要继续时，Hermes 不立即结束当前任务，而是注入一次 Runtime correction，继续剩余步骤。

运行时可看到：

`↻ Required tool steps remain unfinished — continuing`

## 文件

- `unfinished_tool_task_stop.py`：对应 Hermes 的 `agent/unfinished_tool_task_stop.py`
- `conversation_loop.patch`：对 Hermes `agent/conversation_loop.py` 的最小增量补丁
- `SHA256SUMS`：补丁文件校验值

## 识别范围

包含真实运行中出现过的措辞，例如：

- 未完成、尚未完成
- 尚未通过验证、未通过验证
- 尚未成功验证
- 需要继续、仍需执行、需要执行
- 才算完成、才算真实完成、才能报告完成

同时保留保护条件：

- 用户明确要求不要执行或只解释时不触发
- 真正已经完成时不触发
- 没有工具时不触发
- continuation 有最大次数限制
- 明确 unfinished 状态优先于自相矛盾的 completion 声明

## 安装

在 Hermes 源码根目录：

1. 将 `unfinished_tool_task_stop.py` 复制到 `agent/unfinished_tool_task_stop.py`
2. 应用 `conversation_loop.patch`
3. 执行 Python 语法检查
4. 若 Hermes Gateway 为长期运行进程，重启 Gateway 以加载新源码

## 2026-08-10 验证

- Detector 边界测试：8/8 passed
- 既有关键回归：65 passed
- Runtime Guard：真实 conversation_loop 命中
- 自然 S3 多步骤任务完整成功：rclone → Bucket read → upload → download → cmp

## 设计原则

这不是全局强制模型永远继续工作。

它只处理一个窄场景：assistant 自己已经明确承认用户要求的真实工具工作仍未完成，却准备以普通 `finish_reason=stop` 结束。

## 安全

本目录只保存通用代码和 patch，不包含 API Key、S3 Access Key、S3 Secret Key、私人运行配置或测试凭据。
