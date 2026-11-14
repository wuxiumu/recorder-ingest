#!/usr/bin/env python3
"""recorder-ingest — 索尼 IC 录音笔自动入库 + 本地语音转写

每轮运行做三件事：
1. 扫描录音笔目录里的新音频（自动跳过还在写入的文件）
2. 复制到 Obsidian 知识库（录音笔/音频/<日期>/），并用 whisper.cpp 本地转写成 Markdown 笔记
3. 确认副本完整（字节数一致）后，删除录音笔里的源文件

全程本地运行，不调用云端 API，零 token 消耗。
幂等设计：以「文件名|字节数」为键记录处理状态，重复运行不会重复处理。

可用环境变量覆盖默认配置：
  RECORDER_SRC   录音笔目录（默认 /Volumes/IC RECORDER/REC_FILE/FOLDER01）
  VAULT_DIR      知识库根目录（默认 ~/Documents/Obsidian Vault）
  WHISPER_MODEL  whisper 模型路径（默认 ~/Documents/kimi/models/ggml-small.bin）
"""

import json
import os
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path

SRC_DIR = Path(os.environ.get("RECORDER_SRC", "/Volumes/IC RECORDER/REC_FILE/FOLDER01"))
VAULT = Path(os.environ.get("VAULT_DIR", str(Path.home() / "Documents" / "Obsidian Vault")))
AUDIO_DIR = VAULT / "录音笔" / "音频"
NOTE_DIR = VAULT / "录音笔" / "转写"
MODEL = os.environ.get("WHISPER_MODEL", str(Path.home() / "Documents" / "kimi" / "models" / "ggml-small.bin"))
AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".wma", ".aac", ".flac", ".ogg"}
WHISPER_TIMEOUT = 1500  # 单个文件转写最长 25 分钟


def _workspace() -> Path:
    """状态目录：优先用定时任务注入的工作区，否则用脚本旁的 workspace/。"""
    root = os.environ.get("DAIMON_BLUEPRINT_AUTOMATION_WORKSPACE_PATH")
    p = Path(root) if root else Path(__file__).resolve().parent / "workspace"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _load_state() -> dict:
    f = _workspace() / "processed.json"
    if f.exists():
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save_state(state: dict) -> None:
    (_workspace() / "processed.json").write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _file_key(path: Path) -> str:
    st = path.stat()
    return f"{path.name}|{st.st_size}"


def _is_stable(path: Path) -> bool:
    """确认文件已写完（大小两秒内不再变化）。"""
    try:
        s1 = path.stat().st_size
        time.sleep(2)
        return path.stat().st_size == s1 and s1 > 0
    except OSError:
        return False


def _duration_sec(path: Path) -> float:
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=60,
        )
        return round(float(out.stdout.strip()), 1)
    except Exception:
        return 0.0


def _unique(dest_dir: Path, name: str) -> Path:
    dest = dest_dir / name
    if not dest.exists():
        return dest
    stem, suffix = Path(name).stem, Path(name).suffix
    for i in range(1, 100):
        cand = dest_dir / f"{stem}_{i}{suffix}"
        if not cand.exists():
            return cand
    raise RuntimeError(f"无法为 {name} 生成不重名的目标文件")


def _dest_for(src: Path, dest_dir: Path) -> tuple:
    """返回 (目标路径, 是否已存在完整副本)。同名同大小视为已复制，直接复用。"""
    plain = dest_dir / src.name
    if plain.exists() and plain.stat().st_size == src.stat().st_size:
        return plain, True
    return _unique(dest_dir, src.name), False


def _copy_audio(src: Path, dest: Path) -> None:
    # copyfile 只拷数据；元数据（chflags/扩展属性）在部分 macOS 环境会触发 EPERM，单独忽略
    shutil.copyfile(src, dest)
    try:
        shutil.copystat(src, dest)
    except OSError:
        pass


def _transcribe(audio: Path, work: Path) -> str:
    """ffmpeg 转成 16k 单声道 wav，再交给 whisper.cpp（Metal GPU 加速）。"""
    wav = work / (audio.stem + "_16k.wav")
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", str(audio),
         "-ar", "16000", "-ac", "1", str(wav)],
        check=True, capture_output=True, timeout=600,
    )
    out_base = work / (audio.stem + "_out")
    subprocess.run(
        ["whisper-cli", "-m", MODEL, "-l", "zh",
         "--prompt", "以下是普通话录音的转写内容。",  # 引导输出简体中文
         "-otxt", "-of", str(out_base), str(wav)],
        check=True, capture_output=True, timeout=WHISPER_TIMEOUT,
    )
    return out_base.with_suffix(".txt").read_text(encoding="utf-8").strip()


