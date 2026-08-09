"""復原計畫產生器：優先級允入控制 ＋ 迭代式壅塞收斂。

這裡是決策的可驗證核心 —— 不是 LLM 生成路徑，而是演算法在容量、延遲、
損失、成本四個約束下求解，再交給孿生推演驗證。LLM 只負責解釋與排序理由。
"""

from __future__ import annotations

from dataclasses import dataclass

from .domain import Action, RecoveryPlan, Service
from .twin.engine import DigitalTwin


@dataclass(frozen=True)
class Strategy:
    key: str
    label: str
    util_target: float        # 鏈路使用率硬上限
    queue_budget: float       # 容許的排隊延遲／基礎延遲比值，決定使用率的軟上限
    cost_weight: float        # 路徑排序時成本的權重（0=只看延遲，1=只看成本）
    protect_priority: int     # 此優先級（含）以內的業務不會被 refine 迴圈犧牲
    quota_pacing: float       # 允許用到「撐完事件的速率」的幾倍（>1 = 透支配額）

    @property
    def rho_cap(self) -> float:
        """由排隊預算反推使用率上限。

        M/M/1 下排隊延遲 = base·ρ/(1−ρ)。要讓它不超過 base 的 K 倍，
        必須 ρ ≤ K/(1+K)。高基礎延遲的鏈路（衛星、壅塞 5G）因此會被
        自動保守分配 —— 這是把「延遲 SLO」翻譯成「容量約束」的關鍵一步。
        """
        return min(self.util_target, self.queue_budget / (1.0 + self.queue_budget))


STRATEGIES = (
    # quota_pacing 是這三個策略真正分歧的地方：
    #   救命優先 —— 為了現在救人，允許把衛星配額用到 2.5 倍速（幾小時後會斷）
    #   均衡     —— 小幅透支
    #   費用優先 —— 嚴格配速，確保撐完整場事件
    # 有了耗竭性資源，「哪個方案比較好」才不再有顯而易見的答案。
    Strategy("protect_critical", "救命優先", util_target=0.80, queue_budget=0.8,
             cost_weight=0.10, protect_priority=1, quota_pacing=2.5),
    Strategy("balanced", "品質與費用均衡", util_target=0.88, queue_budget=1.5,
             cost_weight=0.45, protect_priority=0, quota_pacing=1.25),
    Strategy("lowest_cost", "費用優先", util_target=0.92, queue_budget=3.0,
             cost_weight=0.85, protect_priority=0, quota_pacing=1.0),
)

# key 是稽核與 API 的穩定識別字，label 是給人看的名稱。單一來源，
# 避免前端、CLI、Agent 敘述各自維護一份翻譯而講出不同的方案名。
STRATEGY_LABELS = {s.key: s.label for s in STRATEGIES}

def _quota_cap_mbps(twin: DigitalTwin, link_id: str, pacing: float) -> float:
    """把「剩餘配額要撐完整場事件」換算成這條線路的 Mbps 上限。

    800 GB 的配額配上 120 Mbps 的天線，聽起來很夠 —— 但事件要撐 24 小時的話，
    可持續速率只有 28 Mbps。這個換算就是人工判斷最容易失手的地方：
    眼前看到的是「衛星還有 75% 容量」，看不到的是「照這樣用 5 小時後歸零」。
    """
    link = twin.links[link_id]
    if link.quota_gb is None:
        return float("inf")
    remaining = max(link.quota_gb - twin.quota_used_gb.get(link_id, 0.0), 0.0)
    hours = max(twin.event_hours, 0.1)
    return (remaining / hours) * 1000 * 8 / 3600 * pacing


_MAX_REFINE_ROUNDS = 12
_HOT_LINK_UTIL_PCT = 45.0   # 使用率高於此值才算「壅塞熱點」，值得回收頻寬


def _path_metrics(twin: DigitalTwin, path: list[str]) -> tuple[float, float, float]:
    """(基礎延遲 ms, 成本 元/GB, 瓶頸容量 Mbps)"""
    links = twin.path_links(path)
    if not links:
        return float("inf"), float("inf"), 0.0
    latency = sum(l.base_latency_ms + l.netem_extra_latency_ms for l in links)
    cost = sum(l.cost_per_gb for l in links)
    bottleneck = min(l.effective_capacity_mbps for l in links)
    return latency, cost, bottleneck


