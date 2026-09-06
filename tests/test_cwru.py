"""CWRU 軸承振動外部驗證層的測試。

守的東西和 `test_validation.py` 一致，但多一條這份資料集特有的：

1. **資料不存在時要 skip，不能讓 CI 紅掉**。`data/external/` 不進版控（100 MB 級），
   所以任何吃資料的測試都掛 `requires_dataset`。純算術的測試永遠會跑。
2. **對應層真的有理由**：每一條 CWRU 特徵 → 專案訊號的對應都必須附得起追問的說明，
   而且「本專案沒有這個量」必須是程式碼裡宣告出來的事實，不是文件裡的一句話。
3. **誠實邊界是程式裡的常數**：`DATASET_IS_SYNTHETIC = False`（真實量測）
   與 `REALITY_NOTICE`（但它是實驗台）必須同時成立 —— 少了後者，這份驗證
   最容易被誤讀成「已在真實產線驗證軸承劣化」。
4. **48 kHz 陷阱**：正常基準檔的取樣率和故障檔不同。不對齊的話「正常 vs 故障」
   會被取樣率本身分開，AUC 會漂亮得不像話 —— 而且沒有人看得出來哪裡錯了。
   這是本模組最容易產生假數字的地方，所以測試守得最緊。
"""

from __future__ import annotations

import math

import pytest

from factory_guardian.validation import cwru
from factory_guardian.validation.cwru import (
    BANDS,
    FAULT_CLASSES,
    FEATURE_MAPPINGS,
    FEATURES,
    NORMAL_DECIMATION,
    NORMAL_SAMPLE_RATE_HZ,
    RECORDINGS,
    SAMPLE_RATE_HZ,
    WINDOW,
    FingerprintModel,
    Normalizer,
    Window,
    dataset_available,
    extract_features,
    load_windows,
    mapping_table,
    missing_files,
    run_validation,
)

requires_dataset = pytest.mark.skipif(
    not dataset_available(),
    reason=(
        f"CWRU 資料不存在（{cwru.DATASET_DIR}）；"
        "取得指令：python3 -m factory_guardian.validation.cwru --download"
        "（另見 docs/factory_guardian/cwru_validation.md）"
    ),
)


def _window(uid: int, fault_class: str = "normal", **features: float) -> Window:
    """手工樣本。特徵值直接給，不經過 .mat，讓演算法測試不依賴資料集。"""
    base = {name: 0.0 for name in FEATURES}
    base.update(features)
    return Window(
        uid=uid,
        recording=0,
        fault_class=fault_class,
        fault_size_in=0.0 if fault_class == "normal" else 0.007,
        load_hp=0,
        features=base,
    )


# =========================================================================== 誠實邊界
class TestHonestBoundaries:
    def test_dataset_is_declared_real_measurement_not_synthetic(self) -> None:
        """CWRU 是真實加速規量測。這一點與 AI4I 正好相反，不可混為一談。"""
        from factory_guardian.validation.ai4i import DATASET_IS_SYNTHETIC as AI4I_SYNTHETIC

        assert cwru.DATASET_IS_SYNTHETIC is False
        assert AI4I_SYNTHETIC is True

    def test_the_test_rig_caveat_travels_with_the_numbers(self) -> None:
        """「真實量測」最危險的誤讀是「真實產線」。這句提醒必須跟著報表走，
        有人只複製表格時它也跟著走。"""
        notice = cwru.REALITY_NOTICE
        assert "實驗台" in notice
        assert "人工" in notice
        assert "非" in notice and "產線" in notice

    def test_license_does_not_claim_commercial_use(self) -> None:
        """CWRU 沒有附 CC 類授權。把它寫成「可商用」是最容易出事的一種樂觀 ——
        AI4I 那條線可以（CC BY 4.0），這條不行，兩者不可混為一談。"""
        from factory_guardian.validation.ai4i import DATASET_LICENSE as AI4I_LICENSE

        assert "可商用" in AI4I_LICENSE
        assert "可商用" not in cwru.DATASET_LICENSE
        assert "確認授權" in cwru.DATASET_LICENSE

    def test_normal_recordings_are_declared_48k(self) -> None:
        """48 kHz 陷阱：正常檔與故障檔的取樣率不同，且降採樣倍率必須整除。"""
        normals = [r for r in RECORDINGS if r.is_normal]
        faults = [r for r in RECORDINGS if not r.is_normal]
        assert normals and faults
        assert all(r.sample_rate_hz == NORMAL_SAMPLE_RATE_HZ for r in normals)
        assert all(r.sample_rate_hz == SAMPLE_RATE_HZ for r in faults)
        assert NORMAL_DECIMATION == NORMAL_SAMPLE_RATE_HZ // SAMPLE_RATE_HZ
        assert NORMAL_SAMPLE_RATE_HZ == NORMAL_DECIMATION * SAMPLE_RATE_HZ

    def test_bearing_degradation_gap_is_the_reason_this_module_exists(self) -> None:
        """三個 CWRU 故障類別全部落在專案的 `bearing_degradation` 底下 ——
        那正是 AI4I 驗證不到的那一個。"""
        from factory_guardian.twin.faults import FAULTS

        assert "bearing_degradation" in FAULTS
        assert set(FAULT_CLASSES) == {"inner_race", "ball", "outer_race"}
        assert cwru.NO_FAULT_LABEL not in FAULT_CLASSES