def _write_note(audio_saved: Path, date_dir: str, transcript: str,
                duration: float, source_name: str) -> Path:
    NOTE_DIR.mkdir(parents=True, exist_ok=True)
    note = _unique(NOTE_DIR, audio_saved.stem + ".md")
    rel_audio = f"../音频/{date_dir}/{audio_saved.name}"
    body = [
        "---",
        f'标题: "{audio_saved.stem}"',
        "来源: 录音笔",
        f'原始文件: "{source_name}"',
        f"时长(秒): {duration}",
        f"入库时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "标签: [录音笔, 语音转写]",
        "---",
        "",
        f"# {audio_saved.stem}",
        "",
        f"![[{rel_audio}]]",
        "",
        "## 转写全文",
        "",
        transcript or "（无内容）",
        "",
    ]
    note.write_text("\n".join(body), encoding="utf-8")
    return note


def run(ctx=None):
    """主入口。返回结构化结果，既可被定时任务框架调用，也可独立运行。"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if not SRC_DIR.is_dir():
        return {"artifact": {
            "status": "recorder_offline",
            "message": "录音笔未连接，本轮跳过",
            "checkedAt": now,
            "processed": [],
            "errors": [],
        }}

    def delete_source(src: Path, rec: dict) -> bool:
        """知识库已有完整副本（大小一致）才删除录音笔源文件。"""
        saved = Path(rec["savedTo"])
        if saved.exists() and saved.stat().st_size == src.stat().st_size:
            src.unlink()
            return True
        return False

    state = _load_state()
    processed, deleted, errors = [], [], []

    files = sorted(
        (p for p in SRC_DIR.iterdir()
         if p.is_file() and p.suffix.lower() in AUDIO_EXTS),
        key=lambda p: p.stat().st_mtime,
    )

    for f in files:
        key = _file_key(f)
        rec = state.get(key, {})

        # 已完整入库（包括历史轮次）：只需确保源文件被删除
        if rec.get("copied") and rec.get("transcribed") and rec.get("savedTo"):
            try:
                if delete_source(f, rec):
                    deleted.append(f.name)
            except Exception as e:
                errors.append(f"{f.name}: 删除源文件失败 - {e}")
            continue

        if not _is_stable(f):
            errors.append(f"{f.name}: 文件仍在写入，留待下一轮")
            continue

        date_dir = datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m-%d")
        dest_dir = AUDIO_DIR / date_dir
        dest_dir.mkdir(parents=True, exist_ok=True)

        try:
            saved = Path(rec["savedTo"]) if rec.get("savedTo") else None
            if not rec.get("copied") or not saved or not saved.exists():
                saved, already = _dest_for(f, dest_dir)
                if not already:
                    _copy_audio(f, saved)
                rec.update({"copied": True, "savedTo": str(saved)})
                state[key] = rec
                _save_state(state)

            # 副本确认完整后，无论转写是否成功都删除源文件
            try:
                if delete_source(f, rec):
                    deleted.append(f.name)
            except Exception as e:
                errors.append(f"{f.name}: 删除源文件失败 - {e}")

            duration = _duration_sec(saved)
            transcript = _transcribe(saved, _workspace())
            note = _write_note(saved, date_dir, transcript, duration, f.name)

            rec.update({"transcribed": True, "notePath": str(note)})
            state[key] = rec
            _save_state(state)
            processed.append({
                "name": f.name,
                "savedTo": str(saved),
                "notePath": str(note),
                "durationSec": duration,
            })
        except Exception as e:  # 单个文件失败不阻塞其余文件
            state[key] = rec
            _save_state(state)
            errors.append(f"{f.name}: {e}")

    if processed:
        status = "ok"
        msg = f"新入库 {len(processed)} 条录音并完成转写"
    elif errors:
        status, msg = "partial", f"无新增完成项，{len(errors)} 个文件待处理或出错"
    else:
        status, msg = "no_new", "没有新录音"
    if deleted:
        msg += f"；已清空录音笔 {len(deleted)} 个源文件"

    return {"artifact": {
        "status": status,
        "message": msg,
        "checkedAt": now,
        "processed": processed,
        "deletedSources": deleted,
        "errors": errors,
    }}


if __name__ == "__main__":
    print(json.dumps(run()["artifact"], ensure_ascii=False, indent=2))
