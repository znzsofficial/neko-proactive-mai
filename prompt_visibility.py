"""Request-only filtering of standalone, host-generated proactive tasks."""

import re


TASK = re.compile(
    r'<plugin_proactive_task id="proactive:(?:neko\.proactive|attention-seeker-mai):(\d{10,16})" '
    r'plugin_id="(?:neko\.proactive|attention-seeker-mai)">\n'
    r"插件请求你主动处理一轮聊天：[^\x00]*\n"
    r"请结合当前聊天关系、记忆和上下文，自行决定是否回复以及如何表达。\n"
    r"</plugin_proactive_task>"
)


def task_timestamp(item, *, legacy=False):
    """Only whole text-only user items; never quotes, tools, or mixed media."""
    if not isinstance(item, dict):
        return None
    if legacy:
        if item.get("role") != "user" or not isinstance(item.get("content"), str):
            return None
        text = item["content"]
    else:
        if item.get("item_type") != "UserMessageItem":
            return None
        parts = item.get("parts")
        if not isinstance(parts, list) or len(parts) != 1:
            return None
        part = parts[0]
        if (
            not isinstance(part, dict)
            or part.get("type") != "text"
            or not isinstance(part.get("text"), str)
        ):
            return None
        text = part["text"]
    # Nested/quoted blocks are not standalone host tasks.
    if (
        text.count("<plugin_proactive_task") != 1
        or text.count("</plugin_proactive_task>") != 1
    ):
        return None
    match = TASK.fullmatch(text)
    return int(match[1]) if match else None


def filter_request(kwargs):
    """Preserve newest task (timestamp, then position) in each request list.

    No cross-chat state, history mutation, insertion, or persistent receipts.
    Unknown serialization formats are deliberately left intact.
    """
    modified = dict(kwargs)
    removed = 0
    for key in ("items", "messages"):
        items = kwargs.get(key)
        if not isinstance(items, list):
            continue
        owned = [
            (stamp, index)
            for index, item in enumerate(items)
            if (stamp := task_timestamp(item, legacy=key == "messages")) is not None
        ]
        if len(owned) <= 1:
            continue
        keep = max(owned)[1]
        discard = {index for _, index in owned if index != keep}
        modified[key] = [
            item for index, item in enumerate(items) if index not in discard
        ]
        removed += len(discard)
    return modified, removed
