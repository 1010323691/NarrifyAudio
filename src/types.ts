// Shared backend types — mirror the FastAPI response/request shapes exactly.
// Keeping these in one place so the views, api modules and stores agree.

// ------------------------------ files ------------------------------
export interface UploadResult {
  path: string
  name: string
  size: number
  id?: string
  file_id?: string
  project_id?: string
}
export interface FileItem {
  name: string
  is_dir: boolean
  size: number | null
  id?: string
  file_id?: string
  project_id?: string
}
/** Response of ``GET /api/files/list/{module}``. */
export interface DirListResult {
  path: string
  items: FileItem[]
}

// ------------------------------ text ------------------------------
export interface TextStats {
  chars: number
  paras: number
  chapters: number
}
export interface TextToggles {
  keep_single_space: boolean
  sentence_break: boolean
  dialogue_separate: boolean
  detect_chapters: boolean
  punct_ellipsis: boolean
  punct_repeated: boolean
  punct_lone_ascii: boolean
  punct_quotes: boolean
  punct_dash: boolean
  live: boolean
}
export interface TextFormatResult {
  source: string
  encoding: string
  output_path: string
  stats: TextStats
  preview: string
  full_length: number
  file_id?: string
  project_id?: string
}

// ------------------------------ book ------------------------------
export interface BookChapter {
  seq: number
  num: number | null
  numStr: string
  title: string
  chars: number
}
export interface BookSequenceReport {
  count: number
  parseable: number
  unparseable: number
  first: number | null
  last: number | null
  gaps: { after: number; missing: number[] }[]
  duplicates: { seq: number; num: number }[]
  disorder: { seq: number; num: number; prevNum: number }[]
  hasIssues: boolean
}
export interface BookAnalyzeResult {
  source: string
  encoding: string
  base: string
  total_chars: number
  chapters: BookChapter[]
  chapter_count: number
  filenames: string[]
  /** The chapter format the splitter actually recognizes (「第N章」) — user-facing. */
  expected_format: string
  sequence: BookSequenceReport
  /** Count/sequence before deterministic smart repair, for diagnostics. */
  raw_chapter_count?: number
  raw_sequence?: BookSequenceReport
  repair_status?: 'ok' | 'clean' | null
  error: string | null
}
export interface BookSplitFile {
  name: string
  path: string
  chars: number
}
export interface BookSplitResult {
  output_dir: string
  file_count: number
  files: BookSplitFile[]
  /** Final chapter list used to create the files. */
  chapters?: BookChapter[]
}

// ------------------------------ book · smart split ------------------------------
export type SmartConfidence = 'high' | 'medium' | 'low'
/** One per final (kept) chapter: original number -> repaired number + actions. */
export interface SmartRepairAction {
  seq: number
  orig_num: number | null
  orig_numStr: string
  orig_title: string
  final_num: number
  actions: string[]
  confidence: SmartConfidence
}
/** A chapter deleted during repair: duplicate dropped or duplicate copy truncated. */
export interface SmartRemoved {
  seq: number
  num: number
  numStr: string
  title: string
  kind: 'dropped' | 'truncated'
}
export interface SmartWarning {
  type: string
  detail: string
}
export interface SmartSplitChapter {
  seq: number
  orig_num: number | null
  orig_numStr: string
  final_num: number
  title: string
  chars: number
  actions: string[]
  confidence: SmartConfidence
}
export interface BookSmartSplitResult {
  status: 'ok' | 'clean'
  output_dir: string
  file_count: number
  files: BookSplitFile[]
  chapters: SmartSplitChapter[]
  report: {
    actions: SmartRepairAction[]
    warnings: SmartWarning[]
    removed: SmartRemoved[]
  }
  baseline_chars: number | null
  original_count: number
  expected_format: string
}

// ------------------------------ audio ------------------------------
export interface AudioProbeResult {
  ok: boolean
  name: string
  path: string
  duration: number
  size: number
  ext: string
  mime: string
}
export interface AudioSegment {
  index: number
  start: number
  duration: number
}
export interface AudioPlanResult {
  duration: number
  count: number
  each: number
  target: number
  segments: AudioSegment[]
}
export interface AudioPause {
  start: number
  end: number
}
export interface AudioSilencesResult {
  duration: number
  pause_count: number
  pauses: AudioPause[]
  count: number
  segments: AudioSegment[]
  snapped: number
  fallbacks: number
  aligned: boolean
  shifts: number[]
}
export interface AudioCutFile {
  name: string
  path: string
  size: number
  duration: number
}
export interface AudioCutResult {
  output_dir: string
  file_count: number
  files: AudioCutFile[]
  duration: number
  smart_align: boolean
}