def _rank_paths(twin: DigitalTwin, svc: Service, paths: list[list[str]], st: Strategy) -> list[list[str]]:
    """依策略對候選路徑排序：先過濾延遲不可行者，再用成本/延遲加權。"""
    scored: list[tuple[float, list[str]]] = []
    for p in paths:
        latency, cost, bottleneck = _path_metrics(twin, p)
        if bottleneck <= 0:
            continue
        # 基礎延遲已超過 SLO 的路徑直接淘汰（加上排隊只會更差）
        infeasible_penalty = 1000.0 if latency > svc.slo.max_latency_ms else 0.0
        norm_latency = latency / max(svc.slo.max_latency_ms, 1.0)
        norm_cost = cost / 5.0  # 衛星整條約 4.2 元/GB，用 5 做尺度歸一
        score = (1 - st.cost_weight) * norm_latency + st.cost_weight * norm_cost + infeasible_penalty
        scored.append((score, p))
    scored.sort(key=lambda x: x[0])
    return [p for _, p in scored]


def _greedy_allocate(
    twin: DigitalTwin,
    st: Strategy,
    candidate_paths: dict[str, list[list[str]]],
) -> tuple[dict[str, list[str]], dict[str, float]]:
    """兩階段允入控制：先保底，再加碼。

    早期版本是「依優先級一個一個吃到飽」。資源充裕時看不出問題，但只要有
    耗竭性資源（衛星配額），排序第一的 P0 就會把上限吃光，同為 P0 的第二個
    服務拿到零 —— 急診活著、加護病房斷線，這在臨床上完全站不住腳。

    改成電信 QoS 的標準兩階段模型：
      Pass 1（保底 / CIR）受保護服務先各自預留「最低可服務頻寬」，確保沒有人歸零
      Pass 2（加碼 / PIR）再依優先級把剩餘頻寬往上加到完整需求

    容量上限用 st.rho_cap（由排隊預算推導），高延遲鏈路會被自動保守使用；
    再與配額上限取小者 —— 線路開得起，不代表整場事件用得起。
    """
    quota = {lid: _quota_cap_mbps(twin, lid, st.quota_pacing) for lid in twin.links}
    soft = {lid: min(l.effective_capacity_mbps * st.rho_cap, quota[lid])
            for lid, l in twin.links.items()}
    hard = {lid: min(l.effective_capacity_mbps * st.util_target, quota[lid])
            for lid, l in twin.links.items()}
    paths: dict[str, list[str]] = {}
    admitted: dict[str, float] = {}
    ranked = {
        svc.id: _rank_paths(twin, svc, candidate_paths[svc.id], st)
        for svc in twin.services.values()
    }
    order = sorted(twin.services.values(), key=lambda s: s.priority)

    def take(path: list[str], mbps: float) -> None:
        for l in twin.path_links(path):
            soft[l.id] -= mbps
            hard[l.id] -= mbps

    # ---- Pass 1：保底
    for svc in order:
        protected = svc.priority <= st.protect_priority or svc.is_critical
        if not protected:
            continue
        need_min = twin.min_mbps(svc.id)
        # 受保護業務在 soft 排不進去時可動用 hard —— 寧可延遲吃緊也不讓它斷線
        for budget in (soft, hard):
            for path in ranked[svc.id]:
                links = twin.path_links(path)
                if links and min(budget[l.id] for l in links) >= need_min:
                    paths[svc.id] = path
                    admitted[svc.id] = need_min
                    take(path, need_min)
                    break
            if svc.id in admitted:
                break

    # ---- Pass 2：依優先級加碼到完整需求
    for svc in order:
        need_full, need_min = twin.required_mbps(svc.id), twin.min_mbps(svc.id)
        current = admitted.get(svc.id, 0.0)
        path = paths.get(svc.id)

        if path is None:                       # Pass 1 沒配到（非受保護、或塞不下）
            for cand in ranked[svc.id]:
                links = twin.path_links(cand)
                if links and min(soft[l.id] for l in links) >= need_min:
                    path = paths[svc.id] = cand
                    break
        if path is None:
            # 連最低頻寬都排不進去 → 暫停該業務，把頻寬讓給更關鍵者
            paths[svc.id] = ranked[svc.id][0] if ranked[svc.id] else []
            admitted[svc.id] = 0.0
            continue

        links = twin.path_links(path)
        headroom = max(min(soft[l.id] for l in links), 0.0)
        add = min(need_full - current, headroom)
        if current <= 0.0 and add < need_min:  # 加了還是達不到最低服務水準
            admitted[svc.id] = 0.0
            continue
        if add > 0:
            admitted[svc.id] = current + add
            take(path, add)
        else:
            admitted.setdefault(svc.id, current)

    return paths, admitted


