"""聲音模態：特徵、偵測器、合成邊界、以及接進診斷之後的行為。

這組測試守住五件事，每一件都對應評審會追問的一個問題：

1. 「聲音是真的有在算，還是畫面上的裝飾？」→ 特徵抽取與四個指標可由波形量回來，
   偵測器在合成正常音訊上分數低、在故障音訊上分數高，且 AUC 遠高於隨機。
2. 「你們的合成音訊會不會偷看答案？」→ `AcousticObservation` 不含任何故障標籤，
   整份 snapshot 也不含（比照 `test_twin.py::test_snapshot_never_exposes_ground_truth`）。
3. 「多裝一支麥克風會不會偷偷改掉既有結論？」→ 聲音沒發言權時，
   融合式**逐位元**退化成原本的純感測器判斷。
4. 「合成的跟真的分得開嗎？」→ 合成觀測一律 `synthetic=True`，
   且 `acoustics/dcase.py`（真實資料）不被 twin / agents / api 匯入。
5. 「外部真實資料上的成績？」→ 資料集在的話就真的重跑一次並和官方 baseline 比；
   **資料集不在時整組 skip**，CI 不會因為少了 1 GB 的檔案而變紅。
"""

from __future__ import annotations

import importlib
import importlib.util
import math
import pathlib

import pytest

from factory_guardian.acoustics import signatures, synthetic
from factory_guardian.acoustics.signatures import (
    ACOUSTIC_RESPONSE,
    INDICATOR_NAMES,
    NOMINAL_INDICATORS,
    acoustic_signatures,
)
from factory_guardian.agents.diagnosis import (
    ACOUSTIC_STRENGTH_FLOOR,
    ACOUSTIC_STRENGTH_FULL,
    W_ACOUSTIC_SHARE,
    W_SIGNATURE,
    DiagnosisAgent,
)
from factory_guardian.agents.monitoring import MonitoringAgent
from factory_guardian.domain import Action, ActionKind, MachineState, Severity
from factory_guardian.twin.engine import FactoryTwin
from factory_guardian.twin.scenarios import get_scenario

FAULT_IDS = ("bearing_degradation", "cooling_failure", "motor_overload")

# 聲學的**訊號處理**部分是 optional dependency（pip install -e '.[acoustics]'）。
# 指紋知識、孿生體的合成觀測與診斷整合則是純 Python，不裝也要能測 ——
# 那些才是 Demo 閉環真正會走到的路徑。
_AUDIO_MODULES = ("numpy", "scipy", "librosa", "soundfile", "sklearn")
_MISSING_AUDIO = [m for m in _AUDIO_MODULES if importlib.util.find_spec(m) is None]
requires_audio_stack = pytest.mark.skipif(
    bool(_MISSING_AUDIO),
    reason=f"缺少聲學相依套件 {_MISSING_AUDIO}；pip install -e '.[acoustics]'",
)


# --------------------------------------------------------------------------------------
# 指紋知識本身
# --------------------------------------------------------------------------------------
def test_every_fault_has_an_acoustic_signature():
    """三個故障模型都要有對應的聲學響應，否則「多模態」就只有兩個半模態。"""
    from factory_guardian.twin.faults import FAULTS

    assert set(ACOUSTIC_RESPONSE) == set(FAULTS)
    for signature in acoustic_signatures().values():
        assert set(signature.profile) == set(INDICATOR_NAMES)
        assert signatures.strength(signature.profile) > 0.5, "指紋不能是零向量，那等於沒有鑑別力"


def test_cooling_and_motor_are_acoustically_opposite():
    """整個聲音模態的價值就在這一條。

    冷卻失效與馬達過載**都會推高溫度**，感測器指紋在這裡分得辛苦；
    但一個是「泵停了 → 少一個純音」、一個是「負載上升 → 多出線頻諧波」，
    在聲學空間裡是幾乎相反的兩個向量。
    """
    sigs = acoustic_signatures()
    cos = signatures.cosine(sigs["cooling_failure"].profile, sigs["motor_overload"].profile)
    assert cos < -0.8, f"冷卻失效與馬達過載的聲學指紋應該幾乎相反，實際 {cos:.3f}"


