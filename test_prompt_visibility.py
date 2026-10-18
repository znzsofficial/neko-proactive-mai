import copy
import unittest

from prompt_visibility import filter_request


def task(stamp, plugin="neko.proactive"):
    return (
        f'<plugin_proactive_task id="proactive:{plugin}:{stamp}" plugin_id="{plugin}">\n'
        "插件请求你主动处理一轮聊天：群聊冷场了，主动找个话题聊聊\n"
        '触发原因：attention_proactive\n附加信息：{"source": "lonelymai"}\n'
        "请结合当前聊天关系、记忆和上下文，自行决定是否回复以及如何表达。\n"
        "</plugin_proactive_task>"
    )


def item(text, kind="UserMessageItem"):
    return {
        "item_type": kind,
        "meta": {"item_id": text[-12:]},
        "parts": [{"type": "text", "text": text}],
    }


class PromptVisibilityTests(unittest.TestCase):
    def test_latest_only_and_input_unchanged(self):
        a, b = item(task(1791900000000)), item(task(1791900001000))
        ordinary = item("用户：冷场插件太烦了")
        source = {
            "items": [a, ordinary, b],
            "session_id": "private",
            "tools": ["untouched"],
        }
        before = copy.deepcopy(source)
        result, count = filter_request(source)
        self.assertEqual(count, 1)
        self.assertEqual(result["items"], [ordinary, b])
        self.assertEqual(source, before)
        self.assertIs(result["items"][1], b)

    def test_timestamp_not_accidental_list_order(self):
        newest, older = item(task(1791900001000)), item(task(1791900000000))
        self.assertEqual(
            filter_request({"items": [newest, older]})[0]["items"], [newest]
        )

    def test_duplicate_latest_keeps_last(self):
        a, b = item(task(1791900001000)), item(task(1791900001000))
        result, count = filter_request({"items": [a, b]})
        self.assertEqual(count, 1)
        self.assertIs(result["items"][0], b)

    def test_other_plugins_quotes_roles_media_and_malformed_preserved(self):
        latest = item(task(1791900001000))
        mixed = item(task(1791900000000))
        mixed["parts"].append({"type": "image", "image_base64": "kept"})
        keep = [
            item(task(1791900000000, "other-plugin")),
            item("用户引用：\n" + task(1791900000000)),
            item(task(1791900000000), "AssistantMessageItem"),
            item(task(1791900000000), "FunctionCallOutputItem"),
            mixed,
            item(task(1791900000000).replace("</plugin_proactive_task>", "")),
            item(task(1791900000000) + "\n" + task(1791900000001)),
            {"item_type": "ReasoningItem", "text_parts": ["冷场"]},
            None,
        ]
        result, count = filter_request({"items": keep + [latest]})
        self.assertEqual(count, 0)
        self.assertEqual(result["items"], keep + [latest])

    def test_zero_single_and_unknown_noop(self):
        for payload in [
            {},
            {"items": None},
            {"items": []},
            {"items": [item(task(1791900000000))]},
        ]:
            result, count = filter_request(payload)
            self.assertEqual(count, 0)
            self.assertEqual(result, payload)

    def test_dual_shapes_and_idempotence(self):
        texts = [task(1791900000000), task(1791900001000)]
        payload = {
            "items": [item(t) for t in texts],
            "messages": [{"role": "user", "content": t} for t in texts],
        }
        result, count = filter_request(payload)
        self.assertEqual(count, 2)
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(len(result["messages"]), 1)
        self.assertEqual(filter_request(result), (result, 0))

    def test_no_cross_request_chat_state(self):
        first = {"items": [item(task(1791900001000))], "session_id": "one"}
        second = {"items": [item(task(1791900000000))], "session_id": "two"}
        filter_request(first)
        self.assertEqual(filter_request(second), (second, 0))
