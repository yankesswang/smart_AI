"""營運實境層測試:工單模型、當班流量模擬、影子後台查詢、營運 API。

這一組測試守的是三件事:

1. **情境是合法的。** 每個樣板產出的動作請求都必須通過 G1 —— 樣板寫壞了會讓
   模擬流量整批卡在 schema 驗證,而畫面上只會看到「都沒事」。
2. **來源鏈的語意沒有被寫歪。** 正常業務的來源鏈只能有可信通道;內部系統的
   查詢結果是證據不是指令。這條規則一旦破掉,誤攔率會爆掉而測試不會叫。
3. **模擬層碰不到治理結果。** 工單脈絡不進裁決、標註不外洩、劇本的待核件
   不會被背景自動簽掉。
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from agentgate.api.server import create_app
from agentgate.console import (
    CASE_TEMPLATES,
    TEMPLATES_BY_ID,
    OpsSimulator,
    build_case,
    current_shift,
)
from agentgate.gates.g1_resolution import validate_request
from agentgate.ontology import Channel
from agentgate.pipeline import AgentGatePipeline, GateConfig
from agentgate.shadow import ShadowTelecomEnv

FIXED_NOW = datetime(2026, 8, 17, 10, 30, tzinfo=timezone.utc)


def _sim(**kwargs) -> OpsSimulator:
    shadow = ShadowTelecomEnv()
    pipeline = AgentGatePipeline(shadow=shadow, config=GateConfig())
    return OpsSimulator(pipeline=pipeline, shadow=shadow, **kwargs)


# --------------------------------------------------------------------------------------
class TestCaseTemplates:
    @pytest.mark.parametrize("template", CASE_TEMPLATES, ids=lambda t: t.template_id)
    def test_template_builds_a_valid_request(self, template):
        shadow = ShadowTelecomEnv()
        case, request = build_case(template, random.Random(7), shadow, FIXED_NOW, 1)

        assert validate_request(request) == [], f"{template.template_id} 產出的請求過不了 G1"
        assert case.turns, "工單必須有對話逐字 —— 沒有對話就回到了憑空生出動作的老問題"
        assert request.provenance_chain, "來源鏈不得為空"
        assert request.reasoning, "Agent 推理摘要不得為空"

    @pytest.mark.parametrize(
        "template",
        [t for t in CASE_TEMPLATES if t.klass == "normal"],
        ids=lambda t: t.template_id,
    )
    def test_normal_cases_carry_only_trusted_instruction_sources(self, template):
        """正常業務的來源鏈只能有可信通道。

        這是回歸測試。把帳務系統的查詢結果當成 TOOL_OUTPUT 塞進來源鏈,
        「查證後退費」就會整批被 G0-R1 擋掉 —— 那是誤攔,不是治理。
        內部查證屬於證據,它出現在對話裡(role=tool),不進鏈。
        """
        shadow = ShadowTelecomEnv()
        _, request = build_case(template, random.Random(3), shadow, FIXED_NOW, 1)
        channels = {p.channel for p in request.provenance_chain}
        assert channels <= {Channel.USER_VERIFIED, Channel.SYSTEM}, (
            f"{template.template_id} 的來源鏈含不可信通道 {channels};"
            "正常業務不該有 —— 檢查是不是把『證據』寫成了『指令』"
        )

    @pytest.mark.parametrize(
        "template",
        [t for t in CASE_TEMPLATES if t.attack_type == "injection"],
        ids=lambda t: t.template_id,
    )
    def test_injection_cases_carry_the_untrusted_source(self, template):
        """注入情境的指令一定要能追溯到不可信通道,否則 G0 沒有東西可以擋。"""
        shadow = ShadowTelecomEnv()
        _, request = build_case(template, random.Random(5), shadow, FIXED_NOW, 1)
        channels = {p.channel for p in request.provenance_chain}
        assert channels & {Channel.TOOL_OUTPUT, Channel.MEMORY, Channel.USER_UNVERIFIED}

    def test_pdf_injection_attachment_exposes_the_planted_line(self):
        """附件必須帶得出被夾帶的那一行,而且對得上來源鏈的 ref。

        前端要把那一行標紅。標不出來的話,提示注入在簡報上就又變回一個名詞。
        """
        shadow = ShadowTelecomEnv()
        case, request = build_case(
            TEMPLATES_BY_ID["pdf_injection"], random.Random(1), shadow, FIXED_NOW, 1)

        attachment = case.attachments[0]
        assert attachment.injected_line >= 0
        # 檔名與來源鏈的 ref 必須指向同一份文件
        assert attachment.filename in attachment.ref
        planted = attachment.lines[attachment.injected_line]
        assert "backup@ext-mail.example" in planted
        assert any(p.source_ref == attachment.ref for p in request.provenance_chain)

    def test_case_context_never_leaks_the_label(self):
        """工單脈絡不得帶攻擊標註 —— 那等同於答案,核准介面看得到就是作弊。"""
        shadow = ShadowTelecomEnv()
        for template in CASE_TEMPLATES:
            case, request = build_case(template, random.Random(11), shadow, FIXED_NOW, 1)
            keys = set(request.context)
            assert not keys & {"class", "klass", "attack_type"}
            assert "attack" not in str(request.context).lower()
            assert case.klass in {"normal", "attack"}      # 標註本身仍在,只是不外流


# --------------------------------------------------------------------------------------
class TestContextIsInert:
    def test_context_does_not_change_the_verdict(self):
        """同一個動作,帶不帶工單脈絡,裁決必須一模一樣。"""
        shadow = ShadowTelecomEnv()
        pipeline = AgentGatePipeline(shadow=shadow, config=GateConfig())
        template = TEMPLATES_BY_ID["refund_duplicate"]

        _, with_ctx = build_case(template, random.Random(2), shadow, FIXED_NOW, 1)
        _, without = build_case(template, random.Random(2), shadow, FIXED_NOW, 1)
        without.context = {}

        a = pipeline.evaluate(with_ctx)
        b = pipeline.evaluate(without)
        assert (a.status, a.risk, a.gate_blocked_at) == (b.status, b.risk, b.gate_blocked_at)
        assert [f.rule_id for f in a.findings] == [f.rule_id for f in b.findings]


# --------------------------------------------------------------------------------------
class TestOpsSimulator:
    def test_warm_start_backfills_a_shift(self):
        sim = _sim()
        created = sim.warm_start(minutes=30, now=FIXED_NOW)
        assert created > 0
        assert sim.records

        summary = sim.summary(FIXED_NOW)
        assert summary["cases"] == len(sim.records)
        assert summary["status_counts"]["executed"] > 0

    def test_backfilled_records_carry_historical_timestamps(self):
        """回填的稽核紀錄要分佈在過去這段時間裡,不能全部擠在寫入的那一秒。"""
        sim = _sim()
        sim.warm_start(minutes=30, now=FIXED_NOW)

        stamps = sorted(r.ts for r in sim.pipeline.audit.records)
        first = datetime.fromisoformat(stamps[0])
        last = datetime.fromisoformat(stamps[-1])
        assert last - first > timedelta(minutes=20)
        assert last <= FIXED_NOW + timedelta(seconds=1)

    def test_hash_chain_survives_a_full_shift(self):
        sim = _sim()
        sim.warm_start(minutes=45, now=FIXED_NOW)
        assert sim.pipeline.audit.verify()["ok"]

    def test_audit_records_point_back_to_their_case(self):
        sim = _sim()
        sim.warm_start(minutes=15, now=FIXED_NOW)
        case_ids = {r.case.case_id for r in sim.records}
        stamped = [r for r in sim.pipeline.audit.records if r.case_id]
        assert stamped, "稽核紀錄必須帶得出來源工單"
        assert {r.case_id for r in stamped} <= case_ids

    def test_no_attack_case_is_ever_executed(self):
        """異常情境可以被攔下、也可以被送去給人簽,但不能自己就過了。"""
        sim = _sim()
        sim.warm_start(minutes=45, now=FIXED_NOW)
        leaked = [r for r in sim.records
                  if r.case.klass == "attack" and r.verdict["status"] == "executed"]
        assert leaked == [], [r.case.template_id for r in leaked]

    def test_traffic_stays_mostly_automatic(self):
        """自動放行率要維持在多數。

        一個把兩成流量丟給主管的治理層沒有人會用 —— 這條測試守的是產品可用性,
        不是安全性。改樣板權重時它會先叫。
        """
        sim = _sim()
        sim.warm_start(minutes=45, now=FIXED_NOW)
        assert sim.summary(FIXED_NOW)["auto_pass_rate"] > 0.6

    def test_simulator_leaves_the_demo_protagonist_alone(self):
        """ACC-1001 是四分鐘劇本的主角,流量模擬不得動到她的帳務狀態。"""
        sim = _sim()
        before = sim.shadow.get_account("ACC-1001").summary()
        sim.warm_start(minutes=45, now=FIXED_NOW)
        assert sim.shadow.get_account("ACC-1001").summary() == before

    def test_auto_resolution_never_touches_demo_approvals(self):
        """劇本送進來的待核件必須留在佇列裡等現場的人按。"""
        from agentgate.demo import build_step_case

        sim = _sim()
        sim.warm_start(minutes=20, now=FIXED_NOW)

        _, request = build_step_case("refund")
        verdict = sim.pipeline.evaluate(request)
        assert verdict.status == "pending_approval"

        sim.advance(FIXED_NOW + timedelta(minutes=30))
        item = sim.pipeline.queue.get(verdict.approval_id)
        assert item.status == "pending", "劇本的待核件被背景邏輯簽掉了"

    def test_pausing_stops_new_cases(self):
        sim = _sim()
        sim.warm_start(minutes=10, now=FIXED_NOW)
        sim.set_running(False, now=FIXED_NOW)

        count = len(sim.records)
        sim.advance(FIXED_NOW + timedelta(minutes=10))
        assert len(sim.records) == count

        sim.set_running(True, now=FIXED_NOW + timedelta(minutes=10))
        sim.advance(FIXED_NOW + timedelta(minutes=20))
        assert len(sim.records) > count

    def test_same_seed_reproduces_the_same_shift(self):
        a, b = _sim(), _sim()
        a.warm_start(minutes=20, now=FIXED_NOW)
        b.warm_start(minutes=20, now=FIXED_NOW)
        assert ([r.case.template_id for r in a.records]
                == [r.case.template_id for r in b.records])

    def test_inject_runs_a_named_scenario_immediately(self):
        sim = _sim()
        record = sim.inject(TEMPLATES_BY_ID["pdf_injection"], now=FIXED_NOW)
        assert record.verdict["status"] == "blocked"
        assert record.verdict["gate_blocked_at"] == "G0"
        assert sim.get_case(record.case.case_id) is record

    def test_advance_does_not_flood_after_a_long_gap(self):
        """離開頁面很久再回來,不該一次補幾百件。"""
        sim = _sim()
        sim.advance(FIXED_NOW)
        sim.advance(FIXED_NOW + timedelta(hours=6))
        assert len(sim.records) <= 40


# --------------------------------------------------------------------------------------
class TestShiftAndSla:
    @pytest.mark.parametrize("hour,expected", [(3, "夜班"), (9, "早班"), (20, "中班")])
    def test_shift_follows_the_clock(self, hour, expected):
        now = datetime(2026, 8, 17, hour, 0, tzinfo=timezone.utc)
        assert current_shift(now)["shift"] == expected

    def test_high_risk_gets_a_tighter_deadline(self):
        """高風險動作卡在佇列本身就是營運損失,時限必須比低風險短。"""
        from agentgate.gates.g4_approval import SLA_SECONDS

        assert SLA_SECONDS["high"] < SLA_SECONDS["medium"] < SLA_SECONDS["low"]

    def test_pending_item_reports_its_sla(self):
        sim = _sim()
        sim.warm_start(minutes=10, now=FIXED_NOW)
        pending = sim.pipeline.queue.pending()
        assert pending, "回填一段班之後佇列不該是空的"

        state = pending[0].sla_state(FIXED_NOW)
        assert state["sla_seconds"] > 0
        assert state["age_seconds"] >= 0
        assert state["breached"] is (state["age_seconds"] > state["sla_seconds"])


# --------------------------------------------------------------------------------------
class TestShadowDirectory:
    def test_synthetic_population_looks_like_a_customer_list(self):
        """外洩表格印出「用戶2317」的話,現場只會看到假資料。"""
        shadow = ShadowTelecomEnv()
        account = shadow.get_account("ACC-2001")
        assert not account.name.startswith("用戶")
        assert account.msisdn.count("-") == 2
        assert "*" in account.id_number and "*" in account.email
        assert account.address

    def test_search_by_account_name_and_msisdn(self):
        shadow = ShadowTelecomEnv()
        assert shadow.search_accounts("ACC-1001")[0].account_id == "ACC-1001"
        assert shadow.search_accounts("林小美")[0].account_id == "ACC-1001"
        assert shadow.search_accounts("0912345678")[0].account_id == "ACC-1001"
        assert shadow.search_accounts("查無此人") == []


# --------------------------------------------------------------------------------------
class TestOpsApi:
    @pytest.fixture
    def client(self) -> TestClient:
        return TestClient(create_app(live_ops=True, warm_minutes=20))

    def test_state_reports_a_running_desk(self, client):
        data = client.get("/api/ops/state").json()
        assert data["enabled"] is True
        assert data["summary"]["cases"] > 0
        assert data["stream"]
        assert data["traffic"]
        assert "影子環境" in data["disclaimer"]

    def test_stream_rows_drill_into_a_case_file(self, client):
        case_id = client.get("/api/ops/state").json()["stream"][0]["case_id"]
        detail = client.get(f"/api/ops/case/{case_id}").json()
        assert detail["case"]["case_id"] == case_id
        assert detail["case"]["turns"]
        assert detail["verdict"]["status"]
        assert detail["audit"], "案件檔案要帶得出它自己的稽核紀錄"

    def test_unknown_case_is_404(self, client):
        assert client.get("/api/ops/case/CS-NOPE").status_code == 404

    def test_inject_named_scenario(self, client):
        res = client.post("/api/ops/inject", json={"template_id": "pdf_injection"})
        assert res.json()["verdict"]["gate_blocked_at"] == "G0"
        assert client.post("/api/ops/inject", json={"template_id": "nope"}).status_code == 404

    def test_stream_can_be_paused_for_the_demo(self, client):
        assert client.post("/api/ops/stream", json={"running": False}).json()["running"] is False
        assert client.get("/api/state").json()["live_ops"] is False
        assert client.post("/api/ops/stream", json={"running": True}).json()["running"] is True

    def test_shadow_backend_is_browsable(self, client):
        listing = client.get("/api/ops/accounts?q=ACC-1001").json()
        assert listing["matched"] == 1
        assert listing["total"] == 600

        detail = client.get("/api/ops/account/ACC-1001").json()
        assert detail["account"]["name"] == "林小美"
        assert client.get("/api/ops/account/ACC-9999").status_code == 404

    def test_pending_queue_is_ordered_by_urgency(self, client):
        pending = client.get("/api/gate/pending").json()["pending"]
        if len(pending) < 2:
            pytest.skip("這一班剛好沒有兩件以上待決")
        breached = [p["sla"]["breached"] for p in pending]
        assert breached == sorted(breached, reverse=True), "逾時的案件必須排在最上面"

    def test_demo_step_returns_its_source_case(self, client):
        """劇本每一步都要帶出來源工單 —— 注入那一步還要帶出附件原文。"""
        step = client.post("/api/demo/step", json={"step_id": "injection"}).json()
        assert step["verdict"]["gate_blocked_at"] == "G0"

        attachment = step["case"]["attachments"][0]
        assert "backup@ext-mail.example" in attachment["lines"][attachment["injected_line"]]

        # 劇本產生的工單也要能用同一個案件檢視端點調閱
        detail = client.get(f"/api/ops/case/{step['case']['case_id']}").json()
        assert detail["case"]["subject"] == step["case"]["subject"]

    def test_reset_returns_to_a_staffed_desk_not_an_empty_one(self, client):
        """重設是「回到剛接班的樣子」,不是「回到從來沒運作過的系統」。"""
        assert client.post("/api/demo/reset", json={}).json()["live_ops"] is True
        assert client.get("/api/ops/state").json()["summary"]["cases"] > 0
        assert client.get("/api/gate/audit/verify").json()["ok"]