def test_deviation_vector_is_zero_for_a_healthy_machine():
    assert signatures.strength(signatures.deviation_vector(NOMINAL_INDICATORS)) == pytest.approx(0.0)


# --------------------------------------------------------------------------------------
# 特徵抽取與渲染器（合成路徑）
# --------------------------------------------------------------------------------------
@requires_audio_stack
def test_log_mel_summary_has_the_declared_shape():
    import numpy as np

    from factory_guardian.acoustics.features import DEFAULT_CONFIG, log_mel_summary

    waveform = np.random.default_rng(0).standard_normal(16_000).astype(np.float32) * 0.1
    features = log_mel_summary(waveform)
    assert features.shape == (DEFAULT_CONFIG.feature_dim,)
    assert DEFAULT_CONFIG.feature_dim == DEFAULT_CONFIG.n_mels * len(DEFAULT_CONFIG.statistics)
    assert np.isfinite(features).all()


@requires_audio_stack
def test_louder_audio_has_a_higher_mean_log_mel():
    """特徵要真的跟著訊號走 —— 這是最基本的正確性檢查。"""
    import numpy as np

    from factory_guardian.acoustics.features import log_mel_summary

    rng = np.random.default_rng(1)
    quiet = rng.standard_normal(16_000).astype(np.float32) * 0.01
    loud = quiet * 10.0
    # dB 尺度上 ×10 的振幅 = +20 dB，mean 區塊（前 64 維）應該整體抬高。
    assert log_mel_summary(loud)[:64].mean() > log_mel_summary(quiet)[:64].mean() + 15.0


@requires_audio_stack
@pytest.mark.parametrize("fault_id", [None, *FAULT_IDS])
def test_renderer_round_trips_level_and_spectral_balance(fault_id):
    """`target_indicators` → `synthesize_waveform` → `indicators_from_waveform` 要收斂。

    這條閉環是「兩條路徑接得起來」的具體證明：DCASE 驗證用的那個特徵抽取器，
    可以直接跑在孿生體產生的音訊上。

    波峰因數不在這裡檢查 —— 它有物理下限（高斯噪音成分自身的波峰因數約 11.4 dB），
    低於那個值渲染不出來。衝擊性的檢查改由下一個測試負責。
    """
    from factory_guardian.acoustics.features import indicators_from_waveform

    target = synthetic.target_indicators(fault_id, 1.0)
    measured = indicators_from_waveform(synthetic.synthesize_waveform(target, duration_s=2.0))

    assert measured["spl_db"] == pytest.approx(target["spl_db"], abs=0.5)
    assert measured["high_band_ratio"] == pytest.approx(target["high_band_ratio"], abs=0.05)
    assert measured["tonal_ratio"] == pytest.approx(target["tonal_ratio"], abs=0.10)


@requires_audio_stack
def test_renderer_makes_bearing_defects_audibly_impulsive():
    """軸承缺陷的聲學特徵就是「衝擊」，渲染出來必須真的量得到。"""
    from factory_guardian.acoustics.features import indicators_from_waveform

    healthy = indicators_from_waveform(
        synthetic.synthesize_waveform(synthetic.target_indicators(None, 0.0), duration_s=2.0)
    )
    bearing = indicators_from_waveform(
        synthetic.synthesize_waveform(
            synthetic.target_indicators("bearing_degradation", 1.0), duration_s=2.0
        )
    )
    assert bearing["crest_factor_db"] > healthy["crest_factor_db"] + 3.0
    assert bearing["high_band_ratio"] > healthy["high_band_ratio"] + 0.1


# --------------------------------------------------------------------------------------
# 偵測器（不依賴 1 GB 資料集）
# --------------------------------------------------------------------------------------
def _rendered(fault_id: str | None, progress: float, seed: int, duration_s: float = 1.0):
    target = synthetic.target_indicators(fault_id, progress)
    return synthetic.synthesize_waveform(target, duration_s=duration_s, seed=seed)


