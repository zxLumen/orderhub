import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import config
from app.collector_agent import Agent


class AgentPlatformRoutingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_data = config.DATA_DIR
        config.DATA_DIR = Path(self.tmp.name)
        self.agent = Agent()

    def tearDown(self):
        config.DATA_DIR = self.old_data
        self.tmp.cleanup()

    def test_web_platform_routes_to_collect_web(self):
        with patch("app.collector_agent.collect_web", return_value={"ok": True, "items": 3}) as web, \
             patch("app.collector_agent.collect_jd") as jd, \
             patch.object(self.agent, "sync") as sync, \
             patch.object(self.agent, "_save_state"):
            result = self.agent.collect("recent", platform="pdd")
        self.assertTrue(result["ok"])
        web.assert_called_once_with("pdd", "recent")
        jd.assert_not_called()
        sync.assert_called_once()

    def test_jd_still_uses_dedicated_collector(self):
        with patch("app.collector_agent.collect_web") as web, \
             patch("app.collector_agent.collect_jd", return_value={"ok": True, "items": 1}) as jd, \
             patch.object(self.agent, "sync"), \
             patch.object(self.agent, "_save_state"):
            self.agent.collect("recent", platform="jd")
        jd.assert_called_once_with("recent")
        web.assert_not_called()

    def test_unsupported_platform_does_not_collect(self):
        with patch("app.collector_agent.collect_web") as web, \
             patch("app.collector_agent.collect_jd") as jd, \
             patch.object(self.agent, "sync"), \
             patch.object(self.agent, "_save_state"):
            result = self.agent.collect("recent", platform="meituan")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "unsupported_platform")
        web.assert_not_called()
        jd.assert_not_called()

    def test_login_required_opens_login_tab(self):
        with patch("app.collector_agent.collect_web", return_value={"ok": False, "error": "login_required"}), \
             patch.object(self.agent, "sync"), \
             patch.object(self.agent, "_save_state"), \
             patch.object(self.agent, "_ensure_platform_login") as ensure:
            self.agent.collect("recent", platform="taobao")
        ensure.assert_called_once_with("taobao", "login_required")

    def test_poll_command_uses_platform(self):
        command = {"id": 7, "platform": "xianyu", "mode": "recent"}
        with patch.object(self.agent, "_request", return_value={"command": command}), \
             patch.object(self.agent, "collect") as collect:
            claimed = self.agent.poll_command()
        self.assertTrue(claimed)
        collect.assert_called_once_with("recent", command_id=7, platform="xianyu")


if __name__ == "__main__":
    unittest.main()
