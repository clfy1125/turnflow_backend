# 링크 페이지 이미지가 안 뜨는 건 — 원인 규명 + 프론트 작업 요청서

> 2026-09-22 · 백엔드 → 프론트
> 발단: CS #8f11703f (`soosun91@gmail.com`, 프로) — *"02 류혜영 립 이미지가 턴플로우에선 늦게 뜨고,
> 인스타 링크로 타고 들어가면 전혀 안 뜹니다"*
>
> 이 한 건을 파다가 **죽은 `blob:` URL 이 서버에 그대로 저장되는 프론트 결함**을 찾았습니다.
> 지금 prod 에 **6건, 그중 5건이 공개 페이지**로 살아 있고 **5건은 복구가 불가능**합니다.
> 백엔드 쪽에서도 결함 2개가 나왔지만 **현재는 프론트가 우회하고 있어 증상을 만들지 않습니다**(§4).

---

## 0. 요약 — 누가 무엇을 하나

| # | 문제 | 담당 | 상태 |
|---|---|---|---|
| **F-1** | **`update('images', …)` 가 blob 가드를 통과해 죽은 `blob:` URL 이 DB 에 저장됨** | **프론트** | 요청 (핵심) |
| **F-2** | **`images[]` ↔ `image_media_ids[]` 인덱스가 어긋남 → 사후 복구 불가** | **프론트** | 요청 |
| F-3 | 언마운트 정리(`IMAGE_PENDING_SENTINEL`)가 `images[]` 를 안 봄 | 프론트 | 제안 |
| B-1 | `.webp` 가 `application/octet-stream` 으로 서빙됨 (397건) | 백엔드 | 위생 수정 예정 |
| B-2 | `media.turnflow.clfy.ai.kr` 에 CORS 헤더 없음 | 백엔드 | 위생 수정 예정 |

**F-1 이 "이미지가 안 뜬다" 의 실증된 원인입니다.** B-1·B-2 는 실재하지만 `/image-proxy` 가
받아내고 있어 **지금은 사용자 증상으로 나타나지 않습니다** — 근거는 §4 의 실측 체인입니다.

> ⚠️ **먼저 솔직히**: CS #8f11703f **그 건 자체**가 F-1 때문이라는 직접 증거는 없습니다.
> 해당 블록은 `thumbnail_url` 경로(가드가 **적용되는** 쪽)이고, 고객이 재시도 끝에 스스로
> 해결해 현재 데이터는 정상입니다. 증상 시점 데이터는 남아 있지 않습니다(그 페이지 스냅샷 0건).
> **F-1 은 그 건을 파다 발견된 별개의 실제 피해**이고, 이쪽이 훨씬 큽니다.

---

## 1. 실제 피해 (prod 전수 스캔, 2026-09-22)

`Block.data` 전체를 JSON 문자열로 훑어 `blob:` 을 검색했습니다. (`Page.data` 는 0건)

| 블록 | 페이지 | 공개 | 위치 | 복구 |
|---|---|---|---|---|
| 12717 | `@page-xn221uytfn` | **O** | `data.images[0]` | △ media 830 (인덱스가 맞아떨어진 경우) |
| 9692 | `@dongtan-body` | **O** | `data.images[0]` | ❌ |
| 13522 | `@jangdoctor` | **O** | `data.images[0]` | ❌ |
| 13643 | `@buildingspot` | **O** | `data.images[1]` | ❌ |
| 15002 | `@page-xtiyec0dhb` | **O** | `data.images[1]` | ❌ (2026-09-21 발생 — **현재진행형**) |
| 348 | `@terrarium-high` | X | `data.images[3]` | ❌ |

저장된 값의 실제 모습:

```
blob:https://turnflow.link/a492c7d2-54be-4802-bdad-0f1d632049d7
blob:https://link.turnflow.clfy.ai.kr/ff30c1f5-09aa-42ca-ad2c-3ff8203a277b   ← 2026-04, 옛 호스트
```

`blob:` URL 은 **그것을 만든 브라우저 탭 안에서만** 유효합니다. 탭이 닫히는 순간 죽고,
다른 사람·다른 기기에서는 **처음부터 존재한 적이 없는 주소**입니다.
→ 만든 본인 화면에서는 멀쩡해 보이고, **방문자에게만 안 보입니다.**
고객이 "내 폰에선 되는데 남들은 안 보인대요" 라고 말하는 전형적인 형태입니다.