@pytest.fixture(scope="module")
def fitted_detector():
    """只用「正常」音訊擬合 —— 這就是無監督的具體意思。

    module scope：渲染波形要跑 FFT，不該為了每個測試重做一次。
    本專案 pytest 幾秒跑完是刻意維護的優點。
    """
    from factory_guardian.acoustics.detector import AcousticAnomalyDetector

    normals = [_rendered(None, 0.0, seed=s) for s in range(16)]
    return AcousticAnomalyDetector(n_neighbors=4).fit_waveforms(normals), normals


@requires_audio_stack
def test_detector_trains_on_normal_audio_only_and_scores_faults_higher(fitted_detector):
    detector, normals = fitted_detector
    assert detector.fitted

    held_out_normal = [detector.score_waveform(_rendered(None, 0.0, seed=900 + i)) for i in range(8)]
    faulty = [
        detector.score_waveform(_rendered(fid, 1.0, seed=950 + i))
        for i, fid in enumerate(FAULT_IDS)
    ]
    assert max(held_out_normal) < min(faulty), (
        f"故障音訊的分數必須全面高於正常音訊；normal={held_out_normal}, faulty={faulty}"
    )


@requires_audio_stack
def test_detector_auc_is_far_above_chance(fitted_detector):
    """AUC 高於隨機（0.5）—— 而且要高很多，不是勉強過關。"""
    from factory_guardian.acoustics.detector import auc_scores

    detector, _ = fitted_detector
    scores, labels = [], []
    for i in range(9):
        scores.append(detector.score_waveform(_rendered(None, 0.0, seed=2000 + i)))
        labels.append(0)
    for i in range(9):
        fid = FAULT_IDS[i % len(FAULT_IDS)]
        scores.append(detector.score_waveform(_rendered(fid, 0.7, seed=3000 + i)))
        labels.append(1)

    auc, pauc = auc_scores(labels, scores)
    assert auc > 0.9, f"AUC {auc:.3f} 未顯著高於隨機"
    assert pauc > 0.6, f"pAUC {pauc:.3f} 過低（低誤報率下抓不到異常，產線上沒有價值）"


@requires_audio_stack
def test_alarm_threshold_comes_from_normal_scores_only(fitted_detector):
    """門檻由訓練集（全正常）的分位數決定，不是拿測試異常調出來的。"""
    detector, _ = fitted_detector
    threshold = detector.threshold_at_quantile(0.95)
    assert threshold > 0
    assert detector.score_waveform(_rendered("bearing_degradation", 1.0, seed=77)) > threshold


@requires_audio_stack
def test_detector_refuses_to_fit_on_a_single_sample():
    import numpy as np

    from factory_guardian.acoustics.detector import AcousticAnomalyDetector

    with pytest.raises(ValueError):
        AcousticAnomalyDetector().fit(np.zeros((1, 128), dtype=np.float32))


# --------------------------------------------------------------------------------------
# Digital Twin 的合成觀測
# --------------------------------------------------------------------------------------
def test_only_machining_machines_have_a_microphone(twin: FactoryTwin):
    """包裝機沒有 vibration 訊號，也就沒有可據以推導的聲學物理。

    硬給它一支麥克風等於憑空編一組數字 —— 那正是這個專案不做的事。
    """
    snapshot = twin.run(3)
    assert snapshot.machines["M-A"].acoustics is not None
    assert snapshot.machines["M-B"].acoustics is not None
    assert snapshot.machines["M-C"].acoustics is None


def test_synthetic_audio_is_always_labelled_as_synthetic(twin: FactoryTwin):
    """三個可信度原則之一：合成資料明確標示。聲音不能例外。"""
    observation = twin.run(3).machines["M-A"].acoustics
    assert observation is not None
    assert observation.synthetic is True
    payload = observation.to_dict()
    assert payload["synthetic"] is True
    assert "SYNTHETIC" in payload["note"]
    assert "非真實錄音" in payload["note"]
    assert "vibration" in payload["provenance"]