# =========================================================================== 對應層
class TestFeatureMapping:
    def test_every_mapping_carries_a_rationale(self) -> None:
        for m in FEATURE_MAPPINGS:
            assert m.rationale.strip(), f"{m.feature} 沒有對應理由"
            assert m.kind in {"direct", "analog", "absent"}

    def test_only_rms_maps_directly_to_a_project_signal(self) -> None:
        """本專案的 Twin 只輸出一個 vibration RMS 純量。誇大對應關係等於誇大驗證覆蓋範圍。"""
        direct = [m for m in FEATURE_MAPPINGS if m.kind == "direct"]
        assert [m.feature for m in direct] == ["rms"]
        assert direct[0].project_signal == "vibration"
        assert sum(1 for m in FEATURE_MAPPINGS if m.project_signal) == 1

    def test_analog_mappings_point_at_real_acoustic_indicators(self) -> None:
        """對應到聲學指標的那幾條，指標名稱必須真的存在於 `acoustics/signatures.py`。"""
        from factory_guardian.acoustics.signatures import INDICATOR_NAMES

        for m in FEATURE_MAPPINGS:
            if m.acoustic_indicator:
                assert m.acoustic_indicator in INDICATOR_NAMES, m.feature

    def test_features_and_bands_are_consistent(self) -> None:
        assert FEATURES == tuple(m.feature for m in FEATURE_MAPPINGS)
        assert {name for name, _, _ in BANDS} <= set(FEATURES)
        # 頻帶不可重疊、不可留縫，且上界不得超過 Nyquist。
        edges = [(lo, hi) for _, lo, hi in BANDS]
        assert edges == sorted(edges)
        for (_, hi), (lo, _) in zip(edges, edges[1:]):
            assert hi == lo
        assert edges[-1][1] <= SAMPLE_RATE_HZ / 2

    def test_mapping_table_reports_the_absent_quantities(self) -> None:
        """文件表由程式產生，「專案沒有這個量」必須看得見。"""
        table = mapping_table()
        assert "（無）" in table
        assert "absent" in table
        assert "`rms`" in table


