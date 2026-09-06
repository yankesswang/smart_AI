"""外部驗證層的測試。

這一層的測試要守的東西和 Demo 那邊不一樣。Demo 測的是「閉環有沒有跑完」，
這裡測的是「**這些數字能不能被引用**」，所以守四件事：

1. **資料集不存在時要 skip，不能讓 CI 紅掉**。`data/external/` 不進版控（1 GB 級的檔案），
   所以任何吃資料集的測試都掛 `requires_dataset`。純算術的測試則永遠會跑。
2. **對應層真的對到了**：通道值必須等於原始 CSV 欄位，不能在改名的路上被動過手腳。
   而且缺 vibration 這件事必須是**程式碼裡宣告出來的事實**，不是文件裡的一句話。
3. **指標算對**：混淆矩陣、precision / recall、Top-k 全部拿手算的例子對答案。
   指標算錯的話，上面所有結論都是錯的，而且不會有人發現。
4. **指紋法顯著優於隨機**。這是本次驗證的最低標；沒過的話結論就是方法不成立。
"""

from __future__ import annotations

import math

import pytest

from factory_guardian.validation import ai4i
from factory_guardian.validation.ai4i import (
    CHANNEL_MAPPINGS,
    DERIVED_MAPPINGS,
    DIAGNOSABLE_MODES,
    FAULT_MODES,
    MISSING_SIGNALS,
    NO_FAULT_LABEL,
    STRICT_CHANNELS,
    Sample,
    dataset_available,
    dataset_summary,
    load_samples,
    mapping_table,
)
from factory_guardian.validation.baselines import (
    MajorityBaseline,
    StratifiedRandomBaseline,
    ThresholdRuleBaseline,
    sklearn_available,
)
from factory_guardian.validation.fingerprint import (
    W_DOCS,
    W_PRIOR,
    W_SIGNATURE,
    FingerprintModel,
    Normalizer,
    cosine,
)
from factory_guardian.validation.metrics import (
    classification_report,
    random_top_k,
    top_k_hit_rate,
)
from factory_guardian.validation.runner import (
    ATTRIBUTION_LABELS,
    END_TO_END_LABELS,
    run_validation,
    stratified_folds,
)

requires_dataset = pytest.mark.skipif(
    not dataset_available(),
    reason=f"AI4I 2020 資料集不存在（{ai4i.DATASET_CSV}）；請見 docs/factory_guardian/external_validation.md",
)


# --------------------------------------------------------------------------- 假樣本
def _sample(uid: int, temperature: float, current: float, rpm: float, wear: float, *modes: str) -> Sample:
    """手工樣本。通道值直接給，不經過 CSV，讓演算法測試不依賴資料集。"""
    return Sample(
        uid=uid,
        product_type="L",
        channels={
            "temperature": temperature,
            "current": current,
            "rpm": rpm,
            "wear": wear,
            "thermal_margin": temperature - 300.0,
            "power": current * rpm,
        },
        modes=modes,
        machine_failure=1 if modes else 0,
    )


