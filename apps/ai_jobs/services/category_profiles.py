"""카테고리별 '진짜 한국에서 쓰는' 페이지 레시피 지식베이스.

생성 모델(deepseek)은 컨셉만 주면 ① 블록이 빈약하고 ② 카테고리에 안 맞는 일반적
구성을 만들며 ③ 매번 같은 cover_bg 히어로만 쓴다. 이 모듈은 카테고리별로
- 어떤 섹션이 들어가야 하는지(한국 실사용 기준: 네이버 예약/카카오톡 채널/스마트스토어 등)
- 어떤 톤·이모지로 카피를 써야 하는지
- 히어로를 cover 로 할지 avatar 로 할지(다양성)
- 이미지 키워드 풀(빈 이미지 슬롯 폴백)
를 정의해 프롬프트에 주입하고, 이미지/텍스트 가드가 참조하게 한다.

리서치 출처 요약(2025-2026 한국 관행): 리틀리/인포크링크 프로필, 네이버 예약·스마트플레이스,
카카오톡 채널·오픈채팅, 스마트스토어·네이버폼 공구, 스페이스클라우드 공간대여,
salondeletter 모바일 청첩장(인사말이 길어도 되는 유일한 예외), 쿠팡 파트너스 제휴.
"""

from __future__ import annotations

# ── 카테고리 키(슬러그) ──────────────────────────────────────
PROFILE = "profile"
BIZCARD = "bizcard"
LANDING = "landing"
PORTFOLIO = "portfolio"
BROCHURE = "brochure"
RENTAL = "rental"
GROUPBUY = "groupbuy"
INVITATION = "invitation"
AFFILIATE = "affiliate"
COMMISSION = "commission"
PROMO = "promo"
GENERIC = "generic"


