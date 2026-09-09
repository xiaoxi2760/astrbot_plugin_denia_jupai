# 娅娅举牌（astrbot_plugin_denia_jupai）

AstrBot 举牌 GIF 表情插件：把自定义文字或图片生成成娅娅 / 小爱 / 西格莉卡举牌动图，外加「尤诺说」「西西说」单图文字表情。

## 安装

- 面板「插件管理」→「从 GitHub 安装」，仓库地址填 `https://github.com/xiaoxi2760/astrbot_plugin_denia_jupai`
- 或面板「从本地上传安装」，选择 `astrbot_plugin_denia_jupai-*.zip`

依赖 `numpy`、`Pillow`，面板安装时会自动处理。

## 使用

直接发送消息即可，无需 @ 或 `/`：

| 消息 | 效果 |
|---|---|
| `娅娅举牌1 好想回来` | 娅娅出镜。1 眨眼，2 红温，3 开心，4 悲伤，5 期待，6 哭哭 |
| `娅娅举牌 好想回来` | 不带编号默认 1 号动作（等同 `娅娅举牌1`） |
| `小爱举牌1 嘿嘿` | 小爱出镜，编号含义同上 |
| `西格莉卡举牌1 咕噜噜` | 西格莉卡（Sigrika）出镜：1咕噜噜 2点亮语义 3开心 4悲伤 5得意 6哭哭，默认橙字，不带文字用各自默认文案 |
| `尤诺说 月亮游离世间` | 单图文字表情；不带文字用默认文案「月亮游离世间」 |
| `西西说 再发找人弄你` | 单图文字表情；不带文字用默认文案「再发找人弄你」 |
| `西西摸`（引用图片） | 图片塞进圆形窗口跟着动 |
| `西西展示`（引用图片） | 同上，另一个模板 |
| `娅娅举牌1 生日快乐#粉` | 文字颜色改为粉色，也可用 `#e74c3c` 这类 6 位色号 |
| `举牌帮助` | 查看完整帮助 |

其他行为：

- 不带编号默认 1 号动作（如 `娅娅举牌` = `娅娅举牌1`）。
- 引用一张图片再发「娅娅举牌/小爱举牌」，图片会铺满牌面；消息带文字则图上叠字，不带文字则纯图上牌。
- 只发编号时使用面板配置的默认文字；西格莉卡/尤诺说/西西说使用模板自带默认文案。
- 面板可配置默认文字和默认字色。

## 新增角色 / 新增模板（1.8.0 起）

### 方式一：模板目录 + roles.json（推荐，不改代码）

1. 新建模板目录 `assets/templates/{key}/`，放入：
   - `frame.gif` 帧序列（静态图用 `frame.jpg`，文件名可在 meta.json 里改）
   - `calibration.json` 逐帧标定（fullframe 模式：`frames[]` 含每帧 `corners/center/angle_deg/width/height`，可参照 `sigrika_p1`；goldpig 圆窗模式：`radius` + `centers`）
   - `meta.json` 模板描述，例如：

     ```json
     {
       "name": "新角色举牌",
       "mode": "fullframe",
       "min_font_size": 10,
       "max_font_size": 44,
       "text_box_scale": [0.9, 0.66],
       "default_text": "默认文案",
       "default_color": "#ffae2e",
       "font": null
     }
     ```

2. 在 `assets/roles.json` 注册角色（文件不存在就新建）：

   ```json
   {
     "新角色": {
       "templates": {"1": "{key}"},
       "default_color": "#ffae2e",
       "use_template_default": false,
       "image": false,
       "help_actions": "1开心 2悲伤"
     }
   }
   ```

3. 重载插件即可，`core.py` 自动扫描 `assets/templates/` 注册模板，帮助文本自动带上新角色。

### 方式二：改代码

角色集中在 `main.py` 顶部的 `ROLES` 注册表：加一条记录（编号→模板 key、默认字色、是否支持塞图、帮助文案）。静态单图表情（如尤诺说/西西说）加进 `STATIC_CMDS` 一行即可。

### 标定怎么来

`calibration.json` 的逐帧四边形是牌子白面在 300×300 画布上的像素坐标，可用标注工具从 GIF 各帧量取；`angle_deg` 与白面底边平行。整合 Rust 版 meme 素材时，可直接从其源码/数据文件提取（历史脚本 `_build_sigrika_assets.py`、`_build_new_assets.py` 在仓库根目录可参照）。

## 目录结构

```text
main.py            插件入口（角色注册表 ROLES + 静态指令表 STATIC_CMDS）
core.py            合成核心（模板规格扫描 assets/templates/）
assets/
  base/ sign/     娅娅、小爱双层素材 + calibration.json
  xixi/            西西（xixi-rs）素材与标定
  templates/       规格模板目录（sigrika_p1~6、iuno_say、zhaoren_nongni，每个含 frame+calibration+meta）
  roles.json       可选：角色注册配置（不改代码加角色）
  font.ttf         阿里妈妈方圆体（默认字体）
  sigrika_font.ttf 可选：放入后西格莉卡用原版萝莉体渲染
metadata.yaml      插件元数据
_conf_schema.json  面板配置
requirements.txt   第三方依赖
```

## 版权

插件代码以 MIT 许可发布（见 `LICENSE`）；角色形象与素材版权归原作者所有（娅娅/小爱素材与 xixi-rs（Sigrika）素材分别来自对应作者授权整合），仅限个人娱乐使用；字体为阿里妈妈方圆体，可免费商用。
