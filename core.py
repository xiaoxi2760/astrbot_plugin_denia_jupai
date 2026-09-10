# -*- coding: utf-8 -*-
"""举牌合成核心(合并版方案)-- 架构取 yangyangsay,排版取 贴字管线 v5。

架构(高效合成,单张 ~0.3-0.4s):
  人物层 assets/base/{key}.gif + 牌子层 assets/sign/{key}.gif 分离;牌面白面
  四边形逐帧标定在 assets/calibration.json(坐标在牌子层画布上)。
  文字只渲染一次,逐帧只做「按标定中心平移 + 按底边角度旋转」贴上,
  没有任何逐帧检测。

排版(贴字管线 v5,已验收观感):
  - 字体 阿里妈妈方圆体;锚定墨高随字数变化:1~2 字放满 INK_MAX px、≥8 字回到
    原版墨高 INK_TARGET px(平滑过渡);超长自动缩小,下限 MIN_SIZE,
    仍放不下抛 TextTooLong,不出烂图
  - 换行按像素宽度 + 行首标点禁则(NO_HEAD 回收上一行行尾);行高 LINE_H
  - 文字安全区 = 牌面四边形内缩 PAD;墨迹居中修正(全角标点 em 空格)
  - emoji:与秧秧原程序同思路,用系统 emoji 字体渲染彩色字形(不随插件打包)--
    Windows=Segoe UI Emoji,Linux=fontconfig 查到的 Noto Color Emoji 等;
    emoji 段落按「位图贴片」参与换行测量与整块居中;系统没有 emoji 字体时
    退回普通字形(不报错)
  - 输出 GIF:保留牌子真实逐帧时长(30/40ms 交替);全片共享调色板
    (247 数据色 + 8 级 白->字色 AA 渐变槽 + 透明索引 255),无逐帧闪色

模板(情绪 key = 素材/标定键):
  hongwen=红温  kaixin=开心  beishang=悲伤  qidai=期待  kuku=哭哭

对外 API:
  render(key, text, color=None, image=None) -> bytes (image/gif, 300x300 循环)
    image 非 None(PIL/bytes/本地路径)时把图片铺满牌面白面:
    有文字则图上叠字,无文字则纯图上牌
  load_image(src) -> PIL.Image      接受 PIL / bytes / 本地路径,动图取第一帧
  parse_color(color) -> (r, g, b)   接受 预设名 / "r,g,b" / "#rrggbb" / (r,g,b)
  split_color_tail(text) -> (正文, 颜色或None)   解析尾部 "#颜色" / "-c 颜色"
  TextTooLong / JupaiError
"""
from __future__ import annotations

import io
import json
import math
import os
import re
import subprocess
from collections import OrderedDict
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent
FONT_PATH = ROOT / "assets" / "font.ttf"
CALIB_PATH = ROOT / "assets" / "calibration.json"
XIXI_CALIB_PATH = ROOT / "assets" / "xixi" / "calibration.json"
XIXI_OPTIONAL_FONT = ROOT / "assets" / "xixi_font.ttf"

# xixi-rs 移植的模板 key(资产与标定见 assets/xixi/)
# 举牌 1/2 已由 sigrika_p1~6(assets/templates/)取代并下线, 仅保留摸/展示(goldpig)
XIXI_HOLDSIGN_KEYS: set = set()
XIXI_GOLDPIG_KEYS = {"xixi_goldpig", "xixi_goldpig_2"}

# 合并帧/遮罩/xixi 帧 LRU 缓存上限:每模板 30 帧 RGBA 约 11MB,6 个约 65MB 封顶
MAX_CACHED_TEMPLATES = 6


# ---------------- 模板规格(assets/templates/{key}/meta.json 自动注册) ----------------
def _scan_template_specs() -> dict[str, dict]:
    """扫描 assets/templates/ 下每个含 meta.json 的目录,返回 {key: spec}。

    新增一个模板 = 新建目录 assets/templates/{key}/(frame.gif + calibration.json + meta.json),
    无需改代码,core 自动注册到 TEMPLATES;main.py 在 ROLES(或 assets/roles.json)引用该 key 即可。
    meta.json 支持字段:
      name             模板显示名(注册进 TEMPLATES)
      mode             fullframe(整帧 GIF + 逐帧标定贴字)| goldpig(圆形窗口贴图)
      file             帧序列文件名,默认 frame.gif
      calibration      标定文件名,默认 calibration.json(fullframe: frames[];goldpig: radius/centers)
      font             字体文件名(assets/ 下,如 sigrika_font.ttf);缺省用插件默认 font.ttf
      min_font_size / max_font_size   字号搜索范围(fullframe)
      text_box_scale   [w, h] 文字框 = 标定矩形 × 缩放(fullframe)
      default_text     模板自带默认文字(空文字时用)
      default_color    默认字色(供 main.py 读取)
      image            True = 支持引用图片铺牌面(layered 专用字段,fullframe 预留)
    """
    tdir = ROOT / "assets" / "templates"
    specs: dict[str, dict] = {}
    if not tdir.is_dir():
        return specs
    for d in sorted(tdir.iterdir()):
        mf = d / "meta.json"
        if not (d.is_dir() and mf.is_file()):
            continue
        try:
            spec = json.loads(mf.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(spec, dict) and spec.get("name"):
            specs[d.name] = spec
    return specs

LINE_H = 1.18
PAD = 12            # 文字安全区 = 牌面四边形内缩
SS = 8              # 文字渲染超采样(8x 渲染 -> LANCZOS 精缩 -> 逐帧纯旋转)
MIN_SIZE = 12       # 可读下限,仍放不下则 TextTooLong
INK_TARGET = 26     # 原程序烘焙字实测墨高(px),长文本锚定默认字号
INK_MAX = 40        # 字数很少时允许放大的墨高上限(1~2 字放满,平滑回落)
NO_HEAD = ",。!?;:、)】》...!?,.:;"
DEFAULT_RGB = (246, 196, 196)   # 默认字色 = 预设「粉」
RAMP_STEPS = 8      # 白->字色 预留 AA 渐变槽位数
EMOJI_H = 1.0       # emoji 位图墨高 = 字号 × EMOJI_H

NAMED_RGB = {
    "黑": (50, 50, 50), "粉": (246, 196, 196), "红": (231, 76, 60),
    "橙": (230, 126, 34), "黄": (241, 196, 15), "绿": (46, 204, 113),
    "深绿": (22, 86, 54), "青": (26, 188, 156), "蓝": (52, 152, 219),
    "深蓝": (40, 116, 166), "紫": (155, 89, 182), "金": (255, 215, 0),
}

# 情绪 key:main.py 的指令与配置都用这份表
TEMPLATES = {
    "blink": "娅娅眨眼",
    "hongwen": "娅娅红温",
    "kaixin": "娅娅开心",
    "beishang": "娅娅悲伤",
    "qidai": "娅娅期待",
    "kuku": "娅娅哭哭",
    "am_blink": "小爱眨眼",
    "am_hongwen": "小爱红温",
    "am_kaixin": "小爱开心",
    "am_beishang": "小爱悲伤",
    "am_qidai": "小爱期待",
    "am_kuku": "小爱哭哭",
    "xixi_goldpig": "西西摸",
    "xixi_goldpig_2": "西西展示",
}

# 自动注册的模板规格(assets/templates/,见 _scan_template_specs)
_template_specs: dict[str, dict] = _scan_template_specs()
for _k, _spec in _template_specs.items():
    TEMPLATES[_k] = _spec["name"]


_cache: dict = {}

# ---------------- 进程内缓存(懒加载;LRU 控制内存) ----------------
_calib_data: dict | None = None            # assets/calibration.json 解析缓存
_xixi_calib_data: dict | None = None       # assets/xixi/calibration.json 解析缓存
_ink_ratio_value: float | None = None      # _ink_ratio() 结果缓存
_font_cache: dict[tuple[str, int], ImageFont.FreeTypeFont] = {}
_merged_cache: OrderedDict[str, tuple] = OrderedDict()      # key -> (merged_frames, durs, rects)
_mask_cache: OrderedDict[str, list] = OrderedDict()         # key -> [面板遮罩 L 图 × 帧数]
_xixi_frame_cache: OrderedDict[str, tuple] = OrderedDict()  # xixi key -> (frames, durs)
_template_frame_cache: OrderedDict[str, tuple] = OrderedDict()  # spec key -> (frames, durs)
_template_srcpal_cache: dict[str, bytes] = {}               # spec key -> 源 GIF 实用色 246 条
_blank_palette_cache: dict[str, bytes] = {}                 # key -> 246 色空白数据调色板


def _get_font(size: int, path: Path = FONT_PATH) -> ImageFont.FreeTypeFont:
    """按 (字体路径, 字号) 缓存 truetype 字体,避免重复加载/解析。"""
    key = (str(path), size)
    if key not in _font_cache:
        _font_cache[key] = ImageFont.truetype(str(path), size)
    return _font_cache[key]


class JupaiError(Exception):
    """举牌合成失败(用户可读的原因在 message 里)。"""


class TextTooLong(JupaiError):
    """文字在最小字号下仍放不进牌面安全区。"""


def parse_color(color) -> tuple[int, int, int]:
    """预设名 / "r,g,b" / "#rrggbb" / (r,g,b) / None -> RGB 元组。非法抛 ValueError。"""
    if color is None:
        return DEFAULT_RGB
    if isinstance(color, (tuple, list)):
        try:
            r, g, b = (int(v) for v in color)
        except (ValueError, TypeError):
            raise ValueError(f"颜色参数不认识:{color}") from None
        if not all(0 <= v <= 255 for v in (r, g, b)):
            raise ValueError(f"颜色越界:{color}")
        return (r, g, b)
    s = str(color).strip().lstrip("#")
    if s in NAMED_RGB:
        return NAMED_RGB[s]
    parts = s.replace(",", ",").split(",")
    try:
        if len(parts) == 3:
            r, g, b = (int(v) for v in parts)
        else:
            r, g, b = int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16)
    except ValueError:
        raise ValueError(f"颜色参数不认识:{color}(可用预设 {'、'.join(NAMED_RGB)} 或 r,g,b)") from None
    if not all(0 <= v <= 255 for v in (r, g, b)):
        raise ValueError(f"颜色越界:{color}")
    return (r, g, b)