# ── 카테고리 프로필 정의 ─────────────────────────────────────
# hero: "cover" | "avatar"  — 프로필 히어로 전략(다양성의 핵심)
# long_text: 인사말 등 긴 문단을 허용하는 유일 카테고리는 invitation 뿐.
CATEGORY_PROFILES: dict[str, dict] = {
    PROFILE: {
        "label": "프로필 링크 (인플루언서·크리에이터)",
        "hero": "avatar",
        "min_blocks": 14,
        "long_text": False,
        "mood": "친근·트렌디. 파스텔/뉴트럴(크림·베이지·소프트핑크·세이지) 또는 다크+포인트 1색.",
        "sections": [
            "프로필 헤더(center/left 아바타 + 닉네임 + 한 줄 소개)",
            "SNS 아이콘 줄(인스타/유튜브/틱톡)",
            "고정 공지 배너(진행 중 이벤트/공구)",
            "검색 블록(search — 콘텐츠가 많은 페이지라 필수, 공지 바로 아래)",
            "대표 CTA 1개(가장 밀고 싶은 링크 — small)",
            "인기 콘텐츠 갤러리(gallery thumbnail — 6장 이상)",
            "**콘텐츠 카테고리 묶음 — folder(toggle) 사용**: '🎬 콘텐츠 모아보기' folder 안에 "
            "임시 id 를 단 single_link 들을 child_block_ids 로 묶어라(공통 규칙 12 예시 참조). "
            "묶을 게 2~3개뿐이면 group_link list 로 대체 가능",
            "추천템/공구 링크(small 여러 개)",
            "협업·협찬 문의 폼(customer/inquiry — small)",
        ],
        "services": [
            "유튜브·인스타·틱톡",
            "협업 문의(이메일/폼)",
            "스마트스토어·공구 링크",
            "카카오톡 채널/오픈채팅",
        ],
        "copy": [
            "📌 이번 주 공구 오픈했어요!",
            "🎬 최근 인기 영상 모아보기",
            "💌 협업·협찬 문의는 여기로",
            "🔔 새 소식 알림 신청",
        ],
        "hero_keywords": [
            "young woman smiling portrait natural light",
            "content creator portrait camera studio",
            "woman portrait soft daylight",
        ],
        "gallery_keywords": [
            "vlog daily life korea",
            "lifestyle flat lay aesthetic",
            "cafe travel moment",
            "youtube creator desk setup",
        ],
        "thumb_keywords": ["youtube thumbnail lifestyle", "instagram lifestyle photo"],
    },
    BIZCARD: {
        "label": "디지털 명함 (프리랜서·전문가)",
        "hero": "avatar",
        "min_blocks": 16,
        "long_text": False,
        "mood": "미니멀·신뢰감. 화이트/차콜/네이비 + 단일 포인트색. 정돈된 산세리프.",
        "sections": [
            "헤더(center 아바타 + 이름·직함 + 한 줄 태그라인)",
            "한 줄 신뢰 요소(경력/대표 클라이언트)",
            "대표 CTA: 상담 문의(카카오톡 채널 — **medium 스탠다드**: 비즈니스 직결 소통 창구라 "
            "한 줄 컴팩트로 묻히면 안 되고 쇼케이스는 과함)",
            "포트폴리오/이력서 링크(Notion·Behance·PDF — small 여러 개)",
            "제공 서비스 group_link(list)",
            "작업 사례 간단 갤러리(gallery, 있으면)",
            "클라이언트 추천사(text toggle 1개 — 아이디/회사 ★ 한줄평 3~4개)",
            "연락 수단 + SNS(social: 이메일/전화/링크드인)",
        ],
        "services": [
            "카카오톡 채널/오픈채팅 상담",
            "Notion 이력서·Behance 포트폴리오",
            "이메일·전화",
            "링크드인",
            "PDF 이력서 다운로드",
        ],
        "copy": [
            "💼 5년차 브랜드 디자이너입니다",
            "📩 프로젝트 문의는 카톡으로 편하게",
            "🎨 포트폴리오 보러 가기",
            "☕ 커피챗부터 편하게 연락 주세요",
        ],
        "hero_keywords": [
            "professional portrait business casual headshot",
            "designer working laptop office portrait",
        ],
        "gallery_keywords": ["minimal workspace desk", "design portfolio mockup"],
        "thumb_keywords": ["notion portfolio page", "behance project cover", "resume document"],
    },
    LANDING: {
        "label": "앱·제품 출시 랜딩페이지",
        "hero": "cover",
        "min_blocks": 15,
        "long_text": False,
        "mood": "모던·세련된 단색 포인트(딥블루/인디고/민트). 화이트 여백, 큰 타이포. **올드한 스톡사진·촌스러운 그래디언트 금지**.",
        "sections": [
            "히어로(cover_bg + 한 줄 핵심 카피)",
            "핵심 기능·혜택 3가지(group_link grid-3, 항목마다 thumbnail_url 필수)",
            "앱 화면 미리보기(gallery thumbnail: UI 목업 캡처)",
            "사용 시나리오(text 짧게: 문제→해결)",
            "대표 CTA(무료체험/사전예약 — **medium 스탠다드** + badge: 핵심 전환 버튼)",
            "실사용 후기(text toggle 1개 — 아이디 ★ 한줄평 5~6개) — **가짜 '만족도 100%'·억지 별점 금지**",
            "앱스토어/구글플레이 링크(group_link grid-2 또는 small 2개)",
            "FAQ(text toggle)",
            "문의(카카오톡 채널 — small)",
        ],
        "services": [
            "App Store·Google Play 배지",
            "무료체험/사전예약 폼",
            "카카오톡 채널",
            "쿠폰코드",
        ],
        "copy": [
            "🚀 지금 무료로 시작하세요",
            "⏰ 사전예약 D-3 · 첫 달 50% 할인",
            "⭐ 이미 3만 명이 사용 중",
            "📲 앱스토어 · 구글플레이 동시 출시",
        ],
        "hero_keywords": [
            "person using smartphone app happy",
            "hand holding smartphone screen",
            "smartphone mockup minimal desk",
        ],
        "gallery_keywords": ["smartphone app screen hand", "mobile phone interface closeup"],
        "thumb_keywords": [
            "app feature icon flat",
            "smartphone app screen",
            "five star rating review",
        ],
    },
    PORTFOLIO: {
        "label": "포트폴리오 (사진작가·디자이너)",
        "hero": "cover",
        "min_blocks": 15,
        "long_text": False,
        "mood": "이미지가 주인공. 여백 많은 미니멀 모노톤(화이트/블랙/그레이) + 작은 포인트.",
        "sections": [
            "커버 히어로(cover_bg 대표작) + 한 줄 소개",
            "**인스타그램 등 SNS(social) — 사진작가/디자이너는 필수**(맨 위쪽에)",
            "대표작 갤러리(gallery thumbnail — 6장 이상, 작품이 많아야 신뢰)",
            "분야별 시리즈 group_link(grid-2, 썸네일: 인물/제품/웨딩/브랜딩). "
            "**라벨-블록 일치**: '📂 분야별 작업 보기' 같은 라벨을 붙였으면 바로 그 자리에 그 "
            "내용의 group_link(또는 folder)가 와야 한다 — 라벨만 폴더처럼 쓰고 일반 링크를 "
            "나열하면 블록 이해도가 없어 보인다",
            "추가 작업 갤러리(gallery thumbnail — 작품 더, 6장+)",
            "촬영/작업 안내(요금 요약 — text 짧게, 표처럼)",
            "예약·문의 CTA(네이버 예약/카카오톡 채널 — small)",
            "인스타 피드로 유도하는 small 링크('📷 인스타그램 @핸들에서 더 보기')",
            "후기(text toggle 1개 — 아이디 ★ 한줄평 5~6개)",
            "About 약력(text plain, **2~3문장으로 아주 짧게** — 긴 자기소개 금지)",
        ],
        "services": [
            "네이버 예약(촬영 예약)",
            "카카오톡 채널 상담",
            "인스타그램·Behance",
            "촬영 문의 폼",
        ],
        "copy": [
            "✨ 당신의 가장 좋은 순간을 담아요",
            "📷 촬영 문의 / 예약 바로가기",
            "🗂 작업물 전체 보기",
            "💬 상담 후 예약 진행됩니다",
        ],
        "hero_keywords": [
            "professional photography portfolio hero",
            "portrait photography studio light",
        ],
        "gallery_keywords": [
            "portrait photography fine art",
            "product photography minimal",
            "editorial fashion photo",
            "film photography moody",
        ],
        "thumb_keywords": ["photography category cover", "wedding portrait shoot"],
    },
    BROCHURE: {
        "label": "브랜드·제품 브로슈어",
        "hero": "cover",
        "min_blocks": 15,
        "long_text": False,
        "mood": "브랜드 1차색 + 뉴트럴 베이스(아이보리/그레이/모카). 따뜻하고 정돈된 톤.",
        "sections": [
            "대표 비주얼 히어로(cover_bg, 사용 장면)",
            "브랜드 한 줄 슬로건(text)",
            "제품 라인업(group_link grid-2, 썸네일+가격)",
            "제품 상세 갤러리(gallery thumbnail)",
            "핵심 혜택/특징(text 짧게 또는 toggle)",
            "브랜드 스토리(text plain, 짧게)",
            "구매처 CTA(스마트스토어/자사몰 — **medium 스탠다드** + badge: 핵심 전환 버튼)",
            "후기(text toggle 1개 — 아이디 ★ 한줄평 5~6개, 1~2개만 넣지 말 것)",
            "도매/문의(customer/inquiry — small), 위치(map)",
        ],
        "services": [
            "스마트스토어·쿠팡·자사몰 구매",
            "카카오톡 채널 상담",
            "카탈로그 PDF",
            "네이버지도",
        ],
        "copy": [
            "🌿 매일 쓰는 것일수록, 제대로",
            "📦 오늘 주문 시 내일 도착",
            "🍓 누적 판매 5만 개 · 재구매율 38%",
            "🛒 스마트스토어에서 구매하기",
        ],
        "hero_keywords": [
            "organic product lifestyle flat lay",
            "artisan food brand photography warm",
        ],
        "gallery_keywords": ["product still life natural light", "brand lifestyle scene"],
        "thumb_keywords": [
            "jam jar product photo",
            "organic food product white background",
            "handmade product detail",
        ],
    },
    RENTAL: {
        "label": "공간 대여·예약",
        "hero": "cover",
        "min_blocks": 15,
        "long_text": False,
        "mood": "클린+웜. 뉴트럴/베이지/우드 톤, 여백 많이, 포인트 1색. 실제 공간 사진 중심.",
        "sections": [
            "공간 대표사진 히어로(cover_bg)",
            "한 줄 소개(예: '성수동 4인 감성 파티룸')",
            "공간 사진 갤러리(gallery thumbnail — 6장+: 낮/밤, 빔프로젝터, 주방, 디테일)",
            "시간당 요금(text **plain — 토글 금지**: 요금은 가장 중요한 정보라 숨기면 안 된다. "
            "'⏰ 평일 15,000원/시간' 처럼 이모지+줄 단위로 또렷하게)",
            "예약 CTA(네이버 예약 — **medium 스탠다드**: 핵심 전환 버튼)",
            "편의시설(text 불릿 — **링크 블록 금지**: 클릭할 곳이 없는 정보를 single_link 로 "
            "만들면 이상하다. '📶 와이파이 · 🖥 빔프로젝터 · 🍳 간이주방' 식 이모지 불릿 텍스트로)",
            "이용안내(text toggle, 짧은 불릿)",
            "주차 안내(text toggle — **바로 위 이용안내와 같은 포맷**으로 나란히: 형식 통일)",
            "위치(map)",
            "후기(text toggle 1개 — 아이디 ★ 한줄평 5~6개)",
            "문의(카카오톡 채널/전화 — small)",
        ],
        "services": [
            "네이버 예약·스마트플레이스",
            "스페이스클라우드·아워플레이스",
            "카카오맵/네이버지도 길찾기",
            "카카오톡 채널·전화",
        ],
        "copy": [
            "⏰ 시간당 15,000원 · 최소 2시간",
            "🅿️ 건물 내 주차 2시간 무료",
            "📅 네이버 예약 바로가기",
            "🎉 4인 기준 · 인원 추가 시 1인 5,000원",
        ],
        "hero_keywords": [
            "modern cozy party room interior",
            "stylish studio space interior daylight",
        ],
        "gallery_keywords": [
            "interior lounge space design",
            "meeting room cozy interior",
            "studio rental space",
        ],
        "thumb_keywords": ["interior amenities detail", "cozy room corner decor"],
    },
    GROUPBUY: {
        "label": "공동구매",
        "hero": "cover",
        "min_blocks": 16,
        "long_text": False,
        "mood": "활력·긴급(FOMO). 고대비 포인트(레드/오렌지) + 큰 숫자(할인율·D-day).",
        "sections": [
            "상품 대표사진 히어로(cover_bg) + 공구 타이틀",
            "할인 강조 배너(notice: 정가→공구가, 할인율)",
            '**공구 일정 — schedule 블록 필수**(`_type:"schedule"`, `schedule_layout:"list"`): '
            "OPEN/마감/배송을 schedule_items 항목으로(각각 title·start_date·end_date, 컨셉의 실제 "
            "날짜만 — D-day 뱃지가 자동으로 붙는다). calendar 레이아웃 금지",
            "주문 CTA(네이버폼/스마트스토어 — **medium 스탠다드**: 핵심 전환 버튼)",
            "**코너별 진열**: '🔥 이번 공구 BEST' 같은 이모지 섹션 헤더(text default) 아래 "
            "대표 상품 single_link medium 2~3개(썸네일+가격+취소선) → spacer 구분선 → 다음 코너. "
            "옵션이 많으면 group_link grid-2(전 항목 썸네일+가격)로 묶어도 좋다",
            "상품 상세 설명(text 짧게 + gallery 디테일 사진 여러 장)",
            "주문 방법 ①폼 ②입금 ③배송(text 짧게, 번호 스텝)",
            "배송/입금 안내(text)",
            "후기(text toggle 1개 — 아이디 ★ 한줄평 5~6개)",
            "문의(카카오톡 채널 — small)",
        ],
        "services": [
            "네이버폼 주문서·스마트스토어",
            "카카오톡 채널/오픈채팅",
            "계좌 입금·카카오페이 송금",
            "인스타 DM",
        ],
        "copy": [
            "🔥 정가 39,000원 → 공구가 24,900원 (36%)",
            "⏰ 마감 D-2 · 일요일 밤 마감",
            "💬 주문은 아래 폼으로",
            "⭐ 재구매율 92% · 후기 폭발",
        ],
        "hero_keywords": [
            "baby eating with spoon high chair",
            "toddler meal table bright kitchen",
            "product flat lay bright table",
        ],
        "gallery_keywords": [
            "baby food bowl spoon table",
            "toddler eating healthy meal",
            "kitchen tableware set bright",
        ],
        "thumb_keywords": [
            "baby bowl spoon tableware",
            "toddler plate food colorful",
            "kitchen product photo bright",
        ],
    },
    INVITATION: {
        "label": "모바일 초대장 (청첩장·돌잔치)",
        "hero": "cover",
        "min_blocks": 18,  # 실제 청첩장(salondeletter)은 매우 길다 — 절대 빈약하면 안 됨
        "long_text": True,  # ⭐ 인사말/글귀에 한해 따뜻한 긴 문단 허용 (유일 예외)
        # 검증된 실제 청첩장 페이지(@wedding)를 DB 레퍼런스 few-shot 으로 주입 — 사용자가
        # "이 디자인을 그대로 학습하라"고 지정한 기준 디자인(아이보리+골드, 명조, 시구→인사말
        # →혼주→갤러리→오시는길→주차/식사→계좌→RSVP→방명록→맺음말 흐름).
        "reference_slug": "wedding",
        "mood": "소프트·파스텔·우아. 아이보리/베이지/연핑크/세이지, 명조(Nanum Myeongjo), 여백.",
        "sections": [
            "메인 사진 히어로(cover_bg) + 이름·날짜",
            "글귀/인용(text plain, center — 짧은 시구)",
            "인사말(text plain, center — 따뜻한 문단 OK, 두 사람 이야기)",
            "예식 일시·장소(text, 날짜/시간/홀 또렷이)",
            "양가 혼주 안내(text: 신랑측 OOO·OOO의 아들 / 신부측 OOO·OOO의 딸)",
            "두 사람 사진 갤러리(gallery thumbnail — 6장 이상, 넉넉히)",
            "D-day(text: '🗓 2026년 10월 10일 · D-130' 처럼 예식일을 글로. schedule 블록은 날짜가 "
            "엉뚱한 달로 표시되는 버그가 있으니 쓰지 말 것)",
            "오시는 길(map) + 교통 안내(text: 🚇지하철 / 🚌버스 / 🚘자가용)",
            "주차 안내(text 짧게)",
            "식사 안내(text 짧게: 뷔페/위치/시간)",
            "마음 전하실 곳(group_link list: 신랑측 계좌 / 신부측 계좌 — 계좌번호)",
            "참석 여부 RSVP(single_link small '💌 참석 여부 알려주기' → 네이버폼/카카오 링크. "
            "customer/inquiry 폼은 입력칸 라벨이 영어로 떠 어색하니 쓰지 말 것)",
            "방명록 안내(single_link small → 외부 방명록/카카오 링크)",
            "맺음말(text plain, center — 짧게)",
        ],
        "services": [
            "카카오맵/네이버지도 길찾기",
            "계좌번호(마음 전하실 곳)",
            "참석여부 RSVP 폼",
            "카카오톡 공유",
        ],
        "copy": [
            "🤍 저희 두 사람, 결혼합니다",
            "📍 오시는 길",
            "💝 마음 전하실 곳",
            "💌 참석 여부 전달",
        ],
        "hero_keywords": [
            "wedding couple soft pastel photography",
            "elegant flowers wedding aesthetic",
        ],
        "gallery_keywords": [
            "wedding couple photo natural",
            "couple lifestyle photography soft",
            "wedding details flowers ring",
        ],
        "thumb_keywords": ["wedding flower detail", "soft pastel texture"],
    },
    AFFILIATE: {
        "label": "제휴 마케팅 (추천템 모음)",
        "hero": "avatar",
        "min_blocks": 16,
        "long_text": False,
        "mood": "깔끔한 1색 + 화이트(리틀리 기본). 상품 썸네일이 주인공, UI 중립·미니멀.",
        "sections": [
            "프로필 헤더(center 아바타 + 한 줄 소개) + SNS(social)",
            "**제휴 고지 — 맨 앞(상단) 배치**(text plain 작게: '이 페이지는 쿠팡 파트너스 활동의 "
            "일환으로, 구매 시 일정액의 수수료를 제공받습니다.' — 수익 고지는 법적 투명성 요소라 "
            "하단에 묻지 말고 프로필 바로 아래에)",
            "**할인 쿠폰코드 — 앞쪽 강조**(notice 또는 text: 'SOSO10' 같은 코드는 구매 전환의 "
            "핵심 정보라 buttonColor 강조로 크게)",
            "이번 주 추천 배너(notice: '오늘의 픽')",
            "추천 상품을 **작은 링크 여러 개로**: group_link `list`/`grid-2` — **모든 항목 "
            "thumbnail_url 필수**(상품 사진이 보여야 구매한다). single_link `small`(썸네일 포함) 다수. "
            "구매 사이트는 작은 링크를 많이 보여주는 게 정석 — 큰 카드 남발 금지.",
            "카테고리 구분(스킨케어/메이크업/헤어) — 코너가 3개 이상이니 **각 코너를 "
            "folder(toggle)** 로 묶어라(하위 상품 블록들에 임시 id → child_block_ids 참조, "
            "공통 규칙 12 예시). folder 라벨 예: '🧴 스킨케어 추천 모아보기'",
            "이달의 강조 1개(single_link large + thumbnail — **딱 하나만**, 이벤트/특가 상품)",
            "내돈내산 후기(text toggle 1개 — 아이디 ★ 한줄평 5~6개)",
        ],
        "services": [
            "쿠팡 파트너스·스마트스토어",
            "올리브영·무신사·에이블리 제휴 링크",
            "인스타 DM(자동DM)",
            "쿠폰코드",
        ],
        "copy": [
            "🛒 이번 주 내돈내산 추천템",
            "👇 프로필 링크에서 바로 구매",
            "🔥 공구 D-2 · 역대급 최저가",
            "💬 쿠폰코드는 아래에서 확인",
        ],
        "hero_keywords": [
            "beauty woman portrait soft light",
            "woman applying skincare face portrait",
        ],
        "gallery_keywords": ["beauty product flat lay", "skincare cosmetics aesthetic"],
        "thumb_keywords": [
            "cosmetic product white background",
            "skincare bottle product",
            "makeup product photo",
            "hair product bottle",
        ],
    },
    COMMISSION: {
        "label": "커미션·재능 판매 (일러스트)",
        "hero": "avatar",  # 일러스트는 스톡 사진이 안 맞음 → 작가 아바타 중심, 작품은 외부 링크로
        "min_blocks": 14,
        "long_text": True,  # 주의사항/환불 규정은 길어도 '프로처럼' 보임
        "mood": "작가 그림체 컬러풀·아기자기(파스텔/키치). 단, 가격/주의 영역은 정돈된 박스.",
        "sections": [
            "헤더(center 아바타 + 작가명 + 한 줄 소개) + SNS(트위터/인스타)",
            "오픈 상태 배너(notice: 'OPEN / 선착순 N명')",
            "작업 예시 갤러리(gallery thumbnail 4~6장 — **'digital illustration art'/'anime style "
            "character art' 류 일러스트 키워드만**, 실사 인물/풍경 사진 금지: 일러스트 작가 페이지에 "
            "실사 스톡사진이 섞이면 신뢰가 깨진다)",
            "**작품 더 보러가기**: 인스타/포스타입/그라폴리오 외부 링크(single_link small 여러 개)",
            "타입별 가격표(group_link list 또는 text: 아이콘/반신/전신/SD + 배경 추가) — **썸네일 없이 텍스트로**",
            "신청 방법·CTA(폼/포스타입, single_link small)",
            "이용 안내·주의사항(text toggle — 길어도 OK)",
            "환불 규정(text toggle)",
            "문의(트위터/인스타 DM — small)",
        ],
        "services": [
            "포스타입(리퀘스트)·그라폴리오",
            "구글폼/네이버폼 신청서",
            "트위터(X)·인스타 DM·오픈채팅",
            "계좌이체·카카오페이",
        ],
        "copy": [
            "🎨 커미션 OPEN! 선착순 5명",
            "📋 신청 양식 작성 후 폼 제출",
            "⚠️ 채색 시작 후에는 환불이 어려워요",
            "💸 입금 확인 후 작업 시작됩니다",
        ],
        "hero_keywords": ["digital illustration art colorful", "cute character illustration art"],
        "gallery_keywords": [
            "digital art illustration portrait",
            "anime style character art",
            "watercolor illustration art",
        ],
        "thumb_keywords": ["character illustration sample", "digital drawing art"],
    },
    PROMO: {
        "label": "홍보·프로모션 (오픈/이벤트)",
        "hero": "cover",
        "min_blocks": 15,
        "long_text": False,
        "mood": "고채도·고대비 포인트(레드/오렌지/옐로) + 큰 숫자. 긴급성·축제감.",
        "sections": [
            "이벤트 타이틀 히어로(cover_bg) + 기간/D-day",
            "핵심 혜택 한 방(notice: 1+1, 할인율 — 가장 크게)",
            "이벤트 기간(text: '6/15~6/30 · 진행중' — '종료/End' 표현 금지)",
            "참여 방법 ①②③(text 짧게, 번호 스텝)",
            "쿠폰/응모 CTA(카카오톡 채널 추가 — **medium 스탠다드**: 핵심 전환 버튼)",
            "메뉴/상품 미리보기(group_link grid-2 — **모든 항목 thumbnail_url 필수**, 카드색은 "
            "페이지 배경과 분명히 다른 명도로: 배경에 동화되면 단조롭다) — 1+1 대상 상품 강조",
            "방문 후기(text toggle 1개 — 아이디 ★ 한줄평 5~6개)",
            "유의사항(text toggle, 짧은 불릿)",
            "위치·운영시간(map) + 전화(social)",
        ],
        "services": [
            "카카오톡 채널 추가(쿠폰)",
            "인스타 팔로우/응모",
            "네이버 예약·배달앱",
            "네이버지도",
        ],
        "copy": [
            "🎉 오픈 기념 아메리카노 1+1",
            "⏰ 이번 주말만 · 선착순 100명",
            "🔥 채널 추가하면 첫 잔 무료",
            "📍 OO점 그랜드 오픈",
        ],
        "hero_keywords": ["cafe interior cozy warm opening", "coffee shop ambiance bright"],
        "gallery_keywords": [
            "coffee latte art cafe",
            "cafe dessert pastry",
            "cafe interior detail cozy",
        ],
        "thumb_keywords": ["coffee cup drink photo", "cafe menu item photo"],
    },
    GENERIC: {
        "label": "일반 링크인바이오",
        "hero": "avatar",
        "min_blocks": 11,
        "long_text": False,
        "mood": "깔끔한 1색 + 화이트/중립 배경, 포인트 1색.",
        "sections": [
            "프로필 헤더 + 한 줄 소개",
            "대표 CTA 1개",
            "주요 링크 group_link",
            "갤러리 또는 후기",
            "문의/SNS",
        ],
        "services": ["카카오톡 채널", "인스타·유튜브", "스마트스토어", "문의 폼"],
        "copy": ["✨ 반가워요!", "👇 아래에서 둘러보세요", "💬 문의는 카톡으로"],
        "hero_keywords": ["clean lifestyle brand photography", "minimal aesthetic background"],
        "gallery_keywords": ["lifestyle photography clean", "aesthetic product flat lay"],
        "thumb_keywords": ["clean product photo", "lifestyle thumbnail"],
    },
}