def test_acoustics_never_expose_ground_truth():
    """比照 test_twin.py::test_snapshot_never_exposes_ground_truth。

    麥克風只送四個數字出去。刻意不附任何 per-fault 的相似度 ——
    那等於把 Ground Truth 換個名字送給 Agent。
    """
    twin = FactoryTwin(seed=20260809, tick_minutes=1.0)
    twin.schedule(get_scenario("bearing-degradation").injections)
    twin.run(15)

    observation = twin.snapshot().machines["M-A"].acoustics
    assert observation is not None
    payload = str(observation.to_dict())
    assert "bearing_degradation" not in payload
    assert "fault" not in payload

    # 整份 snapshot 也一樣（加了麥克風之後這條原則仍然成立）。
    whole = str(twin.snapshot().to_dict())
    assert "bearing_degradation" not in whole
    assert "fault" not in whole
    # 但引擎內部確實知道答案。
    assert twin.ground_truth == {"M-A": "bearing_degradation"}


@pytest.mark.parametrize("scenario_id,indicator,rising", [
    ("bearing-degradation", "crest_factor_db", True),    # 衝擊性上升
    ("bearing-degradation", "high_band_ratio", True),    # 高頻能量上升
    ("cooling-failure", "tonal_ratio", False),           # 冷卻泵停了 → 少一個純音
    ("motor-overload", "tonal_ratio", True),             # 負載上升 → 線頻諧波增強
])
def test_faults_move_the_expected_acoustic_indicator(scenario_id, indicator, rising):
    twin = FactoryTwin(seed=7)
    before = twin.run(2).machines["M-A"].acoustics.indicators[indicator]
    twin.schedule(get_scenario(scenario_id).injections)
    after = twin.run(20).machines["M-A"].acoustics.indicators[indicator]
    assert (after > before) is rising


def test_healthy_and_hazard_scenarios_keep_the_microphone_quiet():
    """工安事件沒有設備故障 → 聲音不該亂叫，否則會製造假的設備告警。"""
    twin = FactoryTwin(seed=3)
    twin.schedule(get_scenario("hazard-zone").injections)
    observation = twin.run(12).machines["M-A"].acoustics
    deviation = signatures.deviation_vector(observation.indicators)
    assert signatures.strength(deviation) < 0.25


def test_stopped_machine_falls_back_to_background_noise(twin: FactoryTwin):
    """停機的機台，麥克風聽到的是現場背景音，不是「異常安靜的運轉聲」。"""
    twin.run(5)
    running_spl = twin.snapshot().machines["M-A"].acoustics.spl_db
    twin.apply(Action(ActionKind.STOP_MACHINE, "M-A"))
    twin.run(6)
    assert twin.snapshot().machines["M-A"].state is MachineState.STOPPED
    assert twin.snapshot().machines["M-A"].acoustics.spl_db < running_spl - 15.0


def test_acoustic_rendering_is_deterministic():
    a, b = FactoryTwin(seed=42), FactoryTwin(seed=42)
    a.schedule(get_scenario("bearing-degradation").injections)
    b.schedule(get_scenario("bearing-degradation").injections)
    assert a.run(20).machines["M-A"].acoustics == b.run(20).machines["M-A"].acoustics


# --------------------------------------------------------------------------------------
# 接進診斷
# --------------------------------------------------------------------------------------
def _diagnose(ctx, scenario_id: str, seed: int = 99, confirm_ticks: int = 4):
    twin = FactoryTwin(seed=seed)
    twin.schedule(get_scenario(scenario_id).injections)
    monitor, diagnoser = MonitoringAgent(ctx), DiagnosisAgent(ctx)
    event = None
    snapshot = twin.snapshot()
    for _ in range(40):
        snapshot = twin.step()
        significant = [e for e in monitor.detect(snapshot, twin.topo) if e.severity.rank >= Severity.WARNING.rank]
        if significant:
            event = significant[0]
            break
    assert event is not None
    for _ in range(confirm_ticks):
        snapshot = twin.step()
        monitor.detect(snapshot, twin.topo)
    return diagnoser.diagnose(event, snapshot, twin.topo, monitor.smoothed_readings(snapshot.machines["M-A"]))


