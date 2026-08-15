"""舞台 Demo：把研究文件 §5.1 的四分鐘閉環劇本變成可重複、可量測穩定度的一等公民。

競賽現場只有一次機會，所以這個模組要回答的不是「能不能跑」，而是三個更硬的問題：

1. **順序對不對** —— :mod:`~factory_guardian.stage.script` 把文件 §5.1 的八段搬成資料，
   每段寫明「要證明什麼」與「要念哪幾個真實欄位」。劇本裡沒有任何預錄數值。
2. **每次都一樣嗎** —— :mod:`~factory_guardian.stage.director` 固定 seed 與注入時點，
   把整場決策壓成一枚指紋；同一指令跑兩次，指紋必須相同。
3. **斷網還跑得完嗎** —— ``offline=True`` 讓整場跑在「無 ``OPENAI_API_KEY`` ＋
   廠區對外鏈路中斷」的最壞情況下，並量出**離線備援時間**。

:mod:`~factory_guardian.stage.reliability` 則把上面三件事連續跑 N 次，
輸出文件 §5.3 要求的「連續成功次數、離線備援時間、資料遺失率」。

    factory-guardian stage --speed live            # 現場：照劇本走完四分鐘
    factory-guardian stage --speed fast --offline  # 最壞情況彩排
    factory-guardian stage-check --runs 30         # 連續 30 次穩定度統計
"""

from __future__ import annotations

from .director import (
    DEFAULT_SCENARIO,
    SPEEDS,
    STAGE_SEED,
    ActReport,
    StageCheck,
    StageDirector,
    StageRun,
    decision_fingerprint,
    fingerprint_hash,
    resolve_speed,
    scripted_approval,
    stage_settings,
)
from .reliability import ReliabilityReport, RunOutcome, percentile, run_reliability
from .render import StageRenderer, render_reliability
from .script import ACT_IDS, SCRIPT, TOTAL_SECONDS, Act, Metric, act, acts_for, mmss, script_dict

__all__ = [
    "ACT_IDS",
    "DEFAULT_SCENARIO",
    "SCRIPT",
    "SPEEDS",
    "STAGE_SEED",
    "TOTAL_SECONDS",
    "Act",
    "ActReport",
    "Metric",
    "ReliabilityReport",
    "RunOutcome",
    "StageCheck",
    "StageDirector",
    "StageRenderer",
    "StageRun",
    "act",
    "acts_for",
    "decision_fingerprint",
    "fingerprint_hash",
    "mmss",
    "percentile",
    "render_reliability",
    "resolve_speed",
    "run_reliability",
    "script_dict",
    "scripted_approval",
    "stage_settings",
]
