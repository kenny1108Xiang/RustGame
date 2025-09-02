# -*- coding: utf-8 -*-
"""
music_downloader.py
需求：
- 讀取 UTF-8 with BOM 的 music_download.txt，每行：<URL><空白><檔名>
- 平行下載(yt-dlp)→檢查音訊→(必要時)ffmpeg 轉 MP3 192k/48kHz/雙聲道→輸出到 ./music
- 產生 done-urllist.txt（UTF-8 with BOM）每行：boombox.serverurllist "檔名,CDN"
- 清單尾端自動補上 'done'（若尚未有）
"""

import os
import sys
import argparse
import shutil
import subprocess
import tempfile
import uuid
import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

# ---- 常數設定（依你的原始值）----
URLLIST_NAME = "done-urllist.txt"
OUTDIR = "music"
GITHUB_USER = "kenny1108Xiang"
GITHUB_REPO = "RustGame"
GITHUB_BRANCH = "main"

# ---- 跨平台：以腳本所在資料夾為工作目錄 ----
def chdir_to_script_root():
    try:
        root = os.path.dirname(os.path.abspath(__file__))
        os.chdir(root)
    except Exception:
        pass

# ---- 編碼輔助：UTF-8 with BOM ----
UTF8_BOM = 'utf-8-sig'

def read_lines_utf8bom(path):
    with open(path, 'r', encoding=UTF8_BOM, newline='') as f:
        return f.read().splitlines()

def write_lines_utf8bom(path, lines):
    # 以 UTF-8 BOM 覆寫
    text = "\n".join(lines)
    # 確保資料夾存在
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, 'w', encoding=UTF8_BOM, newline='') as f:
        f.write(text)

def append_line_utf8bom(path, line):
    # 以 UTF-8 BOM 追加；Python 只會在檔案開頭寫 BOM，所以這裡不會重複插入
    with open(path, 'a', encoding=UTF8_BOM, newline='') as f:
        f.write(("\n" if os.path.getsize(path) > 0 else "") + line)

# ---- 檔名清理（Windows 禁用字元 + 修剪）----
FORBIDDEN = '\\/:*?"<>|'

def sanitize_name(name: str) -> str:
    s = "".join(('_' if c in FORBIDDEN else c) for c in name)
    s = s.strip().rstrip('.')  # 去頭尾空白與尾端句點
    if not s:
        s = "untitled"
    return s

# ---- 外部工具檢查 ----
def ensure_tools():
    missing = []
    for tool in ("yt-dlp", "ffmpeg", "ffprobe"):
        if shutil.which(tool) is None:
            missing.append(tool)
    if missing:
        raise RuntimeError(f"找不到必要工具：{', '.join(missing)}（請安裝並加入 PATH）")

# ---- 呼叫外部程式 ----
def run_cmd(cmd, cwd=None):
    # 回傳 (exitcode, stdout, stderr)
    p = subprocess.run(
        cmd,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding='utf-8',
        errors='replace'  # 避免奇怪編碼炸掉
    )
    return p.returncode, p.stdout, p.stderr

# ---- ffprobe 工具函式 ----
def ffprobe_codec(src_path) -> str:
    code, out, _ = run_cmd(["ffprobe", "-v", "error", "-select_streams", "a:0",
                            "-show_entries", "stream=codec_name",
                            "-of", "default=nw=1:nk=1", src_path])
    if code == 0:
        return (out or "").strip()
    return ""

def ffprobe_bitrate(src_path) -> int:
    code, out, _ = run_cmd(["ffprobe", "-v", "error", "-select_streams", "a:0",
                            "-show_entries", "stream=bit_rate",
                            "-of", "default=nw=1:nk=1", src_path])
    if code == 0:
        s = (out or "").strip().splitlines()[0] if out else "0"
        try:
            return int(s)
        except ValueError:
            return 0
    return 0

