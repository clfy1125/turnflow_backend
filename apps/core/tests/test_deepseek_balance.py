"""DeepSeek 잔액 감시 테스트.

이 감시의 값어치는 "울려야 할 때만 우는 것"에 있다. 그래서 검증하는 것은 잔액 계산이
아니라 **발신 규칙**이다 — 조회 실패로 가짜 🔴 가 나가지 않는지, 충전 전까지 매 주기
울어대지 않는지, 경보 없이 지나간 건 조용히 끝나는지.
"""

from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase

from apps.core import tasks as core_tasks


def _resp(balance, available=True, status=200):
    class R:
        ok = status == 200
        status_code = status

        @staticmethod
        def json():
            return {
                "is_available": available,
                "balance_infos": [
                    {"currency": "USD", "total_balance": str(balance), "granted_balance": "0.00"}
                ],
            }

    return R()


class DeepSeekBalanceTests(TestCase):
    def setUp(self):
        # 이 프로젝트의 테스트 DB/캐시는 dev 와 공유될 수 있다 → 우리 키만 확실히 비운다.
        cache.delete(core_tasks._ALERT_STATE_KEY)
        cache.delete(core_tasks._SAMPLE_KEY)
        self.settings_ctx = self.settings(
            DEEPSEEK_API_KEY="sk-test",
            DEEPSEEK_BALANCE_WARN_USD=10.0,
            DEEPSEEK_BALANCE_CRIT_USD=3.0,
            DEEPSEEK_BALANCE_REPEAT_HOURS=24,
        )
        self.settings_ctx.enable()
        self.addCleanup(self.settings_ctx.disable)

    def _run(self, balance, available=True, status=200):
        with (
            patch.object(
                core_tasks.requests, "get", return_value=_resp(balance, available, status)
            ),
            patch.object(core_tasks, "send_telegram_notification", return_value=True) as tg,
        ):
            result = core_tasks.check_deepseek_balance()
        return result, [c.args[0] for c in tg.call_args_list]

    def test_충분하면_조용하다(self):
        result, sent = self._run(49.98)
        self.assertEqual(result["level"], "ok")
        self.assertEqual(sent, [])

    def test_경고_임계_이하면_1회만_운다(self):
        _, first = self._run(8.0)
        self.assertEqual(len(first), 1)
        self.assertIn("🟡", first[0])
        # 같은 등급이 이어져도 반복 발신하지 않는다 (충전 전까지 3시간마다 울면 경보 피로)
        _, second = self._run(7.5)
        self.assertEqual(second, [])

    def test_등급이_나빠지면_즉시_다시_운다(self):
        self._run(8.0)  # warn
        _, sent = self._run(2.0)  # crit
        self.assertEqual(len(sent), 1)
        self.assertIn("🔴", sent[0])

    def test_잔액0_소진은_금액과_무관하게_긴급(self):
        # is_available=False = 호출이 이미 402 로 거절되는 상태
        result, sent = self._run(0.0, available=False)
        self.assertEqual(result["level"], "crit")
        self.assertIn("402", sent[0])

    def test_조회_실패는_경보하지_않는다(self):
        # 네트워크/HTTP 오류로 가짜 🔴 가 나가면 진짜 경보를 무시하게 된다
        result, sent = self._run(0, status=500)
        self.assertFalse(result["ok"])
        self.assertEqual(sent, [])

    def test_경보났던_건만_회복을_알린다(self):
        self._run(2.0)  # crit 경보
        _, sent = self._run(50.0)  # 충전
        self.assertEqual(len(sent), 1)
        self.assertIn("🟢", sent[0])
        # 회복 이후엔 다시 조용
        _, again = self._run(50.0)
        self.assertEqual(again, [])

    def test_경보없이_지나간_건_회복도_조용하다(self):
        self._run(49.0)  # 계속 ok
        _, sent = self._run(48.0)
        self.assertEqual(sent, [])

    def test_키_없으면_noop(self):
        with self.settings(DEEPSEEK_API_KEY=""):
            with patch.object(core_tasks, "send_telegram_notification") as tg:
                result = core_tasks.check_deepseek_balance()
        self.assertFalse(result["ok"])
        tg.assert_not_called()
