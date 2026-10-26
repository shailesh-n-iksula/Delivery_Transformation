# Paste transcripts here

1. In Granola, copy the transcript (not the summary).
2. Paste it into a new .txt file in this folder. Keep Granola's header lines if you have them
   (Meeting Title / Date / Meeting participants / Transcript:). The file name is used as the title otherwise.
3. Name the note "Project :: topic :: SP" (for example "SiteOne :: returns export :: SP") so it routes correctly.
4. Run:  python3 -m dcp tick --project SP     (or:  python3 -m dcp ingest --project SP)

What happens: the transcript is checked, given a flow id, and filed under processed/.
It is refused, and stays here with the reason, if it looks like an AI summary, is under 100 words,
or is tagged for another project. The same call pasted twice is recognised and not drafted twice.