2026-04 건부터 있는 걸 보면 **오래된 결함**이고, 가장 최근 건이 어제(09-21)라 **지금도 발생 중**입니다.

---

## 2. F-1 — 왜 저장되나 (메커니즘)

### 2-1. 가드는 있는데, 스칼라 4개 키만 봅니다

`src/app/pages/link/components/BlockEditor.tsx:385`

```js
const IMAGE_URL_FIELDS = new Set(['thumbnail_url', 'avatar_url', 'cover_image_url', 'image_url']);
//                                 ↑ 'images' (배열) 가 없다

const update = (key, value) => {
  const resolvedValue = (IMAGE_URL_FIELDS.has(key) && value === '') ? null : value;
  ...
  // blob: URL은 로컬 livePreview 전용이므로 서버 저장 대상에서 제외
  // (서버에 저장되면 페이지 revoke 시 영구히 깨진 이미지로 남음)
  if (IMAGE_URL_FIELDS.has(key)
      && typeof resolvedValue === 'string'
      && (resolvedValue.startsWith('blob:') || resolvedValue === IMAGE_PENDING_SENTINEL)) {
    return;                                    // ← 여기서 막아야 하는데
  }

  // Debounced auto-save
  saveTimerRef.current = setTimeout(() => onSave(block, latestDataRef.current), 800);
};
```

의도는 정확합니다(주석까지 정확합니다). 문제는 **조건 두 개가 배열을 배제**한다는 것입니다.

| 조건 | `key = 'thumbnail_url'` | `key = 'images'` |
|---|---|---|
| `IMAGE_URL_FIELDS.has(key)` | ✅ true | ❌ **false** |
| `typeof resolvedValue === 'string'` | ✅ true | ❌ **false** (배열) |

→ `images` 는 가드를 그냥 지나쳐 **800ms 디바운스 자동저장으로 직행**합니다.

### 2-2. 그런데 blob 은 "업로드 중"도 아니고 **크롭하는 도중**에 들어갑니다

`BlockEditor.tsx:430-437` (갤러리 추가 경로)

```js
let galleryPreviewIdx = -1;
const onGalleryLivePreview = (previewUrl) => {        // ← 크롭 다이얼로그가 드래그마다 호출
  const imgs = [...(latestDataRef.current.images ?? [])];
  if (galleryPreviewIdx < 0) { galleryPreviewIdx = imgs.length; imgs.push(previewUrl); }
  else { imgs[galleryPreviewIdx] = previewUrl; }
  update('images', imgs);                             // ← 800ms 뒤 서버 저장 예약
};
const result = await editImage(file, { width: 1080, height: 1080, ... }, onGalleryLivePreview);
```

즉 **사용자가 크롭 핸들을 잡고 0.8초만 멈춰도** `blob:` 이 서버에 PATCH 됩니다.
업로드가 시작되기 한참 전입니다. 그래서 `setUploading(true)` / 제출 버튼 `disabled` 같은
기존 방어가 **전혀 걸리지 않습니다**.

### 2-3. 왜 대부분은 멀쩡한가 (그리고 왜 가끔 터지는가)

정상 흐름에서는 뒤이어 진짜 URL 로 덮어써집니다:

```js
const res = await uploadMedia(...);
if (res.ok && res.data) {
  curImgs[galleryPreviewIdx] = res.data.url;   // blob → 실제 URL
  update('images', curImgs);                   // 다시 저장 → 덮어써짐
}
```

**살아남는 경로** — 덮어쓰기가 도달하지 못하는 경우:

1. 크롭 중 **탭/브라우저를 닫음** → blob 만 저장된 채 끝
2. 크롭 확정 후 **업로드가 실패**했는데 `galleryPreviewIdx < 0` 이라 splice 정리가 안 됨
3. 업로드 중 **페이지 이탈/블록 접기** (§F-3 — 언마운트 정리가 `images[]` 를 안 봄)
4. `catch` 로 빠진 경우 — `alert` 만 띄우고 **배열은 blob 인 채로 방치**

```js
} catch { alert(t('link.blockEditor.errors.uploadError')); }   // ← 정리 없음
finally { setUploading(false); }
```

6건이라는 숫자가 딱 이 시나리오의 빈도로 보입니다 — 흔치 않지만 **꾸준히** 발생합니다.

---

## 3. F-2 — 사후 복구가 왜 불가능한가

