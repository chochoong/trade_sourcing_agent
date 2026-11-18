"""화면에 나가는 오류 문구에 Errno·영어 원문·예외 이름이 그대로 섞이지 않는지."""
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import anthropic
from trade_hub import db

from sourcing_agent import hub
from sourcing_agent.errors import SourcingError, friendly
from sourcing_agent.llm import LLMResponseError


def _api_error(cls, code=None, message="error"):
    """SDK 버전마다 생성자가 달라서, 생성자를 거치지 않고 friendly() 가 읽는 속성만 채움."""
    e = cls.__new__(cls)
    Exception.__init__(e, message)
    e.message, e.body, e.status_code = message, {"error": {"message": message}}, code
    return e


_status = _api_error


class FriendlyTest(unittest.TestCase):
    def check(self, e, *must):
        msg = friendly(e)
        self.assertNotIn("Errno", msg)
        self.assertNotIn("WinError", msg)
        for m in must:
            self.assertIn(m, msg)
        return msg

    def test_files(self):
        self.check(FileNotFoundError(2, "No such file or directory", "C:/x/results.json"),
                   "파일을 찾지 못했습니다", "results.json")
        self.check(PermissionError(13, "Permission denied", "C:/x/suppliers.csv"), "열어 두었다면", "suppliers.csv")
        self.check(OSError(28, "No space left on device"), "실패했습니다")

    def test_api(self):
        self.check(_api_error(anthropic.APIConnectionError, message="Connection error."), "인터넷 연결")
        self.check(_status(anthropic.AuthenticationError, 401, "invalid x-api-key"), "ANTHROPIC_API_KEY")
        self.check(_status(anthropic.BadRequestError, 400, "Your credit balance is too low"), "크레딧")
        self.check(_status(anthropic.RateLimitError, 429, "rate limited"), "한도")
        self.check(_status(anthropic.InternalServerError, 529, "Overloaded"), "혼잡")

    def test_misc(self):
        msg = self.check(LLMResponseError("JSON 파싱 실패: x\n--- 원문 ---\n" + "가" * 3000), "AI 응답")
        self.assertNotIn("원문", msg)
        self.check(sqlite3.OperationalError("database is locked"), "다른 작업")
        self.assertEqual(friendly(ValueError("구매 방식이 retail 이 아님")), "구매 방식이 retail 이 아님")


class FromDbFailureTest(unittest.TestCase):
    """from-db 실행이 실패하면 공용 DB(agent_runs.error)·대시보드에 한국어 문구가 남는다."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.env = mock.patch.dict(os.environ, {"TRADE_DB": str(Path(self.tmp.name) / "t.db"),
                                                "ANTHROPIC_API_KEY": "test"})
        self.env.start()
        conn = db.connect()
        with conn:
            db.upsert_product(conn, "AI안경", stage="sourcing")
        conn.close()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_error_saved_readable(self):
        boom = FileNotFoundError(2, "No such file or directory", "prompts/discover.md")
        with mock.patch.object(hub, "run_discover", side_effect=boom):
            with self.assertRaises(SourcingError):
                hub.run_from_db(log=lambda *_: None)
        conn = db.connect()
        err = conn.execute("SELECT error FROM agent_runs ORDER BY id DESC LIMIT 1").fetchone()[0]
        conn.close()
        self.assertIn("파일을 찾지 못했습니다", err)
        self.assertNotIn("Errno", err)


if __name__ == "__main__":
    unittest.main()