@pytest.mark.parametrize("scenario_id,expected", [
    ("bearing-degradation", "bearing_degradation"),
    ("cooling-failure", "cooling_failure"),
    ("motor-overload", "motor_overload"),
])
def test_acoustic_evidence_supports_the_true_root_cause(ctx, scenario_id, expected):
    diagnosis = _diagnose(ctx, scenario_id)
    assert diagnosis.top.fault_id == expected

    acoustic = diagnosis.acoustics
    assert acoustic["available"] is True
    assert acoustic["synthetic"] is True
    # 真正的根因應該拿到最高的聲學餘弦。
    assert max(acoustic["cosines"], key=acoustic["cosines"].get) == expected
    assert acoustic["cosines"][expected] > 0.8
    # 有效權重就是文件裡寫的那個數字，不是另一個。
    assert acoustic["effective_weight"] == pytest.approx(W_SIGNATURE * W_ACOUSTIC_SHARE, abs=1e-6)
    assert any(e.source == "acoustic" for e in diagnosis.top.evidence)
    assert any("非真實錄音" in e.statement for e in diagnosis.top.evidence if e.source == "acoustic")


def test_weights_still_add_up_the_way_the_dashboard_shows_them(ctx):
    """Dashboard 把信心度拆成「餘弦 × 0.75 ＋ 先驗 × 0.15 ＋ 文件 × 0.10 = 合計」。

    聲音是融合進第一項的，所以這條式子必須**仍然成立** ——
    否則畫面上會出現一個加不起來的合計，那比沒有聲音模態更糟。
    """
    diagnosis = _diagnose(ctx, "bearing-degradation")
    weights = diagnosis.weights
    assert weights["signature"] == pytest.approx(0.75)
    assert weights["prior"] == pytest.approx(0.15)
    assert weights["docs"] == pytest.approx(0.10)
    # 拆解出來的感測器／聲學權重必須加回原本的 0.75。
    assert weights["signature_sensor"] + weights["signature_acoustic"] == pytest.approx(weights["signature"])

    for candidate in diagnosis.candidates:
        scores = candidate.scores
        if "cosine" not in scores:
            continue
        expected = (
            weights["signature"] * max(0.0, scores["cosine"])
            + weights["prior"] * scores["prior"]
            + weights["docs"] * scores["docs"]
        )
        assert scores["combined"] == pytest.approx(expected, abs=1e-9)


def test_fused_cosine_is_the_declared_convex_combination(ctx):
    diagnosis = _diagnose(ctx, "bearing-degradation")
    share = diagnosis.acoustics["share"]
    assert share == pytest.approx(W_ACOUSTIC_SHARE * diagnosis.acoustics["gate"])
    for candidate in diagnosis.candidates:
        scores = candidate.scores
        if "cosine_sensor" not in scores:
            continue
        fused = (1.0 - share) * scores["cosine_sensor"] + share * scores["cosine_acoustic"]
        assert scores["cosine"] == pytest.approx(fused, abs=1e-9)


def test_signal_strength_still_comes_from_the_four_sensor_signals_only(ctx):
    """聲音**不**進 signal_strength。

    signal_strength 決定「訊號強度閘」與 Orchestrator 的 confirm 門檻，
    讓聲學指標流進去會改掉一組跟聲音無關的既有語意。
    """
    diagnosis = _diagnose(ctx, "bearing-degradation")
    observed = {row["name"] for row in diagnosis.observations}
    assert observed == {"temperature", "vibration", "current", "rpm_pct"}
    assert not observed & set(INDICATOR_NAMES)
    recomputed = math.sqrt(sum(row["deviation"] ** 2 for row in diagnosis.observations))
    assert diagnosis.signal_strength == pytest.approx(recomputed, abs=0.02)


def test_a_quiet_microphone_leaves_the_diagnosis_bit_for_bit_unchanged(ctx):
    """整個整合最重要的安全性質。

    聲音沒發言權時（gate = 0），融合式退化成 `cos_fused = cos_sensor`，
    也就是原本的純感測器判斷。多裝一支麥克風不該在「聲音根本沒說話」的情況下
    動到任何既有結論。
    """
    twin = FactoryTwin(seed=99)
    twin.schedule(get_scenario("hazard-zone").injections)
    monitor, diagnoser = MonitoringAgent(ctx), DiagnosisAgent(ctx)
    snapshot = twin.run(6)
    monitor.detect(snapshot, twin.topo)
    from factory_guardian.agents.safety import SafetyAgent

    event = SafetyAgent(ctx).detect_hazard_event(snapshot)
    assert event is not None
    diagnosis = diagnoser.diagnose(event, snapshot, twin.topo, monitor.smoothed_readings(snapshot.machines["M-A"]))

    assert diagnosis.acoustics["gate"] < 1e-9
    assert diagnosis.acoustics["share"] == 0.0
    for candidate in diagnosis.candidates:
        if "cosine_sensor" in candidate.scores:
            assert candidate.scores["cosine"] == candidate.scores["cosine_sensor"]
    # 聲音沒說話 → 不該憑空生出一條聲學證據。
    assert not any(e.source == "acoustic" for c in diagnosis.candidates for e in c.evidence)