// ------------------------------ tts ------------------------------
export interface TTSStatus {
  implemented: boolean
  message: string
}

// ------------------------------ tts: 角色配音 / 音频合成 / 音频合并 ------------------------------
export interface VoiceItem {
  name: string
  line_count: number
  status: 'ready' | 'pending' // overall ready = alias OR a usable voice (clone/design/custom); a bare foundation is NOT ready
  foundation_status: 'none' | 'done' | 'failed' // Phase 1 (语音推理基础) state
  clone_status: 'none' | 'done' | 'failed' // Phase 2 (克隆音频) state
  type: string // clone | design | custom | foundation | ''
  alias_of: string // non-empty -> this label points at another character's voice
  gender: 'male' | 'female' | '' // '' = unknown; pre-filled by Phase 1, the badge pick wins
  description: string
  preview: string // path relative to 04_voice_profiles/ (playable via downloadUrl('04_voice_profiles', preview)); '' if none
  /** The character's clone candidates (new format; a legacy single-take entry synthesises
   *  one, entries without a clone yield []). ``preview`` is relative to 04_voice_profiles/. */
  candidates: { id: string; preview: string; seed: number }[]
  /** The user's candidate pick; null = no explicit pick (the first candidate is active). */
  selected_audio_id: string | null
}
export interface VoicesListResult {
  has_script: boolean
  script_path: string
  voice_config_path: string
  speakers: VoiceItem[]
}
/** 角色配音 · 合并角色：source 的全部台词在 Parse 源数据中改为 target，source 的
 *  声音配置被删除（候选音频文件留盘）。``files`` = 实际被改写的解析 JSON 文件名。 */
export interface MergeSpeakersResult {
  ok: boolean
  source: string
  target: string
  replaced: number
  files: string[]
}
export interface PrepareFoundationsOptions {
  speakers?: string[]
  new_only?: boolean
  overrides?: Record<string, string>
  /** Which parsed JSON (in 03_parsed_json/) to read; undefined → most recent. */
  script?: string
}
/** Phase 2 (TTS only): options for ``POST /api/tts/make-clones`` (批量制作克隆音频). */
export interface MakeClonesOptions {
  speakers?: string[]
  new_only?: boolean
  /** 批内行数上限（单个 worker 进程内的 GPU 张量批；1 = 逐条串行；1..64）；
   *  undefined → 后端缺省 (config.tts.batch_concurrency=4)。 */
  concurrency?: number
  /** Which parsed JSON to read for the character set; undefined → most recent. */
  script?: string
  /** Per-character clone-candidate count: undefined/null → auto (absolute log-scale
   *  ladder on each character's OWN line count — the 旁白's 10×+ line count can't
   *  demote the leads); 2/4/6/8 → fixed count. */
  candidate_count?: number | null
}
/** Options for ``POST /api/tts/batch`` (音频合成). */
export interface BatchRunOptions {
  indices?: number[]
  script?: string
  scripts?: string[]
}

