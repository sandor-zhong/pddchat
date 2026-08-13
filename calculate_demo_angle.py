"""计算本地旋转拼图 HTML 中圆形图片的对齐角度。

适用范围：配套的 pdd_rotate_captcha_demo.html 本地演示文件。
脚本只做离线图片分析，不访问网站、不拖动真实验证码，也不提交验证请求。

安装依赖：
    uv add pillow numpy

运行：
    uv run python calculate_demo_angle.py

生成对齐预览图：
    uv run python calculate_demo_angle.py --preview calculated_alignment_preview.png
"""

from __future__ import annotations

import argparse
import base64
from dataclasses import dataclass
from html.parser import HTMLParser
from io import BytesIO
from pathlib import Path

import numpy as np
from PIL import Image


DEFAULT_HTML = Path(__file__).with_name("pdd_rotate_captcha_demo.html")


@dataclass(frozen=True)
class AngleResult:
    """一次角度搜索的结果。"""

    best_angle: float
    best_score: float
    candidates: list[tuple[float, float]]


class CaptchaImageParser(HTMLParser):
    """从 HTML 的 img 标签中取得两张 Data URI 图片。"""

    def __init__(self) -> None:
        super().__init__()
        self.background_src: str | None = None
        self.circle_src: str | None = None

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        if tag != "img":
            return

        values = dict(attrs)
        class_names = set((values.get("class") or "").split())
        src = values.get("src") or ""

        if "rotateslider-img-bg" in class_names:
            self.background_src = src
        elif "rotateslider-circle" in class_names:
            self.circle_src = src


def decode_data_uri(src: str) -> Image.Image:
    """把 data:image/...;base64 URI 解码为 RGBA 图片。"""

    prefix, encoded = src.split(",", maxsplit=1)
    if ";base64" not in prefix:
        raise ValueError("图片不是 Base64 Data URI")
    return Image.open(BytesIO(base64.b64decode(encoded))).convert("RGBA")


def extract_images(html_path: Path) -> tuple[Image.Image, Image.Image]:
    """从演示 HTML 中提取背景图和圆形图。"""

    parser = CaptchaImageParser()
    parser.feed(html_path.read_text(encoding="utf-8"))

    if not parser.background_src or not parser.circle_src:
        raise ValueError("HTML 中没有找到背景图或圆形图")

    return (
        decode_data_uri(parser.background_src),
        decode_data_uri(parser.circle_src),
    )


def bilinear_sample(
    image: np.ndarray,
    x: np.ndarray,
    y: np.ndarray,
) -> np.ndarray:
    """在浮点坐标上进行双线性像素采样。"""

    height, width = image.shape[:2]
    x = np.clip(x, 0, width - 1)
    y = np.clip(y, 0, height - 1)

    x0 = np.floor(x).astype(np.int32)
    y0 = np.floor(y).astype(np.int32)
    x1 = np.minimum(x0 + 1, width - 1)
    y1 = np.minimum(y0 + 1, height - 1)

    weight_x = (x - x0)[:, None]
    weight_y = (y - y0)[:, None]

    top = image[y0, x0] * (1 - weight_x) + image[y0, x1] * weight_x
    bottom = image[y1, x0] * (1 - weight_x) + image[y1, x1] * weight_x
    return top * (1 - weight_y) + bottom * weight_y


def _sample_ring(
    image: np.ndarray,
    center_x: float,
    center_y: float,
    radius: float,
    theta: np.ndarray,
) -> np.ndarray:
    """在 image 上以 (center_x, center_y) 为圆心、radius 为半径采样一圈像素。"""
    return bilinear_sample(
        image,
        center_x + radius * np.cos(theta),
        center_y + radius * np.sin(theta),
    )


def _detect_cutout_radius(bg: np.ndarray, bg_cx: float, bg_cy: float) -> float:
    """从背景图径向亮度剖面自动检测切洞边缘半径。

    切洞内部通常很暗，外部是正常场景亮度，最大梯度处即为切洞边缘。
    """
    gray = np.mean(bg, axis=2)  # 转灰度
    h, w = gray.shape
    max_r = min(bg_cx, bg_cy, w - bg_cx, h - bg_cy) - 2

    n_angles = 720
    theta = np.linspace(0, 2 * np.pi, n_angles, endpoint=False)
    radii = np.arange(max_r * 0.5, max_r, 0.25)

    profile: list[float] = []
    for r in radii:
        xs = bg_cx + r * np.cos(theta)
        ys = bg_cy + r * np.sin(theta)
        samples = bilinear_sample(gray[:, :, None], xs, ys)
        profile.append(float(np.mean(samples)))

    grad = np.abs(np.diff(profile))
    edge_idx = int(np.argmax(grad))
    return float(radii[edge_idx])


