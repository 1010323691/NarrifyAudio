"""Measured automatic TTS batch limits, shared by orchestration and the child worker.

2026-10-07, RTX 5090: one clone speaker, CustomVoice + Base resident, original
reference audio, three seeds per accepted limit. Production uses 10% headroom, rounded down
to an even number. Raw limits: 296 / 242 / 192 / 128 / 90 / 80. See scripts/testing/README.md.
These are sample-validated limits; runtime OOM recovery remains necessary.
"""

AUTO_BATCH_POINTS = ((5, 266), (20, 216), (50, 172), (100, 114), (150, 80), (200, 72))
AUTO_BATCH_MAX = max(cap for _, cap in AUTO_BATCH_POINTS)
AUTO_BATCH_CAPS = tuple(sorted({cap for _, cap in AUTO_BATCH_POINTS}, reverse=True))
