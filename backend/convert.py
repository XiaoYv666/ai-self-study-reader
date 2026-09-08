"""LibreOffice headless 转换封装（M2：pptx/docx → pdf）。

技术文档 §1 选型：一条转换链覆盖 PPT/Word；1 slide = 1 page，页码定位天然成立。

约定：
- soffice 路径自动探测（各平台默认安装位 → PATH），探测不到按平台给出可操作的报错
- subprocess --headless --convert-to pdf --outdir；180s 超时；失败原因透传
- 输出文件名由 LibreOffice 决定（同 basename 换 .pdf 后缀），转换后改名归位
- 纯同步函数：路由层用 asyncio.to_thread 调，不阻塞事件循环
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

def _windows_soffice_candidates() -> list[str]:
    """Windows 默认安装位（Program Files 64/32 位两处 + ProgramW6432）。

    soffice.exe / soffice.com 均为可执行入口（前者 GUI 子系统、后者控制台
    子系统的包装，headless 均可用），两个都探测。非 Windows 环境变量缺失
    时返回空列表（os.path.join("", ...) 会拼出相对路径，必须过滤）。
    """
    candidates: list[str] = []
    for env_var in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432"):
        base = os.environ.get(env_var, "")
        if not base:
            continue
        for exe in ("soffice.exe", "soffice.com"):
            candidates.append(os.path.join(base, "LibreOffice", "program", exe))
    return candidates


# 各平台常见安装位（无需 PATH 即可命中；其余场景统一走 which 兜底）
# - macOS：Homebrew cask 默认装到 /Applications；Intel/Homebrew 手动链
# - Linux：发行版包管理器默认 /usr/bin、/usr/local/bin；Homebrew on Linux /opt/homebrew
# - Windows：见 _windows_soffice_candidates()（Program Files 下的默认安装位）
SOFFICE_CANDIDATES = [
    # macOS
    "/Applications/LibreOffice.app/Contents/MacOS/soffice",
    "/usr/local/bin/soffice",
    "/opt/homebrew/bin/soffice",
    # Linux
    "/usr/bin/soffice",
] + _windows_soffice_candidates()


def _soffice_install_hint() -> str:
    """按当前平台返回 LibreOffice 安装提示文案。"""
    if sys.platform.startswith("win"):
        return (
            "Windows：从 https://www.libreoffice.org 下载安装（默认安装到 Program Files，"
            "重启应用后可自动探测）；或将安装目录下的 program 子目录加入 PATH"
        )
    if sys.platform == "darwin":
        return "macOS：brew install --cask libreoffice，或从 https://www.libreoffice.org 下载安装"
    return "Linux：sudo apt install libreoffice（Debian/Ubuntu）或 sudo dnf install libreoffice（Fedora）"


CONVERT_TIMEOUT_S = 180  # 100MB 大文件转换慢（风险 §9），超时报「手动导出 PDF」


def find_soffice() -> str | None:
    """探测 soffice 可执行文件路径；找不到返回 None。

    顺序：各平台默认安装位 → PATH（which）。Windows 上 which 也能命中
    soffice.exe / soffice.com（program 目录加入 PATH 的场景）。
    """
    for cand in SOFFICE_CANDIDATES:
        if Path(cand).is_file():
            return cand
    for name in ("soffice", "soffice.exe", "soffice.com"):
        found = shutil.which(name)
        if found:
            return found
    return None


def convert_to_pdf(src: str | Path, out_dir: str | Path, timeout_s: int = CONVERT_TIMEOUT_S) -> Path:
    """LibreOffice headless 转 PDF。

    - src：原文件（pptx/docx/…LibreOffice 支持的任意格式）
    - out_dir：输出目录（data/pdfs/）；返回生成的 PDF 路径
    - 抛 RuntimeError（含 stderr 原因），由调用方转成 500 + 手动导出提示
    """
    src = Path(src).resolve()  # 统一绝对路径，避免受调用方 CWD 影响
    out_dir = Path(out_dir)
    if not src.is_file():
        raise RuntimeError(f"待转换文件不存在：{src}")

    soffice = find_soffice()
    if not soffice:
        raise RuntimeError(
            f"未找到 LibreOffice（soffice）。请安装：{_soffice_install_hint()}，"
            "或手动将文件导出为 PDF 后上传"
        )

    out_dir.mkdir(parents=True, exist_ok=True)
    # LibreOffice 输出名 = src 同名换 .pdf；若已存在会被直接覆盖（无 --outdir 冲突问题）
    expected = out_dir / (src.stem + ".pdf")

    # -env:UserInstallation 隔离用户配置目录，避免并发调用/残留锁导致 headless 假死
    # 注意：file:// 后必须是绝对路径——相对路径会拼出非法 URL，soffice 静默挂起直到超时
    profile_url = Path(out_dir / ".lo_profile").resolve().as_uri()
    cmd = [
        soffice,
        "--headless",
        "--norestore",
        f"-env:UserInstallation={profile_url}",
        "--convert-to", "pdf",
        "--outdir", str(out_dir),
        str(src),
    ]
    try:
        proc = subprocess.run(  # noqa: S603 —— 参数受控
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(
            f"LibreOffice 转换超时（>{timeout_s}s）：{src.name}。"
            "可手动导出 PDF 后上传"
        ) from None

    if proc.returncode != 0 or not expected.is_file():
        reason = (proc.stderr or proc.stdout or "").strip().splitlines()
        reason_tail = reason[-1] if reason else f"exit={proc.returncode}"
        raise RuntimeError(
            f"LibreOffice 转换失败（{src.name}）：{reason_tail}。"
            "可手动导出 PDF 后上传（部分动画/特殊字体排版会失真）"
        )
    return expected