def _priority_repair(
    twin: DigitalTwin, paths: dict[str, list[str]], admitted: dict[str, float]
) -> dict[str, float]:
    """消除優先級反轉。

    貪婪裝箱可能出現「HIS（P3，需 10 Mbps）擠不進去，但訪客 Wi-Fi（P4，需 5 Mbps）
    剛好塞得下」的結果 —— 對醫院而言這是不可接受的：災害期間讓訪客上網、
    卻讓批價系統斷線。這裡做兩件事：

    1. 回收：若把共用鏈路上的低優先級頻寬全數收回，足以讓被拒的高優先級業務
       達到最低需求，就執行回收。
    2. 級聯停用：回收後仍無法服務的話，該路徑上所有更低優先級的業務一律停用，
       確保「沒有人比我更該被服務」這個不變式成立。
    """
    admitted = dict(admitted)
    by_priority = sorted(twin.services.values(), key=lambda s: s.priority)

    def link_set(sid: str) -> set[str]:
        return {l.id for l in twin.path_links(paths.get(sid) or [])}

    for svc in by_priority:
        if admitted.get(svc.id, 0.0) > 0.0 or not paths.get(svc.id):
            continue

        victim_links = link_set(svc.id)
        donors = [
            s for s in by_priority
            if s.priority > svc.priority
            and admitted.get(s.id, 0.0) > 0.0
            and victim_links & link_set(s.id)
        ]
        donors.sort(key=lambda s: -s.priority)   # 從最不重要的開始收

        reclaimable = sum(admitted[d.id] for d in donors)
        if reclaimable >= twin.min_mbps(svc.id):
            for d in donors:
                admitted[d.id] = 0.0
            admitted[svc.id] = min(twin.required_mbps(svc.id), reclaimable)
        else:
            # 救不起來 → 至少不能讓更低優先級的業務佔著頻寬
            for d in donors:
                admitted[d.id] = 0.0

    return admitted


def _refine(
    twin: DigitalTwin, st: Strategy, paths: dict[str, list[str]], admitted: dict[str, float]
) -> dict[str, float]:
    """迭代收斂：關鍵業務未達 SLO 時，回收「共用同一條壅塞鏈路」的低優先級頻寬。

    關鍵在於犧牲對象必須與受害者共用熱點鏈路 —— 去限流一個走衛星的行政系統，
    對 5G 上的壅塞毫無幫助。每一輪都真的重新推演，所以計畫的預測值是驗證過的。
    """
    admitted = dict(admitted)
    # 低優先級在前，同級則先犧牲吃頻寬多的
    order = sorted(twin.services.values(), key=lambda s: (-s.priority, -admitted.get(s.id, 0.0)))

    for _ in range(_MAX_REFINE_ROUNDS):
        shadow = twin.clone()
        shadow.routing.paths = dict(paths)
        shadow.routing.admitted = dict(admitted)
        snap = shadow.evaluate("refine")

        failing = [
            sid for sid, sstate in snap.services.items()
            if twin.services[sid].is_critical and not sstate.slo_met and sstate.reachable
        ]
        if not failing:
            break

        # 受害者路徑上、使用率超過門檻的鏈路 = 值得回收頻寬的熱點
        # 熱點 = 使用率高，或者「配額已經逼近上限」。後者很反直覺：
        # 配額綁住時，鏈路使用率可能只有 43%，但規劃上它已經滿了 ——
        # 只看使用率的話，迴圈會誤判成「不是壅塞造成的」而提早放棄。
        hot: set[str] = set()
        for sid in failing:
            for lid in snap.services[sid].link_ids:
                cap = _quota_cap_mbps(twin, lid, st.quota_pacing)
                if (snap.links[lid].utilization_pct >= _HOT_LINK_UTIL_PCT
                        or (cap != float("inf") and snap.links[lid].load_mbps >= cap * 0.9)):
                    hot.add(lid)
        if not hot:
            break  # 延遲問題不是壅塞造成的（例如基礎延遲本身就超標），限流無濟於事

        # 每個業務的頻寬下限：受保護者最多降到最低需求，其餘可完全停用。
        # 這一點很關鍵 —— 壅塞常常是「受保護業務彼此排擠」造成的，
        # 若禁止動它們，迴圈會卡住而讓關鍵業務全數失守。
        def floor_of(s) -> float:
            return 0.0 if s.priority > st.protect_priority else twin.min_mbps(s.id)

        victim = None
        for cand in order:
            cur = admitted.get(cand.id, 0.0)
            if cur <= floor_of(cand) + 1e-6:
                continue                                   # 已在下限，榨不出頻寬
            cand_links = {l.id for l in twin.path_links(paths.get(cand.id) or [])}
            if hot & cand_links:                           # 只犧牲真的擠在同一條鏈路上的業務
                victim = cand
                break
        if victim is None:
            break

        cur = admitted[victim.id]
        floor = floor_of(victim)
        halved = cur * 0.5
        admitted[victim.id] = floor if halved <= floor else halved

    return admitted