# =========================================================================== 對應層
class TestSignalMapping:
    def test_every_mapping_carries_a_rationale(self) -> None:
        """對應規則必須附理由。沒有理由的對應在評審面前就是「你自己說了算」。"""
        for m in CHANNEL_MAPPINGS + DERIVED_MAPPINGS:
            assert m.rationale.strip(), f"{m.channel} 缺少對應理由"
            assert len(m.rationale) > 20, f"{m.channel} 的理由太短，不足以支撐追問"

    def test_project_signals_covered_and_vibration_declared_missing(self) -> None:
        """本專案四訊號中，temperature / current / rpm 有對應，vibration 必須被宣告為缺席。"""
        mapped = {m.project_signal for m in CHANNEL_MAPPINGS if m.project_signal}
        assert {"temperature", "current", "rpm"} <= mapped
        assert "vibration" not in mapped

        missing = {name for name, _ in MISSING_SIGNALS}
        assert "vibration" in missing, "缺 vibration 必須是程式碼裡宣告的事實"

    def test_torque_is_declared_a_proxy_not_a_direct_match(self) -> None:
        """扭矩→電流是代理，不是同一個量。標成 direct 會是實質的誇大。"""
        current = next(m for m in CHANNEL_MAPPINGS if m.channel == "current")
        assert current.kind == "proxy"
        assert current.column == "Torque [Nm]"

    def test_derived_channels_are_separated_from_strict(self) -> None:
        """推導通道（ΔT / 功率）正好是 AI4I 的出題規則，必須與嚴格對應分開。"""
        assert all(m.group == "strict" for m in CHANNEL_MAPPINGS)
        assert all(m.group == "derived" for m in DERIVED_MAPPINGS)
        assert set(STRICT_CHANNELS).isdisjoint({m.channel for m in DERIVED_MAPPINGS})

    def test_dataset_is_labelled_synthetic(self) -> None:
        """合成資料的標示不可以被拿掉 —— 這是整個驗證的誠實邊界。"""
        assert ai4i.DATASET_IS_SYNTHETIC is True
        assert "非真實工廠量測" in ai4i.SYNTHETIC_NOTICE
        assert "CC BY 4.0" in ai4i.DATASET_LICENSE

    def test_rnf_is_declared_undiagnosable(self) -> None:
        """RNF 依定義與感測器無關；標成可診斷就等於承諾一件做不到的事。"""
        rnf = next(m for m in FAULT_MODES if m.code == "RNF")
        assert rnf.diagnosable is False
        assert "RNF" not in DIAGNOSABLE_MODES
        assert set(DIAGNOSABLE_MODES) == {"TWF", "HDF", "PWF", "OSF"}

    def test_every_fault_mode_records_its_generating_rule(self) -> None:
        """出題規則要寫出來：這份資料的標籤是規則生成的，不是量測的。"""
        for m in FAULT_MODES:
            assert m.generating_rule.strip(), f"{m.code} 未記錄生成規則"

    def test_mapping_table_reports_the_absent_signal(self) -> None:
        rows = mapping_table()
        absent = [r for r in rows if r["kind"] == "absent"]
        assert absent, "對應表必須把缺席訊號列出來，不能只列有的"
        assert any(r["project_signal"] == "vibration" for r in absent)


@requires_dataset
class TestDatasetLoading:
    def test_counts_match_the_published_dataset(self) -> None:
        """對上 UCI 公佈的統計。對不上就代表我們讀的不是那份資料。"""
        summary = dataset_summary(load_samples())
        assert summary["rows"] == ai4i.EXPECTED_ROWS
        assert summary["machine_failure"] == 339
        assert summary["by_mode"] == {"TWF": 46, "HDF": 115, "PWF": 95, "OSF": 98, "RNF": 19}

    def test_channels_equal_the_raw_columns(self) -> None:
        """改名不能改值。這條擋的是「對應層偷偷做了正規化/縮放」。"""
        for s in load_samples()[:200]:
            assert s.channels["temperature"] == s.raw["Process temperature [K]"]
            assert s.channels["current"] == s.raw["Torque [Nm]"]
            assert s.channels["rpm"] == s.raw["Rotational speed [rpm]"]
            assert s.channels["wear"] == s.raw["Tool wear [min]"]

    def test_derived_channels_follow_the_documented_formula(self) -> None:
        for s in load_samples()[:200]:
            assert s.channels["thermal_margin"] == pytest.approx(
                s.raw["Process temperature [K]"] - s.raw["Air temperature [K]"]
            )
            assert s.channels["power"] == pytest.approx(
                s.raw["Torque [Nm]"] * 2 * math.pi * s.raw["Rotational speed [rpm]"] / 60.0
            )

    def test_label_derivation(self) -> None:
        samples = load_samples()
        assert sum(1 for s in samples if s.is_normal) == 9652
        assert sum(1 for s in samples if len(s.modes) > 1) == 24
        # RNF-only 是 18 筆而不是 19 —— 有一筆同時帶其他模式。這是資料集本身的特性，
        # 記在測試裡是為了避免以後有人「修正」成 19 而不知道自己改壞了什麼。
        assert sum(1 for s in samples if s.is_rnf_only) == 18
        assert all(s.label == NO_FAULT_LABEL for s in samples if s.is_normal)