# ---- 主要任務：下載 + 轉檔（必要時）----
def process_item(item, outdir, user, repo, branch):
    """
    item: (url, filename)
    回傳 dict：
      {
        "success": bool,
        "file": 檔名 (不含副檔名),
        "cdn": 成功時的 CDN URL,
        "dest": 實際輸出檔路徑,
        "message": 描述
      }
    """
    url, raw_name = item
    safe_name = sanitize_name(raw_name)

    # 為每筆建立獨立暫存資料夾
    tmpdir = tempfile.mkdtemp(prefix="yt_")
    try:
        # 下載（以 uuid 作為基底，讓 yt-dlp 自取副檔名）
        base = os.path.join(tmpdir, uuid.uuid4().hex)
        code, _, err = run_cmd(["yt-dlp", "-f", "bestaudio", url, "-o", f"{base}.%(ext)s"])
        if code != 0:
            return {"success": False, "file": safe_name, "cdn": None, "dest": None, "message": f"yt-dlp 失敗：{err.strip()[:300]}"}

        # 找到實際下載的檔案（base.*）
        src = None
        for name in os.listdir(tmpdir):
            if name.startswith(os.path.basename(base) + "."):
                src = os.path.join(tmpdir, name)
                break
        if not src or not os.path.exists(src):
            return {"success": False, "file": safe_name, "cdn": None, "dest": None, "message": "未找到暫存檔"}

        # 讀取音訊資訊
        codec = ffprobe_codec(src)
        bps = ffprobe_bitrate(src)

        # 目標檔名，若撞名則加時間戳
        dest_base = os.path.join(outdir, f"{safe_name}.mp3")
        if os.path.exists(dest_base):
            ts = datetime.datetime.now().strftime("%Y%m%d%H%M%S%f")
            safe_name = f"{safe_name}-{ts}"
            dest_base = os.path.join(outdir, f"{safe_name}.mp3")

        # 轉檔或直移（~192kbps mp3 直接移動）
        if src.lower().endswith(".mp3") and codec.lower() == "mp3" and 192000 <= bps <= 192500:
            # 直接移動
            shutil.move(src, dest_base)
        else:
            # ffmpeg 轉檔
            code, _, err = run_cmd([
                "ffmpeg", "-y", "-i", src,
                "-ar", "48000", "-ac", "2", "-b:a", "192k", dest_base
            ])
            # 轉檔完清掉來源暫存
            try:
                if os.path.exists(src):
                    os.remove(src)
            except Exception:
                pass
            if code != 0:
                return {"success": False, "file": safe_name, "cdn": None, "dest": None, "message": f"ffmpeg 轉檔失敗：{err.strip()[:300]}"}

        cdn = f"https://cdn.jsdelivr.net/gh/{user}/{repo}@{branch}/music/{safe_name}.mp3"
        return {"success": True, "file": safe_name, "cdn": cdn, "dest": dest_base, "message": "OK"}
    except Exception as e:
        return {"success": False, "file": safe_name, "cdn": None, "dest": None, "message": f"例外：{e}"}
    finally:
        # 刪暫存目錄
        try:
            shutil.rmtree(tmpdir, ignore_errors=True)
        except Exception:
            pass

# ---- 主流程 ----
def main():
    chdir_to_script_root()

    parser = argparse.ArgumentParser(description="Music downloader (yt-dlp + ffmpeg)")
    parser.add_argument("--list", default="music_download.txt", help="清單檔路徑（UTF-8 with BOM）")
    parser.add_argument("--threads", type=int, default=0, help="並行執行緒數（預設 0=自動）")
    args = parser.parse_args()

    # 確保輸出資料夾
    os.makedirs(OUTDIR, exist_ok=True)

    # 清單檔存在性
    if not os.path.exists(args.list):
        print(f"找不到 {args.list}（目前目錄：{os.getcwd()}）", flush=True)
        return 1

    ensure_tools()

    # 讀清單 + 檢查是否已完成
    lines = read_lines_utf8bom(args.list)
    last_non_empty = next((ln for ln in reversed(lines) if ln.strip() != ""), None)
    if last_non_empty and last_non_empty.strip().lower() == "done":
        print("此清單已經完成，請修改清單後再執行")
        return 0

    # 解析：<URL><空白><檔名>；遇到 done 停止
    items = []
    for line in lines:
        if not line.strip():
            continue
        if line.strip().lower() == "done":
            break
        parts = line.split(None, 1)
        if len(parts) < 2:
            print(f"格式錯誤，略過：{line}")
            continue
        url = parts[0]
        filename = parts[1]
        items.append((url, filename))

    if not items:
        print("清單為空或僅有 done")
        return 0

    # 自動 threads
    cpu = os.cpu_count() or 4
    auto_threads = min(len(items), min(cpu * 3, 24))
    threads = auto_threads if args.threads <= 0 else args.threads
    print(f"檢測到 {len(items)} 首曲目；CPU={cpu}；並行數設定為 {threads}")

    # 下載/轉檔（平行）
    results = []
    with ThreadPoolExecutor(max_workers=threads) as ex:
        future_to_item = {ex.submit(process_item, it, OUTDIR, GITHUB_USER, GITHUB_REPO, GITHUB_BRANCH): it for it in items}
        for fut in as_completed(future_to_item):
            res = fut.result()
            results.append(res)
            # 即時列印進度
            if res["success"]:
                print(f"[OK] {res['file']}")
            else:
                print(f"[FAIL] {res['file']} - {res['message']}")

    # 彙整輸出（僅成功者）
    out_lines = [f'boombox.serverurllist "{r["file"]},{r["cdn"]}"' for r in results if r.get("success")]
    # 寫 done-urllist.txt（UTF-8 BOM）
    write_lines_utf8bom(URLLIST_NAME, out_lines)

    # 清單尾端補 'done'
    if not (last_non_empty and last_non_empty.strip().lower() == "done"):
        try:
            append_line_utf8bom(args.list, "done")
        except Exception:
            # 保底：重寫整個檔案（保留原內容 + done）
            try:
                write_lines_utf8bom(args.list, lines + ["done"])
            except Exception:
                pass

    ok_count = sum(1 for r in results if r.get("success"))
    fail_count = sum(1 for r in results if not r.get("success"))
    print(f"完成 ✅ 成功：{ok_count}，失敗：{fail_count}；輸出清單：{URLLIST_NAME}")

    return 0

if __name__ == "__main__":
    sys.exit(main())