⚠️ **`image_media_ids` 로 일괄 복구하려 하지 마세요. 인덱스가 안 맞습니다.**

실제 prod 데이터:

| 블록 | `images` 길이 | `image_media_ids` |
|---|---:|---|
| 12717 | 8 | `[830, 831]` |
| 9692 | 6 | `[]` |
| 13522 | 5 | `[None, None, 1013, 1014, 1015]` |
| 13643 | 2 | `[]` |
| 15002 | 7 | `[]` |
| 348 | 4 | `[189]` |

두 배열이 index-parallel 이라는 보장이 **코드에도 없습니다**:

```js
while (curIds.length < galleryPreviewIdx) curIds.push(null);   // 구멍을 null 로 메움
curIds[galleryPreviewIdx] = res.data.id;
```

이 `while` 은 **성공 경로에서만** 돕니다. URL 붙여넣기·AI 생성·기존 데이터로 들어온 이미지는
`images` 만 늘고 `image_media_ids` 는 안 늘어나 **그때부터 두 배열이 영구히 어긋납니다**.
짝을 가정하고 채우면 **엉뚱한 이미지가 박힙니다.**

대조적으로 `thumbnail_url` 은 형제 키 `thumbnail_media_id` 를 **같은 객체 안에** 들고 있어
번들의 복구 루틴이 살려냅니다. `images[]` 는 **문자열 배열**이라 그 루틴이 손도 못 댑니다.

```js
// 배포 번들에 이미 있는 복구 루틴 — thumbnail_url 만 구제한다
const S = f.filter(C => typeof C.thumbnail_url === 'string' && C.thumbnail_url.startsWith('blob:'));
// ... V ? {...H, thumbnail_url: V} : {...H, thumbnail_url: undefined, thumbnail_media_id: undefined}
```

---

## 4. 백엔드 결함 2건 — 실재하지만 **지금은 증상을 안 만듭니다**

조사 중 백엔드에서도 결함이 나왔습니다. 프론트가 이미 우회하고 있어 **F-1 과 혼동하지 않도록**
근거를 같이 남깁니다.

### B-1. `.webp` 가 `application/octet-stream` 으로 서빙됨 (397건)

```
storages/backends/s3.py:630   _type, encoding = mimetypes.guess_type(name)

prod 컨테이너:  guess_type("x.webp") → (None, None)         ← /etc/mime.types 없음(python:3.11-slim)
                guess_type("x.png")  → ('image/png', None)   ← 파이썬 내장표에 있음
→ django-storages 폴백 = application/octet-stream
```

- DB 는 맞게 적혀 있습니다 (`PageMedia.mime_type = 'image/webp'`). 틀린 건 **R2 오브젝트의 헤더**뿐입니다.
- `.jpg` 558 · `.png` 72 는 정상. **`.webp` 397건만** 해당됩니다.
- 프론트는 이미 이걸 알고 허용하고 있습니다 — 건드리지 않아도 됩니다:
  ```js
  // api.ts isImageBlob()
  // 일부 CDN/R2 가 mime 미설정(application/octet-stream) 으로 내려주기도 함 → 크기만 있으면 허용.
  if (t === '' || t === 'application/octet-stream') return blob.size > 256;
  ```

### B-2. `media.turnflow.clfy.ai.kr` 에 CORS 헤더 없음

`Access-Control-Allow-Origin` 없음, `OPTIONS` → **403**. 프론트 주석에도 이미 적혀 있습니다:

```js
// R2 CDN(media.*) 은 CORS 헤더가 없어 fetch 가 막힌다 — dev 에서는 `/_r2/...` 프록시로 동일 출처화해 우회.
if (isExternal && parsed && import.meta.env.DEV && parsed.host.startsWith('media.')) { ... }
```

### 실측 — prod 에서 `downloadMediaBlob` 체인을 그대로 재현

`https://turnflow.link` 오리진에서 실제 브라우저로 4단계를 순서대로 실행한 결과:

| 단계 | 결과 |
|---|---|
| 1) `/image-proxy?url=…` | ✅ **200 · `image/webp` · 17302B** (octet-stream 을 여기서 **교정**해 줌) |
| 2) `fetch(url, {mode:'cors'})` | ❌ BLOCKED |
| 3) `img.crossOrigin='anonymous'` + canvas | ❌ onerror (CORS 차단) |
| 4) 평범한 `<img src>` = 공개 페이지 렌더 | ✅ **loaded 625×625** |