def test_missing_dataset_raises_with_a_download_hint(tmp_path) -> None:
    """沒有資料集時要給得出下載位置。這條不掛 skip —— 它測的正是缺檔案的路徑。"""
    assert dataset_available(tmp_path / "nope.csv") is False
    with pytest.raises(FileNotFoundError) as exc:
        load_samples(tmp_path / "nope.csv")
    assert ai4i.DATASET_URL in str(exc.value)


# =========================================================================== 指標
class TestMetrics:
    def test_confusion_matrix_and_per_class_scores_are_hand_checkable(self) -> None:
        """手算對答案。

        真實：A A A B B C；預測：A A B B B C
        A: tp=2, fn=1, fp=0 → P=1.000, R=0.667
        B: tp=2, fn=0, fp=1 → P=0.667, R=1.000
        C: tp=1, fn=0, fp=0 → P=1.000, R=1.000
        """
        y_true = ["A", "A", "A", "B", "B", "C"]
        y_pred = ["A", "A", "B", "B", "B", "C"]
        report = classification_report(y_true, y_pred, ("A", "B", "C"))

        assert report.total == 6
        assert report.accuracy == pytest.approx(5 / 6)
        assert report.per_class["A"].precision == pytest.approx(1.0)
        assert report.per_class["A"].recall == pytest.approx(2 / 3)
        assert report.per_class["B"].precision == pytest.approx(2 / 3)
        assert report.per_class["B"].recall == pytest.approx(1.0)
        assert report.per_class["C"].f1 == pytest.approx(1.0)
        assert report.matrix["A"]["B"] == 1
        assert report.macro_f1 == pytest.approx((0.8 + 0.8 + 1.0) / 3)

    def test_confusion_matrix_rows_sum_to_support(self) -> None:
        y_true = ["A", "A", "B"]
        y_pred = ["B", "A", "A"]
        report = classification_report(y_true, y_pred, ("A", "B"))
        for label in ("A", "B"):
            assert sum(report.matrix[label].values()) == report.per_class[label].support

    def test_macro_f1_is_not_dominated_by_the_majority_class(self) -> None:
        """不平衡資料下 accuracy 會騙人，macro-F1 不會。這條就是在守這個選擇。"""
        y_true = ["N"] * 98 + ["F", "F"]
        y_pred = ["N"] * 100
        report = classification_report(y_true, y_pred, ("N", "F"))
        assert report.accuracy == pytest.approx(0.98)
        assert report.macro_f1 < 0.5

    def test_top_k_hit_rate(self) -> None:
        rankings = [["A", "B", "C"], ["C", "B", "A"], ["B", "C", "A"]]
        truths = [("A",), ("A",), ("C",)]
        assert top_k_hit_rate(rankings, truths, 1) == pytest.approx(1 / 3)
        assert top_k_hit_rate(rankings, truths, 2) == pytest.approx(2 / 3)
        assert top_k_hit_rate(rankings, truths, 3) == pytest.approx(1.0)

    def test_top_k_credits_any_true_mode_for_multi_label_rows(self) -> None:
        """AI4I 有 24 筆同時被標多個模式；答對其中一個在現場就是有效診斷。"""
        assert top_k_hit_rate([["B", "A"]], [("A", "B")], 1) == pytest.approx(1.0)

    def test_random_reference_values(self) -> None:
        assert random_top_k(4, 1) == pytest.approx(0.25)
        assert random_top_k(4, 3) == pytest.approx(0.75)
        assert random_top_k(5, 3) == pytest.approx(0.6)


