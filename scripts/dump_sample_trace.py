"""把端到端事故场景导出为联调样例 JSON。

用法：python3 scripts/dump_sample_trace.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from weight_camp_safety.scenario import build_scenario  # noqa: E402


def main() -> None:
    handles = build_scenario()
    payload = {
        "description": "红旗症状事故全链路：入营疾病分层、禁忌转诊、资质门禁、幂等补传、本地停训、应急外发箱、复核恢复、商业隔离。",
        "rule_book_version": "2026.09-approved",
        "events": handles.log.all_events(),
        "gate_decisions": handles.gate_results,
    }
    out = ROOT / "data" / "sample_trace.json"
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {len(payload['events'])} events -> {out}")


if __name__ == "__main__":
    main()
