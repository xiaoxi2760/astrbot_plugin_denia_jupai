# -*- coding: utf-8 -*-
"""今天你想为娅娅欢呼吗 · 自定义文字的举牌 GIF 表情生成。

消息：直接发 `娅娅举牌1`~`娅娅举牌6`、`小爱举牌1`~`小爱举牌6`（编号含义相同：
1眨眼 2红温 3开心 4悲伤 5期待 6哭哭）+ `举牌帮助`，无需 @ 或 `/`。
发「娅娅举牌」不带编号默认无反应（有意不匹配该消息）。
引用一张图片再发消息可把图片铺进牌面：消息后有文字→图上叠字，
没文字→纯图上牌。
核心管线在 core.py（合并版方案：人物层+牌子层分离，仅依赖 numpy/Pillow，
与 AstrBot 解耦，可独立测试；单张 ~0.3-0.4s）。

Handler 铁律：全部 handler 都是 async generator（含 yield）；stop_event() 只放在最后一次 yield 之后。
"""
import re
import json
import asyncio
import tempfile
import time
import uuid
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image as CompImage, Reply as CompReply
from astrbot.api.star import Context, Star, StarTools, register

from .core import JupaiError, TextTooLong, load_image, parse_color, render, split_color_tail, template_default_color, render_help_card

PLUGIN_NAME = "astrbot_plugin_denia_jupai"
VERSION = "1.8.0"

# 角色注册表：新增角色 = 在 ROLES 加一条（或写 roles.json），并准备对应素材 + core.TEMPLATES 的 key。
# 编号含义固定：1眨眼 2红温 3开心 4悲伤 5期待 6哭哭（动作相同，最多牌子颜色/角色不同；
# 不遵循该含义的角色用 help_actions 描述自己的编号动作）。
#   templates:            编号 -> core 模板 key（可只提供部分编号，未提供的编号会提示用户）
#   default_color:        该角色默认字色；None = 用面板配置 default_color
#   use_template_default: True = 不带文字时用模板自带默认文字（如西西举牌）；False = 用面板 default_text
#   image:                True = 支持引用图片铺牌面；False = 不支持
#   help_actions:         帮助文本里的编号动作说明（None = 省略）
ROLES = {
    "娅娅": {
        "templates": {"1": "blink", "2": "hongwen", "3": "kaixin",
                      "4": "beishang", "5": "qidai", "6": "kuku"},
        "default_color": None,
        "use_template_default": False,
        "image": True,
        "help_actions": (
            "1眨眼：wink卖萌，偷偷比心\n"
            "2红温：气鼓鼓炸毛，吐槽、急了、不爽专用\n"
            "3开心：眉开眼笑，报喜、庆祝、夸夸\n"
            "4悲伤：蔫蔫的难过脸，emo、求安慰\n"
            "5期待：搓手手等待，催更、蹲人、等回复\n"
            "6哭哭：眼泪汪汪，委屈、撒娇、求抱抱"),
    },
    "小爱": {
        "templates": {"1": "am_blink", "2": "am_hongwen", "3": "am_kaixin",
                      "4": "am_beishang", "5": "am_qidai", "6": "am_kuku"},
        "default_color": None,
        "use_template_default": False,
        "image": True,
        "help_actions": "1~6 与娅娅完全相同（眨眼/红温/开心/悲伤/期待/哭哭）",
    },
    "西西": {
        "templates": {"1": "sigrika_p1", "2": "sigrika_p2", "3": "sigrika_p3",
                      "4": "sigrika_p4", "5": "sigrika_p5", "6": "sigrika_p6"},
        "default_color": "#ffae2e",
        "use_template_default": False,
        "image": False,
        "help_actions": "6 种动作：3开心 4悲伤 5得意 6哭哭（默认橙字）",
    },
}

# 单发静态表情：指令 -> 模板 key（mode=static，默认文案在模板 meta.json）
STATIC_CMDS = {
    "尤诺说": "iuno_say",
    "西西说": "zhaoren_nongni",
}
_STATIC_PATTERN = r"^(" + "|".join(re.escape(c) for c in STATIC_CMDS) + r")(?:\s+(.*))?$"
_STATIC_RE = re.compile(_STATIC_PATTERN)