# =========================================================================== 指紋方法
class TestFingerprintMath:
    def test_weights_sum_to_one(self) -> None:
        assert W_SIGNATURE + W_PRIOR + W_DOCS == pytest.approx(1.0)
        assert W_SIGNATURE > W_PRIOR + W_DOCS, "指紋必須主導排名，這是關鍵設計決定 5"

    def test_cosine_basics(self) -> None:
        assert cosine({"a": 1.0, "b": 0.0}, {"a": 2.0, "b": 0.0}) == pytest.approx(1.0)
        assert cosine({"a": 1.0, "b": 0.0}, {"a": 0.0, "b": 1.0}) == pytest.approx(0.0)
        assert cosine({"a": 1.0}, {"a": -1.0}) == pytest.approx(-1.0)

    def test_cosine_of_a_zero_vector_is_zero_not_nan(self) -> None:
        """全部訊號都在正常值時，偏離向量是零向量。回 NaN 會讓整條排序爛掉。"""
        assert cosine({"a": 0.0, "b": 0.0}, {"a": 1.0, "b": 1.0}) == 0.0

    def test_cosine_is_scale_invariant(self) -> None:
        """餘弦只看方向：故障剛開始（訊號弱）與惡化後應該指向同一個候選。"""
        early = {"temperature": 0.4, "current": 0.1}
        late = {"temperature": 4.0, "current": 1.0}
        profile = {"temperature": 2.0, "current": 0.5}
        assert cosine(early, profile) == pytest.approx(cosine(late, profile))

    def test_normalizer_baselines_come_from_normal_rows_only(self) -> None:
        """基準線要是「機器沒事時長什麼樣」。混進故障樣本會稀釋掉偏離量。"""
        samples = [_sample(i, 300.0, 40.0, 1500.0, 100.0) for i in range(50)]
        samples += [_sample(100 + i, 400.0, 40.0, 1500.0, 100.0, "HDF") for i in range(50)]
        norm = Normalizer.fit(samples, STRICT_CHANNELS)
        assert norm.nominal["temperature"] == pytest.approx(300.0)

    def test_normalizer_survives_a_constant_channel(self) -> None:
        samples = [_sample(i, 300.0, 40.0, 1500.0, 100.0) for i in range(10)]
        norm = Normalizer.fit(samples, STRICT_CHANNELS)
        assert norm.scale["temperature"] > 0
        assert all(math.isfinite(v) for v in norm.deviation(samples[0]).values())


class TestFingerprintModel:
    @staticmethod
    def _toy_training_set() -> list[Sample]:
        """兩種可分離的故障：HDF＝溫度單獨升高；PWF＝扭矩升高且轉速下降。"""
        rows: list[Sample] = []
        for i in range(120):
            jitter = (i % 7) * 0.1
            rows.append(_sample(i, 300.0 + jitter, 40.0 + jitter, 1500.0 + i % 5, 100.0 + i % 9))
        for i in range(30):
            rows.append(_sample(1000 + i, 320.0 + i * 0.1, 40.0, 1500.0, 100.0, "HDF"))
        for i in range(30):
            rows.append(_sample(2000 + i, 300.0, 60.0 + i * 0.1, 1300.0, 100.0, "PWF"))
        return rows

    def test_learns_separable_fingerprints(self) -> None:
        model = FingerprintModel(channels=STRICT_CHANNELS).fit(self._toy_training_set())
        assert model.predict(_sample(9001, 330.0, 40.0, 1500.0, 100.0)) == "HDF"
        assert model.predict(_sample(9002, 300.0, 70.0, 1250.0, 100.0)) == "PWF"

    def test_rank_returns_a_full_candidate_set_with_the_no_fault_option(self) -> None:
        model = FingerprintModel(channels=STRICT_CHANNELS).fit(self._toy_training_set())
        ranked = model.rank(_sample(9003, 330.0, 40.0, 1500.0, 100.0))
        ids = [c.fault_id for c in ranked]
        assert NO_FAULT_LABEL in ids, "候選集大小要固定，Top-k 才可比"
        assert len(ids) == len(set(ids)), "候選不可重複"
        combined = [c.combined for c in ranked if c.fault_id != NO_FAULT_LABEL]
        assert combined == sorted(combined, reverse=True)

    def test_no_fault_gate_fires_on_a_healthy_sample(self) -> None:
        """訊號全在正常範圍時，最誠實的答案是「沒有故障徵兆」，不是硬挑一個最像的。

        探針直接取正常運轉點（normalizer 學到的 nominal），偏離向量因此是零向量 ——
        這是「完全健康」在這個方法裡的定義，不用手寫近似值去猜。
        """
        model = FingerprintModel(channels=STRICT_CHANNELS).fit(self._toy_training_set())
        assert model.normalizer is not None
        nominal = model.normalizer.nominal
        healthy = _sample(
            9004, nominal["temperature"], nominal["current"], nominal["rpm"], nominal["wear"]
        )
        assert model.strength_of(healthy) == pytest.approx(0.0, abs=1e-9)
        assert model.predict(healthy) == NO_FAULT_LABEL

    def test_confidence_is_suppressed_when_the_signal_is_weak(self) -> None:
        """早期劣化不該給高信心 —— 對應 Orchestrator 的 confirm_diagnosis。"""
        model = FingerprintModel(channels=STRICT_CHANNELS).fit(self._toy_training_set())
        weak = model.rank(_sample(9005, 303.0, 40.0, 1500.0, 100.0))
        strong = model.rank(_sample(9006, 340.0, 40.0, 1500.0, 100.0))
        weak_hdf = next(c.confidence for c in weak if c.fault_id == "HDF")
        strong_hdf = next(c.confidence for c in strong if c.fault_id == "HDF")
        assert weak_hdf < strong_hdf

    def test_thresholds_are_calibrated_from_training_data_only(self) -> None:
        model = FingerprintModel(channels=STRICT_CHANNELS).fit(self._toy_training_set())
        assert model.no_fault_threshold > 0
        assert model.strength_full >= model.no_fault_threshold

    def test_is_deterministic(self) -> None:
        train = self._toy_training_set()
        probe = _sample(9007, 325.0, 45.0, 1450.0, 110.0)
        a = FingerprintModel(channels=STRICT_CHANNELS).fit(train).rank(probe)
        b = FingerprintModel(channels=STRICT_CHANNELS).fit(train).rank(probe)
        assert [c.fault_id for c in a] == [c.fault_id for c in b]
        assert [round(c.combined, 9) for c in a] == [round(c.combined, 9) for c in b]