export interface PrepareFoundationsResult {
  count: number
  aliases: number
  speakers: string[]
  voice_config_path: string
  results: { speaker: string; ok: boolean; type: string; description: string }[]
}
/** Phase 2 (TTS) batch result (``POST /api/tts/make-clones``). */
export interface MakeClonesResult {
  count: number
  ok: number
  failed: number
  speakers: string[]
  voice_config_path: string
  output_dir: string
  results: { speaker: string; ok: boolean; type: string; preview: string; reason?: string; candidates?: number }[]
}
export interface BatchResult {
  total: number
  completed: number
  /** Failed segments (a multi-file run tags each entry with its file name in `script`). */
  failed: { index: number; speaker: string; reason: string; script?: string }[]
  output_dir: string
  manifest_path: string
  /** Cumulative (after this run) number of segments already synthesized — 「累计已合成 X」. */
  done_count?: number
  /** Total synthesizable segments in the script — 「全部 Y」 (denominator of the cumulative count). */
  all_count?: number
  /** Per-file outcomes (multi-file runs only; one entry per requested file, in request order). */
  files?: BatchFileResult[]
}
/** Per-file outcome of a multi-file synthesis run (``BatchResult.files``). */
export interface BatchFileResult {
  /** The parsed JSON file name (03_parsed_json/). */
  script: string
  total: number
  completed: number
  /** Number of failed segments in this file (top-level `failed` carries the details, tagged). */
  failed: number
  output_dir: string
  manifest_path: string
  /** Cumulative done segments after the run (resume-aware). */
  done_count: number
  /** Total synthesizable segments in the file. */
  all_count: number
  /** Non-null when the file itself failed fatally (e.g. unreadable JSON) — isolated, the rest
   *  of the batch continues (the reason is in the task log). */
  error: string | null
}
/** Per-file synthesis stats (the 待合成 rows; ``GET /api/tts/batch-status?scripts=…``). */
export interface BatchFileStatus {
  name: string
  total: number
  completed: number
  remaining: number
  /** Every segment synthesized (ok + file on disk) → the row's 【已合成】 badge. */
  complete: boolean
  /** Distinct speakers in the file (first-appearance order, incl. NARRATOR). */
  speakers: number
  /** Speakers with a usable voice (an alias, or a ready clone/design/custom) — the same rule
   *  the 角色配音 page uses for its ready state. */
  ready: number
  /** Speakers without a usable voice (warned before the run). */
  missing: string[]
  /** Voices changed after the last synthesis; every line for these speakers is re-rendered. */
  stale_speakers?: string[]
}
/** Response of ``GET /api/tts/batch-status?scripts=…`` (one entry per requested file, in order). */
export interface BatchStatusFiles {
  files: BatchFileStatus[]
}
/** Synthesis progress for the 待合成 card's 【已合成 / 总段落】 (``GET /api/tts/batch-status``). */
export interface MergeResult {
  file: string
  path: string
  segments: number
  size: number
}
/** One row of the merge page's package list (GET /api/tts/merge-status): the package's
 *  synthesis completion. `total` = the source parsed JSON's synthesizable segment count
 *  (a manifest-length total would mark a mid-cancelled package "ready"). */
export interface MergePackageStatus {
  name: string
  total: number
  completed: number
  remaining: number
  complete: boolean
}
export interface MergeStatusPackages {
  packages: MergePackageStatus[]
}

// ------------------------------ script (LLM -> JSON) ------------------------------
/** Response of ``POST /api/script/generate-files``: one independent task per file. */
export interface GenerateFilesResult {
  task_ids: string[]
  files: { file: string; task_id: string }[]
}

// ------------------------------ tasks ------------------------------
export type TaskStatus =
  | 'pending'
  | 'running'
  | 'paused'
  | 'cancelled'
  | 'succeeded'
  | 'failed'

export interface TaskLog {
  level: string
  msg: string
  t: number
}
export interface TaskSnapshot {
  id: string
  module: string
  label: string
  /** Current engine stage, e.g. parse/check. */
  phase?: string
  status: TaskStatus
  progress: number
  current: string
  logs: TaskLog[]
  /** Raw LLM stream (「流式反馈」 panel); populated by `llm_chunk` events / snapshots. */
  llm_stream?: string
  /** Live LLM generation rate (chars/s) for the 文本解析 per-window gauge; 0 when idle / queued. */
  llm_cps?: number
  /** 10-second-window average of the LLM generation rate (chars/s) for the 文本解析 吞吐量 card;
   *  backend-computed as (chars over the last 10 s) / 10 s from the real streamed text; 0 when idle. */
  llm_cps_10s?: number
  /** Cumulative original-text chars processed (per completed chunk) — 处理速度 numerator. */
  llm_chars?: number
  /** Cumulative processing seconds up to the last completed chunk — 处理速度 denominator. */
  llm_secs?: number
  /** 已合成段数 (音频合成 进度指标, cumulative: pre-run done + this run's); 0 when the task
   *  doesn't report segments (only 音频合成 tasks do). */
  seg_done?: number
  /** Total synthesizable segments of the run (音频合成 进度指标 denominator). */
  seg_total?: number
  /** 已合成字数 (音频合成 进度指标, cumulative; stripped code points of the done segments). */
  seg_chars_done?: number
  /** Total chars of the run's full segment table (音频合成 进度指标 denominator). */
  seg_chars_total?: number
  result: Record<string, any>
  error: string
  created: number
  started: number
  finished: number
  /** Monotonic creation order (backend `itertools.count`) — batch order after reload. */
  seq: number
}
export type TaskControl = 'cancel' | 'pause' | 'resume' | 'retry'

