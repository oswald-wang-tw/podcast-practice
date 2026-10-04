# Podcast Practice

Turn English podcasts and their transcripts into offline listening practice with
**uv + Montreal Forced Aligner (MFA)**. The player supports click-to-seek sentences,
word highlighting, slower playback, sentence looping, pausing at sentence boundaries,
transcript hiding, search, and keyboard shortcuts.

Each episode produces a portable `player.html` with embedded audio, SRT/VTT subtitles,
word timestamps in JSON, and a TextGrid. All audio analysis runs locally, with no SaaS,
API keys, or subscriptions. Network access is needed for the initial setup and model
downloads.

## Initial setup

Automatic setup supports **Linux x86_64** (Ubuntu 22.04/24.04 or a compatible
distribution is recommended) and macOS 14 or later on Intel and Apple Silicon.
Linux ARM and Windows are not supported by automatic setup; the tool reports an
unsupported platform instead of attempting to install incompatible packages.

1. Install [uv](https://docs.astral.sh/uv/getting-started/installation/) and Git.
2. Clone the repository and set up the local runtime:

```sh
git clone https://github.com/oswald-wang-tw/podcast-practice.git
cd podcast-practice
uv sync --locked
uv run podcast-practice setup
uv run podcast-practice doctor
```

You can also transfer an exported Git bundle to another computer:

```sh
git clone /path/to/podcast-practice.bundle podcast-practice
cd podcast-practice
uv sync --locked
uv run podcast-practice setup
```

**You do not need to install Python, Conda, or ffmpeg separately.** uv manages Python
and the project's Python dependencies. `setup` downloads the official micromamba
2.9.0 binary, creates an isolated MFA 3.4.2 environment with Kaldi and ffmpeg under
`.runtime/`, and downloads the English MFA/G2P models and the local `base.en` model
used for coarse alignment.

MFA's native dependencies cannot be installed with `uv pip install` alone. This
repository manages that environment without modifying system Python or requiring
you to activate Conda for daily use. See the
[official MFA installation guide](https://montreal-forced-aligner.readthedocs.io/en/latest/installation.html).

Allow several GB of disk space for the initial setup. Download and installation
times depend on your connection and computer; subsequent runs reuse the models.

## Daily use

Save an audio file and its UTF-8 transcript, then run:

```sh
uv run podcast-practice build /path/to/today.mp3 /path/to/today.txt \
  --title "Today's episode" --offline --open
```

Use a different pair of files for the next episode, without changing the code:

```sh
uv run podcast-practice build /path/to/another.m4a /path/to/another.txt \
  --title "Another podcast" --offline --open
```

- Audio: MP3, M4A, WAV, FLAC, and other formats supported by ffmpeg.
- Transcripts: English `.txt` or `.md`, or timestamped `.srt` or `.vtt` files.
  Markdown support covers text paragraphs and headings, not complex formatting.
- `--offline` prevents coarse-alignment model downloads. Install the MFA models with
  `setup` first. Inference always runs locally.
- Each episode gets a directory identified by a content fingerprint. Different
  inputs create a new episode; rerunning the same inputs reuses completed results.
- `--open` opens the finished player in your default browser. You can also open the
  generated HTML directly, without a server.

Browse your practice library:

```sh
uv run podcast-practice serve --open
```

The server listens only on `127.0.0.1:8766`. Press Ctrl+C to stop it. Individual
players remain usable offline after the server stops. Commands use `library/` in
the current working directory by default; run them from the repository directory
to use the same library.

## Keyboard shortcuts

The player and library both have a shortcut-help button. Press `?` to open the
shortcut list and `Esc` to close it. Letter shortcuts do not require Shift.

| Player shortcut | Action |
| --- | --- |
| Space | Play / pause |
| Left / Right arrow | Previous / next sentence |
| Shift + Left / Right arrow | Seek backward / forward by 5 seconds |
| `R` | Replay the selected sentence |
| `[` / `]` | Decrease / increase playback speed |
| `L` | Toggle sentence looping |
| `P` | Toggle pausing at the end of a sentence |
| `F` | Toggle automatic transcript following |
| `H` | Hide / show the full transcript |
| `V` | Reveal / hide the current sentence while the transcript is hidden |
| `/` | Focus search and select the current search text |
| `Esc` in search | Clear the search and leave the search field |
| `?` | Open the shortcut list |

In the library, use Up / Down arrow to select an episode, Home / End to move to the
first / last episode, and Enter to open the selected episode. These navigation
shortcuts work when focus is on the page or an episode link.

Tab moves between controls. Space activates a focused button. Shortcuts leave text
input, form controls, input-method composition, and Ctrl / Alt / Command
combinations to their normal behavior. Holding a toggle key changes the option
only once; navigation, seeking, and speed shortcuts can repeat.

Update an existing episode's player without rerunning alignment:

```sh
uv run podcast-practice render library/episode-name-xxxxxxxx
```

## Delete and restore episodes

Each episode in the library has a delete button. After deletion, use the undo
button or expand the trash section to restore it. Restoration remains available
after reloading the page or restarting the server.

Deleting an episode moves its entire output directory to `library/.trash/`. It
preserves original audio and transcript inputs, shared models, and alignment
caches. Items in the trash still use disk space. Delete and restore buttons are
disabled when opening `index.html` through `file://`; use `serve` or the CLI:

```sh
uv run podcast-practice delete episode-name-xxxxxxxx
uv run podcast-practice trash
uv run podcast-practice restore <trash-id>
```

Each item in the trash also has a permanent-delete button. Its confirmation dialog
shows the episode title and deletion scope. Confirming permanently removes that
episode's player, audio copy, subtitles, and other outputs; it cannot be restored
from the trash. Original inputs, shared models, and processing caches under
`.runtime/jobs/` are preserved.

The CLI requires an explicit `--yes` to delete an item permanently:

```sh
uv run podcast-practice purge <trash-id> --yes
```

Use `--library /path/to/library` for a custom library. Restoration refuses to
overwrite an existing directory with the same name.

## Transcript format

Plain transcripts do not need timestamps. Use normal English paragraphs, optionally
with speaker headings:

```text
Host
Welcome to today's episode. We will talk about a new idea.

[MUSIC PLAYING]

Alex Taylor
Thanks for having me. Let me explain how it works.

Host: What should listeners try first?
```

Speaker names are detected automatically. If needed, repeat `--speaker "Alex Taylor"`
to specify speakers explicitly.

Markers such as `[MUSIC PLAYING]` and `[AD PLAYING]` are excluded from spoken text.
URLs, Markdown headings, and FT sharing prompts are also skipped. Transcript files
are treated as data; commands or instructions inside them are never executed.

The displayed transcript preserves the original wording. Alignment input expands
numbers, percentages, years, and uppercase abbreviations into spoken English. If
the recording uses a different pronunciation, write the intended spoken form in
the input transcript. Only English is currently supported.

## Episode outputs

```text
library/
  index.html
  episode-name-xxxxxxxx/
    player.html         # Embedded audio; portable offline practice in one file
    audio.mp3
    transcript.txt
    subtitles.srt
    subtitles.vtt
    alignment.json      # Original text, speakers, word/sentence times, model metadata
    alignment.TextGrid
    review.json         # Segments recommended for manual review
    windows.json        # MFA sentence search windows; can be edited manually
```

Use `--output /path/to/episode` to choose an episode's output directory. Audio,
transcripts, models, `.venv`, processing caches, and libraries are excluded by
`.gitignore`; cloning the repository does not transfer those local files.

## Alignment workflow

1. Preserve the original display text and create normalized English pronunciation
   input.
2. Generate G2P pronunciations only for words missing from the dictionary. Cache
   them across episodes to avoid repeating work.
3. Run an initial MFA alignment over the full episode.
4. Run local faster-whisper recognition and match words to estimate coarse speech
   locations, excluding music, ads, and trailing audio missing from the transcript.
5. Run MFA again within the estimated sentence search windows to produce the final
   word timestamps.
6. Validate word completeness and timestamp ordering, then export the player,
   subtitles, and review report.

For SRT/VTT input, the supplied timestamps define search windows, so coarse
recognition is unnecessary. **Final word timestamps always come from MFA. Coarse
recognition does not replace your transcript.**

You do not need to hardcode music timestamps, speaker lists, or absolute file paths
for each episode. The tool stops and asks you to check the inputs when coarse word
match coverage falls below 35%. If the transcript is deliberately edited, you can
continue with `--allow-low-confidence`.

Automatic alignment can still produce inaccurate word boundaries. The player marks
sentences recommended for review. Missing transcript content, background music,
edited wording, and unusual pronunciations may require manual adjustment.

## Configuration and troubleshooting

Choose a smaller, faster coarse-alignment model:

```sh
uv run podcast-practice setup --asr-model tiny.en
```

`build` uses the model selected during setup by default. Use `--asr-model small.en`
for a larger model; download it before using `--offline`.

Reuse an existing MFA environment and model directories without downloading them
again. These paths are stored locally in `.runtime/config.json`:

```sh
uv run podcast-practice setup --mfa-prefix /path/to/mfa-env \
  --model-dir /path/to/MFA --asr-cache /path/to/asr-cache
```

Use `build ... --no-asr` to skip coarse recognition. Check music and trailing audio
boundaries yourself in that mode. Timestamped SRT/VTT input still uses its supplied
times.

To correct a sentence, copy the episode's `windows.json`, edit its `begin` and `end`
values in seconds, and run:

```sh
uv run podcast-practice build /path/to/today.mp3 /path/to/today.txt \
  --title "Today's episode" --windows /path/to/windows.json --offline
```

You can supply only the entries you want to change, such as
`[{"id": 7, "begin": 41.0, "end": 45.0}]`. Sentence IDs start at 0. Windows must stay
within the audio duration and must not overlap neighboring sentences.

Refresh the player without rerunning alignment:

```sh
uv run podcast-practice render library/episode-name-xxxxxxxx
```

`--force` allows output replacement while preserving computation caches for the same
inputs. To recompute an episode from scratch, move its
`.runtime/jobs/<fingerprint>/` cache directory out of the way before rebuilding.
Failures and Ctrl+C preserve processing caches; detailed logs remain in the job
directory.

## Development and tests

```sh
uv sync --locked --group dev
uv run pytest
uv run ruff check .
```

Tests cover transcript formats, number and speaker parsing, automatic speech
windows, invalid or overlapping windows, original-text preservation, HTML escaping,
and player and library keyboard behavior. The regular test suite does not download
speech models or require MFA. Real-audio alignment quality still needs listening
checks on the target machine.

Keyboard regression tests use Node.js without npm dependencies. When Node.js is
available, pytest runs them automatically; otherwise that test is skipped. You can
also run them directly:

```sh
node --test tests/keyboard.test.cjs
```

Package code lives in `src/podcast_practice/`; the reusable player template is
`src/podcast_practice/assets/player.html`. Python dependencies are locked in
`uv.lock`, and tool versions are defined in the runtime code. MFA is pinned to
3.4.2; its transitive native dependencies are resolved through conda-forge and are
not managed by `uv.lock`.