class TestBaselines:
    def test_threshold_rule_picks_one_channel_per_mode(self) -> None:
        """這個對照組的定義就是「一次只看一個訊號」。多看一個就不是現行流程了。"""
        model = ThresholdRuleBaseline(channels=STRICT_CHANNELS).fit(
            TestFingerprintModel._toy_training_set()
        )
        assert {r.fault_id for r in model.rules} == {"HDF", "PWF"}
        assert all(r.channel in STRICT_CHANNELS for r in model.rules)
        assert all(r.direction in (1, -1) for r in model.rules)

    def test_threshold_rule_reports_no_fault_when_nothing_fires(self) -> None:
        model = ThresholdRuleBaseline(channels=STRICT_CHANNELS).fit(
            TestFingerprintModel._toy_training_set()
        )
        assert model.predict(_sample(9100, 300.0, 40.0, 1500.0, 100.0)) == NO_FAULT_LABEL

    def test_majority_baseline_learns_the_majority_label(self) -> None:
        model = MajorityBaseline().fit(TestFingerprintModel._toy_training_set())
        assert model.label == NO_FAULT_LABEL

    def test_random_baseline_is_reproducible(self) -> None:
        train = TestFingerprintModel._toy_training_set()
        probe = _sample(9200, 310.0, 45.0, 1400.0, 120.0)
        a = StratifiedRandomBaseline(seed=7).fit(train).rank(probe)
        b = StratifiedRandomBaseline(seed=7).fit(train).rank(probe)
        assert a == b, "報告裡的隨機 baseline 數字必須可重現"