# ---------------- 文本尾部颜色标记 ----------------
_TAIL_HASH = re.compile(r"[##]([^\s##]+)\s*$")          # 结尾 "#粉" / "#e74c3c"
_TAIL_C = re.compile(r"-\s*[cCc]\s*(\S+)\s*$")            # 旧写法 "-c 粉"
_HEX_RE = re.compile(r"[0-9a-fA-F]{6}")
_DASH_NORM = str.maketrans({"-": "-", "-": "-", "-": "-", "-": "-"})


def split_color_tail(text: str) -> tuple[str, str | None]:
    """从文本尾部解析颜色标记,返回 (剥色正文, 颜色或 None)。

    两种写法(都不挑输入法):
      文字#颜色     主写法:预设名或 6 位 hex;只有匹配成功才剥掉,
                    否则 # 当普通文字原样保留(想输出字面 # 就写 ##)
      文字-c 颜色   旧写法:兼容全角横线(-/-/-/-)、大小写、横线后带不带空格
    """
    s = (text or "").rstrip()
    m = _TAIL_HASH.search(s)
    if m:
        token = m.group(1)
        head = s[: m.start()]
        if head.endswith(("#", "#")):                  # ##转义:还原一个字面 #
            return head + token, None
        if token in NAMED_RGB or _HEX_RE.fullmatch(token):
            return head.rstrip(), token
    n = s.translate(_DASH_NORM)
    m = _TAIL_C.search(n)
    if m:
        return s[: m.start()].rstrip(), m.group(1)
    return text, None


# ---------------- emoji(系统字体,无则退化普通字形) ----------------
# 文本呈现符号:单独出现时当普通文本,带 VS16/ZWJ 才走 emoji 字体
_EMOJI_LO = (
    "".join(chr(c) for c in range(0x2600, 0x27C0))     # 杂项符号 + 装饰符号
    + "".join(chr(c) for c in range(0x2B00, 0x2C00))   # 星星/箭头/圆圈
    + "\u00A9\u00AE\u203C\u2049\u2122\u2139\u231A\u231B\u2328\u23CF"
    + "".join(chr(c) for c in range(0x23E9, 0x23FB))
    + "\u24C2\u25AA\u25AB\u25B6\u25C0\u2934\u2935\u3030\u303D\u3297\u3299"
    + "".join(chr(c) for c in range(0x2190, 0x21B0))   # 箭头
)
# 不带 VS16 也默认 emoji 呈现的常用字(聊天高频)
_EMOJI_PRES = frozenset(
    "\u2B50\u2B55\u26A1\u26BD\u26BE\u26C4\u26C5\u26D4\u26F3\u26F5\u26FD"
    "\u2705\u2708\u270A\u270B\u270C\u270D\u270F\u2712\u2714\u2716\u2728"
    "\u2733\u2734\u2744\u2747\u274C\u274E\u2753\u2754\u2755\u2757\u2763\u2764"
    "\u2795\u2796\u2797\u27A1\u27B0\u27BF"
)
_SKIN = "\U0001F3FB\U0001F3FC\U0001F3FD\U0001F3FE\U0001F3FF"


def _is_emoji_base(ch: str) -> bool:
    return ("\U0001F000" <= ch <= "\U0001FAFF") or (ch in _EMOJI_LO)


def _tokenize(text: str) -> list[tuple[str, str]]:
    """切成 ("t", 文本段) / ("e", emoji簇) 两种 token(ZWJ 序列/肤色/VS16 归入簇)。"""
    tokens: list[tuple[str, str]] = []
    buf = ""
    i, n = 0, len(text)

    def flush():
        nonlocal buf
        if buf:
            # 普通文字按单字成 token 参与换行测量;绘制端会把相邻 t 合并回整段
            tokens.extend(("t", ch) for ch in buf)
            buf = ""

    while i < n:
        ch = text[i]
        if ch in "#*0123456789" and i + 1 < n and text[i + 1] == "\u20E3":
            j = i + 1
            if j + 1 < n and text[j + 1] == "\uFE0F":
                j += 1
            flush()
            tokens.append(("e", text[i:j + 1]))
            i = j + 1
            continue
        if _is_emoji_base(ch):
            cluster = ch
            i += 1
            if i < n and (text[i] == "\uFE0F" or text[i] in _SKIN):
                cluster += text[i]
                i += 1
            while i + 1 < n and text[i] == "\u200D" and _is_emoji_base(text[i + 1]):
                cluster += text[i] + text[i + 1]
                i += 2
                if i < n and (text[i] == "\uFE0F" or text[i] in _SKIN):
                    cluster += text[i]
                    i += 1
            if ("\U0001F000" <= ch <= "\U0001FAFF") or ch in _EMOJI_PRES \
                    or "\uFE0F" in cluster or "\u200D" in cluster:
                flush()
                tokens.append(("e", cluster))
            else:                       # 普通文本符号(☆ © ← 等)
                buf += cluster
            continue
        buf += ch
        i += 1
    flush()
    return tokens


_EMOJI_FONT: str | None = None
_EMOJI_FONT_LOOKED = False
_emoji_ref_cache: dict[str, np.ndarray | None] = {}
_emoji_bm_cache: dict[tuple[str, int], Image.Image | None] = {}


