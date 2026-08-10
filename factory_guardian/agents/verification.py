"""Verification Agent（規格 §1.2、§6 Step 9）。

> 執行後必須回到 Simulator 驗證結果，而不是停在「LLM 建議」。

做法：把執行前的 KPI、方案的**預測值**與執行後在**真實孿生體**上量到的實際值三者比對。
任何一項沒達標就 FAIL，Orchestrator 會據此改採次佳方案重跑閉環。

這裡刻意用真實孿生體：規劃階段跑的是「診斷信念模型」，
如果診斷錯了，就會在這一步被抓出來 —— 這正是驗證存在的意義。
"""

from __future__ import annotations

from ..domain import (
    RecoveryPlan,
    VerificationCheck,
    VerificationReport,
)
from ..llm import SYSTEM_PROMPT
from .base import Agent

# 容忍度：預測與實際之間允許的落差
PRODUCTION_TOLERANCE_PCT = 12.0
DELAY_TOLERANCE_MIN = 15.0
HEALTH_TOLERANCE = 8.0


class VerificationAgent(Agent):
    name = "verification-agent"
    role = "執行後 KPI 驗證"

    def verify(
        self,
        plan: RecoveryPlan,
        before: dict[str, float],
        twin,
        machine_id: str,
        settle_ticks: int = 30,
        do_nothing: RecoveryPlan | None = None,
    ) -> VerificationReport:
        """在真實孿生體上跑 ``settle_ticks`` 分鐘，然後檢查方案是否真的有效。

        參考基準刻意**不是**「執行前的狀態」。設備劣化是進行式的，
        拿 20 分鐘後的結果去比事故剛發生時的產線達成率，任何方案都必定「變差」——
        那個比較沒有意義。真正該回答的問題是兩個：

        1. 實際結果有沒有達到這個方案自己的預測？（預測可信嗎）
        2. 有沒有贏過「什麼都不做」？（介入有價值嗎）

        ``do_nothing`` 是不作為方案（PLAN-A）。注意反事實比較只有在
        「什麼都不做」本身是合法選項時才成立：如果 PLAN-A 已經被 Safety Agent 擋掉，
        拿產能去和一個不准執行的選項比是沒有意義的 —— 工安事件本來就是
        「用產能換安全」，那不是失敗，那正是系統該做的事。
        """
        with self.timed():
            # 觀察窗內逐 tick 取樣，用**平均值**和方案的預測平均對比。
            # 只取結束時的單點會非常敏感：維修剛好在窗口邊界完成與否，
            # 會讓同一個正確的方案時而通過、時而失敗。
            samples: list[float] = []
            with self.tool("twin.run", f"ticks={settle_ticks}"):
                for _ in range(max(1, settle_ticks)):
                    snap = twin.step()
                    samples.append(snap.production_pct)
            after = twin.kpi()
            measured_production = sum(samples) / len(samples) if samples else after["production_pct"]
            after["production_pct_mean"] = measured_production

            proj = plan.projection
            checks = [
                VerificationCheck(
                    name="觀察窗內平均產線達成率達到方案預測（容忍 %.0f%%）" % PRODUCTION_TOLERANCE_PCT,
                    expected=proj.production_pct,
                    actual=measured_production,
                    tolerance=PRODUCTION_TOLERANCE_PCT,
                    passed=measured_production >= proj.production_pct - PRODUCTION_TOLERANCE_PCT,
                    comparator=">=",
                ),
                VerificationCheck(
                    name="最大訂單延遲不高於方案預測（容忍 %.0f 分鐘）" % DELAY_TOLERANCE_MIN,
                    expected=proj.max_order_delay_min,
                    actual=after["max_order_delay_min"],
                    tolerance=DELAY_TOLERANCE_MIN,
                    passed=after["max_order_delay_min"] <= proj.max_order_delay_min + DELAY_TOLERANCE_MIN,
                    comparator="<=",
                ),
            ]

            counterfactual_valid = (
                do_nothing is not None and do_nothing.feasible and do_nothing.plan_id != plan.plan_id
            )
            if counterfactual_valid and do_nothing is not None:
                base = do_nothing.projection
                checks.append(
                    VerificationCheck(
                        name="產線達成率優於『什麼都不做』的反事實基準",
                        expected=base.production_pct,
                        actual=measured_production,
                        tolerance=PRODUCTION_TOLERANCE_PCT,
                        passed=measured_production >= base.production_pct - PRODUCTION_TOLERANCE_PCT,
                        comparator=">=",
                    )
                )
                checks.append(
                    VerificationCheck(
                        name="最大訂單延遲優於『什麼都不做』的反事實基準",
                        expected=base.max_order_delay_min,
                        actual=after["max_order_delay_min"],
                        tolerance=DELAY_TOLERANCE_MIN,
                        passed=after["max_order_delay_min"] <= base.max_order_delay_min + DELAY_TOLERANCE_MIN,
                        comparator="<=",
                    )
                )

            # 設備保全：處置後設備劣化必須被止住 —— 健康度不低於方案預測的最低點。
            machine_health = twin.snapshot().machines[machine_id].health
            checks.append(
                VerificationCheck(
                    name="設備健康度不低於方案預測的最低點",
                    expected=proj.min_health,
                    actual=machine_health,
                    tolerance=HEALTH_TOLERANCE,
                    passed=machine_health >= proj.min_health - HEALTH_TOLERANCE,
                    comparator=">=",
                )
            )
            passed = all(c.passed for c in checks)

        report = VerificationReport(
            plan_id=plan.plan_id,
            passed=passed,
            checks=checks,
            before=before,
            after=after,
        )
        report.narrative = self._narrate(report, plan)
        self.log(
            "verify",
            plan_id=plan.plan_id,
            passed=passed,
            settle_ticks=settle_ticks,
            before={k: round(v, 2) for k, v in before.items()},
            after={k: round(v, 2) for k, v in after.items()},
            checks=[c.to_dict() for c in checks],
            note="在真實孿生體上驗證；規劃階段用的是診斷信念模型。",
        )
        return report

    def _narrate(self, report: VerificationReport, plan: RecoveryPlan) -> str:
        def fallback() -> str:
            verdict = "通過" if report.passed else "未通過"
            failed = [c.name for c in report.checks if not c.passed]
            lines = [
                f"方案 {plan.plan_id}｜{plan.title} 執行後驗證{verdict}。",
                f"產線達成率 {report.before.get('production_pct', 0):.0f}% → {report.after.get('production_pct', 0):.0f}%"
                f"（方案預測 {plan.projection.production_pct:.0f}%）。",
                f"最大訂單延遲 {report.before.get('max_order_delay_min', 0):.0f} 分鐘 → "
                f"{report.after.get('max_order_delay_min', 0):.0f} 分鐘。",
                f"工廠健康度 {report.before.get('factory_health', 0):.0f} → {report.after.get('factory_health', 0):.0f}。",
            ]
            if failed:
                lines.append("未達標項目：" + "、".join(failed) + "，將改採次佳方案重跑閉環。")
            return "\n".join(lines)

        if self.ctx.llm is None:
            return fallback()
        payload = {
            "plan": plan.plan_id,
            "passed": report.passed,
            "before": {k: round(v, 2) for k, v in report.before.items()},
            "after": {k: round(v, 2) for k, v in report.after.items()},
            "checks": [c.to_dict() for c in report.checks],
        }
        user = (
            "以下是執行後在模擬器上實際量到的 KPI 驗證結果，請用 3~4 行繁體中文寫成驗證結論。"
            "不要杜撰數字，也不要改變通過與否的判定。\n\n"
            f"{payload}"
        )
        return self.ctx.llm.narrate(SYSTEM_PROMPT, user, fallback, actor=self.name).text


__all__ = ["VerificationAgent", "PRODUCTION_TOLERANCE_PCT", "DELAY_TOLERANCE_MIN"]
