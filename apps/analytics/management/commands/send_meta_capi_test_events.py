"""Meta CAPI 검증용 테스트 이벤트 전송 — 대행사와 실시간으로 맞출 때 쓴다.

왜 명령으로 두나
----------------
Meta [테스트 이벤트] 탭은 **실시간 뷰**다. 전송 후 30초 내에 뜨고, 기록은 24시간만
남는다. 그래서 "지금 보고 있다"는 순간에 맞춰 쏴야 하는데 메신저로 초 단위를 맞추는 건
비현실적이다. → ``--repeat`` 로 일정 간격 반복 전송하면 상대가 그 창 안에 아무 때나
열어도 보인다. 핸드셰이크가 필요 없다.

토큰을 교체할 때마다 재검증이 필요하므로 일회용 스크립트가 아니라 명령으로 남긴다.

⚠️⚠️ **테스트 코드 없이 돌리면 실집계가 오염된다.** ``test_event_code`` 가 붙은 이벤트는
Meta 가 [테스트 이벤트] 탭에만 넣고 리포트·최적화에서 제외한다. 코드가 없으면 **가짜
전환이 실제 광고 성과로 들어간다.** 그래서 코드가 없으면 실행을 거부하고,
``--allow-live`` 를 명시해야만 통과시킨다.

사용법::

    # 대행사가 탭을 열어둔 동안 5분간 20초 간격 (권장)
    python manage.py send_meta_capi_test_events --repeat 15 --interval 20

    # 1회만
    python manage.py send_meta_capi_test_events

    # 설정값 대신 다른 코드로
    python manage.py send_meta_capi_test_events --code TEST12345
"""

from __future__ import annotations

import time

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.analytics import meta_capi

# 대행사가 확인하겠다고 한 3종 (요청서 기준). value 는 검증용 더미값.
_EVENTS = (
    (meta_capi.EVENT_COMPLETE_REGISTRATION, None),
    (meta_capi.EVENT_START_TRIAL, {"currency": "KRW", "value": "0"}),
    (meta_capi.EVENT_PURCHASE, {"currency": "KRW", "value": "9900"}),
)

# 검증용 가짜 사용자 — 실제 회원 데이터를 테스트에 쓰지 않는다.
_VERIFY_EMAIL = "capi-verify@turnflow.link"
_VERIFY_EXTERNAL_ID = "capi-verify"


class Command(BaseCommand):
    help = (
        "Meta [테스트 이벤트] 탭 검증용으로 CompleteRegistration/StartTrial/Purchase 를 전송한다."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--code",
            default="",
            help="test_event_code. 생략하면 settings.META_CAPI_TEST_EVENT_CODE 를 쓴다.",
        )
        parser.add_argument(
            "--repeat",
            type=int,
            default=1,
            help="반복 횟수 (기본 1). 상대가 탭을 여는 창을 넓힌다.",
        )
        parser.add_argument("--interval", type=int, default=20, help="반복 간격 초 (기본 20)")
        parser.add_argument(
            "--allow-live",
            action="store_true",
            help="⚠️ 테스트 코드 없이 전송한다 — **실집계에 가짜 전환이 섞인다**. 거의 쓸 일 없다.",
        )

    def handle(self, *args, **options):
        if not meta_capi.is_enabled():
            raise CommandError(
                "CAPI 가 비활성이다. META_CAPI_ENABLED / ACCESS_TOKEN / DATASET_ID 를 확인할 것."
            )

        code = options["code"] or getattr(settings, "META_CAPI_TEST_EVENT_CODE", "")
        if not code and not options["allow_live"]:
            raise CommandError(
                "test_event_code 가 없다. 코드 없이 보내면 **실집계에 가짜 전환이 들어간다** — "
                "--code 로 지정하거나, 정말 실집계로 보낼 거면 --allow-live 를 붙일 것."
            )

        repeat = max(1, options["repeat"])
        interval = max(1, options["interval"])
        total_seconds = (repeat - 1) * interval

        self.stdout.write(f"데이터세트 : {settings.META_CAPI_DATASET_ID}")
        self.stdout.write(f"테스트 코드: {code or '(없음 — 실집계로 나간다!)'}")
        self.stdout.write(
            f"전송 계획  : {repeat}회 × {len(_EVENTS)}종, {interval}초 간격 "
            f"(총 {total_seconds // 60}분 {total_seconds % 60}초)"
        )
        self.stdout.write("→ Meta [테스트 이벤트] 탭은 전송 후 30초 내에 표시된다.\n")

        ok_count = 0
        fail_count = 0
        for round_no in range(1, repeat + 1):
            now = int(timezone.now().timestamp())
            stamp = timezone.localtime().strftime("%H:%M:%S")
            user_data = meta_capi.build_user_data(
                email=_VERIFY_EMAIL,
                external_id=_VERIFY_EXTERNAL_ID,
                client_ip="1.1.1.1",
                client_user_agent="TurnflowCAPIVerify/1.0",
            )
            line = [f"[{stamp}] {round_no}/{repeat}"]
            for name, custom in _EVENTS:
                event = meta_capi.build_event(
                    event_name=name,
                    # 라운드마다 유니크 — 같은 id 를 반복하면 Meta 가 중복으로 합쳐
                    # 두 번째 이후가 화면에 안 뜬다.
                    event_id=f"verify-{name}-{now}-{round_no}",
                    event_time=now,
                    user_data=user_data,
                    event_source_url="https://turnflow.link/",
                    custom_data=custom,
                )
                result = meta_capi.send_events([event], test_event_code=code)
                if result["ok"]:
                    ok_count += 1
                    line.append(f"{name}=OK")
                else:
                    fail_count += 1
                    line.append(f"{name}=실패({result['status']} {result['body']})")
            self.stdout.write("  " + " · ".join(line))

            if round_no < repeat:
                time.sleep(interval)

        self.stdout.write("")
        if fail_count:
            self.stdout.write(self.style.ERROR(f"성공 {ok_count} / 실패 {fail_count}"))
        else:
            self.stdout.write(self.style.SUCCESS(f"전부 성공 ({ok_count}건)"))
            self.stdout.write(
                "Meta 이벤트 관리자 → 데이터세트 → [테스트 이벤트] 탭에서 "
                "'서버(Server)' 출처로 확인할 수 있다."
            )
