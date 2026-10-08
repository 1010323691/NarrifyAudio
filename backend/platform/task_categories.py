"""Task-center categories shared by read models and batch controls."""
TASK_CATEGORIES = {
    "script": ("script.parse",),
    "voices-foundation": ("voices.foundation",),
    "voices-clone": ("voices.clone",),
    "tts": ("tts.batch",),
    "preview": ("tts.preview_render",),
    "merge": ("tts.merge",),
    "bgm": ("bgm.analysis", "bgm.segment", "bgm.match", "bgm.mix", "bgm.package"),
    "resources": ("resources.scan", "resources.package", "resources.cleanup", "project.progress"),
}
