"""Stop-guard for explicit self-declared unfinished tool tasks."""

from __future__ import annotations

_UNFINISHED = (
    "未完成",
    "尚未完成",
    "仍未完成",
    "还未完成",
    "尚未通过验证",
    "未通过验证",
    "还未通过验证",
    "尚未成功验证",
    "未成功验证",
    "没有全部完成",
    "还没有完成",
    "⏳",
    "not completed",
    "still pending",
    "remaining",
)
_CONTINUE = (
    "下一步",
    "继续执行",
    "必须继续",
    "接着执行",
    "然后执行",
    "需要继续",
    "还需要继续",
    "仍需继续",
    "需要执行",
    "仍需执行",
    "还需要执行",
    "才算完成",
    "才算真实完成",
    "才能报告完成",
    "next step",
    "continue executing",
    "continue with",
)
_EXECUTE = ("执行", "运行", "测试", "上传", "下载", "读取", "写入", "安装", "配置", "修复", "terminal", "rclone", "curl", "pytest", "python", "bash", "shell")
_NO_EXECUTE = ("不要执行", "不要运行", "无需执行", "只解释", "只告诉我", "仅说明", "do not execute", "don't execute")
_COMPLETE = ("任务已完成", "验收已完成", "全部关键步骤均通过", "所有关键步骤均通过", "all required steps completed", "acceptance completed")

def build_unfinished_tool_task_nudge(*, user_message, assistant_content: str, valid_tool_names, attempts: int) -> str | None:
    if attempts >= 3 or not valid_tool_names:
        return None
    user = str(user_message or "").lower()
    text = str(assistant_content or "").strip()
    low = text.lower()
    if not text:
        return None
    if any(marker.lower() in user for marker in _NO_EXECUTE):
        return None
    if not any(marker.lower() in user for marker in _EXECUTE):
        return None
    unfinished = any(
        marker.lower() in low
        for marker in _UNFINISHED
    )
    continuation = any(
        marker.lower() in low
        for marker in _CONTINUE
    )

    # Explicit current unfinished work wins over a contradictory completion
    # claim.  A response such as "all steps passed, but cmp is still pending
    # and next we must run cmp" must not be allowed to finalize.
    if unfinished and continuation:
        return ("[Runtime correction: Your own previous response explicitly states that required tool work remains unfinished. Do not summarize or stop. Execute the next pending tool action now. Continue through the user's remaining required steps, and only send the final answer after the stated completion criteria have actually been satisfied.]")

    if any(marker.lower() in low for marker in _COMPLETE):
        return None

    return None