// ------------------------------ config ------------------------------
export interface AppConfig {
  paths: { working_dir: string }
  text: TextToggles
  ffmpeg: {
    ffmpeg_path: string
    ffprobe_path: string
  }
  audio: {
    target_duration: string
    naming_format: string
    start_number: string
    smart_align: boolean
    align_tolerance: number
  }
  tts: {
    /** 遗留字段（无读取方，仅为旧配置 round-trip 保留）。 */
    parallel_workers: number
    /** 长度排序后每批最多容纳的行数；合成支持 1..128。 */
    batch_concurrency: number
    /** 音频合成页的自动批量开关。 */
    batch_auto: boolean
    /** Administrator-configured synthesis seed; -1 means random. */
    batch_seed: number
  }
  llm: {
    base_url: string
    api_key: string
    model_name: string
  }
  prompts: {
    system_prompt: string
    user_prompt: string
  }
  persona_prompts: {
    system_prompt: string
    user_prompt: string
    advanced_prompt: string
  }
  generation: {
    chunk_size: number
    max_tokens: number
    temperature: number
    top_p: number
    top_k: number
    min_p: number
    presence_penalty: number
    banned_tokens: number[]
    /** Max files parsed in parallel (LLM jobs); the rest queue behind a shared gate. */
    max_concurrency: number
    /** 解析后归属抽样率（0 = 关闭）：1/3 纯随机（整书错误率仪表）+ 2/3 风险加权。
     *  每本读数记入任务日志与 config/spot_check_history.json；降不降由用户手动决定。 */
    spot_check_rate: number
    spot_check_adaptive: boolean
    spot_check_min_rate: number
    spot_check_max_rate: number
    /** 断句失败校验开关（解析内阶段；关闭 = 跳过该阶段并记录日志）。 */
    revalidate_splits: boolean
    /** 纯归属标签条删除开关（解析内阶段；关闭 = 跳过该阶段并记录日志）。 */
    delete_saying_tags: boolean
    /** 角色匹配检查开关（解析内阶段，重判 chunk 边界两侧条目；关闭 = 跳过该阶段并记录日志）。 */
    check_boundary_speakers: boolean
    /** 解析内重判批大小（角色匹配检查 / 断句失败校验 / 归属抽样共用）；不在设置页露出。 */
    check_batch_size: number
    /** 解析内重判上下文窗口（角色匹配检查 / 断句失败校验 / 归属抽样共用）；不在设置页露出。 */
    check_context_window: number
    /** 超长段落检查开关（解析内阶段 A）：超长条目先 LLM 语义重切、后机械分段兜底；
     *  关闭 = 整体跳过（含机械分段），最终不保证字数上界。 */
    check_long_paragraphs: boolean
    /** 段落硬上限（字数 = strip 后 Unicode 码点数）：任何条目最终不得超过此值，
     *  由超长段落检查的机械分段兜底保证。 */
    max_paragraph_chars: number
    /** 纯标点条目吸收开关（解析内阶段 B，确定性零 LLM 成本）：无词字符条目并入相邻
     *  NARRATOR（标题守卫），无邻接则删除。 */
    absorb_punct_entries: boolean
    /** 同人段落合并开关（解析内机械后处理，确定性零 LLM 成本）：连续同 speaker 条目
     *  按词字符数合并（块+段 ≤100 字，或较短一方 ≤10 字强制），边界无收尾标点补「。」，
     *  instruct 取词字符多者，章标题两侧不合并。置于超长机械分段**之前**——其 ≤10 强制
     *  合并可能造出超上限块，由随后的机械分段切回（段落上限硬保证不变）。 */
    merge_same_speaker: boolean
  }
  ui: {
    theme: string
    show_parse_logs: boolean
    /** 侧边栏是否显示「音频分集」导航项（默认关 = 隐藏）。 */
    show_audio_split: boolean
  }
  /** 背景音乐系统（阶段 7）：章节级 BGM 匹配 + 最终混音。 */
  bgm: {
    /** BGM 增益（0~2，1 = 原曲电平、2 = 2 倍）。 */
    volume: number
    /** 淡入秒数（混音时钳 min(fade_in, 时长/2)）。 */
    fade_in: number
    /** 淡出秒数（混音时钳 min(fade_out, 时长/2)）。 */
    fade_out: number
    /** 循环策略 = 重复策略：true = 循环铺满；false = 只播一遍，其余静音。 */
    loop: boolean
    /** 匹配最低分：低于此分不进候选（钳 ≥1）。 */
    min_match_score: number
    /** 章节 LLM 分析采样字数（头/中/尾三窗）。 */
    analysis_chars: number
    /** 段落分析每批送 LLM 的条目数（隐藏参数，不进设置页）。 */
    segment_batch_size: number
    /** 段落级 intensity 1/2/3 → volume 的倍率（越界/缺失按档 2；结果 clamp ≤2.0）。 */
    segment_volume_tiers: number[]
  }
}

