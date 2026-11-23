
<div align="center">

![:name](https://count.getloli.com/@astrbot_plugin_parser_qqofficial?name=astrbot_plugin_parser_qqofficial&theme=minecraft&padding=6&offset=0&align=top&scale=1&pixelated=1&darkmode=auto)

# astrbot_plugin_parser_qqofficial

_✨ 链接解析器（QQ 官方机器人适配版） ✨_  

[![License](https://img.shields.io/badge/License-MIT-green.svg)](https://opensource.org/licenses/MIT)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](https://www.python.org/)
[![AstrBot](https://img.shields.io/badge/AstrBot-3.4%2B-orange.svg)](https://github.com/Soulter/AstrBot)
[![GitHub](https://img.shields.io/badge/GitHub-BUGJI-blue)](https://github.com/BUGJI/astrbot_plugin_parser_qqofficial)
[![Original](https://img.shields.io/badge/上游-Zhalslar%2Fastrbot__plugin__parser-lightgrey)](https://github.com/Zhalslar/astrbot_plugin_parser)

</div>

## 📌 关于本项目

> **上游仓库已停止更新（原仓库已删除）**。本项目基于上游 `Zhalslar/astrbot_plugin_parser` **最后一个版本**二次修改，并持续适配 **QQ 官方机器人（`qq_official` / `qq_official_webhook`）**。
>
> - 本仓库：<https://github.com/BUGJI/astrbot_plugin_parser_qqofficial>
> - 上游原地址（已失效）：<https://github.com/Zhalslar/astrbot_plugin_parser>
>
> 如上游日后恢复，请自行比对差异；本仓库独立维护，不自动跟踪上游。

## 📖 介绍

内置解析器适配矩阵（需在 `parsers_template` 中启用才会生效；仓库中未注册的解析器如 `jmcomic`、`pixiv`、`iwara`、`nhentai` 不在此列）：

| 平台 | 触发的消息形态 | 视频 | 图集 | 音频 | 备注 |
| --- | --- | :---: | :---: | :---: | --- |
| B 站 | `av` / `BV` / `bmBV`、视频/动态/直播/收藏夹/专栏/Opus 链接、短链 | ✅ | ✅ | ✅ | `bmBV` 提取音频；多 P 视频可在编号后指定分 P |
| 抖音 | 分享短链、电脑端链接、`aweme_id`、作品 ID | ✅ | ✅ | ❌ | 支持视频与图文作品，部分内容需要 Cookie |
| 快手 | 标准链接、`v.kuaishou.com` 短链、`chenzhongtech.com` 链接 | ✅ | ✅ | ❌ | 支持视频与图集，登录态可提高解析成功率 |
| QQ 空间 | `h5.qzone.qq.com/ugc/share`、`mobile.qzone.qq.com/l` 公开分享链接/卡片 | ✅ | ✅ | ❌ | 默认关闭；可选 SnowLuma 自动凭证或手动 Cookie，保留公开页面回退 |
| 微信视频号 | `weixin.qq.com/sph` 短链、带 `token`/`eid` 的预览长链 | ✅ | ❌ | ❌ | 短链需要腾讯元宝 Cookie；预览长链自带凭证时可直接解析 |
| 微博 | 博文、视频、`show`、文章及分享链接 | ✅ | ✅ | ❌ | Cookie 可提升受限内容和登录态页面的可用性 |
| 小红书 | 标准链接、`xhslink.com` / `xhslink.cn` 短链、卡片 | ✅ | ✅ | ❌ | 支持视频笔记与图文笔记，建议配置 Cookie |
| 小黑盒 | 社区帖子、分享链接、游戏详情链接/卡片 | ✅ | ✅ | ❌ | 支持图文混排；可配置正文、原图和视频发送方式 |
| 知乎 | 专栏、问题、回答、想法链接/卡片 | ✅ | ✅ | ❌ | 解析正文中的图片和视频，登录态可改善受限页面访问 |
| TikTok | 标准链接、`vt.tiktok.com` / `vm.tiktok.com` 短链 | ✅ | ✅ | ❌ | 照片集使用图文接口解析，失败时会在视频与图文路径间回退 |
| YouTube | 视频、Shorts、`youtu.be` 短链；`ym` + 链接 | ✅ | ❌ | ✅ | 普通链接下载视频；`ym` 前缀提取音频，通常需要 Cookie |
| Metube | YouTube 视频、Shorts、`youtu.be` 短链 | ✅ | ❌ | ❌ | 需要独立 Metube 服务；启用后接管 YouTube 视频路由，`ym` 音频仍由 YouTube 解析器处理 |
| Instagram | `p` / `reel` / `reels` / `tv` / `share` 链接 | ✅ | ✅ | ❌ | 支持帖子、图集和 Reels；Cookie 可提高私有或受限内容成功率 |
| Twitter / X | `twitter.com` / `x.com` 状态链接 | ✅ | ✅ | ❌ | 通过解析服务提取推文视频、图片和引用内容 |
| AcFun | `ac` 号或稿件链接 | ✅ | ❌ | ❌ | 下载并合并 HLS 视频流，Cookie 为可选项 |
| 网易云音乐 | 歌曲链接、`163cn.tv` 短链、MP3/外链播放地址 | ❌ | ❌ | ✅ | 支持歌曲元数据、封面和音频直链，部分资源受版权或登录态限制 |
| NGA | `ngabbs.com` / `nga.178.com` / `bbs.nga.cn` 主题链接 | ❌ | ✅ | ❌ | 返回主题首帖文本与图片；登录可见内容需要 Cookie |
| CPP 无差别同人站 | `icp.red` 短链、展品 `/d/`、图文 `/w/` 链接 | ❌ | ✅ | ❌ | 未登录时展品试阅会退化为仅封面；正文显示长度可配置 |

本插件目标：覆盖常见内容平台，已支持范围见上表，其余平台持续扩充中，欢迎提交 PR。

> **本仓库为上游最后版本的二改分支，仅适配 QQ 官方机器人（`qq_official` / `qq_official_webhook`）**，仅支持 QQ 群聊与私聊（C2C），不包含 OneBot/aiocqhttp 通道，也不支持频道（guild）。
>
> OneBot / aiocqhttp 用户请勿使用本仓库，本仓库不保证兼容。

---

## 💿 安装

1. 将本目录放到 AstrBot 的 `data/plugins/astrbot_plugin_parser_qqofficial`。
2. 在 AstrBot 的「平台」中配置 **QQ 官方机器人**（WebSocket 或 Webhook）。
3. 重启 AstrBot，在插件配置面板按需调整参数。

也可在 AstrBot 插件市场通过本仓库地址安装：

```bash
# 在 AstrBot 插件管理中使用仓库地址安装
https://github.com/BUGJI/astrbot_plugin_parser_qqofficial
```

> 本插件与 OneBot 版 `astrbot_plugin_parser` 可共存，使用独立的数据目录与配置。

## ⚙️ 配置

在 AstrBot 的插件配置面板中按分组调整，常用项如下（括号内为默认值）。

### 基础与访问控制

| 配置项 | 说明 |
| --- | --- |
| `whitelist` / `blacklist` | 解析白名单 / 黑名单（填写会话 ID），白名单非空时仅白名单会话生效；黑名单优先于白名单 |
| `require_at_in_group`（开） | 群聊消息必须 @ 机器人，否则不触发 |
| `enable_reply_parse`（关） | 开启后，引用消息中的链接也可被解析 |
| `debounce_interval`（300） | 同一会话内相同链接的防抖秒数，`0` 表示不防抖 |

### 媒体与下载

| 配置项 | 说明 |
| --- | --- |
| `source_max_size`（90） | 允许下载的媒体最大体积（MB） |
| `source_max_minute`（15） | 允许下载的视频最大时长（分钟） |
| `audio_to_file`（开） | 音频以文件形式上传，关闭则走语音 |
| `single_heavy_render_card`（关） | 单条重媒体（视频/音频/文件）也渲染为卡片 |
| `forward_threshold`（2） | 图集/多媒体的分组阈值 |
| `show_download_fail_tip`（开） | 下载失败时提示失败项 |
| `download_timeout`（280） | 媒体下载超时秒数 |
| `download_retry_times`（2） | 媒体下载失败重试次数 |
| `common_timeout`（15） | 普通 API 请求超时秒数 |

### QQ 官方发送

| 配置项 | 说明 |
| --- | --- |
| `qq_gallery_render_card`（开） | 图集渲染为卡片 |
| `qq_card_max_images`（9） | 每张卡片最多图片数，超额自动拆分多卡 |
| `qq_image_format`（jpg） | 图片发送格式（jpg / png / 原格式） |
| `qq_native_card`（开） | 满足条件时使用官方图文卡片（`msg_type=8`），缺封面或非 https 链接自动回退 |
| `qq_quote_reply`（关） | 解析结果以引用形式回复触发消息 |
| `qq_keyboard`（开） | 附带内嵌键盘（`qq_keyboard_open_link` / `qq_keyboard_reparse` 控制按钮） |
| `qq_markdown`（auto） | Markdown 发送模式：`auto` / 开 / 关 |
| `qq_material_upload`（关） | 卡片上传官方素材库，以 Markdown 图片发送，失败回退为图片 |
| `qq_reply_budget_override`（0） | 被动回复次数上限，`0` 使用官方默认（私聊 4 / 群聊 5） |
| `qq_overflow_mode`（off） | 额度用尽后的投递方式：`off` / `proactive`（主动消息）/ `private`（群转私聊） |
| `qq_overflow_limit`（3） | 溢出投递的最大条数 |

### 网络与缓存

| 配置项 | 说明 |
| --- | --- |
| `proxy`（空） | 下载/请求代理地址 |
| `clean_cron`（`30 2 * * *`） | 自动清理缓存的 Cron 周期 |

### 解析器

`parsers_template` 按平台分别配置，常见字段：`enable`（启用）、`cookies`（登录态）、`use_proxy`（走代理），以及各平台专属项（如 B 站 `video_quality` / `video_codec_list` / `block_pcdn`，Metube `metube_url` / `video_codec` / `video_format`，网易云 `audio_quality` / `audio_quality_fallback`，小黑盒 `show_body_text` / `use_original_image` / `mixed_layout` / `video_send_mode`）。

## 🎉 指令

|   指令   |         权限          |        说明        |
| :------: | :-------------------: |  :---------------: |
| 开启解析 |      ADMIN            |     开启当前会话的解析功能      |
| 关闭解析 |      ADMIN            |    关闭当前会话的解析功能      |
| 解析设置 |      用户            | 展示 Markdown 设置面板，可切换画质/音质/原图偏好与菜单开关 |
| 解析帮助 |      用户            | 展示命令帮助页（别名 `/帮助`） |
| 解析登录 |      ADMIN            | 统一登录入口：`/解析登录` 查看帮助，`/解析登录 状态` 汇总各平台状态，`/解析登录 B站 扫码`，`/解析登录 网易云 手机/短信/邮箱/扫码/刷新` |
| 撤回 |      ADMIN            | 撤回机器人自己发送的消息：`/撤回 [消息ID]`，不填 ID 时撤回最近一次直发消息（官方限制 2 分钟内，别名 `/撤回解析`） |
| 画质菜单 |      用户            | 按用户开关解析后的画质/音质菜单：`/画质菜单 开`、`/画质菜单 关`、`/画质菜单 状态`（别名 `/画质面板`、`/音质菜单`、`/音质面板`，默认关闭） |
| 画质 |      用户            | 设置个人视频画质偏好：`/画质 _1080P`、`/画质 720`、`/画质 默认`、`/画质 查看` |
| 音质 |      用户            | 设置个人音频音质偏好：`/音质 lossless`、`/音质 hires`、`/音质 默认`、`/音质 查看` |
| 原图 |      用户            | 设置个人图片偏好：`/原图 开`（原图）、`/原图 关`（压缩）、`/原图 默认`、`/原图 查看` |

---

## 🧠 插件工作流程

当插件运行后，每一条消息的处理流程如下：

1. **消息接收**  
   监听所有消息事件，获取消息链与原始文本内容  
   - 支持普通文本、链接、结构化卡片（`ark_data`）

2. **基础过滤**  
   - 跳过已被禁用的会话  
   - 跳过空消息  
   - 跳过频道（guild）消息  
   - 群聊默认必须 @ 本机器人；@ 了其他机器人则不解析

3. **链接提取与匹配**  
   - 若为卡片消息，优先从卡片文本 / `ark_data` 中提取 `jump_url` 等跳转链接  
   - 使用「关键词 + 正则」双重匹配，定位对应解析器  
   - 未匹配到解析规则则直接退出

4. **防抖判定（Link Debouncer）**  
   - 对同一会话内的相同链接进行时间窗口限制  
   - 命中防抖规则则跳过解析，避免短时间重复处理

5. **内容解析**  
   - 调用对应平台解析器获取媒体信息  
   - 生成统一的 `ParseResult` 数据结构

6. **媒体下载与消息构建**  
   - 下载视频 / 图片 / 音频 / 文件  
   - 根据配置决定音频发送方式  
   - 可按配置提示下载失败项

7. **卡片渲染**  
   - 图集统一渲染为卡片（每卡最多 9 图），超额自动拆分多卡  
   - 使用 PIL 渲染并缓存图片

8. **预算感知发送**  
   - QQ 官方单条消息仅支持一个富媒体  
   - 被动回复次数有限（私聊 4 次 / 群聊 5 次），发送前统一规划，避免超限  
   - 不适用合并转发；纯文本强制关闭 Markdown

---

## 🧩 扩展

插件支持自定义解析器，通过继承 `BaseParser` 类并实现 `platform`, `handle` 即可。

示例解析器请看 [core/parsers/example.py](core/parsers/example.py)。

---

## 🎉 致谢

- 本项目初始版本改自 [nonebot-plugin-parser](https://github.com/fllesser/nonebot-plugin-parser) 插件，请前往原仓库给作者点个 Star！
- 本项目由 [Zhalslar/astrbot_plugin_parser](https://github.com/Zhalslar/astrbot_plugin_parser)（原仓库已删除）最后版本二改而来，感谢原作者 **Zhalslar** 的贡献。

## 🤪 注意

- 上游仓库已删库停更，本仓库为独立维护的 QQ 官方适配分支；上游相关链接可能全部失效。
- 如需更丰富的解析器，这里推荐一个另外维护的分支：<https://github.com/huawubot/astrbot_plugin_parser>（非本仓库，仅作参考）。
- 提交 Issue / PR 请前往本仓库：<https://github.com/BUGJI/astrbot_plugin_parser_qqofficial>。
- 版本变更见 [CHANGELOG.md](CHANGELOG.md)。


---

## ✨ 官方能力增强

- **图文卡片（`msg_type=8`）**：单作品/预览在具备 https 封面与链接时，改用官方原生图文卡片，点击可回到原内容；缺封面或非 https 链接自动回退为渲染卡片。
- **引用回复**：`qq_quote_reply` 开启后，解析结果以引用形式回复触发消息。
- **内嵌键盘**：默认附带「打开原链接 / 重新解析」按钮（回调按钮点击后重新解析并发送）。
- **画质/音质菜单**：用户可用 `/画质菜单 开` 启用；源文件发送后追加一条菜单，可切换音频的「文件/语音」与其他档位。同一文件在有效期内只弹一次，按钮仅发起者可点。Metube 档位可能需要重新下载，耗时较长。
- **个人媒体偏好**：`/画质`、`/音质`、`/原图` 可分别保存个人档位偏好；解析时优先级为：菜单/命令显式选择 > 用户偏好 > 平台全局配置 > 内置默认。视频与音质偏好按平台隔离、图片偏好全局共享，统一保存于插件数据目录 `preferences.json`。
- **Markdown**：`qq_markdown=auto` 时，带标题/链接的文本使用 Markdown 发送，链接可点击。
- **素材管理**：`qq_material_upload` 开启后，把渲染卡片上传到官方素材库（`qqbot-media://`），以 Markdown 图片发送，可与键盘组合；失败自动回退为图片。
- **额度溢出投递**：`qq_overflow_mode` 可选 `proactive`（主动消息续发）或 `private`（群聊转私聊发给触发者），最多补发 `qq_overflow_limit` 条，避免图集/多视频被截断。
- **撤回**：管理员可用 `/撤回 [消息ID]` 撤回机器人自己发送的消息（官方限制 2 分钟内；不填 ID 时撤回最近一次直发的图文卡片）。

---

## ⚠️ QQ 官方适配限制

- **仅 QQ 群聊 + 私聊（C2C）**，不处理频道（guild）消息。
- **无表情仲裁**：官方通道的「表情表态」仅频道支持，群聊/私聊无法贴表情。多机器人同群时请依赖「必须 @ 机器人」来区分，插件不再做跨 Bot 仲裁。
- **无合并转发**：官方发送仅支持文本 / Markdown / 富媒体，原「合并转发」策略已替换为「图集卡片化 + 回复额度控制」。
- **被动回复次数**：单聊 4 次、群聊 5 次（可在插件配置中通过 `qq_reply_budget_override` 调大，`0` 表示使用官方默认）。图集超过额度时会自动拆分卡片或省略多余图片。
- **画质菜单额度**：菜单消息会额外占用 1 次被动回复额度；额度不足时只发源文件，不再弹菜单。
- **卡片链接**：小程序卡片可能只提供预览图而无跳转链接，此时无法解析，只能依赖正文里的裸链接。
- **图片格式**：发送前会按配置转换为 jpg/png，动图取首帧。