def _build_actions(
    twin: DigitalTwin, paths: dict[str, list[str]], admitted: dict[str, float]
) -> list[Action]:
    actions: list[Action] = []
    for sid in sorted(twin.services, key=lambda s: twin.services[s].priority):
        svc = twin.services[sid]
        new_path, new_mbps = paths.get(sid) or [], admitted.get(sid, 0.0)
        old_path = twin.routing.paths.get(sid) or []
        old_mbps = twin.routing.admitted.get(sid, 0.0)

        if new_path and new_path != old_path:
            wan = next(
                (l.kind.value for l in twin.path_links(new_path) if l.kind.value in {"fiber", "5g", "satellite"}),
                "unknown",
            )
            actions.append(Action(
                "reroute", sid, {"path": new_path, "wan": wan},
                rationale=f"{svc.name} 改由 {wan} 承載",
            ))
        if abs(new_mbps - old_mbps) > 0.5:
            kind = "throttle" if new_mbps < old_mbps else "admit"
            actions.append(Action(
                kind, sid, {"mbps": round(new_mbps, 1), "was_mbps": round(old_mbps, 1)},
                rationale=f"優先級 P{svc.priority}：{'降速釋出頻寬' if kind == 'throttle' else '保障關鍵頻寬'}",
            ))
    return actions


def generate_plans(twin: DigitalTwin) -> list[RecoveryPlan]:
    """對當前（故障中）狀態產生多個候選復原計畫，並在影子孿生上推演。"""
    plans: list[RecoveryPlan] = []
    # 鏈路狀態在一次規劃期間不變；三種策略共用候選路徑，避免重跑 NetworkX。
    candidate_paths = {
        service_id: twin.candidate_paths(service_id, k=4)
        for service_id in twin.services
    }
    for idx, st in enumerate(STRATEGIES, start=1):
        paths, admitted = _greedy_allocate(twin, st, candidate_paths)
        admitted = _priority_repair(twin, paths, admitted)
        admitted = _refine(twin, st, paths, admitted)
        actions = _build_actions(twin, paths, admitted)

        plan = RecoveryPlan(
            id=f"plan-{idx:02d}-{st.key}",
            strategy=st.key,
            summary=f"{st.label}：{len(actions)} 個動作，使用率上限 {st.util_target:.0%}",
            actions=actions,
        )
        plan.projected = simulate(twin, plan)
        plans.append(plan)

    return _dedupe(plans)


def simulate(twin: DigitalTwin, plan: RecoveryPlan):
    """在影子孿生推演計畫成效 —— 執行前必經的一步。"""
    shadow = twin.clone()
    shadow.apply_plan(plan)
    return shadow.evaluate(f"projected::{plan.id}")


def score_plan(before, after, plan: RecoveryPlan) -> float:
    """綜合評分：關鍵可用率 60%、整體 SLA 25%、成本效率 15%。"""
    if after is None:
        return 0.0
    crit_gain = after.critical_availability_pct - before.critical_availability_pct
    slo_gain = after.slo_compliance_pct - before.slo_compliance_pct
    cost_penalty = max(0.0, after.monthly_cost_ntd - before.monthly_cost_ntd) / 1000.0
    complexity_penalty = 0.4 * len(plan.actions)
    return 0.60 * crit_gain + 0.25 * slo_gain - 0.15 * cost_penalty - complexity_penalty


def _dedupe(plans: list[RecoveryPlan]) -> list[RecoveryPlan]:
    """動作完全相同的計畫只留一個，避免評審看到三個一樣的方案。"""
    seen: dict[tuple, RecoveryPlan] = {}
    for p in plans:
        key = tuple(sorted((a.type, a.target, str(sorted(a.params.items()))) for a in p.actions))
        if key not in seen:
            seen[key] = p
    return list(seen.values())