**공개 페이지 렌더(4)도, 편집기의 blob 취득(1)도 prod 에서 성공합니다.**
→ B-1·B-2 는 현재 사용자 증상을 만들지 않습니다. 백엔드에서 위생 차원으로 고치겠습니다.

> 다만 **우회에 의존하는 상태**입니다. `/image-proxy`(CF Pages 함수)가 죽으면 편집기의
> 이미지 재편집이 즉시 전멸합니다. `/_r2` 우회는 `import.meta.env.DEV` 라 **배포 번들엔 0건**이라
> prod 에는 대체 경로가 `/image-proxy` **하나뿐**입니다. 이 점은 알고 계시면 좋겠습니다.

---

## 5. 요청 — 프론트 수정 제안

### F-1. 가드를 배열까지 확장 (필수)

`update()` 의 조건을 값의 **모양**으로 판정하도록 바꾸는 게 가장 안전합니다.

```js
const IMAGE_URL_FIELDS = new Set(['thumbnail_url', 'avatar_url', 'cover_image_url', 'image_url']);
const IMAGE_ARRAY_FIELDS = new Set(['images']);

const hasUnsavableImage = (key, v) => {
  const dead = (s) => typeof s === 'string'
    && (s.startsWith('blob:') || s === IMAGE_PENDING_SENTINEL);
  if (IMAGE_URL_FIELDS.has(key)) return dead(v);
  if (IMAGE_ARRAY_FIELDS.has(key)) return Array.isArray(v) && v.some(dead);
  return false;
};

const update = (key, value) => {
  ...
  if (hasUnsavableImage(key, resolvedValue)) return;   // 자동저장 예약 안 함
  saveTimerRef.current = setTimeout(() => onSave(block, latestDataRef.current), 800);
};
```

**주의 — 이것만으로는 부족합니다.** `update('images', …)` 는 미리보기 갱신도 겸하므로,
자동저장을 막으면 *그 시점의* 저장은 건너뛰지만 **다른 필드의 저장이 나중에 트리거되면
`latestDataRef.current` 통째로 나가면서 blob 이 같이 실립니다.** 그래서 ↓ 가 같이 필요합니다.

### F-1b. 저장 직전에 한 번 더 거르기 (권장 — 최후의 방어선)

`onSave` 로 나가는 페이로드를 한 곳에서 정제하면 경로가 몇 개든 뚫리지 않습니다.

```js
const stripUnsavable = (data) => {
  const d = { ...data };
  for (const f of IMAGE_URL_FIELDS) {
    if (typeof d[f] === 'string' && (d[f].startsWith('blob:') || d[f] === IMAGE_PENDING_SENTINEL)) d[f] = null;
  }
  if (Array.isArray(d.images)) {
    const keep = d.images.map((v, i) => [v, i])
      .filter(([v]) => !(typeof v === 'string' && (v.startsWith('blob:') || v === IMAGE_PENDING_SENTINEL)));
    d.images = keep.map(([v]) => v);
    if (Array.isArray(d.image_media_ids)) {
      d.image_media_ids = keep.map(([, i]) => d.image_media_ids[i] ?? null);  // 짝을 유지한 채로 제거
    }
  }
  return d;
};
// 모든 저장 경로에서: onSave(block, stripUnsavable(latestDataRef.current))
```

blob 을 **빼고** 저장하면 사용자는 "이미지가 안 올라갔네" 로 인지하고 다시 올립니다.
blob 을 **넣고** 저장하면 본인 화면엔 보이는데 방문자만 못 보는 지금 상태가 됩니다.
**전자가 훨씬 낫습니다.**

### F-2. `images` 와 `image_media_ids` 를 한 객체로 묶기 (중기)

현재 구조는 두 배열의 인덱스 동기화를 모든 경로가 각자 지켜야 해서 이미 깨져 있습니다.
`thumbnail_url` + `thumbnail_media_id` 처럼 **한 객체 안에** 두면 어긋날 수가 없고,
사후 복구도 가능해집니다.

```js
images: [{ url: '…', media_id: 1013 }, …]   // 기존 문자열 배열과 호환 레이어 필요
```

당장은 어렵겠지만, 최소한 **F-1b 의 `keep` 방식처럼 두 배열을 항상 같이 조작**해 주세요.

### F-3. 언마운트 정리에 `images[]` 추가 (작음)

