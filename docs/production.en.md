# Produce your first audiobook

[English home](../README.md) · [中文首页](../README.zh-CN.md) · [中文指南](production.md)

This guide is for regular users producing an audiobook from a manuscript. Install the application using the [Windows deployment guide](windows.en.md) or [Linux deployment guide](linux.en.md). See [operations](operations.en.md) for service control, backups, and troubleshooting. Chinese UI labels are included so you can find the corresponding controls.

## Before you start

Administrators sign in at `/#/admin/login` to configure storage, LLM access, TTS, and user quotas. Regular users sign in at `/#/login` to use the production workspace. The portals have separate role permissions; an administrator account cannot substitute for a regular user account in the production workspace.

Start with a short manuscript and complete one chapter before processing a whole book.

| Capability | Requirements |
| --- | --- |
| Projects, text formatting, splitting, and chapter review | Application, database, queue, and Worker pool running |
| Script parsing, voice foundations, and BGM paragraph analysis | Accessible LLM configured by an administrator; sufficient quota |
| Voice candidates, dialogue synthesis, and individual line regeneration | Working local TTS environment and models; sufficient quota |
| Audio probing, merging, and mixing | FFmpeg, ffprobe and shared `.venv` audio dependencies (merging requires pydub); mixing also needs a music library |

A remote LLM does not require local CUDA. Follow the deployment guide for the GPU, environment, and models used by local TTS. The presence of engine files alone does not prove that real synthesis works.

## 1. Create a project and organize your manuscript

Create a project in My Projects (「我的项目」) and open its workspace. Production pages use the active project, so switch projects before working on another book. The project overview shows stage progress, ongoing tasks, and failures that need attention.

In Formatting and Splitting (「排版与分册」), use Add Source Documents (「添加源文档」) to upload TXT or EPUB files. Select several files or add more later. Files become one book in list order; move them up or down or remove them before processing. Refreshing restores the source list from the last processing run.

EPUB extraction follows the book's reading order and retains the original upload. Images, fonts, and navigation are excluded from body text. Encrypted body content and image-only scans are unsupported; there is no OCR or PDF/DOCX manuscript import.

Choose a method in Processing Settings (「处理设置」):

- **Smart splitting (「智能分册」)** identifies chapters and splits them. If no chapters are found, it falls back to the target character count.
- **Split by character count (「按字数分册」)** divides the full text near paragraph or sentence boundaries, using the target character count.

You can also configure sentence breaks, separate dialogue paragraphs, spaces, and punctuation normalization. Start Processing (「开始处理」) runs formatting, chapter analysis, and splitting in the background, producing chapter text for parsing.

## 2. Review chapters

Check chapter numbers, titles, body text, review reasons, and automatic adjustments. Review flagged chapters individually and save their review marks. Automatic processing helps detect and repair anomalies; duplicated numbers, missing chapters, and mistaken headings still need human review.

Use search, filters, pagination, and chapter details. Processing and review state are saved so you can leave and return. Reprocessing creates a new chapter version; previous split text or parsing results may become overwritten or stale. Confirm the current version before continuing.

## 3. Parse speakers and dialogue

In Text Parsing (「文本解析」), select pending, failed, all, or filtered chapters and click Start Parsing (「开始解析」). Results contain chapter-level speakers, dialogue, and voice instructions. Compare them with the source text.

Parsing Checks (「解析检查项」) cover content completeness, abnormal sentence breaks, long paragraphs, speaker attribution, voice instructions, and attribution spot checks. Settings apply to future submissions; submitted tasks retain their configuration snapshot. These checks can attempt repairs but cannot guarantee correct speakers and dialogue throughout the book.

Submission may be blocked while formatting is running, when source content has changed, or when a task for the same chapter is already active. Follow the page instructions to finish upstream work or refresh state. Retry failed chapters individually instead of reparsing the whole book.

## 4. Produce and select character voices

Character Voices (「角色配音」) has two stages:

1. **Voice foundations (「语音推理基础」)** use an LLM to generate a voice description and reference text from character dialogue and context. Regenerate individual characters or supply a custom voice description.
2. **Voice candidates** use local TTS to generate reference audio from those foundations. Audition the candidates and select the preferred voice for subsequent dialogue synthesis. A default candidate is used when no explicit selection is made.

The page supports gender markers, individual regeneration, filling missing foundations or candidates, and manual character merging. Relationship hints are informational; similar names are not automatically treated as one person. Merge only after confirming that they refer to the same character.

These candidate voices are generated from descriptions. This workflow does not provide an interface for uploading arbitrary recordings of a real person and automatically cloning them. Candidate generation can partially succeed; inspect each character's status and audition the results.

## 5. Synthesize dialogue audio