# =========================================================================== 切分
class TestFolds:
    def test_every_sample_gets_exactly_one_fold(self) -> None:
        samples = TestFingerprintModel._toy_training_set()
        folds = stratified_folds(samples, n_folds=5, seed=1)
        assert len(folds) == len(samples)
        assert set(folds) == {0, 1, 2, 3, 4}

    def test_folds_are_stratified(self) -> None:
        """每個 fold 都要有各類樣本；否則某些 fold 的 per-class recall 是無定義的。"""
        samples = TestFingerprintModel._toy_training_set()
        folds = stratified_folds(samples, n_folds=5, seed=1)
        for fold in range(5):
            labels = {s.label for s, f in zip(samples, folds) if f == fold}
            assert {"HDF", "PWF", NO_FAULT_LABEL} <= labels

    def test_split_is_deterministic(self) -> None:
        samples = TestFingerprintModel._toy_training_set()
        assert stratified_folds(samples, 5, 42) == stratified_folds(samples, 5, 42)


# =========================================================================== 端到端
@requires_dataset
class TestValidationRun:
    """真的跑一次外部驗證。折數壓到 2、關掉 ablation 與學習曲線是為了測試速度；
    文件裡引用的數字用預設的 5 折（`python -m factory_guardian.validation`）產生。"""

    @pytest.fixture(scope="class")
    def report(self):
        return run_validation(n_folds=2, with_ablations=False, with_learning_curve=False)

    def test_dataset_summary_is_reported(self, report) -> None:
        assert report.dataset["rows"] == ai4i.EXPECTED_ROWS
        assert report.config["sklearn"] == sklearn_available()

    def test_fingerprint_beats_random_by_a_wide_margin(self, report) -> None:
        """**本次驗證的最低標。** 沒過就代表指紋法在外部資料上不成立。"""
        fp = next(r for r in report.attribution if r.method == "fingerprint_cosine")
        chance = random_top_k(len(ATTRIBUTION_LABELS), 1)
        assert fp.top1 > chance * 2, f"Top-1 {fp.top1:.3f} 未達隨機 {chance:.3f} 的兩倍"
        assert fp.top3 > random_top_k(len(ATTRIBUTION_LABELS), 3)
        assert fp.report.macro_f1 > 0.5

    def test_fingerprint_beats_the_single_signal_threshold_rule(self, report) -> None:
        """對上「現行流程」。輸給它的話，這套系統就沒有存在的理由。"""
        fp = next(r for r in report.attribution if r.method == "fingerprint_cosine")
        rule = next(r for r in report.attribution if r.method == "threshold_rule")
        assert fp.top1 > rule.top1
        assert fp.report.macro_f1 > rule.report.macro_f1

    def test_fingerprint_beats_both_floor_baselines(self, report) -> None:
        fp = next(r for r in report.attribution if r.method == "fingerprint_cosine")
        for floor in ("majority", "stratified_random"):
            other = next(r for r in report.attribution if r.method == floor)
            assert fp.top1 > other.top1

    def test_every_diagnosable_mode_is_scored(self, report) -> None:
        """四個模式都要有 precision / recall，不能只報好看的那幾個。"""
        fp = next(r for r in report.attribution if r.method == "fingerprint_cosine")
        for code in DIAGNOSABLE_MODES:
            metrics = fp.report.per_class[code]
            assert metrics.support > 0
            assert 0.0 <= metrics.precision <= 1.0
            assert 0.0 <= metrics.recall <= 1.0

    def test_attribution_excludes_the_no_fault_option_for_every_method(self, report) -> None:
        """歸因任務對所有方法一視同仁地移除 no-fault，否則比較不公平。"""
        for result in report.attribution:
            assert NO_FAULT_LABEL not in result.report.labels
        assert NO_FAULT_LABEL in END_TO_END_LABELS

    def test_confusion_matrix_rows_account_for_every_sample(self, report) -> None:
        """每一列加總 = 該類樣本數。對不上就代表有預測被默默吞掉了。"""
        fp = next(r for r in report.attribution if r.method == "fingerprint_cosine")
        for label in fp.report.labels:
            assert sum(fp.report.matrix[label].values()) == fp.report.per_class[label].support

    def test_sklearn_absence_is_reported_not_hidden(self, report) -> None:
        """缺 sklearn 時必須在報告裡講出來，不能靜默跳過對照組。"""
        joined = " ".join(report.notes)
        assert ("scikit-learn" in joined) or ("sklearn" in joined)
        if not sklearn_available():
            assert "未執行" in joined

    def test_report_serialises(self, report) -> None:
        import json

        payload = report.to_dict()
        assert json.loads(json.dumps(payload, ensure_ascii=False))
        assert payload["dataset"]["rows"] == ai4i.EXPECTED_ROWS

    def test_markdown_carries_the_synthetic_notice(self, report) -> None:
        """報表本身要帶著誠實邊界。有人只複製表格時，那句話得跟著走。"""
        text = report.to_markdown()
        assert ai4i.SYNTHETIC_NOTICE in text
        assert "CC BY 4.0" in text