/** A recursively-partial ``AppConfig`` — mirrors the backend's deep-merge ``update_config``
 *  (``PUT /api/config``), which patches only the fields actually sent, at any nesting depth.
 *  Nested object sections may be partial; arrays are provided whole. */
export type DeepPartial<T> = {
  [K in keyof T]?: T[K] extends readonly any[]
    ? T[K]
    : T[K] extends object
      ? DeepPartial<T[K]>
      : T[K]
}

/** Workspace state from ``GET /api/workspace``. */
export interface WorkspaceInfo {
  set: boolean
  /** The workspace folder; empty string when no workspace is set. */
  path: string
  /** Whether the pointed folder still exists on disk (false = moved/deleted → re-select). */
  exists?: boolean
  /** True when no user workspace is set yet (pipeline is locked). */
  is_default: boolean
  /** Artifact directory name → absolute path (01_input, 02_split_text, …); empty when unset. */
  dirs: Record<string, string>
  workspace_id?: string
  project_id?: string
  workspace_name?: string
}

// ------------------------------ music library（全局音乐库） ------------------------------
/** The four tag categories (buckets). */
export type MusicTagCategory = 'scene' | 'mood' | 'emotion' | 'custom'
/** A track's tag buckets — one list per category. */
export type TrackTags = Record<MusicTagCategory, string[]>
/** One entry of ``music_library/music_index.json`` ``tracks``. */
export interface MusicTrack {
  /** Duration in seconds (0 when the probe failed — non-blocking). */
  duration: number
  /** Actual on-disk size, or null if the indexed file is missing. */
  size_bytes?: number | null
  /** Number of saved chapter assignments across managed workspaces (admin view). */
  use_count?: number | null
  enabled: boolean
  description: string
  tags: TrackTags
  added_at: string
  /** Folder name the track belongs to ("" = 未分类 / library root).
   *  Folders are index metadata (no physical subdirectory) — matching
   *  never reads this field. */
  folder: string
}
/** One entry of the index ``folders`` section (user-created folder metadata). */
export interface MusicFolder {
  created_at: string
}
/** One entry of ``music_library/music_tag_suggestions.json`` — AI candidate
 *  tags pending user confirmation (the batch AI recognition never writes the
 *  index; candidates only). */
export interface MusicSuggestion {
  tags: Partial<Record<MusicTagCategory, string[]>>
  suggested_at: string
  model: string
}
/** Response of ``GET /api/music/library``. */
export interface MusicLibrary {
  version: number
  tags: Record<MusicTagCategory, string[]>
  tracks: Record<string, MusicTrack>
  /** User-created folders (name -> metadata). Legacy indexes may lack the
   *  section — treat as empty. */
  folders?: Record<string, MusicFolder>
  /** Track count per folder key (zero-count folders included). */
  folder_counts?: Record<string, number>
  /** Pending AI candidates (existing tracks only — orphans of deleted
   *  tracks are filtered out server-side). */
  suggestions?: Record<string, MusicSuggestion>
}
/** Response of a music delete / batch-delete (some names may be skipped). */
export interface MusicDeleteResult {
  deleted: string[]
  skipped: { name: string; reason: string }[]
  missing: string[]
}
/** Response of ``POST /api/music/suggest-tags-batch`` (one Task per track). */
export interface SuggestBatchResult {
  task_ids: string[]
  tracks: { name: string; task_id: string }[]
}
/** Response of ``POST /api/music/tracks/apply-suggestions`` (adopt cached AI
 *  candidates — only tracks with candidates AND no manual tags are touched). */
export interface ApplySuggestionsResult {
  applied: string[]
  skipped_manual: string[]
  no_suggestion: string[]
  missing: string[]
}