# ── 컨셉 → 카테고리 추론 ─────────────────────────────────────
# 순서 중요: 구체적인 것부터 검사(공동구매·청첩장 등이 일반 '프로필'보다 먼저).
_INFER_RULES: list[tuple[str, tuple[str, ...]]] = [
    (
        INVITATION,
        (
            "청첩장",
            "초대장",
            "결혼식",
            "결혼합니다",
            "웨딩",
            "예식",
            "돌잔치",
            "돌잔치초대",
            "백일",
        ),
    ),
    (GROUPBUY, ("공동구매", "공구", "공동 구매", "공구가")),
    (COMMISSION, ("커미션", "의뢰받", "일러스트레이터", "작업 의뢰", "그림 의뢰")),
    (
        RENTAL,
        ("공간 대여", "공간대여", "대관", "파티룸", "모임공간", "스튜디오 대여", "공유오피스"),
    ),
    (BIZCARD, ("명함", "디지털 명함", "전자명함")),
    (PORTFOLIO, ("포트폴리오", "촬영", "사진작가", "작업물", "레퍼런스")),
    (LANDING, ("랜딩", "출시", "런칭", "사전예약", " 앱 ", "앱 ", "베타", "다운로드")),
    (BROCHURE, ("브로슈어", "팜플렛", "팜플릿", "브랜드 스토리", "제품 라인업", "라인업")),
    (
        PROMO,
        (
            "프로모션",
            "오픈 이벤트",
            "오픈이벤트",
            "그랜드 오픈",
            "오픈 기념",
            "이벤트",
            "1+1",
            "할인 행사",
        ),
    ),
    (AFFILIATE, ("제휴", "추천템", "쿠팡 파트너스", "내돈내산", "공구템", "추천 제품")),
    (BIZCARD, ("프리랜서", "프리렌서")),
    (PROFILE, ("프로필 링크", "인플루언서", "유튜버", "크리에이터", "브이로그", "채널")),
]