# =========================================================================== 多原型
class TestMultiPrototype:
    """`--prototypes N` 與 `Candidate.prototype`。

    這一組守的核心是「預設不動既有數字」：`docs/factory_guardian/external_validation.md` §7 的
    0.724 / 0.706 全部綁在單一原型上，預設值一改，別人重跑就對不起來了。
    """

    def test_default_is_one_prototype(self) -> None:
        from factory_guardian.validation.runner import DEFAULT_PROTOTYPES

        assert DEFAULT_PROTOTYPES == 1
        assert FingerprintModel().n_prototypes == 1

    def test_single_prototype_reports_index_zero(self) -> None:
        model = FingerprintModel(channels=STRICT_CHANNELS).fit(
            TestFingerprintModel._toy_training_set()
        )
        ranked = model.rank(_sample(9300, 330.0, 40.0, 1500.0, 100.0))
        assert all(c.prototype == 0 for c in ranked)

    def test_two_prototypes_split_a_two_sided_fault(self) -> None:
        """雙側故障（同一標籤、兩個相反方向）是單一原型結構上做不到的事。

        造一個 PWF 樣的類別：一半功率過高（扭矩升、轉速降），一半功率過低（扭矩降、轉速升）。
        單一質心會落在兩簇中間、方向失去意義；兩個原型才抓得回來。
        """
        rows: list[Sample] = []
        for i in range(120):
            rows.append(_sample(i, 300.0 + (i % 5) * 0.1, 40.0, 1500.0 + i % 5, 100.0))
        for i in range(40):
            rows.append(_sample(1000 + i, 300.0, 60.0 + i * 0.05, 1300.0, 100.0, "PWF"))
        for i in range(40):
            rows.append(_sample(2000 + i, 300.0, 20.0 - i * 0.05, 1700.0, 100.0, "PWF"))
        for i in range(40):
            rows.append(_sample(3000 + i, 330.0 + i * 0.1, 40.0, 1500.0, 100.0, "HDF"))

        high = _sample(9400, 300.0, 70.0, 1250.0, 100.0)
        low = _sample(9401, 300.0, 15.0, 1750.0, 100.0)

        single = FingerprintModel(channels=STRICT_CHANNELS, n_prototypes=1).fit(rows)
        double = FingerprintModel(channels=STRICT_CHANNELS, n_prototypes=2).fit(rows)

        def pwf_cosine(model: FingerprintModel, sample: Sample) -> float:
            return next(c.cosine for c in model.rank(sample) if c.fault_id == "PWF")

        # 結構性主張：單一質心落在兩個相反方向的簇中間，方向失去意義 —— 餘弦掉到接近 0。
        # 兩個原型則各自對準一側，兩個極端都拿得到接近 1 的餘弦。
        # 用餘弦而不是 predict() 來斷言，是因為 predict() 還受競爭類別影響，
        # 那會讓這個測試在測「多原型有沒有用」之外多測了一件事。
        for probe in (high, low):
            assert abs(pwf_cosine(single, probe)) < 0.5
            assert pwf_cosine(double, probe) > 0.9
        assert double.predict(high) == "PWF" and double.predict(low) == "PWF"
        # 兩個極端必須命中**不同**的原型，否則 k-means 根本沒把兩簇分開。
        assert {c.prototype for c in double.rank(high) if c.fault_id == "PWF"} != {
            c.prototype for c in double.rank(low) if c.fault_id == "PWF"
        }

    def test_prototype_choice_is_deterministic(self) -> None:
        """命中哪個原型會進稽核輸出，所以它必須可重現。"""
        rows = TestFingerprintModel._toy_training_set()
        probe = _sample(9402, 325.0, 45.0, 1450.0, 110.0)
        a = FingerprintModel(channels=STRICT_CHANNELS, n_prototypes=2).fit(rows).rank(probe)
        b = FingerprintModel(channels=STRICT_CHANNELS, n_prototypes=2).fit(rows).rank(probe)
        assert [(c.fault_id, c.prototype) for c in a] == [(c.fault_id, c.prototype) for c in b]


