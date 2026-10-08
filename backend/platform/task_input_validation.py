"""Validate submitted input versions at engine execution checkpoints."""
from ..core.input_versions import input_metadata, validate_files, value_version
from ..core.paths import get_or_prepare_layout
from ..core.script_snapshot import reference_paths, source_version
from .task_contracts import TaskExecutionError


def validate_task_inputs(payload, *, script=True):
    metadata = input_metadata()
    state_keys = ("_assignment_version", "_segment_analysis_version", "_chapter_analysis_version")
    if not (metadata.get("_script_inputs") or metadata.get("_music_inputs") or payload.get("_input_files") or payload.get("_music_files")
            or any(key in payload for key in state_keys)):
        return
    layout = get_or_prepare_layout()
    try:
        reference = metadata.get("_script_inputs")
        if script and reference and source_version(reference_paths(reference, layout.workspace))[0] != reference["version"]:
            raise RuntimeError("剧本输入已变更，请重新提交。")
        if payload.get("_input_files"):
            validate_files(layout.workspace, payload["_input_files"])
        if metadata.get("_music_inputs") or payload.get("_music_files"):
            from ..engines import music
            validate_files(music._library_dir(), metadata.get("_music_inputs") or [])
            validate_files(music._library_dir(), payload.get("_music_files") or [])
        stems = payload.get("chapters") or ([payload["stem"]] if payload.get("stem") else [])
        if not any(key in payload for key in state_keys):
            return
        from ..engines import bgm
        for key, loader in (("_assignment_version", bgm.load_assignments),
                            ("_segment_analysis_version", bgm.load_segment_analysis),
                            ("_chapter_analysis_version", bgm.load_analysis)):
            if key in payload:
                if len(stems) != 1 or value_version(loader(layout, stems).get("chapters", {}).get(stems[0])) != payload[key]:
                    raise RuntimeError("章节制作输入已变更，请重新提交。")
    except (OSError, ValueError, RuntimeError, KeyError) as exc:
        raise TaskExecutionError("input_changed", str(exc)) from exc