// ------------------------------ bgm（背景音乐：章节匹配 + 混音） ------------------------------
/** LLM chapter-atmosphere analysis (one chapter of ``chapter_music_analysis.json``). */
export interface ChapterAnalysis {
  scene: string[]
  mood: string[]
  emotion: string[]
  custom: string[]
  analyzed_at: string
  edited: boolean
  edited_at?: string
}
/** A chapter's BGM assignment (one chapter of ``bgm_assignments.json``). */
export interface BgmAssignment {
  /** Snapshot of the chapter tags at match time. */
  tags: { scene: string[]; mood: string[]; emotion: string[]; custom: string[] }
  /** Bare music-library file name; null = no BGM; absent entry = never matched. */
  music: string | null
  locked: boolean
  manual: boolean
  score: number | null
  reason: string
  matched_at: string
  /** 段落级模式标记：true = 该章由「匹配（时间轴）」驱动（混音只读 Timeline）；
   *  章节模式匹配显式写 false（把遗留时间轴作废）。旧文件无此键 = 章节模式。 */
  segment?: boolean
}
/** The chapter's paragraph (段落级) LLM analysis row field (null = never analyzed). */
export interface SegmentChapterAnalysis {
  analyzed_at: string
  entry_count: number
  /** 指纹 vs 当前 03 条目不匹配（或 03 缺失）= 分析已失效，需重跑段落分析。 */
  stale: boolean
  /** All paragraph scene tags, deduplicated for the chapter row. */
  tags: TrackTags
}
/** One BGM span of a paragraph-level timeline (a contiguous music run). */
export interface TimelineSpan {
  /** Start offset in seconds (from the chapter's timeline origin). */
  start: number
  /** End offset in seconds. */
  end: number
  /** Bare music-library file name. */
  music_id: string
  /** The span's BGM volume (intensity-mapped, already clamped ≤ 2.0). */
  volume: number
  /** LLM intensity 1/2/3 (max over the merged run). */
  intensity: number
  tags: { scene: string[]; mood: string[]; emotion: string[]; custom: string[] }
  /** Mechanical match score (first scene of the run). */
  score: number | null
  /** Mechanical match reason (first scene, +「（合并 N 段）」/「（短段并入）」when merged). */
  reason: string
  /** LLM scene description (v2 timelines; absent on v1 files). */
  scene_desc?: string
  /** LLM mood description (v2 timelines; absent on v1 files). */
  mood_desc?: string
  /** LLM switch reason — why this scene breaks from the previous one (v2). */
  switch_reason?: string
}
/** One chapter's timeline file (``08_bgm/timelines/<stem>.json``). */
export interface BgmTimeline {
  version: number
  stem: string
  generated_at: string
  model: string
  /** 段落分析指纹（= 分析时的 03 文本 sha256）。 */
  fingerprint: string
  entry_count: number
  /** 计算时的 06 旁白时长（秒）= 混音新鲜度锚点。 */
  duration: number
  timeline: TimelineSpan[]
}
/** Response of ``GET /api/bgm/timeline/{stem}``. */
export interface BgmTimelineResult {
  timeline: BgmTimeline | null
}
/** The row-level timeline summary (full file via getTimeline). */
export interface BgmTimelineSummary {
  sections: number
  duration: number | null
  generated_at: string
}
/** One row of ``GET /api/bgm/chapters`` (disk-state basis = 02_split_text stems). */
export interface BgmChapterRow {
  /** Chapter stem (02_split_text/<stem>.txt without .txt). */
  stem: string
  /** True once 06_audio_merge/<stem>.mp3 (or .wav) exists — mixing needs the narration. */
  narration_exists: boolean
  /** True once 08_bgm/<stem>.mp3 exists (a no-BGM chapter's copy2 also counts). */
  mix_exists: boolean
  analysis: ChapterAnalysis | null
  assignment: BgmAssignment | null
  /** The assignment points at a music file that no longer exists in the library. */
  music_missing: boolean
  /** 段落分析行字段（null = 该章从未段落分析）。 */
  segment_analysis: SegmentChapterAnalysis | null
  /** 时间轴行字段（segment 标记或当前段落分析仍有效时下发；null = 无有效时间轴）。 */
  timeline: BgmTimelineSummary | null
  /** 任一时间轴 span 的曲目已从音乐库删除。 */
  segment_music_missing: boolean
}
/** Response of ``GET /api/bgm/chapters``. */
export interface BgmChaptersResult {
  chapters: BgmChapterRow[]
  /** Current matching mode: "random" | "llm" (persisted in bgm_assignments.json). */
  mode: string
}
/** Response of ``POST /api/bgm/match``. */
export interface BgmMatchResult {
  mode: string
  matched: number
  no_bgm: number
  skipped_locked: number
}
/** Response of batch analyze / mix endpoints: one independent task per chapter. */
export interface BgmBatchResult {
  task_ids: string[]
  chapters: { stem: string; task_id: string }[]
}
/** Response of ``POST /api/bgm/package``. */
export interface BgmPackageResult {
  zip_path: string
  file_count: number
  base: string
}