# =========================================================================== AUC 指標
class TestAucMath:
    """AUC / pAUC 的手寫實作。指標算錯的話，CWRU 那份文件整份都是錯的且不會有人發現。"""

    def test_perfect_and_inverted_separation(self) -> None:
        from factory_guardian.validation.metrics import roc_auc

        assert roc_auc([0, 0, 1, 1], [0.1, 0.2, 0.8, 0.9]) == pytest.approx(1.0)
        assert roc_auc([1, 1, 0, 0], [0.1, 0.2, 0.8, 0.9]) == pytest.approx(0.0)

    def test_ties_count_as_half(self) -> None:
        """全部同分 = 完全沒有鑑別力 = 0.5。這條錯了，AUC 會在平手時虛高。"""
        from factory_guardian.validation.metrics import roc_auc

        assert roc_auc([0, 1, 0, 1], [1.0, 1.0, 1.0, 1.0]) == pytest.approx(0.5)
        # 手算：pos={1,1}, neg={1,0} → (1 + 0.5)/2 = 0.75
        assert roc_auc([1, 1, 0, 0], [1.0, 1.0, 1.0, 0.0]) == pytest.approx(0.75)

    def test_partial_auc_is_mcclish_standardised(self) -> None:
        """pAUC 必須與 `acoustics/detector.py`（sklearn max_fpr）同一把尺，
        否則 `docs/factory_guardian/cwru_validation.md` 的 pAUC 不能和 `docs/factory_guardian/acoustic_validation.md` 比較。"""
        from factory_guardian.validation.metrics import partial_auc

        assert partial_auc([0, 0, 1, 1], [0.1, 0.2, 0.8, 0.9], 0.1) == pytest.approx(1.0)
        # 完全隨機（全部同分）在 McClish 標準化下是 0.5。
        assert partial_auc([0, 1, 0, 1], [1.0, 1.0, 1.0, 1.0], 0.1) == pytest.approx(0.5)

    def test_matches_sklearn_when_available(self) -> None:
        import random

        from factory_guardian.validation.metrics import partial_auc, roc_auc

        if not sklearn_available():
            pytest.skip("scikit-learn 未安裝")
        from sklearn.metrics import roc_auc_score

        rng = random.Random(20260809)
        for _ in range(5):
            labels = [rng.randint(0, 1) for _ in range(200)]
            scores = [round(rng.gauss(y * 0.8, 1.0), 2) for y in labels]
            assert roc_auc(labels, scores) == pytest.approx(roc_auc_score(labels, scores))
            assert partial_auc(labels, scores, 0.1) == pytest.approx(
                roc_auc_score(labels, scores, max_fpr=0.1)
            )


@requires_dataset
class TestPrototypeRun:
    """真的用 `--prototypes 2` 跑一次，確認 CLI 這條路徑是通的、而且真的變好。"""

    def test_two_prototypes_beat_one_on_ai4i(self) -> None:
        one = run_validation(n_folds=2, with_ablations=False, with_learning_curve=False)
        two = run_validation(
            n_folds=2, with_ablations=False, with_learning_curve=False, n_prototypes=2
        )
        fp1 = next(r for r in one.attribution if r.method == "fingerprint_cosine")
        fp2 = next(r for r in two.attribution if r.method == "fingerprint_cosine")
        assert one.config["n_prototypes"] == 1
        assert two.config["n_prototypes"] == 2
        # §8.2 的主張：多原型解決的是雙側故障 PWF，所以 PWF recall 一定要上去。
        assert fp2.report.per_class["PWF"].recall > fp1.report.per_class["PWF"].recall
        assert fp2.top1 > fp1.top1