def test_gate_has_a_dead_zone_then_opens_gradually():
    """gate 的三段行為：死區內棄權 → 線性開啟 → 完全開啟。

    死區存在的理由是量測雜訊：健康機台的聲學偏離量永遠不會恰好是零，
    沒有死區的話「麥克風不會動到既有結論」就只是近似成立。
    """
    def match(**overrides):
        return DiagnosisAgent._acoustic_match(
            _observation({**NOMINAL_INDICATORS, **overrides}), list(FAULT_IDS)
        )

    # 死區內：偏離量 0.9 / 6.0 = 0.15 < 0.25 → 完全棄權
    silent = match(spl_db=NOMINAL_INDICATORS["spl_db"] + 0.9)
    assert silent["strength"] < ACOUSTIC_STRENGTH_FLOOR
    assert silent["gate"] == 0.0 and silent["share"] == 0.0

    # 死區與飽和之間：部分發言權
    partial = match(spl_db=NOMINAL_INDICATORS["spl_db"] + 2.4)
    assert ACOUSTIC_STRENGTH_FLOOR < partial["strength"] < ACOUSTIC_STRENGTH_FULL
    assert 0.0 < partial["gate"] < 1.0

    # 明確的故障：完全開啟，且 share 恰好是宣告的 0.20
    strong = DiagnosisAgent._acoustic_match(
        _observation(synthetic.target_indicators("bearing_degradation", 1.0)), list(FAULT_IDS)
    )
    assert strong["strength"] > ACOUSTIC_STRENGTH_FULL
    assert strong["gate"] == pytest.approx(1.0)
    assert partial["share"] < strong["share"] == pytest.approx(W_ACOUSTIC_SHARE)


def _observation(indicators: dict[str, float]):
    from factory_guardian.domain import AcousticObservation

    return AcousticObservation(
        machine_id="M-A", sensor_id="MIC-A", sample_rate_hz=16_000, window_s=10.0,
        spl_db=indicators["spl_db"], high_band_ratio=indicators["high_band_ratio"],
        tonal_ratio=indicators["tonal_ratio"], crest_factor_db=indicators["crest_factor_db"],
    )


# --------------------------------------------------------------------------------------
# 真實／合成的架構隔離
# --------------------------------------------------------------------------------------
def test_real_dataset_module_is_not_reachable_from_the_demo_loop():
    """`dcase.py`（真實錄音）不得被 twin / agents / api 匯入。

    這是「真實資料只驗證偵測器、不參與 Demo」這句話的程式保證：
    不是靠紀律，是靠 import graph。
    """
    import ast
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1] / "factory_guardian"
    offenders = []
    for path in list((root / "twin").rglob("*.py")) + list((root / "agents").rglob("*.py")) + list(
        (root / "api").rglob("*.py")
    ):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [f"{node.module or ''}.{a.name}" for a in node.names]
                names.append(node.module or "")
            if any("dcase" in n for n in names):
                offenders.append(str(path))
    assert not offenders, f"這些檔案匯入了真實資料集模組：{offenders}"