def _ncc_score(fg_ring: np.ndarray, bg_ring: np.ndarray) -> float:
    """归一化互相关 (Normalized Cross-Correlation)。

    对亮度偏移和对比度缩放不变，比原始 RGB 差值更可靠。
    返回值域 [0, 1]，1 表示完美匹配。
    """
    a = fg_ring.flatten().astype(np.float64)
    b = bg_ring.flatten().astype(np.float64)

    a_c = a - a.mean()
    b_c = b - b.mean()

    norm_a = np.linalg.norm(a_c)
    norm_b = np.linalg.norm(b_c)

    if norm_a < 1e-10 or norm_b < 1e-10:
        return 0.0

    return float(np.dot(a_c, b_c) / (norm_a * norm_b))


def _ring_shift(ring: np.ndarray, shift: float) -> np.ndarray:
    """对环采样数组沿环方向做亚样本循环平移（线性插值）。

    shift 单位是采样点个数，可为小数。
    """
    n = ring.shape[0]
    idx = np.arange(n, dtype=np.float64) - shift
    idx0 = np.floor(idx).astype(np.int64) % n
    idx1 = (idx0 + 1) % n
    w = (idx - np.floor(idx))[:, None]
    return ring[idx0] * (1 - w) + ring[idx1] * w


def _angle_score_ncc_shifted(
    bg_rings_norm: list[np.ndarray],
    fg_rings: list[np.ndarray],
    shift: float,
) -> float:
    """用环形移位 + NCC 计算角度得分（越大越好）。

    fg_rings 是未旋转的圆形图环采样，shift 是平移量（采样点单位）。
    """
    total_ncc = 0.0
    for i, fg_ring in enumerate(fg_rings):
        shifted = _ring_shift(fg_ring, shift)
        # 归一化后与 bg 做点积
        a = shifted.flatten()
        a_c = a - a.mean()
        norm_a = np.linalg.norm(a_c)
        if norm_a < 1e-10:
            continue
        total_ncc += float(np.dot(a_c, bg_rings_norm[i]) / norm_a)
    return total_ncc / len(fg_rings)


def _parabolic_minimum(
    angles: np.ndarray,
    scores: np.ndarray,
) -> tuple[float, float]:
    """在 3 个等距点 (angles, scores) 上拟合抛物线，求最小值位置。

    scores 越小越好。返回 (插值角度, 插值得分)。
    """
    if len(angles) != 3:
        idx = int(np.argmin(scores))
        return float(angles[idx]), float(scores[idx])

    h = angles[1] - angles[0]
    denom = scores[0] - 2.0 * scores[1] + scores[2]

    if abs(denom) < 1e-12:
        return float(angles[1]), float(scores[1])

    offset = 0.5 * (scores[0] - scores[2]) / denom
    offset = max(-1.0, min(1.0, offset))  # 限制在 ±h 范围内

    return float(angles[1] + offset * h), float(scores[1])