def infer_category(concept: str) -> str:
    """컨셉 문구에서 카테고리를 추론한다. 못 찾으면 PROFILE(가장 흔함)."""
    text = f" {concept or ''} "
    for category, keywords in _INFER_RULES:
        for kw in keywords:
            if kw in text:
                return category
    return PROFILE


# ReferenceCategory(DB, GET /api/v1/ai/categories/) 슬러그 → 레시피 키 별칭.
# 프론트는 카테고리 선택 UI 의 슬러그를 그대로 보내면 된다.
CATEGORY_ALIASES: dict[str, str] = {
    "profile-link": PROFILE,
    "digital-card": BIZCARD,
    "space-booking": RENTAL,
    "group-buy": GROUPBUY,
    "promotion": PROMO,
}


def normalize_category(value: str) -> str:
    """레시피 키 또는 ReferenceCategory 슬러그를 레시피 키로 정규화. 모르면 ""."""
    v = (value or "").strip().lower()
    if v in CATEGORY_PROFILES:
        return v
    return CATEGORY_ALIASES.get(v, "")


def resolve_category(user_input: dict) -> str:
    """user_input 의 명시 ``category`` 우선(레시피 키/DB 슬러그 모두 허용),
    없으면 concept 에서 추론. 명시값이 유효한 키가 아니면 추론으로 폴백."""
    explicit = normalize_category(user_input.get("category") or "")
    if explicit:
        return explicit
    return infer_category(user_input.get("concept") or "")


