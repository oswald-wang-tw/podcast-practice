# Podcast Practice

用 **uv + Montreal Forced Aligner（MFA）**，把每天不同的英文 podcast 與 transcript 做成離線聽力練習。點句跳播、逐字反白、慢速、單句循環、句尾暫停、隱藏原稿與搜尋都在播放器中完成。

每集輸出一個內嵌音檔的 `player.html`，以及 SRT/VTT、逐字 JSON、TextGrid。所有音訊分析在本機執行，沒有 SaaS、API key 或訂閱。只有首次安裝／模型下載需要網路。

## 首次安裝（Linux）

支援 **Linux x86_64**（建議 Ubuntu 22.04/24.04 或相容系統），以及 macOS 14 以上的 Intel / Apple Silicon。Linux ARM 與 Windows 尚未提供自動安裝；程式會明確提示，不會嘗試錯誤的平台套件。

1. 安裝 [uv](https://docs.astral.sh/uv/getting-started/installation/) 和 Git。
2. Clone repo，進入目錄：

```sh
git clone <你的遠端網址> podcast-practice
cd podcast-practice
uv sync --locked
uv run podcast-practice setup
uv run podcast-practice doctor
```

目前如果只有本機 repo，可先把 `podcast-practice.bundle` 複製到另一台電腦：

```sh
git clone /path/to/podcast-practice.bundle podcast-practice
cd podcast-practice
uv sync --locked
uv run podcast-practice setup
```

**不必預先安裝 Python、Conda 或 ffmpeg。** uv 管理 Python 與本工具的依賴；`setup` 會下載官方 micromamba 2.9.0，在 `.runtime/` 建立 MFA 3.4.2、Kaldi 與 ffmpeg 的隔離環境，並下載英文 MFA / G2P 模型及本地粗定位模型 `base.en`。

MFA 的原生依賴不能只靠 `uv pip install` 安裝。這個 repo 自動處理那一層，不修改系統 Python，也不需要每天手動 activate Conda。[MFA 官方安裝說明](https://montreal-forced-aligner.readthedocs.io/en/latest/installation.html)

首次安裝請預留數 GB 磁碟空間，耗時取決於下載與電腦速度。之後模型會重用。

## 每天使用

把音檔和 UTF-8 transcript 存到任意位置，再執行：

```sh
uv run podcast-practice build /path/to/today.mp3 /path/to/today.txt \
  --title "今天這集的名稱" --offline --open
```

下一天換兩個檔案即可，無須改程式：

```sh
uv run podcast-practice build /path/to/another.m4a /path/to/another.txt \
  --title "另一個 Podcast" --offline --open
```

- 音訊格式：ffmpeg 可讀取的 MP3、M4A、WAV、FLAC 等。
- 原稿格式：英文 `.txt` / `.md`，或已有時間的 `.srt` / `.vtt`。`.md` 只支援文字段落與標題，不處理複雜 Markdown。
- `--offline` 禁止粗定位模型下載；MFA 模型須先由 `setup` 安裝。實際推論始終在本地進行。
- 每集以內容指紋建立獨立資料夾；換檔即產生新內容，重跑同一組輸入會使用已完成結果。
- `--open` 完成後用系統預設瀏覽器開啟。也可直接開啟產生的 HTML，不需要 server。

查看每天的練習庫：

```sh
uv run podcast-practice serve --open
```

只監聽 `127.0.0.1:8766`。按 Ctrl+C 結束；播放器仍可單獨離線使用。

## Transcript 建議格式

不需自己加時間。可以是正常英文段落，或講者標題：

```text
Host
Welcome to today's episode. We will talk about a new idea.

[MUSIC PLAYING]

Alex Taylor
Thanks for having me. Let me explain how it works.

Host: What should listeners try first?
```

講者名稱會自動偵測，必要時可重複傳入 `--speaker "Alex Taylor"` 明確指定。
`[MUSIC PLAYING]`、`[AD PLAYING]` 等標記不會當成口說內容；一般 URL、Markdown 標題、FT 分享提示會略過。附件中的文字只作為資料，不會執行其中的命令或指示。

顯示文字保留原稿；對齊輸入會展開數字、百分比、年份及大寫縮寫。若數字／縮寫的實際念法與自動展開不同，可在輸入原稿中寫成實際英文念法。語言目前限英文。

## 每集輸出

```text
library/
  index.html
  episode-name-xxxxxxxx/
    player.html         # 音檔內嵌，這一個檔案即可跨電腦離線練習
    audio.mp3
    transcript.txt
    subtitles.srt
    subtitles.vtt
    alignment.json      # 原文、講者、逐字／逐句時間、模型資訊
    alignment.TextGrid
    review.json         # 建議人工核對的區段
    windows.json        # MFA 分句搜尋範圍，可手動修改
```

可用 `--output /path/to/episode` 指定單集目錄。音檔、原稿、模型、`.venv`、處理快取與練習庫都被 `.gitignore` 排除，**clone 只帶程式與 uv.lock**。

## 對齊流程

1. 保留顯示原文，同時建立英文發音正規化輸入。
2. 只為字典沒有的新單字產生 G2P 發音，跨集快取，避免重算整篇發音。
3. MFA 先對整集對齊。
4. 本地 faster-whisper 辨識音訊，將可匹配的字詞作為粗定位，用來排除音樂、廣告及未附原稿的尾段。
5. MFA 在自動取得的句子搜尋範圍內重新對齊，產生最終逐字時間。
6. 驗證字詞完整性與時間順序，再輸出播放器、字幕及核對報告。

已有 SRT/VTT 時使用其時間作為搜尋範圍，不需粗定位辨識。**最終逐字時間都來自 MFA；粗定位辨識不會替換你的 transcript。**

無需為每一集寫固定的音樂時間、講者清單或絕對檔案路徑。音檔與原稿的粗定位吻合率低於 35% 時，工具會停止並提示核對；若原稿確實經過大幅編輯，可加 `--allow-low-confidence` 繼續。

這仍是自動對齊，不能保證每個字都準確。播放器會標示建議核對的句子；原稿缺漏、背景音樂、改寫內容或特殊發音可能需要人工調整。

## 調整與故障排除

模型較小、較快的選擇：

```sh
uv run podcast-practice setup --asr-model tiny.en
```

`build` 預設沿用 setup 的模型。可用 `--asr-model small.en` 選更大的模型；先下載後才能加 `--offline`。

已有 MFA／模型時可重用，不重新下載（只記錄在本機 `.runtime/config.json`）：

```sh
uv run podcast-practice setup --mfa-prefix /path/to/mfa-env \
  --model-dir /path/to/MFA --asr-cache /path/to/asr-cache
```

不使用粗定位模型：`build ... --no-asr`。這時需自行核對音樂與尾段；有 SRT/VTT 的輸入仍使用提供的時間。

修正某一句：複製該集的 `windows.json`，修改那句的 `begin` / `end`（秒），然後執行：

```sh
uv run podcast-practice build /path/to/today.mp3 /path/to/today.txt \
  --title "今天這集的名稱" --windows /path/to/windows.json --offline
```

也可只放需修改的項目，例如 `[{"id": 7, "begin": 41.0, "end": 45.0}]`；`id` 從 0 開始。時間不能超出音檔或與相鄰句子重疊。

更新播放器樣式，不重新跑對齊：

```sh
uv run podcast-practice render library/episode-name-xxxxxxxx
```

`--force` 允許覆寫輸出，仍保留相同輸入的計算快取。若要重新計算同一集，可移走該集 `.runtime/jobs/<指紋>/` 後重跑。失敗或 Ctrl+C 會保留處理快取；詳細 log 在該工作目錄。

## 開發與測試

```sh
uv sync --locked --group dev
uv run pytest
uv run ruff check .
```

測試涵蓋不同原稿、數字／講者解析、音樂與尾段的自動範圍、錯誤／重疊時間拒絕、輸出原文完整性及 HTML 跳脫。普通測試不下載語音模型，也不需要 MFA。

已在 macOS 實際跑通完整 MP3／原稿，以及不同長度的 M4A／原稿，並驗證播放器。Linux x86_64 的固定版本下載網址與 Conda 套件解算已確認可用；目前尚未在 Linux 主機完成實際對齊測試。

套件程式碼在 `src/podcast_practice/`，通用播放器在 `assets/player.html`。工具版本與 Python 依賴鎖定在 `pyproject.toml` / `uv.lock`；MFA 原生套件固定 3.4.2，其 transitive Conda 依賴由 conda-forge 解算，未假裝由 uv.lock 管理。