def _find_system_emoji_font() -> str | None:
    """找系统 emoji 字体:Windows=seguiemj;Linux/macOS 先问 fontconfig 再试常见路径。"""
    cands: list[Path] = []
    if os.name == "nt":
        cands.append(Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts" / "seguiemj.ttf")
    else:
        try:
            out = subprocess.run(["fc-list"], capture_output=True, text=True, timeout=5).stdout
            for line in out.splitlines():
                p = line.split(":", 1)[0].strip()
                if "emoji" in line.lower() and p.lower().endswith((".ttf", ".ttc", ".otf")):
                    cands.append(Path(p))
        except Exception:
            pass
        cands += [
            Path("/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf"),
            Path("/usr/share/fonts/truetype/noto/NotoColorEmojiCBDT.ttf"),
            Path("/usr/share/fonts/noto-color-emoji/NotoColorEmoji.ttf"),
            Path("/System/Library/Fonts/Apple Color Emoji.ttc"),
        ]
    for p in cands:
        try:
            if p.is_file():
                return str(p)
        except OSError:
            continue
    return None


def _emoji_font_path() -> str | None:
    global _EMOJI_FONT, _EMOJI_FONT_LOOKED
    if not _EMOJI_FONT_LOOKED:
        _EMOJI_FONT = _find_system_emoji_font()
        _EMOJI_FONT_LOOKED = True
    return _EMOJI_FONT


def _emoji_ref(cluster: str) -> np.ndarray | None:
    """emoji 簇的参考位图(RGBA np,按墨迹裁边);系统缺字形返回 None。"""
    if cluster in _emoji_ref_cache:
        return _emoji_ref_cache[cluster]
    ref = None
    path = _emoji_font_path()
    if path:
        try:
            try:
                f = ImageFont.truetype(path, 256)
            except OSError:                     # Noto CBDT 位图字体只接受 109
                f = ImageFont.truetype(path, 109)
            tile = Image.new("RGBA", (f.size * 6, f.size * 3), (0, 0, 0, 0))
            ImageDraw.Draw(tile).text(
                (f.size // 2, f.size // 2), cluster, font=f, embedded_color=True)
            bbox = tile.getbbox()
            if bbox and bbox[2] - bbox[0] >= 2 and bbox[3] - bbox[1] >= 2:
                ref = np.asarray(tile.crop(bbox))
        except Exception:
            ref = None
    _emoji_ref_cache[cluster] = ref
    return ref


def _emoji_bitmap(cluster: str, px: int) -> Image.Image | None:
    """目标墨高 px 的 RGBA 位图(LANCZOS 缩放,带缓存)。"""
    key = (cluster, px)
    if key in _emoji_bm_cache:
        return _emoji_bm_cache[key]
    ref = _emoji_ref(cluster)
    if ref is None:
        _emoji_bm_cache[key] = None
        return None
    h0, w0 = ref.shape[:2]
    im = Image.fromarray(ref).resize(
        (max(1, round(w0 * px / h0)), px), Image.Resampling.LANCZOS)
    _emoji_bm_cache[key] = im
    return im


def _tok_width(tok: tuple[str, str], font, probe) -> float:
    """token 在最终字号下的宽度(换行测量用)。"""
    if tok[0] == "t":
        return probe.textlength(tok[1], font=font)
    ref = _emoji_ref(tok[1])
    if ref is None:                             # 无 emoji 字体:按普通字形测
        return probe.textlength(tok[1], font=font)
    h0, w0 = ref.shape[:2]
    return font.size * EMOJI_H * (w0 / h0)


def _ink_ratio() -> float:
    """方圆体 CJK 墨高/em(实测,用于墨高锚定字号);结果缓存。"""
    global _ink_ratio_value
    if _ink_ratio_value is None:
        probe = Image.new("L", (300, 300), 0)
        ImageDraw.Draw(probe).text(
            (150, 150), "我", font=_get_font(200, FONT_PATH), fill=255, anchor="mm")
        ys, _ = np.where(np.asarray(probe) > 0)
        _ink_ratio_value = (ys.max() - ys.min() + 1) / 200.0
    return _ink_ratio_value


def _calib() -> dict:
    """assets/calibration.json 解析缓存(进程内只读一次)。"""
    global _calib_data
    if _calib_data is None:
        _calib_data = json.loads(CALIB_PATH.read_text(encoding="utf-8"))
    return _calib_data


def _xixi_calib() -> dict:
    """assets/xixi/calibration.json 解析缓存(进程内只读一次)。"""
    global _xixi_calib_data
    if _xixi_calib_data is None:
        _xixi_calib_data = json.loads(XIXI_CALIB_PATH.read_text(encoding="utf-8"))
    return _xixi_calib_data


def _assets(key: str):
    """返回 (合并空白帧, 逐帧时长ms, 逐帧标定)。

    合并空白帧 = base 人物层 + sign 牌子层 合成一次后缓存(LRU)。
    渲染从合并帧出发,每帧少 1 次 copy + 1 次 alpha_composite,内存也只存一份。
    """
    if key in _merged_cache:
        _merged_cache.move_to_end(key)
        return _merged_cache[key]
    if key not in TEMPLATES:
        raise JupaiError(f"未知模板:{key}")
    base_frames, _ = _load(ROOT / "assets" / "base" / f"{key}.gif")
    sign_frames, durs = _load(ROOT / "assets" / "sign" / f"{key}.gif")
    rects = _calib()[key]
    n = min(len(base_frames), len(sign_frames), len(rects))
    if n == 0:
        raise JupaiError(f"模板 {key} 素材为空")
    merged = []
    for i in range(n):
        canvas = base_frames[i].copy()
        canvas.alpha_composite(sign_frames[i])
        merged.append(canvas)
    entry = (merged, durs[:n], rects[:n])
    _merged_cache[key] = entry
    while len(_merged_cache) > MAX_CACHED_TEMPLATES:
        _merged_cache.popitem(last=False)
    return entry


def _sign_masks(key: str) -> list:
    """面板遮罩缓存(塞图模式用):每模板只算一次,LRU 控制内存。"""
    if key in _mask_cache:
        _mask_cache.move_to_end(key)
        return _mask_cache[key]
    sign_frames, _ = _load(ROOT / "assets" / "sign" / f"{key}.gif")
    rects = _calib()[key]
    n = min(len(sign_frames), len(rects))
    masks = [_panel_mask(sign_frames[i], rects[i]) for i in range(n)]
    _mask_cache[key] = masks
    while len(_mask_cache) > MAX_CACHED_TEMPLATES:
        _mask_cache.popitem(last=False)
    return masks


def _xixi_font_path() -> Path:
    """xixi 举牌字体:优先用户自放的 assets/xixi_font.ttf(原版荆南麦圆体),
    否则复用插件自带阿里妈妈方圆体(默认,不额外打包 8MB 字体)。"""
    return XIXI_OPTIONAL_FONT if XIXI_OPTIONAL_FONT.is_file() else FONT_PATH


def _xixi_frames(key: str):
    """xixi 模板帧(assets/xixi/*.gif),LRU 缓存。"""
    if key in _xixi_frame_cache:
        _xixi_frame_cache.move_to_end(key)
        return _xixi_frame_cache[key]
    frames, durs = _load(ROOT / "assets" / "xixi" / f"{key}.gif")
    _xixi_frame_cache[key] = (frames, durs)
    while len(_xixi_frame_cache) > MAX_CACHED_TEMPLATES:
        _xixi_frame_cache.popitem(last=False)
    return frames, durs


def _template_dir(key: str) -> Path:
    """规格模板目录 assets/templates/{key}/。"""
    return ROOT / "assets" / "templates" / key


def template_default_color(key: str) -> str | None:
    """模板 meta.json 声明的默认字色（如 "#000000"）；非规格模板返回 None。"""
    spec = _template_specs.get(key)
    return spec.get("default_color") if spec else None


def split_banned_tokens(cfg: str) -> list[str]:
    """把违禁词配置文本拆成词列表：支持换行/逗号（全半角）/分号/空白分隔，去重保序。"""
    words: list[str] = []
    seen: set[str] = set()
    for token in re.split(r"[\n\r,，;；\s]+", str(cfg or "")):
        token = token.strip()
        if token and token.lower() not in seen:
            seen.add(token.lower())
            words.append(token)
    return words


def banned_hit(text: str, words: list[str]) -> str | None:
    """正文是否命中违禁词（忽略大小写与词内空白）；返回命中的词，未命中 None。"""
    body = re.sub(r"\s+", "", str(text or "")).lower()
    if not body:
        return None
    for w in words:
        if re.sub(r"\s+", "", str(w)).lower() in body:
            return w
    return None


def _template_spec(key: str) -> dict:
    try:
        return _template_specs[key]
    except KeyError:
        raise JupaiError(f"未知模板:{key}") from None


def _template_calib(key: str) -> dict:
    """规格模板标定(目录内 calibration.json,进程内不缓存,文件小)。"""
    spec = _template_spec(key)
    p = _template_dir(key) / spec.get("calibration", "calibration.json")
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise JupaiError(f"模板 {key} 标定读取失败:{e}") from None


def _template_frames(key: str):
    """规格模板帧序列(目录内 frame.gif/frame.jpg),LRU 缓存。"""
    if key in _template_frame_cache:
        _template_frame_cache.move_to_end(key)
        return _template_frame_cache[key]
    spec = _template_spec(key)
    frames, durs = _load(_template_dir(key) / spec.get("file", "frame.gif"))
    _template_frame_cache[key] = (frames, durs)
    while len(_template_frame_cache) > MAX_CACHED_TEMPLATES:
        _template_frame_cache.popitem(last=False)
    return frames, durs


def _template_font_path(key: str) -> Path:
    """规格模板字体:meta.font 指定的 assets/ 下字体存在则用,否则插件默认字体。"""
    fn = _template_spec(key).get("font")
    if fn:
        p = ROOT / "assets" / fn
        if p.is_file():
            return p
    return FONT_PATH


def _template_data_palette(key: str, rgb) -> bytes:
    """规格模板数据调色板：源帧 RGBA 实用色并集（按用量取前 246）+ 当前文字色。

    直接从 RGBA 帧取色，不碰 GIF 索引表（局部/全局表语义易错）；
    源像素在调色板内的零损映射，被挤掉的只有极稀有色（肉眼不可见）。
    """
    if key not in _template_srcpal_cache:
        frames, _ = _template_frames(key)
        usage: dict[tuple[int, int, int], int] = {}
        for f in frames:
            arr = np.asarray(f)
            opaque = arr[arr[..., 3] >= 128]
            if not len(opaque):
                continue
            uniq, cnts = np.unique(opaque[:, :3], axis=0, return_counts=True)
            for rgbj, c in zip(uniq.tolist(), cnts.tolist()):
                t = (rgbj[0], rgbj[1], rgbj[2])
                usage[t] = usage.get(t, 0) + int(c)
        rgbs_all = sorted(usage, key=lambda k: -usage[k])
        rgbs = rgbs_all[:246]
        if len(rgbs_all) > 246:
            # 纯用量 Top-246 会把跨帧并集大（如 p4=1028 色）时"距离大、用量中等"
            # 的色挤掉，产生可见色偏。改为：保留集 = 纯用量 Top-240 + 按
            # "用量×近邻距离"损失排序补 6 个最伤颜色。
            all_arr = np.array(rgbs_all, dtype=np.int32)
            kept_arr = np.array(rgbs, dtype=np.int32)
            dist = np.sqrt(((all_arr[:, None, :] - kept_arr[None, :, :]) ** 2).sum(-1)).min(1)
            w = np.array([usage[tuple(c)] for c in all_arr], dtype=np.float64)
            extra = {tuple(all_arr[i]) for i in np.argsort(-(dist * w))[:6].tolist()}
            base = [c for c in rgbs_all[:240]]
            rgbs = (base + [c for c in rgbs_all if tuple(c) in extra and c not in set(base)])[:246]
        entries: list[int] = []
        for rj, gj, bj in rgbs:
            entries += [rj, gj, bj]
        entries += [0] * (246 * 3 - len(entries))
        _template_srcpal_cache[key] = bytes(entries)
    r, g, b = rgb
    return _template_srcpal_cache[key] + bytes((r, g, b))


def _load(path: Path):
    im = Image.open(path)
    frames, durs = [], []
    i = 0
    while True:
        try:
            im.seek(i)
        except EOFError:
            break
        frames.append(im.convert("RGBA"))
        durs.append(int(im.info.get("duration", 30) or 30))
        i += 1
    return frames, durs


def _wrap(text: str, font, iw: int) -> list[list[tuple[str, str]]]:
    """token 级像素宽度换行 + 行首标点禁则(标点回收上一行行尾)。"""
    probe = ImageDraw.Draw(Image.new("L", (1, 1)))
    lines: list[list[tuple[str, str]]] = []
    for para in text.split("\n"):
        cur: list[tuple[str, str]] = []
        for tok in _tokenize(para):
            w = _tok_width(tok, font, probe)
            if cur and sum(_tok_width(t, font, probe) for t in cur) + w > iw:
                lines.append(cur)
                cur = [tok]
            else:
                cur.append(tok)
        lines.append(cur)
    lines = [l for l in lines if l]
    for i in range(1, len(lines)):
        while lines[i]:
            first = lines[i][0]
            if first[0] == "e" or not first[1][:1] or first[1][0] not in NO_HEAD:
                break
            lines[i - 1].append(("t", first[1][0]))
            rest = ("t", first[1][1:])
            lines[i][0] = rest
            if not rest[1]:
                lines[i].pop(0)
    return [l for l in lines if l]


def _ink_target(text: str) -> float:
    """按字数定锚定墨高:1~2 字放满 INK_MAX,字数增多平滑回落,≥8 字回到 INK_TARGET。"""
    n = sum(1.0 if ord(ch) > 0x2E7F else 0.5 for ch in text)   # 宽字符(汉字/emoji)计 1,窄字符计 0.5
    t = min(1.0, max(0.0, (n - 2.0) / 6.0))
    return INK_MAX + (INK_TARGET - INK_MAX) * t


def _fit(text: str, iw: int, ih: int, font_path: Path = FONT_PATH):
    """从墨高锚定字号往下找能放进安全区的最大字号;返回 (font, lines, lh)。"""
    default = max(10, int(round(_ink_target(text) / _ink_ratio())))
    probe = ImageDraw.Draw(Image.new("L", (1, 1)))
    for size in range(default, MIN_SIZE - 1, -1):
        font = _get_font(size, font_path)
        lines = _wrap(text, font, iw)
        lh = int(size * LINE_H)
        if (lines and lh * len(lines) <= ih
                and max(sum(_tok_width(t, font, probe) for t in l) for l in lines) <= iw):
            return font, lines, lh
    raise TextTooLong(f"文字太长啦,牌子上写不下:{text!r}")


def _fit_range(text: str, iw: int, ih: int, font_path: Path,
               max_size: float, min_size: float):
    """xixi 举牌的字号搜索(移植 Rust fit_text):从 max_size 往下降到 min_size,
    放不下则退回 min_size 单字换行;仍放不下抛 TextTooLong。返回 (font, lines, lh)。"""
    probe = ImageDraw.Draw(Image.new("L", (1, 1)))
    for size in range(int(max_size), int(min_size) - 1, -1):
        font = _get_font(size, font_path)
        lines = _wrap(text, font, iw)
        lh = int(size * LINE_H)
        if (lines and lh * len(lines) <= ih
                and max(sum(_tok_width(t, font, probe) for t in l) for l in lines) <= iw):
            return font, lines, lh
    font = _get_font(int(min_size), font_path)
    lines = _wrap(text, font, iw)
    lh = int(min_size * LINE_H)
    if not lines or lh * len(lines) > ih:
        raise TextTooLong(f"文字太长啦,牌子上写不下:{text!r}")
    return font, lines, lh


def _bottom_angle(rect) -> float:
    bottom = sorted(rect["corners"], key=lambda p: p[1], reverse=True)[:2]
    left, right = sorted(bottom, key=lambda p: p[0])
    return math.degrees(math.atan2(right[1] - left[1], right[0] - left[0]))


def load_image(src) -> Image.Image:
    """PIL Image / bytes / 本地路径 -> RGBA PIL 图(动图取第一帧)。"""
    if isinstance(src, Image.Image):
        return src.convert("RGBA")
    if isinstance(src, (bytes, bytearray)):
        return Image.open(io.BytesIO(src)).convert("RGBA")
    if isinstance(src, (str, Path)):
        return Image.open(src).convert("RGBA")
    inner = getattr(src, "image", None)         # 兼容 pil_utils.BuildImage 等
    if isinstance(inner, Image.Image):
        return inner.convert("RGBA")
    raise JupaiError(f"不支持的图片类型:{type(src).__name__}")


def _cover_resize(img: Image.Image, w: int, h: int) -> Image.Image:
    """等比缩放图片盖满 (w,h) 再中心裁切(图小于目标时允许放大)。"""
    scale = max(w / img.width, h / img.height)
    resized = img.resize(
        (max(w, round(img.width * scale)), max(h, round(img.height * scale))),
        Image.Resampling.LANCZOS)
    left = (resized.width - w) // 2
    top = (resized.height - h) // 2
    return resized.crop((left, top, left + w, top + h))


def _panel_mask(sign_frame: Image.Image, rect) -> Image.Image:
    """遮罩 = 标定四边形 ∩ 牌面白色像素(亮度>210 渐变过渡):
    贴图只落白面,蓝框、线稿和手保持可见。"""
    polygon = Image.new("L", sign_frame.size, 0)
    ImageDraw.Draw(polygon).polygon(
        [(round(x), round(y)) for x, y in rect["corners"]], fill=255)
    red, green, blue, alpha = sign_frame.convert("RGBA").split()
    white = Image.merge("RGB", (red, green, blue)).convert("L").point(
        lambda v: 0 if v < 210 else min(255, (v - 210) * 6))
    mask = Image.new("L", sign_frame.size, 0)
    mask.paste(white, mask=polygon)
    return Image.composite(mask, Image.new("L", sign_frame.size, 0), alpha)


def _image_layer(rect, img: Image.Image, mask: Image.Image) -> Image.Image:
    """图片 cover 裁切 + 按牌底角度旋转,alpha 乘牌面遮罩后返回整帧 RGBA 层。
    mask 由 _sign_masks 预计算缓存(避免每帧每请求重复 _panel_mask)。"""
    w = max(1, round(float(rect["width"])))
    h = max(1, round(float(rect["height"])))
    face = _cover_resize(img, w, h)
    face = face.rotate(-_bottom_angle(rect), expand=True,
                       resample=Image.Resampling.BICUBIC)
    cx, cy = rect["center"]
    layer = Image.new("RGBA", mask.size, (0, 0, 0, 0))
    layer.alpha_composite(face, (round(cx - face.width / 2), round(cy - face.height / 2)))
    arr = np.asarray(layer).copy()
    mk = np.asarray(mask)
    arr[..., 3] = (arr[..., 3].astype(np.uint16) * mk.astype(np.uint16) // 255).astype(np.uint8)
    return Image.fromarray(arr, "RGBA")


def _shift(arr: np.ndarray, dx: int, dy: int) -> np.ndarray:
    """整块平移(越界丢弃,空位补 0);arr 为 2D 或 3D。"""
    out = np.zeros_like(arr)
    h, w = arr.shape[:2]
    y0, y1 = max(0, dy), min(h, h + dy)
    x0, x1 = max(0, dx), min(w, w + dx)
    out[y0:y1, x0:x1] = arr[y0 - dy:y1 - dy, x0 - dx:x1 - dx]
    return out


def _text_layer(text: str, w: int, h: int, rgb,
                font=None, lines=None, lh=None,
                font_path: Path = FONT_PATH,
                stroke_width: int = 0) -> Image.Image:
    """文字层:文字走单色蒙版、emoji 走彩色位图,混排后整块墨迹居中,
    预乘alpha 精缩到 (w,h) 的 RGBA(防旋转色渗)。

    默认内部 _fit 选字号;xixi 路径可传入 _fit_range 已选好的
    (font, lines, lh, font_path) 复用同一套渲染。
    stroke_width > 0 时文字加同色描边(静态模板用,emoji 不加)。"""
    if font is None:
        font, lines, lh = _fit(text, w, h, font_path)
    mw, mh = w * SS, h * SS
    fss = _get_font(font.size * SS, font_path)
    probe = ImageDraw.Draw(Image.new("L", (1, 1)))
    ascent, descent = fss.getmetrics()
    lhss = int(font.size * SS * LINE_H)
    top = (mh - lhss * len(lines)) // 2

    mask = Image.new("L", (mw, mh), 0)
    md = ImageDraw.Draw(mask)
    emoji_layer = Image.new("RGBA", (mw, mh), (0, 0, 0, 0))
    px_ss = max(4, round(font.size * SS * EMOJI_H))

    for k, toks in enumerate(lines):
        # 相邻文本 token 合并成段;同一段共用基线笔位(anchor="ls")保证行内自然排版
        runs: list[tuple[str, str]] = []
        for kind, val in toks:
            if kind == "t" and runs and runs[-1][0] == "t":
                runs[-1] = ("t", runs[-1][1] + val)
            else:
                runs.append((kind, val))
        widths = []
        for kk, vv in runs:
            if kk == "t":
                widths.append(probe.textlength(vv, font=fss))
            else:
                bm = _emoji_bitmap(vv, px_ss)
                widths.append(float(bm.width) if bm is not None
                              else probe.textlength(vv, font=fss))
        x = (mw - sum(widths)) / 2
        ymid = top + lhss * k + lhss // 2
        y_base = ymid + (ascent + descent) / 2 - descent
        for (kk, vv), tw in zip(runs, widths):
            bm = _emoji_bitmap(vv, px_ss) if kk == "e" else None
            if bm is not None:
                emoji_layer.alpha_composite(bm, (round(x), round(ymid - bm.height / 2)))
            elif stroke_width > 0:
                md.text((x, y_base), vv, font=fss, fill=255, anchor="ls",
                        stroke_width=max(1, round(stroke_width * SS)), stroke_fill=255)
            else:
                md.text((x, y_base), vv, font=fss, fill=255, anchor="ls")
            x += tw

    a_text = np.asarray(mask).astype(float) / 255.0
    e_full = np.asarray(emoji_layer).astype(float)
    a_emoji = e_full[..., 3] / 255.0
    alpha = np.maximum(a_text, a_emoji)

    iys, ixs = np.where(alpha > 0.004)
    if len(iys):
        dx = int(round(mw / 2 - (ixs.min() + ixs.max() + 1) / 2))
        dy = int(round(mh / 2 - (iys.min() + iys.max() + 1) / 2))
        if dx or dy:
            a_text = _shift(a_text, dx, dy)
            e_full = _shift(e_full, dx, dy)

    # 文字蒙版 -> 字色;emoji 预乘缩放防色渗
    text_img = Image.fromarray((a_text * 255).astype(np.uint8), "L").resize(
        (w, h), Image.Resampling.LANCZOS)
    ea = e_full[..., 3:4] / 255.0
    rgbp = Image.fromarray(np.clip(e_full[..., :3] * ea, 0, 255).astype(np.uint8), "RGB").resize(
        (w, h), Image.Resampling.LANCZOS)
    eam = Image.fromarray(e_full[..., 3].astype(np.uint8), "L").resize(
        (w, h), Image.Resampling.LANCZOS)
    e_rgb = np.asarray(rgbp).astype(float)
    e_a = np.asarray(eam).astype(float) / 255.0
    emoji_rgb = np.where(e_a[..., None] > 0, e_rgb / np.maximum(e_a, 1e-6)[..., None], 0.0)

    t_a = np.asarray(text_img).astype(float) / 255.0
    out_a = np.maximum(t_a, e_a)
    out_rgb = np.where((t_a >= e_a)[..., None], np.array(rgb, dtype=float), emoji_rgb)
    return Image.fromarray(
        np.dstack([np.clip(out_rgb, 0, 255).astype(np.uint8),
                   (out_a * 255).astype(np.uint8)]), "RGBA")


def _blank_palette_for(key: str, rgb) -> bytes:
    """纯文字渲染用的空白数据调色板:空白帧量化 246 色(按 key 缓存)
    + 当前文字纯色 1 色 = 247 数据色。这样纯文字渲染跳过逐次 quantize。"""
    if key not in _blank_palette_cache:
        if key in XIXI_HOLDSIGN_KEYS or key in XIXI_GOLDPIG_KEYS:
            blank_frames, _ = _xixi_frames(key)
        else:
            blank_frames, _, _ = _assets(key)
        samples = []
        for f in blank_frames:
            arr = np.asarray(f)
            samples.append(arr[arr[..., 3] >= 128][::11][:20000, :3])
        data = Image.fromarray(np.concatenate(samples).reshape(-1, 1, 3))
        q = data.quantize(246, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE)
        _blank_palette_cache[key] = bytes(q.getpalette()[:246 * 3])
    r, g, b = rgb
    return _blank_palette_cache[key] + bytes((r, g, b))


def _has_emoji(text: str) -> bool:
    """文本里是否含 emoji token(有则不能用空白调色板,要采样进 emoji 颜色)。"""
    return any(kind == "e" for kind, _ in _tokenize(text))


def _encode(frames: list[Image.Image], durs: list[int], rgb,
            data_palette: bytes | None = None) -> bytes:
    """全片共享调色板(247 数据色 + 8 级白->字色 AA 渐变 + 透明槽),无逐帧闪烁。
    data_palette 非 None 时跳过逐帧采样量化(纯文字缓存路径)。"""
    if data_palette is None:
        samples = []
        for f in frames:
            arr = np.asarray(f)
            samples.append(arr[arr[..., 3] >= 128][::11][:20000, :3])
        data = Image.fromarray(np.concatenate(samples).reshape(-1, 1, 3))
        q = data.quantize(247, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE)
        pal = list(q.getpalette()[:247 * 3])
    else:
        pal = list(data_palette[:247 * 3])
    white = np.array([255, 255, 255])
    for t in (i / (RAMP_STEPS + 1) for i in range(1, RAMP_STEPS + 1)):
        c = (white + (np.array(rgb) - white) * t).astype(int)
        pal += [int(c[0]), int(c[1]), int(c[2])]
    pal += [0, 0, 0]                      # 255 = 透明
    pal_img = Image.new("P", (1, 1))
    pal_img.putpalette(pal)

    out_frames = []
    for f in frames:
        p = f.convert("RGB").quantize(palette=pal_img, dither=Image.Dither.NONE)
        arr = np.asarray(p).copy()
        arr[np.asarray(f)[..., 3] < 128] = 255
        p_img = Image.fromarray(arr, "P")
        p_img.putpalette(pal)
        out_frames.append(p_img)
    buf = io.BytesIO()
    out_frames[0].save(
        buf, format="GIF", save_all=True, append_images=out_frames[1:],
        transparency=255, disposal=2, duration=durs, loop=0,
    )
    return buf.getvalue()


def _render_xixi_holdsign(key: str, text: str, rgb) -> bytes:
    """西西举牌 1/2:xixi 全帧 PNG 序列 + 逐帧标定中心/角度贴字。

    文字区与逐帧位姿全部读 assets/xixi/calibration.json(由 Rust 源码提取)。
    字体默认复用插件 assets/font.ttf;用户自放 assets/xixi_font.ttf 时优先使用。
    """
    cal = _xixi_calib()[key]
    text = text or cal["default_text"]
    iw = int(round(cal["text_area_w"]))
    ih = int(round(cal["text_area_h"]))
    font_path = _xixi_font_path()
    font, lines, lh = _fit_range(
        text, iw, ih, font_path, cal["max_font_size"], cal["min_font_size"])
    layer = _text_layer(text, iw, ih, rgb,
                        font=font, lines=lines, lh=lh, font_path=font_path)
    frames, durs = _xixi_frames(key)
    ox, oy = cal["center_offset"]
    out = []
    for i, (cx, cy) in enumerate(cal["centers"]):
        canvas = frames[i].copy()
        ang = cal["angles"][i]
        a = math.radians(ang)
        # Rust: canvas.translate(cx,cy); canvas.rotate(ang);
        #       draw_at(ox - w/2, oy - h/2)  →  块中心=(ox,oy) 再随平面旋转。
        # PIL rotate 正角为逆时针,所以这里用 -ang(与插件既有 _bottom_angle 约定一致)。
        px = ox * math.cos(a) - oy * math.sin(a)
        py = ox * math.sin(a) + oy * math.cos(a)
        rot = layer.rotate(-ang, expand=True, resample=Image.Resampling.BICUBIC)
        canvas.alpha_composite(
            rot, (round(cx + px - rot.width / 2), round(cy + py - rot.height / 2)))
        out.append(canvas)
    if _has_emoji(text):
        return _encode(out, durs, rgb)
    return _encode(out, durs, rgb, data_palette=_blank_palette_for(key, rgb))


def _render_fullframe_holdsign(key: str, text: str, rgb) -> bytes:
    """规格模板(mode=fullframe):整帧 GIF + 逐帧标定贴字。

    标定格式(目录内 calibration.json):
      frames[]: {corners, center:[x,y], angle_deg, width, height, ...}
    文字框 = frame0 的 width/height × meta.text_box_scale(默认 1.0),与 xixi 同思路:
    文字只渲染一次,逐帧按标定中心平移 + 按 angle_deg 旋转贴入。
    """
    text = (text or "").strip()
    if not text:
        text = _template_spec(key).get("default_text") or ""
    if not text:
        raise ValueError("文字为空")
    cal = _template_calib(key)
    rects = cal.get("frames") or []
    if not rects:
        raise JupaiError(f"模板 {key} 标定为空")
    frames, durs = _template_frames(key)
    n = min(len(frames), len(rects))
    if n == 0:
        raise JupaiError(f"模板 {key} 素材为空")
    spec = _template_spec(key)
    sx, sy = tuple(spec.get("text_box_scale") or [1.0, 1.0])[:2]
    f0 = rects[0]
    iw = max(1, round(float(f0["width"]) * sx))
    ih = max(1, round(float(f0["height"]) * sy))
    font_path = _template_font_path(key)
    font, lines, lh = _fit_range(text, iw, ih, font_path,
                                 spec.get("max_font_size", 44.0),
                                 spec.get("min_font_size", 10.0))
    layer = _text_layer(text, iw, ih, rgb,
                        font=font, lines=lines, lh=lh, font_path=font_path)
    out = []
    for i in range(n):
        rect = rects[i]
        canvas = frames[i].copy()
        rot = layer.rotate(-float(rect["angle_deg"]), expand=True,
                           resample=Image.Resampling.BICUBIC)
        cx, cy = rect["center"]
        canvas.alpha_composite(
            rot, (round(float(cx) - rot.width / 2), round(float(cy) - rot.height / 2)))
        out.append(canvas)
    return _encode_template(out, durs[:n], key, rgb)


def _encode_template(frames: list[Image.Image], durs: list[int], key: str, rgb) -> bytes:
    """规格模板专用精确索引编码：

    调色板 = 246 源色（RGBA 帧实用色并集，见 _template_data_palette）+ 文字色
    + 8 级白->字色 AA 渐变 + 透明 = 256 色。源像素按 RGB 精确查表映射（零偏移），
    文字/emoji 像素全调色板欧氏最近邻。绕开 Pillow quantize 的 15 位色键
    近似匹配（会把白抢成 255,253,251 之类的近色）。
    """
    src_arr = np.frombuffer(_template_data_palette(key, rgb)[:246 * 3], np.uint8).reshape(246, 3).astype(np.int32)
    white = np.array([255, 255, 255], np.int32)
    grad = np.array([white + (np.array(rgb, np.int32) - white) * (i / (RAMP_STEPS + 1))
                     for i in range(1, RAMP_STEPS + 1)], np.int32)
    pal_all = np.concatenate([src_arr, np.array(rgb, np.int32).reshape(1, 3), grad], axis=0)
    pal_list = pal_all.astype(np.uint8).reshape(-1).tolist() + [0, 0, 0]

    src_idx = {(int(r) << 16) | (int(g) << 8) | int(b): i
               for i, (r, g, b) in enumerate(src_arr.tolist())}
    out_frames = []
    for f in frames:
        arr = np.asarray(f)
        km = ((arr[..., 0].astype(np.int32) << 16) | (arr[..., 1].astype(np.int32) << 8)
              | arr[..., 2].astype(np.int32))
        uniq, inv = np.unique(km, return_inverse=True)
        idx_for_uniq = np.array([src_idx.get(int(k), -1) for k in uniq.tolist()], np.int32)
        need = idx_for_uniq < 0
        if need.any():
            u = uniq[need].astype(np.int32)
            cols = np.stack([(u >> 16) & 0xFF, (u >> 8) & 0xFF, u & 0xFF], axis=1)
            d2 = ((cols[:, None, :] - pal_all[None, :, :]) ** 2).sum(-1)
            idx_for_uniq[need] = np.argmin(d2, axis=1).astype(np.int32)
        idxmap = idx_for_uniq[inv.reshape(km.shape)]
        idxmap[arr[..., 3] < 128] = 255
        p_img = Image.fromarray(idxmap.astype(np.uint8), "P")
        p_img.putpalette(pal_list)
        out_frames.append(p_img)
    buf = io.BytesIO()
    out_frames[0].save(
        buf, format="GIF", save_all=True, append_images=out_frames[1:],
        transparency=255, disposal=2, duration=durs, loop=0,
    )
    return buf.getvalue()


def _render_template_static(key: str, text: str, rgb) -> bytes:
    """规格模板(mode=static):静态单图贴字,输出 PNG。

    meta 字段:text_size [w,h] 文字画布;pos [x,y] 贴图中心;angle 倾角(度,
    同符号约定:layer.rotate(-angle));stroke_width 描边宽(0=无);
    default_text / min_font_size / max_font_size 同其他模式。
    """
    text = (text or "").strip()
    if not text:
        text = _template_spec(key).get("default_text") or ""
    if not text:
        raise ValueError("文字为空")
    spec = _template_spec(key)
    tw, th = (spec.get("text_size") or [200, 40])[:2]
    iw, ih = max(1, round(float(tw))), max(1, round(float(th)))
    font_path = _template_font_path(key)
    font, lines, lh = _fit_range(text, iw, ih, font_path,
                                 spec.get("max_font_size", 80.0),
                                 spec.get("min_font_size", 5.0))
    layer = _text_layer(text, iw, ih, rgb, font=font, lines=lines, lh=lh,
                        font_path=font_path, stroke_width=int(spec.get("stroke_width", 0)))
    ang = float(spec.get("angle", 0.0))
    rot = layer.rotate(-ang, expand=True, resample=Image.Resampling.BICUBIC) if ang else layer
    frames, _ = _template_frames(key)
    canvas = frames[0].copy()
    px, py = (spec.get("pos") or [canvas.width / 2, canvas.height / 2])[:2]
    canvas.alpha_composite(
        rot, (round(float(px) - rot.width / 2), round(float(py) - rot.height / 2)))
    buf = io.BytesIO()
    canvas.save(buf, format="PNG")
    return buf.getvalue()


def _render_template_goldpig(key: str, img: Image.Image) -> bytes:
    """规格模板(mode=goldpig):图片按 cover 填满圆形窗口,逐帧跟随圆心;
    标定同 xixi_goldpig:{radius, centers:[[cx,cy]×n]}。"""
    cal = _template_calib(key)
    if img is None:
        raise JupaiError("这个表情需要一张图片:请引用一张图片再发指令")
    r = int(round(float(cal["radius"])))
    face = _cover_resize(img, 2 * r, 2 * r)
    mask = Image.new("L", (2 * r, 2 * r), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, 2 * r, 2 * r), fill=255)
    arr = np.asarray(face).copy()
    arr[..., 3] = (arr[..., 3].astype(np.uint16) * np.asarray(mask).astype(np.uint16) // 255).astype(np.uint8)
    face = Image.fromarray(arr, "RGBA")
    frames, durs = _template_frames(key)
    out = []
    for i, (cx, cy) in enumerate(cal["centers"]):
        canvas = Image.new("RGBA", frames[i].size, (0, 0, 0, 0))
        canvas.alpha_composite(face, (round(float(cx)) - r, round(float(cy)) - r))
        canvas.alpha_composite(frames[i])
        out.append(canvas)
    return _encode(out, durs[:len(out)], (255, 255, 255))


def _render_xixi_goldpig(key: str, img: Image.Image) -> bytes:
    """西西摸/西西展示:图片按 cover 填满圆形窗口,逐帧跟随圆心移动;
    模板帧后贴(手指/边框盖在图片上层),与 Rust 版同构。"""
    cal = _xixi_calib()[key]
    if img is None:
        raise JupaiError("这个表情需要一张图片:请引用一张图片再发 西西摸/西西展示")
    r = int(round(cal["radius"]))
    face = _cover_resize(img, 2 * r, 2 * r)
    mask = Image.new("L", (2 * r, 2 * r), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, 2 * r, 2 * r), fill=255)
    arr = np.asarray(face).copy()
    arr[..., 3] = (arr[..., 3].astype(np.uint16) * np.asarray(mask).astype(np.uint16) // 255).astype(np.uint8)
    face = Image.fromarray(arr, "RGBA")
    frames, durs = _xixi_frames(key)
    out = []
    for i, (cx, cy) in enumerate(cal["centers"]):
        canvas = Image.new("RGBA", (300, 300), (0, 0, 0, 0))
        canvas.alpha_composite(face, (round(cx - r), round(cy - r)))
        canvas.alpha_composite(frames[i])
        out.append(canvas)
    return _encode(out, durs, (255, 255, 255))


def render(key: str, text: str, color=None, image=None) -> bytes:
    """渲染一张举牌 GIF。key 见 TEMPLATES;color 见 parse_color();
    image 非 None(PIL/bytes/路径,见 load_image)时把图片铺满牌面白面:
    有文字则在图上叠字,无文字则纯图上牌。

    无图且文字为空抛 ValueError(xixi 模板例外:使用模板默认文字或提示需要图片);
    文字超长抛 TextTooLong;未知模板、素材缺失或图片不可读抛 JupaiError。
    """
    text = (text or "").strip()
    if key not in TEMPLATES:
        raise JupaiError(f"未知模板:{key}")
    rgb = parse_color(color)

    # xixi-rs 移植的 4 个模板:全帧序列,不走 base+sign 双层管线
    if key in XIXI_HOLDSIGN_KEYS:
        return _render_xixi_holdsign(key, text, rgb)
    if key in XIXI_GOLDPIG_KEYS:
        return _render_xixi_goldpig(key, load_image(image) if image is not None else None)

    # 规格模板(assets/templates/,meta.json 描述;见 _scan_template_specs)
    if key in _template_specs:
        mode = _template_specs[key].get("mode", "fullframe")
        if mode == "fullframe":
            return _render_fullframe_holdsign(key, text, rgb)
        if mode == "static":
            return _render_template_static(key, text, rgb)
        if mode == "goldpig":
            return _render_template_goldpig(key, load_image(image) if image is not None else None)
        raise JupaiError(f"模板 {key} 模式未知:{mode}")

    # ---- 原有 娅娅/小爱 双层管线(合并帧缓存 + 遮罩缓存) ----
    if not text and image is None:
        raise ValueError("文字为空")
    merged_frames, durs, rects = _assets(key)
    img = load_image(image) if image is not None else None
    masks = _sign_masks(key) if img is not None else None
    quad = rects[0]
    layer = None
    if text:
        layer = _text_layer(
            text, round(float(quad["width"])) - 2 * PAD,
            round(float(quad["height"])) - 2 * PAD, rgb)

    frames = []
    for i in range(len(rects)):
        canvas = merged_frames[i].copy()
        if img is not None:
            canvas.alpha_composite(_image_layer(rects[i], img, masks[i]))
        if layer is not None:
            rot = layer.rotate(-_bottom_angle(rects[i]), expand=True, resample=Image.Resampling.BICUBIC)
            cx, cy = rects[i]["center"]
            canvas.alpha_composite(rot, (round(cx - rot.width / 2), round(cy - rot.height / 2)))
        frames.append(canvas)

    if img is None and not _has_emoji(text):
        return _encode(frames, durs, rgb, data_palette=_blank_palette_for(key, rgb))
    return _encode(frames, durs, rgb)


# ---------------- 帮助卡片（举牌帮助 渲染成图片） ----------------
_HELP_CARD_W = 860           # 卡片画布宽（竖版：高按内容自适应，宽窄于常规高度）
_HELP_MARGIN = 44            # 左右边距
_HELP_FONTS: dict[int, "ImageFont.FreeTypeFont"] = {}


def _help_font(size: int, font_path: Path | None = None) -> "ImageFont.FreeTypeFont":
    """帮助卡片用字体（独立缓存；默认插件字体）。"""
    key = (str(font_path), size)
    if key not in _HELP_FONTS:
        _HELP_FONTS[key] = ImageFont.truetype(str(font_path or FONT_PATH), size)
    return _HELP_FONTS[key]


def _help_collect(roles: dict, static_cmds: dict | None = None) -> dict:
    """从角色注册表 + 模板规格动态收集帮助内容（加角色/模板自动更新）。"""
    data: dict = {"roles": [], "statics": []}
    for name, info in roles.items():
        nums = ",".join(sorted(info.get("templates", {}), key=lambda s: int(s) if s.isdigit() else 99))
        actions = str(info.get("help_actions") or "").strip()
        dc = info.get("default_color")
        data["roles"].append({
            "cmd": f"{name}举牌{('[1-6]' if nums == '1,2,3,4,5,6' else ('1~' + nums.split(',')[-1] if nums and nums != '1' else ''))}",
            "name": name,
            "actions": actions,
            "color": dc if isinstance(dc, str) else None,
        })
    for cmd, key in (static_cmds or {}).items():
        spec = _template_specs.get(key) or {}
        data["statics"].append({
            "cmd": cmd,
            "default": spec.get("default_text") or "",
            "color": spec.get("default_color") or "#000000",
        })
    return data


def render_banned_list_card(words: list[str],
                            title: str = "违禁词列表") -> bytes:
    """违禁词列表竖版卡片 PNG。words 为空时渲染空态。"""
    F_TITLE = _help_font(44)
    F_BODY = _help_font(28)
    F_MUT = _help_font(22)

    C_INK = (60, 52, 60)
    C_HEAD = (255, 143, 187)
    C_MUT = (128, 120, 130)

    W = 860
    M = 44
    inner = W - M * 2

    # 每词一块（序号 + 词，词超宽折行）
    items: list[list[tuple]] = []
    for i, w in enumerate(words, 1):
        s = f"{i:>2}. {w}"
        lines = []
        while s:
            lo, hi = 1, len(s)
            while lo < hi:
                mid = (lo + hi + 1) // 2
                bbox = F_BODY.getbbox(s[:mid])
                if (bbox[2] - bbox[0]) <= inner - 8:
                    lo = mid
                else:
                    hi = mid - 1
            lines.append(s[:lo])
            s = s[lo:]
        items.append(lines)

    H = M + 70 + 24 + (len(items) * (44 if words else 0)) + 40
    if not words:
        H = M + 190

    img = Image.new("RGB", (W, H), (250, 246, 248))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((16, 16, W - 16, H - 16), radius=26, fill=(255, 255, 255),
                        outline=(240, 234, 238), width=2)
    d.text((M, M), title, font=F_TITLE, fill=C_HEAD)
    y = M + 70
    d.line((M, y, W - M, y), fill=(235, 228, 232), width=2)
    y += 24
    if not words:
        d.text((M, y), "还没有违禁词。添加：举牌添加违禁词 词1 词2 …", font=F_MUT, fill=C_MUT)
    else:
        for lines in items:
            for k, ln in enumerate(lines):
                d.text((M + (0 if k == 0 else 34), y), ln, font=F_BODY, fill=C_INK)
                y += 44
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def render_help_card(roles: dict, static_cmds: dict | None = None,
                     version: str = "", plugin_title: str = "今天你想为娅娅欢呼吗") -> bytes:
    """渲染帮助卡片 PNG。内容全部动态收集：ROLES（含 roles.json 新增角色）、
    STATIC_CMDS（含 assets/templates/ 新增 static 模板）、颜色预设。

    纯 Pillow 绘制，无新依赖；输出宽 1080、高按内容自适应。
    """
    data = _help_collect(roles, static_cmds)
    F_TITLE = _help_font(46)
    F_SEC = _help_font(30)
    F_BODY = _help_font(26)
    F_SMALL = _help_font(22)

    C_INK = (60, 52, 60)           # 正文墨色
    C_MUT = (128, 120, 130)        # 弱化
    C_LINE = (235, 228, 232)       # 分隔线
    C_HEAD = (255, 143, 187)       # 主题粉
    C_CARD = (252, 248, 250)       # 小卡片底

    W = _HELP_CARD_W
    M = _HELP_MARGIN
    inner = W - M * 2

    # ---- 预排版：先算高度再画 ----
    sections: list[tuple[str, list]] = []   # (标题, 行列表)；行 = (字体, 文本, 颜色, 缩进)
    lh_body = 40
    lh_small = 34

    def add_lines(sec_title: str, lines: list):
        sections.append((sec_title, lines))

    # 用法
    usage = [
        (F_BODY, "发「角色举牌 + 编号 + 想说的话」，直接发消息即可触发（无需 @ 或 /）", C_INK, 0),
        (F_BODY, "不带编号默认 1 号动作，如：娅娅举牌 生日快乐 = 娅娅举牌1 生日快乐", C_MUT, 0),
        (F_BODY, "文字末尾 #颜色 换字色：好想回来#粉 / #e74c3c（旧写法 -c 颜色 也认）", C_INK, 0),
        (F_BODY, "引用一张图片再发指令：图片铺满牌面，指令后有文字→图上叠字，无文字→纯图上牌", C_INK, 0),
    ]
    add_lines("怎么用", usage)

    # 角色（动态）
    role_lines = []
    for r in data["roles"]:
        color_tag = f"（默认字色 {r['color']}）" if r["color"] else ""
        role_lines.append((F_BODY, f"{r['cmd']} {color_tag}", C_INK, 0))
        for ln in [x for x in r["actions"].split("\n") if x.strip()]:
            role_lines.append((F_SMALL, "　" + ln, C_MUT, 0))
    if role_lines:
        add_lines("可用角色（含新增，自动更新）", role_lines)

    # 单发表情（动态）
    st_lines = []
    for s in data["statics"]:
        tail = f"，不带文字用默认文案「{s['default']}」" if s["default"] else ""
        st_lines.append((F_BODY, f"{s['cmd']} 想说的话{tail}", C_INK, 0))
    if st_lines:
        add_lines("单发表情（自动更新）", st_lines)

    # 颜色表
    preset_line = " / ".join(NAMED_RGB)
    color_lines = [
        (F_BODY, "预设：" + preset_line, C_INK, 0),
        (F_BODY, "或 6 位色号：#e74c3c", C_MUT, 0),
    ]
    add_lines("颜色", color_lines)

    # 违禁词管理
    ban_lines = [
        (F_BODY, "举牌添加违禁词 词1 词2 … ｜ 举牌删除违禁词 词 ｜ 举牌违禁词列表（列表发图）", C_INK, 0),
        (F_BODY, "群管理员和 bot 主人可用；命中违禁词的文字会被拒绝生成，面板设置里也可查看调整", C_MUT, 0),
    ]
    add_lines("违禁词管理", ban_lines)

    # 塞图
    img_lines = [
        (F_BODY, "西西摸 / 西西展示：引用一张图片再发指令，图片塞进圆形窗口跟着动", C_INK, 0),
        (F_BODY, "带图表情比纯文字版大（约 1MB），发出去稍慢一点", C_MUT, 0),
    ]
    add_lines("塞图上牌", img_lines)

    # ---- 高度计算 ----
    def text_w(font, s):
        return font.getbbox(s)[2] - font.getbbox(s)[0]

    h = M + 76                      # 顶距 + 标题区
    for _, lines in sections:
        h += 30 + 14                # 段标题 + 间距
        for font, s, _, _ in lines:
            wrapped = max(1, -(-text_w(font, s) // inner)) if s else 1
            h += (lh_small if font is F_SMALL else lh_body) * wrapped
        h += 18                     # 段后间距
    h += 44
    H = h

    img = Image.new("RGB", (W, H), (250, 246, 248))
    d = ImageDraw.Draw(img)
    # 圆角白卡
    d.rounded_rectangle((16, 16, W - 16, H - 16), radius=26, fill=(255, 255, 255),
                        outline=(240, 234, 238), width=2)

    y = M
    d.text((M, y), plugin_title, font=F_TITLE, fill=C_HEAD)
    tw = text_w(F_TITLE, plugin_title)
    d.text((M + tw + 18, y + 14), f"v{version}" if version else "", font=F_SMALL, fill=C_MUT)
    y += 76

    for sec_title, lines in sections:
        d.text((M, y), sec_title, font=F_SEC, fill=C_HEAD)
        y += 30
        d.line((M, y + 4, W - M, y + 4), fill=C_LINE, width=2)
        y += 14
        for font, s, color, _ in lines:
            if not s:
                y += lh_small
                continue
            # 超宽折行（按字符粗略二分到宽度内）
            while text_w(font, s) > inner and len(s) > 2:
                lo, hi = 1, len(s)
                while lo < hi:
                    mid = (lo + hi + 1) // 2
                    if text_w(font, s[:mid]) <= inner:
                        lo = mid
                    else:
                        hi = mid - 1
                d.text((M, y), s[:lo], font=font, fill=color)
                s = s[lo:]
                y += lh_small if font is F_SMALL else lh_body
            if s:
                d.text((M, y), s, font=font, fill=color)
            y += lh_small if font is F_SMALL else lh_body
        y += 18

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