def _load_roles_config() -> dict:
    """加载插件目录 assets/roles.json（可选）：覆盖同名角色、新增角色，无需改代码。

    格式与 ROLES 条目一致，例：
      {"新角色": {"templates": {"1": "sigrika_p1"}, "default_color": "#ffae2e",
                  "use_template_default": false, "image": false,
                  "help_actions": "1开心笑 2委屈"}}
    """
    p = Path(__file__).resolve().parent / "assets" / "roles.json"
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        logger.warning(f"{PLUGIN_NAME} roles.json 解析失败（{e!r}），忽略自定义角色")
        return {}
    return data if isinstance(data, dict) else {}


ROLES.update(_load_roles_config())


def _build_help(roles: dict) -> str:
    """从角色注册表自动生成帮助文本（加角色后帮助会自动更新）。"""
    lines = [f"今天你想为娅娅欢呼吗 v{VERSION}"]
    names = "、".join(roles)
    lines.append(f"发「{names}举牌 + 编号 + 想说的话」举牌写字；不带编号默认 1 号动作")
    lines.append(f"（如：娅娅举牌 生日快乐 = 娅娅举牌1 生日快乐；可用角色：{names}）")
    lines.append("动作说明：")
    for r, info in roles.items():
        acts = info.get("help_actions")
        if acts:
            lines.append(f"  {r}：{acts}")
    lines += [
        "单发表情：尤诺说 / 西西说 + 想说的话（如：尤诺说 月亮游离世间；不带文字用默认文案）\n"
        "塞图上牌（底图）：引用一张图片再发指令（或发图时同消息带指令）\n"
        "  底图会等比铺满整个牌面，牌框、花纹和手会盖在图上面\n"
        "  过宽/过长的图按牌面比例居中裁切（宽图裁两侧、竖长图裁上下）\n"
        "  指令后有文字→图上叠字，没文字→纯图上牌（例：回复一张图发「娅娅举牌2」）\n"
        "  图片大小不限，多大都会自动缩到牌面尺寸；GIF 底图只取第一帧\n"
        "  带图的表情比纯文字版大（1MB 左右），发出去会稍慢一点\n"
        "换字色：文字末尾加 #颜色，如 好想回来#粉、#e74c3c（娅娅默认粉；西西默认 #f8b860；西格莉卡默认 #ffae2e）\n"
        "  可用 黑/粉/红/橙/黄/绿/深绿/青/蓝/深蓝/紫/金 或 6 位色号；旧写法 -c 颜色 也认\n"
        "例：娅娅举牌1 好想回来#粉 / 小爱举牌3 生日快乐 / 西格莉卡举牌5 开心#金（图上叠字建议配深色字）\n"
        "群聊直接发即可，无需 @ 机器人或 / 前缀",
    ]
    return "\n".join(lines)

# 用正则而不是 command 过滤器：AstrBot 的 command 默认要求 @ 机器人或唤醒前缀，
# 正则过滤可以让群聊直接发消息就触发（@ 和 / 前缀仍然兼容）。
# 角色名从 ROLES 自动生成；编号可省略（不带编号默认 1 号动作）。
_ROLE_NAMES = "|".join(re.escape(r) for r in ROLES)
COMMAND_PATTERN = rf"^({_ROLE_NAMES})举牌([1-6]?)(?:\s+(.*))?$"
_COMMAND_RE = re.compile(COMMAND_PATTERN)

# 西西摸 / 西西展示：引用图片把图塞进圆形窗口（无文字）
XIXI_TOUCH_PATTERN = r"^西西(摸|展示)$"
_XIXI_TOUCH_RE = re.compile(XIXI_TOUCH_PATTERN)

HELP_TEXT = _build_help(ROLES)


