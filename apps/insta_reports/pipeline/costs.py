"""비용 장부 — 모든 유료 호출(API usage)을 계정 단위로 기록.

runs/{username}/costs.json 에 누적. 토큰 수는 API 응답 usage 원본을 저장하므로
단가표(config.PRICES) 변경 시 재정산 가능.
"""

import json
import threading
from datetime import UTC, datetime

from . import config


class CostLedger:
    def __init__(self, username: str):
        self.username = username
        self.path = config.RUNS_DIR / username / "costs.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        if self.path.exists():
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
        else:
            self.data = {"username": username, "entries": []}

    def record_llm(
        self,
        stage: str,
        model: str,
        in_tok: int,
        out_tok: int,
        note: str = "",
        cached: bool = False,
    ) -> float:
        p = config.PRICES.get(model, {"in": 0.0, "out": 0.0, "estimated": True})
        cost = 0.0 if cached else (in_tok * p["in"] + out_tok * p["out"]) / 1e6
        with self._lock:
            self.data["entries"].append(
                {
                    "ts": datetime.now(UTC).isoformat(),
                    "stage": stage,
                    "kind": "llm",
                    "model": model,
                    "input_tokens": in_tok,
                    "output_tokens": out_tok,
                    "cost_usd": round(cost, 6),
                    "price_estimated": p.get("estimated", True),
                    "cached": cached,
                    "note": note,
                }
            )
            self._save()
        return cost

    def record_flat(self, stage: str, provider: str, cost_usd: float, note: str = ""):
        with self._lock:
            self.data["entries"].append(
                {
                    "ts": datetime.now(UTC).isoformat(),
                    "stage": stage,
                    "kind": "flat",
                    "model": provider,
                    "cost_usd": round(cost_usd, 6),
                    "note": note,
                }
            )
            self._save()

    def _save(self):
        self.path.write_text(json.dumps(self.data, ensure_ascii=False, indent=1), encoding="utf-8")

    def summary(self) -> dict:
        by_stage: dict[str, dict] = {}
        for e in self.data["entries"]:
            s = by_stage.setdefault(
                e["stage"], {"cost_usd": 0.0, "calls": 0, "input_tokens": 0, "output_tokens": 0}
            )
            s["cost_usd"] += e["cost_usd"]
            s["calls"] += 1
            s["input_tokens"] += e.get("input_tokens", 0)
            s["output_tokens"] += e.get("output_tokens", 0)
        total = sum(s["cost_usd"] for s in by_stage.values())
        return {
            "username": self.username,
            "total_usd": round(total, 4),
            "by_stage": {
                k: {**v, "cost_usd": round(v["cost_usd"], 4)} for k, v in by_stage.items()
            },
        }
