"""
ui/image_util.py — numpy 画面 ↔ Qt 图像的唯一换算出口

从 ui/template_tool.py 迁入（原来两份副本散在 template_tool 与 main.py，
flow_editor 里还有一份不归本模块管）。规则：

- numpy_to_qimage(img)：numpy（OpenCV BGR/BGRA）→ QImage，立即 .copy() 防悬空指针。
- numpy_to_qpixmap(img, max_size)：→ 等比缩放 QPixmap 缩略图（模板库显示用）。

BGRA（抠图模板）→ RGBA8888 保留透明；其余按 BGR 3 通道处理（老行为）。
"""

import cv2
from PySide6.QtCore import Qt
from PySide6.QtGui import QImage, QPixmap


def numpy_to_qimage(img) -> QImage:
    """numpy 数组 → QImage（Qt 显示用）。立即 .copy() 防悬空指针。
    BGRA（抠图模板）→ RGBA8888 保留透明；其余按 BGR 3 通道处理（老行为）。"""
    h, w = img.shape[:2]
    if img.ndim == 3 and img.shape[2] == 4:
        rgba = cv2.cvtColor(img, cv2.COLOR_BGRA2RGBA)
        return QImage(rgba.data, w, h, 4 * w, QImage.Format_RGBA8888).copy()
    rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
    return QImage(rgb.data, w, h, 3 * w, QImage.Format_RGB888).copy()


def numpy_to_qpixmap(img, max_size=80) -> QPixmap:
    """numpy 数组 → QPixmap 缩略图（用于模板库显示）"""
    qimg = numpy_to_qimage(img)
    pixmap = QPixmap.fromImage(qimg)
    # 等比缩放到 max_size
    if pixmap.width() > max_size or pixmap.height() > max_size:
        pixmap = pixmap.scaled(max_size, max_size, Qt.KeepAspectRatio, Qt.SmoothTransformation)
    return pixmap
