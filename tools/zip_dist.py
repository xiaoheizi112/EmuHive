"""tools/zip_dist.py — 把 dist/EmuHive/ 打成**真 zip**（bsdtar 教训专用）

2026-10-08 事故：Windows 自带 tar.exe 的 `-a` 按扩展名自动选格式在打包机上
失灵，产出的是改了 .zip 后缀的纯 tar——1607 端解压中文目录名乱码 → 模板库
全体找不到 → 挂机 .shape 风暴。zipfile 写出的包带规范 UTF-8 旗标，资源管理
器/WinRAR/7-Zip 解压中文名都不歪。

用法：python tools/zip_dist.py   （产物 dist/EmuHive.zip）
"""
import os
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "dist" / "EmuHive"
OUT = ROOT / "dist" / "EmuHive.zip"


def main():
    if not SRC.is_dir():
        print(f"✗ 没有 {SRC}（先跑 PyInstaller）")
        return 1
    n = 0
    with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED) as z:
        for dirpath, _, files in os.walk(SRC):
            for f in files:
                p = Path(dirpath) / f
                z.write(p, p.relative_to(SRC.parent).as_posix())
                n += 1
    size = OUT.stat().st_size
    print(f"ZIP_OK {n} 个文件 {size} 字节 = {size/1048576:.1f} MiB（Windows 显示口径）")
    print(f"路径：{OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