In Audio Synthesis (「音频合成」), check character voices and select chapters and synthesis scope. Normally, fill missing segments first. To regenerate everything, use the full regeneration operation and review the prompt about resetting existing results.

The background task generates separate audio segments for dialogue and reports completed, partial, and failed chapters. Parsing files without synthesizable dialogue cannot be synthesized directly. After changing a character voice, check whether existing segments still match the current voice configuration.

Complete and audition one chapter before expanding the scope. Synthesized segments are production materials; merge or complete another delivery stage before collecting finished audio.

## 6. Preview and revise individual lines

Chapter Preview (「整章预览」) lets you audition lines, edit text, speakers, and voice instructions, and regenerate a single line. When separate line audio is unavailable, playback may fall back to the merged chapter without seeking to that particular line.

Edits first remain in the editing state. Regenerated audio is a staged preview; audition it and save when satisfied. Each edited line must have a successful staged render matching its current text, speaker, and voice instructions before it can be saved to the official chapter.

**Saving updates the official script and segment audio, and invalidates the chapter's existing merged audio, BGM mix, and paragraph timeline.** Merge again after revisions, then check or rebuild the affected music timeline and mix. Wait for conflicting synthesis, merging, or save operations on the same chapter to finish.

## 7. Merge chapters and apply optional processing

### Merge a complete chapter

In Audio Merge (「音频合并」), check input completeness and submit ready chapters. Only fully synthesized chapters can be submitted. The first merge creates a chapter MP3; merging again overwrites the selected chapters' previous output. Audition the result and check transitions and the chapter as a whole.

### Optional: background music

Background Music (「背景音乐」) uses merged chapter narration directly. Match tracks or select them manually, review paragraphs and the timeline, then mix and audition. Use whole-chapter random matching or ask the LLM to analyze paragraphs. Recomputing a paragraph timeline from existing analysis does not itself call the LLM.

Mixing requires merged narration and available tracks. After revising dialogue, merging again, or changing tracks or paragraphs, check that the timeline matches the current narration before mixing. The page may block conflicting operations on a chapter already being processed.

## 8. Track tasks and collect finished audio

### Task Center (「任务中心」)

Workers execute long-running production tasks in the background. You can leave a page and return later. Closing the browser does not cancel a task; the server, database, queue, and Workers still need to run. Processing cannot continue while services are stopped; check Task Center for the authoritative state after recovery.

Tasks can be queued, running, paused, cancelling, successful, failed, or timed out. Open a project's task category in Task Center to use Pause All (「暂停全部」), Start All (「启动全部」), or Cancel All (「取消全部」) for eligible tasks. Retry failures using the controls available in the relevant workbench. Cancellation may take time. Pauses caused by an unavailable LLM differ from manual pauses; read the status and error details. Give the administrator the project, chapter, task ID, and error when troubleshooting a failure.

### My Resources (「我的资源」)

The page has Finished Outputs (「成品」), Production Materials (「制作资料」), and Storage Management (「存储管理」). Finished outputs support auditioning, individual downloads, and packaging a selected scope. Production materials—including sources, chapters, parsing JSON, voice profiles, and audio segments—support inspection and returning to the workbench, without download or packaging controls.

Download eligibility depends on reliable successful delivery records and the current file identity, rather than its output directory alone. Partial merges with missing segments and subsequent mixes are not downloadable. Older files without reliable output records may remain production materials. The resource inventory needs an update after task completion; use Update Inventory (「更新清单」) when needed.

Export packages are retained for 7 days and cannot be downloaded after their source project enters the trash. A ZIP is a collection of finished outputs, not a restorable project backup. Follow [operations](operations.en.md) to back up both the database and workspace.

### Storage, trash, and quota

Storage cleanup only removes ordinary files older than 7 days from allowed temporary or cache directories. Official text, voice profiles, audio, BGM materials, configuration, and logs are excluded. Inspect the candidate list first. Execution rechecks eligibility and active tasks, so actual reclaimed space may be lower than the preview. Deleted caches cannot be restored, including files deleted before a cleanup task was cancelled.

A project in Trash (「回收站」) is retained for one calendar month. Moving it there does not immediately delete files or release disk space. Restore it to continue production; trash is not an independent backup.

Usage (「使用量」) shows available quota, quota already reserved by TTS tasks, actual consumption, and transaction details. LLM usage is measured by final effective output. TTS reserves against actual input, charges successful synthesis, and releases unexecuted portions. Prompts, tokens, request counts, and audio duration are not the billing basis here. Administrators configure quota; resource scans, packaging, and cleanup do not consume production quota.

See [operations](operations.en.md) for resource maintenance and backups, and the [development guide](development.en.md) for implementation details and customization.
