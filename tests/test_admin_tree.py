"""슬래시 명령과 자동완성의 관리자 권한 검사."""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

try:
    import discord
    import dotenv

    with patch.object(dotenv, "load_dotenv"):
        from bot import main
except ModuleNotFoundError as exc:
    if exc.name not in {"discord", "dotenv", "ollama", "yaml", "aiosqlite"}:
        raise
    raise unittest.SkipTest(f"의존성 없음: {exc.name}") from exc


class AdminTreeTests(unittest.IsolatedAsyncioTestCase):
    async def test_non_admin_stops_before_dispatch(self):
        for kind in (discord.InteractionType.autocomplete, discord.InteractionType.application_command):
            with self.subTest(kind=kind):
                response = Mock(autocomplete=AsyncMock(), send_message=AsyncMock())
                interaction = SimpleNamespace(type=kind, user=SimpleNamespace(id=1), response=response)
                hub = Mock(ensure=AsyncMock())
                with patch.object(main, "ADMIN_IDS", {2}), patch.object(main, "hub", hub):
                    await main.bot.tree._call(interaction)

                if kind is discord.InteractionType.autocomplete:
                    response.autocomplete.assert_awaited_once_with([])
                    response.send_message.assert_not_awaited()
                else:
                    response.send_message.assert_awaited_once_with("관리자만 사용할 수 있어요.", ephemeral=True)
                    response.autocomplete.assert_not_awaited()
                hub.ensure.assert_not_awaited()
                hub.sync_peers.assert_not_called()

    async def test_admin_can_use_both_interaction_types(self):
        for kind in (discord.InteractionType.autocomplete, discord.InteractionType.application_command):
            with self.subTest(kind=kind):
                interaction = SimpleNamespace(type=kind, user=SimpleNamespace(id=2), guild_id=3,
                                              guild=SimpleNamespace(name="테스트 서버"), response=Mock())
                hub = Mock(ensure=AsyncMock())
                with patch.object(main, "ADMIN_IDS", {2}), patch.object(main, "hub", hub):
                    self.assertTrue(await main.bot.tree.interaction_check(interaction))

                hub.ensure.assert_awaited_once_with(3, "테스트 서버")
                hub.use.assert_called_once_with(3)
                self.assertEqual(interaction.response.mock_calls, [])
