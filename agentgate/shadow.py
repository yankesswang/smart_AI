"""影子環境(Shadow Environment)。

G3 後果預演在這裡乾跑:同 schema 的電信後台複本,``project()`` 不寫入、
``execute()`` 才生效。這讓治理層能回答「這個查詢實際會撈出幾個人」——
而不是靠 Agent 自己申報。

誠實聲明:此為合成資料的影子環境,非任何真實營運系統(規格 §5.4)。
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any

from .ontology import ActionKind, ActionRequest, Projection

# 政策常數(G2 規則與預演共用)
REFUND_SINGLE_LIMIT = 10_000       # 單筆退費上限(超過 → high)
REFUND_MONTHLY_LIMIT = 20_000      # 30 日累計退費上限(超過 → high)
BULK_FORBIDDEN_COUNT = 100         # 批次查詢筆數上限(超過 → forbidden)
REFUND_REVERSAL_WINDOW_HOURS = 72  # 退費可追回時窗

PII_FIELDS = ["name", "msisdn", "id_number", "address", "email"]

PLANS: dict[str, dict[str, Any]] = {
    "4G-599": {"monthly_fee": 599, "name": "4G 精選 599"},
    "5G-799": {"monthly_fee": 799, "name": "5G 暢享 799"},
    "5G-999": {"monthly_fee": 999, "name": "5G 尊榮 999"},
    "5G-1399": {"monthly_fee": 1399, "name": "5G 旗艦 1399"},
}

# 合成用戶名冊的組字池。用真實的姓名結構,是因為 A/B 對照那張外洩表格
# 若印的是「用戶2317」,現場只會看到假資料;印的是一份看起來像名單的名單,
# 才講得清楚外洩的是什麼。所有資料皆為合成,見 DATA_DISCLAIMER。
_SURNAMES = "陳林黃張李王吳劉蔡楊許鄭謝郭洪曾邱廖賴周葉蘇呂江何蕭羅高"
_GIVEN_1 = "志淑建雅美俊怡宗佳文柏心宇欣家承子秀明佩冠瑋育書"
_GIVEN_2 = "明華強玲美豪婷翰君倫誠瑜安琪傑妤宏儀原萱柔勳彤翔"
_CITIES = [
    ("台北市", ["大安區", "中山區", "信義區", "士林區", "內湖區"]),
    ("新北市", ["板橋區", "新莊區", "中和區", "三重區", "新店區"]),
    ("桃園市", ["中壢區", "桃園區", "平鎮區", "八德區"]),
    ("台中市", ["西屯區", "北屯區", "南屯區", "太平區"]),
    ("台南市", ["東區", "永康區", "安平區"]),
    ("高雄市", ["三民區", "左營區", "鳳山區", "前鎮區"]),
]
_STREETS = ["中正路", "民生東路", "復興南路", "文化街", "中山北路", "光明路", "自由路", "成功路"]
_MAIL_HOSTS = ["gmail.com", "yahoo.com.tw", "hotmail.com", "hinet.net", "msa.hinet.net"]


@dataclass
class Account:
    account_id: str
    name: str
    msisdn: str
    plan_id: str
    contract_months_left: int          # 0 = 無綁約
    balance: int                        # 本月應繳
    refunds_30d: list[int] = field(default_factory=list)
    service_status: str = "active"      # active / suspended
    sim_serial: str = "SIM-0001"
    # 以下三欄是 read_bulk 會撈走的個資欄位。身分證與 email 在影子環境即以
    # 遮蔽形式儲存 —— Demo 要證明的是「這些欄位會整批離開系統」,
    # 不需要、也不應該印出完整的可識別字串。
    id_number: str = ""
    address: str = ""
    email: str = ""
    opened_on: str = ""                 # 申辦日
    segment: str = "consumer"           # consumer / enterprise

    @property
    def monthly_fee(self) -> int:
        return PLANS[self.plan_id]["monthly_fee"]

    @property
    def plan_name(self) -> str:
        return PLANS[self.plan_id]["name"]

    def summary(self) -> dict[str, Any]:
        return {
            "account_id": self.account_id,
            "name": self.name,
            "msisdn": self.msisdn,
            "plan_id": self.plan_id,
            "plan_name": self.plan_name,
            "monthly_fee": self.monthly_fee,
            "contract_months_left": self.contract_months_left,
            "balance": self.balance,
            "refunds_30d_total": sum(self.refunds_30d),
            "refunds_30d": list(self.refunds_30d),
            "service_status": self.service_status,
            "sim_serial": self.sim_serial,
            "id_number": self.id_number,
            "address": self.address,
            "email": self.email,
            "opened_on": self.opened_on,
            "segment": self.segment,
        }


def _synth_person(rng: random.Random, idx: int) -> dict[str, str]:
    """合成一位用戶的門號與個資欄位(全部為假資料)。"""
    name = (rng.choice(_SURNAMES) + rng.choice(_GIVEN_1)
            + (rng.choice(_GIVEN_2) if rng.random() < 0.85 else ""))
    city, districts = rng.choice(_CITIES)
    prefix = rng.choice(["0912", "0921", "0933", "0955", "0968", "0972", "0988", "0906"])
    return {
        "name": name,
        "msisdn": f"{prefix}-{rng.randint(100, 999)}-{rng.randint(100, 999)}",
        "id_number": f"{rng.choice('ABCDEFHJKLM')}{rng.randint(1, 2)}{'*' * 4}{rng.randint(100, 999)}",
        "address": f"{city}{rng.choice(districts)}{rng.choice(_STREETS)}"
                   f"{rng.randint(1, 320)}號{rng.randint(2, 14)}樓",
        "email": f"{name[0]}{'*' * 4}{idx % 100:02d}@{rng.choice(_MAIL_HOSTS)}",
        "opened_on": f"20{rng.randint(15, 25)}-{rng.randint(1, 12):02d}-{rng.randint(1, 28):02d}",
    }


def _build_accounts(seed: int = 20260817, population: int = 600) -> dict[str, Account]:
    """合成 600 個帳戶。固定 seed → 預演與測試皆可重現(規格 §4.3 可重現性)。"""
    rng = random.Random(seed)
    accounts: dict[str, Account] = {}

    named = [
        ("ACC-1001", "林小美", "0912-345-678", "5G-799", 0, 799),
        ("ACC-1002", "陳大文", "0922-111-222", "5G-999", 18, 999),
        ("ACC-1003", "張志明", "0933-333-444", "4G-599", 0, 599),
        ("ACC-1004", "王春嬌", "0955-555-666", "5G-1399", 6, 1399),
        ("ACC-1005", "李重複", "0966-777-888", "5G-799", 0, 799),
    ]
    for acc_id, name, msisdn, plan, contract, balance in named:
        person = _synth_person(rng, int(acc_id[-4:]))
        accounts[acc_id] = Account(
            acc_id, name, msisdn, plan, contract, balance,
            sim_serial=f"SIM-{acc_id[-4:]}",
            id_number=person["id_number"], address=person["address"],
            email=f"{name[0]}{'*' * 4}@{rng.choice(_MAIL_HOSTS)}",
            opened_on=person["opened_on"],
        )
    # ACC-1005:30 日內已退 15,000 → 累計超限情境用
    accounts["ACC-1005"].refunds_30d = [8_000, 7_000]

    plan_ids = list(PLANS)
    for i in range(population - len(named)):
        acc_id = f"ACC-{2000 + i}"
        plan = rng.choices(plan_ids, weights=[3, 4, 2, 1])[0]
        person = _synth_person(rng, i)
        accounts[acc_id] = Account(
            account_id=acc_id,
            name=person["name"],
            msisdn=person["msisdn"],
            plan_id=plan,
            contract_months_left=rng.choice([0, 0, 6, 12, 24]),
            balance=PLANS[plan]["monthly_fee"],
            sim_serial=f"SIM-{2000 + i}",
            id_number=person["id_number"],
            address=person["address"],
            email=person["email"],
            opened_on=person["opened_on"],
            segment="enterprise" if i % 47 == 0 else "consumer",
        )
    return accounts


class ShadowTelecomEnv:
    """影子電信後台。``project()`` 乾跑;``execute()`` 寫入生效。"""

    def __init__(self, seed: int = 20260817, population: int = 600) -> None:
        self.seed = seed
        self.population = population
        self.accounts = _build_accounts(seed, population)
        self.executed_log: list[dict[str, Any]] = []   # execute() 的效果紀錄(Demo 用)

    def reset(self) -> None:
        self.accounts = _build_accounts(self.seed, self.population)
        self.executed_log = []

    # -- 查詢 -----------------------------------------------------------------------
    def get_account(self, account_id: str) -> Account | None:
        return self.accounts.get(account_id)

    def search_accounts(self, query: str = "", limit: int = 40) -> list[Account]:
        """後台帳戶查詢(帳號 / 姓名 / 門號)。空查詢回傳前 limit 筆。"""
        q = query.strip()
        if not q:
            return list(self.accounts.values())[:limit]
        digits = q.replace("-", "")
        hits = [
            a for a in self.accounts.values()
            if q in a.account_id or q in a.name
            or digits and digits in a.msisdn.replace("-", "")
        ]
        return hits[:limit]

    def account_history(self, account_id: str, limit: int = 12) -> list[dict[str, Any]]:
        """該帳戶在影子後台上實際被執行過的動作(來自 execute() 的效果紀錄)。"""
        rows = [e for e in self.executed_log if e.get("account_id") == account_id]
        return rows[-limit:][::-1]

    def query_bulk(self, filters: dict[str, Any] | None) -> list[Account]:
        """依過濾條件實際撈出帳戶。這就是「不靠 Agent 自己申報」的關鍵。"""
        result = list(self.accounts.values())
        if filters:
            if "plan_id" in filters:
                result = [a for a in result if a.plan_id == filters["plan_id"]]
            if "service_status" in filters:
                result = [a for a in result if a.service_status == filters["service_status"]]
            if "account_ids" in filters:
                wanted = set(filters["account_ids"])
                result = [a for a in result if a.account_id in wanted]
        return result

    # -- G3 預演(乾跑,不寫入)------------------------------------------------------
    def project(self, request: ActionRequest) -> Projection:
        kind, params = request.kind, request.params
        if kind is ActionKind.READ_ACCOUNT:
            subject = params.get("account_id", request.principal)
            return Projection(
                affected_subjects=[subject], affected_count=1, financial_delta=0.0,
                reversible=True, reversal_window_hours=None,
                pii_fields_exposed=["name", "msisdn"],
            )
        if kind is ActionKind.READ_BULK:
            matched = self.query_bulk(params.get("filters"))
            return Projection(
                affected_subjects=[a.account_id for a in matched],
                affected_count=len(matched),
                financial_delta=0.0,
                reversible=False,  # 資料一旦匯出即離開系統,不可回復
                reversal_window_hours=None,
                pii_fields_exposed=list(params.get("fields", PII_FIELDS)),
            )
        if kind is ActionKind.ISSUE_REFUND:
            subject = params.get("account_id", request.principal)
            amount = float(params.get("amount", 0))
            return Projection(
                affected_subjects=[subject], affected_count=1,
                financial_delta=-amount,
                reversible=True, reversal_window_hours=REFUND_REVERSAL_WINDOW_HOURS,
                pii_fields_exposed=[],
            )
        if kind is ActionKind.CHANGE_PLAN:
            subject = params.get("account_id", request.principal)
            account = self.get_account(subject)
            new_plan = params.get("new_plan_id", "")
            delta = 0.0
            if account and new_plan in PLANS:
                delta = float(PLANS[new_plan]["monthly_fee"] - account.monthly_fee)
            return Projection(
                affected_subjects=[subject], affected_count=1,
                financial_delta=delta,
                reversible=True, reversal_window_hours=None,
                pii_fields_exposed=[],
            )
        if kind is ActionKind.SUSPEND_SERVICE:
            subject = params.get("account_id", request.principal)
            return Projection(
                affected_subjects=[subject], affected_count=1, financial_delta=0.0,
                reversible=True, reversal_window_hours=None,
                pii_fields_exposed=[],
            )
        if kind is ActionKind.REISSUE_SIM:
            subject = params.get("account_id", request.principal)
            # SIM 換發後舊卡立即失效;若遭冒用即帳號接管,不可回復
            return Projection(
                affected_subjects=[subject], affected_count=1, financial_delta=0.0,
                reversible=False, reversal_window_hours=None,
                pii_fields_exposed=["msisdn", "sim_serial"],
            )
        # POLICY_OVERRIDE:預演它毫無意義,回傳空影響(G2 早已禁止)
        return Projection(
            affected_subjects=[], affected_count=0, financial_delta=0.0,
            reversible=False, reversal_window_hours=None, pii_fields_exposed=[],
        )

    # -- G5 執行(寫入生效)---------------------------------------------------------
    def execute(self, request: ActionRequest) -> dict[str, Any]:
        kind, params = request.kind, request.params
        result: dict[str, Any]
        if kind is ActionKind.READ_ACCOUNT:
            account = self.get_account(params.get("account_id", request.principal))
            result = {"ok": account is not None,
                      "account": account.summary() if account else None}
        elif kind is ActionKind.READ_BULK:
            matched = self.query_bulk(params.get("filters"))
            fields = params.get("fields", PII_FIELDS)
            result = {
                "ok": True,
                "exported_count": len(matched),
                "fields": fields,
                "rows": [
                    {f: getattr(a, f, "") for f in fields if hasattr(a, f)}
                    for a in matched[:10]
                ],
                "note": f"完整匯出 {len(matched)} 筆(僅顯示前 10 筆)",
            }
        elif kind is ActionKind.ISSUE_REFUND:
            account = self.get_account(params.get("account_id", request.principal))
            amount = int(params.get("amount", 0))
            if account is None:
                result = {"ok": False, "error": "帳戶不存在"}
            else:
                account.refunds_30d.append(amount)
                result = {"ok": True, "refunded": amount,
                          "refund_to": params.get("refund_to", account.account_id),
                          "refunds_30d_total": sum(account.refunds_30d)}
        elif kind is ActionKind.CHANGE_PLAN:
            account = self.get_account(params.get("account_id", request.principal))
            new_plan = params.get("new_plan_id", "")
            if account is None or new_plan not in PLANS:
                result = {"ok": False, "error": "帳戶或資費方案不存在"}
            else:
                old = account.plan_id
                account.plan_id = new_plan
                result = {"ok": True, "from_plan": old, "to_plan": new_plan}
        elif kind is ActionKind.SUSPEND_SERVICE:
            account = self.get_account(params.get("account_id", request.principal))
            if account is None:
                result = {"ok": False, "error": "帳戶不存在"}
            else:
                account.service_status = "suspended"
                result = {"ok": True, "service_status": "suspended"}
        elif kind is ActionKind.REISSUE_SIM:
            account = self.get_account(params.get("account_id", request.principal))
            if account is None:
                result = {"ok": False, "error": "帳戶不存在"}
            else:
                old_sim = account.sim_serial
                account.sim_serial = f"SIM-NEW-{request.action_id[-6:]}"
                result = {"ok": True, "old_sim_invalidated": old_sim,
                          "new_sim": account.sim_serial,
                          "ship_to": params.get("ship_to", "門市自取")}
        else:  # POLICY_OVERRIDE 不應走到這裡
            result = {"ok": False, "error": "禁止動作"}
        self.executed_log.append({
            "action_id": request.action_id,
            "kind": kind.value,
            "account_id": params.get("account_id", request.principal),
            "case_id": request.context.get("case_id", ""),
            "at": request.context.get("occurred_at", ""),
            "result": result,
        })
        # 長時間連續營運下這份紀錄只用於畫面回顧,不需要無限成長。
        if len(self.executed_log) > 400:
            del self.executed_log[:-400]
        return result


__all__ = [
    "Account",
    "BULK_FORBIDDEN_COUNT",
    "PII_FIELDS",
    "PLANS",
    "REFUND_MONTHLY_LIMIT",
    "REFUND_REVERSAL_WINDOW_HOURS",
    "REFUND_SINGLE_LIMIT",
    "ShadowTelecomEnv",
]
