"""Auto-installer for the whisper.cpp voice transcription backend.

Bot users see a confirmation prompt (OK/Cancel) before any system change
runs; ``install_async`` is gated behind that callback. The install path
is idempotent — re-running a partially completed install picks up where
it left off.

Steps performed (each skipped when already done):
  1. ``apt-get install`` build toolchain + ``ffmpeg`` (needed by
     transcribe.py to convert OGG → 16k mono WAV).
  2. ``git clone`` whisper.cpp into ``WHISPER_SRC``.
  3. ``cmake`` build (Release).
  4. Copy the produced ``whisper-cli`` binary to ``/usr/local/bin``.
  5. Download ``ggml-medium.bin`` (~1.5 GB) into ``config.whisper_model_path``.

Progress is reported through an async callback so the caller can stream
chat messages between steps.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
from collections.abc import Awaitable, Callable
from pathlib import Path

from .config import config

logger = logging.getLogger(__name__)


WHISPER_SRC = Path("/opt/whisper.cpp")
WHISPER_BIN_DST = Path("/usr/local/bin/whisper-cli")
MODEL_URL = "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-medium.bin"


ProgressCB = Callable[[str], Awaitable[None]]


def whisper_bin_path() -> Path | None:
    """Resolve where ``whisper-cli`` lives now, or None if missing."""
    found = shutil.which(config.whisper_bin) or shutil.which("whisper-cli")
    if found:
        return Path(found)
    if WHISPER_BIN_DST.exists():
        return WHISPER_BIN_DST
    return None


def whisper_model_path() -> Path:
    return Path(config.whisper_model_path)


def is_bin_ready() -> bool:
    return whisper_bin_path() is not None


def is_model_ready() -> bool:
    p = whisper_model_path()
    # Sanity threshold so a half-finished download doesn't masquerade
    # as a valid model. The medium model is ~1.5 GB.
    return p.exists() and p.stat().st_size > 500 * 1024 * 1024


def is_ready() -> bool:
    return is_bin_ready() and is_model_ready()


def describe_plan() -> str:
    """Return a human-readable list of remaining install steps.

    Used by the Telegram confirmation prompt so the user knows what
    they're authorising before tapping OK.
    """
    lines: list[str] = []
    if not is_bin_ready():
        lines.append("• `apt-get install` build-essential cmake git ffmpeg")
        lines.append(
            "• `git clone` whisper.cpp → `/opt/whisper.cpp` + cmake build "
            "(на arm64 это 5–10 минут)"
        )
        lines.append("• cp `build/bin/whisper-cli` → `/usr/local/bin/whisper-cli`")
    if not is_model_ready():
        lines.append(
            f"• скачать `ggml-medium.bin` (~1.5 GB) в `{config.whisper_model_path}`"
        )
    return "\n".join(lines)


async def _run(
    cmd: list[str],
    *,
    cwd: Path | None = None,
    timeout: float = 600.0,
) -> tuple[int, str]:
    """Run a subprocess and capture combined stdout+stderr."""
    logger.info("voice_install: run %s (cwd=%s)", " ".join(cmd), cwd)
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        cwd=str(cwd) if cwd else None,
    )
    try:
        stdout_b, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        try:
            await proc.wait()
        except Exception:
            pass
        return 124, "timeout"
    return proc.returncode or 0, (stdout_b or b"").decode("utf-8", "replace")


def _tail(out: str, n: int = 300) -> str:
    out = out.strip()
    if len(out) <= n:
        return out
    return "…" + out[-n:]


async def _install_bin(progress: ProgressCB) -> bool:
    """Toolchain + clone + build + copy. Returns False on failure."""
    await progress("1/4 `apt-get install` build toolchain + ffmpeg…")
    rc, out = await _run(
        [
            "apt-get",
            "install",
            "-y",
            "--no-install-recommends",
            "build-essential",
            "cmake",
            "git",
            "ffmpeg",
        ],
        timeout=900,
    )
    if rc != 0:
        await progress(f"❌ apt-get failed (rc={rc})\n```\n{_tail(out)}\n```")
        return False

    if not WHISPER_SRC.exists():
        await progress(f"2/4 `git clone` whisper.cpp → `{WHISPER_SRC}`…")
        WHISPER_SRC.parent.mkdir(parents=True, exist_ok=True)
        rc, out = await _run(
            [
                "git",
                "clone",
                "--depth=1",
                "https://github.com/ggerganov/whisper.cpp.git",
                str(WHISPER_SRC),
            ],
            timeout=600,
        )
        if rc != 0:
            await progress(f"❌ git clone failed (rc={rc})\n```\n{_tail(out)}\n```")
            return False
    else:
        await progress(f"2/4 `{WHISPER_SRC}` уже есть, пропускаю clone")

    await progress(
        "3/4 cmake build (Release) — это самый долгий шаг, ~5–10 мин на arm64…"
    )
    rc, out = await _run(
        ["cmake", "-B", "build", "-DCMAKE_BUILD_TYPE=Release"],
        cwd=WHISPER_SRC,
        timeout=600,
    )
    if rc != 0:
        await progress(f"❌ cmake config failed (rc={rc})\n```\n{_tail(out)}\n```")
        return False
    nproc = os.cpu_count() or 2
    rc, out = await _run(
        ["cmake", "--build", "build", "--config", "Release", "-j", str(nproc)],
        cwd=WHISPER_SRC,
        timeout=1800,
    )
    if rc != 0:
        await progress(f"❌ cmake build failed (rc={rc})\n```\n{_tail(out)}\n```")
        return False

    src_bin = WHISPER_SRC / "build" / "bin" / "whisper-cli"
    if not src_bin.exists():
        # Fallback layout some whisper.cpp builds use.
        alt = WHISPER_SRC / "build" / "whisper-cli"
        if alt.exists():
            src_bin = alt
    if not src_bin.exists():
        await progress(
            f"❌ собранный бинарник не найден ни в `{WHISPER_SRC}/build/bin/`, "
            f"ни в `{WHISPER_SRC}/build/`"
        )
        return False

    await progress(f"4/4 копирую `whisper-cli` → `{WHISPER_BIN_DST}`…")
    try:
        WHISPER_BIN_DST.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_bin, WHISPER_BIN_DST)
        os.chmod(WHISPER_BIN_DST, 0o755)
    except OSError as e:
        await progress(f"❌ copy failed: {e}")
        return False
    return True


async def _download_model(progress: ProgressCB) -> bool:
    """Stream ``ggml-medium.bin`` to disk via wget (curl fallback)."""
    target = whisper_model_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".part")
    if tmp.exists():
        # Stale half-download — drop it so wget starts clean.
        try:
            tmp.unlink()
        except OSError:
            pass

    await progress(
        "Скачиваю `ggml-medium.bin` (~1.5 GB). Зависит от канала — "
        "пара минут на быстром, дольше на мобильном."
    )
    fetcher = shutil.which("wget") or shutil.which("curl")
    if fetcher is None:
        await progress("❌ ни `wget`, ни `curl` не найдены в PATH")
        return False

    if fetcher.endswith("wget"):
        cmd = [fetcher, "-q", "-O", str(tmp), MODEL_URL]
    else:
        cmd = [fetcher, "-fsSL", "-o", str(tmp), MODEL_URL]
    rc, out = await _run(cmd, timeout=3600)
    if rc != 0:
        await progress(f"❌ загрузка не удалась (rc={rc})\n```\n{_tail(out)}\n```")
        try:
            tmp.unlink()
        except OSError:
            pass
        return False

    # Sanity check the size before promoting the .part to final.
    if tmp.stat().st_size < 500 * 1024 * 1024:
        await progress(
            f"❌ скачанный файл подозрительно мал ({tmp.stat().st_size} байт) — "
            "не похоже на ggml-medium.bin. Удалил."
        )
        try:
            tmp.unlink()
        except OSError:
            pass
        return False

    try:
        tmp.rename(target)
    except OSError as e:
        await progress(f"❌ rename `.part` → final failed: {e}")
        return False
    return True


async def install_async(progress: ProgressCB) -> bool:
    """Run every missing install step. Idempotent — completed steps are
    detected and skipped.

    ``progress(text)`` is awaited per step and on terminal success/failure.
    Returns True on overall success.
    """
    if is_ready():
        await progress("✅ whisper.cpp уже установлен и модель на месте.")
        return True

    if not is_bin_ready():
        if not await _install_bin(progress):
            return False

    if not is_model_ready():
        if not await _download_model(progress):
            return False

    if not is_ready():
        await progress(
            "⚠ install_async завершился, но `is_ready()` всё ещё False — "
            "проверь PATH / права на /usr/local/bin"
        )
        return False

    await progress(
        "✅ Готово. Бэкенд `whisper.cpp` работоспособен — отправь "
        "голосовое в чат, чтобы проверить."
    )
    return True
