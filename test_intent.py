import importlib.util
import pathlib
import sys
import tomllib
import types
import unittest
from unittest.mock import AsyncMock, Mock


@unittest.skipUnless(importlib.util.find_spec('maibot_sdk'), 'requires MaiBot SDK')
class LifeIntentTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.root = pathlib.Path(__file__).parent
        pkg = types.ModuleType('lonely_intent_test')
        pkg.__path__ = [str(self.root)]
        sys.modules[pkg.__name__] = pkg
        spec = importlib.util.spec_from_file_location('lonely_intent_test.plugin', self.root / 'plugin.py')
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
        self.mod = mod
        class Bot(mod.LonelyMaiPlugin):
            config = mod.LonelyMaiConfig()
            ctx = types.SimpleNamespace(logger=Mock(), maisaka=types.SimpleNamespace(
                proactive=types.SimpleNamespace(trigger=AsyncMock())))
        self.bot = Bot()
        self.state = mod.ChatStreamState('chat', 'target')

    async def test_default_life_intent_uses_same_planner_path(self):
        self.assertTrue(await self.bot._do_proactive_chat(self.state, 'target'))
        kw = self.bot.ctx.maisaka.proactive.trigger.call_args.kwargs
        self.assertEqual(kw['stream_id'], 'chat')
        self.assertEqual(kw['intent'], self.bot.config.intent.private_text)
        self.assertIn('分享自己的日常', kw['intent'])
        self.assertIn('后来怎么样了', kw['intent'])
        self.assertIn('早安', kw['intent'])
        self.assertIn('叫起床', kw['intent'])
        self.assertLess(len(kw['intent']), 200)
        self.assertEqual(kw['reason'], 'attention_proactive')
        self.assertEqual(kw['metadata'], {'source': 'lonelymai', 'target_id': 'target', 'chat_kind': 'private'})

    async def test_custom_intent_is_not_replaced_or_extended(self):
        self.bot.config.intent.private_text = '自定义意图'
        self.bot.config.intent.reason = 'custom_reason'
        await self.bot._do_proactive_chat(self.state, 'target')
        kw = self.bot.ctx.maisaka.proactive.trigger.call_args.kwargs
        self.assertEqual(kw['intent'], '自定义意图')
        self.assertEqual(kw['reason'], 'custom_reason')

    def test_template_uses_complete_default_and_stays_disabled(self):
        template = tomllib.loads((self.root / 'config.toml').read_text(encoding='utf-8'))
        self.assertEqual(template['intent']['private_text'], self.bot.config.intent.private_text)
        self.assertNotIn('text', template['intent'])
        self.assertNotIn('text', type(self.bot.config.intent).model_fields)
        self.assertFalse(template['plugin']['enabled'])
        self.assertEqual(template['plugin']['config_version'], '2.0.0')
        self.assertEqual(template['plugin']['version'], '2.0.4')

    async def test_group_uses_normal_cold_prompt_not_private_text(self):
        self.bot.config.intent.private_text = '明确私聊定制'
        state = self.mod.ChatStreamState('group-chat', 'target', is_group=True)
        await self.bot._do_proactive_chat(state, 'target')
        kw = self.bot.ctx.maisaka.proactive.trigger.call_args.kwargs
        self.assertEqual(kw['intent'], '群聊冷场了，主动找个话题聊聊')
        self.assertEqual(kw['metadata']['chat_kind'], 'group')

    async def test_group_and_private_custom_prompts_select_independently(self):
        self.bot.config.intent.group_text = '群聊自定义'
        self.bot.config.intent.private_text = '私聊自定义'
        for group, expected in [(True, '群聊自定义'), (False, '私聊自定义')]:
            state = self.mod.ChatStreamState('chat', 'target', is_group=group)
            await self.bot._do_proactive_chat(state, 'target')
            self.assertEqual(self.bot.ctx.maisaka.proactive.trigger.call_args.kwargs['intent'], expected)

    def test_blank_group_does_not_fall_back_to_private_text(self):
        self.bot.config.intent.group_text = '  '
        self.bot.config.intent.private_text = '私聊新设置'
        state = self.mod.ChatStreamState('chat', 'target', is_group=True)
        self.assertEqual(self.bot._intent_for(state), '群聊冷场了，主动找个话题聊聊')
        self.bot.config.intent.private_text = '  '
        default = self.mod.LonelyMaiConfig().intent.private_text
        self.assertEqual(self.bot._intent_for(self.state), default)

    def test_unknown_legacy_text_is_not_kept_in_config(self):
        cfg = self.mod.LonelyMaiConfig.model_validate({'intent': {'text': '旧字段不应被保存'}})
        self.assertNotIn('text', cfg.model_dump()['intent'])
        self.assertFalse(hasattr(cfg.intent, 'text'))

    async def test_equal_numeric_group_and_friend_ids_have_separate_states(self):
        self.bot.config.target.allowed_groups = ['42']
        self.bot.config.target.allowed_friends = ['42']
        self.bot.ctx.chat = types.SimpleNamespace(
            get_stream_by_group_id=AsyncMock(return_value={'stream': {'session_id': 'group-chat'}}),
            get_stream_by_user_id=AsyncMock(return_value={'stream': {'session_id': 'private-chat'}}))
        await self.bot._init_target_streams()
        self.assertEqual(len(self.bot._states), 2)
        group = self.bot._states[(True, '42')]
        private = self.bot._states[(False, '42')]
        self.assertEqual((group.stream_id, group.is_group), ('group-chat', True))
        self.assertEqual((private.stream_id, private.is_group), ('private-chat', False))
        group.last_proactive_time = 123
        private.last_proactive_time = 456
        await self.bot._init_target_streams()
        self.assertEqual(self.bot._states[(True, '42')].last_proactive_time, 123)
        self.assertEqual(self.bot._states[(False, '42')].last_proactive_time, 456)

    async def test_manual_command_uses_matched_stream_kind(self):
        self.bot.config.intent.group_text = '群聊手动'
        self.bot.config.intent.private_text = '私聊手动'
        self.bot._states = {
            (True, '42'): self.mod.ChatStreamState('group-chat', '42', is_group=True),
            (False, '42'): self.mod.ChatStreamState('private-chat', '42')}
        for sid, expected in [('group-chat', '群聊手动'), ('private-chat', '私聊手动')]:
            result = await self.bot.cmd_trigger(stream_id=sid)
            self.assertTrue(result[0])
            self.assertEqual(self.bot.ctx.maisaka.proactive.trigger.call_args.kwargs['intent'], expected)

    async def test_trigger_exception_is_reported_as_failure(self):
        self.bot.ctx.maisaka.proactive.trigger.side_effect = RuntimeError('test')
        self.assertFalse(await self.bot._do_proactive_chat(self.state, 'target'))