def get_profile(category: str) -> dict:
    return CATEGORY_PROFILES.get(category, CATEGORY_PROFILES[GENERIC])


def is_long_text_category(category: str) -> bool:
    return bool(get_profile(category).get("long_text"))


def hero_strategy(category: str) -> str:
    return get_profile(category).get("hero", "avatar")


def build_recipe_prompt(category: str, include_mood: bool = True) -> str:
    """카테고리 레시피를 프롬프트 섹션 문자열로 렌더한다(새-페이지 생성용).

    Args:
        include_mood: False 면 무드/색 지시를 뺀다 — 컨셉 이미지 팔레트나 레퍼런스
            템플릿이 디자인 주도권을 가질 때, 레시피의 기본 색 취향(크림/베이지 등)이
            그것과 경쟁해 "맨날 비슷한 색감"이 되는 문제를 막는다. 구조(섹션)·카피·
            이미지 전략은 그대로 유지.
    """
    p = get_profile(category)
    hero_line = (
        "프로필 히어로는 **cover_bg + cover_image_url(대표 이미지)** 로 강한 첫인상을."
        if p["hero"] == "cover"
        else "프로필은 **center(또는 left) 아바타** 로 — 사람/브랜드가 주인공. cover_bg 남발 금지."
    )
    read_line = (
        "이 카테고리는 **인사말/글귀에 한해 따뜻한 긴 문단 허용**(그 외 섹션은 짧게)."
        if p["long_text"]
        else "**모든 텍스트는 짧고 스캔 가능하게** — 헤드라인 1줄 + 본문 1~2문장. 긴 문단 금지(특히 about/이용안내는 불릿·짧은 줄로)."
    )
    lines = [f"### [카테고리 레시피 — {p['label']}]"]
    if include_mood:
        lines.append(f"- 무드/색: {p['mood']}")
    else:
        lines.append(
            "- 무드/색: **이 레시피가 정하지 않는다** — 위/아래에 주어진 팔레트(컨셉 이미지 "
            "추출 #hex) 또는 레퍼런스 페이지의 색을 그대로 따르라."
        )
    lines += [
        f"- 프로필 레이아웃: {hero_line}",
        "- 꼭 들어가야 할 섹션(위→아래, 컨셉에 맞게 가감):",
    ]
    lines += [f"  {i + 1}. {s}" for i, s in enumerate(p["sections"])]
    lines.append(
        f"- 한국에서 실제 쓰는 링크/CTA(가능한 것만, 그럴듯한 실제형 URL): {', '.join(p['services'])}."
    )
    lines.append("- 카피 톤(이모지를 줄 맨 앞 1개로, 과용 금지). 이런 느낌으로 새로 작성:")
    lines += [f"    {c}" for c in p["copy"]]
    lines.append(f"- 가독성: {read_line}")
    lines.append("")
    lines.append("### [공통 강제 규칙 — 위반 시 실패]")
    lines.append(
        f"1. **블록을 풍부하게 — 최소 {p['min_blocks']}개는 바닥 조건일 뿐, 더 많을수록 좋다**. "
        "잘 만든 실제 페이지는 25~30블록이다(사람이 공들여 만든 것처럼). 빈약/단조 금지. "
        "한 종류만 반복하지 말고 **여러 블록 타입을 섞어라**(single_link/group_link/text/gallery/"
        "notice/social/map/spacer/folder). 단 쓸데없는 채우기 블록 말고 **그 비즈니스에 진짜 "
        "필요한 정보**로 채워라."
    )
    lines.append(
        "1-1. **섹션 리듬(사람 손맛)**: 페이지를 주제별 섹션으로 나누고, 각 섹션은 "
        '**이모지 섹션 헤더(text, `text_layout:"default"`, headline 한 줄 — 예: "🔥 이번 주 BEST", '
        '"🛁 목욕·위생 케어", "📚 시즌 1 에피소드") → 내용 블록들 → spacer 구분선** 순서로 묶어라. '
        "이 리듬이 있어야 긴 페이지도 정돈돼 보인다."
    )
    lines.append(
        '2. **링크 카드 3단계 크기 정책**: ①기본은 `layout:"small"`(컴팩트 — 보조 링크 전부). '
        "②**페이지의 주요 전환 CTA 딱 1개는 `medium`(스탠다드)** — 카톡 문의·무료체험·예약·주문처럼 "
        "비즈니스 소통/전환에 직결되는 버튼(한 줄로 묻히면 안 되고 큰 쇼케이스는 과함). "
        "②-1 **상품 카드는 예외**: 썸네일+가격이 있는 상품성 카드(`medium`)는 여러 개 써도 좋다 — "
        "코너별 2~3개 진열이 자연스럽다. "
        "③`large`(쇼케이스)는 **상단 와이드 이미지가 핵심인 대표 상품/이벤트 1개만**(thumbnail_url 필수). "
        "그 외 연락·계좌·다운로드 류 보조 버튼은 전부 small."
    )
    lines.append(
        "2-1. **group_link 항목 썸네일 필수**: group_link 의 `links` 항목에는 레이아웃이 list 든 "
        "grid 든 **모든 항목에 `thumbnail_url`({{image:구체 키워드}})** 을 채워라 — list 도 좌측에 "
        "썸네일이 렌더되며, 없으면 '사진이 빠진 페이지'로 보인다. **유일한 예외는 후기 리스트** "
        "(제목 '이름 ★★★★★')와 텍스트 가격표 — 이 둘만 썸네일 생략."
    )
    lines.append(
        "2-2. **라벨-블록 일치**: 라벨이 약속하는 UI 와 실제 블록 타입을 일치시켜라. "
        "'분야별 보기'·'모아보기' 라벨이면 그 자리에 실제 group_link/folder 묶음이 와야 하고, "
        "'갤러리' 라벨이면 gallery 블록이어야 한다. 클릭할 수 없는 정보(편의시설 나열 등)를 "
        "single_link 로 만들지 마라 — text 불릿으로."
    )
    lines.append(
        "3. **후기는 텍스트 토글 1개로**: group_link 로 줄줄이 만들지 말고 **text 블록 1개** "
        '(`text_layout:"toggle"`, headline 예: "💬 실제 후기 모음") 안에 5~6개 후기를 모아라. '
        "각 후기는 `아이디 ★★★★★` 한 줄 + 다음 줄에 한줄평, 후기 사이는 빈 줄. 이름은 **실명이 "
        "아니라 아이디/닉네임**(예: 달콤한하루, mins_pick, 콩이맘22, 여행자J). 별은 유니코드 ★"
        "(이모지 ⭐ 금지), **별점을 일부러 다르게**(대부분 ★★★★★, 1~2개는 ★★★★☆) — 다 똑같으면 "
        "가짜 티. 한줄평은 20자 내외로 짧게."
    )
    lines.append(
        '4. **가짜 통계 금지**: "만족도 100%", 억지 별점 수치, 가짜 다운로드/회원수 같은 건 AI 티가 난다. '
        "쓰려면 그럴듯한 사용자 한줄평(후기)으로."
    )
    lines.append(
        "5. **사진(이미지) 자체가 콘텐츠인 타입**(포트폴리오/커미션/공간/제품)은 갤러리·썸네일을 "
        "넉넉히 — 작품/공간/제품 사진이 많아야 신뢰가 생긴다."
    )
    lines.append(
        "6. **이미지 키워드는 실제 피사체를 영어로 아주 구체적으로**(예: 'blueberry jam glass jar', "
        "'skincare serum bottle white background'). "
        "'product'/'lifestyle'/'flat lay' 같은 모호한 단어만 쓰면 엉뚱한 사진(말벌·풍경·과일)이 나온다. "
        "음식/뷰티/상품은 **그 상품 자체**가 또렷이 보이는 키워드로. "
        "단, 검색되는 곳은 서양 스톡사진 사이트다 — **한국 특수 상품(실리콘 흡착식판, 수제잼 선물세트 "
        "등)은 검색 결과가 없으니, 그 상품이 쓰이는 일반적인 장면으로 풀어 써라**(예: 실리콘 유아식기 → "
        "'baby eating with spoon high chair', 수제잼 → 'jam on toast bread'). "
        "'korean' 같은 국적/인종 한정어도 검색 실패의 주범이니 빼라."
    )
    lines.append(
        "7. **영상(video) 블록은 [목표] 컨셉에 실제 영상 URL(youtube/youtu.be/tiktok/vimeo)이 "
        "주어진 경우에만** — 그 URL 을 `video_urls` 에 그대로 넣어라(다른 URL 창작 절대 금지: "
        "환각 URL 은 깨진 임베드가 되어 백엔드가 자동 제거한다). 컨셉에 영상 URL 이 없으면 video 금지."
    )
    lines.append(
        "8. **정확한 한국어**: 오타·띄어쓰기 오류 금지(예: '바로'를 '비로', '점심시간'을 '정심시간'으로 쓰지 마라). "
        "채널은 '카카오톡 채널'(서비스 종료된 '카카오스토리' 금지). 한/영 중복 버튼(예: '무료체험'+'Free Trial') 금지."
    )
    lines.append(
        "9. **고객정보 폼(customer/inquiry)은 꼭 필요할 때만** — 입력칸 라벨이 영어로 떠 어색하다. "
        "단순 문의/예약/신청은 카카오톡 채널·네이버폼 등 **외부 링크(single_link small)** 로 받는 게 더 자연스럽다."
    )
    lines.append(
        "10. **중요 정보는 숨기지 마라**: 요금표·쿠폰코드·일시·계좌 같은 핵심 정보는 toggle 로 접지 "
        '말고 `text_layout:"plain"` 으로 바로 보이게. toggle 은 길어도 되는 보조 정보(이용안내· '
        "주의사항·FAQ·환불규정)에만. 같은 성격의 안내(이용안내/주차안내)는 **같은 포맷으로 나란히**."
    )
    lines.append(
        "11. **text 블록 가독성 설계**: 마크다운(`**볼드**`)·HTML 은 렌더되지 않는다 — 굵게 만들 수 "
        "없으니 **이모지 1개 + 줄바꿈 + 짧은 줄** 로 구조를 만들어라(예: '⏰ 평일 15,000원/시간\\n"
        "🌙 심야 12,000원/시간'). 빈 줄로 문단을 나누고, headline 에 핵심을 담아라."
    )
    lines.append(
        "12. **유틸리티 블록 활용 (섹션에 명시된 카테고리는 필수)**:\n"
        "    - **search**: 블록이 16개 이상으로 길어지면 profile(과 notice) 바로 아래에 검색 블록 "
        '1개(`_type:"search"`, search_placeholder 는 페이지 성격에 맞게).\n'
        "    - **folder**: 같은 성격의 링크/코너가 3개 이상이면 folder(toggle)로 묶어라. "
        "**작성법(이 형식을 그대로)** — 하위 블록들에 임시 정수 id 를 달고 folder 가 참조:\n"
        '      {"id": 901, "type": "single_link", "order": 5, "data": {"_type": "single_link", '
        '"label": "수분 크림", "url": "https://...", "thumbnail_url": "{{image:moisturizer jar}}"}}\n'
        '      {"id": 902, "type": "single_link", "order": 6, "data": {"_type": "single_link", '
        '"label": "토너", "url": "https://...", "thumbnail_url": "{{image:toner bottle}}"}}\n'
        '      {"type": "single_link", "order": 7, "data": {"_type": "folder", '
        '"label": "🧴 스킨케어 모아보기", "child_block_ids": [901, 902], '
        '"folder_display_mode": "toggle", "is_collapsed_default": false}}\n'
        "      (백엔드가 임시 id 를 실제 ID 로 재매핑하고, 하위 블록은 폴더 안에만 렌더된다.)\n"
        "    - **schedule**: 공구 일정·행사·오픈 일정처럼 **실제 날짜가 컨셉에 있으면** schedule 블록 "
        '사용(`schedule_layout:"list"` 만 — calendar 는 현재 달만 보여 미래 일정이 가려진다). '
        'schedule_items 항목 형식: {"id": "uuid", "title": "공구 OPEN", "start_date": "2026-06-15", '
        '"end_date": "2026-06-15"}. 날짜는 컨셉에 적힌 실제 날짜만 — 임의 창작 금지.'
    )
    lines.append("")
    return "\n".join(lines)
