"""Task types accepted by the durable task API and executable by its Worker."""

LEGACY_ENGINE_TASK_TYPES = frozenset({
    "voices.foundation", "voices.clone", "tts.batch", "tts.merge",
    "bgm.analysis", "bgm.segment", "bgm.mix", "bgm.match", "bgm.package",
    "music.suggest_tags", "audio.zip", "audio.export", "tts.reset",
})

SUPPORTED_TASK_TYPES = frozenset({
    "text.format", "book.analyze", "book.split", "script.parse",
    "audio.silences", "audio.cut", *LEGACY_ENGINE_TASK_TYPES,
})

BILLABLE_TASK_TYPES = frozenset({
    "script.parse", "voices.foundation", "voices.clone", "tts.batch",
    "bgm.analysis", "bgm.segment", "music.suggest_tags",
})