def test_twin_does_not_need_numpy_or_sklearn_to_render_acoustics():
    """每 tick 的聲學路徑必須是純 Python。

    Digital Twin 一個 Benchmark 會跑上千個 tick，那條路徑上不能有 FFT，
    否則測試套件的執行時間會爆掉（這是本專案刻意維護的優點之一）。
    """
    source = importlib.import_module("factory_guardian.acoustics.synthetic")
    module_ast = __import__("ast").parse(
        __import__("pathlib").Path(source.__file__).read_text(encoding="utf-8")
    )
    top_level_imports = [
        n for n in module_ast.body if isinstance(n, (__import__("ast").Import, __import__("ast").ImportFrom))
    ]
    flattened = " ".join(__import__("ast").dump(n) for n in top_level_imports)
    assert "numpy" not in flattened, "numpy 必須是延遲載入（只在波形渲染器裡）"
    assert "sklearn" not in flattened


# --------------------------------------------------------------------------------------
# 外部真實資料（DCASE2020 / MIMII）—— 資料集不在時整組 skip
# --------------------------------------------------------------------------------------
def _dcase():
    return importlib.import_module("factory_guardian.acoustics.dcase")


# 這裡刻意**不**匯入 dcase 模組來問路徑：它在 module level 依賴 numpy，
# 而 collection 階段若 numpy 缺席就會整個模組收集失敗 —— skip 的意義就沒了。
# 直接看檔案系統，再由 test_dataset_path_matches_the_module 守住兩邊不會走鐘。
_DATASET_ZIP = (
    pathlib.Path(__file__).resolve().parents[1]
    / "data" / "external" / "dcase2020_pump" / "dev_data_pump.zip"
)
requires_dataset = pytest.mark.skipif(
    bool(_MISSING_AUDIO) or not _DATASET_ZIP.is_file(),
    reason="DCASE2020 pump 資料集未下載（1.03 GB，不進版控）——見 docs/acoustic_validation.md",
)


@requires_audio_stack
def test_dataset_path_matches_the_module():
    """測試裡寫死的路徑必須和模組宣告的一致，否則 skip 條件會悄悄失效。"""
    assert _dcase().DATASET_ZIP == _DATASET_ZIP


@requires_audio_stack
def test_dataset_metadata_is_always_declared():
    """就算資料集不在，授權與出處也必須是可查詢的（這是引用外部資料的義務）。"""
    dcase = _dcase()
    info = dcase.dataset_info()
    assert "CC BY-NC-SA 4.0" in info["license"]
    assert "非商業" in info["license"]
    assert info["url"].startswith("https://")
    assert "MIMII" in info["name"]
    assert "validation" in info["role"] and "never used in the demo loop" in info["role"]
    assert set(dcase.OFFICIAL_BASELINE) == {"00", "02", "04", "06", "average"}
    assert dcase.PAUC_P == 0.1


@requires_dataset
def test_dataset_split_matches_the_documented_counts():
    dcase = _dcase()
    clips = list(dcase.iter_clips())
    train = [c for c in clips if c.split == "train"]
    test = [c for c in clips if c.split == "test"]
    assert len(train) == 3349
    assert sum(c.label for c in train) == 0, "訓練集必須全是正常音訊 —— 無監督的前提"
    assert len([c for c in test if c.label == 0]) == 400
    assert len([c for c in test if c.label == 1]) == 456


@requires_dataset
def test_detector_beats_the_official_baseline_on_real_pump_audio():
    """docs/acoustic_validation.md §4 那張表，就是這個測試算出來的。

    第一次執行會建特徵快取（約 23 秒）；之後由快取評估只要 1.4 秒。
    """
    dcase = _dcase()
    result = dcase.evaluate(dcase.ensure_feature_cache())

    for machine_id, row in result["per_machine"].items():
        baseline = dcase.OFFICIAL_BASELINE[machine_id]
        assert row["auc"] > baseline["auc"], f"id {machine_id} AUC 未超越官方 baseline"
        assert row["pauc"] > baseline["pauc"], f"id {machine_id} pAUC 未超越官方 baseline"
        assert row["train_clips"] > 500

    average, baseline_average = result["average"], dcase.OFFICIAL_BASELINE["average"]
    assert average["auc"] > baseline_average["auc"]
    assert average["pauc"] > baseline_average["pauc"]
    # 文件裡報的是 0.9030 / 0.7855；容忍度留給 scikit-learn 版本差異。
    assert average["auc"] == pytest.approx(0.9030, abs=0.02)
    assert average["pauc"] == pytest.approx(0.7855, abs=0.03)