`BlockEditor.tsx:365-378` 의 unmount cleanup 은 스칼라 4개 필드의 `IMAGE_PENDING_SENTINEL` 만
정리합니다. 업로드 중 블록을 접거나 페이지를 이동하면 `images[]` 의 blob 은 그대로 남습니다.

### (참고) `catch` 경로 정리 누락

```js
} catch { alert(t('link.blockEditor.errors.uploadError')); }   // ← splice 정리 없음
```

`galleryPreviewIdx` 가 유효하면 여기서도 배열에서 빼 주세요. 성공/취소 경로에는 이미 있습니다.

---

## 6. 검증 방법

### 고친 뒤 이 쿼리가 늘지 않으면 성공입니다

백엔드에서 주기적으로 돌려 드릴 수 있습니다. 현재 기준선 = **6건**.

```python
# Block.data 전수 스캔
import json
from apps.pages.models import Block
hits = [b for b in Block.objects.iterator()
        if "blob:" in json.dumps(b.data or {}, ensure_ascii=False)]
print(len(hits))   # 2026-09-22 기준 6
```

### 재현 시나리오 (수정 전에 먼저 돌려 보세요)

수정 **전에** 먼저 재현해 두세요 — 탐지기가 진짜 잡는지부터 확인하는 게 순서입니다.

1. 갤러리가 있는 블록에서 이미지 추가 → 크롭 다이얼로그를 **열어 둔 채 1초 이상 대기**
2. 네트워크 탭에서 `PATCH …/blocks/{id}/` 가 나가는지 확인
3. 그 요청 본문의 `data.images` 에 `blob:…` 이 들어 있으면 재현 성공
4. 크롭 창을 **확정하지 말고 탭을 닫음** → 새로고침 → 이미지 자리가 깨져 있으면 완성

느린 네트워크(DevTools Slow 3G)로 하면 재현이 훨씬 쉽습니다.

---

## 7. 깨진 5건 처리 (백엔드 ↔ 프론트 협의 필요)

`@dongtan-body` · `@jangdoctor` · `@buildingspot` · `@page-xtiyec0dhb` · `@terrarium-high` 는
**원본을 되찾을 방법이 없습니다**(media_id 없음). 선택지:

| 안 | 내용 | 비고 |
|---|---|---|
| A | DB 에서 해당 `blob:` 항목만 제거 | 공개 페이지의 깨진 이미지가 즉시 사라짐. 고객 통보 필요 |
| B | 그대로 두고 고객에게 재업로드 안내 | 안내 전까지 계속 깨져 보임 |
| C | 프론트가 로드 실패 이미지를 자동 숨김 | 근본 해결은 아니지만 **모든 과거 사고를 한 번에 덮음** |

백엔드 의견은 **A + C** 입니다. A 로 지금 깨진 걸 치우고, C 로 앞으로의 유사 사고를 흡수합니다.
`@page-xn221uytfn`(12717) 한 건은 media 830 으로 복구가 가능해 보이나 **인덱스가 맞는지 확신이
없어 손대지 않았습니다** — 필요하면 해당 고객 확인 후 진행하겠습니다.

---

## 8. 조사에 쓴 근거 (재확인용)

- prod DB 전수 스캔: `Block.data` 6건 / `Page.data` 0건
- 배포 번들 확인 — 로컬 소스가 아니라 **실제 배포본**에서 갭 확인
  `https://turnflow.link/assets/TurnflowLinkPage-CKvEfmsU.js`
  → `new Set(["thumbnail_url","avatar_url","cover_image_url","image_url"])` · `N("images",fe)`
  → `/_r2` 는 **0건**(DEV 전용이라 빌드에서 제거됨)
- 실브라우저 체인 재현(§4 표) — 헤더만 봐서는 어느 단계가 죽는지 알 수 없어 4단계를 각각 실행
- CS 원건 페이지(`@hwajalal`)는 **현재 정상** — 이미지 4장 전부 200, 공개 API 0.6~1.0초,
  Chromium 렌더 3장 정상. 고객이 11분간 업로드 7회(6건 고아) 끝에 자체 해결

문의는 백엔드로 주세요. F-1b 페이로드 정제 위치만 정해 주시면 백엔드 쪽에서
저장 시점 검증(서버가 `blob:` 을 아예 거절)을 추가하는 것도 가능합니다 — 다만
**서버 거절은 사용자에게 저장 실패로 보이므로** 프론트 정제가 먼저인 게 맞다고 봅니다.