@register(
    PLUGIN_NAME,
    "xiaoxi2760",
    "今天你想为娅娅欢呼吗——自定义文字/图片的举牌 GIF 表情生成（娅娅举牌1~6 / 小爱举牌1~6 / 举牌帮助）",
    VERSION,
)
class JupaiPlugin(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.config = config or {}
        self._cache_dir = None   # 惰性初始化（StarTools 依赖运行期状态）

    # ---------------- 工具 ----------------
    def _get_cache_dir(self) -> Path:
        if self._cache_dir is None:
            try:
                base = Path(StarTools.get_data_dir(PLUGIN_NAME))
            except Exception as e:
                logger.warning(f"{PLUGIN_NAME} 无法获取插件数据目录（{e!r}），退回系统临时目录")
                base = Path(tempfile.gettempdir()) / PLUGIN_NAME
            self._cache_dir = base / "cache"
            self._cache_dir.mkdir(parents=True, exist_ok=True)
        return self._cache_dir

    def _split_color(self, text: str) -> tuple[str, str]:
        """解析文本尾部的颜色标记（#颜色 / -c 颜色）；没有则用面板默认色"""
        text, color = split_color_tail(text)
        return text.strip(), color or str(self.config.get("default_color", "粉"))

    # 静态表情（尤诺说/西西说等）单发指令表：指令 -> 模板 key

    def _cleanup_cache(self, keep_seconds: int = 3600):
        now = time.time()
        for f in self._get_cache_dir().glob("*.gif"):
            try:
                if now - f.stat().st_mtime > keep_seconds:
                    f.unlink()
            except OSError:
                pass

    async def _extract_image(self, event: AstrMessageEvent) -> Path | None:
        """取消息里（含被引用消息）的第一张图片的本地路径；没有则 None。

        引用内容优先用适配器填好的 Reply.chain（新版适配器会自动 get_msg 拉取）；
        chain 为空（旧版适配器不填、或拉取失败）时自己再拉一次兜底。
        """
        chain = list(event.get_messages() or [])
        comps = list(chain)
        for comp in list(comps):                # 适配器填好的引用内容也排进候选
            inner = getattr(comp, "chain", None)
            if inner:
                comps.extend(inner)
        for comp in comps:
            if isinstance(comp, CompImage):
                path = await self._image_to_path(comp)
                if path is not None:
                    return path
        img = await self._quoted_image(event)   # 兜底：主动拉引用消息找图
        if img is not None:
            return await self._image_to_path(img)
        if any(isinstance(c, CompReply) for c in chain):
            logger.info(f"{PLUGIN_NAME} 引用消息里没有图片，按纯文字/默认文字处理")
        return None

    async def _quoted_image(self, event: AstrMessageEvent) -> CompImage | None:
        """Reply.chain 为空时主动调 OneBot get_msg 拉引用消息，找到图构造成 Image 组件。"""
        reply = next((c for c in (event.get_messages() or [])
                      if isinstance(c, CompReply) and getattr(c, "id", None)), None)
        if reply is None:
            return None
        bot = getattr(event, "bot", None)       # aiocqhttp 平台事件才有 bot
        if bot is None:
            logger.info(f"{PLUGIN_NAME} 当前平台不支持主动拉取引用消息，按纯文字处理")
            return None
        try:
            data = await bot.call_action(action="get_msg", message_id=int(reply.id))
        except Exception as e:
            logger.warning(f"{PLUGIN_NAME} 拉取引用消息失败（get_msg）: {e!r}")
            return None
        for seg in data.get("message") or []:
            if not isinstance(seg, dict) or seg.get("type") != "image":
                continue
            d = seg.get("data") or {}
            if d.get("url"):
                return CompImage(file=d.get("file") or d["url"], url=d["url"])
            file_id = d.get("file") or d.get("file_id")
            if file_id:                          # 只有文件名时问一次 get_image 拿直链
                try:
                    ret = await bot.call_action(action="get_image", file=file_id)
                    if ret and ret.get("url"):
                        return CompImage(file=file_id, url=ret["url"])
                except Exception as e:
                    logger.warning(f"{PLUGIN_NAME} 获取图片直链失败（get_image）: {e!r}")
        return None

    async def _image_to_path(self, comp) -> Path | None:
        """图片组件 -> 本地文件路径；优先 convert_to_file_path（自动下载/转存）。"""
        try:
            p = await comp.convert_to_file_path()
            if isinstance(p, (tuple, list)):
                p = p[0]
            if p and Path(str(p)).is_file():
                return Path(p)
        except Exception as e:
            logger.warning(f"{PLUGIN_NAME} 获取图片失败（convert_to_file_path）: {e!r}")
        f = getattr(comp, "file", None)
        if f and Path(str(f)).is_file():
            return Path(str(f))
        return None

    async def _make(self, event: AstrMessageEvent, template_key: str, text: str,
                    color: str, image_path: Path | None = None,
                    template_default: bool = False):
        """渲染并发图；任何错误都转成可读文本回复。
        image_path 非 None 时图片铺进牌面：有文字图上叠字，无文字纯图。
        template_default=True 时空文字交给 core 用模板自带默认文字（如西西举牌）。"""
        text = (text or "").strip()
        if not text and image_path is None and not template_default:
            text = str(self.config.get("default_text", "生日快乐！"))
        try:
            rgb = parse_color(color)
        except ValueError as e:
            yield event.plain_result(str(e))
            return
        loop = asyncio.get_running_loop()
        img = None
        if image_path is not None:
            try:
                img = await loop.run_in_executor(None, load_image, image_path)
            except Exception as e:
                logger.error(f"{PLUGIN_NAME} 图片读取失败: {e!r}")
                yield event.plain_result("图片读取失败，换一张试试吧")
                return
        try:
            data = await loop.run_in_executor(None, render, template_key, text, rgb, img)
        except TextTooLong:
            yield event.plain_result(
                f"文字太长啦，牌子上写不下（当前 {len(text)} 字），换短一点的试试吧")
            return
        except JupaiError as e:
            yield event.plain_result(f"举牌生成失败：{e}")
            return
        except Exception as e:
            logger.error(f"{PLUGIN_NAME} 渲染异常: {e!r}")
            yield event.plain_result("举牌生成失败，详情见控制台日志")
            return

        self._cleanup_cache()
        ext = "png" if data[:8] == b"\x89PNG\r\n\x1a\n" else "gif"
        out = self._get_cache_dir() / f"{uuid.uuid4().hex}.{ext}"
        out.write_bytes(data)
        yield event.image_result(str(out))

    # ---------------- 举牌消息 ----------------
    @filter.regex(COMMAND_PATTERN)
    async def jupai_cmd(self, event: AstrMessageEvent):
        """娅娅举牌/小爱举牌/西西举牌 + 可选编号（不带编号默认 1）+ 可选文字。
        直接发消息即可触发，无需 @ 或 /。新增角色只需在 ROLES 加一条。"""
        m = _COMMAND_RE.match(event.get_message_str().strip())
        if not m:
            return
        role, num = m.group(1), (m.group(2) or "1")
        text = re.sub(r"\s+", " ", (m.group(3) or "")).strip()
        info = ROLES[role]
        template_key = info["templates"].get(num)
        if template_key is None:
            nums = "、".join(info["templates"].keys())
            yield event.plain_result(f"{role}举牌只支持编号：{nums}")
            event.stop_event()
            return
        img = await self._extract_image(event) if info.get("image", True) else None
        t, c = split_color_tail(str(text))
        if not c:
            c = info.get("default_color") or str(self.config.get("default_color", "粉"))
        async for r in self._make(event, template_key, t, c, img,
                                  template_default=info.get("use_template_default", False)):
            yield r
        event.stop_event()

    # ---------------- 西西摸/西西展示（图片塞圆窗） ----------------
    @filter.regex(XIXI_TOUCH_PATTERN)
    async def xixi_goldpig_cmd(self, event: AstrMessageEvent):
        """西西摸 / 西西展示：引用一张图片再发指令，图片塞进圆形窗口跟着动。"""
        m = _XIXI_TOUCH_RE.match(event.get_message_str().strip())
        if not m:
            return
        template_key = "xixi_goldpig" if m.group(1) == "摸" else "xixi_goldpig_2"
        img = await self._extract_image(event)
        if img is None:
            yield event.plain_result("这个表情需要一张图片：请引用一张图片再发 西西摸/西西展示")
            event.stop_event()
            return
        async for r in self._make(event, template_key, "", "#f8b860", img,
                                  template_default=True):
            yield r
        event.stop_event()

    # ---------------- 尤诺说/西西说（静态单图贴字） ----------------
    @filter.regex(_STATIC_PATTERN)
    async def static_cmd(self, event: AstrMessageEvent):
        """尤诺说 / 西西说 + 可选文字（不带文字用模板默认文案）；输出 PNG。"""
        m = _STATIC_RE.match(event.get_message_str().strip())
        if not m:
            return
        template_key = STATIC_CMDS[m.group(1)]
        text = re.sub(r"\s+", " ", (m.group(2) or "")).strip()
        t, c = split_color_tail(str(text))
        if not c:
            # 尤诺说/西西说等静态模板用自己的默认字色（meta.default_color，如黑色），
            # 不吃面板默认粉色
            c = (template_default_color(template_key)
                 or str(self.config.get("default_color", "粉")))
        async for r in self._make(event, template_key, t, c, None,
                                  template_default=True):
            yield r
        event.stop_event()

    # ---------------- 帮助 ----------------
    @filter.regex(r"^举牌帮助$")
    async def help_cmd(self, event: AstrMessageEvent):
        """举牌帮助：渲染帮助卡片图片（内容随角色/模板注册表自动更新）"""
        loop = asyncio.get_running_loop()
        data = await loop.run_in_executor(
            None, render_help_card, ROLES, STATIC_CMDS, VERSION)
        self._cleanup_cache()
        out = self._get_cache_dir() / f"help_{uuid.uuid4().hex}.png"
        out.write_bytes(data)
        yield event.image_result(str(out))
        event.stop_event()