def calculate_angle(
    background: Image.Image,
    circle: Image.Image,
    *,
    sample_count: int = 1440,
    coarse_step: float = 1.0,
    fine_step: float = 0.02,
    fine_range: float = 5.0,
    fine_candidates: int = 10,
) -> AngleResult:
    """三阶段 NCC 搜索最佳 CSS 顺时针旋转角度，亚度数精度。

    第一阶段·粗搜：步长 1°，360° 全扫描，NCC 定位候选区域。
    第二阶段·精搜：在粗搜最优 N 个候选 ±5° 内以 0.05° 步长细化。
    第三阶段·抛物线插值：对精搜最优拟合抛物线，求亚度数最小值。

    多圈采样：沿 cutout 边界内外 9 对半径配对采样（bg_r + fg_r ≈ diameter）。
    NCC 对亮度/对比度差异不敏感，是结构匹配的标准方法。
    """

    bg = np.asarray(background.convert("RGB"), dtype=np.float64)
    fg = np.asarray(circle.convert("RGB"), dtype=np.float64)

    bg_h, bg_w = bg.shape[:2]
    fg_h, fg_w = fg.shape[:2]

    bg_cx, bg_cy = bg_w / 2, bg_h / 2
    fg_cx, fg_cy = fg_w / 2, fg_h / 2

    diameter = min(fg_w, fg_h)
    fg_radius = diameter / 2

    # 自动检测背景图中切洞的实际边缘半径（通常略小于 fg_radius）。
    cutout_radius = _detect_cutout_radius(bg, bg_cx, bg_cy)

    # 9 对半径配对：以检测到的切洞边缘为基准向两侧采样。
    offsets = np.array([0.5, 1.0, 1.5, 2.0, 3.0, 4.0, 5.0, 6.5, 8.0])
    bg_radii = cutout_radius + offsets
    fg_radii = fg_radius - offsets

    theta = np.linspace(0, 2 * np.pi, sample_count, endpoint=False)

    # 预计算背景各环，并归一化为单位向量（NCC 分母中的 bg 部分）。
    bg_rings_norm: list[np.ndarray] = []
    for radius in bg_radii:
        ring = _sample_ring(bg, bg_cx, bg_cy, radius, theta)
        flat = ring.flatten()
        centered = flat - flat.mean()
        norm = np.linalg.norm(centered)
        bg_rings_norm.append(centered / norm if norm > 1e-10 else centered)

    # 预计算圆形图各环（未旋转），旋转 = 环形移位。
    fg_rings: list[np.ndarray] = []
    for radius in fg_radii:
        fg_rings.append(_sample_ring(fg, fg_cx, fg_cy, radius, theta))

    samples_per_degree = sample_count / 360.0

    def score_at(angle_deg: float) -> float:
        return _angle_score_ncc_shifted(
            bg_rings_norm, fg_rings, angle_deg * samples_per_degree,
        )

    # ---- 第一阶段：粗搜（NCC，1° 步长，向量化移位）----
    coarse_angles_deg = np.arange(0, 360, coarse_step)
    coarse_scores = np.array([score_at(a) for a in coarse_angles_deg])
    coarse_top = np.argsort(coarse_scores)[::-1]  # NCC 越大越好

    # ---- 第二阶段：精搜（NCC，0.02° 步长）----
    final_angle: float = float(coarse_angles_deg[coarse_top[0]])
    final_score: float = float(coarse_scores[coarse_top[0]])

    for idx in coarse_top[:fine_candidates]:
        center_deg = float(coarse_angles_deg[idx])
        fine_angles_deg = np.arange(
            center_deg - fine_range,
            center_deg + fine_range + fine_step * 0.5,
            fine_step,
        )
        for a_deg in fine_angles_deg:
            s = score_at(float(a_deg))
            if s > final_score:
                final_score = s
                final_angle = float(a_deg)

    # ---- 第三阶段：抛物线插值 ----
    test_angles = np.array([
        final_angle - fine_step,
        final_angle,
        final_angle + fine_step,
    ])
    test_scores = np.array([score_at(a) for a in test_angles])
    interp_angle, _ = _parabolic_minimum(test_angles, -test_scores)
    if not np.isnan(interp_angle) and 0 <= interp_angle < 360:
        final_angle = interp_angle

    final_angle = final_angle % 360

    # 收集候选（用于展示）：对粗搜角度按 NCC 降序排列后取 top 5。
    candidates: list[tuple[float, float]] = [
        (float(coarse_angles_deg[i]), float(coarse_scores[i]))
        for i in coarse_top[:5]
    ]

    # best_score 转为误差语义（越小越好），便于外部统一理解。
    return AngleResult(
        best_angle=final_angle,
        best_score=1.0 - final_score,
        candidates=[(a, 1.0 - s) for a, s in candidates],
    )


def save_preview(
    background: Image.Image,
    circle: Image.Image,
    css_clockwise_angle: float,
    output_path: Path,
) -> None:
    """生成圆形图已经覆盖到背景中央的对齐预览。"""

    result = background.convert("RGBA").copy()

    # Pillow 的正角度是逆时针，方向与 CSS 相反。
    pillow_counterclockwise_angle = (360 - css_clockwise_angle) % 360
    rotated = circle.convert("RGBA").rotate(
        float(pillow_counterclockwise_angle),
        resample=Image.Resampling.BICUBIC,
        expand=False,
    )

    left = round((result.width - rotated.width) / 2)
    top = round((result.height - rotated.height) / 2)
    result.alpha_composite(rotated, (left, top))

    output_path.parent.mkdir(parents=True, exist_ok=True)
    result.save(output_path)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="计算本地旋转拼图演示 HTML 的圆形图片角度",
    )
    parser.add_argument(
        "html",
        nargs="?",
        type=Path,
        default=DEFAULT_HTML,
        help=f"演示 HTML 路径，默认：{DEFAULT_HTML.name}",
    )
    parser.add_argument(
        "--preview",
        type=Path,
        help="可选：保存对齐后的预览图片",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    html_path = args.html.resolve()

    if not html_path.exists():
        raise SystemExit(f"文件不存在：{html_path}")

    background, circle = extract_images(html_path)
    result = calculate_angle(background, circle)

    print(f"HTML：{html_path}")
    print(f"背景图：{background.width} × {background.height}")
    print(f"圆形图：{circle.width} × {circle.height}")
    print(f"最佳 CSS 顺时针角度：{result.best_angle:.2f}°")
    print(f"等价 Pillow 逆时针角度：{(360 - result.best_angle) % 360:.2f}°")
    print(f"最低圆周梯度误差：{result.best_score:.4f}")
    print("误差最低的 5 个候选角度：")
    for angle, score in result.candidates:
        print(f"  {angle:7.2f}°  score={score:.4f}")
        
    if args.preview:
        output_path = args.preview.resolve()
        save_preview(background, circle, result.best_angle, output_path)
        print(f"对齐预览：{output_path}")


if __name__ == "__main__":
    main()
