"""压测引擎（临时测试入口）：纯函数 —— 自动生成文本的长度/旋转/可切片性 + 克隆音色选择。

The engine's Task worker (``stress_test``) drives a real shared .venv subprocess and is
covered by hand via the 音频合成 page's 压测 dialog; only the pure helpers are unit
tested here (same convention as the other TTS-family engines).
"""
import pytest

from backend.engines import tts_stress as Stress

_PUNCT = set("，。！？…；：、""''（）")


class TestGenerateText:
    def test_exact_length(self):
        for L in (1, 7, 10, 20, 30, 100, 500, 1024):
            assert len(Stress.generate_text(L, 0)) == L

    def test_zero_and_negative_give_empty(self):
        assert Stress.generate_text(0, 0) == ""
        assert Stress.generate_text(-5, 0) == ""

    def test_only_cjk_and_sentence_punct(self):
        text = Stress.generate_text(800, 123)
        assert text
        assert all(("一" <= c <= "鿿") or c in _PUNCT for c in text)

    def test_rows_of_one_run_differ_by_offset(self):
        # The per-row rotation (offset = row * 13) must keep rows of one run distinct.
        assert Stress.generate_text(30, 0) != Stress.generate_text(30, 13)

    def test_offset_larger_than_paragraph_wraps(self):
        # Any offset (even huge) slices cleanly — the wrap-safe double paragraph.
        for off in (0, 1, Stress.MAX_STRESS_CHARS, 10 ** 6):
            assert len(Stress.generate_text(Stress.MAX_STRESS_CHARS, off)) == Stress.MAX_STRESS_CHARS

    def test_max_chars_within_allowed_range(self):
        # The API validates 1..MAX_STRESS_CHARS == the worker's MAX_SEQ_CHARS.
        assert Stress.MAX_STRESS_CHARS == 2500


class TestRoundVerdict:
    def test_deadline_is_chars_over_10_per_second(self):
        # 64 行 × 50 字 = 3200 字 → 320 秒（1 秒必须出 10 个字）
        assert Stress.round_deadline_seconds(3200) == 320.0
        assert Stress.round_deadline_seconds(640) == 64.0
        assert Stress.round_deadline_seconds(0) == 0.0

    def test_within_budget_passes(self):
        assert Stress.judge_round(300.0, 320.0) == (True, "")

    def test_exact_budget_passes(self):
        ok, _reason = Stress.judge_round(320.0, 320.0)
        assert ok

    def test_over_budget_fails_with_timeout_reason(self):
        ok, reason = Stress.judge_round(321.0, 320.0)
        assert not ok
        assert "超时" in reason and "10 字/秒" in reason

    def test_unmeasured_synth_time_fails(self):
        # 超时被杀 / 未见「模型就绪」→ 标准无法度量 → 失败（被杀的轮次永不判通过）
        ok, reason = Stress.judge_round(None, 320.0)
        assert not ok
        assert "无法测得" in reason


class TestPickStressSpeaker:
    def _config(self):
        return {
            "甲": {"type": "clone", "ref_audio": "04_voice_profiles/a.wav", "ref_text": "你好，我是甲。"},
            "乙": {"type": "clone", "ref_audio": "", "ref_text": "缺 ref_audio 的克隆"},
            "丙": {"type": "design", "description": "一个 design 音色"},
        }

    def test_auto_picks_first_usable_clone(self):
        assert Stress.pick_stress_speaker(self._config(), None) == "甲"

    def test_explicit_usable_clone(self):
        assert Stress.pick_stress_speaker(self._config(), "甲") == "甲"

    def test_explicit_clone_without_ref_raises(self):
        with pytest.raises(RuntimeError, match="不是可用的克隆音色"):
            Stress.pick_stress_speaker(self._config(), "乙")

    def test_explicit_non_clone_raises(self):
        with pytest.raises(RuntimeError, match="不是可用的克隆音色"):
            Stress.pick_stress_speaker(self._config(), "丙")

    def test_unknown_speaker_raises(self):
        with pytest.raises(RuntimeError, match="不是可用的克隆音色"):
            Stress.pick_stress_speaker(self._config(), "丁")

    def test_no_clone_at_all_raises(self):
        with pytest.raises(RuntimeError, match="没有可用的克隆音色"):
            Stress.pick_stress_speaker({"丙": {"type": "design"}}, None)
