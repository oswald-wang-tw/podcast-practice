"""Provision native MFA dependencies locally, without changing system Python."""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import tarfile
import urllib.request
from pathlib import Path

from filelock import FileLock

from .errors import PracticeError

MFA_VERSION = "3.4.2"
MICROMAMBA_VERSION = "2.9.0"
MODELS = {"acoustic": "english_mfa", "dictionary": "english_mfa", "g2p": "english_us_mfa"}


def conda_platform(system: str | None = None, machine: str | None = None) -> str:
    system, machine = system or platform.system(), (machine or platform.machine()).lower()
    if system == "Darwin" and machine in {"arm64", "aarch64"}:
        return "osx-arm64"
    if system == "Darwin" and machine in {"x86_64", "amd64"}:
        return "osx-64"
    if system == "Linux" and machine in {"x86_64", "amd64"}:
        return "linux-64"
    raise PracticeError(
        f"目前自動安裝支援 Linux x86_64 與 macOS（Intel / Apple Silicon）；"
        f"偵測到 {system} {machine}。可用 --mfa-prefix 指向自行安裝的 MFA 環境。"
    )


class Runtime:
    def __init__(self, root: Path):
        self.root = root.expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.config_path = self.root / "config.json"
        self.config = json.loads(self.config_path.read_text()) if self.config_path.exists() else {}
        self.prefix = Path(self.config.get("mfa_prefix", self.root / "mfa-env"))
        self.state = Path(self.config.get("model_dir", self.root / "mfa-state"))
        self.asr_cache = Path(self.config.get("asr_cache", self.root / "asr-models"))
        self.lock = FileLock(str(self.root / "runtime.lock"), timeout=1)

    @property
    def bin(self) -> Path:
        return self.prefix / "bin"

    @property
    def mfa(self) -> Path:
        return self.bin / "mfa"

    def environment(self) -> dict[str, str]:
        env = os.environ.copy()
        env.update(
            {
                "PATH": str(self.bin) + os.pathsep + env.get("PATH", ""),
                "MFA_ROOT_DIR": str(self.state),
                "MAMBA_ROOT_PREFIX": str(self.root / "conda"),
                "XDG_CACHE_HOME": str(self.root / "cache"),
                "MPLCONFIGDIR": str(self.root / "cache" / "matplotlib"),
                "NUMBA_CACHE_DIR": str(self.root / "cache" / "numba"),
                "HF_HOME": str(self.root / "huggingface"),
                "OPENBLAS_NUM_THREADS": "2",
                "OMP_NUM_THREADS": "2",
                "PYTHONUNBUFFERED": "1",
                "LC_ALL": "C.UTF-8" if platform.system() == "Linux" else "en_US.UTF-8",
            }
        )
        for key in ["MFA_ROOT_DIR", "XDG_CACHE_HOME", "MPLCONFIGDIR", "NUMBA_CACHE_DIR", "HF_HOME"]:
            Path(env[key]).mkdir(parents=True, exist_ok=True)
        return env

    def run(self, args: list[str | Path], *, log: Path | None = None) -> None:
        """No shell interpolation; filenames containing spaces/metacharacters are safe."""
        command = [str(arg) for arg in args]
        handle = log.open("a", encoding="utf-8") if log else None
        try:
            with subprocess.Popen(
                command,
                env=self.environment(),
                cwd=self.root,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                errors="replace",
            ) as proc:
                assert proc.stdout is not None
                try:
                    for line in proc.stdout:
                        print(line, end="", flush=True)
                        if handle:
                            handle.write(line)
                    code = proc.wait()
                except KeyboardInterrupt:
                    proc.terminate()
                    try:
                        proc.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                    raise
            if code:
                hint = f"，詳細記錄：{log}" if log else ""
                raise PracticeError(f"{Path(command[0]).name} 執行失敗（exit {code}）{hint}")
        except FileNotFoundError as exc:
            raise PracticeError("找不到本地 MFA/ffmpeg，請先執行 podcast-practice setup。") from exc
        finally:
            if handle:
                handle.close()

    def require_ready(self) -> None:
        if not self.mfa.is_file() or not (self.bin / "ffmpeg").is_file():
            raise PracticeError("本地 MFA 尚未安裝。請先執行 uv run podcast-practice setup。")
        missing = [name for kind, name in MODELS.items() if not self.model_path(kind).exists()]
        if missing:
            raise PracticeError(f"缺少模型：{', '.join(missing)}。請重新執行 setup。")

    def model_path(self, kind: str) -> Path:
        extension = "dict" if kind == "dictionary" else "zip"
        return self.state / "pretrained_models" / kind / f"{MODELS[kind]}.{extension}"

    def install(
        self,
        *,
        mfa_prefix: Path | None = None,
        model_dir: Path | None = None,
        asr_cache: Path | None = None,
        asr_model: str = "base.en",
        skip_asr: bool = False,
    ) -> None:
        if mfa_prefix:
            self.prefix = mfa_prefix.expanduser().resolve()
            if not (self.prefix / "bin" / "mfa").is_file():
                raise PracticeError("--mfa-prefix 必須指向已有 bin/mfa 的 Conda/MFA 環境。")
        if model_dir:
            self.state = model_dir.expanduser().resolve()
        if asr_cache:
            self.asr_cache = asr_cache.expanduser().resolve()
        if not self.mfa.is_file():
            subdir = conda_platform()
            executable = self.root / "bin" / "micromamba"
            executable.parent.mkdir(parents=True, exist_ok=True)
            if not executable.exists():
                url = f"https://micro.mamba.pm/api/micromamba/{subdir}/{MICROMAMBA_VERSION}"
                archive = self.root / "micromamba.tar.bz2"
                print(f"下載 micromamba {MICROMAMBA_VERSION}（{subdir}）…", flush=True)
                request = urllib.request.Request(url, headers={"User-Agent": "podcast-practice"})
                try:
                    with urllib.request.urlopen(request, timeout=60) as response:
                        with archive.open("wb") as target:
                            shutil.copyfileobj(response, target)
                    # Read only the expected binary; never extract arbitrary archive paths.
                    with tarfile.open(archive, "r:bz2") as bundle:
                        member = bundle.getmember("bin/micromamba")
                        source = bundle.extractfile(member)
                        if source is None or not member.isfile():
                            raise PracticeError("micromamba 壓縮檔沒有預期的執行檔。")
                        with executable.open("wb") as target:
                            shutil.copyfileobj(source, target)
                    executable.chmod(0o755)
                except OSError as exc:
                    raise PracticeError(f"下載 micromamba 失敗：{exc}") from exc
            print("建立 MFA 隔離環境；首次會下載數百 MB 的原生依賴…", flush=True)
            self.run(
                [
                    executable,
                    "create",
                    "-p",
                    self.prefix,
                    "-c",
                    "conda-forge",
                    "--override-channels",
                    "python=3.11",
                    f"montreal-forced-aligner={MFA_VERSION}",
                    "ffmpeg",
                    "-y",
                ]
            )
        self.run([self.mfa, "version"])
        for kind, name in MODELS.items():
            if not self.model_path(kind).is_file():
                print(f"下載 MFA {kind} 模型：{name}…", flush=True)
                self.run([self.mfa, "model", "download", kind, name])
        self.config.update(
            {
                "mfa_prefix": str(self.prefix),
                "model_dir": str(self.state),
                "asr_cache": str(self.asr_cache),
                "asr_model": asr_model,
                "mfa_version": MFA_VERSION,
            }
        )
        if not skip_asr:
            from .anchors import load_asr

            print(f"準備本地粗定位模型 {asr_model}…", flush=True)
            load_asr(self, asr_model, offline=False)
        self.config_path.write_text(json.dumps(self.config, ensure_ascii=False, indent=2))
        self.require_ready()
        print("Setup 完成。之後可用 build 指定每天的音檔與 transcript。", flush=True)

    def lexicon(self, tokens: list[str], job: Path) -> Path:
        """Generate only missing pronunciations and persist them across episodes."""
        base = self.model_path("dictionary").read_text(encoding="utf-8")
        cache_path = self.root / "pronunciations.dict"
        cache = cache_path.read_text(encoding="utf-8") if cache_path.exists() else ""
        known = {line.split()[0] for line in (base + "\n" + cache).splitlines() if line.strip()}
        unknown = sorted(set(tokens) - known)
        if unknown:
            print(f"產生 {len(unknown)} 個新單字／專有名詞的發音…", flush=True)
            source, generated = job / "unknown-words.txt", job / "new-pronunciations.dict"
            source.write_text("\n".join(unknown) + "\n", encoding="utf-8")
            self.run(
                [
                    self.mfa,
                    "g2p",
                    source,
                    MODELS["g2p"],
                    generated,
                    "--num_pronunciations",
                    "1",
                    "--no_use_mp",
                ],
                log=job / "mfa.log",
            )
            fresh = generated.read_text(encoding="utf-8")
            produced = {line.split()[0] for line in fresh.splitlines() if len(line.split()) > 1}
            missing = set(unknown) - produced
            if missing:
                raise PracticeError(
                    "無法產生這些字的發音，請在原稿中改寫：" + ", ".join(sorted(missing))
                )
            cache += "\n" + fresh
            cache_path.write_text(cache, encoding="utf-8")
        destination = job / f"lexicon-{job.name}.dict"
        destination.write_text(base.rstrip() + "\n" + cache.strip() + "\n", encoding="utf-8")
        return destination