# =========================================================================== 特徵
class TestFeatureMath:
    def test_rms_and_crest_factor_of_a_sine(self) -> None:
        """正弦波：RMS = A/√2、峰值因數 = √2、峭度 = −1.5。三個都是手算得出來的常數。"""
        signal = [math.sin(2 * math.pi * 60 * i / SAMPLE_RATE_HZ) for i in range(WINDOW)]
        f = extract_features(signal)
        assert f["rms"] == pytest.approx(1 / math.sqrt(2), abs=1e-3)
        # 容差放到 0.05：4096 個取樣點不會剛好落在波峰上，離散取樣本來就抓不到真峰值。
        assert f["crest_factor"] == pytest.approx(math.sqrt(2), abs=0.05)
        assert f["kurtosis"] == pytest.approx(-1.5, abs=1e-2)

    def test_band_ratios_sum_to_one(self) -> None:
        import random

        rng = random.Random(20260809)
        f = extract_features([rng.gauss(0.0, 1.0) for _ in range(WINDOW)])
        assert sum(f[name] for name, _, _ in BANDS) == pytest.approx(1.0, abs=1e-6)

    def test_band_ratio_follows_the_tone_frequency(self) -> None:
        """把純音放進某一個頻帶，該頻帶的能量佔比就該逼近 1。頻率軸算錯的話這條會爆。"""
        for name, lo, hi in BANDS:
            centre = (lo + hi) / 2 or 250.0
            tone = [math.sin(2 * math.pi * centre * i / SAMPLE_RATE_HZ) for i in range(WINDOW)]
            assert extract_features(tone)[name] > 0.9, name

    def test_impulses_raise_kurtosis_without_raising_rms_much(self) -> None:
        """軸承局部凹坑的物理：能量沒怎麼變、波形變尖。RMS 看不到、峭度看得到 ——
        這正是「多看幾個指標」在振動上的價值，也是專案主張的核心。"""
        import random

        rng = random.Random(7)
        smooth = [rng.gauss(0.0, 1.0) for _ in range(WINDOW)]
        spiky = list(smooth)
        for i in range(0, WINDOW, 512):
            spiky[i] += 8.0
        a, b = extract_features(smooth), extract_features(spiky)
        assert b["kurtosis"] > a["kurtosis"] + 2.0
        assert b["rms"] < a["rms"] * 1.5

    def test_zero_signal_does_not_produce_nan(self) -> None:
        f = extract_features([0.0] * WINDOW)
        assert all(math.isfinite(v) for v in f.values())


# =========================================================================== 正規化 / 模型
class TestModels:
    @staticmethod
    def _train() -> list[Window]:
        """三類可分離的故障 + 一批正常樣本。"""
        rows = [_window(i, "normal", rms=0.1 + (i % 5) * 0.001) for i in range(60)]
        for i in range(30):
            rows.append(_window(100 + i, "inner_race", rms=0.5 + i * 0.01, kurtosis=4.0))
            rows.append(_window(200 + i, "ball", rms=0.3 + i * 0.01, crest_factor=6.0))
            rows.append(_window(300 + i, "outer_race", rms=0.6 + i * 0.01, band_3000_6000=0.8))
        return rows

    def test_normalizer_baselines_come_from_normal_windows_only(self) -> None:
        """基準線的定義就是「這台機器沒事時長什麼樣」。混進故障樣本會稀釋掉偏離量。"""
        norm = Normalizer.fit(self._train())
        assert norm.nominal["rms"] == pytest.approx(0.1 + (sum(i % 5 for i in range(60)) / 60) * 0.001)

    def test_normalizer_survives_a_constant_feature(self) -> None:
        norm = Normalizer.fit([_window(i, "normal", rms=0.1) for i in range(10)])
        assert norm.scale["rms"] > 0
        assert all(math.isfinite(v) for v in norm.deviation(_window(99, "normal", rms=0.2)).values())

    def test_strength_is_zero_at_the_nominal_point(self) -> None:
        """完全健康 = 零偏離向量。這就是 `diagnosis.py` 的 no-fault gate 用的那個量。"""
        norm = Normalizer.fit(self._train())
        healthy = _window(9000, "normal", **norm.nominal)
        assert Normalizer.strength(norm.deviation(healthy)) == pytest.approx(0.0, abs=1e-9)

    def test_fingerprint_learns_separable_classes(self) -> None:
        model = FingerprintModel().fit(self._train())
        assert set(model.prototypes) == set(FAULT_CLASSES)
        assert model.predict(_window(9001, "inner_race", rms=0.7, kurtosis=4.5)) == "inner_race"
        assert model.predict(_window(9002, "ball", rms=0.4, crest_factor=6.5)) == "ball"

    def test_fingerprint_rank_is_a_full_permutation(self) -> None:
        model = FingerprintModel().fit(self._train())
        ranked = model.rank(_window(9003, "inner_race", rms=0.7, kurtosis=4.5))
        assert sorted(ranked) == sorted(FAULT_CLASSES)

    def test_fingerprint_is_deterministic(self) -> None:
        train = self._train()
        probe = _window(9004, "ball", rms=0.45, crest_factor=5.0, kurtosis=1.0)
        a = FingerprintModel(n_prototypes=2).fit(train).rank(probe)
        b = FingerprintModel(n_prototypes=2).fit(train).rank(probe)
        assert a == b, "報告裡的數字必須可重現"


