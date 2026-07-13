# music-to-bb

酷狗音乐歌单 → Bilibili 收藏夹自动转换工具。

输入酷狗概念版歌单链接，自动解析歌曲列表、搜索 Bilibili 视频、智能匹配并一键添加到你的 B 站收藏夹。

## 功能

- **歌单解析** — 支持 HTTP API 和 Playwright 浏览器双引擎，兼容各种酷狗分享链接
- **智能匹配** — 多维评分算法（关键词、音质标签、官方认证、播放量/收藏量）自动匹配最佳视频
- **歌手证据检测** — 自动匹配缺失歌手证据时标记需审核
- **双模式** — CLI 命令行 + GUI 图形界面，按需选择
- **并发匹配** — 多线程并发搜索，可通过 `--workers` 控制并发数
- **扫码登录** — Bilibili 二维码登录，Cookie 本地持久化
- **手动审核** — 支持自动匹配后逐首审核，或完全手动选择视频（支持 BV 号直接输入）
- **可定制** — 屏蔽词(b.txt)、加权词(w.txt)、UP主加权(w-up.txt) 均可自行编辑
- **HTTP重试** — 带指数退避的自动重试机制

## 安装

```bash
# 克隆仓库
git clone https://github.com/gguage/music-to-bb.git
cd music-to-bb

# 安装依赖
pip install -r requirements.txt

# 安装 Playwright 浏览器（GUI/浏览器解析模式需要）
playwright install chromium
```

## 使用

### GUI 模式

```bash
python main.py gui
```

### CLI 模式

```bash
# 基本转换
python main.py convert "https://m.kugou.com/share/zlist.html?id=xxx"

# 增加搜索页数提高匹配率
python main.py convert "链接" --search-pages 5

# 使用并发加速
python main.py convert "链接" --workers 8

# 指定目标收藏夹
python main.py convert "链接" --favorite "我的收藏"

# 自动匹配后逐首审核
python main.py convert "链接" --manual-review

# 完全手动匹配
python main.py convert "链接" --manual

# 跳过确认直接添加
python main.py convert "链接" --yes

# 强制使用浏览器解析
python main.py convert "链接" --browser always
```

### 其他命令

```bash
# 登录 Bilibili
python main.py login

# 列出收藏夹
python main.py favorites list

# 创建收藏夹
python main.py favorites create "新建收藏夹" --intro "简介" --private

# 浏览器状态
python main.py browser status
python main.py browser install
python main.py browser clear

# 版本
python main.py version
```

## 配置文件

| 文件 | 作用 |
|------|------|
| `b.txt` | 屏蔽关键词（翻唱、伴奏、cover、教程等），匹配到则跳过该视频 |
| `w.txt` | 加权关键词（官方、MV、无损、4K、Hi-Res等），匹配到则加分 |
| `w-up.txt` | UP主加权列表，指定UP主的视频优先匹配 |

## 项目结构

```
music-to-bb/
├── main.py          # 入口：CLI 参数解析
├── core.py          # 核心流程：解析→匹配→收藏
├── kugou.py         # 酷狗歌单爬取（HTTP API + Playwright）
├── bilibili.py      # Bilibili API 客户端（搜索/收藏/登录）
├── matcher.py       # 视频匹配评分引擎
├── models.py        # 数据模型
├── netx.py          # HTTP 客户端（重试/限速）
├── config.py        # 配置路径解析与迁移
├── errors.py        # 错误分类与退出码
├── gui.py           # GUI 界面（CustomTkinter）
├── manual_match.py  # 交互式手动匹配
├── b.txt            # 屏蔽关键词
├── w.txt            # 加权关键词
├── w-up.txt         # UP主加权列表
└── requirements.txt

## 许可证

GNU Affero General Public License v3.0 (AGPL-3.0)

```
