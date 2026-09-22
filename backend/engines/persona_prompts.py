"""Default persona (voice-design) prompts.

Ported from the source project's ``persona_prompts.txt`` (three parts separated by
``---SEPARATOR---``). These are the *bundled* defaults: an empty value in
``config.app.persona_prompts`` falls back to the matching constant here (mirroring
how ``script_prompts.py`` feeds the text→JSON stage).

``PERSONA_USER_PROMPT`` is a template with the placeholders ``{speaker}`` and
``{line_windows}`` (the character's sampled lines, each with its local context) —
substituted per character by ``backend/engines/voices.py`` via ``str.replace`` (so a
user-edited template containing other braces never breaks the fill).
"""

PERSONA_SYSTEM_PROMPT = (
    "You produce concise JSON only."
)

PERSONA_USER_PROMPT = """You are a voice designer for an audiobook. Design the speaking voice of the character '{speaker}' so it can be synthesised and then cloned for the whole book.

Below are moments from the book featuring this character, sampled across the story — their lines near the opening, the middle, and the closing (when a character has too few lines to fill all three, whatever exists is shown). Each target line is printed together with the entries immediately around it in the script: the surrounding narration and the other characters' lines supply the local dramatic context — how the scene plays, how the character reacts and is described. The character's own line in each block is marked with ★.

{line_windows}

Using how '{speaker}' actually speaks across these moments — their words, rhythm, and the context around each line — infer what their voice sounds like.

Produce a JSON object with exactly three keys: 'description', 'ref_text' and 'gender'. Both are written in the same language as the character's lines.
- 'description': a concise voice spec — one or two sentences — covering ONLY the *acoustic* qualities of the voice: gender, age band, timbre, pitch register, speaking rate, intonation, accent/dialect, and the overall character of the delivery (warm vs cold, light vs gravelly, brisk vs slow, etc.). Determine gender and age from the character's *identity* as stated in the surrounding context — pronouns / relationship words (他/她, 男友/女友, 老师/同学) and role / age cues (学生, 孩子, 老人, an explicit age or grade) — and only when the context gives none of these may you infer from how they sound (then pick a neutral, unmarked default; do not lean on "青年"). Match the age band to the *person*, not to the fluency of the delivery: a 学生 or 儿童 is a 少年 / 青少年 / 儿童 (brighter, younger, less mature timbre) and is NEVER 青年 — a fluent, articulate sixteen-year-old still gets 青少年, not 青年 — and an elder is 老年. Write the age as a voice band, never as a fact about the person ("青少年" is fine, "他是一名学生" is not). Every trait you write must hold for this voice in *any* line they speak — if it only fits one scene or one emotion, drop it. Do NOT describe the character as a person (no personality, title, backstory, relationships, or plot role) and do NOT describe how their tone *shifts* with the situation (e.g. "when it gets urgent the voice turns sharp") — the result is a single, fixed reference voice that cannot change. In the same language as the lines, aim for something like "青年男性，音色清亮，音高中偏高，语速偏快，语调轻快上扬" (or, for a student, "少年男性，音色清亮偏稚嫩，音高偏高，语速偏快") rather than "性格外向幽默，紧急时语气变得急切严肃，重情重义"（the second describes a person and a shifting mood, not a fixed voice）.
- 'ref_text': a short, natural passage of 40-60 characters (about 12-15 seconds of speech) in this character's own voice — usually one coherent line, or at most two short connected lines — that represents how they normally speak. It is synthesised into the reference clip the whole book is cloned from, so make it natural and typical, and keep it strictly within 40-60 characters (never longer).
- 'gender': the character's gender as "male" or "female" — the SAME identity cue you already used for the description's gender (pronouns / relationship words / role and age cues). If the context gives no gender cue at all, use "unknown".
Only output the JSON object and nothing else."""