# =========================================================================== 資料層
class TestDataset:
    def test_mat_key_rule_handles_the_duplicated_variable_file(self) -> None:
        """`99.mat` 內同時含 `X098_*` 與 `X099_*`（上游檔案的瑕疵）。
        用命名規則而不是「取第一個變數」，才不會安靜地拿到別段錄音。"""
        by_id = {r.file_id: r for r in RECORDINGS}
        assert by_id[99].mat_key == "X099_DE_time"
        assert by_id[105].mat_key == "X105_DE_time"

    def test_every_load_covers_every_class(self) -> None:
        """leave-one-load-out 要成立，每個負載都必須有全部四類；否則某個 fold 是無定義的。"""
        for load in cwru.LOADS:
            classes = {r.fault_class for r in RECORDINGS if r.load_hp == load}
            assert classes == {cwru.NO_FAULT_LABEL, *FAULT_CLASSES}, load

    def test_missing_files_reports_what_to_download(self, tmp_path) -> None:
        """資料不存在時要給得出下載清單，而不是丟一個看不懂的例外。"""
        assert dataset_available(tmp_path) is False
        assert missing_files(tmp_path) == [r.file_id for r in RECORDINGS]


# =========================================================================== 端到端
@requires_dataset
class TestCwruRun:
    """真的跑一次外部驗證。"""

    @pytest.fixture(scope="class")
    def report(self):
        return run_validation()

    def test_windows_are_loaded_for_every_recording(self) -> None:
        windows = load_windows()
        assert {w.recording for w in windows} == {r.file_id for r in RECORDINGS}
        assert all(len(w.features) == len(FEATURES) for w in windows)

    def test_detection_beats_chance(self, report) -> None:
        """最低標：只用正常樣本擬合，故障樣本要被排在前面。"""
        fp = next(r for r in report.detection if r.method == "fingerprint_strength")
        assert fp.auc > 0.8
        assert fp.pauc > 0.5

    def test_attribution_beats_majority_and_chance(self, report) -> None:
        fp = next(r for r in report.attribution if r.method == "fingerprint_cosine")
        majority = next(r for r in report.attribution if r.method == "majority")
        assert fp.top1 > 2 * (1 / len(FAULT_CLASSES))
        assert fp.top1 > majority.top1
        assert fp.report.macro_f1 > 0.5

    def test_every_class_is_scored(self, report) -> None:
        """三類都要有 precision / recall，不能只報好看的那幾個。"""
        fp = next(r for r in report.attribution if r.method == "fingerprint_cosine")
        for cls in FAULT_CLASSES:
            assert fp.report.per_class[cls].support > 0

    def test_confusion_matrix_rows_account_for_every_sample(self, report) -> None:
        fp = next(r for r in report.attribution if r.method == "fingerprint_cosine")
        for label in fp.report.labels:
            assert sum(fp.report.matrix[label].values()) == fp.report.per_class[label].support

    def test_single_feature_baseline_is_reported(self, report) -> None:
        """一定要有一個「現行流程」對照組，而且它是在訓練切分上最佳化過的。"""
        assert any(r.method == "single_feature_rule" for r in report.attribution)
        assert any(r.method == "rms_threshold" for r in report.detection)

    def test_severity_transfer_is_reported_even_though_it_is_bad(self, report) -> None:
        """跨嚴重度轉移是本模組唯一不容易的設定，也是唯一會讓數字難看的一節。
        它必須存在 —— 只報同尺寸內的近滿分而藏起這一節，就是選擇性報告。"""
        assert report.severity_transfer
        for row in report.severity_transfer:
            assert row["train_size_in"] != row["test_size_in"]
            assert 0.0 <= float(row["fingerprint_top1"]) <= 1.0

    def test_sklearn_absence_is_reported_not_hidden(self, report) -> None:
        joined = " ".join(report.notes)
        assert "scikit-learn" in joined
        if not report.config["sklearn"]:
            assert "未執行" in joined

    def test_markdown_carries_the_reality_notice(self, report) -> None:
        """報表本身要帶著「這是實驗台不是產線」，有人只複製表格時它也跟著走。"""
        assert cwru.REALITY_NOTICE in report.to_markdown()

    def test_report_serialises(self, report) -> None:
        import json

        payload = report.to_dict()
        assert json.loads(json.dumps(payload, ensure_ascii=False))
        assert payload["dataset"]["is_synthetic"] is False
